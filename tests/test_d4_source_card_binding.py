"""D4-NEXT-01C-R1 定向测试：正式报告与来源任务卡强绑定。

覆盖返工卡 §7 的 S01–S13（本地 SYNTHETIC，不代表真实宿主/成员数据库/MVP 通过）。
runtime 落点：本返工卡唯一输出根
`.conductor-local/d4-real-chain-validation/report-source-binding-r1/20261008-devwkb1-auto-001/runtime`。
全部数据为本地 synthetic，不调用真实 Agent、不改宿主配置、不写业务数据。

要点：
- 建卡时冻结受控来源（Markdown 整体校验；JSON 可选 sourceCard 引用并核对内容）；
- 正式导入在写事务内查库核对来源，缺失/编号/版本/畸形一律 source_card_mismatch；
- `--source-card-id/--source-card-revision` 只是成对一致性断言，不能建立缺失来源；
- 冻结来源不跟随原文件漂移；旧库只读不迁移；worker 不能自绑定来源。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contracts.document_io import build_task_card_source_reference, inspect_document
from contracts.schemas import ContractError
from scripts import d4ctl
from service.context import owner_context, reviewer_context, worker_context
from service.report_import import import_report
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

# runtime 落点：D4_VAL_RUNTIME_DIR 可覆盖；默认落本返工卡唯一输出根（不硬编码 R1 证据）。
EVIDENCE_ROOT = (
    os.environ.get("D4_VAL_RUNTIME_DIR")
    or "/Users/demo/.hermes/skills/conductor/.conductor-local/d4-real-chain-validation/"
       "report-source-binding-r2/20261008-devwkb1-auto-002"
)
CARD_ID = "D4-NEXT-01C-R1-SRC"
CARD_REVISION = 1


def _scalar_text(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % text


def _dump(obj, indent=0):
    """受控 YAML 子集文本生成（与生产解析同口径，测试仅做输入构造）。"""
    lines = []
    pad = " " * indent
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, (dict, list)) and value:
                lines.append("%s%s:" % (pad, key))
                lines.append(_dump(value, indent + 2))
            elif isinstance(value, (dict, list)):
                lines.append("%s%s: %s" % (pad, key, "{}" if isinstance(value, dict) else "[]"))
            else:
                lines.append("%s%s: %s" % (pad, key, _scalar_text(value)))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                block = _dump(item, indent + 2)
                first, _, rest = block.partition("\n")
                lines.append("%s- %s" % (pad, first.strip()))
                if rest:
                    lines.append(rest)
            else:
                lines.append("%s- %s" % (pad, _scalar_text(item)))
    return "\n".join(lines)


class SourceCardBindingTestCase(unittest.TestCase):
    """隔离脚手架：每例一个临时库，全部落在本返工卡 runtime 内。"""

    def setUp(self):
        runtime = Path(EVIDENCE_ROOT) / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime))
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "d4.sqlite3"
        self.allowed = str(self.root)
        self.workspace = self.root / "workspace"
        (self.workspace / "evidence").mkdir(parents=True)
        (self.workspace / "report.md").write_text("# 交付报告\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    # -- 受控入口封装 -----------------------------------------------------
    def cli(self, argv, synthetic=True, transport=None):
        return d4ctl.run(
            ["--db", str(self.db), "--allowed-root", self.allowed, "--agent", "dev-1"] + argv,
            transport=transport, synthetic=synthetic,
        )

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def card_frontmatter(self, **over):
        base = {
            "schemaVersion": "task-card-board.v1",
            "cardId": CARD_ID,
            "cardRevision": CARD_REVISION,
            "projectId": "conductor",
            "title": "验证来源任务卡强绑定",
            "goal": "证明正式报告必须匹配冻结来源。",
            "workspace": str(self.workspace),
            "permissions": {
                "writeScope": ["evidence/"],
                "allowNetwork": False,
                "allowBusinessWrite": False,
                "allowGit": False,
                "allowPublish": False,
            },
            "issuer": {"alias": "cox", "role": "sales-architect", "host": "Codex",
                       "agentId": "cox"},
            "executor": {"alias": "dev-1", "role": "skill-worker", "host": "QClaw",
                         "agentId": "dev-1", "environmentId": "qclaw-local"},
            "reviewer": {"alias": "cox", "role": "sales-architect"},
            "sessionMode": "new",
            "boardSummary": {"title": "验证来源强绑定", "stage": "来源硬核对已经写完",
                             "problem": "真实成员登记还没接通", "nextStep": "等负责人复核"},
        }
        base.update(over)
        return base

    def write_card(self, name="src-card.md", **over):
        path = self.root / name
        path.write_text("---\n%s\n---\n\n正文：来源任务卡夹具。\n"
                        % _dump(self.card_frontmatter(**over)), encoding="utf-8")
        return path

    def task_payload(self, card_path, **over):
        payload = {
            "projectId": "conductor",
            "title": "验证来源任务卡强绑定",
            "agentId": "dev-1",
            "sourceId": "qclaw-gateway",
            "workspace": str(self.workspace),
            "goal": "证明正式报告必须匹配冻结来源。",
            "permissions": {
                "writeScope": ["evidence/"],
                "allowNetwork": False,
                "allowBusinessWrite": False,
                "allowGit": False,
                "allowPublish": False,
            },
            "sourceCard": {"path": str(card_path), "cardId": CARD_ID,
                           "cardRevision": CARD_REVISION},
        }
        payload.update(over)
        return payload

    def create_task_md(self, **over):
        card = self.write_card(**over)
        code, result = self.cli(["task", "create", "--file", str(card)])
        self.assertEqual(code, 0, result)
        return result["data"]["taskId"]

    def create_task_json(self, card_path=None, **over):
        card_path = card_path or self.write_card()
        path = self.write_json("task.json", self.task_payload(card_path, **over))
        code, result = self.cli(["task", "create", "--file", path])
        return code, result

    def submit_attempt(self, task_id, attempt_id=None):
        try:
            from test_d4ctl import ResponsiveTransport
        except ImportError:  # pragma: no cover
            self.skipTest("缺少 test_d4ctl 脚手架")
        argv = ["attempt", "submit", "--task", task_id]
        if attempt_id:
            argv += ["--attempt", attempt_id]
        code, result = self.cli(argv, transport=ResponsiveTransport())
        self.assertEqual(code, 0, result)
        return result["data"]["attemptId"]

    def deliver(self, task_id, attempt_id, sequence=1):
        payload = {
            "eventId": "evt-%s-%02d" % (attempt_id, sequence),
            "projectId": "conductor", "taskId": task_id, "attemptId": attempt_id,
            "sourceId": "qclaw-gateway", "agentId": "dev-1", "sequence": sequence,
            "type": "task.delivered", "occurredAt": "2026-10-08T01:00:00+08:00",
            "payload": {
                "summary": "来源强绑定切片", "reportPath": str(self.workspace / "report.md"),
                "changedFiles": ["evidence/report.md"],
                "verification": ["定向测试已跑"], "risks": [], "unfinished": [],
            },
        }
        code, result = self.cli(["event", "ingest", "--file",
                                 self.write_json("deliver-%02d.json" % sequence, payload)])
        self.assertEqual(code, 0, result)
        return result["data"]["data"]["deliveryId"]

    def bootstrap(self):
        task_id = self.create_task_md()
        attempt_id = self.submit_attempt(task_id)
        delivery_id = self.deliver(task_id, attempt_id)
        return task_id, attempt_id, delivery_id

    def report_frontmatter(self, task_id, attempt_id, card_path, **over):
        base = {
            "schemaVersion": "report-board.v1",
            "reportId": "D4-NEXT-01C-R1-QA",
            "reportRevision": 1,
            "reportType": "qa",
            "projectId": "conductor",
            "taskId": task_id,
            "attemptId": attempt_id,
            "sourceCard": {"path": str(card_path), "cardId": CARD_ID,
                           "cardRevision": CARD_REVISION},
            "requestedBy": {"alias": "cox", "role": "sales-architect", "host": "Codex",
                            "agentId": "cox"},
            "executedBy": {"alias": "qa-1", "role": "qa-reporter", "host": "OpenClaw",
                           "agentId": "qa-1", "environmentId": "qclaw-local"},
            "adapter": "unverified", "sessionId": "unverified", "hostRunId": "unverified",
            "reviewedBy": {"alias": "cox", "role": "sales-architect"},
            "businessSummary": {"summary": "来源强绑定已经落地", "owner": "cox",
                                "unfinished": [], "impact": [],
                                "nextStep": "等负责人复核"},
            "evidence": [{"kind": "test", "detail": "本返工卡定向测试", "level": "reported"}],
            "boardSummary": {"stage": "来源强绑定已经落地", "result": "导入必须匹配冻结来源",
                             "problem": "真实成员登记还没接通", "nextStep": "等负责人复核"},
        }
        base.update(over)
        return base

    def write_report(self, name, frontmatter, body="正文：不承载可信状态。"):
        path = self.root / name
        path.write_text("---\n%s\n---\n\n%s\n" % (_dump(frontmatter), body), encoding="utf-8")
        return str(path)

    def report_path(self, task_id, attempt_id, card_path=None, name="report-board.md", **over):
        card_path = card_path or self.write_card()
        return self.write_report(name, self.report_frontmatter(task_id, attempt_id, card_path,
                                                              **over))

    def import_report(self, path, task_id, attempt_id, delivery_id, extra=()):
        return self.cli(["report", "import", "--task", task_id, "--attempt", attempt_id,
                         "--delivery", delivery_id, "--file", path] + list(extra))

    def counts(self, task_id):
        store = SqliteStore(str(self.db))
        try:
            return {"reports": len(store.list_reports(task_id)),
                    "tasks": len(store.list_tasks())}
        finally:
            store.close()

    def source_row(self, task_id):
        store = SqliteStore(str(self.db))
        try:
            return store.get_source_card(task_id)
        finally:
            store.close()


class FreezeOnCreateTests(SourceCardBindingTestCase):
    def test_s01_markdown_create_freezes_source_and_survives_reopen(self):
        task_id = self.create_task_md()
        row = self.source_row(task_id)
        self.assertIsNotNone(row)
        self.assertEqual(row["card_id"], CARD_ID)
        self.assertEqual(int(row["card_revision"]), CARD_REVISION)
        self.assertEqual(row["project_id"], "conductor")
        self.assertTrue(row["source_sha256"].startswith("sha256:"))
        # 实际文件 SHA 与库内登记一致（摘要由实际读取计算，不信任卡内自报）
        from contracts.document_io import bytes_digest
        raw = (self.root / "src-card.md").read_bytes()
        self.assertEqual(row["source_sha256"], bytes_digest(raw))
        # 重开 SQLite 仍在
        self.assertEqual(self.source_row(task_id)["card_id"], CARD_ID)

    def test_s01_json_create_with_source_card_registers_source(self):
        code, result = self.create_task_json()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceCardRegistered"])
        row = self.source_row(result["data"]["taskId"])
        self.assertEqual(row["card_id"], CARD_ID)

    def test_s10_legacy_json_without_source_card_still_creates_task(self):
        # 旧 JSON 无 sourceCard：任务可建，但来源未登记（不能新导入正式报告）
        payload = self.task_payload(self.write_card())
        payload.pop("sourceCard")
        path = self.write_json("task.json", payload)
        code, result = self.cli(["task", "create", "--file", path])
        self.assertEqual(code, 0, result)
        self.assertFalse(result["data"]["sourceCardRegistered"])
        self.assertIsNone(self.source_row(result["data"]["taskId"]))

    def test_s04_non_int_card_revision_on_source_is_rejected(self):
        card = self.write_card(name="bad.md", cardRevision="1")  # 字符串冒充
        code, result = self.create_task_json(card_path=card)
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("source_card_mismatch", "identity_missing",
                                                "document_invalid"))

    def test_s07_json_source_mismatch_rejects_before_create(self):
        card = self.write_card()  # 卡 workspace = self.workspace
        other_ws = self.root / "other-ws"
        other_ws.mkdir()
        code, result = self.create_task_json(card_path=card, workspace=str(other_ws))
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        # 未创建任务、未登记来源
        store = SqliteStore(str(self.db))
        try:
            self.assertEqual(len(store.list_tasks()), 0)
        finally:
            store.close()

    def test_s07_json_source_card_id_mismatch_rejected(self):
        card = self.write_card()
        payload = self.task_payload(card, sourceCard={"path": str(card),
                                                      "cardId": "OTHER", "cardRevision": 1})
        code, result = self.cli(["task", "create", "--file",
                                 self.write_json("task.json", payload)])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")

    def test_s12_symlink_source_card_is_rejected(self):
        real = self.write_card()
        link = self.root / "src-link.md"
        os.symlink(real, link)
        code, result = self.cli(["task", "create", "--file", str(link)])
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("path_symlink_rejected", "document_invalid",
                                                "path_escape"))


class ReportImportChecksTests(SourceCardBindingTestCase):
    def test_s02_legal_import_without_params_verifies_source(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        code, result = self.import_report(self.report_path(task_id, attempt_id),
                                          task_id, attempt_id, delivery_id)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceFile"]["sourceCardVerified"])
        self.assertEqual(result["data"]["sourceCard"]["cardId"], CARD_ID)

    def test_s03_wrong_card_id_without_params_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        card = self.write_card()
        fm = self.report_frontmatter(task_id, attempt_id, card)
        fm["sourceCard"] = dict(fm["sourceCard"], cardId="WRONG-CARD")
        path = self.write_report("wrong-id.md", fm)
        code, result = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_s04_wrong_revision_without_params_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        card = self.write_card()
        fm = self.report_frontmatter(task_id, attempt_id, card)
        fm["sourceCard"] = dict(fm["sourceCard"], cardRevision=99)
        path = self.write_report("wrong-rev.md", fm)
        code, result = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_s04_bool_revision_is_not_accepted(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        card = self.write_card()
        fm = self.report_frontmatter(task_id, attempt_id, card)
        fm["sourceCard"] = dict(fm["sourceCard"], cardRevision=True)
        path = self.write_report("bool-rev.md", fm)
        code, result = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_s05_task_without_source_cannot_import_even_with_params(self):
        # 旧 JSON 建卡（无来源）+ 正常交付
        payload = self.task_payload(self.write_card())
        payload.pop("sourceCard")
        code, created = self.cli(["task", "create", "--file",
                                  self.write_json("task.json", payload)])
        self.assertEqual(code, 0, created)
        task_id = created["data"]["taskId"]
        attempt_id = self.submit_attempt(task_id)
        delivery_id = self.deliver(task_id, attempt_id)
        path = self.report_path(task_id, attempt_id)
        # 不传参数 -> 拒绝
        code, result = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        # 传"正确"参数也不能临时补绑
        code, result = self.import_report(path, task_id, attempt_id, delivery_id,
                                          extra=["--source-card-id", CARD_ID,
                                                 "--source-card-revision", str(CARD_REVISION)])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_s06_params_conflicting_with_db_are_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report(path, task_id, attempt_id, delivery_id,
                                          extra=["--source-card-id", "OTHER-CARD",
                                                 "--source-card-revision", "9"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_s06_half_given_params_is_usage_error(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report(path, task_id, attempt_id, delivery_id,
                                          extra=["--source-card-id", CARD_ID])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_s09_replay_and_wrong_source_not_replayed(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        first = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(first[0], 0, first)
        second_code, second = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(second_code, 0, second)
        self.assertTrue(second["data"]["replayed"])
        self.assertEqual(self.counts(task_id)["reports"], 1)

    def test_s11_frozen_source_does_not_follow_file_drift(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        # 篡改来源卡原文（内容漂移），库内冻结事实不变 -> 仍按库核对
        (self.root / "src-card.md").write_text("---\ncardId: \"TAMPERED\"\ncardRevision: 9\n---\n",
                                               encoding="utf-8")
        row = self.source_row(task_id)
        self.assertEqual(row["card_id"], CARD_ID)
        code, result = self.import_report(self.report_path(task_id, attempt_id),
                                          task_id, attempt_id, delivery_id)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceFile"]["sourceCardVerified"])

    def test_s11_source_row_not_overwritable(self):
        # 来源唯一性：同一 task_id 不能二次写入覆盖（两个独立连接）
        task_id = self.create_task_md()
        writer = SqliteStore(str(self.db))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                writer.insert_source_card({
                    "task_id": task_id, "project_id": "conductor", "card_id": "EVIL",
                    "card_revision": 2, "source_path": "/tmp/x.md",
                    "source_sha256": "sha256:deadbeef", "canonical_digest": "sha256:x",
                    "registered_json": "{}", "created_at": "2026-10-08T00:00:00+08:00",
                })
        finally:
            writer.close()
        self.assertEqual(self.source_row(task_id)["card_id"], CARD_ID)


class LegacyAndBoundaryTests(SourceCardBindingTestCase):
    def test_s10_read_only_legacy_db_has_no_source_table(self):
        task_id = self.create_task_md()
        writer = SqliteStore(str(self.db))
        writer.conn.execute("DROP TABLE task_source_cards")
        writer.close()
        reader = SqliteStore(str(self.db), read_only=True)
        try:
            self.assertFalse(reader.has_source_cards_table())
            self.assertIsNone(reader.get_source_card(task_id))
        finally:
            reader.close()
        # 写模式 additive 重建，不迁移旧数据
        rewriter = SqliteStore(str(self.db))
        try:
            self.assertTrue(rewriter.has_source_cards_table())
        finally:
            rewriter.close()

    def test_s12_worker_context_cannot_self_bind_source(self):
        # R3：用**实际 worker_context** 调用服务层 create_task。
        # worker 只有 TASK_EXECUTE，没有 TASK_CREATE；即便把伪造的受控来源对象
        # 也一并传入，也必须在授权阶段 permission_denied 且零写，而不是
        # None + 笼统 Exception 的弱断言。
        store = SqliteStore(str(self.db))
        try:
            service = TaskService(store, allowed_roots=[self.allowed])
            payload = self.task_payload(self.write_card())
            payload.pop("sourceCard")
            context = worker_context(actor_id="dev-1", entry="test-worker-cli", synthetic=True)
            with self.assertRaises(ContractError) as cm:
                service.create_task(
                    payload, context,
                    {"cardId": CARD_ID, "cardRevision": CARD_REVISION,
                     "projectId": "conductor", "path": str(self.root / "src-card.md"),
                     "sha256": "sha256:" + "0" * 64, "canonicalDigest": "sha256:x"},
                )
            self.assertEqual(cm.exception.code, "permission_denied")
            self.assertEqual(len(store.list_tasks()), 0)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM task_source_cards").fetchone()[0], 0)
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0], 0)
        finally:
            store.close()

    def test_s13_document_inspect_still_read_only(self):
        card = self.write_card()
        code, result = self.cli(["document", "inspect", "--file", str(card)])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["documentKind"], "task-card")
        self.assertTrue(result["data"]["sideEffects"]["databaseOpened"] is False)
        self.assertIsNone(self.source_row("nonexistent-task"))

    def test_s13_import_without_controlled_context_fails_closed(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        store = SqliteStore(str(self.db))
        try:
            self.assertRaises(Exception, import_report, store, task_id, attempt_id,
                              delivery_id, path, None)
            self.assertEqual(len(store.list_reports(task_id)), 0)
        finally:
            store.close()


class R3CompletionTests(SourceCardBindingTestCase):
    """D4-NEXT-01C-R3：空来源区分 + 有效默认化边界 + S08/S10/S11 补全。

    全部为本地 SYNTHETIC；每例独立临时库，落在本返工卡 runtime 内。
    """

    def _task_count(self):
        store = SqliteStore(str(self.db))
        try:
            return len(store.list_tasks())
        finally:
            store.close()

    def _activity_count(self):
        store = SqliteStore(str(self.db))
        try:
            return store.conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0]
        finally:
            store.close()

    def _source_card_count(self):
        store = SqliteStore(str(self.db))
        try:
            return store.conn.execute("SELECT COUNT(*) FROM task_source_cards").fetchone()[0]
        finally:
            store.close()

    # ---- 空来源：缺 key 保持 legacy；显式 null / 错误引用拒绝且零写 ----
    def test_s14_explicit_null_source_card_rejected_zero_write(self):
        card = self.write_card()
        code, result = self.create_task_json(card_path=card, sourceCard=None)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self._task_count(), 0)
        self.assertEqual(self._source_card_count(), 0)
        self.assertEqual(self._activity_count(), 0)

    def test_s14_missing_key_is_legacy_without_source(self):
        # 与显式 null 对照：缺 key 仍兼容建卡，但不登记来源（不能新导入正式报告）。
        payload = self.task_payload(self.write_card())
        payload.pop("sourceCard")
        code, result = self.cli(["task", "create", "--file",
                                 self.write_json("legacy.json", payload)])
        self.assertEqual(code, 0, result)
        self.assertFalse(result["data"]["sourceCardRegistered"])
        self.assertIsNone(self.source_row(result["data"]["taskId"]))

    def test_s14_reference_missing_field_rejected(self):
        cases = {
            "path": {"cardId": CARD_ID, "cardRevision": CARD_REVISION},
            "cardId": {"path": "x", "cardRevision": CARD_REVISION},
            "cardRevision": {"path": "x", "cardId": CARD_ID},
        }
        for missing, ref in cases.items():
            with self.subTest(missing=missing):
                card = self.write_card()
                code, result = self.create_task_json(card_path=card, sourceCard=dict(ref))
                self.assertEqual(code, 1, result)
                self.assertEqual(result["error"]["code"], "source_card_mismatch")
                self.assertEqual(self._task_count(), 0)

    def test_s14_reference_bad_types_rejected(self):
        for label, revision in {"bool": True, "zero": 0, "negative": -1, "string": "1"}.items():
            with self.subTest(cardRevision=label):
                card = self.write_card()
                ref = {"path": str(card), "cardId": CARD_ID, "cardRevision": revision}
                code, result = self.create_task_json(card_path=card, sourceCard=ref)
                self.assertEqual(code, 1, result)
                self.assertEqual(result["error"]["code"], "source_card_mismatch")
                self.assertEqual(self._task_count(), 0)
        card = self.write_card()
        for label, ref in {
            "empty_cardId": {"path": str(card), "cardId": "", "cardRevision": 1},
            "null_path": {"path": None, "cardId": CARD_ID, "cardRevision": 1},
        }.items():
            with self.subTest(field=label):
                code, result = self.create_task_json(card_path=card, sourceCard=ref)
                self.assertEqual(code, 1, result)
                self.assertEqual(result["error"]["code"], "source_card_mismatch")

    def test_s14_reference_not_object_rejected(self):
        card = self.write_card()
        code, result = self.create_task_json(card_path=card, sourceCard="not-an-object")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self._task_count(), 0)

    # ---- 有效默认化边界（§3）：合法同默认值正例 / 不放宽严格卡 ----
    def test_s15_both_omit_acceptance_and_limits_is_legit(self):
        code, result = self.create_task_json(card_path=self.write_card())
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceCardRegistered"])

    def test_s15_null_acceptance_and_limits_normalize_like_omitted(self):
        card = self.write_card()
        code, result = self.create_task_json(card_path=card, acceptance=None, limits=None)
        self.assertEqual(code, 0, result)

    def test_s15_json_adds_limits_beyond_card_default_rejected(self):
        card = self.write_card()  # 卡无 limits -> 有效默认 maxAttempts=2
        code, result = self.create_task_json(card_path=card, limits={"maxAttempts": 1})
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self._task_count(), 0)

    def test_s15_json_omits_acceptance_card_requires_it_rejected(self):
        card = self.write_card(acceptance=["必须完成 A"])
        code, result = self.create_task_json(card_path=card)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")

    def test_s15_acceptance_type_error_not_silently_widened(self):
        card = self.write_card(acceptance=["必须完成 A"])
        code, result = self.create_task_json(card_path=card, acceptance="必须完成 A")
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")

    # ---- S08：来源登记写入失败 -> task/source/activity 三表整体回滚 ----
    def test_s08_source_insert_failure_rolls_back_all_three_tables(self):
        card = self.write_card()
        path = self.write_json("s08.json", self.task_payload(card))
        with mock.patch.object(
            SqliteStore, "insert_source_card",
            side_effect=sqlite3.OperationalError("simulated source insert failure"),
        ):
            code, result = self.cli(["task", "create", "--file", path])
        self.assertEqual(code, 1, result)
        self.assertEqual(self._task_count(), 0)
        self.assertEqual(self._source_card_count(), 0)
        self.assertEqual(self._activity_count(), 0)

    # ---- S10：历史已入账 sourceCardVerified=false 读取保留 false，不重新验证 ----
    def test_s10_existing_unverified_report_not_reverified_on_replay(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        first = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(first[0], 0, first)
        self.assertTrue(first[1]["data"]["sourceFile"]["sourceCardVerified"])
        # 模拟修复前遗留：历史已入账记录 sourceCardVerified=false。
        store = SqliteStore(str(self.db))
        try:
            store.conn.execute(
                "UPDATE reports SET source_file_json = ?",
                (json.dumps({"sha256": "sha256:" + "0" * 64, "bytes": 1,
                             "sourceCardVerified": False}),),
            )
        finally:
            store.close()
        replay_code, replay = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(replay_code, 0, replay)
        self.assertTrue(replay["data"]["replayed"])
        # 读取/重放保留历史 false，不因再次核对而改写成 true。
        self.assertFalse(replay["data"]["sourceFile"]["sourceCardVerified"])

    def test_s10_registered_source_project_drift_rejected_on_import(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        store = SqliteStore(str(self.db))
        try:
            store.conn.execute(
                "UPDATE task_source_cards SET project_id = ? WHERE task_id = ?",
                ("other-project", task_id),
            )
        finally:
            store.close()
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    # ---- S11：旧/迟到交付对当前任务拒绝（不只是来源原文漂移） ----
    def test_s11_stale_delivery_rejected_for_current_task(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        newer = "del-%s-v2" % task_id
        # 构造一个更新的当前交付，使 bootstrap 的交付成为"旧交付"。
        store = SqliteStore(str(self.db))
        try:
            store.insert_delivery({
                "delivery_id": newer, "task_id": task_id, "attempt_id": attempt_id,
                "summary": "更新的交付", "changed_files_json": "[]", "verification_json": "[]",
                "risks_json": "[]", "report_path": None, "unfinished_json": "[]",
                "source_delivery_id": None, "created_at": "2026-10-08T02:00:00+08:00",
            })
            store.update_task(task_id, current_delivery_id=newer,
                              updated_at="2026-10-08T02:00:00+08:00")
        finally:
            store.close()
        path = self.report_path(task_id, attempt_id)
        # 用旧交付导入 -> 拒绝（旧/迟到交付不能绑定当前任务）
        code, result = self.import_report(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "review_version_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)


class R4SourceDigestBoundaryTests(SourceCardBindingTestCase):
    """D4-NEXT-01C-R4：来源摘要整串匹配窄返工（服务层表驱动）。

    回归 cox 独立审核发现的缺陷：`_SHA256_RE` 用 `$` + `re.match`，Python 的 `$`
    允许在结尾换行前匹配，导致 `sha256:<64hex>\\n`（尾 LF）被误接受。R4 改为 `\\Z`
    整串锚定后，正例仍放行、尾 LF/CR/空白/垃圾/短值/超长/非字符串全部拒绝且三表零写。

    全部为本地 SYNTHETIC；每例独立临时库，落在本返工卡 runtime 内；
    直接调服务层以覆盖受控入口读取后的摘要形态校验（d4ctl 路径会重算文件摘要）。
    """

    def _counts(self):
        store = SqliteStore(str(self.db))
        try:
            return {
                "tasks": len(store.list_tasks()),
                "task_source_cards": store.conn.execute(
                    "SELECT COUNT(*) FROM task_source_cards").fetchone()[0],
                "activity": store.conn.execute(
                    "SELECT COUNT(*) FROM activity").fetchone()[0],
            }
        finally:
            store.close()

    def _service_store(self):
        # 服务层前置校验需要来源与成员已登记，才能走到 _validate_source_card。
        store = SqliteStore(str(self.db))
        store.register_source("qclaw-gateway", "test", {}, "test")
        store.register_agent("dev-1", "test", "test", "test")
        return store

    def _source(self, card, **over):
        source = {
            "cardId": CARD_ID, "cardRevision": CARD_REVISION, "projectId": "conductor",
            "path": str(card), "sha256": "sha256:" + "a" * 64,
            "canonicalDigest": "controlled-test",
        }
        source.update(over)
        return source

    def _payload_without_source_card(self, card):
        payload = self.task_payload(card)
        payload.pop("sourceCard")
        return payload

    def test_r4_legal_sha256_accepted_and_identity_from_context(self):
        # 合法 sha256:64 小写 hex -> 接受、来源登记；登记身份只取受控 CallerContext，
        # payload 自报身份（干扰项）必须被忽略。
        card = self.write_card()
        store = self._service_store()
        try:
            service = TaskService(store, allowed_roots=[self.allowed])
            payload = self._payload_without_source_card(card)
            payload["registeredBy"] = {"actorId": "attacker", "role": "owner"}
            payload["actorId"] = "attacker"
            context = owner_context(
                actor_id="cox-controlled", entry="test-controlled-entry", synthetic=True)
            result = service.create_task(payload, context, self._source(card))
            saved = store.get_source_card(result["taskId"])
            self.assertIsNotNone(saved)
            self.assertEqual(saved["source_sha256"], "sha256:" + "a" * 64)
            registered = json.loads(saved["registered_by_json"])
            self.assertEqual(registered, context.to_dict())
            self.assertEqual(registered["actorId"], "cox-controlled")
            self.assertEqual(self._counts(),
                             {"tasks": 1, "task_source_cards": 1, "activity": 1})
        finally:
            store.close()

    def test_r4_digest_shape_boundaries_rejected_zero_write(self):
        # 表驱动负例：尾 LF/CR/空白/垃圾/短值/超长/大写/前缀缺失/非字符串 -> 拒绝且零写。
        cases = {
            "tail_LF": "sha256:" + "a" * 64 + "\n",
            "tail_CR": "sha256:" + "a" * 64 + "\r",
            "tail_space": "sha256:" + "a" * 64 + " ",
            "tail_tab": "sha256:" + "a" * 64 + "\t",
            "leading_space": " sha256:" + "a" * 64,
            "embedded_LF": "sha256:" + "a" * 32 + "\n" + "a" * 32,
            "garbage": "sha256:" + "g" * 64,
            "short": "sha256:aa",
            "too_long": "sha256:" + "a" * 65,
            "uppercase": "sha256:" + "A" * 64,
            "no_prefix": "a" * 64,
            "empty": "",
            "none": None,
            "int": 123,
            "list": ["sha256:" + "a" * 64],
            "dict": {"sha256": "sha256:" + "a" * 64},
        }
        for label, value in cases.items():
            with self.subTest(sha256=label):
                card = self.write_card()
                store = self._service_store()
                try:
                    service = TaskService(store, allowed_roots=[self.allowed])
                    context = owner_context(
                        actor_id="cox-controlled", entry="test-controlled-entry",
                        synthetic=True)
                    with self.assertRaises(ContractError) as cm:
                        service.create_task(self._payload_without_source_card(card), context,
                                            self._source(card, sha256=value))
                    self.assertEqual(cm.exception.code, "source_card_mismatch")
                    self.assertEqual(self._counts(),
                                     {"tasks": 0, "task_source_cards": 0, "activity": 0})
                finally:
                    store.close()

    def test_r4_version_bool_string_zero_rejected_zero_write(self):
        # 版本严格性保留 + 服务层补断言：bool/string/zero/负数 -> 拒绝且零写。
        for label, revision in {"bool": True, "string": "1", "zero": 0,
                                "negative": -1}.items():
            with self.subTest(cardRevision=label):
                card = self.write_card()
                store = self._service_store()
                try:
                    service = TaskService(store, allowed_roots=[self.allowed])
                    context = owner_context(
                        actor_id="cox-controlled", entry="test-controlled-entry",
                        synthetic=True)
                    with self.assertRaises(ContractError) as cm:
                        service.create_task(self._payload_without_source_card(card), context,
                                            self._source(card, cardRevision=revision))
                    self.assertEqual(cm.exception.code, "source_card_mismatch")
                    self.assertEqual(self._counts(),
                                     {"tasks": 0, "task_source_cards": 0, "activity": 0})
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
