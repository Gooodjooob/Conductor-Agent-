"""Per-attempt fixed-provider gateway. Never a general HTTP/CONNECT proxy.

Worker receives a random local capability, never the provider credential. The
OS sandbox must restrict outbound connections to this gateway's exact port.
This module uses only explicitly supplied credentials and never reads env.
"""
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import secrets
import socket
import ssl
import threading
from .claude import strict_object


PROVIDERS = {
    "anthropic": ("api.anthropic.com", frozenset(("/v1/messages", "/v1/messages?beta=true", "/v1/messages/count_tokens", "/v1/messages/count_tokens?beta=true"))),
    "deepseek": ("api.deepseek.com", frozenset(("/chat/completions",))),
}


class EgressError(RuntimeError):
    pass


def redact_provider_secret(data, secret):
    if not isinstance(data, (bytes, bytearray)) or not isinstance(secret, bytes) or not secret:
        raise EgressError("INVALID_REDACTION_INPUT")
    return bytes(data).replace(secret, b"[redacted]")


class FixedUpstreamProxy:
    MAX_BODY = 4 * 1024 * 1024
    MAX_RESPONSE = 16 * 1024 * 1024

    def __init__(self, provider, credential, model, port=0, timeout=30):
        if provider not in PROVIDERS:
            raise EgressError("FIXED_PROVIDER_REQUIRED")
        if not isinstance(credential, str) or not 8 <= len(credential) <= 4096 or any(c in credential for c in "\r\n\x00"):
            raise EgressError("INVALID_PROVIDER_CREDENTIAL")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", model):
            raise EgressError("FIXED_MODEL_REQUIRED")
        if type(port) is not int or not 0 <= port <= 65535 or not 0 < timeout <= 120:
            raise EgressError("INVALID_PROXY_BOUND")
        self.provider, self.model = provider, model
        self._credential, self.capability = credential, secrets.token_urlsafe(36)
        self.host, self.paths = PROVIDERS[provider]
        self.timeout, self.port = timeout, port
        self.server = self.thread = None
        self._slots = threading.BoundedSemaphore(4)

    def start(self):
        if self.server:
            raise EgressError("PROXY_ALREADY_STARTED")
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass  # Request URLs/headers/prompts never enter standard logs.

            def setup(self):
                super().setup()
                self.connection.settimeout(proxy.timeout)

            def reject(self, code=403):
                body = b'{"error":{"type":"egress_denied","message":"request denied by fixed upstream gateway"}}'
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True

            def do_POST(self):
                if not proxy._slots.acquire(blocking=False):
                    return self.reject(429)
                connection = None
                try:
                    expected_host = "127.0.0.1:" + str(proxy.port)
                    if (self.path not in proxy.paths or self.headers.get_all("Host") != [expected_host]
                        or self.headers.get("Origin") or self.headers.get("Transfer-Encoding")
                        or self.headers.get("Content-Encoding") or self.headers.get("Expect")):
                        return self.reject()
                    key_headers = self.headers.get_all("x-api-key", [])
                    auth_headers = self.headers.get_all("Authorization", [])
                    supplied = key_headers[0] if len(key_headers) == 1 and not auth_headers else (
                        auth_headers[0][7:] if len(auth_headers) == 1 and not key_headers and auth_headers[0].startswith("Bearer ") else "")
                    if not hmac.compare_digest(supplied, proxy.capability):
                        return self.reject(401)
                    lengths = self.headers.get_all("Content-Length", [])
                    if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= proxy.MAX_BODY:
                        return self.reject(413)
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        return self.reject(415)
                    raw = self.rfile.read(int(lengths[0]))
                    if len(raw) != int(lengths[0]):
                        return self.reject(400)
                    data = json.loads(raw, object_pairs_hook=strict_object)
                    if not isinstance(data, dict) or data.get("model") != proxy.model or not isinstance(data.get("messages"), list):
                        return self.reject(400)
                    headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
                    if proxy.provider == "anthropic":
                        headers.update({"x-api-key": proxy._credential, "anthropic-version": "2023-06-01"})
                        beta = self.headers.get_all("anthropic-beta", [])
                        if len(beta) > 1 or (beta and not re.fullmatch(r"[a-zA-Z0-9,._-]{1,1024}", beta[0])):
                            return self.reject(400)
                        if beta:
                            headers["anthropic-beta"] = beta[0]
                    else:
                        headers["Authorization"] = "Bearer " + proxy._credential
                    connection, response = proxy._forward(self.path, raw, headers)
                    if response.status != 200:
                        return self.reject(502)  # Never follow redirects or expose upstream error bodies.
                    content_type = response.getheader("Content-Type", "").split(";")[0]
                    if content_type not in ("application/json", "text/event-stream"):
                        return self.reject(502)
                    self.send_response(200)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    total, pending = 0, b""
                    secret = proxy._credential.encode()
                    while True:
                        chunk = response.read1(16384)
                        if not chunk:
                            self.wfile.write(redact_provider_secret(pending, secret))
                            break
                        total += len(chunk)
                        if total > proxy.MAX_RESPONSE:
                            break
                        pending += chunk
                        # Retain a credential-length tail so split chunks cannot
                        # accidentally echo the provider secret to the worker.
                        cutoff = max(0, len(pending) - len(secret))
                        if cutoff:
                            prefix = pending[:cutoff]
                            # A match spanning cutoff is emitted as one unit.
                            start = pending.rfind(secret, max(0, cutoff-len(secret)+1), cutoff+len(secret))
                            if 0 <= start < cutoff < start+len(secret):
                                cutoff = start
                                prefix = pending[:cutoff]
                            self.wfile.write(redact_provider_secret(prefix, secret))
                            self.wfile.flush()
                            pending = pending[cutoff:]
                except (OSError, ValueError, TypeError, http.client.HTTPException):
                    # A response may already have started; close it rather than
                    # append a second HTTP response that could confuse clients.
                    self.close_connection = True
                finally:
                    if connection:
                        connection.close()
                    proxy._slots.release()

            def do_CONNECT(self): self.reject()
            def do_GET(self): self.reject()
            def do_PUT(self): self.reject()
            def do_DELETE(self): self.reject()
            def do_PATCH(self): self.reject()
            def do_OPTIONS(self): self.reject()

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def _forward(self, path, raw, headers):
        # No environment proxy, URL join, user-selected hostname or redirects.
        connection = http.client.HTTPSConnection(self.host, 443, timeout=self.timeout,
                                                context=ssl.create_default_context())
        connection.request("POST", path, body=raw, headers=headers)
        return connection, connection.getresponse()

    def worker_env(self):
        if not self.server:
            raise EgressError("PROXY_NOT_STARTED")
        prefix = "ANTHROPIC" if self.provider == "anthropic" else "DEEPSEEK"
        return {prefix + "_BASE_URL": "http://127.0.0.1:" + str(self.port),
                prefix + "_API_KEY": self.capability}

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None
        self._credential = ""
