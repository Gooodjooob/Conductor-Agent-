"""D4-VAL-001 定向测试：契约结构与错误码。

运行目录（隔离）：受环境变量 D4_VAL_RUNTIME_DIR 控制，默认写入本次 evidence 子目录。
"""
import os
import tempfile
import unittest
from pathlib import Path

from contracts.schemas import (
    ERROR_CODES,
    ContractError,
    validate_create_task,
    validate_delivery,
    validate_event,
    validate_review_decision,
)
from service.context import (
    owner_context,
    reviewer_context,
    source_context,
    worker_context,
)


def runtime_root():
    base = os.environ.get("D4_VAL_RUNTIME_DIR")
    root = Path(base) if base else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def allowed_roots():
    """受控服务配置：本轮唯一允许的隔离根（只指向本卡隔离 runtime）。

    workspace 必须落在服务配置注入的 allowed roots 内；调用方不得用 payload 自定上限。
    """
    return [os.path.realpath(str(runtime_root()))]


def validation_workspace():
    """默认受控 workspace：落在隔离 runtime 内（非任意目录）。"""
    return os.path.realpath(os.path.join(str(runtime_root()), "workspace"))


def create_task_payload(**over):
    payload = {
        "projectId": "conductor",
        "title": "验证 D4 真实任务链路",
        "agentId": "dev-1",
        "sourceId": "qclaw-local",
        "workspace": validation_workspace(),
        "permissions": {
            "writeScope": [".conductor-local/d4-real-chain-validation/"],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        },
        "goal": "证明任务可被指定 Agent 收到、执行、交付、返工并通过。",
    }
    payload.update(over)
    return payload


# 受控入口注入的调用者上下文（测试用合成入口；synthetic 标记模拟证据）
def ctx_owner(actor_id="cox"):
    return owner_context(actor_id=actor_id, entry="r1-test-harness", synthetic=True)


def ctx_reviewer(actor_id="cox"):
    return reviewer_context(actor_id=actor_id, entry="r1-test-harness", synthetic=True)


def ctx_worker(actor_id="dev-1"):
    return worker_context(actor_id=actor_id, entry="r1-test-harness", synthetic=True)


def ctx_source(source_id="qclaw-local"):
    return source_context(source_id=source_id, entry="r1-test-harness", synthetic=True)


class ContractTests(unittest.TestCase):
    def test_valid_create_task_passes(self):
        self.assertEqual(validate_create_task(create_task_payload()), {})

    def test_missing_fields_reported(self):
        errors = validate_create_task({"projectId": "conductor"})
        self.assertIn("missing", errors)
        for field in ("title", "agentId", "sourceId", "workspace", "goal"):
            self.assertIn(field, errors["missing"])

    def test_workspace_must_be_absolute(self):
        errors = validate_create_task(create_task_payload(workspace="relative/dir"))
        self.assertEqual(errors.get("workspace"), "workspace 必须是绝对路径")

    def test_permissions_keys_required(self):
        errors = validate_create_task(create_task_payload(permissions={"allowGit": False}))
        self.assertIn("permissions_missing", errors)
        self.assertIn("allowNetwork", errors["permissions_missing"])

    def test_event_valid_and_unknown_type(self):
        good = {
            "eventId": "evt-1", "projectId": "conductor", "taskId": "t1", "attemptId": "a1",
            "sourceId": "qclaw-local", "agentId": "dev-1", "sequence": 1,
            "type": "task.started", "occurredAt": "2026-09-29T11:20:00+08:00",
        }
        self.assertEqual(validate_event(good), {})
        bad = dict(good, type="task.exploded")
        self.assertIn("type", validate_event(bad))

    def test_event_sequence_must_be_int(self):
        bad = {
            "eventId": "evt-2", "projectId": "conductor", "taskId": "t1", "attemptId": "a1",
            "sourceId": "qclaw-local", "agentId": "dev-1", "sequence": "x",
            "type": "task.started", "occurredAt": "2026-09-29T11:20:00+08:00",
        }
        self.assertIn("sequence", validate_event(bad))

    def test_delivery_requires_summary_and_report(self):
        errors = validate_delivery({"changedFiles": []})
        self.assertIn("summary", errors["missing"])
        self.assertIn("reportPath", errors["missing"])
        self.assertIn("risks", errors["missing"])

    def test_review_decision_rules(self):
        self.assertEqual(
            validate_review_decision({"decision": "approved", "deliveryId": "d1"}), {}
        )
        self.assertIn("decision", validate_review_decision({"decision": "maybe", "deliveryId": "d1"}))
        self.assertIn(
            "note",
            validate_review_decision({"decision": "changes_requested", "deliveryId": "d1"}),
        )

    def test_contract_error_rejects_unknown_code(self):
        with self.assertRaises(ValueError):
            ContractError("not_a_code")

    def test_error_codes_cover_contract_table(self):
        for code in (
            "invalid_task", "permission_denied", "unknown_source", "duplicate_event",
            "event_conflict", "stale_sequence", "invalid_transition",
            "review_version_mismatch", "host_unconfirmed",
        ):
            self.assertIn(code, ERROR_CODES)


if __name__ == "__main__":
    unittest.main()
