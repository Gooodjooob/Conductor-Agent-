"""Real local HTTP/Scheduler/Runner, synthetic CLI output; no paid host calls."""
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

from orchestrator.execution import PlanExecution, validate_execution, verify_upstream
from orchestrator.gateway import Rejected
from orchestrator.paperclip import Paperclip
from orchestrator.paperclip_import import write_private_json
from orchestrator.recovery import backup_state, state_lease


def make_bundle(root, hosts=('codex', 'workbuddy', 'qclaw')):
    root = Path(root).resolve()
    document = {'schema': 'paperclip-orchestrator-plan/v1',
                'paperclip': {'issueId': 'root-issue', 'title': 'Fixture development task',
                             'description': 'SYNTHETIC task, not a real Paperclip project',
                             'status': 'in_progress', 'projectId': 'fixture-project', 'parentId': None},
                'plan': {'planId': 'fixture-execution', 'revision': 1, 'title': 'Fixture execution',
                         'maxConcurrency': 2, 'members': [], 'nodes': []},
                'tasks': [], 'writeBack': 'controller-finalize-only'}
    config = {'schema': 'orchestrator-jobs/v1', 'jobs': []}
    issues = {'root-issue': dict(document['paperclip'], id='root-issue', companyId='fixture-company')}
    for i, host in enumerate(hosts):
        task, member = 'task-' + str(i), 'member-' + str(i)
        work = root / ('work-' + str(i))
        work.mkdir(mode=0o700)
        prompt = 'SYNTHETIC fixture output only ' + task
        spec = {'taskId': task, 'issueId': 'issue-' + str(i), 'revision': 1, 'workerId': member,
                'runnerId': 'runner-' + str(i), 'qaId': 'qa-' + str(i), 'reviewerId': 'reviewer-' + str(i),
                'controllerId': 'controller-' + str(i), 'workspace': str(work),
                'instructionDigest': 'a' * 64, 'promptDigest': hashlib.sha256(prompt.encode()).hexdigest(),
                'artifactPaths': ['output.txt'], 'children': [],
                'upstreamPolicy': {'reviewPolicy': 'human_only', 'executionPolicy': None, 'parentId': 'root-issue'}}
        document['tasks'].append(spec)
        document['plan']['members'].append({'memberId': member, 'host': host, 'roles': ['developer'], 'maxConcurrency': 1})
        document['plan']['nodes'].append({'taskId': task, 'title': 'Fixture ' + host, 'memberId': member,
                                          'role': 'developer', 'dependsOn': []})
        issues[spec['issueId']] = dict(id=spec['issueId'], title='Fixture child', status='todo',
                                       companyId='fixture-company', projectId='fixture-project', **spec['upstreamPolicy'])
        job = {'taskId': task, 'provider': 'openclaw' if host == 'qclaw' else host,
               'prompt': prompt, 'timeout': 5}
        if host == 'qclaw':
            state = root / ('openclaw-' + str(i))
            state.mkdir(mode=0o700)
            fixture = work / 'openclaw-fixture.py'
            fixture.write_text('''import json, sys
from pathlib import Path
method = sys.argv[sys.argv.index('call')+1]
params = json.loads(sys.argv[sys.argv.index('--params')+1])
if method == 'config.get':
    result = {'agents': {'list': [{'id': 'agent-test', 'workspace': str(Path.cwd()),
              'sandbox': {'mode': 'all', 'workspaceAccess': 'ro', 'docker': {'network': 'none'}},
              'tools': {'deny': ['exec', 'process', 'network']}}]}}
elif method == 'agent':
    with Path('submissions.txt').open('a') as f: f.write('accepted\\n')
    result = {'runId': params['idempotencyKey'], 'status': 'accepted'}
elif method == 'agent.wait':
    Path('output.txt').write_text('SYNTHETIC OpenClaw artifact\\n')
    result = {'status': 'ok'}
elif method == 'chat.history':
    result = {'messages': [{'role': 'assistant', 'text': 'SYNTHETIC reply'}]}
else:
    result = {'tasks': []}
print(json.dumps(result), flush=True)
''')
            job.update(openclawCommand=[sys.executable, str(fixture)], openclawStateDir=str(state),
                       openclawAgentId='agent-test', openclawSessionKey='agent:agent-test:' + task,
                       openclawReadOnlyPreflight=True)
        elif host == 'dsh':
            fixture = (Path(__file__).parent / 'fixtures' / 'isolated_sdk.py').resolve()
            job.update(command=[sys.executable, str(fixture)], runtimeRoots=[sys.prefix, str(fixture.parent)])
        else:
            fixture = work / 'cli-fixture.py'
            events = ([{'type': 'thread.started', 'thread_id': task},
                       {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'SYNTHETIC reply'}},
                       {'type': 'turn.completed'}] if host == 'codex' else
                      [{'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'SYNTHETIC reply'}])
            fixture.write_text('import json\nfrom pathlib import Path\n'
                               'import sys\nPath("argv.json").write_text(json.dumps(sys.argv))\n'
                               'with Path("submissions.txt").open("a") as f: f.write("accepted\\n")\n'
                               'Path("output.txt").write_text("SYNTHETIC CLI artifact\\n")\n'
                               'for event in ' + repr(events) + ': print(json.dumps(event), flush=True)\n')
            job.update(command=[sys.executable, str(fixture)], runtimeRoots=[sys.prefix])
        config['jobs'].append(job)
    return document, config, issues


class UpstreamFixture:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []
        self.token = 'synthetic-upstream-' + 'x' * 32
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass
            def reply(self, code, body):
                self.send_response(code)
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())
            def do_GET(self):
                owner.calls.append(('GET', self.path))
                if self.path == '/api/health':
                    return self.reply(200, {'deploymentMode': 'authenticated', 'authReady': True})
                if self.headers.get('Authorization') != 'Bearer ' + owner.token:
                    return self.reply(401, {})
                key = self.path.removeprefix('/api/issues/') if hasattr(str, 'removeprefix') else self.path[len('/api/issues/'):]
                return self.reply(200, owner.issues[key]) if key in owner.issues else self.reply(404, {})
            def do_PATCH(self):
                owner.calls.append(('PATCH', self.path))
                self.reply(405, {})
            do_POST = do_PATCH
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = Paperclip('http://127.0.0.1:' + str(self.server.server_port), self.token)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.document, self.config, issues = make_bundle(self.root)
        self.upstream = UpstreamFixture(issues)
        self.runtime = None

    def tearDown(self):
        if self.runtime:
            self.runtime.close()
        self.upstream.close()
        self.tmp.cleanup()

    def new_runtime(self, **kwargs):
        self.runtime = PlanExecution(self.root / 'state', self.document, self.config, self.upstream.client, **kwargs)
        return self.runtime

    def wait_state(self, state, ids=None):
        expected = ids or [s['taskId'] for s in self.document['tasks']]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            cards = {c['taskId']: c for c in self.runtime.snapshot()['cards']}
            if all(cards[t]['state'] == state for t in expected):
                return cards
            time.sleep(.05)
        self.fail('states: ' + repr({k: (c['state'], c['error']) for k, c in cards.items()}))

    def test_three_adapter_routes_and_live_snapshot_stop_at_validation(self):
        self.new_runtime().start()
        cards = self.wait_state('awaiting_validation')
        self.assertEqual({c['host'] for c in cards.values()}, {'codex', 'workbuddy', 'qclaw'})
        request = urllib.request.Request(self.runtime.addresses['dashboard'] + '/api/snapshot',
                                         headers={'Authorization': 'Bearer ' + self.runtime.dashboard_token})
        with urllib.request.urlopen(request, timeout=2) as response:
            snapshot = json.load(response)
        self.assertEqual(snapshot['mode'], 'plan-execute')
        self.assertIsNotNone(snapshot['lastObservedAt'])
        for spec in self.document['tasks']:
            task = self.runtime.read_task(spec['taskId'])
            self.assertEqual(task['state'], 'awaiting_validation')
            self.assertEqual(task['verdicts'], {})
            output = (Path(spec['workspace']) / 'output.txt').read_text()
            self.assertNotIn(self.upstream.token, output)
            self.assertNotIn(self.runtime.runner_tokens[spec['taskId']], output)
        self.assertTrue(all(method == 'GET' for method, _ in self.upstream.calls))
        self.assertNotIn('gatewayToken', json.dumps(snapshot))

    def test_resume_preserves_attempt_and_never_reexecutes_terminal_job(self):
        self.new_runtime().start()
        first = self.wait_state('awaiting_validation')
        self.runtime.close()
        self.new_runtime(resume=True).start()
        second = self.wait_state('awaiting_validation')
        self.assertEqual({k:c['attemptId'] for k,c in first.items()}, {k:c['attemptId'] for k,c in second.items()})
        for spec in self.document['tasks']:
            self.assertEqual((Path(spec['workspace']) / 'submissions.txt').read_text(), 'accepted\n')

    def test_dependency_waits_for_independent_verdicts_and_sync(self):
        self.document['plan']['nodes'][1]['dependsOn'] = ['task-0']
        self.new_runtime().start()
        self.wait_state('awaiting_validation', ['task-0', 'task-2'])
        child = next(c for c in self.runtime.snapshot()['cards'] if c['taskId'] == 'task-1')
        self.assertEqual(child['state'], 'queued')
        self.assertFalse((Path(self.document['tasks'][1]['workspace']) / 'submissions.txt').exists())

    def test_incomplete_or_drifted_job_rejected_before_state_or_launch(self):
        variants = []
        missing = copy.deepcopy(self.config); missing['jobs'].pop(); variants.append(missing)
        drift = copy.deepcopy(self.config); drift['jobs'][0]['prompt'] = 'changed'; variants.append(drift)
        host = copy.deepcopy(self.config); host['jobs'][0]['provider'] = 'workbuddy'; variants.append(host)
        injection = copy.deepcopy(self.config); injection['jobs'][0]['gatewayToken'] = 'rogue'; variants.append(injection)
        policy = copy.deepcopy(self.config); policy['jobs'][2]['openclawReadOnlyPreflight'] = False; variants.append(policy)
        for config in variants:
            with self.assertRaises(Rejected):
                PlanExecution(self.root / 'state', self.document, config, self.upstream.client)
        self.assertFalse((self.root / 'state').exists())

    def test_default_sandbox_normalized_to_read_only(self):
        _specs, jobs, _order = validate_execution(self.document, self.config, self.root / 'state')
        self.assertEqual(jobs['task-0']['sandbox'], 'read-only')
        self.new_runtime().start()
        self.wait_state('awaiting_validation')
        argv = json.loads((Path(self.document['tasks'][0]['workspace']) / 'argv.json').read_text())
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')

    def test_upstream_policy_drift_before_launch_is_visible_no_replay(self):
        self.new_runtime()
        self.upstream.issues['issue-0']['reviewPolicy'] = 'changed'
        self.runtime.start()
        cards = self.wait_state('unknown', ['task-0'])
        self.assertEqual(cards['task-0']['error'], 'UPSTREAM_POLICY_DRIFT')
        self.runtime.close()
        self.upstream.issues['issue-0']['reviewPolicy'] = 'human_only'
        self.new_runtime(resume=True).start()
        self.wait_state('unknown', ['task-0'])
        self.assertFalse((Path(self.document['tasks'][0]['workspace']) / 'submissions.txt').exists())

    def test_exclusive_owner_changed_binding_and_root_guard(self):
        runtime = self.new_runtime()
        with self.assertRaisesRegex(Rejected, 'EXECUTION_ALREADY_RUNNING'):
            PlanExecution(self.root / 'state', self.document, self.config, self.upstream.client, resume=True)
        runtime.close()
        changed = copy.deepcopy(self.config); changed['jobs'][0]['timeout'] = 7
        with self.assertRaisesRegex(Rejected, 'EXECUTION_BINDING_CHANGED'):
            PlanExecution(self.root / 'state', self.document, changed, self.upstream.client, resume=True)
        with self.assertRaisesRegex(Rejected, 'EXECUTION_STATE_EXISTS_USE_RESUME'):
            PlanExecution(self.root / 'state', self.document, self.config, self.upstream.client)

    def test_shutdown_waits_for_receipt_and_closes_threads_and_ports(self):
        self.new_runtime().start()
        self.wait_state('awaiting_validation')
        threads = (self.runtime.gateway_thread, self.runtime.dashboard_thread, self.runtime.scheduler_thread)
        ports = (self.runtime.gateway.server_port, self.runtime.dashboard.server_port)
        self.runtime.close()
        self.runtime.close()
        self.assertTrue(all(not t.is_alive() for t in threads))
        for port in ports:
            with socket.socket() as sock:
                self.assertNotEqual(sock.connect_ex(('127.0.0.1', port)), 0)

    def test_startup_port_failure_releases_gateway_and_owner(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0)); sock.listen()
            with self.assertRaises(OSError):
                self.new_runtime(dashboard_port=sock.getsockname()[1])
        self.new_runtime(resume=True).start()
        self.wait_state('awaiting_validation')

    def test_backup_lease_blocks_gateway_and_scheduler_transactions(self):
        self.new_runtime()
        entered = [threading.Event(), threading.Event()]
        ready = [threading.Event(), threading.Event()]
        def transaction(index, factory):
            ready[index].set()
            with factory(): entered[index].set()
        threads = [threading.Thread(target=transaction, args=(0, self.runtime.gateway.gateway.transaction)),
                   threading.Thread(target=transaction, args=(1, self.runtime.scheduler.transaction))]
        with state_lease(self.root / 'state', exclusive=True):
            for thread in threads: thread.start()
            self.assertTrue(all(event.wait(1) for event in ready))
            self.assertTrue(all(not event.wait(.08) for event in entered))
        for thread in threads: thread.join(2)
        self.assertTrue(all(event.is_set() for event in entered))
        backup_state(str(self.root / 'state'), str(self.root / 'backup'))
        self.assertTrue((self.root / 'backup' / 'BACKUP_MANIFEST.json').exists())

    def test_failure_and_timeout_do_not_unlock_dependency_or_replay(self):
        self.document['plan']['nodes'][1]['dependsOn'] = ['task-0']
        fixture = Path(self.config['jobs'][0]['command'][1])
        fixture.write_text('import time\nfrom pathlib import Path\n'
                           'with Path("submissions.txt").open("a") as f: f.write("accepted\\n")\n'
                           'time.sleep(10)\n')
        self.config['jobs'][0]['timeout'] = .3
        self.new_runtime().start()
        self.wait_state('unknown', ['task-0'])
        child = next(c for c in self.runtime.snapshot()['cards'] if c['taskId'] == 'task-1')
        self.assertEqual(child['state'], 'queued')
        self.runtime.close()
        self.new_runtime(resume=True).start()
        self.wait_state('unknown', ['task-0'])
        self.assertEqual((fixture.parent / 'submissions.txt').read_text(), 'accepted\n')

    def test_missing_ledger_resume_refused(self):
        self.new_runtime()
        self.runtime.close()
        (self.root / 'state' / 'gateway.sqlite').unlink()
        with self.assertRaisesRegex(Rejected, 'EXECUTION_LEDGER_MISSING_NO_REPLAY'):
            self.new_runtime(resume=True)

    def test_scheduler_failure_visible_and_stops_dispatch(self):
        self.new_runtime()
        with patch.object(self.runtime.scheduler, 'tick', side_effect=Rejected('SYNTHETIC_SCHEDULER_FAILURE')):
            self.runtime.start()
            self.assertTrue(self.runtime.stop_event.wait(2))
            snapshot = self.runtime.snapshot()
            self.assertEqual(snapshot['schedulerError'], 'SYNTHETIC_SCHEDULER_FAILURE')
            self.assertIsNone(snapshot['lastObservedAt'])
            self.assertFalse(snapshot['dispatchEnabled'])
            self.assertFalse(self.runtime.scheduler.futures)

    def test_restore_hold_refuses_execution(self):
        self.new_runtime()
        self.runtime.close()
        (self.root / 'state' / 'RESTORE_PENDING.json').write_text('{}')
        with self.assertRaisesRegex(Rejected, 'RESTORE_ACTIVATION_REQUIRED'):
            self.new_runtime(resume=True)

    def test_cli_check_then_execute_and_sigterm(self):
        plan_file, jobs_file = self.root / 'plan.json', self.root / 'jobs.json'
        write_private_json(plan_file, self.document); write_private_json(jobs_file, self.config)
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'local_mvp.py'
        args = [sys.executable, str(script), '--plan', str(plan_file), '--jobs', str(jobs_file),
                '--state-dir', str(self.root / 'state'), '--paperclip-url', self.upstream.client.url,
                '--gateway-port', '0', '--dashboard-port', '0']
        env = dict(os.environ, PAPERCLIP_TOKEN=self.upstream.token)
        result = subprocess.run(args + ['--check'], env=env, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['dispatched'])
        self.assertFalse((self.root / 'state').exists())
        log = self.root / 'cli.log'
        with log.open('w') as output:
            process = subprocess.Popen(args + ['--execute'], env=env, stdout=output, stderr=output, text=True)
            try:
                deadline = time.monotonic() + 8
                snapshot = {}
                while time.monotonic() < deadline:
                    if log.stat().st_size:
                        info = json.loads(log.read_text().splitlines()[0])
                        self.assertEqual(info.get('mode'), 'plan-execute', info)
                        request = urllib.request.Request(info['dashboard'] + '/api/snapshot',
                                    headers={'Authorization': 'Bearer ' + info['dashboardReadToken']})
                        with urllib.request.urlopen(request, timeout=2) as response: snapshot = json.load(response)
                        if all(c['state'] == 'awaiting_validation' for c in snapshot['cards']): break
                    time.sleep(.05)
                self.assertTrue(snapshot and all(c['state'] == 'awaiting_validation' for c in snapshot['cards']))
                process.terminate()
                self.assertEqual(process.wait(timeout=8), 0)
                self.assertNotIn(self.upstream.token, log.read_text())
            finally:
                if process.poll() is None:
                    process.kill(); process.wait()


if __name__ == '__main__':
    unittest.main()
