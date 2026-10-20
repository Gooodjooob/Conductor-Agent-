"""D4-VAL-002-BRIDGE 定向测试：qclaw-gateway adapter 与 CLI transport。

关键断言（任务卡 §5）：
- accepted/in_flight + gateway metadata → 非空 host receipt；其余全部 fail closed
- 回执 runId 与本次 attempt 派生的幂等键不一致 → 不采信
- 同一 attempt 两次 submit 只调 transport 一次；unknown 不自动重试
- 凭据（state dir/config）只走环境变量，绝不进 argv；命令只允许绝对路径 + shell=False
- agent.wait/tasks.list/chat.history 仅预留，fail closed；不把终态快照伪装成 task.started
- 真实 Agent id 来自 adapter 配置，不被任务 payload 自报覆盖
- G1（D4-AGENT-ISOLATION-GATE）：宿主 Agent id 固定为受控常量 agent-d4-exec-002，
  构造函数不提供覆盖参数，任务 payload 自报 agentId 无效
"""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.qclaw_gateway import (
    CliGatewayTransport,
    GatewayTransportError,
    QClawGatewayAdapter,
    build_decision_message,
    build_task_message,
    validate_gateway_receipt,
)
from contracts.schemas import ContractError
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    ctx_owner,
    ctx_source,
    ctx_worker,
    runtime_root,
    validation_workspace,
)


class MockTransport:
    """离线 mock transport：模拟 gateway call agent 的回执，按队列顺序返回。

    evidence_level 自报 "LOCAL" 表示"本机可复现的协议链路"；离线/真实之辨
    由入口 synthetic 标记承担（本测试全部走 synthetic=True 的受控入口）。
    """

    evidence_level = "LOCAL"

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def queue(self, *items):
        self.responses.extend(items)

    def call(self, method, params, timeout_ms):
        self.calls.append({"method": method, "params": dict(params), "timeoutMs": timeout_ms})
        if not self.responses:
            raise AssertionError("MockTransport: 未预期的调用 %r" % (method,))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def gateway_response(idempotency_key, status="accepted", transport="gateway", **over):
    """构造 gateway call agent 的回执（对齐真实回执的关键字段）。"""
    response = {
        "runId": idempotency_key,
        "status": status,
        "meta": {"transport": transport},
        "sessionKey": "agent:agent-d4-exec-002:d4-ignored",
    }
    response.update(over)
    return response


def make_task_row(task_id="task-bridge-1", source_id="qclaw-gateway", agent_id="dev-1"):
    """adapter 层测试用的最小 task 行（snake_case，对齐 sqlite store 行结构）。"""
    return {
        "task_id": task_id,
        "project_id": "conductor",
        "source_id": source_id,
        "agent_id": agent_id,
        "goal": "验证 gateway 桥接切片",
        "workspace": validation_workspace(),
        "acceptance_json": json.dumps(["回执必须 fail closed"], ensure_ascii=False),
        "permissions_json": json.dumps({
            "writeScope": [".conductor-local/d4-real-chain-validation/"],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        }, ensure_ascii=False),
        "status": "active",
        "current_attempt_id": "att-bridge-1",
    }


def make_attempt_row(attempt_id="att-bridge-1", task_id="task-bridge-1"):
    return {"attempt_id": attempt_id, "task_id": task_id, "attempt_no": 1,
            "created_at": "2026-09-30T12:00:00+08:00"}


def make_review_row(review_id="rev-bridge-1", task_id="task-bridge-1", decision="changes_requested",
                    note="补充权限摘要后再交付", resulting_attempt_id="att-bridge-2"):
    return {
        "review_id": review_id,
        "task_id": task_id,
        "attempt_id": "att-bridge-1",
        "delivery_id": "dlv-bridge-1",
        "decision": decision,
        "note": note,
        "resulting_attempt_id": resulting_attempt_id,
        "review_round": 1,
        "reviewer_id": "cox",
    }


class GatewayReceiptValidationTests(unittest.TestCase):
    """validate_gateway_receipt 纯函数边界。"""

    KEY = "att-bridge-1"

    def test_accepts_accepted_and_in_flight_with_matching_run_id(self):
        for status in ("accepted", "in_flight"):
            run_id, got = validate_gateway_receipt(
                gateway_response(self.KEY, status=status), self.KEY)
            self.assertEqual(run_id, self.KEY)
            self.assertEqual(got, status)

    def test_rejects_non_object(self):
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt(["nope"], self.KEY)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_rejects_missing_gateway_transport_label(self):
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt(gateway_response(self.KEY, transport="embedded"), self.KEY)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        self.assertEqual(ctx.exception.details["reason"], "transport_label_missing")

    def test_rejects_missing_meta(self):
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt({"runId": self.KEY, "status": "accepted"}, self.KEY)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_trusted_cli_accepts_real_receipt_shape_with_bound_session(self):
        response = {"runId": self.KEY, "status": "accepted",
                    "sessionKey": "agent:agent-d4-exec-002:d4-task-bridge-1"}
        self.assertEqual(
            validate_gateway_receipt(
                response, self.KEY, trusted_gateway=True,
                expected_session_key=response["sessionKey"]),
            (self.KEY, "accepted"),
        )
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt(
                dict(response, sessionKey="agent:other"), self.KEY,
                trusted_gateway=True, expected_session_key=response["sessionKey"])
        self.assertEqual(ctx.exception.details["reason"], "session_key_mismatch")
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt(
                dict(response, meta={"transport": "embedded"}), self.KEY,
                trusted_gateway=True, expected_session_key=response["sessionKey"])
        self.assertEqual(ctx.exception.details["reason"], "transport_label_missing")

    def test_rejects_missing_run_id(self):
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt({"meta": {"transport": "gateway"}, "status": "accepted"}, self.KEY)
        self.assertEqual(ctx.exception.details["reason"], "run_id_missing")

    def test_rejects_mismatched_run_id(self):
        with self.assertRaises(ContractError) as ctx:
            validate_gateway_receipt(gateway_response("att-someone-else"), self.KEY)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        self.assertEqual(ctx.exception.details["reason"], "run_id_mismatch")

    def test_rejects_unknown_status(self):
        for status in ("queued", "completed", "failed", "", None):
            with self.assertRaises(ContractError) as ctx:
                validate_gateway_receipt(gateway_response(self.KEY, status=status), self.KEY)
            self.assertEqual(ctx.exception.details["reason"], "status_not_accepted")


class AdapterTestsBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.db = Path(self.tmp.name) / "d4-bridge.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()


class GatewayAdapterProtocolTests(AdapterTestsBase):
    """adapter 协议层：凭 MockTransport 直接验证 submit / send_decision。"""

    def _adapter(self, transport, **over):
        return QClawGatewayAdapter(transport, **over)

    def test_single_receipt_template_binds_submit_and_rework_runs(self):
        task, attempt = make_task_row(), make_attempt_row()
        review = make_review_row()
        for message, expected_run, expected_attempt in (
            (build_task_message(task, attempt), attempt["attempt_id"], attempt["attempt_id"]),
            (build_decision_message(task, review, make_attempt_row("att-bridge-2")),
             "relay-" + review["review_id"], "att-bridge-2"),
        ):
            templates = [line for line in message.splitlines() if line.startswith("最终 JSON：")]
            self.assertEqual(len(templates), 1)
            bundle = json.loads(templates[0].split("：", 1)[1])
            self.assertEqual(bundle["hostRunId"], expected_run)
            self.assertEqual([event["type"] for event in bundle["events"]],
                             ["task.started", "task.delivered"])
            for event in bundle["events"]:
                self.assertEqual(event["attemptId"], expected_attempt)
                self.assertEqual(event["taskId"], task["task_id"])
            self.assertIn("/workspace/receipts/", message)
            self.assertIn("近似自报时间", message)

    def test_capabilities_reported_as_unverified(self):
        # 静态声明如实：本切片未在真实宿主上验证 → connected=False。
        adapter = self._adapter(MockTransport())
        caps = adapter.capabilities()
        self.assertTrue(caps.submit)
        self.assertTrue(caps.host_acceptance)
        self.assertTrue(caps.stable_ids)
        self.assertFalse(caps.restart_recovery)
        self.assertEqual(caps.evidence_level, "UNVERIFIED")
        report = adapter.capability_report()
        self.assertEqual(report["adapter"], "qclaw-gateway")
        self.assertFalse(report["connected"])

    def test_adapter_requires_callable_transport(self):
        with self.assertRaises(ValueError):
            QClawGatewayAdapter(None)
        with self.assertRaises(ValueError):
            QClawGatewayAdapter(object())

    def test_submit_accepted_generates_non_empty_host_receipt(self):
        task = make_task_row()
        attempt = make_attempt_row()
        transport = MockTransport([gateway_response(attempt["attempt_id"])])
        result = self._adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertEqual(result.source_id, task["source_id"])
        self.assertEqual(result.host_receipt, attempt["attempt_id"])
        self.assertTrue(result.host_receipt)
        self.assertEqual(result.evidence_level, "LOCAL")

    def test_submit_in_flight_also_accepted_by_host(self):
        task = make_task_row()
        attempt = make_attempt_row()
        transport = MockTransport([gateway_response(attempt["attempt_id"], status="in_flight")])
        result = self._adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertIn("in_flight", result.note)

    def test_submit_params_use_fixed_d4_gateway_agent_mapping(self):
        # G1：宿主 Agent id 固定为受控常量；task.payload 的 agentId（逻辑 dev-1）不得覆盖。
        task = make_task_row(agent_id="dev-1")
        attempt = make_attempt_row()
        transport = MockTransport([gateway_response(attempt["attempt_id"])])
        self._adapter(transport).submit(task, attempt)
        params = transport.calls[0]["params"]
        self.assertEqual(params["agentId"], "agent-d4-exec-002")
        self.assertNotEqual(params["agentId"], task["agent_id"])
        self.assertEqual(params["sessionKey"], "agent:agent-d4-exec-002:d4-%s" % task["task_id"])
        self.assertEqual(params["idempotencyKey"], attempt["attempt_id"])
        self.assertFalse(params["deliver"])
        self.assertEqual(transport.calls[0]["method"], "agent")

    def test_adapter_rejects_agent_id_injection(self):
        # G1：构造函数不提供 agent_id 覆盖入口；注入会 fail loud（不静默接受）。
        # 关键字注入 → TypeError；位置参数注入会落到 timeout 参数上 → ValueError。
        transport = MockTransport()
        with self.assertRaises(TypeError):
            QClawGatewayAdapter(transport, agent_id="agent-rogue")
        with self.assertRaises((TypeError, ValueError)):
            QClawGatewayAdapter(transport, "agent-rogue")
        self.assertEqual(len(transport.calls), 0)

    def test_submit_rejects_attempt_from_another_task_before_transport(self):
        task = make_task_row(task_id="task-a")
        attempt = make_attempt_row(task_id="task-b")
        transport = MockTransport([gateway_response(attempt["attempt_id"])])
        with self.assertRaises(ContractError) as ctx:
            self._adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(len(transport.calls), 0)

    def test_submit_fail_closed_on_embedded_transport(self):
        transport = MockTransport([gateway_response("att-bridge-1", transport="embedded")])
        with self.assertRaises(ContractError) as ctx:
            self._adapter(transport).submit(make_task_row(), make_attempt_row())
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_submit_fail_closed_on_missing_run_id(self):
        transport = MockTransport([{"meta": {"transport": "gateway"}, "status": "accepted"}])
        with self.assertRaises(ContractError) as ctx:
            self._adapter(transport).submit(make_task_row(), make_attempt_row())
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_submit_fail_closed_on_mismatched_run_id(self):
        transport = MockTransport([gateway_response("att-other")])
        with self.assertRaises(ContractError) as ctx:
            self._adapter(transport).submit(make_task_row(), make_attempt_row())
        self.assertEqual(ctx.exception.details["reason"], "run_id_mismatch")

    def test_submit_fail_closed_on_unknown_status(self):
        transport = MockTransport([gateway_response("att-bridge-1", status="queued")])
        with self.assertRaises(ContractError) as ctx:
            self._adapter(transport).submit(make_task_row(), make_attempt_row())
        self.assertEqual(ctx.exception.details["reason"], "status_not_accepted")

    def test_submit_propagates_transport_error(self):
        transport = MockTransport([GatewayTransportError("GATEWAY_TIMEOUT")])
        with self.assertRaises(GatewayTransportError):
            self._adapter(transport).submit(make_task_row(), make_attempt_row())

    def test_task_message_contains_ids_goal_and_permissions(self):
        task = make_task_row()
        attempt = make_attempt_row()
        message = build_task_message(task, attempt)
        for expected in (
            "taskId: task-bridge-1",
            "attemptId: att-bridge-1",
            "goal: 验证 gateway 桥接切片",
            "workspace: %s" % validation_workspace(),
            "acceptance[1]: 回执必须 fail closed",
            "writeScope",
            "allowPublish",
        ):
            self.assertIn(expected, message)

    def test_send_decision_relay_params_and_receipt(self):
        task = make_task_row()
        review = make_review_row()
        attempt = make_attempt_row(attempt_id="att-bridge-2")
        transport = MockTransport([gateway_response("relay-rev-bridge-1")])
        receipt = self._adapter(transport).send_decision(task, review, attempt)
        self.assertEqual(receipt["runId"], "relay-rev-bridge-1")
        self.assertEqual(receipt["idempotencyKey"], "relay-rev-bridge-1")
        self.assertEqual(receipt["sessionKey"], "agent:agent-d4-exec-002:d4-task-bridge-1")
        params = transport.calls[0]["params"]
        self.assertEqual(params["idempotencyKey"], "relay-rev-bridge-1")

    def test_send_decision_message_contains_rework_facts(self):
        # G4：消息必须包含原 taskId、新 attemptId、返工说明与权限原文摘要。
        task = make_task_row()
        review = make_review_row()
        attempt = make_attempt_row(attempt_id="att-bridge-2")
        message = build_decision_message(task, review, attempt)
        for expected in (
            "taskId: task-bridge-1",
            "attemptId: att-bridge-2",
            "返工说明: 补充权限摘要后再交付",
            "writeScope",
            "allowNetwork",
        ):
            self.assertIn(expected, message)

    def test_send_decision_requires_changes_requested(self):
        review = make_review_row(decision="approved", resulting_attempt_id=None)
        with self.assertRaises(ContractError) as ctx:
            self._adapter(MockTransport()).send_decision(
                make_task_row(), review, make_attempt_row())
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_send_decision_requires_new_attempt(self):
        review = make_review_row(resulting_attempt_id=None)
        with self.assertRaises(ContractError) as ctx:
            self._adapter(MockTransport()).send_decision(
                make_task_row(), review, make_attempt_row())
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_send_decision_rejects_mismatched_review_or_attempt_before_transport(self):
        task = make_task_row()
        transport = MockTransport([gateway_response("relay-rev-bridge-1")])
        wrong_review = make_review_row(task_id="task-other")
        with self.assertRaises(ContractError) as ctx:
            self._adapter(transport).send_decision(
                task, wrong_review, make_attempt_row(attempt_id="att-bridge-2"))
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(len(transport.calls), 0)

        wrong_attempt = make_attempt_row(attempt_id="att-bridge-3")
        with self.assertRaises(ContractError) as ctx2:
            self._adapter(transport).send_decision(
                task, make_review_row(), wrong_attempt)
        self.assertEqual(ctx2.exception.code, "invalid_transition")
        self.assertEqual(len(transport.calls), 0)

    def test_send_decision_fail_closed_on_bad_receipt(self):
        transport = MockTransport([gateway_response("relay-att-other")])
        with self.assertRaises(ContractError):
            self._adapter(transport).send_decision(
                make_task_row(), make_review_row(), make_attempt_row(attempt_id="att-bridge-2"))
        # transport 报错 → 原样传播，不生成任何审核事实
        transport2 = MockTransport([GatewayTransportError("GATEWAY_EXIT_1")])
        with self.assertRaises(GatewayTransportError):
            self._adapter(transport2).send_decision(
                make_task_row(), make_review_row(), make_attempt_row(attempt_id="att-bridge-2"))

    def test_reserved_interfaces_fail_closed(self):
        adapter = self._adapter(MockTransport())
        task = make_task_row()
        attempt = make_attempt_row()
        for call in (
            lambda: adapter.wait_for_terminal(task, attempt, 1000),
            lambda: adapter.list_host_tasks(),
            lambda: adapter.fetch_history(task, attempt),
        ):
            with self.assertRaises(ContractError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "invalid_transition")


class GatewayAdapterServiceIntegrationTests(AdapterTestsBase):
    """TaskService 集成层：mock transport 下 submit_attempt 完整链路落库。"""

    def _setup(self, transport):
        store = SqliteStore(self.db)
        store.register_source("qclaw-gateway", label="bridge-002 测试来源")
        store.register_agent("dev-1", label="bridge-002 worker")
        adapter = QClawGatewayAdapter(transport)
        tasks = TaskService(store, adapter=adapter, allowed_roots=allowed_roots())
        return store, tasks

    def _create_task_and_attempt(self, store, tasks):
        payload = create_task_payload(sourceId="qclaw-gateway")
        created = tasks.create_task(payload, ctx_owner())
        task_id = created["taskId"]
        attempt = tasks.create_attempt(task_id, context=ctx_worker())
        return task_id, attempt["attemptId"]

    def test_submit_attempt_accepted_writes_host_receipt(self):
        transport = MockTransport()
        store, tasks = self._setup(transport)
        payload = create_task_payload(sourceId="qclaw-gateway")
        payload["acceptance"] = ["真实任务消息包含验收条件"]
        task_id = tasks.create_task(payload, ctx_owner())["taskId"]
        attempt_id = tasks.create_attempt(task_id, context=ctx_worker())["attemptId"]
        transport.queue(gateway_response(attempt_id))
        result = tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        self.assertFalse(result.get("replayed", False))
        row = store.get_attempt(attempt_id)
        self.assertIsNotNone(row["accepted_by_host_at"])
        stored = json.loads(row["submission_json"])
        self.assertEqual(stored["host_receipt"], attempt_id)
        # 成功路径 submit_state 落库为 "submitted"（接收时间另存 accepted_by_host_at）
        self.assertEqual(row["submit_state"], "submitted")
        message = transport.calls[0]["params"]["message"]
        task = store.get_task(task_id)
        self.assertIn("goal: %s" % task["goal"], message)
        self.assertIn("workspace: %s" % task["workspace"], message)
        self.assertIn("acceptance[1]: %s" % json.loads(task["acceptance_json"])[0], message)
        store.close()

    def test_double_submit_calls_transport_once_and_replays(self):
        transport = MockTransport()
        store, tasks = self._setup(transport)
        task_id, attempt_id = self._create_task_and_attempt(store, tasks)
        transport.queue(gateway_response(attempt_id))
        first = tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        second = tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        self.assertTrue(second.get("replayed"))
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(second["submission"], first["submission"])
        store.close()

    def test_transport_error_records_unknown_and_never_retries(self):
        transport = MockTransport([GatewayTransportError("GATEWAY_TIMEOUT")])
        store, tasks = self._setup(transport)
        task_id, attempt_id = self._create_task_and_attempt(store, tasks)
        with self.assertRaises(ContractError) as ctx:
            tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        self.assertEqual(ctx.exception.code, "submit_unknown")
        row = store.get_attempt(attempt_id)
        self.assertEqual(row["submit_state"], "unknown")
        # unknown 后重试：不再调 transport，继续 fail closed
        with self.assertRaises(ContractError) as ctx2:
            tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        self.assertEqual(ctx2.exception.code, "submit_unknown")
        self.assertEqual(len(transport.calls), 1)
        store.close()

    def test_bad_receipt_records_unknown_and_never_retries(self):
        transport = MockTransport([gateway_response("att-forged")])
        store, tasks = self._setup(transport)
        task_id, attempt_id = self._create_task_and_attempt(store, tasks)
        with self.assertRaises(ContractError) as ctx:
            tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        row = store.get_attempt(attempt_id)
        self.assertEqual(row["submit_state"], "unknown")
        self.assertIsNone(row["accepted_by_host_at"])
        with self.assertRaises(ContractError):
            tasks.submit_attempt(task_id, attempt_id, ctx_worker())
        self.assertEqual(len(transport.calls), 1)
        store.close()


class CliGatewayTransportTests(unittest.TestCase):
    """真实 CLI transport 的 argv/env 安全边界（不执行真实 gateway 调用）。

    构造校验只要求绝对路径，不要求文件存在；测试用通用 fixture 路径，
    不硬编码本机 QClaw 安装位置，保证可移植。
    """

    BIN = "/usr/bin/openclaw-fixture"
    STATE = "/tmp/openclaw-state-fixture"

    def test_rejects_relative_command_and_state_dir(self):
        with self.assertRaises(ValueError):
            CliGatewayTransport(command=["openclaw"], state_dir=self.STATE)
        with self.assertRaises(ValueError):
            CliGatewayTransport(command=[self.BIN], state_dir="relative/state")

    def _transport(self, **over):
        kwargs = {"command": [self.BIN], "state_dir": self.STATE}
        kwargs.update(over)
        return CliGatewayTransport(**kwargs)

    @staticmethod
    def _fake_completed(stdout, returncode=0):
        completed = subprocess.CompletedProcess(args=[], returncode=returncode)
        completed.stdout = stdout
        completed.stderr = "boom" if returncode else ""
        return completed

    def test_call_builds_safe_argv_and_env(self):
        transport = self._transport()
        transport._approved_key = "x"
        with mock.patch("adapters.qclaw_gateway.subprocess.run") as run:
            run.return_value = self._fake_completed(
                json.dumps({"runId": "x", "status": "accepted",
                            "meta": {"transport": "gateway"}}))
            transport.call("agent", {"agentId": "agent-d4-exec-002",
                                     "idempotencyKey": "x"}, 15000)
        kwargs = run.call_args.kwargs
        argv = run.call_args.args[0]
        self.assertEqual(kwargs["shell"], False)
        self.assertEqual(kwargs["env"]["OPENCLAW_STATE_DIR"], self.STATE)
        # 凭据绝不进 argv：state dir 与环境变量值不出现在任何 argv 元素
        for element in argv:
            self.assertNotIn(self.STATE, str(element))
            self.assertNotIn("OPENCLAW_STATE_DIR", str(element))
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertIn("gateway", argv)
        self.assertIn("call", argv)
        self.assertIn("agent", argv)
        self.assertEqual(argv[-1], "15000")

    def test_call_parses_json_with_proxy_bootstrap_prefix(self):
        transport = self._transport()
        transport._approved_key = "x"
        stdout = "[proxy-bootstrap] ready\n%s\n" % json.dumps(
            {"runId": "x", "status": "accepted", "meta": {"transport": "gateway"}})
        with mock.patch("adapters.qclaw_gateway.subprocess.run") as run:
            run.return_value = self._fake_completed(stdout)
            response = transport.call("agent", {"agentId": "agent-d4-exec-002",
                                                "idempotencyKey": "x"}, 100)
        self.assertEqual(response["runId"], "x")

    def test_adapter_accepts_unlabeled_real_cli_receipt_after_preflight(self):
        transport = self._transport()
        task = make_task_row()
        attempt = make_attempt_row()
        response = {
            "runId": attempt["attempt_id"],
            "status": "accepted",
            "sessionKey": "agent:agent-d4-exec-002:d4-task-bridge-1",
            "acceptedAt": 1790823462000,
        }

        def approve(_task, key):
            transport._approved_key = key

        with mock.patch.object(transport, "preflight", side_effect=approve), \
                mock.patch("adapters.qclaw_gateway.subprocess.run") as run:
            run.return_value = self._fake_completed(json.dumps(response))
            receipt = QClawGatewayAdapter(transport).submit(task, attempt)
        self.assertEqual(receipt.status, "accepted_by_host")
        self.assertEqual(receipt.host_receipt, attempt["attempt_id"])
        self.assertEqual(run.call_count, 1)

    def test_agent_call_without_matching_preflight_is_rejected_without_rpc(self):
        transport = self._transport()
        with mock.patch("adapters.qclaw_gateway.subprocess.run") as run:
            with self.assertRaises(GatewayTransportError):
                transport.call("agent", {"agentId": "agent-d4-exec-002",
                                         "idempotencyKey": "x"}, 100)
            transport._approved_key = "x"
            with self.assertRaises(GatewayTransportError):
                transport.call("agent", {"agentId": "agent-d4-exec-002",
                                         "idempotencyKey": "y"}, 100)
            run.assert_not_called()

    def test_call_raises_on_nonzero_exit(self):
        transport = self._transport()
        with mock.patch("adapters.qclaw_gateway.subprocess.run") as run:
            run.return_value = self._fake_completed("{}", returncode=1)
            with self.assertRaises(GatewayTransportError) as ctx:
                transport.call("config.get", {}, 100)
        self.assertIn("GATEWAY_EXIT_1", str(ctx.exception))

    def test_call_raises_on_timeout(self):
        transport = self._transport()
        with mock.patch("adapters.qclaw_gateway.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1)):
            with self.assertRaises(GatewayTransportError) as ctx:
                transport.call("config.get", {}, 100)
        self.assertIn("GATEWAY_TIMEOUT", str(ctx.exception))

    def test_call_raises_on_invalid_json(self):
        transport = self._transport()
        with mock.patch("adapters.qclaw_gateway.subprocess.run") as run:
            run.return_value = self._fake_completed("not json at all")
            with self.assertRaises(GatewayTransportError) as ctx:
                transport.call("config.get", {}, 100)
        self.assertIn("GATEWAY_INVALID_JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
