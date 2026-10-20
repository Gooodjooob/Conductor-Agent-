"""Authenticated, no-redirect, narrow Paperclip close projection."""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from .gateway import Rejected, require


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Rejected('UPSTREAM_REDIRECT_DENIED')


class Paperclip:
    def __init__(self, url, token, timeout=5):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme in ('http', 'https') and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and parsed.path in ('', '/'), 'INVALID_UPSTREAM', 400)
        require(parsed.scheme == 'https' or parsed.hostname in ('127.0.0.1', '::1'), 'TLS_REQUIRED', 400)
        require(isinstance(token, str) and len(token) >= 24 and '\n' not in token and '\r' not in token, 'UPSTREAM_TOKEN_REQUIRED', 400)
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None, authenticated=True):
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.url + '/api' + path, data=json.dumps(body).encode() if body is not None else None, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2_000_001)
                require(len(raw) <= 2_000_000, 'UPSTREAM_RESPONSE_TOO_LARGE')
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return exc.code, {}
            raise Rejected('UPSTREAM_HTTP_' + str(exc.code)) from exc
        except (OSError, ValueError) as exc:
            raise Rejected('UPSTREAM_UNKNOWN') from exc

    def assert_boundary(self):
        status, health = self.request('GET', '/health', authenticated=False)
        require(status == 200 and health.get('deploymentMode') == 'authenticated' and health.get('authReady') is True, 'UNSAFE_UPSTREAM_MODE')
        status, _ = self.request('GET', '/companies', authenticated=False)
        require(status in (401, 403), 'ANONYMOUS_UPSTREAM_ACCESS')

    def reconcile_and_close(self, expected):
        self.assert_boundary()
        for child in expected.get('children', []):
            child_id = child['issueId']
            require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', child_id), 'INVALID_ISSUE_ID')
            child_status, child_issue = self.request('GET', '/issues/' + child_id)
            child_marker = '\n\n[orchestrator-p0 accepted sha256:' + child['resultDigest'] + ']'
            require(child_status == 200 and child_issue.get('id') == child_id and child_issue.get('status') == 'done' and (child_issue.get('description') or '').endswith(child_marker), 'UPSTREAM_CHILD_NOT_DONE')
            require(all(child_issue.get(key) == value for key, value in child['policy'].items()), 'UPSTREAM_CHILD_POLICY_DRIFT')
        issue_id = expected['issueId']
        require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', issue_id), 'INVALID_ISSUE_ID')
        path = '/issues/' + issue_id
        status, issue = self.request('GET', path)
        require(status == 200 and issue.get('id') == issue_id, 'UPSTREAM_READ_FAILED')
        require(all(issue.get(key) == value for key, value in expected['policy'].items()), 'UPSTREAM_POLICY_DRIFT')
        marker = '\n\n[orchestrator-p0 accepted sha256:' + expected['resultDigest'] + ']'
        description = issue.get('description') or ''
        if issue.get('status') == 'done':
            require(description.endswith(marker), 'UNVERIFIED_UPSTREAM_DONE')
            return 'synced'
        require(issue.get('status') in ('todo', 'in_progress', 'in_review'), 'UPSTREAM_STATE_CONFLICT')
        # Absolute set is retry-safe; no append-comment endpoints or worker reruns.
        updated = description if description.endswith(marker) else description + marker
        status, _ = self.request('PATCH', path, {'status': 'done', 'description': updated})
        require(status == 200, 'UPSTREAM_WRITE_FAILED')
        status, after = self.request('GET', path)
        require(status == 200 and after.get('status') == 'done' and (after.get('description') or '').endswith(marker), 'UPSTREAM_NOT_CONFIRMED')
        require(all(after.get(key) == value for key, value in expected['policy'].items()), 'UPSTREAM_POLICY_DRIFT')
        return 'synced'
