"""D4-VAL-001 R1 边界测试：身份/权限/契约/路径/limits 的 fail-closed 规则。"""
import os
import tempfile
import unittest
from pathlib import Path

from contracts.paths import assert_within_scope, resolve_within, resolve_workspace
from contracts.schemas import (
    ContractError,
    LIMIT_RANGES,
    validate_create_task,
    validate_limits,
    validate_permissions,
)
from service.context import CallerContext, REVIEW_SUBMIT

from test_d4_contracts import (
    create_task_payload,
    ctx_owner,
    ctx_source,
    ctx_worker,
    runtime_root,
)
from test_d4_events import DELIVERY_BODY, ev, make_env


def _perms(**over):
    perms = {
        "writeScope": [".conductor-local/d4-real-chain-validation/"],
        "allowNetwork": False,
        "allowBusinessWrite": False,
        "allowGit": False,
        "allowPublish": False,
    }
    perms.update(over)
    return perms


class ContractBoundaryTests(unittest.TestCase):
    def test_permissions_must_be_real_bool(self):
        for key in ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish"):
            errors = validate_permissions(_perms(**{key: 0}))
            self.assertIn("permissions.%s" % key, errors)

    def test_permissions_require_all_keys_and_scope_array(self):
        partial = _perms()
        del partial["allowPublish"]
        self.assertIn("permissions_missing", validate_permissions(partial))
        self.assertIn("permissions.writeScope", validate_permissions(_perms(writeScope=[])))
        self.assertIn("permissions.writeScope", validate_permissions(_perms(writeScope="x")))

    def test_limits_must_be_strict_int_in_range(self):
        self.assertIn("limits.maxAttempts", validate_limits({"maxAttempts": "2"}))
        self.assertIn("limits.maxAttempts", validate_limits({"maxAttempts": True}))
        self.assertIn("limits.maxAttempts", validate_limits({"maxAttempts": 3}))
        self.assertIn("limits.timeoutMinutes", validate_limits({"timeoutMinutes": 0}))
        self.assertIn("limits.nope", validate_limits({"nope": 1}))
        self.assertEqual(validate_limits({"maxAttempts": 2}), {})
        self.assertEqual(LIMIT_RANGES["maxAttempts"], (1, 2))


class PathBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_workspace_requires_absolute(self):
        with self.assertRaises(ContractError) as ctx:
            resolve_workspace("relative/dir")
        self.assertEqual(ctx.exception.code, "invalid_task")

    def test_parent_escape_is_rejected(self):
        workspace = str(self.root)
        with self.assertRaises(ContractError) as ctx:
            resolve_within(workspace, "../../etc/passwd")
        self.assertEqual(ctx.exception.code, "path_escape")
        with self.assertRaises(ContractError) as ctx2:
            resolve_within(workspace, "/etc/passwd")
        self.assertEqual(ctx2.exception.code, "path_escape")

    def test_symlink_escape_is_rejected(self):
        workspace = self.root / "ws"
        outside = self.root / "outside"
        workspace.mkdir()
        outside.mkdir()
        (workspace / "link").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ContractError) as ctx:
            resolve_within(str(workspace), "link/secret.txt")
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_assert_within_scope_enforces_scope(self):
        workspace = str(self.root)
        scopes = [".conductor-local/"]
        with self.assertRaises(ContractError) as ctx:
            assert_within_scope(workspace, scopes, "other/file.md")
        self.assertEqual(ctx.exception.code, "path_escape")
        # 落在 scope 内则通过
        self.assertTrue(assert_within_scope(workspace, scopes, ".conductor-local/file.md"))


class ServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.env = make_env(self.tmp.name)

    def tearDown(self):
        self.env.store.close()
        self.tmp.cleanup()

    def test_every_entry_requires_injected_identity(self):
        with self.assertRaises(ContractError) as c1:
            self.env.tasks.create_task(create_task_payload(), context=None)
        self.assertEqual(c1.exception.code, "permission_denied")

        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        with self.assertRaises(ContractError) as c2:
            self.env.tasks.create_attempt(task["taskId"], context=None)
        self.assertEqual(c2.exception.code, "permission_denied")

        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        with self.assertRaises(ContractError) as c3:
            self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=None)
        self.assertEqual(c3.exception.code, "permission_denied")

        with self.assertRaises(ContractError) as c4:
            self.env.events.ingest(ev("e", 1, "task.progress", task, attempt), context=None)
        self.assertEqual(c4.exception.code, "permission_denied")

        with self.assertRaises(ContractError) as c5:
            self.env.reviews.submit_review(task["taskId"], {"decision": "approved", "deliveryId": "d"})
        self.assertEqual(c5.exception.code, "permission_denied")

    def test_worker_cannot_approve_even_with_capability(self):
        # 伪造一个带 REVIEW_SUBMIT 但角色为 worker 的上下文，仍被角色门拒绝
        fake_worker = CallerContext(
            actor_id="dev-1", role="worker",
            capabilities=frozenset({REVIEW_SUBMIT}), entry="forged", synthetic=True,
        )
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self.env.events.ingest(
            ev("e1", 1, "task.accepted", task, attempt, {"hostReceipt": "ack"}), context=self.env.source
        )
        self.env.events.ingest(
            ev("e2", 2, "task.delivered", task, attempt, DELIVERY_BODY), context=self.env.source
        )
        delivery = self.env.store.get_task(task["taskId"])["current_delivery_id"]
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(
                task["taskId"], {"decision": "approved", "deliveryId": delivery},
                context=fake_worker,
            )
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_submit_requires_matching_agent(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        stranger = ctx_worker("dev-999")
        with self.assertRaises(ContractError) as ctx:
            self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=stranger)
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_delivery_outside_scope_is_rejected(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self.env.events.ingest(
            ev("e1", 1, "task.accepted", task, attempt, {"hostReceipt": "ack"}), context=self.env.source
        )
        escaping = dict(DELIVERY_BODY, changedFiles=["outside/secret.md"])
        with self.assertRaises(ContractError) as ctx:
            self.env.events.ingest(ev("e2", 2, "task.delivered", task, attempt, escaping),
                                   context=self.env.source)
        self.assertEqual(ctx.exception.code, "path_escape")
        self.assertEqual(self.env.store.count_deliveries(task["taskId"]), 0)

    def test_blocked_attempt_can_be_resumed(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self.env.events.ingest(
            ev("e1", 1, "task.blocked", task, attempt,
               {"reason": "等凭据", "owner": "cox", "resumeWhen": "给凭据"}), context=self.env.source
        )
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "blocked")
        # blocked 状态允许再开一轮（人工恢复），不是自动循环
        resumed = self.env.tasks.create_attempt(task["taskId"], note="恢复", context=self.env.worker)
        self.assertEqual(resumed["ordinal"], 2)

    def test_validate_create_task_structure(self):
        errors = validate_create_task({"projectId": "x"})
        self.assertIn("missing", errors)
        self.assertEqual(validate_create_task(create_task_payload()), {})


if __name__ == "__main__":
    unittest.main()
