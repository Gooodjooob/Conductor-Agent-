"""D4 heartbeat controller tests; synthetic fixtures only, no real automation or model.

任务卡 D4-AUTOMATION-GUARD-IMPLEMENTATION-01 acceptance[10]：覆盖门禁与暂停阈值、
deadline、合法交付优先、噪声、重复 tick、并发、跨身份、恶意路径、暂停失败与恢复门。
worker 本轮不执行 Bash，由 cox 独立 unittest 验证。
"""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone

from scripts import d4ctl, d4_heartbeat_control as hb
from storage.sqlite_store import SqliteStore

AUTO_ID = "auto-fixture"
THREAD_ID = "thread-fixture"
ATTEMPT_ID = "att-fixture"
BASE_NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)


class HeartbeatFixtureBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.watch = self.root / "watch"
        self.watch.mkdir(mode=0o700)
        self.home = self.root / "home"
        self.home.mkdir()
        self.db = self.root / "d4.sqlite"
        self.started_epoch = (BASE_NOW - timedelta(seconds=10)).timestamp()
        payload = self.root / "task.json"
        payload.write_text(json.dumps({"projectId": "d4-heartbeat", "title": "fixture",
            "agentId": "dev-wkb1", "sourceId": "workbuddy-codebuddy",
            "workspace": str(self.workspace), "goal": "fixture",
            "permissions": {"writeScope": ["."], "allowNetwork": False,
                "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}}))
        code, created = d4ctl.run(["--db", str(self.db), "--allowed-root", str(self.root),
            "--source", "workbuddy-codebuddy", "--agent", "dev-wkb1",
            "task", "create", "--file", str(payload)])
        self.assertEqual(code, 0, created)
        self.task_id = created["data"]["taskId"]
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.insert_attempt({"attempt_id": ATTEMPT_ID, "task_id": self.task_id,
                    "ordinal": 1, "state": "running", "created_at": "2026-10-09T11:00:00+00:00",
                    "submitted_at": "2026-10-09T11:00:01+00:00",
                    "accepted_by_host_at": "2026-10-09T11:00:02+00:00",
                    "started_at": None, "terminal_at": None, "terminal_kind": None,
                    "blocked_json": None, "note": None, "submit_state": "submitted",
                    "submission_json": None})
                store.insert_event({"event_id": "evt-1", "task_id": self.task_id,
                    "attempt_id": ATTEMPT_ID, "project_id": "d4-heartbeat",
                    "source_id": "workbuddy-codebuddy", "agent_id": "dev-wkb1",
                    "sequence": 1, "type": "task.started",
                    "occurred_at": "2026-10-09T11:00:03+00:00",
                    "received_at": "2026-10-09T11:00:03+00:00",
                    "content_hash": "fixture", "outcome": "applied",
                    "reason": None, "result_json": None})
        finally:
            store.close()
        # 冻结 policy 必须先于 config 定义：workflow 与 runner config 共用同一 policy 与 SHA
        # （镜像真实 runtime 字段，acceptance[4][5]）。
        self.policy = {"schemaVersion": "d4-model-policy.v1", "primary": "glm-5.3-flash",
            "allowedModels": ["glm-5.3-flash"],
            "effectiveAt": "2026-10-01T00:00:00+00:00",
            "authorizedAt": "2026-10-01T00:00:00+00:00",
            "transparentFallback": False,
            "allowedAgents": ["dev-wkb1"], "allowedSources": ["workbuddy-codebuddy"],
            "allowedRoots": [str(self.root)]}
        self.write_config()
        (self.runtime / "state.json").write_text(json.dumps({"status": "running"}))
        # 真实 fcntl 排它锁表示 runner 存活；tearDown 显式释放（不再写空串占位冒充）。
        self.runner_lock_fd = os.open(self.runtime / "runner.lock",
                                      os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.runner_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # started.touch 与 os.utime 分两句，确定 deadline 起点 = started + 冻结 timeout。
        started = self.runtime / "started"
        started.touch(mode=0o600)
        os.utime(started, (self.started_epoch, self.started_epoch))
        self.workflow = self.root / "workflow.json"
        self.write_workflow()
        # host runtime 目录与运行中的 runtime.json（镜像 d4-workbuddy-store 布局，供 collector 只读）。
        (self.home / "d4-workbuddy-store/attempts" / ATTEMPT_ID).mkdir(parents=True)
        self.write_host_runtime()

    def tearDown(self):
        fd = getattr(self, "runner_lock_fd", None)
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
        self.tmp.cleanup()

    def release_runner_lock(self):
        """显式释放 runner 锁：表示 runner 已停（不再 mock PID）。"""
        fcntl.flock(self.runner_lock_fd, fcntl.LOCK_UN)

    def reacquire_runner_lock(self):
        """重新持有 runner 锁：表示 runner 再次存活。"""
        fcntl.flock(self.runner_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def write_workflow(self):
        # workflow 顶层权威绑定字段：db / allowedAgents / allowedSource
        # （acceptance[2]；modelPolicy 成员集不替代顶层，此处补齐真实必需字段）。
        document = {"schemaVersion": "d4-workflow.v1", "automationId": AUTO_ID,
                    "ownerThreadId": THREAD_ID, "taskId": self.task_id,
                    "attemptId": ATTEMPT_ID,
                    "db": str(self.db),
                    "allowedAgents": ["dev-wkb1"],
                    "allowedSource": "workbuddy-codebuddy",
                    "currentRunner": str(self.runtime), "runtimeDir": str(self.runtime),
                    "modelPolicy": self.policy}
        self.workflow.write_text(json.dumps(document))
        return document

    def write_config(self):
        """写入冻结 runner config（含 modelPolicy / modelPolicySha256，镜像真实 runtime）。"""
        config = {"taskId": self.task_id, "attemptId": ATTEMPT_ID, "db": str(self.db),
                  "home": str(self.home), "binary": str(self.root / "fixture"),
                  "model": "glm-5.3-flash", "binding": {"agentId": "dev-wkb1"},
                  "sourceId": "workbuddy-codebuddy",
                  "timeoutMs": 60000, "receiptWaitSeconds": 0,
                  "allowedRoot": str(self.root),
                  "modelPolicy": self.policy,
                  "modelPolicySha256": hb.sha256_bytes(
                      hb.canonical_json(self.policy).encode("utf-8"))}
        (self.runtime / "config.json").write_text(json.dumps(config))
        return config

    def apply_policy(self, policy):
        """一致替换 workflow 与冻结 config 的 modelPolicy（保持 SHA 一致，专测授权内容）。"""
        self.policy = policy
        self.write_workflow()
        self.write_config()

    def write_host_runtime(self, **fields):
        path = self.home / "d4-workbuddy-store/attempts" / ATTEMPT_ID / "runtime.json"
        runtime = {"sessionId": ATTEMPT_ID, "workspace": str(self.workspace),
                   "requestedModel": "glm-5.3-flash", "state": "running"}
        runtime.update(fields)
        path.write_text(json.dumps(runtime))

    def tick(self, tick_id, now=BASE_NOW):
        return hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch), tick_id, now=now)

    def snapshot(self, now=BASE_NOW):
        return hb.collect_snapshot(str(self.workflow), AUTO_ID, THREAD_ID, now=now)

    def watch_state(self):
        return json.loads((self.watch / "state.json").read_text())

    def insert_delivery(self, delivery_id="dlv-1"):
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.insert_delivery({"delivery_id": delivery_id, "task_id": self.task_id,
                    "attempt_id": ATTEMPT_ID, "summary": "fixture",
                    "changed_files_json": "[]", "verification_json": "[]",
                    "risks_json": "[]", "report_path": "receipts/report.md",
                    "unfinished_json": "[]", "source_delivery_id": None,
                    "created_at": "2026-10-09T11:30:00+00:00"})
        finally:
            store.close()

    def insert_review(self, review_id="rev-1", delivery_id="dlv-1", decision="approved"):
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.insert_review({"review_id": review_id, "task_id": self.task_id,
                    "delivery_id": delivery_id, "decision": decision, "note": None,
                    "review_round": 1, "resulting_attempt_id": None,
                    "created_at": "2026-10-09T11:40:00+00:00"})
        finally:
            store.close()

    def write_reconciliation_evidence(self, fingerprint, name="evidence.json"):
        path = self.root / name
        path.write_text(json.dumps({"taskId": self.task_id, "attemptId": ATTEMPT_ID,
            "fingerprint": fingerprint, "processChecked": True,
            "sourceChecked": True, "receiptsChecked": True}))
        return path


class HeartbeatSnapshotTests(HeartbeatFixtureBase):
    def test_snapshot_identity_and_bindings(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["schemaVersion"], "d4-heartbeat-snapshot.v1")
        self.assertEqual(snapshot["automationId"], AUTO_ID)
        self.assertEqual(snapshot["taskId"], self.task_id)
        self.assertEqual(snapshot["attemptId"], ATTEMPT_ID)
        self.assertEqual(snapshot["submitState"], "submitted")
        self.assertTrue(snapshot["runnerAlive"])
        self.assertIsNone(snapshot["anomaly"])
        self.assertFalse(snapshot["reconciliationRequired"])
        self.assertEqual(snapshot["deadlineAt"], "2026-10-09T12:00:50+00:00")
        self.assertIsNotNone(snapshot["modelPolicySha256"])

    def test_identity_mismatch_and_missing_are_reconcile_anomalies(self):
        document = self.write_workflow()
        document["automationId"] = "auto-other"
        self.workflow.write_text(json.dumps(document))
        snapshot = hb.collect_snapshot(str(self.workflow), AUTO_ID, THREAD_ID)
        self.assertEqual(snapshot["errorCode"], "WORKFLOW_IDENTITY_CONFLICT")
        self.assertTrue(snapshot["reconciliationRequired"])
        document = self.write_workflow()
        document.pop("ownerThreadId")
        self.workflow.write_text(json.dumps(document))
        snapshot = hb.collect_snapshot(str(self.workflow), AUTO_ID, THREAD_ID)
        self.assertEqual(snapshot["errorCode"], "WORKFLOW_IDENTITY_MISSING")

    def test_source_file_existence_alone_is_not_a_delivery(self):
        receipts = self.workspace / "receipts" / self.task_id / ATTEMPT_ID
        receipts.mkdir(parents=True)
        (receipts / "events.json").write_text("{}")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["deliveryState"], "none")
        self.assertIsNone(snapshot["deliveryId"])
        self.assertIsNotNone(snapshot["receiptDigest"])

    def test_collector_is_read_only_over_evidence(self):
        watched = [self.workflow, self.runtime / "config.json", self.runtime / "state.json",
                   self.runtime / "runner.lock", self.db]
        before = {path: path.read_bytes() for path in watched}
        self.snapshot()
        self.tick("t0")
        for path in watched:
            self.assertEqual(before[path], path.read_bytes(), path.name)


class HeartbeatDirectiveTests(HeartbeatFixtureBase):
    def test_running_within_deadline_waits(self):
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "WAIT")
        self.assertEqual(directive["schemaVersion"], "d4-heartbeat-directive.v1")
        self.assertFalse(directive["notifyUser"])
        self.assertIsNone(directive["platformAction"])

    def test_new_delivery_is_review_priority_and_approved_is_next_candidate(self):
        self.insert_delivery()
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "REVIEW")
        self.assertEqual(directive["data"]["deliveryId"], "dlv-1")
        self.insert_review()
        directive = self.tick("t2")
        self.assertEqual(directive["action"], "NEXT")
        self.assertFalse(directive["data"]["autoLaunch"])
        self.assertTrue(directive["notifyUser"])
        directive = self.tick("t3")
        self.assertEqual(directive["action"], "NEXT")
        self.assertFalse(directive["notifyUser"])

    def test_execution_unknown_reconcile_until_confirmed_then_pause(self):
        self.write_host_runtime(state="unknown")
        d1 = self.tick("t1")
        self.assertEqual(d1["action"], "RECONCILE")
        fingerprint = d1["data"]["errorFingerprint"]
        self.assertEqual(d1["data"]["errorCount"], 1)
        d2 = self.tick("t2")
        self.assertEqual(d2["action"], "RECONCILE")
        self.assertEqual(d2["data"]["errorCount"], 2)
        d3 = self.tick("t3")
        self.assertEqual(d3["action"], "RECONCILE")
        self.assertIn("reconciliation_confirmation_required", d3["reason"])
        evidence = self.write_reconciliation_evidence(fingerprint)
        hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                      str(self.watch), fingerprint, str(evidence))
        d4 = self.tick("t4")
        self.assertEqual(d4["action"], "PAUSE")
        self.assertEqual(d4["platformAction"]["kind"], "pause_automation")
        self.assertEqual(d4["platformAction"]["automationId"], AUTO_ID)
        self.assertEqual(d4["platformAction"]["ownerThreadId"], THREAD_ID)
        self.assertTrue(d4["notifyUser"])
        d5 = self.tick("t5")
        self.assertEqual(d5["action"], "PAUSE")
        self.assertFalse(d5["notifyUser"])

    def test_model_mismatch_follows_reconcile_then_pause_gate(self):
        self.write_host_runtime(state="exited", observedModel="deepseek-v4.1-flash")
        d1 = self.tick("t1")
        self.assertEqual(d1["action"], "RECONCILE")
        self.assertEqual(d1["data"]["errorCode"], "MODEL_MISMATCH")
        fingerprint = d1["data"]["errorFingerprint"]
        for name in ("t2", "t3"):
            self.assertEqual(self.tick(name)["action"], "RECONCILE")
        evidence = self.write_reconciliation_evidence(fingerprint)
        hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                      str(self.watch), fingerprint, str(evidence))
        self.assertEqual(self.tick("t4")["action"], "PAUSE")

    def test_orphan_submission_is_reconcile_class(self):
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.update_attempt(ATTEMPT_ID, submit_state="unknown")
        finally:
            store.close()
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "ORPHAN_SUBMISSION")

    def test_structured_quota_error_without_backup_reconciles(self):
        (self.runtime / "result.json").write_text(json.dumps(
            {"exitCode": 1, "submission": {"ok": False,
             "error": {"code": "QUOTA_EXHAUSTED", "message": "synthetic"}}}))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "QUOTA_EXHAUSTED")


class HeartbeatThresholdTests(HeartbeatFixtureBase):
    def advance_deadline_ticks(self, start_index):
        overdue = BASE_NOW + timedelta(seconds=120)
        directives = []
        for offset in range(start_index, start_index + 3):
            directives.append(self.tick("t%d" % offset, now=overdue))
        return directives

    def test_deadline_without_progress_baseline_then_pause_after_three(self):
        overdue = BASE_NOW + timedelta(seconds=120)
        d1 = self.tick("t1", now=overdue)
        self.assertEqual(d1["action"], "WAIT")
        self.assertEqual(d1["data"]["noProgressCount"], 1)
        d2 = self.tick("t2", now=overdue)
        d3 = self.tick("t3", now=overdue)
        self.assertEqual((d2["action"], d2["data"]["noProgressCount"]), ("WAIT", 2))
        self.assertEqual((d3["action"], d3["data"]["noProgressCount"]), ("WAIT", 3))
        d4 = self.tick("t4", now=overdue)
        self.assertEqual(d4["action"], "PAUSE")
        self.assertEqual(d4["data"]["noProgressCount"], 4)
        self.assertIn("NO_PROGRESS_AFTER_DEADLINE", d4["data"]["errorFingerprint"])

    def test_noise_does_not_reset_deadline_baseline(self):
        overdue = BASE_NOW + timedelta(seconds=120)
        self.tick("t1", now=overdue)
        # 噪声：更新 state.json 的 pid/时间戳、追加重复日志，都不构成有效进展。
        (self.runtime / "state.json").write_text(json.dumps(
            {"status": "running", "pid": 99999, "at": "2026-10-09T13:00:00+00:00"}))
        d2 = self.tick("t2", now=overdue)
        self.assertEqual(d2["data"]["noProgressCount"], 2)

    def test_valid_progress_resets_deadline_baseline(self):
        overdue = BASE_NOW + timedelta(seconds=120)
        self.tick("t1", now=overdue)
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.insert_event({"event_id": "evt-2", "task_id": self.task_id,
                    "attempt_id": ATTEMPT_ID, "project_id": "d4-heartbeat",
                    "source_id": "workbuddy-codebuddy", "agent_id": "dev-wkb1",
                    "sequence": 2, "type": "task.progress",
                    "occurred_at": "2026-10-09T12:30:00+00:00",
                    "received_at": "2026-10-09T12:30:00+00:00",
                    "content_hash": "fixture-2", "outcome": "applied",
                    "reason": None, "result_json": None})
        finally:
            store.close()
        d2 = self.tick("t2", now=overdue)
        self.assertEqual(d2["action"], "WAIT")
        self.assertEqual(d2["data"]["noProgressCount"], 0)
        d3 = self.tick("t3", now=overdue)
        self.assertEqual(d3["data"]["noProgressCount"], 1)

    def test_attempt_change_rebuilds_baseline(self):
        self.write_host_runtime(state="unknown")
        self.tick("t1")
        self.tick("t2")
        self.assertEqual(self.watch_state()["error"]["count"], 2)
        # 新 attempt：workflow 指向新 attempt，DB 建新轮次 → 重建基线。
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.insert_attempt({"attempt_id": "att-fixture-2", "task_id": self.task_id,
                    "ordinal": 2, "state": "running", "created_at": "2026-10-09T12:00:00+00:00",
                    "submitted_at": "2026-10-09T12:00:01+00:00",
                    "accepted_by_host_at": "2026-10-09T12:00:02+00:00",
                    "started_at": None, "terminal_at": None, "terminal_kind": None,
                    "blocked_json": None, "note": None, "submit_state": "submitted",
                    "submission_json": None})
        finally:
            store.close()
        document = self.write_workflow()
        document["attemptId"] = "att-fixture-2"
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t3")
        self.assertEqual(directive["data"]["errorCount"], 1)

    def test_replayed_tick_returns_cached_directive_without_counting(self):
        d1 = self.tick("t1")
        fingerprint = d1["data"]["errorFingerprint"]
        self.write_host_runtime(state="unknown")
        self.tick("t2")
        self.tick("t3")
        count = self.watch_state()["error"]["count"]
        replay = self.tick("t3")
        self.assertEqual(replay, self.tick("t3"))
        self.assertEqual(self.watch_state()["error"]["count"], count)

    def test_concurrent_same_tick_counts_once_and_distinct_ticks_serialize(self):
        self.write_host_runtime(state="unknown")
        results = {}
        errors = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(4)

        def run(name):
            try:
                # 真实并发启动屏障放在 tick 之前，尽量让 4 个首次 tick 同时进入。
                barrier.wait(timeout=10)
                directive = self.tick("same-tick")
            except BaseException as exc:  # noqa: BLE001 - 显式收集，绝不让后台异常逃逸
                with results_lock:
                    errors.append((name, repr(exc)))
                return
            with results_lock:
                results[name] = directive

        threads = [threading.Thread(target=run, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        # 有界 join 后必须无存活线程，且所有线程异常被显式收集为空（不再让后台 traceback 静默 OK）。
        self.assertFalse(any(thread.is_alive() for thread in threads), "线程未在时限内结束")
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        directives = list(results.values())
        for directive in directives:
            self.assertEqual(directive, directives[0])
        self.assertEqual(self.watch_state()["error"]["count"], 1)

        distinct_results = {}
        distinct_errors = []
        distinct_lock = threading.Lock()
        distinct_barrier = threading.Barrier(3)

        def run_distinct(index):
            name = "distinct-%d" % index
            try:
                distinct_barrier.wait(timeout=10)
                distinct_results[name] = self.tick(name)
            except BaseException as exc:  # noqa: BLE001
                with distinct_lock:
                    distinct_errors.append((name, repr(exc)))

        threads = [threading.Thread(target=run_distinct, args=(index,)) for index in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertFalse(any(thread.is_alive() for thread in threads), "线程未在时限内结束")
        self.assertEqual(distinct_errors, [])
        self.assertEqual(len(distinct_results), 3)
        self.assertEqual(self.watch_state()["error"]["count"], 4)

    def test_directives_created_inside_same_watch_lock(self):
        """确定性回归（acceptance[4]）：directives.mkdir 必须在真实 _WatchLock 持有期间执行。

        通过 instrument 真实 _WatchLock 的 __enter__/__exit__ 持有事实，检查 directives
        目录的 mkdir 调用发生在其间：旧实现在锁外创建 directives，观测到 held=False 而失败；
        修正后必须在锁内（held=True）。不使用会在正确串行锁内死锁的 4 方 mkdir 屏障。
        """
        lock_held = {"held": False}
        mkdir_inside_lock = []
        real_enter = hb._WatchLock.__enter__
        real_exit = hb._WatchLock.__exit__
        real_mkdir = Path.mkdir

        def tracking_enter(self):
            entered = real_enter(self)
            lock_held["held"] = True
            return entered

        def tracking_exit(self, exc_type, exc, tb):
            try:
                return real_exit(self, exc_type, exc, tb)
            finally:
                lock_held["held"] = False

        def tracking_mkdir(self, *args, **kwargs):
            if self.name == "directives":
                mkdir_inside_lock.append(lock_held["held"])
            return real_mkdir(self, *args, **kwargs)

        hb._WatchLock.__enter__ = tracking_enter
        hb._WatchLock.__exit__ = tracking_exit
        Path.mkdir = tracking_mkdir
        try:
            self.tick("t1")
        finally:
            hb._WatchLock.__enter__ = real_enter
            hb._WatchLock.__exit__ = real_exit
            Path.mkdir = real_mkdir
        self.assertEqual(mkdir_inside_lock, [True])
        self.assertTrue((self.watch / "directives").is_dir())


class HeartbeatBoundaryTests(HeartbeatFixtureBase):
    def test_cross_identity_is_refused(self):
        self.tick("t1")
        with self.assertRaisesRegex(ValueError, "WATCH_IDENTITY_MISMATCH"):
            hb.tick(str(self.workflow), AUTO_ID, "thread-other", str(self.watch), "t2",
                    now=BASE_NOW)

    def test_symlink_and_relative_paths_are_refused(self):
        link = self.root / "watch-link"
        link.symlink_to(self.watch)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(link), "t1", now=BASE_NOW)
        workflow_link = self.root / "workflow-link.json"
        workflow_link.symlink_to(self.workflow)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            hb.tick(str(workflow_link), AUTO_ID, THREAD_ID, str(self.watch), "t1", now=BASE_NOW)

    def test_watch_dir_overlapping_protected_roots_is_refused(self):
        # runner 目录本身是受保护根。
        with self.assertRaisesRegex(ValueError, "WATCH_DIR_OVERLAPS_PROTECTED_ROOT"):
            hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(self.runtime), "t1", now=BASE_NOW)
        # 本轮 receipts 目录本身是受保护根（workspace 本身不被整体保护）。
        inside_receipts = self.workspace / "receipts" / self.task_id / ATTEMPT_ID / "watch"
        inside_receipts.mkdir(parents=True, mode=0o700)
        with self.assertRaisesRegex(ValueError, "WATCH_DIR_OVERLAPS_PROTECTED_ROOT"):
            hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(inside_receipts), "t2",
                    now=BASE_NOW)

    def test_watch_writes_only_within_itself(self):
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.tick("t1")
        for path, content in before.items():
            if self.watch not in path.parents:
                self.assertEqual(content, path.read_bytes(), path.name)
        self.assertTrue((self.watch / "state.json").is_file())
        self.assertTrue(any(self.watch.glob("directives/*.json")))

    def test_corrupted_watch_state_is_refused_not_reset(self):
        self.tick("t1")
        (self.watch / "state.json").write_text(json.dumps({"schemaVersion": "bogus.v9"}))
        with self.assertRaisesRegex(ValueError, "WATCH_STATE_SCHEMA_UNSUPPORTED"):
            self.tick("t2")

    def test_naive_now_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch), "t1",
                    now=datetime(2026, 10, 9, 12, 0, 0))


class HeartbeatAcknowledgeTests(HeartbeatFixtureBase):
    def prepare_error(self):
        self.write_host_runtime(state="unknown")
        directive = self.tick("t1")
        return directive["data"]["errorFingerprint"]

    def test_reconciliation_evidence_schema_is_frozen(self):
        fingerprint = self.prepare_error()
        base = {"taskId": self.task_id, "attemptId": ATTEMPT_ID, "fingerprint": fingerprint,
                "processChecked": True, "sourceChecked": True, "receiptsChecked": True}
        cases = [
            {**base, "extra": True},
            {k: v for k, v in base.items() if k != "sourceChecked"},
            {**base, "sourceChecked": False},
            {**base, "fingerprint": "other"},
            {**base, "taskId": "task-other"},
        ]
        for index, evidence in enumerate(cases):
            path = self.root / ("bad-evidence-%d.json" % index)
            path.write_text(json.dumps(evidence))
            with self.assertRaises(ValueError, msg=index):
                hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                              str(self.watch), fingerprint, str(path))
        self.assertNotIn(fingerprint, self.watch_state().get("reconciled", {}))
        good = self.write_reconciliation_evidence(fingerprint)
        result = hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                               str(self.watch), fingerprint, str(good))
        self.assertTrue(result["ok"])
        self.assertIn(fingerprint, self.watch_state()["reconciled"])

    def test_reconciliation_requires_observed_fingerprint(self):
        self.tick("t1")  # 先建立 watch state（其中不含该 fingerprint）
        evidence = self.write_reconciliation_evidence("UNOBSERVED:task/attempt")
        with self.assertRaisesRegex(ValueError, "FINGERPRINT_NOT_OBSERVED"):
            hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                          str(self.watch), "UNOBSERVED:task/attempt",
                                          str(evidence))

    def test_pause_needs_official_receipt_and_durable_record(self):
        fingerprint = self.prepare_error()
        for name in ("t2", "t3"):
            self.tick(name)
        evidence = self.write_reconciliation_evidence(fingerprint)
        hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                      str(self.watch), fingerprint, str(evidence))
        self.tick("t4")
        receipt_path = self.root / "official-receipt.json"
        receipt_path.write_text(json.dumps({"automationId": AUTO_ID,
                                            "ownerThreadId": THREAD_ID, "status": "PAUSED"}))
        durable_path = self.root / "durable.toml"
        durable_path.write_text('id = "%s"\nkind = "heartbeat"\n'
                                'target_thread_id = "%s"\nstatus = "PAUSED"\n'
                                % (AUTO_ID, THREAD_ID))
        # 只有官方回执、没有 durable 记录：失败，不得标记 confirmed。
        with self.assertRaises(ValueError):
            hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                 fingerprint, str(receipt_path), str(self.root / "missing.toml"))
        self.assertNotIn(fingerprint, self.watch_state().get("pauseConfirmed", {}))
        # durable 内容不符：同样失败。
        bad_durable = self.root / "bad-durable.toml"
        bad_durable.write_text('id = "%s"\nkind = "heartbeat"\n'
                               'target_thread_id = "thread-other"\nstatus = "PAUSED"\n'
                               % AUTO_ID)
        with self.assertRaises(ValueError):
            hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                 fingerprint, str(receipt_path), str(bad_durable))
        self.assertNotIn(fingerprint, self.watch_state().get("pauseConfirmed", {}))
        # 官方回执 status != PAUSED：失败。
        bad_receipt = self.root / "bad-receipt.json"
        bad_receipt.write_text(json.dumps({"automationId": AUTO_ID,
                                           "ownerThreadId": THREAD_ID, "status": "RUNNING"}))
        with self.assertRaises(ValueError):
            hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                 fingerprint, str(bad_receipt), str(durable_path))
        # 官方回执 + durable 精确 PAUSED：confirmed，其后 tick 不再输出 PAUSE intent。
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(receipt_path), str(durable_path))
        self.assertTrue(result["ok"])
        self.assertIn(fingerprint, self.watch_state()["pauseConfirmed"])
        directive = self.tick("t5")
        self.assertEqual(directive["action"], "WAIT")
        self.assertIsNone(directive["platformAction"])


class HeartbeatResumeTests(HeartbeatFixtureBase):
    def authorization(self):
        return {"ownerThreadId": THREAD_ID, "automationId": AUTO_ID,
                "userExplicitContinue": True, "maxRuns": 3, "intervalMinutes": 15,
                "reconciliationComplete": True}

    def test_resume_passes_only_with_complete_conditions(self):
        snapshot = self.snapshot()
        result = hb.validate_resume(snapshot, self.authorization())
        self.assertTrue(result["ok"])

    def test_resume_rejects_each_missing_condition(self):
        snapshot = self.snapshot()
        bad = self.authorization()
        bad["maxRuns"] = 0
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "MAX_RUNS_INVALID")
        bad = self.authorization()
        bad["maxRuns"] = True
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "MAX_RUNS_INVALID")
        bad = self.authorization()
        bad["intervalMinutes"] = 4
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "INTERVAL_TOO_SHORT")
        bad = self.authorization()
        bad["userExplicitContinue"] = "yes"
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "USER_CONTINUE_NOT_EXPLICIT")
        bad = self.authorization()
        bad["reconciliationComplete"] = False
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "RECONCILIATION_INCOMPLETE")
        bad = self.authorization()
        bad["ownerThreadId"] = "thread-other"
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "OWNER_THREAD_ID_MISMATCH")
        bad = self.authorization()
        bad["automationId"] = "auto-other"
        self.assertEqual(hb.validate_resume(snapshot, bad)["reason"], "AUTOMATION_ID_MISMATCH")
        self.assertEqual(hb.validate_resume(snapshot, {"ownerThreadId": THREAD_ID})["reason"],
                         "AUTHORIZATION_SCHEMA_INVALID")

    def test_resume_rejects_old_unknown_attempt(self):
        self.write_host_runtime(state="unknown")
        snapshot = self.snapshot()
        result = hb.validate_resume(snapshot, self.authorization())
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "OLD_UNKNOWN_STILL_PRESENT")


class HeartbeatCliTests(HeartbeatFixtureBase):
    def test_cli_tick_outputs_single_controlled_json(self):
        import contextlib
        import io
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = hb.main(["tick", "--workflow", str(self.workflow), "--automation-id", AUTO_ID,
                            "--owner-thread-id", THREAD_ID, "--watch-dir", str(self.watch),
                            "--tick-id", "cli-1"])
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schemaVersion"], "d4-heartbeat-directive.v1")
        self.assertIn(payload["action"], ("WAIT", "REVIEW", "RECONCILE", "PAUSE", "NEXT"))
        serialized = json.dumps(payload)
        self.assertNotIn("prompt", serialized.lower().replace("promptsha", "x")[:0] or serialized)
        for forbidden in ("goal", "permissions", "acceptance"):
            self.assertNotIn(forbidden, serialized)

    def test_cli_unknown_error_refused_with_single_json(self):
        import contextlib
        import io
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = hb.main(["tick", "--workflow", str(self.workflow), "--automation-id", AUTO_ID,
                            "--owner-thread-id", THREAD_ID, "--watch-dir", str(self.root),
                            "--tick-id", "cli-2"])
        self.assertEqual(code, 1)
        payload = json.loads(stdout.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["type"], "ValueError")

    def test_cli_replay_returns_same_directive(self):
        import contextlib
        import io

        def run(tick_id):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                hb.main(["tick", "--workflow", str(self.workflow), "--automation-id", AUTO_ID,
                         "--owner-thread-id", THREAD_ID, "--watch-dir", str(self.watch),
                         "--tick-id", tick_id])
            return stdout.getvalue()

        first = run("replay-1")
        self.assertEqual(first, run("replay-1"))


class HeartbeatRunnerPointerTests(HeartbeatFixtureBase):
    """currentRunner / runtimeDir 当前指针接线（acceptance[3][8]）。"""

    def test_runtime_dir_only_is_supported(self):
        document = self.write_workflow()
        document.pop("currentRunner")
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "WAIT")
        self.assertIsNone(self.snapshot()["anomaly"])

    def test_current_runner_only_is_supported(self):
        document = self.write_workflow()
        document.pop("runtimeDir")
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "WAIT")
        self.assertIsNone(self.snapshot()["anomaly"])

    def test_current_runner_and_runtime_dir_conflict_reconciles(self):
        other = self.root / "runner-other"
        other.mkdir(mode=0o700)
        document = self.write_workflow()
        document["currentRunner"] = str(other)
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "RUNNER_PATH_CONFLICT")

    def test_missing_runner_paths_report_unreadable(self):
        document = self.write_workflow()
        document.pop("currentRunner")
        document.pop("runtimeDir")
        self.workflow.write_text(json.dumps(document))
        snapshot = self.snapshot()
        self.assertEqual(snapshot["errorCode"], "RUNNER_EVIDENCE_UNREADABLE")
        self.assertTrue(snapshot["reconciliationRequired"])


class HeartbeatProtectedPathTests(HeartbeatFixtureBase):
    """准确保护路径边界（acceptance[2][8]）。"""

    def test_same_parent_sibling_watch_is_usable(self):
        sibling = self.root / "watch-sibling"
        sibling.mkdir(mode=0o700)
        directive = hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(sibling), "t1",
                            now=BASE_NOW)
        self.assertEqual(directive["action"], "WAIT")

    def test_workspace_and_home_are_not_blanket_protected(self):
        workspace_watch = self.workspace / "watch"
        workspace_watch.mkdir(mode=0o700)
        directive = hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(workspace_watch),
                            "t1", now=BASE_NOW)
        self.assertEqual(directive["action"], "WAIT")
        home_watch = self.home / "watch-home"
        home_watch.mkdir(mode=0o700)
        directive = hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(home_watch),
                            "t1", now=BASE_NOW)
        self.assertEqual(directive["action"], "WAIT")

    def test_watch_overlapping_each_protected_root_is_refused(self):
        receipts = self.workspace / "receipts" / self.task_id / ATTEMPT_ID
        receipts.mkdir(parents=True)
        attempt_dir = self.home / "d4-workbuddy-store" / "attempts" / ATTEMPT_ID
        candidates = [self.root, self.runtime, attempt_dir, receipts]
        for index, watch in enumerate(candidates):
            os.chmod(watch, 0o700)
            with self.assertRaisesRegex(ValueError, "WATCH_DIR_OVERLAPS_PROTECTED_ROOT",
                                        msg=str(watch)):
                hb.tick(str(self.workflow), AUTO_ID, THREAD_ID, str(watch),
                        "overlap-%d" % index, now=BASE_NOW)


class HeartbeatEvidenceSafetyTests(HeartbeatFixtureBase):
    """只读证据与 symlink 安全（acceptance[4][5][8]）。"""

    def test_read_only_db_and_workflow_are_not_modified(self):
        before_db = self.db.read_bytes()
        before_workflow = self.workflow.read_bytes()
        self.tick("t1")
        self.assertEqual(before_db, self.db.read_bytes())
        self.assertEqual(before_workflow, self.workflow.read_bytes())

    def test_stopped_runner_without_terminal_is_execution_unknown(self):
        self.release_runner_lock()  # 显式停 runner，而非 mock PID
        snapshot = self.snapshot()
        self.assertFalse(snapshot["runnerAlive"])
        self.assertEqual(snapshot["runnerStatus"], "execution_unknown")
        self.assertEqual(snapshot["anomaly"]["code"], "EXECUTION_UNKNOWN")

    def test_symlinked_receipt_file_is_not_read(self):
        receipts = self.workspace / "receipts" / self.task_id / ATTEMPT_ID
        receipts.mkdir(parents=True)
        outside = self.root / "outside-events.json"
        outside.write_text('{"x": 1}')
        (receipts / "events.json").symlink_to(outside)
        snapshot = self.snapshot()
        self.assertIsNone(snapshot["receiptDigest"])

    def test_watch_state_symlink_is_refused(self):
        self.tick("t1")
        state_path = self.watch / "state.json"
        state_path.unlink()
        target = self.root / "outside-state.json"
        target.write_text("{}")
        state_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "WATCH_STATE_SYMLINK_REFUSED"):
            self.tick("t2")

    def test_directive_cache_symlink_is_refused(self):
        self.tick("t1")
        cache = hb._directive_cache_path(self.watch, "t2")
        target = self.root / "outside-directive.json"
        target.write_text("{}")
        cache.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "DIRECTIVE_CACHE_SYMLINK_REFUSED"):
            self.tick("t2")


class HeartbeatBindingTests(HeartbeatFixtureBase):
    """workflow 顶层绑定字段校验（acceptance[2]）：不能落 WAIT。"""

    def test_workflow_db_mismatch_reconciles(self):
        other = self.root / "other.sqlite"
        other.write_text("")
        document = self.write_workflow()
        document["db"] = str(other)
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "DB_MISMATCH")

    def test_empty_allowed_agents_reconciles(self):
        document = self.write_workflow()
        document["allowedAgents"] = []
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "WORKFLOW_BINDING_INVALID")

    def test_source_mismatch_reconciles(self):
        document = self.write_workflow()
        document["allowedSource"] = "other-source"
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "BINDING_NOT_ALLOWED")

    def test_agent_not_allowed_reconciles(self):
        document = self.write_workflow()
        document["allowedAgents"] = ["someone-else"]
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "BINDING_NOT_ALLOWED")

    def test_missing_or_wrong_type_binding_fields_reconcile(self):
        cases = []
        for field in ("db", "allowedAgents", "allowedSource"):
            document = self.write_workflow()
            document.pop(field)
            cases.append((field, document))
        wrong = self.write_workflow()
        wrong["allowedAgents"] = "dev-wkb1"
        cases.append(("allowedAgents-type", wrong))
        wrong = self.write_workflow()
        wrong["db"] = 12345
        cases.append(("db-type", wrong))
        for label, document in cases:
            self.workflow.write_text(json.dumps(document))
            directive = self.tick("t-%s" % label)
            self.assertEqual(directive["action"], "RECONCILE", label)
            self.assertEqual(directive["data"]["errorCode"], "WORKFLOW_BINDING_INVALID", label)


class HeartbeatSymlinkSafetyTests(HeartbeatFixtureBase):
    """普通文件读写拒绝末级与祖先 symlink（acceptance[3][4]）。"""

    def test_read_rejects_final_and_ancestor_symlinks(self):
        real = self.root / "real-evidence"
        real.mkdir()
        (real / "events.json").write_text("{}")
        # 正例：真实普通文件可读。
        self.assertIsNotNone(hb._read_regular_file(real / "events.json", root=self.root))
        # 负例：末级 symlink（即使解析目标仍在允许根内）。
        leaf = self.root / "leaf-events.json"
        leaf.symlink_to(real / "events.json")
        self.assertIsNone(hb._read_regular_file(leaf, root=self.root))
        # 负例：祖先是 symlink（解析目标仍在允许根内也不读）。
        ancestor = self.root / "ancestor-link"
        ancestor.symlink_to(real, target_is_directory=True)
        self.assertIsNone(hb._read_regular_file(ancestor / "events.json", root=self.root))

    def test_receipts_ancestor_symlink_is_not_read(self):
        receipts = self.workspace / "receipts" / self.task_id / ATTEMPT_ID
        receipts.mkdir(parents=True)
        (receipts / "events.json").write_text('{"x": 1}')
        self.assertIsNotNone(self.snapshot()["receiptDigest"])
        outside = self.root / "outside-receipts"
        outside.mkdir()
        (outside / "events.json").write_text('{"x": 2}')
        (receipts / "events.json").unlink()
        receipts.rmdir()
        receipts.symlink_to(outside, target_is_directory=True)
        self.assertIsNone(self.snapshot()["receiptDigest"])

    def test_directives_dir_symlink_is_refused_without_external_writes(self):
        outside = self.root / "outside-directives"
        outside.mkdir(mode=0o700)
        (self.watch / "directives").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "DIRECTIVE_DIR_SYMLINK_REFUSED"):
            self.tick("t1")
        self.assertEqual(list(outside.iterdir()), [])
        # 指向内部目录的 symlink 同样拒绝。
        (self.watch / "directives").unlink()
        inner = self.root / "inner-directives"
        inner.mkdir(mode=0o700)
        (self.watch / "directives").symlink_to(inner, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "DIRECTIVE_DIR_SYMLINK_REFUSED"):
            self.tick("t2")
        self.assertEqual(list(inner.iterdir()), [])


class HeartbeatDeadlinePauseChainTests(HeartbeatFixtureBase):
    """deadline 暂停指纹与确认全链（acceptance[5]）；不真实调用官方暂停/宿主。"""

    OVERDUE = BASE_NOW + timedelta(seconds=120)

    def drive_to_pause(self):
        for tick_id in ("t1", "t2", "t3"):
            self.assertEqual(self.tick(tick_id, now=self.OVERDUE)["action"], "WAIT", tick_id)
        directive = self.tick("t4", now=self.OVERDUE)
        self.assertEqual(directive["action"], "PAUSE")
        return directive

    def test_deadline_pause_exposes_observed_fingerprint_and_confirms(self):
        directive = self.drive_to_pause()
        fingerprint = directive["data"]["errorFingerprint"]
        self.assertTrue(fingerprint)
        self.assertIn("NO_PROGRESS_AFTER_DEADLINE", fingerprint)
        self.assertEqual(self.watch_state()["noProgress"]["fingerprint"], fingerprint)
        receipt = self.root / "official-receipt.json"
        receipt.write_text(json.dumps({"automationId": AUTO_ID,
                                       "ownerThreadId": THREAD_ID, "status": "PAUSED"}))
        durable = self.root / "durable.toml"
        durable.write_text('id = "%s"\nkind = "heartbeat"\n'
                           'target_thread_id = "%s"\nstatus = "PAUSED"\n'
                           % (AUTO_ID, THREAD_ID))
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(receipt), str(durable))
        self.assertTrue(result["ok"])
        self.assertIn(fingerprint, self.watch_state()["pauseConfirmed"])
        after = self.tick("t5", now=self.OVERDUE)
        self.assertEqual(after["action"], "WAIT")
        self.assertIsNone(after["platformAction"])

    def test_pause_confirmation_rejects_tampered_evidence(self):
        fingerprint = self.drive_to_pause()["data"]["errorFingerprint"]
        good_receipt = self.root / "receipt-ok.json"
        good_receipt.write_text(json.dumps({"automationId": AUTO_ID,
                                            "ownerThreadId": THREAD_ID, "status": "PAUSED"}))
        good_durable = self.root / "durable-ok.toml"
        good_durable.write_text('id = "%s"\nkind = "heartbeat"\n'
                                'target_thread_id = "%s"\nstatus = "PAUSED"\n'
                                % (AUTO_ID, THREAD_ID))
        active_durable = self.root / "durable-active.toml"
        active_durable.write_text('id = "%s"\nkind = "heartbeat"\n'
                                  'target_thread_id = "%s"\nstatus = "ACTIVE"\n'
                                  % (AUTO_ID, THREAD_ID))
        bad_automation = self.root / "receipt-automation.json"
        bad_automation.write_text(json.dumps({"automationId": "auto-other",
                                              "ownerThreadId": THREAD_ID, "status": "PAUSED"}))
        bad_owner = self.root / "receipt-owner.json"
        bad_owner.write_text(json.dumps({"automationId": AUTO_ID,
                                         "ownerThreadId": "thread-other", "status": "PAUSED"}))
        cases = [
            ("active-durable", fingerprint, good_receipt, active_durable),
            ("bad-automation", fingerprint, bad_automation, good_durable),
            ("bad-owner", fingerprint, bad_owner, good_durable),
            ("bad-fingerprint", "NOPE:task/attempt", good_receipt, good_durable),
            ("wrong-task", fingerprint.replace(self.task_id, "task-other"),
             good_receipt, good_durable),
            ("wrong-attempt", fingerprint.replace(ATTEMPT_ID, "att-other"),
             good_receipt, good_durable),
        ]
        for label, candidate, receipt, durable in cases:
            with self.assertRaises(ValueError, msg=label):
                hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                     candidate, str(receipt), str(durable))
            self.assertNotIn(candidate, self.watch_state().get("pauseConfirmed", {}), label)
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(good_receipt), str(good_durable))
        self.assertTrue(result["ok"])


class HeartbeatHostEvidenceTests(HeartbeatFixtureBase):
    """宿主 runtime 身份绑定与模型回显（acceptance[3][4]）。"""

    def runtime_path(self):
        return (self.home / "d4-workbuddy-store" / "attempts" / ATTEMPT_ID / "runtime.json")

    def test_valid_host_runtime_is_accepted_without_anomaly(self):
        snapshot = self.snapshot()
        self.assertIsNone(snapshot["anomaly"])
        self.assertEqual(snapshot["hostRuntimeState"], "running")
        self.assertIsNone(snapshot["observedModel"])

    def test_wrong_session_id_reconciles_and_echo_not_adopted(self):
        self.write_host_runtime(sessionId="att-other", observedModel="glm-5.3-flash")
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")
        self.assertIsNone(self.snapshot()["observedModel"])

    def test_wrong_workspace_reconciles(self):
        other = self.root / "other-workspace"
        other.mkdir()
        self.write_host_runtime(workspace=str(other))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")

    def test_wrong_requested_model_reconciles(self):
        self.write_host_runtime(requestedModel="deepseek-v4.1-flash")
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")

    def test_observed_session_or_workspace_mismatch_reconciles(self):
        self.write_host_runtime(observedSessionId="att-other")
        self.assertEqual(self.tick("t1")["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")
        self.write_host_runtime(observedWorkspace=str(self.root))
        self.assertEqual(self.tick("t2")["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")

    def test_wrong_type_fields_reconcile(self):
        self.write_host_runtime(sessionId=123)
        self.assertEqual(self.tick("t1")["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")
        self.write_host_runtime(requestedModel=None)
        self.assertEqual(self.tick("t2")["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")

    def test_observed_model_mismatch_blocks_review_with_reconcile(self):
        self.insert_delivery()
        self.write_host_runtime(observedModel="deepseek-v4.1-flash")
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_MISMATCH")

    def test_init_without_host_runtime_waits(self):
        self.runtime_path().unlink()
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.update_attempt(ATTEMPT_ID, accepted_by_host_at=None)
        finally:
            store.close()
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "WAIT")
        self.assertIsNone(directive["data"]["errorCode"])
        self.assertIsNone(self.snapshot()["observedModel"])

    def test_accepted_with_missing_host_runtime_reconciles(self):
        self.runtime_path().unlink()
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "HOST_RUNTIME_EVIDENCE_MISSING")

    def test_delivery_with_damaged_host_runtime_reconciles(self):
        self.insert_delivery()
        self.runtime_path().write_text("{ not valid json")
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "HOST_RUNTIME_EVIDENCE_MISSING")

    def test_approved_delivery_with_host_mismatch_reconciles_first(self):
        self.insert_delivery()
        self.insert_review()
        self.write_host_runtime(sessionId="att-other")
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "HOST_RUNTIME_BINDING_INVALID")


class HeartbeatModelPolicyTests(HeartbeatFixtureBase):
    """冻结 modelPolicy 一致性 / 生效与授权时间 / 撤销（acceptance[5]）。"""

    def test_valid_policy_passes(self):
        self.assertIsNone(self.snapshot()["anomaly"])

    def test_missing_frozen_policy_reconciles(self):
        config = self.write_config()
        config.pop("modelPolicy")
        config.pop("modelPolicySha256")
        (self.runtime / "config.json").write_text(json.dumps(config))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_MISSING")

    def test_frozen_sha_mismatch_reconciles(self):
        config = self.write_config()
        config["modelPolicySha256"] = "0" * 64
        (self.runtime / "config.json").write_text(json.dumps(config))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_DRIFT")

    def test_policy_drift_reconciles(self):
        document = self.write_workflow()
        document["modelPolicy"] = dict(self.policy, allowedModels=["glm-5.3-flash", "extra-model"])
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_DRIFT")

    def test_revoked_policy_reconciles(self):
        self.apply_policy(dict(self.policy, allowedModels=[]))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_NOT_ALLOWED")

    def test_future_effective_time_reconciles(self):
        self.apply_policy(dict(self.policy, effectiveAt="2099-01-01T00:00:00+00:00"))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_NOT_ALLOWED")

    def test_future_authorized_time_reconciles(self):
        self.apply_policy(dict(self.policy, authorizedAt="2099-01-01T00:00:00+00:00"))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_NOT_ALLOWED")

    def test_bad_schema_reconciles(self):
        self.apply_policy(dict(self.policy, schemaVersion="d4-model-policy.v9"))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_NOT_ALLOWED")

    def test_transparent_fallback_enabled_reconciles(self):
        self.apply_policy(dict(self.policy, transparentFallback=True))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_NOT_ALLOWED")

    def test_approved_delivery_with_policy_drift_reconciles_first(self):
        self.insert_delivery()
        self.insert_review()
        document = self.write_workflow()
        document["modelPolicy"] = dict(self.policy, allowedModels=["glm-5.3-flash", "extra-model"])
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "MODEL_POLICY_DRIFT")

    def test_valid_delivery_still_reviews(self):
        self.insert_delivery()
        directive = self.tick("t1")
        self.assertEqual(directive["action"], "REVIEW")


_R8_MISSING = object()


class HeartbeatHostHomeAndRootTests(HeartbeatFixtureBase):
    """R8 缺口回归：无效 config.home 与未授权 workspace 根（acceptance[3][4][5]）。

    全部 synthetic；不启动真实 runner / 宿主 / 官方暂停，不写业务数据。
    """

    def runtime_path(self):
        return (self.home / "d4-workbuddy-store" / "attempts" / ATTEMPT_ID / "runtime.json")

    def rewrite_config(self, **changes):
        """就地重写冻结 config（镜像只读读取），供本轮边界注入使用。"""
        config = json.loads((self.runtime / "config.json").read_text())
        for key, value in changes.items():
            if value is _R8_MISSING:
                config.pop(key, None)
            else:
                config[key] = value
        (self.runtime / "config.json").write_text(json.dumps(config))
        return config

    def assert_reconcile(self, tick_id, code="HOST_RUNTIME_BINDING_INVALID"):
        directive = self.tick(tick_id)
        self.assertEqual(directive["action"], "RECONCILE", tick_id)
        self.assertEqual(directive["data"]["errorCode"], code, tick_id)
        return directive

    # -- config.home 结构性异常（acceptance[3]）--

    def test_invalid_home_reconciles_even_with_approved_delivery(self):
        self.insert_delivery()
        self.insert_review()  # 已有 approved delivery：不得借此跳过 home 校验。
        cases = [("missing", _R8_MISSING), ("empty", ""), ("integer", 123),
                 ("relative", "relative/home"), ("dotdot", str(self.root / ".."))]
        for index, (label, value) in enumerate(cases):
            self.rewrite_config(home=value)
            with self.subTest(label):
                self.assert_reconcile("home-bad-%d" % index)
        # 合法 home 恢复后，approved delivery 回到原 NEXT 语义（未破坏既有路径）。
        self.rewrite_config(home=str(self.home))
        self.assertEqual(self.tick("home-restored")["action"], "NEXT")

    def test_invalid_home_reconciles_at_initialization(self):
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.update_attempt(ATTEMPT_ID, accepted_by_host_at=None)
        finally:
            store.close()
        self.runtime_path().unlink()
        self.rewrite_config(home="")
        self.assert_reconcile("home-init-empty")

    def test_symlinked_home_reconciles(self):
        self.insert_delivery()
        link = self.root / "home-link"
        link.symlink_to(self.home, target_is_directory=True)
        self.rewrite_config(home=str(link))
        self.assert_reconcile("home-leaf-symlink")
        ancestor = self.root / "home-ancestor-link"
        ancestor.symlink_to(self.root, target_is_directory=True)
        self.rewrite_config(home=str(ancestor / "real-home"))
        self.assert_reconcile("home-ancestor-symlink")

    def test_valid_home_keeps_init_wait_and_accepted_reconcile(self):
        # 合法 home + 未 accepted + 无 delivery + runtime 不存在 → 初始化 WAIT（原语义）。
        self.runtime_path().unlink()
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.update_attempt(ATTEMPT_ID, accepted_by_host_at=None)
        finally:
            store.close()
        directive = self.tick("valid-home-init")
        self.assertEqual(directive["action"], "WAIT")
        self.assertIsNone(directive["data"]["errorCode"])
        # 合法 home + 已 accepted + runtime 缺失 → 仍 RECONCILE（原语义）。
        store = SqliteStore(self.db)
        try:
            with store.transaction():
                store.update_attempt(ATTEMPT_ID,
                                     accepted_by_host_at="2026-10-09T11:00:02+00:00")
        finally:
            store.close()
        self.assert_reconcile("valid-home-accepted",
                              code="HOST_RUNTIME_EVIDENCE_MISSING")

    # -- workspace 授权根（acceptance[4]）--

    def test_workspace_not_under_allowed_root_reconciles(self):
        other = self.root / "allowed-other"
        other.mkdir()
        self.rewrite_config(allowedRoot=str(other))
        self.assert_reconcile("allowed-root-other")

    def test_allowed_root_same_name_prefix_sibling_reconciles(self):
        # 字符串前缀兄弟：workspace=<root>/workspace vs allowedRoot=<root>/workspace-sibling。
        self.rewrite_config(allowedRoot=str(self.workspace) + "-sibling")
        self.assert_reconcile("allowed-root-prefix")

    def test_allowed_root_missing_relative_or_dotdot_reconciles(self):
        self.rewrite_config(allowedRoot=_R8_MISSING)
        self.assert_reconcile("allowed-root-missing")
        self.rewrite_config(allowedRoot="relative/root")
        self.assert_reconcile("allowed-root-relative")
        self.rewrite_config(allowedRoot=str(self.root / ".."))
        self.assert_reconcile("allowed-root-dotdot")

    def test_allowed_root_ancestor_symlink_reconciles(self):
        real = self.root / "real-root"
        real.mkdir()
        link = self.root / "root-link"
        link.symlink_to(real, target_is_directory=True)
        self.rewrite_config(allowedRoot=str(link))
        self.assert_reconcile("allowed-root-symlink")

    def test_workspace_not_under_policy_roots_reconciles(self):
        other = self.root / "policy-other"
        other.mkdir()
        self.apply_policy(dict(self.policy, allowedRoots=[str(other)]))
        self.assert_reconcile("policy-roots-other")

    def test_policy_roots_same_name_prefix_sibling_reconciles(self):
        self.apply_policy(dict(self.policy, allowedRoots=[str(self.workspace) + "-sibling"]))
        self.assert_reconcile("policy-roots-prefix")

    def test_invalid_policy_roots_reconcile_via_policy_gate(self):
        # 空 / 相对 / symlink 根先被既有 policy 门禁拒绝（仍为结构性 RECONCILE，优先 REVIEW/NEXT）。
        self.apply_policy(dict(self.policy, allowedRoots=[]))
        self.assert_reconcile("policy-roots-empty", code="MODEL_POLICY_NOT_ALLOWED")
        self.apply_policy(dict(self.policy, allowedRoots=["relative/root"]))
        self.assert_reconcile("policy-roots-relative", code="MODEL_POLICY_NOT_ALLOWED")
        real = self.root / "policy-real"
        real.mkdir()
        link = self.root / "policy-link"
        link.symlink_to(real, target_is_directory=True)
        self.apply_policy(dict(self.policy, allowedRoots=[str(link)]))
        self.assert_reconcile("policy-roots-symlink", code="MODEL_POLICY_NOT_ALLOWED")

    def test_valid_roots_keep_review_and_approved_semantics(self):
        self.insert_delivery()
        self.assertEqual(self.tick("valid-roots-review")["action"], "REVIEW")
        self.insert_review()
        self.assertEqual(self.tick("valid-roots-next")["action"], "NEXT")

    def test_roots_reconcile_takes_priority_over_review(self):
        self.insert_delivery()  # 合法未审 delivery：仍必须先 RECONCILE。
        other = self.root / "allowed-other-priority"
        other.mkdir()
        self.rewrite_config(allowedRoot=str(other))
        self.assert_reconcile("roots-priority-review")


class HeartbeatOfficialPauseReceiptTests(HeartbeatFixtureBase):
    """原始官方工具回执 + 完整 durable 记录接线（acceptance[3][4][6]）。

    全部 synthetic：直接写入文件级「原始官方 automation_update 工具回执形状」JSON 与
    完整 automation.toml 形状 durable（含额外字段与转义）；不调用官方工具、不伪造真实
    暂停、不接触真实账号。worker 本轮不执行 Bash，由 cox 独立 unittest 验证。
    """

    def prepare_fingerprint(self):
        """驱动到已观察且已对账的 PAUSE 阈值指纹，返回该 fingerprint。"""
        self.write_host_runtime(state="unknown")
        directive = self.tick("t1")
        fingerprint = directive["data"]["errorFingerprint"]
        for name in ("t2", "t3"):
            self.tick(name)
        evidence = self.write_reconciliation_evidence(fingerprint)
        hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                      str(self.watch), fingerprint, str(evidence))
        self.tick("t4")  # 达 ERROR_PAUSE_THRESHOLD 且已对账 → 可确认 PAUSE。
        return fingerprint

    def tool_document(self, payload=None, leading="Updated automation in the app.",
                      is_error=False, extra_blocks=None):
        """构造原始官方 automation_update 工具回执形状（synthetic，非真实回执）。"""
        if payload is None:
            payload = {"automationId": AUTO_ID, "mode": "update", "status": "PAUSED"}
        blocks = [{"type": "text", "text": leading},
                  {"type": "text", "text": json.dumps(payload)}]
        if extra_blocks:
            blocks.extend(extra_blocks)
        return {"content": blocks, "isError": is_error}

    def write_receipt_file(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document))
        return path

    def full_durable_text(self, automation_id=None, owner=None, kind="heartbeat",
                          status="PAUSED"):
        """真实 automation.toml 形状：额外标量字段 + prompt 含 \\n \\" \\\\ \\uXXXX 转义。"""
        aid = AUTO_ID if automation_id is None else automation_id
        tid = THREAD_ID if owner is None else owner
        lines = [
            'version = 1',
            'id = "%s"' % aid,
            'kind = "%s"' % kind,
            'name = "D4 automation guard"',
            'prompt = "line1\\nline2 \\"quoted\\" back\\\\slash u\\u4e2d"',
            'status = "%s"' % status,
            'rrule = "FREQ=MINUTELY;INTERVAL=5;COUNT=12"',
            'target_thread_id = "%s"' % tid,
            'created_at = 1791528575678',
            'updated_at = 1791558330571',
        ]
        return "\n".join(lines) + "\n"

    def write_durable_file(self, name, text):
        path = self.root / name
        path.write_text(text)
        return path

    def assert_rejected(self, fingerprint, receipt, durable, label):
        """任何拒绝：抛受控 ValueError，且不写 pauseConfirmed（确认状态不变）。"""
        with self.assertRaises(ValueError, msg=label):
            hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                 fingerprint, str(receipt), str(durable))
        self.assertNotIn(fingerprint, self.watch_state().get("pauseConfirmed", {}), label)

    def test_raw_tool_receipt_with_full_durable_succeeds(self):
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("tool-receipt.json", self.tool_document())
        durable = self.write_durable_file("full-durable.toml", self.full_durable_text())
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(receipt), str(durable))
        self.assertTrue(result["ok"])
        confirmed = self.watch_state()["pauseConfirmed"][fingerprint]
        self.assertEqual(confirmed["officialReceiptSha256"],
                         hb.sha256_bytes(receipt.read_bytes()))
        self.assertEqual(confirmed["durableRecordSha256"],
                         hb.sha256_bytes(durable.read_bytes()))

    def test_legacy_three_field_receipt_still_accepted(self):
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("legacy-receipt.json", {
            "automationId": AUTO_ID, "ownerThreadId": THREAD_ID, "status": "PAUSED"})
        durable = self.write_durable_file("full-durable-legacy.toml",
                                          self.full_durable_text())
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(receipt), str(durable))
        self.assertTrue(result["ok"])
        self.assertIn(fingerprint, self.watch_state()["pauseConfirmed"])

    def test_escaped_prompt_and_extra_fields_are_parsed(self):
        parsed = hb._parse_restricted_toml(self.full_durable_text())
        self.assertEqual(parsed["prompt"], 'line1\nline2 "quoted" back\\slash u中')
        self.assertEqual(parsed["version"], 1)
        self.assertEqual(parsed["created_at"], 1791528575678)
        self.assertEqual(parsed["id"], AUTO_ID)

    def test_durable_wrong_id_rejected(self):
        # 核心：durable.id 必须 exact == automationId。
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("tool-id.json", self.tool_document())
        wrong = self.write_durable_file("durable-wrong-id.toml",
                                        self.full_durable_text(automation_id="auto-other"))
        self.assert_rejected(fingerprint, receipt, wrong, "wrong-id")

    def test_durable_wrong_owner_kind_status_rejected(self):
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("tool-owner.json", self.tool_document())
        cases = [
            ("wrong-owner", self.full_durable_text(owner="thread-other")),
            ("wrong-kind", self.full_durable_text(kind="cron")),
            ("active", self.full_durable_text(status="ACTIVE")),
        ]
        for label, text in cases:
            durable = self.write_durable_file("durable-%s.toml" % label, text)
            self.assert_rejected(fingerprint, receipt, durable, label)

    def test_durable_structure_failures_rejected(self):
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("tool-struct.json", self.tool_document())
        duplicate = self.write_durable_file("durable-dup.toml",
            'id = "%s"\nid = "%s"\nkind = "heartbeat"\n'
            'target_thread_id = "%s"\nstatus = "PAUSED"\n' % (AUTO_ID, AUTO_ID, THREAD_ID))
        corrupt = self.write_durable_file("durable-corrupt.toml",
            'id = "%s\nkind = "heartbeat"\n' % AUTO_ID)
        unsupported = self.write_durable_file("durable-array.toml",
            'id = "%s"\nkind = "heartbeat"\ntags = [1, 2]\n'
            'target_thread_id = "%s"\nstatus = "PAUSED"\n' % (AUTO_ID, THREAD_ID))
        missing_key = self.write_durable_file("durable-missing.toml",
            'id = "%s"\nkind = "heartbeat"\nstatus = "PAUSED"\n' % AUTO_ID)
        for label, durable in (("duplicate", duplicate), ("corrupt", corrupt),
                               ("unsupported", unsupported), ("missing-key", missing_key)):
            self.assert_rejected(fingerprint, receipt, durable, label)

    def test_durable_leaf_and_ancestor_symlinks_rejected(self):
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("tool-symlink.json", self.tool_document())
        real = self.write_durable_file("real-durable.toml", self.full_durable_text())
        leaf = self.root / "durable-leaf-link.toml"
        leaf.symlink_to(real)
        self.assert_rejected(fingerprint, receipt, leaf, "leaf-symlink")
        ancestor_dir = self.root / "durable-ancestor-real"
        ancestor_dir.mkdir()
        (ancestor_dir / "inner.toml").write_text(self.full_durable_text())
        ancestor = self.root / "durable-ancestor-link"
        ancestor.symlink_to(ancestor_dir, target_is_directory=True)
        self.assert_rejected(fingerprint, receipt, ancestor / "inner.toml", "ancestor-symlink")

    def test_error_and_ambiguous_tool_receipts_rejected(self):
        fingerprint = self.prepare_fingerprint()
        durable = self.write_durable_file("full-durable-tool.toml", self.full_durable_text())
        error_tool = self.write_receipt_file("tool-error.json",
            self.tool_document(is_error=True))
        ambiguous = self.write_receipt_file("tool-ambiguous.json",
            self.tool_document(extra_blocks=[{"type": "text", "text": json.dumps(
                {"automationId": AUTO_ID, "mode": "update", "status": "PAUSED"})}]))
        self.assert_rejected(fingerprint, error_tool, durable, "error-tool")
        self.assert_rejected(fingerprint, ambiguous, durable, "ambiguous")

    def test_wrong_id_status_mode_and_missing_field_rejected(self):
        fingerprint = self.prepare_fingerprint()
        durable = self.write_durable_file("full-durable-tool2.toml", self.full_durable_text())
        cases = [
            ("wrong-id", {"automationId": "auto-other", "mode": "update", "status": "PAUSED"}),
            ("active", {"automationId": AUTO_ID, "mode": "update", "status": "ACTIVE"}),
            ("wrong-mode", {"automationId": AUTO_ID, "mode": "delete", "status": "PAUSED"}),
            ("missing-field", {"automationId": AUTO_ID, "status": "PAUSED"}),
        ]
        for label, payload in cases:
            receipt = self.write_receipt_file("tool-%s.json" % label,
                                              self.tool_document(payload=payload))
            self.assert_rejected(fingerprint, receipt, durable, label)

    def test_cross_fingerprint_rejected_and_state_unchanged(self):
        fingerprint = self.prepare_fingerprint()
        receipt = self.write_receipt_file("tool-cross.json", self.tool_document())
        durable = self.write_durable_file("full-durable-cross.toml", self.full_durable_text())
        cases = [
            ("unobserved", "NOPE:task/attempt"),
            ("wrong-task", fingerprint.replace(self.task_id, "task-other")),
            ("wrong-attempt", fingerprint.replace(ATTEMPT_ID, "att-other")),
        ]
        for label, candidate in cases:
            self.assert_rejected(candidate, receipt, durable, label)


class HeartbeatTomlStringBoundaryTests(unittest.TestCase):
    """R10 acceptance[3]：TOML basic string 非法 Unicode scalar / 控制字符逐点拒绝。

    纯函数级 synthetic 断言；worker 不执行测试，由 cox 离线 unittest 验收。
    """

    def parse(self, raw_value):
        return hb._parse_toml_basic_string(raw_value, 1)

    def assertParseRejected(self, raw_value):
        with self.assertRaises(ValueError, msg=repr(raw_value)):
            self.parse(raw_value)

    def assertParseAccepted(self, raw_value, expected):
        self.assertEqual(self.parse(raw_value), expected)

    def test_surrogate_escape_lower_and_upper_bounds_rejected(self):
        # D800 = 代理下边界，DBFF = 高代理上边界，DC00 = 低代理下边界，DFFF = 上边界。
        for digits in ("D800", "DBFF", "DC00", "DFFF"):
            self.assertParseRejected('"\\u%s"' % digits)
            self.assertParseRejected('"\\U0000%s"' % digits)

    def test_non_surrogate_unicode_escapes_accepted(self):
        self.assertParseAccepted('"\\uD7FF"', "\ud7ff")
        self.assertParseAccepted('"\\uE000"', "\ue000")
        self.assertParseAccepted('"\\u4e2d"', "中")
        self.assertParseAccepted('"\\U0010FFFF"', "\U0010FFFF")

    def test_out_of_range_unicode_rejected(self):
        self.assertParseRejected('"\\U00110000"')
        self.assertParseRejected('"\\UFFFFFFFF"')

    def test_direct_surrogate_characters_rejected(self):
        self.assertParseRejected('"' + "\ud800" + '"')
        self.assertParseRejected('"' + "\udfff" + '"')

    def test_unescaped_control_characters_rejected(self):
        # U+0000–U+0008 + U+000A–U+001F + U+007F；literal TAB U+0009 不在此集合。
        controls = list(range(0x00, 0x09)) + list(range(0x0A, 0x20)) + [0x7F]
        for code in controls:
            self.assertParseRejected('"' + chr(code) + '"')

    def test_literal_tab_and_standard_escapes_accepted(self):
        self.assertParseAccepted('"' + "\t" + '"', "\t")
        self.assertParseAccepted('"\\b\\t\\n\\f\\r\\"\\\\"', "\b\t\n\f\r\"\\")
        self.assertParseAccepted('"a\\tb"', "a\tb")


class HeartbeatOfficialToolPayloadBoundaryTests(unittest.TestCase):
    """R10 acceptance[4]：官方工具回执 payload 歧义 / 重复键逐点拒绝（纯函数级）。"""

    def document(self, *texts, is_error=False):
        return {"content": [{"type": "text", "text": t} for t in texts], "isError": is_error}

    def test_single_complete_object_accepted(self):
        payload = {"automationId": "auto-x", "mode": "update", "status": "PAUSED"}
        result = hb._extract_official_tool_payload(
            self.document("Updated automation in the app.", json.dumps(payload)))
        self.assertEqual(result, payload)

    def test_second_object_without_automationId_is_ambiguous(self):
        payload = {"automationId": "auto-x", "mode": "update", "status": "PAUSED"}
        for extra in ({"status": "ACTIVE"}, {"mode": "update"}, {"foo": "bar"}):
            doc = self.document(json.dumps(payload), json.dumps(extra))
            with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_PAYLOAD_AMBIGUOUS"):
                hb._extract_official_tool_payload(doc)

    def test_non_json_text_is_ignored(self):
        payload = {"automationId": "auto-x", "mode": "update", "status": "PAUSED"}
        doc = self.document("Updated automation in the app.", "second note",
                            json.dumps(payload))
        self.assertEqual(hb._extract_official_tool_payload(doc)["status"], "PAUSED")

    def test_non_object_json_blocks_are_not_payloads(self):
        doc = self.document("[1, 2]", "123", "null", '"note"')
        with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_PAYLOAD_MISSING"):
            hb._extract_official_tool_payload(doc)

    def test_duplicate_json_keys_rejected(self):
        text = ('{"automationId": "auto-x", "automationId": "auto-y", '
                '"mode": "update", "status": "PAUSED"}')
        with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_JSON_DUPLICATE_KEY"):
            hb._extract_official_tool_payload(self.document(text))

    def test_duplicate_status_or_mode_keys_rejected(self):
        for key in ("status", "mode"):
            text = ('{"automationId": "auto-x", "mode": "update", "status": "PAUSED", '
                    '"%s": "ACTIVE"}' % key)
            with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_JSON_DUPLICATE_KEY"):
                hb._extract_official_tool_payload(self.document(text))

    def test_missing_payload_and_error_rejected(self):
        with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_PAYLOAD_MISSING"):
            hb._extract_official_tool_payload(self.document("just prose"))
        with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_TOOL_ERROR"):
            hb._extract_official_tool_payload(self.document(
                json.dumps({"automationId": "x", "mode": "update", "status": "PAUSED"}),
                is_error=True))

    def test_incomplete_single_object_rejected(self):
        for payload in ({"automationId": "x", "status": "PAUSED"},
                        {"automationId": "x", "mode": "update"},
                        {"mode": "update", "status": "PAUSED"}):
            with self.assertRaisesRegex(ValueError, "OFFICIAL_RECEIPT_PAYLOAD_INCOMPLETE"):
                hb._extract_official_tool_payload(self.document(json.dumps(payload)))


class HeartbeatOfficialToolAmbiguityTests(HeartbeatFixtureBase):
    """R10 acceptance[4][5]：第二 JSON 对象 / 重复键 / 非法 Unicode durable 全链 fail-closed。

    全部 synthetic：直接写「原始官方工具回执形状」JSON 与 automation.toml 形状 durable；
    不调用官方工具、不伪造真实暂停；worker 本轮不执行 Bash，由 cox 独立 unittest 验收。
    """

    def drive_to_pause_fingerprint(self):
        self.write_host_runtime(state="unknown")
        fingerprint = self.tick("t1")["data"]["errorFingerprint"]
        for name in ("t2", "t3"):
            self.tick(name)
        evidence = self.write_reconciliation_evidence(fingerprint)
        hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                      str(self.watch), fingerprint, str(evidence))
        self.tick("t4")
        return fingerprint

    def good_durable(self, name="r10-good-durable.toml"):
        path = self.root / name
        path.write_text('id = "%s"\nkind = "heartbeat"\n'
                        'target_thread_id = "%s"\nstatus = "PAUSED"\n' % (AUTO_ID, THREAD_ID))
        return path

    def receipt_with_texts(self, name, texts, is_error=False):
        path = self.root / name
        path.write_text(json.dumps({"content": [{"type": "text", "text": t} for t in texts],
                                    "isError": is_error}))
        return path

    def assert_pause_rejected(self, fingerprint, receipt, durable, label):
        with self.assertRaises(ValueError, msg=label):
            hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                 fingerprint, str(receipt), str(durable))
        self.assertNotIn(fingerprint, self.watch_state().get("pauseConfirmed", {}), label)

    def test_second_status_only_object_rejected_then_single_object_succeeds(self):
        fingerprint = self.drive_to_pause_fingerprint()
        durable = self.good_durable()
        payload = json.dumps({"automationId": AUTO_ID, "mode": "update", "status": "PAUSED"})
        ambiguous = self.receipt_with_texts(
            "r10-ambiguous.json",
            ["Updated automation in the app.", payload, json.dumps({"status": "ACTIVE"})])
        self.assert_pause_rejected(fingerprint, ambiguous, durable, "second-object")
        single = self.receipt_with_texts("r10-single.json",
                                         ["Updated automation in the app.", payload])
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(single), str(durable))
        self.assertTrue(result["ok"])

    def test_duplicate_json_keys_rejected_in_full_chain(self):
        fingerprint = self.drive_to_pause_fingerprint()
        durable = self.good_durable()
        duplicate_text = ('{"automationId": "%s", "status": "PAUSED", "status": "ACTIVE", '
                          '"mode": "update"}' % AUTO_ID)
        receipt = self.receipt_with_texts("r10-dup.json", [duplicate_text])
        self.assert_pause_rejected(fingerprint, receipt, durable, "duplicate-key")

    def test_durable_with_surrogate_escape_rejected(self):
        fingerprint = self.drive_to_pause_fingerprint()
        payload = json.dumps({"automationId": AUTO_ID, "mode": "update", "status": "PAUSED"})
        receipt = self.receipt_with_texts("r10-durable-receipt.json",
                                          ["Updated automation in the app.", payload])
        path = self.root / "r10-surrogate-durable.toml"
        path.write_text('id = "%s"\nkind = "heartbeat"\nprompt = "\\uD800"\n'
                        'target_thread_id = "%s"\nstatus = "PAUSED"\n' % (AUTO_ID, THREAD_ID))
        self.assert_pause_rejected(fingerprint, receipt, path, "surrogate-durable")

    def test_durable_with_control_character_rejected(self):
        fingerprint = self.drive_to_pause_fingerprint()
        payload = json.dumps({"automationId": AUTO_ID, "mode": "update", "status": "PAUSED"})
        receipt = self.receipt_with_texts("r10-durable-receipt2.json",
                                          ["Updated automation in the app.", payload])
        path = self.root / "r10-control-durable.toml"
        path.write_text('id = "%s"\nkind = "heartbeat"\nprompt = "a' % AUTO_ID
                        + chr(0x01) + 'b"\n'
                        'target_thread_id = "%s"\nstatus = "PAUSED"\n' % THREAD_ID)
        self.assert_pause_rejected(fingerprint, receipt, path, "control-durable")


class HeartbeatStructuralPriorityTests(HeartbeatFixtureBase):
    """R11 acceptance[3][4]：结构身份/DB/runner 证据异常优先于 delivery 审核。

    collector 已识别的 5 类结构异常（WORKFLOW_BINDING_INVALID / DB_MISMATCH /
    BINDING_NOT_ALLOWED / RUNNER_PATH_CONFLICT / RUNNER_EVIDENCE_UNREADABLE）即使已有
    new_unreviewed 或 approved delivery，也必须先 RECONCILE，不得输出 REVIEW/NEXT；
    合法 delivery（无异常）的 REVIEW / approved NEXT 语义保持不变。全部 synthetic：
    纯 evaluate_tick 快照 + 隔离 fixture，不启动真实 runner / 宿主 / 官方暂停。
    worker 本轮不执行 Bash，由 cox 独立 unittest 验收。
    """

    STRUCTURAL_CODES = ("WORKFLOW_BINDING_INVALID", "DB_MISMATCH", "BINDING_NOT_ALLOWED",
                        "RUNNER_PATH_CONFLICT", "RUNNER_EVIDENCE_UNREADABLE")

    def synthetic_snapshot(self, code, delivery_state, decision=None):
        """构造纯 evaluate_tick 用的最小合法快照（仅结构化字段，无文件/DB 依赖）。"""
        has_delivery = delivery_state != "none"
        return {
            "schemaVersion": "d4-heartbeat-snapshot.v1",
            "automationId": AUTO_ID,
            "ownerThreadId": THREAD_ID,
            "taskId": self.task_id,
            "attemptId": ATTEMPT_ID,
            "deliveryState": delivery_state,
            "deliveryId": "dlv-1" if has_delivery else None,
            "reviewId": "rev-1" if decision else None,
            "reviewDecision": decision,
            "errorCode": code,
            "anomaly": ({"code": code,
                         "fingerprint": "%s:%s/%s" % (code, self.task_id, ATTEMPT_ID),
                         "detail": {}} if code else None),
            "reconciliationRequired": bool(code),
            "progressSignature": "sig-%s-%s" % (code, delivery_state),
            "deadlineAt": None,
        }

    def assert_reconcile_priority(self, delivery_state, decision=None):
        for code in self.STRUCTURAL_CODES:
            snapshot = self.synthetic_snapshot(code, delivery_state, decision)
            directive, _ = hb.evaluate_tick(snapshot, None,
                                            "pure-%s-%s" % (code, delivery_state), BASE_NOW)
            self.assertEqual(directive["action"], "RECONCILE", code)
            self.assertEqual(directive["data"]["errorCode"], code, code)

    def test_new_unreviewed_delivery_cannot_hide_structural_anomalies(self):
        self.assert_reconcile_priority("new_unreviewed")

    def test_approved_delivery_cannot_hide_structural_anomalies(self):
        self.assert_reconcile_priority("approved", decision="approved")

    def test_legit_delivery_review_and_approved_next_unchanged(self):
        review_snapshot = self.synthetic_snapshot(None, "new_unreviewed")
        directive, _ = hb.evaluate_tick(review_snapshot, None, "legit-review", BASE_NOW)
        self.assertEqual(directive["action"], "REVIEW")
        next_snapshot = self.synthetic_snapshot(None, "approved", decision="approved")
        directive, _ = hb.evaluate_tick(next_snapshot, None, "legit-next", BASE_NOW)
        self.assertEqual(directive["action"], "NEXT")

    # -- 端到端：collector 真报异常 + DB 中有 delivery --
    def test_db_mismatch_with_new_delivery_reconciles_end_to_end(self):
        self.insert_delivery()
        other = self.root / "other-priority.sqlite"
        other.write_text("")
        document = self.write_workflow()
        document["db"] = str(other)
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("db-mismatch-open")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "DB_MISMATCH")

    def test_db_mismatch_with_approved_delivery_reconciles_end_to_end(self):
        self.insert_delivery()
        self.insert_review()
        other = self.root / "other-priority-approved.sqlite"
        other.write_text("")
        document = self.write_workflow()
        document["db"] = str(other)
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("db-mismatch-approved")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "DB_MISMATCH")

    def test_binding_not_allowed_agents_with_new_delivery_reconciles_end_to_end(self):
        self.insert_delivery()
        document = self.write_workflow()
        document["allowedAgents"] = ["someone-else"]
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("agent-not-allowed-open")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "BINDING_NOT_ALLOWED")

    def test_binding_not_allowed_source_with_approved_delivery_reconciles_end_to_end(self):
        self.insert_delivery()
        self.insert_review()
        document = self.write_workflow()
        document["allowedSource"] = "other-source"
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("source-not-allowed-approved")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "BINDING_NOT_ALLOWED")

    def test_workflow_binding_invalid_type_with_new_delivery_reconciles_end_to_end(self):
        self.insert_delivery()
        document = self.write_workflow()
        document["allowedAgents"] = "dev-wkb1"  # 错类型（字符串而非列表）
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("binding-invalid-type-open")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "WORKFLOW_BINDING_INVALID")

    def test_workflow_binding_invalid_missing_with_approved_delivery_reconciles(self):
        self.insert_delivery()
        self.insert_review()
        document = self.write_workflow()
        document.pop("db")
        self.workflow.write_text(json.dumps(document))
        directive = self.tick("binding-invalid-missing-approved")
        self.assertEqual(directive["action"], "RECONCILE")
        self.assertEqual(directive["data"]["errorCode"], "WORKFLOW_BINDING_INVALID")

    # -- 保留：新结构码仍走既有对账确认 + 独立 tick 阈值 → PAUSE → confirmed 等待 --
    def test_structural_code_with_delivery_reconcile_confirm_pause_then_wait(self):
        self.insert_delivery()  # 已有未审 delivery，仍必须先 RECONCILE 再对账/暂停。
        document = self.write_workflow()
        document["allowedAgents"] = ["someone-else"]
        self.workflow.write_text(json.dumps(document))
        counts = []
        fingerprint = None
        for tick_id in ("chain-t1", "chain-t2", "chain-t3"):
            directive = self.tick(tick_id)
            self.assertEqual(directive["action"], "RECONCILE", tick_id)
            fingerprint = directive["data"]["errorFingerprint"]
            counts.append(directive["data"]["errorCount"])
        self.assertEqual(counts, [1, 2, 3])
        self.assertIn("BINDING_NOT_ALLOWED", fingerprint)
        evidence = self.write_reconciliation_evidence(fingerprint)
        hb.acknowledge_reconciliation(str(self.workflow), AUTO_ID, THREAD_ID,
                                      str(self.watch), fingerprint, str(evidence))
        pause_directive = self.tick("chain-t4")
        self.assertEqual(pause_directive["action"], "PAUSE")
        self.assertEqual(pause_directive["platformAction"]["kind"], "pause_automation")
        receipt = self.root / "chain-receipt.json"
        receipt.write_text(json.dumps({"automationId": AUTO_ID,
                                       "ownerThreadId": THREAD_ID, "status": "PAUSED"}))
        durable = self.root / "chain-durable.toml"
        durable.write_text('id = "%s"\nkind = "heartbeat"\n'
                           'target_thread_id = "%s"\nstatus = "PAUSED"\n'
                           % (AUTO_ID, THREAD_ID))
        result = hb.acknowledge_pause(str(self.workflow), AUTO_ID, THREAD_ID, str(self.watch),
                                      fingerprint, str(receipt), str(durable))
        self.assertTrue(result["ok"])
        after = self.tick("chain-t5")
        self.assertEqual(after["action"], "WAIT")
        self.assertIsNone(after["platformAction"])


if __name__ == "__main__":
    unittest.main()
