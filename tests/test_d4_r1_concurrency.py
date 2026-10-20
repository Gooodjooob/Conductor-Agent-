"""D4-VAL-001 R1 并发测试：跨连接的真实 CAS（防重复提交 / 轮次上限）。

每个线程各自持有独立 SqliteStore 连接指向同一数据库文件，模拟两个入口同时提交，
验证原子占位 + busy_timeout 下“外部宿主只被调用一次”。连接在线程内创建，
以符合 sqlite3 的线程约束。
"""
import tempfile
import threading
import unittest
from pathlib import Path

from adapters.local_poc import LocalPocAdapter
from contracts.schemas import ContractError
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    ctx_owner,
    ctx_worker,
    runtime_root,
)


class SharedCountingAdapter(LocalPocAdapter):
    def __init__(self, source_id="qclaw-local"):
        super().__init__(source_id=source_id)
        self.calls = 0
        self._lock = threading.Lock()

    def submit(self, task, attempt):
        with self._lock:
            self.calls += 1
        return super().submit(task, attempt)


class ConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.db = str(Path(self.tmp.name) / "d4-concurrency.sqlite3")
        self.store = SqliteStore(self.db)
        self.store.register_source("qclaw-local", "本地验证来源", at="t0")
        self.store.register_agent("dev-1", "QClaw 执行成员", "registry", at="t0")
        self.adapter = SharedCountingAdapter()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _run_in_threads(self, fn, names=("a", "b"), timeout=30):
        barrier = threading.Barrier(len(names))
        results = {}

        def runner(name):
            store = SqliteStore(self.db)
            try:
                barrier.wait()
                try:
                    results[name] = ("ok", fn(store))
                except ContractError as exc:
                    results[name] = ("error", exc.code)
                except Exception as exc:  # noqa: BLE001 - 非契约异常要显式暴露
                    results[name] = ("crash", type(exc).__name__)
            finally:
                store.close()

        threads = [threading.Thread(target=runner, args=(n,)) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=timeout)
        return results

    def test_concurrent_submit_calls_host_once(self):
        task = self.store_task()
        tid, aid = task["taskId"], task["attemptId"]

        def submit(store):
            return TaskService(store, adapter=self.adapter, allowed_roots=allowed_roots()).submit_attempt(
                tid, aid, context=ctx_worker()
            )

        outcomes = self._run_in_threads(submit)
        self.assertEqual([o[0] for o in outcomes.values()].count("crash"), 0)
        self.assertEqual(self.adapter.calls, 1, "外部宿主只允许被调用一次")
        for kind, value in outcomes.values():
            if kind == "error":
                self.assertIn(value, ("submit_in_progress", "submit_unknown"))
        ok = [v for k, v in outcomes.values() if k == "ok"]
        self.assertTrue(ok)
        self.assertEqual(len([v for v in ok if not v["replayed"]]), 1)
        row = self.store.get_attempt(aid)
        self.assertIsNotNone(row["submitted_at"])
        self.assertEqual(row["submit_state"], "submitted")

    def test_concurrent_attempt_creation_respects_limit(self):
        created = TaskService(self.store, adapter=self.adapter, allowed_roots=allowed_roots()).create_task(
            create_task_payload(limits={"maxAttempts": 1}), context=ctx_owner()
        )
        tid = created["taskId"]

        def make(store):
            return TaskService(store, adapter=self.adapter, allowed_roots=allowed_roots()).create_attempt(
                tid, context=ctx_worker()
            )

        results = self._run_in_threads(make)
        self.assertEqual([o[0] for o in results.values()].count("crash"), 0)
        ok = [v for k, v in results.values() if k == "ok"]
        errs = [v for k, v in results.values() if k == "error"]
        self.assertEqual(len(ok), 1, "maxAttempts=1 时只允许创建一轮")
        self.assertEqual(self.store.count_attempts(tid), 1)
        for code in errs:
            self.assertEqual(code, "invalid_transition")

    # -- 辅助 -------------------------------------------------------------
    def store_task(self):
        service = TaskService(self.store, adapter=self.adapter, allowed_roots=allowed_roots())
        task = service.create_task(create_task_payload(), context=ctx_owner())
        attempt = service.create_attempt(task["taskId"], context=ctx_worker())
        return {"taskId": task["taskId"], "attemptId": attempt["attemptId"]}


if __name__ == "__main__":
    unittest.main()
