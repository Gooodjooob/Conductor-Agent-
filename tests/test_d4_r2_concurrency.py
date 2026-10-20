"""D4-VAL-001 R2 并发回归：真实双 SQLite 连接 + Barrier 交错。

覆盖：eventId 同内容幂等 / 不同内容冲突、并发 create_attempt 状态、review 互斥、
重复 submit 不增加 adapter 调用、外部异常保持 unknown。
每个线程各自 `SqliteStore(db)` 建连接（sqlite3 默认 check_same_thread=True）。
"""
import tempfile
import threading
import unittest
from pathlib import Path

from adapters.local_poc import LocalPocAdapter
from contracts.schemas import ContractError
from service.events import EventService
from service.reviews import ReviewService
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    ctx_owner,
    ctx_reviewer,
    ctx_source,
    ctx_worker,
    runtime_root,
)
from test_d4_events import DELIVERY_BODY, ev


class SharedCountingAdapter(LocalPocAdapter):
    def __init__(self, source_id="qclaw-local"):
        super().__init__(source_id=source_id)
        self.calls = 0
        self._lock = threading.Lock()

    def submit(self, task, attempt):
        with self._lock:
            self.calls += 1
        return super().submit(task, attempt)


class ExplodingAdapter(LocalPocAdapter):
    """外部宿主异常：结果未知，不自动重试。"""

    def __init__(self, source_id="qclaw-local"):
        super().__init__(source_id=source_id)
        self.calls = 0

    def submit(self, task, attempt):
        self.calls += 1
        raise RuntimeError("synthetic host explosion")


class PausedAdapter(LocalPocAdapter):
    def __init__(self):
        super().__init__(source_id="qclaw-local")
        self.entered = threading.Event()
        self.release = threading.Event()

    def submit(self, task, attempt):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError("synthetic adapter timeout")
        return super().submit(task, attempt)


class R2ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.db = str(Path(self.tmp.name) / "d4-r2-concurrency.sqlite3")
        self.store = SqliteStore(self.db)
        self.store.register_source("qclaw-local", "本地验证来源", at="t0")
        self.store.register_agent("dev-1", "QClaw 执行成员", "registry", at="t0")
        self.adapter = SharedCountingAdapter()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # -- 辅助 -------------------------------------------------------------
    def _run_threads(self, fn, names=("a", "b"), timeout=30):
        barrier = threading.Barrier(len(names))
        results = {}

        def runner(name):
            store = SqliteStore(self.db)
            try:
                barrier.wait()
                try:
                    results[name] = ("ok", fn(name, store))
                except ContractError as exc:
                    results[name] = ("error", exc.code)
                except Exception as exc:  # noqa: BLE001
                    results[name] = ("crash", type(exc).__name__)
            finally:
                store.close()

        threads = [threading.Thread(target=runner, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout)
        return results

    def _submitted_task(self):
        service = TaskService(self.store, adapter=self.adapter, allowed_roots=allowed_roots())
        task = service.create_task(create_task_payload(), context=ctx_owner())
        att = service.create_attempt(task["taskId"], context=ctx_worker())
        service.submit_attempt(task["taskId"], att["attemptId"], context=ctx_worker())
        return task, att

    def _delivered_task(self):
        task, att = self._submitted_task()
        events = EventService(self.store)
        events.ingest(ev("accept", 1, "task.accepted", task, att, {"hostReceipt": "ack-1"}),
                      context=ctx_source())
        events.ingest(ev("delivery", 2, "task.delivered", task, att, dict(DELIVERY_BODY)),
                      context=ctx_source())
        delivery = self.store.get_task(task["taskId"])["current_delivery_id"]
        return task, att, delivery

    def _event_rows(self, event_id):
        return self.store.conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()[0]

    # -- eventId 幂等 -----------------------------------------------------
    def test_concurrent_same_event_id_same_content_is_idempotent(self):
        task, att = self._submitted_task()
        payload = ev("dup", 2, "task.progress", task, att, {"step": "same"})
        results = self._run_threads(
            lambda name, store: EventService(store).ingest(payload, context=ctx_source()))
        self.assertEqual([v[0] for v in results.values()].count("crash"), 0)
        self.assertTrue(all(k == "ok" for k, _ in results.values()))
        replayed = sorted(v["replayed"] for _, v in results.values())
        self.assertEqual(replayed, [False, True], "同内容重发必须返回首次结果")
        self.assertEqual(self._event_rows("dup"), 1)

    def test_concurrent_same_event_id_different_content_conflicts(self):
        task, att = self._submitted_task()
        payloads = {
            "a": ev("dup", 2, "task.progress", task, att, {"step": "a"}),
            "b": ev("dup", 2, "task.progress", task, att, {"step": "b"}),
        }
        results = self._run_threads(
            lambda name, store: EventService(store).ingest(payloads[name], context=ctx_source()))
        self.assertEqual([v[0] for v in results.values()].count("crash"), 0)
        kinds = sorted(results[n][0] for n in results)
        self.assertEqual(kinds, ["error", "ok"])
        code = [v for k, v in results.values() if k == "error"][0]
        self.assertEqual(code, "event_conflict")
        self.assertEqual(self._event_rows("dup"), 1)
        self.assertEqual(len(self.store.list_conflicts(task["taskId"])), 1)

    # -- 并发 create_attempt ---------------------------------------------
    def test_concurrent_create_attempt_is_consistent(self):
        service = TaskService(self.store, adapter=self.adapter, allowed_roots=allowed_roots())
        task = service.create_task(create_task_payload(limits={"maxAttempts": 2}), context=ctx_owner())
        tid = task["taskId"]
        results = self._run_threads(
            lambda name, store: TaskService(store, adapter=self.adapter,
                                            allowed_roots=allowed_roots()).create_attempt(
                tid, context=ctx_worker()))
        self.assertEqual([v[0] for v in results.values()].count("crash"), 0)
        kinds = sorted(results[n][0] for n in results)
        # 首个任务处于 ready，只能开一轮；并发的第二个必须在事务内复读后 fail closed
        self.assertEqual(kinds, ["error", "ok"])
        code = [v for k, v in results.values() if k == "error"][0]
        self.assertEqual(code, "invalid_transition")
        self.assertEqual(self.store.count_attempts(tid), 1)
        winner = [v for k, v in results.values() if k == "ok"][0]
        self.assertEqual(self.store.get_task(tid)["current_attempt_id"], winner["attemptId"])

    # -- review 互斥 ------------------------------------------------------
    def test_concurrent_review_is_mutually_exclusive(self):
        task, att, delivery = self._delivered_task()
        results = self._run_threads(lambda name, store: ReviewService(
            store, TaskService(store, allowed_roots=allowed_roots())).submit_review(
            task["taskId"], {"decision": "approved", "deliveryId": delivery},
            context=ctx_reviewer()))
        self.assertEqual([v[0] for v in results.values()].count("crash"), 0)
        kinds = sorted(results[n][0] for n in results)
        self.assertEqual(kinds, ["error", "ok"], "只允许一个审核决定生效")
        code = [v for k, v in results.values() if k == "error"][0]
        self.assertIn(code, ("invalid_transition", "review_version_mismatch"))
        self.assertEqual(len(self.store.list_reviews(task["taskId"])), 1)
        self.assertEqual(self.store.get_task(task["taskId"])["status"], "approved")

    # -- 重复 submit / 外部异常 ------------------------------------------
    def test_repeated_submit_does_not_add_adapter_calls(self):
        task, att = self._submitted_task()
        again = TaskService(self.store, adapter=self.adapter,
                            allowed_roots=allowed_roots()).submit_attempt(
            task["taskId"], att["attemptId"], context=ctx_worker())
        self.assertTrue(again["replayed"])
        self.assertEqual(self.adapter.calls, 1, "重复 submit 不得增加 adapter 调用")

    def test_host_exception_keeps_unknown_and_does_not_retry(self):
        adapter = ExplodingAdapter()
        service = TaskService(self.store, adapter=adapter, allowed_roots=allowed_roots())
        task = service.create_task(create_task_payload(), context=ctx_owner())
        att = service.create_attempt(task["taskId"], context=ctx_worker())
        with self.assertRaises(ContractError) as c1:
            service.submit_attempt(task["taskId"], att["attemptId"], context=ctx_worker())
        self.assertEqual(c1.exception.code, "submit_unknown")
        self.assertEqual(self.store.get_attempt(att["attemptId"])["submit_state"], "unknown")
        with self.assertRaises(ContractError) as c2:
            service.submit_attempt(task["taskId"], att["attemptId"], context=ctx_worker())
        self.assertEqual(c2.exception.code, "submit_unknown")
        self.assertEqual(adapter.calls, 1, "结果未知不得自动重试")

    def _submit_while_state_changes(self, change, expected_acceptance=None):
        adapter = PausedAdapter()
        service = TaskService(self.store, adapter=adapter, allowed_roots=allowed_roots())
        task = service.create_task(create_task_payload(limits={"maxAttempts": 2}), context=ctx_owner())
        att = service.create_attempt(task["taskId"], context=ctx_worker())
        result = {}

        def submit():
            store = SqliteStore(self.db)
            try:
                TaskService(store, adapter=adapter, allowed_roots=allowed_roots()).submit_attempt(
                    task["taskId"], att["attemptId"], context=ctx_worker())
                result["outcome"] = "submitted"
            except ContractError as exc:
                result["outcome"] = exc.code
            finally:
                store.close()

        thread = threading.Thread(target=submit)
        thread.start()
        try:
            self.assertTrue(adapter.entered.wait(5), "提交未进入外部 adapter")
            change(task, att)
        finally:
            adapter.release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("outcome"), "submit_unknown")
        old = self.store.get_attempt(att["attemptId"])
        self.assertEqual(old["submit_state"], "unknown")
        self.assertIsNone(old["submitted_at"])
        self.assertEqual(old["accepted_by_host_at"], expected_acceptance)
        self.assertEqual(len([a for a in self.store.list_activity(task["taskId"])
                              if a["kind"] == "attempt.submit_stale"]), 1)
        with self.assertRaises(ContractError) as replay:
            service.submit_attempt(task["taskId"], att["attemptId"], context=ctx_worker())
        self.assertIn(replay.exception.code, ("invalid_transition", "submit_unknown"))
        return task, att

    def test_submit_return_after_new_attempt_does_not_submit_old_attempt(self):
        def change(task, att):
            EventService(self.store).ingest(
                ev("block-during-submit", 1, "task.blocked", task, att,
                   {"reason": "synthetic", "owner": "cox", "resumeWhen": "new attempt"}),
                context=ctx_source(),
            )
            new = TaskService(self.store, allowed_roots=allowed_roots()).create_attempt(
                task["taskId"], context=ctx_reviewer())
            self.assertNotEqual(new["attemptId"], att["attemptId"])

        task, att = self._submit_while_state_changes(change)
        self.assertNotEqual(self.store.get_task(task["taskId"])["current_attempt_id"],
                            att["attemptId"])

    def test_submit_return_after_terminal_does_not_restore_submission(self):
        def change(task, att):
            with self.store.transaction():
                self.store.update_attempt(att["attemptId"], terminal_at="synthetic-end",
                                          terminal_kind="delivered", state="delivered")
                self.store.update_task(task["taskId"], status="approved")

        task, att = self._submit_while_state_changes(change)
        self.assertEqual(self.store.get_task(task["taskId"])["status"], "approved")
        self.assertEqual(self.store.get_attempt(att["attemptId"])["terminal_kind"], "delivered")

    def test_submit_return_does_not_overwrite_existing_acceptance(self):
        def change(task, att):
            with self.store.transaction():
                self.store.update_attempt(att["attemptId"],
                                          accepted_by_host_at="synthetic-prior-acceptance")

        task, att = self._submit_while_state_changes(
            change, expected_acceptance="synthetic-prior-acceptance")
        self.assertEqual(self.store.get_attempt(att["attemptId"])["accepted_by_host_at"],
                         "synthetic-prior-acceptance")


if __name__ == "__main__":
    unittest.main()
