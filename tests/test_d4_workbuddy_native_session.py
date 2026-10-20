"""D4-WB-NATIVE-SESSION-001 离线 mock 测试 —— WorkBuddy 原生会话能力门。

任务卡 §3：Research Gate 结论为能力阻断（NATIVE_SESSION_CAPABILITY_BLOCKED），
因此本文件只做**离线 mock** 验证，全部用例不触碰真实宿主、真实会话或私有存储：

1. 默认门：无 transport 时一切原生会话操作 fail closed；
2. 注入 mock transport 后：宿主编号校验（attemptId 冒充 / 空值 / 非字符串）；
3. 来源校验：未经 create 登记的编号不可 send/read/close；
4. 幂等：同一幂等键重复发送回放首次结果，不二次投递；
5. 未知结果：transport 返回无法判读时 fail closed 为 unknown，不自动重试。
"""
from __future__ import annotations

import unittest

from adapters.workbuddy_native import (
    HOST_STATES,
    NATIVE_SESSION_CAPABILITY,
    NativeSessionGate,
    NativeSessionTransportError,
    is_d4_attempt_id,
    validate_host_conversation_id,
)
from contracts.schemas import ContractError


class FakeNativeTransport:
    """离线 mock transport：只返回固定应答，不模拟任何真实宿主。"""

    def __init__(self, conversation_id="11111111-2222-4333-8444-555555555555"):
        self.conversation_id = conversation_id
        self.created = []
        self.sent = []
        self.events_response = []
        self.send_results = [{"state": "accepted_by_host"}]
        self.fail_read = False

    def create_conversation(self, workspace, title):
        self.created.append((workspace, title))
        return self.conversation_id

    def send_task(self, conversation_id, task_message, idempotency_key):
        self.sent.append((conversation_id, idempotency_key))
        # 多次投递时按序取应答；超出预置列表则复用最后一条。
        index = min(len(self.sent) - 1, len(self.send_results) - 1)
        return dict(self.send_results[index])

    def read_events(self, conversation_id, cursor):
        if self.fail_read:
            raise NativeSessionTransportError("WORKBUDDY_NATIVE_PROTOCOL_BROKEN")
        return list(self.events_response)

    def get_conversation_link(self, conversation_id):
        return "workbuddy://conversation/%s" % conversation_id

    def close_or_detach(self, conversation_id):
        return {"detached": True}


class CapabilityGateTests(unittest.TestCase):
    def test_default_gate_blocks_create(self):
        gate = NativeSessionGate()
        report = gate.capability_report()
        self.assertEqual(report["capability"], NATIVE_SESSION_CAPABILITY)
        self.assertFalse(report["transportInjected"])
        with self.assertRaises(ContractError) as ctx:
            gate.create_conversation("/tmp/ws", "t")
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertIn("NATIVE_SESSION_CAPABILITY_BLOCKED",
                      str(ctx.exception.details.get("capability")))

    def test_transport_missing_methods_rejected(self):
        class Incomplete:
            pass
        with self.assertRaises(ValueError) as ctx:
            NativeSessionGate(transport=Incomplete())
        self.assertIn("WORKBUDDY_NATIVE_TRANSPORT_INCOMPLETE", str(ctx.exception))


class ConversationIdValidationTests(unittest.TestCase):
    def test_attempt_id_cannot_impersonate_conversation_id(self):
        with self.assertRaises(ContractError) as ctx:
            validate_host_conversation_id("att-demo-800bda0504")
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_is_d4_attempt_id_shape(self):
        self.assertTrue(is_d4_attempt_id("att-demo-6ea2fdbfeb"))
        self.assertFalse(is_d4_attempt_id("att-XYZ"))
        self.assertFalse(is_d4_attempt_id("11111111-2222-4333-8444-555555555555"))
        self.assertFalse(is_d4_attempt_id(None))

    def test_empty_and_non_string_rejected(self):
        for bad in ("", "   ", None, 12345):
            with self.assertRaises(ContractError):
                validate_host_conversation_id(bad)

    def test_uuid_shape_accepted(self):
        self.assertEqual(
            validate_host_conversation_id("11111111-2222-4333-8444-555555555555"),
            "11111111-2222-4333-8444-555555555555",
        )


class SourceValidationTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeNativeTransport()
        self.gate = NativeSessionGate(transport=self.transport)
        self.conversation_id = self.gate.create_conversation("/tmp/ws", "canary title")

    def test_create_registers_host_id(self):
        self.assertEqual(self.conversation_id, self.transport.conversation_id)
        self.assertEqual(self.transport.created, [("/tmp/ws", "canary title")])

    def test_unregistered_id_rejected_for_send(self):
        with self.assertRaises(ContractError) as ctx:
            self.gate.send_task("11111111-2222-4333-8444-555555555555", "msg", "idem-1")
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertEqual(self.transport.sent, [])

    def test_unregistered_id_rejected_for_read_and_close(self):
        foreign = "11111111-2222-4333-8444-555555555555"
        with self.assertRaises(ContractError):
            self.gate.read_events(foreign, 0)
        with self.assertRaises(ContractError):
            self.gate.get_conversation_link(foreign)
        with self.assertRaises(ContractError):
            self.gate.close_or_detach(foreign)

    def test_registered_id_allows_full_lifecycle(self):
        result = self.gate.send_task(self.conversation_id, "task message", "idem-1")
        self.assertEqual(result["state"], "accepted_by_host")
        events = self.gate.read_events(self.conversation_id, 0)
        self.assertEqual(events, [])
        link = self.gate.get_conversation_link(self.conversation_id)
        self.assertIn(self.conversation_id, link)
        self.assertTrue(self.gate.close_or_detach(self.conversation_id)["detached"])


class IdempotencyAndFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeNativeTransport()
        self.gate = NativeSessionGate(transport=self.transport)
        self.conversation_id = self.gate.create_conversation("/tmp/ws", "t")

    def test_duplicate_send_replays_first_result(self):
        first = self.gate.send_task(self.conversation_id, "m1", "idem-1")
        second = self.gate.send_task(self.conversation_id, "m2", "idem-1")
        self.assertTrue(first.get("replayed") is None)
        self.assertTrue(second["replayed"])
        # 幂等键相同 → 只投递一次；不同键才产生第二次真实投递。
        self.assertEqual(len(self.transport.sent), 1)
        third = self.gate.send_task(self.conversation_id, "m3", "idem-2")
        self.assertEqual(third["state"], "accepted_by_host")
        self.assertEqual(len(self.transport.sent), 2)

    def test_unknown_transport_result_fail_closed(self):
        self.transport.send_results = [{"nonsense": True}]
        with self.assertRaises(ContractError) as ctx:
            self.gate.send_task(self.conversation_id, "m", "idem-unknown")
        self.assertEqual(ctx.exception.code, "submit_unknown")

    def test_all_host_states_recognized(self):
        for state in HOST_STATES:
            transport = FakeNativeTransport()
            gate = NativeSessionGate(transport=transport)
            cid = gate.create_conversation("/tmp/ws", "t")
            transport.send_results = [{"state": state}]
            result = gate.send_task(cid, "m", "idem-%s" % state)
            self.assertEqual(result["state"], state)

    def test_read_transport_error_propagates(self):
        self.transport.fail_read = True
        with self.assertRaises(NativeSessionTransportError):
            self.gate.read_events(self.conversation_id, 0)


if __name__ == "__main__":
    unittest.main()
