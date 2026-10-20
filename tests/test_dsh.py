import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from orchestrator.dsh import AdapterError, DSHAdapter


FIXTURE = Path(__file__).parent / "fixtures" / "sdk_fixture.py"


class DSHTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.adapters = []

    def tearDown(self):
        for adapter in reversed(self.adapters):
            adapter.close()
        self.tmp.cleanup()

    def make(self, mode="completed", store="store", timeout=1, execution_timeout=300):
        adapter = DSHAdapter(self.root / store, [sys.executable, str(FIXTURE), mode],
                             self.work, {"HOME": str(self.home), "PATH": "/usr/bin:/bin",
                             "FIXTURE_COUNT": str(self.work / "count")}, timeout=timeout,
                             execution_timeout=execution_timeout)
        self.adapters.append(adapter)
        return adapter

    def submit(self, adapter, prompt="fixture", attempt="attempt-1"):
        return adapter.submit("task-1", 1, attempt, prompt, "frozen-digest")

    def wait_state(self, adapter, wanted):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = adapter.get_status("attempt-1")
            if result["state"] in wanted:
                return result
            time.sleep(.01)
        self.fail(result)

    def test_completed_is_only_awaiting_validation(self):
        a = self.make()
        self.assertTrue(a.start()["protocolReady"])
        self.submit(a)
        r = self.wait_state(a, {"awaiting_validation"})
        self.assertTrue(r["accepted"])
        self.assertFalse(r["qaPassed"])
        self.assertEqual(r["eventCount"], 2)

    def test_parallel_duplicate_single_enqueue(self):
        a = self.make()
        a.start()
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(lambda _: self.submit(a), range(30)))
        self.assertEqual(sum(not r["deduplicated"] for r in results), 1)
        self.assertEqual((self.work / "count").read_text(), "accepted\n")

    def test_second_owner_reads_and_cannot_start(self):
        a = self.make()
        a.start()
        self.submit(a)
        b = self.make()
        self.assertFalse(b.owner)
        self.assertTrue(self.submit(b)["deduplicated"])
        with self.assertRaises(AdapterError):
            b.start()

    def test_cross_process_duplicate(self):
        a = self.make()
        a.start()
        self.submit(a)
        script = """
import json,sys
from orchestrator.dsh import DSHAdapter
a=DSHAdapter(sys.argv[1], [sys.executable,sys.argv[4],'completed'], sys.argv[2], {'HOME':sys.argv[3],'PATH':'/usr/bin:/bin','FIXTURE_COUNT':sys.argv[2]+'/count'},timeout=1)
try:
 print(json.dumps(a.submit('task-1',1,'attempt-1','fixture','frozen-digest')))
finally: a.close()
"""
        env = dict(os.environ, PYTHONPATH=str(FIXTURE.parents[2]))
        children = [subprocess.Popen([sys.executable, "-c", script, str(a.store), str(self.work), str(self.home), str(FIXTURE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env) for _ in range(4)]
        for child in children:
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertTrue(json.loads(out)["deduplicated"])
        self.assertEqual((self.work / "count").read_text(), "accepted\n")

    def test_contract_and_second_attempt_rejected(self):
        a = self.make()
        a.start()
        self.submit(a)
        for kwargs in ({"prompt": "changed"}, {"attempt": "attempt-2"}):
            with self.assertRaisesRegex(AdapterError, "CONFLICT"):
                self.submit(a, **kwargs)

    def test_timeout_is_unknown_without_replay(self):
        a = self.make("timeout", timeout=.1)
        a.start()
        self.assertEqual(self.submit(a)["state"], "unknown")
        self.assertTrue(self.submit(a)["deduplicated"])
        self.assertEqual((self.work / "count").read_text(), "accepted\n")

    def test_sdk_death_is_unknown(self):
        a = self.make("death")
        a.start()
        self.assertEqual(self.submit(a)["state"], "unknown")
        self.assertTrue(self.submit(a)["deduplicated"])

    def test_kill_running_and_read_after_restart(self):
        a = self.make("running")
        a.start()
        self.submit(a)
        self.wait_state(a, {"running"})
        os.kill(a.child.pid, signal.SIGKILL)
        a.child.wait(timeout=2)
        self.wait_state(a, {"unknown"})
        a.close()
        b = self.make("running")
        self.assertEqual(b.get_status("attempt-1")["state"], "unknown")
        self.assertTrue(self.submit(b)["deduplicated"])
        with self.assertRaisesRegex(AdapterError, "RECOVERY_READ_ONLY"):
            b.start()

    def test_failed_receipt_persisted(self):
        a = self.make("error")
        a.start()
        self.submit(a)
        r = self.wait_state(a, {"failed"})
        self.assertEqual(r["reason"]["error"]["code"], "MISSING_CREDENTIAL")
        a.close()
        b = self.make("error")
        self.assertEqual(b.get_status("attempt-1")["state"], "failed")
        self.assertEqual(b.collect("attempt-1")["artifacts"], [])

    def test_protocol_faults_fail_closed(self):
        for mode in ("malformed", "wrong_response", "duplicate_response", "out_of_order", "wrong_turn", "conflict"):
            with self.subTest(mode=mode):
                a = self.make(mode, store=mode)
                a.start()
                self.submit(a)
                self.wait_state(a, {"unknown"})
                a.close()

    def test_non_success_end_reasons(self):
        for mode in ("interrupted", "blocked", "max-tokens", "novel"):
            with self.subTest(mode=mode):
                a = self.make(mode, store=mode)
                a.start()
                self.submit(a)
                self.wait_state(a, {"unknown"})
                a.close()

    def test_cross_session_cannot_complete_parent(self):
        a = self.make("cross_session")
        a.start()
        self.submit(a)
        time.sleep(.05)
        r = a.get_status("attempt-1")
        self.assertEqual(r["state"], "accepted")
        self.assertEqual(r["eventCount"], 0)

    def test_identical_event_deduplicates(self):
        a = self.make("duplicate")
        a.start()
        self.submit(a)
        r = self.wait_state(a, {"awaiting_validation"})
        time.sleep(.02)
        self.assertEqual(a.get_status("attempt-1")["eventCount"], 2)
        self.assertEqual(r["state"], "awaiting_validation")

    def completed(self):
        a = self.make()
        a.start()
        self.submit(a)
        self.wait_state(a, {"awaiting_validation"})
        return a

    def test_artifact_digest_and_immutable_manifest(self):
        a = self.completed()
        (self.work / "result.py").write_text("print(1)\n")
        r = a.collect("attempt-1", ["result.py"])
        self.assertEqual(r["artifacts"], [{"path": "result.py", "size": 9,
            "sha256": hashlib.sha256(b"print(1)\n").hexdigest()}])
        self.assertEqual(a.collect("attempt-1", ["result.py"])["artifacts"], r["artifacts"])
        (self.work / "result.py").write_text("print(2)\n")
        with self.assertRaisesRegex(AdapterError, "MANIFEST_CHANGED"):
            a.collect("attempt-1", ["result.py"])

    def test_symlink_parent_hardlink_and_traversal_rejected(self):
        a = self.completed()
        secret = self.root / "private"
        secret.write_text("private fixture")
        (self.work / "link").symlink_to(secret)
        (self.work / "dirlink").symlink_to(self.root, target_is_directory=True)
        os.link(secret, self.work / "hardlink")
        for path in ("../private", str(secret), "link", "dirlink/private", "hardlink"):
            with self.subTest(path=path), self.assertRaises(AdapterError):
                a.collect("attempt-1", [path])

    def test_nonterminal_artifacts_rejected(self):
        a = self.make("running")
        a.start()
        self.submit(a)
        (self.work / "result").write_text("not proof")
        with self.assertRaisesRegex(AdapterError, "REQUIRE_RUNTIME_COMPLETION"):
            a.collect("attempt-1", ["result"])
        self.assertEqual(a.collect("attempt-1")["artifacts"], [])

    def test_execution_deadline_is_unknown(self):
        a = self.make("running", execution_timeout=.08)
        a.start()
        self.submit(a)
        r = self.wait_state(a, {"unknown"})
        self.assertEqual(r["reason"], "EXECUTION_DEADLINE_EXCEEDED")

    def test_collect_quiesces_runtime(self):
        a = self.completed()
        (self.work / "result").write_text("fixture")
        self.assertTrue(a.collect("attempt-1", ["result"])["runtimeClosed"])
        self.assertIsNotNone(a.child.poll())

    def test_close_reclaims_descendant_process_group(self):
        a = self.make("descendant")
        a.start()
        self.submit(a)
        self.wait_state(a, {"awaiting_validation"})
        pid = int((self.work / "child-pid").read_text())
        a.close()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(.01)
        else:
            # Linux may briefly retain an adopted zombie; it cannot mutate files.
            output = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
            self.assertTrue(output.stdout.strip().startswith("Z"), output.stdout)

    def test_observer_reconciles_actual_owner_death(self):
        script = """
import json,sys,time
from orchestrator.dsh import DSHAdapter
a=DSHAdapter(sys.argv[1], [sys.executable,sys.argv[4],'running'], sys.argv[2], {'HOME':sys.argv[3],'PATH':'/usr/bin:/bin','FIXTURE_COUNT':sys.argv[2]+'/count'},timeout=1)
a.start()
a.submit('task-1',1,'attempt-1','fixture','frozen-digest')
print(a.child.pid,flush=True)
time.sleep(30)
"""
        env = dict(os.environ, PYTHONPATH=str(FIXTURE.parents[2]))
        child = subprocess.Popen([sys.executable, "-c", script, str(self.root/'store'), str(self.work), str(self.home), str(FIXTURE)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        sdk_pid = None
        try:
            sdk_pid = int(child.stdout.readline())
            observer = self.make("running")
            self.assertFalse(observer.owner)
            child.kill()
            child.wait(timeout=2)
            r = observer.get_status('attempt-1')
            self.assertEqual(r['state'], 'unknown')
            self.assertEqual(r['reason'], 'OWNER_LOST')
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=2)
            child.stdout.close()
            child.stderr.close()
            if sdk_pid:
                # The fixture exits on stdin EOF after controller death. Wait
                # for this actual lifecycle fact instead of signalling a group
                # in Darwin's brief exit/reap window (which can return EPERM).
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    state = subprocess.run(["ps", "-o", "stat=", "-p", str(sdk_pid)], capture_output=True, text=True).stdout.strip()
                    if not state or state.startswith("Z"):
                        break
                    time.sleep(.01)
                else:
                    os.killpg(sdk_pid, signal.SIGKILL)
                    self.fail("SDK fixture did not exit after controller stdin closed")

    def test_revision_positive_integer_only(self):
        a = self.make()
        for revision in (0, -1, True, "1", 1.0, None):
            with self.subTest(revision=revision), self.assertRaisesRegex(ValueError, "INVALID_REVISION"):
                a.submit("task", revision, "attempt", "prompt", "digest")

    def test_store_symlink_rejected(self):
        target = self.root / "target"
        target.mkdir(mode=0o700)
        (self.root / "store").symlink_to(target)
        with self.assertRaisesRegex(AdapterError, "STORE_SYMLINK"):
            self.make()
        self.assertEqual(list(target.iterdir()), [])

    def test_store_file_symlinks_and_hardlinks_rejected(self):
        for name in ("owner.lock", "receipt.sqlite", "receipt.sqlite-journal", "receipt.sqlite-wal", "receipt.sqlite-shm", "runtime.stderr"):
            for hardlink in (False, True):
                with self.subTest(name=name, hardlink=hardlink):
                    store = name + str(hardlink)
                    directory = self.root / store
                    directory.mkdir(mode=0o700)
                    target = self.root / ("target-" + store)
                    target.write_text("must remain intact")
                    target.chmod(0o600)
                    if hardlink:
                        os.link(target, directory / name)
                    else:
                        (directory / name).symlink_to(target)
                    with self.assertRaises(AdapterError):
                        a = self.make(store=store)
                        a.start()
                    self.assertEqual(target.read_text(), "must remain intact")

    def test_store_permissions_rejected(self):
        directory = self.root / "store"
        directory.mkdir(mode=0o755)
        with self.assertRaisesRegex(AdapterError, "PRIVATE_OWNED"):
            self.make()

    def test_launch_config_frozen_and_secrets_not_persisted(self):
        a = self.make()
        with self.assertRaisesRegex(AdapterError, "CONFIG_CONFLICT"):
            self.make(mode="running")
        a.env["DEEPSEEK_API_KEY"] = "synthetic-never-used-secret"
        with self.assertRaisesRegex(AdapterError, "CONFIG_CHANGED"):
            a.start()
        self.assertNotIn(b"synthetic-never-used-secret", a.db_path.read_bytes())

    def test_store_inode_replacement_rejected(self):
        a = self.make()
        a.store.rename(self.root / "replaced")
        a.store.mkdir(mode=0o700)
        with self.assertRaisesRegex(AdapterError, "IDENTITY_OR_PERMISSIONS"):
            a.get_status("attempt")


if __name__ == "__main__":
    unittest.main()
