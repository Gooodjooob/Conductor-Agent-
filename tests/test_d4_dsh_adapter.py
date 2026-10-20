"""D4-DSH-ADAPTER-001 定向测试：DSH 适配器的离线边界与 fail-closed 语义。

覆盖卡片 §8「离线测试至少覆盖」的全部条目：

- JSON-RPC 初始化/正常提交/响应解析/字段缺失 —— 本版本**无 JSON-RPC**，等价覆盖为
  版本探测、argv 数组提交、回执判定与回执字段缺失；见 `DshTransportTests` /
  `DshAdapterSubmitTests`。
- `submitted` 与 `accepted_by_host` 分离。
- 重复 event、eventId 冲突、旧 sequence、迟到交付和终态保护。
- 未知提交、断线、超时、非零退出和无效输出不自动重试。
- 路径越界、符号链接逃逸、摘要不匹配和 workspace 外写入。
- 返工新 attempt、新 delivery、旧交付审核和 maxAttempts。
- 两个独立连接下的重复提交并发保护。
- DSH 身份与 D4 登记成员不一致时 fail closed。

**全部离线**：真实 DSH 一次都没跑过（需要联网/凭据/真实任务，卡片禁止）。
真实 `DshHeadlessTransport` 的代码路径由本机 python 桩脚本（fixture）驱动，
既不触网也不修改 DSH 配置。
"""
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from adapters.dsh import (
    D4_DSH_AGENT_ID,
    DSH_ATTEMPT_CONFLICT,
    DSH_HOST_IDENTITY_MISMATCH,
    DSH_NETWORK_REQUIRED_BUT_FORBIDDEN,
    DSH_OUTPUT_TOO_LARGE,
    DSH_RECEIPT_MISMATCH,
    DSH_SOURCE_ID,
    DSH_VERSION_UNSUPPORTED,
    DshAdapter,
    DshHeadlessTransport,
    DshTransportError,
    normalize_version,
)
from contracts.schemas import ContractError
from service.context import owner_context, reviewer_context, source_context, worker_context
from service.events import EventService
from service.reviews import ReviewService
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import runtime_root

STUB_SOURCE = '''\
import os, sys, time
args = sys.argv[1:]
if "--version" in args:
    sys.stdout.write(os.environ.get("DSH_STUB_VERSION", "0.1.1-rc.2") + "\\n")
    sys.exit(int(os.environ.get("DSH_STUB_VERSION_EXIT", "0")))
time.sleep(float(os.environ.get("DSH_STUB_SLEEP", "0")))
sys.stdout.write(os.environ.get("DSH_STUB_STDOUT", "stub final message\\n"))
sys.stderr.write(os.environ.get("DSH_STUB_STDERR", ""))
sys.exit(int(os.environ.get("DSH_STUB_EXIT", "0")))
'''


def _ctx_owner(actor_id="cox"):
    return owner_context(actor_id=actor_id, entry="dsh-adapter-test", synthetic=True)


def _ctx_reviewer(actor_id="cox"):
    return reviewer_context(actor_id=actor_id, entry="dsh-adapter-test", synthetic=True)


def _ctx_worker(actor_id="dev-1"):
    return worker_context(actor_id=actor_id, entry="dsh-adapter-test", synthetic=True)


def _ctx_source():
    return source_context(source_id=DSH_SOURCE_ID, entry="dsh-adapter-test", synthetic=True)


class LaunchCounter:
    """跨线程/跨连接的启动计数（并发保护用）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self.value = 0

    def bump(self):
        with self._lock:
            self.value += 1
            return self.value


class FakeDshTransport:
    """离线夹具：语义等价于合规宿主的 transport 接口。

    `requires_network=False` 使 D4 冻结的 `allowNetwork=false` 不阻断 dispatch，
    从而可以在不触网的前提下覆盖 submit/回执/未知结果分支。
    """

    def __init__(self, *, version="0.1.1-rc.2", requires_network=False, receipt_mode="none",
                 evidence_level="LOCAL", exit_code=0, timed_out=False, receipt=None,
                 launch_error=None, counter=None, stdout="final message\n", stderr=""):
        self.version = version
        self.requires_network = requires_network
        self.receipt_mode = receipt_mode
        self.evidence_level = evidence_level
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.receipt = receipt
        self.launch_error = launch_error
        self.counter = counter
        self.stdout = stdout
        self.stderr = stderr
        self.calls = []

    def probe_version(self):
        if self.version is None:
            raise DshTransportError(DSH_VERSION_UNSUPPORTED, message="unsupported")
        return self.version

    def _receipt_for(self, attempt_id):
        if self.receipt is None:
            return None
        if callable(self.receipt):
            return self.receipt(attempt_id)
        return dict(self.receipt)

    def run(self, task_text, attempt_id):
        if self.counter is not None:
            self.counter.bump()
        self.calls.append({"message": task_text, "attemptId": attempt_id})
        if self.launch_error is not None:
            raise self.launch_error
        return {
            "argv": ["dsh", "--profile", "headless", task_text],
            "stdout": self.stdout, "stderr": self.stderr,
            "exitCode": self.exit_code, "timedOut": self.timed_out,
            "bytes": len(self.stdout) + len(self.stderr),
            "version": self.version,
            "receipt": self._receipt_for(attempt_id),
            "frameDigest": "0" * 64, "framePath": "", "summaryPath": "",
            "summary": {"stdoutBytes": len(self.stdout), "stdoutSha256": "0" * 64,
                        "stdoutFirstLine": " ".join(self.stdout.split())[:120],
                        "stderrBytes": len(self.stderr), "stderrSha256": "1" * 64,
                        "stderrFirstLine": " ".join(self.stderr.split())[:120],
                        "exitCode": self.exit_code, "timedOut": self.timed_out},
        }


def raw_task(workspace, **over):
    """手工构造任务行（适配器单元测试用；不经 TaskService 权限校验）。"""
    base = {
        "task_id": "task-dsh-1", "project_id": "conductor",
        "title": "DSH 适配器离线验证", "agent_id": "dev-1",
        "source_id": DSH_SOURCE_ID, "workspace": str(workspace),
        "goal": "离线验证 DSH 适配器边界。", "acceptance_json": '["回执可追溯"]',
        "permissions_json": json.dumps({
            "writeScope": ["out/"], "allowNetwork": True,
            "allowBusinessWrite": False, "allowGit": False, "allowPublish": False,
        }, ensure_ascii=False),
        "limits_json": json.dumps({"maxAttempts": 2, "maxReviewRounds": 2, "timeoutMinutes": 60}),
        "status": "active", "revision": 1,
        "current_attempt_id": "att-dsh-1", "current_delivery_id": None,
    }
    base.update(over)
    return base


def raw_attempt(task, **over):
    base = {"attempt_id": task["current_attempt_id"], "task_id": task["task_id"],
            "created_at": "2026-10-03T00:00:00+08:00", "terminal_at": None, "state": "created"}
    base.update(over)
    return base


def service_payload(workspace, **over):
    payload = {
        "projectId": "conductor", "title": "DSH 链路离线验证",
        "agentId": "dev-1", "sourceId": DSH_SOURCE_ID, "workspace": str(workspace),
        "permissions": {"writeScope": ["out/"], "allowNetwork": False,
                        "allowBusinessWrite": False, "allowGit": False, "allowPublish": False},
        "goal": "验证 DSH 提交、事件与返工边界。",
    }
    payload.update(over)
    return payload


class DshTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.out = self.workspace / "out"
        self.out.mkdir()
        self.store_dir = self.root / "dsh-store"
        self.store_dir.mkdir(mode=0o700)
        self.db = self.root / "d4.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    # -- 夹具 -------------------------------------------------------------
    def write_stub(self):
        stub = self.root / "dsh-stub.py"
        stub.write_text(STUB_SOURCE, encoding="utf-8")
        return stub

    def real_transport(self, stub, *, env=None, timeout=15.0, max_output=32 * 1024 * 1024,
                       dsh_home=None):
        base_env = {"HOME": str(self.root / "home"), "PATH": "/usr/bin:/bin"}
        base_env.update(env or {})
        (self.root / "home").mkdir(exist_ok=True)
        return DshHeadlessTransport(
            [sys.executable, str(stub)], self.workspace, self.store_dir,
            dsh_home=dsh_home, timeout=timeout, version_timeout=15.0,
            env=base_env, max_output=max_output,
        )


# =====================================================================
# 1. transport 层：版本门禁 / argv / 帧落盘 / 超时 / 输出上限
# =====================================================================
class DshTransportTests(DshTestBase):
    def test_version_probe_accepts_supported_baseline(self):
        transport = self.real_transport(self.write_stub())
        self.assertEqual(transport.probe_version(), "0.1.1-rc.2")

    def test_version_probe_rejects_other_versions_fail_closed(self):
        for bad in ("0.1.5-rc.2", "0.1.0-rc.8", "0.2.0"):
            transport = self.real_transport(
                self.write_stub(), env={"DSH_STUB_VERSION": bad})
            with self.assertRaises(DshTransportError) as ctx:
                transport.probe_version()
            self.assertEqual(ctx.exception.code, DSH_VERSION_UNSUPPORTED)
            self.assertEqual(ctx.exception.details.get("actual"), bad)

    def test_version_probe_unparsable_output_is_unsupported(self):
        stub = self.write_stub()
        transport = self.real_transport(stub, env={"DSH_STUB_VERSION": "not-a-version !!"})
        with self.assertRaises(DshTransportError) as ctx:
            transport.probe_version()
        self.assertEqual(ctx.exception.code, DSH_VERSION_UNSUPPORTED)

    def test_normalize_version_strict(self):
        self.assertEqual(normalize_version("0.1.1-rc.2\n"), "0.1.1-rc.2")
        self.assertEqual(normalize_version("v0.1.1\n"), "0.1.1")
        self.assertIsNone(normalize_version(""))
        self.assertIsNone(normalize_version("--version"))

    def test_command_must_be_absolute_argv(self):
        with self.assertRaises(ValueError):
            DshHeadlessTransport("dsh", self.workspace, self.store_dir,
                                 env={"HOME": str(self.root)})
        with self.assertRaises(ValueError):
            DshHeadlessTransport(["dsh", "--profile"], self.workspace, self.store_dir,
                                 env={"HOME": str(self.root)})

    def test_explicit_home_required(self):
        stub = self.write_stub()
        with self.assertRaises(ValueError):
            DshHeadlessTransport([sys.executable, str(stub)], self.workspace,
                                 self.store_dir, env={})

    def test_argv_is_array_with_positional_task(self):
        transport = self.real_transport(self.write_stub())
        argv = transport.argv("do the thing")
        self.assertEqual(argv, [sys.executable, str(self.write_stub()),
                                "--profile", "headless", "do the thing"])
        self.assertEqual(transport.version_argv(),
                         [sys.executable, str(self.write_stub()), "--version"])
        with self.assertRaises(ValueError):
            transport.argv("   ")

    def test_frames_persisted_private_and_sanitized(self):
        transport = self.real_transport(
            self.write_stub(), env={"DSH_STUB_STDOUT": "secret model text 42\n"})
        run = transport.run("task text", "att-x1")
        self.assertEqual(run["exitCode"], 0)
        raw = Path(run["framePath"])
        summary = json.loads(Path(run["summaryPath"]).read_text(encoding="utf-8"))
        # 原始帧落盘且私有
        self.assertTrue(raw.is_file())
        self.assertEqual(stat.S_IMODE(raw.stat().st_mode) & 0o077, 0)
        self.assertIn("secret model text 42", raw.read_text(encoding="utf-8"))
        # 摘要脱敏：只有长度/摘要/首行截断，且 receipt 恒为 None
        self.assertEqual(summary["stdoutBytes"], len("secret model text 42\n"))
        self.assertEqual(summary["stdoutFirstLine"], "secret model text 42")
        self.assertEqual(summary["receiptMode"], "none")
        self.assertIsNone(run["receipt"])
        self.assertEqual(summary["version"], "0.1.1-rc.2")

    def test_output_over_limit_fails_closed(self):
        transport = self.real_transport(
            self.write_stub(), env={"DSH_STUB_STDOUT": "x" * 5000}, max_output=512)
        with self.assertRaises(DshTransportError) as ctx:
            transport.run("task text", "att-x2")
        self.assertEqual(ctx.exception.code, DSH_OUTPUT_TOO_LARGE)

    def test_timeout_is_reported_not_swallowed(self):
        transport = self.real_transport(
            self.write_stub(), env={"DSH_STUB_SLEEP": "30"}, timeout=1.0)
        run = transport.run("task text", "att-x3")
        self.assertTrue(run["timedOut"])
        self.assertNotEqual(run["exitCode"], 0)


# =====================================================================
# 2. 适配器提交：submitted / accepted_by_host 分离与 fail closed
# =====================================================================
class DshAdapterSubmitTests(DshTestBase):
    def adapter(self, transport):
        return DshAdapter(transport, self.store_dir)

    def test_no_receipt_yields_submitted_only(self):
        transport = self.real_transport(self.write_stub())
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        result = self.adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "submitted")
        self.assertIsNone(result.host_receipt)
        self.assertEqual(result.source_id, DSH_SOURCE_ID)
        self.assertEqual(result.evidence_level, "UNVERIFIED")
        detail = json.loads(result.note)
        self.assertFalse(detail["acceptedByHost"])
        self.assertIsNone(detail["sourceRunId"])
        self.assertIsNone(detail["hostSessionId"])

    def test_version_gate_blocks_dispatch(self):
        transport = FakeDshTransport(version=None)
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.details["dshCode"], DSH_VERSION_UNSUPPORTED)
        self.assertEqual(transport.calls, [])  # 没有 dispatch

    def test_network_gate_blocks_dispatch_when_forbidden(self):
        transport = FakeDshTransport(requires_network=True)
        task = raw_task(self.workspace, permissions_json=json.dumps({
            "writeScope": ["out/"], "allowNetwork": False,
            "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}))
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertEqual(ctx.exception.details["dshCode"], DSH_NETWORK_REQUIRED_BUT_FORBIDDEN)
        self.assertEqual(transport.calls, [])

    def test_timeout_maps_to_submit_unknown_no_retry(self):
        transport = FakeDshTransport(timed_out=True, exit_code=-9)
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")
        self.assertEqual(len(transport.calls), 1)

    def test_nonzero_exit_maps_to_submit_unknown(self):
        transport = FakeDshTransport(exit_code=1, stderr="dsh: boom\n")
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")

    def test_launch_failure_maps_to_submit_unknown(self):
        transport = FakeDshTransport(
            launch_error=DshTransportError("DSH_LAUNCH_FAILED", message="ENOENT"))
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")
        self.assertEqual(ctx.exception.details["dshCode"], "DSH_LAUNCH_FAILED")

    def test_valid_receipt_yields_accepted_by_host(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted",
                                        "sessionId": "session-%s" % attempt_id,
                                        "hostAgentId": D4_DSH_AGENT_ID})
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        result = self.adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertEqual(result.host_receipt, attempt["attempt_id"])
        self.assertEqual(result.evidence_level, "LOCAL")

    def test_receipt_missing_run_id_fails_closed(self):
        transport = FakeDshTransport(receipt_mode="runid",
                                     receipt={"status": "accepted"})
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_receipt_run_id_mismatch_fails_closed(self):
        transport = FakeDshTransport(receipt_mode="runid",
                                     receipt={"runId": "another-run", "status": "accepted"})
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.details["dshCode"], DSH_RECEIPT_MISMATCH)

    def test_receipt_terminal_snapshot_is_not_acceptance(self):
        transport = FakeDshTransport(
            receipt_mode="runid",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "completed"})
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_receipt_host_identity_mismatch_fails_closed(self):
        transport = FakeDshTransport(
            receipt_mode="runid",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted",
                                        "hostAgentId": "someone-else"})
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.details["dshCode"], DSH_HOST_IDENTITY_MISMATCH)

    def test_receipt_with_low_evidence_level_fails_closed(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="UNVERIFIED",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"})
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_duplicate_submit_replays_without_relaunch(self):
        transport = FakeDshTransport()
        adapter = self.adapter(transport)
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        first = adapter.submit(task, attempt)
        second = adapter.submit(task, attempt)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(first.status, second.status)
        # 换一个新的适配器实例（模拟新进程）也从磁盘回放，不重新启动 DSH
        third = DshAdapter(transport, self.store_dir).submit(task, attempt)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(third.status, "submitted")

    def test_duplicate_submit_with_changed_content_conflicts(self):
        transport = FakeDshTransport()
        adapter = self.adapter(transport)
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        adapter.submit(task, attempt)
        changed = raw_task(self.workspace, goal="完全不同的目标")
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(changed, raw_attempt(changed))
        self.assertEqual(ctx.exception.details["dshCode"], DSH_ATTEMPT_CONFLICT)
        self.assertEqual(len(transport.calls), 1)

    def test_attempt_binding_mismatch_fails_closed(self):
        adapter = self.adapter(FakeDshTransport())
        task = raw_task(self.workspace)
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(task, raw_attempt(task, task_id="other-task"))
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_capability_report_is_honest(self):
        real = self.adapter(self.real_transport(self.write_stub()))
        report = real.capability_report()
        self.assertFalse(report["connected"])
        self.assertFalse(report["capabilities"]["host_acceptance"])
        self.assertFalse(report["capabilities"]["stable_ids"])
        self.assertEqual(report["capabilities"]["evidence_level"], "UNVERIFIED")

    def test_agent_id_is_not_overridable(self):
        transport = FakeDshTransport()
        with self.assertRaises(TypeError):
            DshAdapter(transport, self.store_dir, agent_id="attacker")

    def test_source_id_is_fixed(self):
        with self.assertRaises(ValueError):
            DshAdapter(FakeDshTransport(), self.store_dir, source_id="qclaw-gateway")

    def test_reserved_interfaces_fail_closed(self):
        adapter = self.adapter(FakeDshTransport())
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        for call in (lambda: adapter.wait_for_terminal(task, attempt, 1000),
                     lambda: adapter.list_host_tasks(),
                     lambda: adapter.fetch_history(task, attempt)):
            with self.assertRaises(ContractError) as ctx:
                call()
            self.assertTrue(ctx.exception.details["reserved"])

    def test_security_sink_receives_sanitized_events(self):
        events = []
        adapter = DshAdapter(FakeDshTransport(), self.store_dir,
                             security_sink=lambda kind, detail: events.append((kind, detail)))
        task, attempt = raw_task(self.workspace), raw_attempt(raw_task(self.workspace))
        adapter.submit(task, attempt)
        self.assertIn("dsh.submitted_without_host_receipt", [kind for kind, _ in events])


# =====================================================================
# 3. 产物与交付路径校验
# =====================================================================
class DshArtifactTests(DshTestBase):
    def adapter(self):
        return DshAdapter(FakeDshTransport(), self.store_dir)

    def _task_attempt(self, **task_over):
        task = raw_task(self.workspace, **task_over)
        # D4-DSH-02-SDK-001 P1 修复后：首次交付不再依赖 D4 terminal_at；
        # 集成路径用 quiesced=True（宿主停稳）驱动。这里保留 terminal_at
        # 仅用于旧轮次只读拒绝语义的负例。
        attempt = raw_attempt(task, terminal_at="2026-10-03T01:00:00+08:00",
                              terminal_kind="delivered")
        return task, attempt

    def test_artifact_digest_is_recorded(self):
        (self.out / "result.md").write_text("hello dsh", encoding="utf-8")
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)  # terminal_at=None：首次交付前正常状态
        manifest = self.adapter().collect_artifacts(task, attempt, ["out/result.md"],
                                                     quiesced=True)
        self.assertEqual(len(manifest), 1)
        import hashlib
        self.assertEqual(manifest[0]["path"], "out/result.md")
        self.assertEqual(manifest[0]["sha256"],
                         hashlib.sha256(b"hello dsh").hexdigest())
        self.assertEqual(manifest[0]["size"], 9)

    def test_dotdot_escape_rejected(self):
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().collect_artifacts(task, attempt, ["../escape.md"], quiesced=True)
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_absolute_escape_rejected(self):
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().collect_artifacts(task, attempt, ["/etc/passwd"], quiesced=True)
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_symlink_escape_rejected(self):
        link = self.out / "link.md"
        os.symlink("/etc/hosts", str(link))
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().collect_artifacts(task, attempt, ["out/link.md"], quiesced=True)
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_non_regular_file_rejected(self):
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().collect_artifacts(task, attempt, ["out"], quiesced=True)
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_hardlinked_file_rejected(self):
        target = self.out / "shared.md"
        target.write_text("x", encoding="utf-8")
        os.link(str(target), str(self.out / "shared2.md"))
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError):
            self.adapter().collect_artifacts(task, attempt, ["out/shared.md"], quiesced=True)

    def test_duplicate_entries_rejected(self):
        (self.out / "a.md").write_text("a", encoding="utf-8")
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError):
            self.adapter().collect_artifacts(task, attempt, ["out/a.md", "out/./a.md"],
                                             quiesced=True)

    def test_collect_requires_quiesced_proof(self):
        # 原失败语义保留：未证明宿主停稳（quiesced=False 缺省）时拒绝收集。
        task = raw_task(self.workspace)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().collect_artifacts(task, raw_attempt(task), ["out/a.md"])
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_collect_rejects_non_delivered_terminal_attempt(self):
        # 旧轮次已结束但不是 delivered（返工后旧 attempt）：只读拒绝。
        task = raw_task(self.workspace)
        attempt = raw_attempt(task, terminal_at="2026-10-03T01:00:00+08:00",
                              terminal_kind="failed")
        with self.assertRaises(ContractError) as ctx:
            self.adapter().collect_artifacts(task, attempt, ["out/a.md"], quiesced=True)
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_delivery_payload_paths_and_digests(self):
        (self.out / "result.md").write_text("r", encoding="utf-8")
        (self.out / "report.md").write_text("p", encoding="utf-8")
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)  # 首次交付：terminal_at=None 合法
        payload = self.adapter().build_delivery_payload(
            task, attempt, summary="完成离线验证",
            report_entry=str(self.out / "report.md"),
            changed_entries=["out/result.md", "out/report.md"],
            verification=[{"command": "python3 -m unittest", "result": "passed"}],
            risks=["DSH 无接收凭证"], unfinished=[], quiesced=True)
        self.assertEqual(payload["reportPath"], str(self.out / "report.md"))
        self.assertEqual(sorted(payload["changedFiles"]), ["out/report.md", "out/result.md"])
        self.assertEqual(len(payload["artifacts"]), 2)
        self.assertTrue(all(len(item["sha256"]) == 64 for item in payload["artifacts"]))

    def test_delivery_payload_requires_quiesced(self):
        # 原失败语义保留：宿主未停稳（quiesced=False 缺省）不能构造交付。
        (self.out / "result.md").write_text("r", encoding="utf-8")
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().build_delivery_payload(
                task, attempt, summary="x",
                report_entry=str(self.out / "report.md"),
                changed_entries=[], verification=[], quiesced=False)
        self.assertEqual(ctx.exception.code, "invalid_transition")


# =====================================================================
# 3.5 D4-DSH-02-SDK-001 两项 P1 回归
# =====================================================================
class DshP1RegressionTests(DshTestBase):
    """修前缺陷的回归锁定：同探针修前/修后行为可复现。"""

    def adapter(self):
        return DshAdapter(FakeDshTransport(), self.store_dir)

    def test_p1_first_delivery_no_terminal_at(self):
        """P1-1 修前：terminal_at=None + submitted/accepted → invalid_transition 死循环。

        修后：quiesced=True（宿主停稳证明）时首次交付可构造；EventService 入账
        task.delivered 后才写 D4 terminal_at（events.py:372），顺序正确。
        """
        (self.out / "result.md").write_text("r", encoding="utf-8")
        task = raw_task(self.workspace)
        attempt = raw_attempt(task, submitted_at="2026-10-03T00:00:00Z",
                              accepted_by_host_at="2026-10-03T00:00:01Z")
        self.assertIsNone(attempt.get("terminal_at"))
        payload = self.adapter().build_delivery_payload(
            task, attempt, summary="首次交付",
            report_entry=str(self.out / "result.md"),
            changed_entries=["out/result.md"], verification=[], quiesced=True)
        self.assertEqual(payload["changedFiles"], ["out/result.md"])

    def test_p1_workspace_mismatch_zero_launch(self):
        """P1-2 修前：declared ≠ actual 仍 submitted。

        修后：transport.workspace 属性存在且与 task.workspace 的 realpath 不一致时
        invalid_task + DSH_WORKSPACE_MISMATCH，零任务启动（真实 transport 用例）。
        """
        declared = self.root / "declared"
        actual = self.root / "actual"
        declared.mkdir()
        actual.mkdir()
        (declared / "out").mkdir()

        script = "import os,sys;print('0.1.1-rc.2' if '--version' in sys.argv else os.getcwd())"
        transport = DshHeadlessTransport(
            [sys.executable, "-c", script], actual, self.root / "mismatch-store",
            env={"HOME": str(self.root)})
        adapter = DshAdapter(transport, self.root / "submit-store")
        task = raw_task(declared)
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(task, raw_attempt(task))
        self.assertEqual(ctx.exception.code, "invalid_task")
        self.assertEqual(ctx.exception.details.get("dshCode"), "DSH_WORKSPACE_MISMATCH")
        # 修前探针断言 rejected_before_launch=False；修后必须为零启动：
        # 任务 transport.run 不执行，frames 目录不存在（构造函数只建目录骨架，
        # 不写任务帧）。子进程从未收到 --profile headless 的任务调用。
        frames = list((self.root / "mismatch-store").rglob("*.frames.json"))
        self.assertEqual(frames, [])

    def test_p1_workspace_alias_same_realpath_passes(self):
        """macOS 系统路径别名（/var → /private/var）同 realpath 正例通过。"""
        declared = self.root / "aliased"
        declared.mkdir()
        (declared / "out").mkdir()
        real = os.path.realpath(str(declared))
        alias = str(declared)
        if real == alias:
            self.skipTest("本机临时目录无别名前缀")
        task = raw_task(alias)
        attempt = raw_attempt(task)

        class AliasTransport(FakeDshTransport):
            workspace = Path(real)

        adapter = DshAdapter(AliasTransport(), self.store_dir)
        result = adapter.submit(task, attempt)
        self.assertEqual(result.status, "submitted")

    def test_p1_first_delivery_chain_via_real_services(self):
        """离线集成：真实服务状态构造交付 → 入账 → awaiting_review（终端顺序）。"""
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"})
        store = SqliteStore(self.db)
        store.register_source(DSH_SOURCE_ID, label="dsh-headless", at="t0")
        store.register_agent("dev-1", "D4 DSH worker", "registry", at="t0")
        adapter = DshAdapter(transport, self.store_dir)
        tasks = TaskService(store, adapter=adapter,
                            allowed_roots=[os.path.realpath(str(runtime_root()))])
        events = EventService(store)
        task = tasks.create_task(service_payload(self.workspace), context=_ctx_owner())
        attempt = tasks.create_attempt(task["taskId"], context=_ctx_worker())
        tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        # 宿主停稳（离线夹具：立即停稳）→ 构造首次交付 payload
        (self.out / "result.md").write_text("r", encoding="utf-8")
        row = store.get_attempt(attempt["attemptId"])
        self.assertIsNone(row["terminal_at"])  # 修前死循环位置：这里必须还是 None
        payload = adapter.build_delivery_payload(
            store.get_task(task["taskId"]), row,
            summary="首次交付链路", report_entry=str(self.out / "result.md"),
            changed_entries=["out/result.md"], verification=[], quiesced=True)
        # 交付入账 → EventService 写 terminal_at → awaiting_review
        delivered = events.ingest({
            "eventId": "evt-%s-1" % attempt["attemptId"], "projectId": task["projectId"],
            "taskId": task["taskId"], "attemptId": attempt["attemptId"],
            "sourceId": DSH_SOURCE_ID, "agentId": "dev-1", "sequence": 1,
            "type": "task.delivered", "occurredAt": "2026-10-03T10:00:00+08:00",
            "payload": payload}, context=_ctx_source())
        self.assertTrue(delivered["ok"])
        fresh = store.get_attempt(attempt["attemptId"])
        self.assertEqual(fresh["terminal_kind"], "delivered")
        self.assertEqual(store.get_task(task["taskId"])["status"], "awaiting_review")
        store.close()

    def test_p1_delivery_rejected_when_host_still_running(self):
        """宿主未停稳（quiesced=False）时拒绝交付构造（原失败语义保留）。"""
        (self.out / "result.md").write_text("r", encoding="utf-8")
        task = raw_task(self.workspace)
        attempt = raw_attempt(task)
        with self.assertRaises(ContractError) as ctx:
            self.adapter().build_delivery_payload(
                task, attempt, summary="x",
                report_entry=str(self.out / "result.md"),
                changed_entries=["out/result.md"], verification=[])
        self.assertEqual(ctx.exception.code, "invalid_transition")


# =====================================================================
# 4. D4 服务集成：submitted/accepted 分离、事件、返工、并发
# =====================================================================
class DshServiceIntegrationTests(DshTestBase):
    def _services(self, transport, db_path=None, store_dir=None):
        store = SqliteStore(db_path or self.db)
        store.register_source(DSH_SOURCE_ID, label="dsh-headless", at="t0")
        store.register_agent("dev-1", "D4 DSH worker", "registry", at="t0")
        adapter = DshAdapter(transport, store_dir or self.store_dir)
        tasks = TaskService(store, adapter=adapter,
                            allowed_roots=[os.path.realpath(str(runtime_root()))])
        events = EventService(store)
        reviews = ReviewService(store, tasks)
        return store, tasks, events, reviews

    def _ready_attempt(self, tasks, store):
        task = tasks.create_task(service_payload(self.workspace), context=_ctx_owner())
        attempt = tasks.create_attempt(task["taskId"], context=_ctx_worker())
        return task, attempt

    def _event(self, task, attempt, seq, kind, payload=None, event_id=None):
        return {
            "eventId": event_id or "evt-%s-%d" % (attempt["attemptId"], seq),
            "projectId": task["projectId"], "taskId": task["taskId"],
            "attemptId": attempt["attemptId"], "sourceId": DSH_SOURCE_ID,
            "agentId": "dev-1", "sequence": seq, "type": kind,
            "occurredAt": "2026-10-03T10:00:00+08:00",
            "payload": payload or {},
        }

    def _delivery_payload(self):
        return {"summary": "完成离线验证",
                "reportPath": str(self.out / "report.md"),
                "changedFiles": ["out/result.md", "out/report.md"],
                "verification": [{"command": "python3 -m unittest", "result": "passed"}],
                "risks": ["DSH 无接收凭证"], "unfinished": []}

    def test_submitted_does_not_imply_accepted_by_host(self):
        store, tasks, _events, _reviews = self._services(FakeDshTransport())
        task, attempt = self._ready_attempt(tasks, store)
        out = tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        self.assertEqual(out["submission"]["status"], "submitted")
        row = store.get_attempt(attempt["attemptId"])
        self.assertIsNotNone(row["submitted_at"])
        self.assertIsNone(row["accepted_by_host_at"])
        self.assertEqual(row["submit_state"], "submitted")
        store.close()

    def test_accepted_by_host_is_recorded_only_with_receipt(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"})
        store, tasks, _events, _reviews = self._services(transport)
        task, attempt = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        row = store.get_attempt(attempt["attemptId"])
        self.assertIsNotNone(row["accepted_by_host_at"])
        store.close()

    def test_unknown_submission_is_fail_closed_without_optimistic_state(self):
        store, tasks, _events, _reviews = self._services(FakeDshTransport(exit_code=1))
        task, attempt = self._ready_attempt(tasks, store)
        with self.assertRaises(ContractError) as ctx:
            tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        self.assertEqual(ctx.exception.code, "submit_unknown")
        row = store.get_attempt(attempt["attemptId"])
        self.assertEqual(row["submit_state"], "unknown")
        self.assertIsNone(row["submitted_at"])
        self.assertIsNone(row["accepted_by_host_at"])
        # 未知结果不自动重试：再提交只会再次 fail closed，不会重新调用宿主
        with self.assertRaises(ContractError) as again:
            tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        self.assertEqual(again.exception.code, "submit_unknown")
        store.close()

    def test_duplicate_submit_calls_host_once(self):
        counter = LaunchCounter()
        store, tasks, _events, _reviews = self._services(FakeDshTransport(counter=counter))
        task, attempt = self._ready_attempt(tasks, store)
        first = tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        second = tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(counter.value, 1)
        store.close()

    def test_delivery_rejected_without_host_acceptance(self):
        store, tasks, events, _reviews = self._services(FakeDshTransport())
        task, attempt = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        with self.assertRaises(ContractError) as ctx:
            events.ingest(self._event(task, attempt, 2, "task.delivered",
                                      self._delivery_payload()), context=_ctx_source())
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertIsNone(store.get_task(task["taskId"])["current_delivery_id"])
        store.close()

    def test_full_chain_with_compliant_host(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted",
                                        "sessionId": "session-%s" % attempt_id})
        store, tasks, events, reviews = self._services(transport)
        task, attempt = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        events.ingest(self._event(task, attempt, 1, "task.started", {"note": "开工"}),
                      context=_ctx_source())
        started = events.ingest(self._event(task, attempt, 2, "task.delivered",
                                            self._delivery_payload()), context=_ctx_source())
        delivery_id = started["data"]["deliveryId"]
        self.assertEqual(store.get_task(task["taskId"])["status"], "awaiting_review")
        review = reviews.submit_review(
            task["taskId"], {"decision": "approved", "deliveryId": delivery_id,
                             "note": "证据符合本轮目标"}, context=_ctx_reviewer())
        self.assertEqual(review["taskStatus"], "approved")
        self.assertFalse(review["closure"]["git"])
        self.assertFalse(review["closure"]["published"])
        store.close()

    def test_event_dedup_conflict_stale_and_terminal(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"})
        store, tasks, events, reviews = self._services(transport)
        task, attempt = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())

        first = self._event(task, attempt, 1, "task.progress", {"step": "s1", "note": "n1"})
        events.ingest(first, context=_ctx_source())
        replay = events.ingest(dict(first), context=_ctx_source())
        self.assertTrue(replay["replayed"])                      # 同内容 -> 回放

        conflict = dict(first, payload={"step": "s1", "note": "changed"})
        with self.assertRaises(ContractError) as ctx:
            events.ingest(conflict, context=_ctx_source())
        self.assertEqual(ctx.exception.code, "event_conflict")   # 同 id 异内容 -> 冲突

        with self.assertRaises(ContractError) as stale:
            events.ingest(self._event(task, attempt, 1, "task.progress",
                                      {"step": "s0"}, event_id="evt-new-old-seq"),
                          context=_ctx_source())
        self.assertEqual(stale.exception.code, "stale_sequence")

        events.ingest(self._event(task, attempt, 2, "task.delivered",
                                  self._delivery_payload()), context=_ctx_source())
        delivery_id = store.get_task(task["taskId"])["current_delivery_id"]
        reviews.submit_review(task["taskId"], {"decision": "approved",
                                               "deliveryId": delivery_id,
                                               "note": "通过"}, context=_ctx_reviewer())
        with self.assertRaises(ContractError) as terminal:
            events.ingest(self._event(task, attempt, 9, "task.progress",
                                      {"step": "late"}, event_id="evt-late"),
                          context=_ctx_source())
        self.assertEqual(terminal.exception.code, "invalid_transition")
        self.assertEqual(store.get_task(task["taskId"])["status"], "approved")
        store.close()

    def test_rework_creates_new_attempt_and_preserves_old_delivery(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"})
        store, tasks, events, reviews = self._services(transport)
        task, attempt1 = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt1["attemptId"], context=_ctx_worker())
        events.ingest(self._event(task, attempt1, 1, "task.delivered",
                                  self._delivery_payload()), context=_ctx_source())
        delivery1 = store.get_task(task["taskId"])["current_delivery_id"]

        rework = reviews.submit_review(
            task["taskId"], {"decision": "changes_requested", "deliveryId": delivery1,
                             "note": "补充证据"}, context=_ctx_reviewer())
        attempt2 = rework["resultingAttemptId"]
        self.assertNotEqual(attempt1["attemptId"], attempt2)
        self.assertEqual(store.get_task(task["taskId"])["current_attempt_id"], attempt2)
        self.assertIsNone(store.get_task(task["taskId"])["current_delivery_id"])
        self.assertIsNotNone(store.get_delivery(delivery1))      # 旧交付仍可查

        with self.assertRaises(ContractError) as ctx:            # 旧交付不能再审
            reviews.submit_review(task["taskId"], {"decision": "approved",
                                                   "deliveryId": delivery1, "note": "x"},
                                  context=_ctx_reviewer())
        self.assertEqual(ctx.exception.code, "review_version_mismatch")

        attempt2_row = {"taskId": task["taskId"], "attemptId": attempt2}
        tasks.submit_attempt(task["taskId"], attempt2, context=_ctx_worker())
        events.ingest(self._event(task, attempt2_row, 1, "task.delivered",
                                  self._delivery_payload()), context=_ctx_source())
        delivery2 = store.get_task(task["taskId"])["current_delivery_id"]
        self.assertNotEqual(delivery1, delivery2)
        self.assertEqual(len(store.list_deliveries(task["taskId"])), 2)
        store.close()

    def test_late_event_for_old_attempt_is_stale(self):
        transport = FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"})
        store, tasks, events, reviews = self._services(transport)
        task, attempt1 = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt1["attemptId"], context=_ctx_worker())
        events.ingest(self._event(task, attempt1, 1, "task.delivered",
                                  self._delivery_payload()), context=_ctx_source())
        delivery1 = store.get_task(task["taskId"])["current_delivery_id"]
        reviews.submit_review(task["taskId"], {"decision": "changes_requested",
                                               "deliveryId": delivery1, "note": "返工"},
                              context=_ctx_reviewer())
        with self.assertRaises(ContractError) as ctx:
            events.ingest(self._event(task, attempt1, 5, "task.progress", {"step": "late"},
                                      event_id="evt-late-old"), context=_ctx_source())
        self.assertEqual(ctx.exception.code, "stale_sequence")
        store.close()

    def test_max_attempts_blocks_automatic_loop(self):
        store, tasks, events, reviews = self._services(FakeDshTransport(
            receipt_mode="runid", evidence_level="LOCAL",
            receipt=lambda attempt_id: {"runId": attempt_id, "status": "accepted"}))
        task, attempt1 = self._ready_attempt(tasks, store)
        tasks.submit_attempt(task["taskId"], attempt1["attemptId"], context=_ctx_worker())
        events.ingest(self._event(task, attempt1, 1, "task.delivered",
                                  self._delivery_payload()), context=_ctx_source())
        delivery1 = store.get_task(task["taskId"])["current_delivery_id"]
        attempt2 = reviews.submit_review(
            task["taskId"], {"decision": "changes_requested", "deliveryId": delivery1,
                             "note": "再来一次"}, context=_ctx_reviewer())["resultingAttemptId"]
        tasks.submit_attempt(task["taskId"], attempt2, context=_ctx_worker())
        events.ingest(self._event(task, {"taskId": task["taskId"], "attemptId": attempt2},
                                  1, "task.delivered", self._delivery_payload()),
                      context=_ctx_source())
        delivery2 = store.get_task(task["taskId"])["current_delivery_id"]
        with self.assertRaises(ContractError) as ctx:
            reviews.submit_review(task["taskId"], {"decision": "changes_requested",
                                                   "deliveryId": delivery2, "note": "第三次"},
                                  context=_ctx_reviewer())
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(store.count_attempts(task["taskId"]), 2)
        store.close()

    def test_concurrent_duplicate_submit_across_connections(self):
        counter = LaunchCounter()
        transport = FakeDshTransport(counter=counter)
        store, tasks, _events, _reviews = self._services(transport)
        task, attempt = self._ready_attempt(tasks, store)
        store.close()

        results, errors = [], []
        barrier = threading.Barrier(2)

        def worker():
            conn = SqliteStore(self.db)
            local = TaskService(conn, adapter=DshAdapter(transport, self.store_dir),
                                allowed_roots=[os.path.realpath(str(runtime_root()))])
            barrier.wait()
            try:
                results.append(local.submit_attempt(task["taskId"], attempt["attemptId"],
                                                    context=_ctx_worker()))
            except ContractError as exc:
                errors.append(exc.code)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(counter.value, 1, "并发下只能启动一次 DSH")
        # 另一个连接只能是「回放首次结果」或「提交进行中」两种 fail-closed 结果之一；
        # 绝不允许出现第二次真实启动。
        fresh = [item for item in results if not item.get("replayed")]
        self.assertLessEqual(len(fresh), 1)
        for code in errors:
            self.assertIn(code, ("submit_in_progress", "submit_unknown"))
        # 只有一次真实启动，且没有乐观状态写入
        check = SqliteStore(self.db)
        row = check.get_attempt(attempt["attemptId"])
        self.assertIn(row["submit_state"], ("submitted", "submitting", "unknown"))
        check.close()

    def test_identity_mismatch_between_event_agent_and_task_fails_closed(self):
        store, tasks, events, _reviews = self._services(FakeDshTransport())
        task, attempt = self._ready_attempt(tasks, store)
        bad = self._event(task, attempt, 1, "task.progress", {"step": "s"})
        bad["agentId"] = "not-registered-member"
        with self.assertRaises(ContractError) as ctx:
            events.ingest(bad, context=_ctx_source())
        self.assertEqual(ctx.exception.code, "permission_denied")
        store.close()

    def test_unknown_source_rejected(self):
        store = SqliteStore(self.db)
        store.register_agent("dev-1", "worker", "registry", at="t0")
        tasks = TaskService(store, adapter=DshAdapter(FakeDshTransport(), self.store_dir),
                            allowed_roots=[os.path.realpath(str(runtime_root()))])
        with self.assertRaises(ContractError) as ctx:
            tasks.create_task(service_payload(self.workspace), context=_ctx_owner())
        self.assertEqual(ctx.exception.code, "unknown_source")
        store.close()


if __name__ == "__main__":
    unittest.main()
