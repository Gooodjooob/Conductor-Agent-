"""Loopback API. Token identities are configured by the operator, never callers."""
import argparse
import hashlib
import hmac
import json
import os
import stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .gateway import Gateway, Rejected, fields, identifier, require, sha
from .paperclip import Paperclip


def load_config(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, 'CONFIG_MUST_BE_PRIVATE', 400)
        config = json.load(stream)
    fields(config, 'stateDir principals paperclip')
    seen = set()
    identities = set()
    for principal in config['principals']:
        require(isinstance(principal, dict) and set(principal) <= {'id', 'role', 'tasks', 'tokenSha256', 'evidenceRoot'}, 'INVALID_PRINCIPAL', 400)
        require(identifier(principal.get('id')) and principal.get('role') in ('admin', 'worker', 'runner', 'qa', 'reviewer', 'controller'), 'INVALID_PRINCIPAL', 400)
        require(sha(principal.get('tokenSha256')) and principal['tokenSha256'] not in seen, 'DUPLICATE_OR_INVALID_TOKEN', 400)
        require(principal['id'] not in identities, 'DUPLICATE_IDENTITY', 400)
        require(isinstance(principal.get('tasks'), list) and principal['tasks'] and all(t == '*' or identifier(t) for t in principal['tasks']), 'INVALID_SCOPE', 400)
        require(principal['role'] == 'admin' or '*' not in principal['tasks'], 'SCOPED_TOKEN_REQUIRED', 400)
        seen.add(principal['tokenSha256']); identities.add(principal['id'])
    return config


def make_server(config, port=0):
    gateway = Gateway(config['stateDir'])
    client = Paperclip(**config['paperclip']) if config['paperclip'] else None
    if client:
        client.assert_boundary()  # Refuse local_trusted before listening.

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # No header, token, body or arbitrary URL logging.

        def respond(self, status, body):
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def actor(self):
            require(not self.headers.get('Origin'), 'BROWSER_ORIGIN_DENIED', 403)
            require(len(self.headers.get_all('Authorization', [])) == 1, 'AUTH_REQUIRED', 401)
            auth = self.headers.get('Authorization', '')
            require(auth.startswith('Bearer ') and 24 <= len(auth[7:]) <= 512, 'AUTH_REQUIRED', 401)
            hashed = hashlib.sha256(auth[7:].encode()).hexdigest()
            for principal in config['principals']:
                if hmac.compare_digest(hashed, principal['tokenSha256']):
                    return {k: v for k, v in principal.items() if k != 'tokenSha256'}
            raise Rejected('AUTH_INVALID', 401)

        def dispatch(self):
            self.connection.settimeout(5)
            actor = self.actor()
            require('?' not in self.path and '%' not in self.path and '\\' not in self.path, 'INVALID_ROUTE', 400)
            parts = self.path.split('/')
            body = None
            if self.command == 'POST':
                require(not self.headers.get('Transfer-Encoding') and len(self.headers.get_all('Content-Length', [])) == 1, 'CONTENT_LENGTH_REQUIRED', 400)
                try:
                    length = int(self.headers['Content-Length'])
                except ValueError:
                    raise Rejected('INVALID_LENGTH', 400)
                require(0 < length <= 262144, 'BODY_TOO_LARGE', 413)
                raw = self.rfile.read(length)
                require(len(raw) == length, 'INCOMPLETE_BODY', 400)
                def unique(pairs):
                    result = {}
                    for key, value in pairs:
                        require(key not in result, 'DUPLICATE_JSON_KEY', 400)
                        result[key] = value
                    return result
                body = json.loads(raw, object_pairs_hook=unique)
            if self.command == 'POST' and self.path == '/v1/tasks':
                return gateway.create(actor, body)
            if self.command == 'POST' and self.path == '/v1/receipts':
                return gateway.receipt(actor, body)
            if self.command == 'POST' and self.path == '/v1/claims':
                return gateway.claim(actor, body)
            if self.command == 'POST' and self.path == '/v1/verdicts':
                return gateway.verdict(actor, body)
            if len(parts) in (4, 5) and parts[1:3] == ['v1', 'tasks'] and identifier(parts[3]):
                task = parts[3]
                if self.command == 'GET' and len(parts) == 4:
                    return gateway.get(actor, task)
                if self.command == 'POST' and len(parts) == 5:
                    fields(body, '')
                    if parts[4] == 'completion-request':
                        return gateway.completion_request(actor, task)
                    if parts[4] == 'finalize':
                        return gateway.finalize(actor, task)
                    if parts[4] == 'retry':
                        return gateway.retry(actor, task)
                    if parts[4] == 'sync':
                        require(client is not None, 'UPSTREAM_NOT_CONFIGURED', 503)
                        return gateway.flush(actor, task, client)
            raise Rejected('ROUTE_NOT_ALLOWED', 404)

        def handle_request(self):
            try:
                self.respond(200, self.dispatch())
            except Rejected as exc:
                self.respond(exc.status, {'error': exc.code})
            except (ValueError, TypeError, KeyError, AttributeError):
                self.respond(400, {'error': 'INVALID_REQUEST'})
            except Exception:
                self.respond(503, {'error': 'INTERNAL_UNAVAILABLE'})

        do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.gateway = gateway
    return server


def main():
    parser = argparse.ArgumentParser(description='P0 receipt and approval Gateway; loopback only')
    parser.add_argument('--config', required=True)
    parser.add_argument('--port', type=int, default=3180)
    args = parser.parse_args()
    server = make_server(load_config(args.config), args.port)
    print(json.dumps({'listening': server.server_address, 'quota': 'disabled'}), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
