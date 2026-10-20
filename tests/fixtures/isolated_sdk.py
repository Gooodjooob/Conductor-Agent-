"""Synthetic SDK with a real file artifact, only for offline integration tests."""
import json
from pathlib import Path
import sys

for line in sys.stdin:
    request = json.loads(line)
    ident = request['id']
    def emit(**value):
        print(json.dumps(dict(jsonrpc='2.0', **value)), flush=True)
    if request['method'] == 'initialize':
        emit(id=ident, result={'serverInfo': {'name': 'deepseek-harness-sdk-runtime', 'version': 'synthetic'}})
    elif request['method'] == 'shutdown':
        emit(id=ident, result={}); break
    elif request['method'] == 'session/prompt':
        with open('submissions.txt', 'a') as out: out.write('accepted\n')
        sid = request['params']['sessionId']
        emit(id=ident, result={'messageId': 'fixture-message'})
        emit(method='session.event', params={'sessionId': sid, 'event': {'seq': 0, 'type': 'turn/start', 'data': {'turn': 1}}})
        Path('output.txt').write_text('Synthetic SDK artifact; no model was called.\n')
        emit(method='session.event', params={'sessionId': sid, 'event': {'seq': 1, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}})
