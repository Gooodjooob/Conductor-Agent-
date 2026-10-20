"""D4-NEXT-01C 定向测试：报告导入、摘要记账与看板投影。

runtime 落点：D4-NEXT-01C-R1 唯一证据目录
`.conductor-local/d4-real-chain-validation/report-source-binding-r1/20261008-devwkb1-auto-001/runtime`。
全部数据为 synthetic（本地 silent synthetic 链路），不代表真实宿主已接通。

覆盖任务卡 §7：合法导入、重复 replay、同版本内容冲突、旧交付拒绝、缺字段/
身份/来源/schema 交叉/路径越界 fail-closed、正文伪造“已通过”、SQLite 重开恢复、
旧库缺 reports 表、看板摘要通俗性与无内部字段泄漏。

R1 起：建卡经 Markdown 任务卡冻结受控来源；正式导入必须与库内登记来源一致。
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from contracts.board_summary import detect_tech_leak
from projections.read_models import ReadModels
from scripts import d4ctl
from scripts.d4_board import board_data
from service.report_import import import_report
from storage.sqlite_store import SqliteStore

EVIDENCE_ROOT = (
    "/Users/demo/.hermes/skills/conductor/.conductor-local/d4-real-chain-validation/"
    "report-source-binding-r1/20261008-devwkb1-auto-001"
)
BOARD_JS = Path(__file__).resolve().parents[1] / "board" / "board.js"
# D4 053：board.js 只做板面编排，技术详情渲染委托给该视图组件。
PROJECT_VIEW_JS = Path(__file__).resolve().parents[1] / "board" / "project-view.js"


# ---------------------------------------------------------------------------
# 受限 YAML 文本生成（与 01B 同口径；生产解析只走 contracts.document_io）
# ---------------------------------------------------------------------------

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


def report_frontmatter(task_id, attempt_id, card_path, **over):
    base = {
        "schemaVersion": "report-board.v1",
        "reportId": "D4-NEXT-01C-TEST-QA",
        "reportRevision": 1,
        "reportType": "qa",
        "projectId": "conductor",
        "taskId": task_id,
        "attemptId": attempt_id,
        "sourceCard": {
            "path": card_path,
            "cardId": "D4-NEXT-01C-TEST-SRC",
            "cardRevision": 1,
        },
        "requestedBy": {
            "alias": "cox", "role": "sales-architect", "host": "Codex", "agentId": "cox",
        },
        "executedBy": {
            "alias": "qa-1", "role": "qa-reporter", "host": "OpenClaw", "agentId": "qa-1",
            "environmentId": "qclaw-local",
        },
        "adapter": "unverified",
        "sessionId": "unverified",
        "hostRunId": "unverified",
        "reviewedBy": {"alias": "cox", "role": "sales-architect"},
        "businessSummary": {
            "summary": "报告导入已经接到本地账本",
            "owner": "cox",
            "unfinished": [],
            "impact": [],
            "nextStep": "等负责人复核本卡结果",
        },
        "evidence": [{"kind": "test", "detail": "本卡定向测试", "level": "reported"}],
        "boardSummary": {
            "stage": "报告已经接进本地账本",
            "result": "导入、记账和看板展示都能跑通",
            "problem": "真实成员登记还没接通",
            "nextStep": "等负责人复核本卡结果",
        },
    }
    base.update(over)
    return base


_CARD_DIR = EVIDENCE_ROOT


class D4ReportImportTestCase(unittest.TestCase):
    """隔离脚手架：每例一个临时库，全部落在本卡证据目录内。"""

    def setUp(self):
        EVIDENCE_ROOT_PATH = Path(EVIDENCE_ROOT) / "runtime"
        EVIDENCE_ROOT_PATH.mkdir(parents=True, exist_ok=True)
        (EVIDENCE_ROOT_PATH / "card.md").write_text("# source card fixture\n", encoding="utf-8")
        self.tmp = tempfile.TemporaryDirectory(dir=str(EVIDENCE_ROOT_PATH))
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

    def write_report(self, name, frontmatter, body="正文：这里不承载可信状态。"):
        path = self.root / name
        path.write_text("---\n%s\n---\n\n%s\n" % (_dump(frontmatter), body), encoding="utf-8")
        return str(path)

    def create_task(self):
        """D4-NEXT-01C-R1：经 Markdown 任务卡建卡，来源随任务原子冻结登记。"""
        code, result = self.cli(["task", "create", "--file", str(self.task_card_path())])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceCardRegistered"])
        return result["data"]["taskId"]

    def task_card_frontmatter(self, **over):
        base = {
            "schemaVersion": "task-card-board.v1",
            "cardId": "D4-NEXT-01C-TEST-SRC",
            "cardRevision": 1,
            "projectId": "conductor",
            "title": "验证报告导入与来源强绑定",
            "goal": "证明正式报告可以入账并投影到看板。",
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
            "boardSummary": {"title": "验证报告导入", "stage": "来源强绑定已经写完",
                             "problem": "真实成员登记还没接通", "nextStep": "等负责人复核"},
        }
        base.update(over)
        return base

    def task_card_path(self, name="src-card.md", **over):
        path = self.root / name
        path.write_text("---\n%s\n---\n\n正文：来源任务卡夹具。\n" % _dump(
            self.task_card_frontmatter(**over)), encoding="utf-8")
        return path

    def submit_attempt(self, task_id, attempt_id=None):
        try:
            from test_d4ctl import ResponsiveTransport
        except ImportError:  # pragma: no cover - 依赖相邻测试模块存在
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
            "projectId": "conductor",
            "taskId": task_id,
            "attemptId": attempt_id,
            "sourceId": "qclaw-gateway",
            "agentId": "dev-1",
            "sequence": sequence,
            "type": "task.delivered",
            "occurredAt": "2026-10-03T01:00:00+08:00",
            "payload": {
                "summary": "交付报告导入切片",
                "reportPath": str(self.workspace / "report.md"),
                "changedFiles": ["evidence/report.md"],
                "verification": ["定向测试已跑"],
                "risks": [],
                "unfinished": [],
            },
        }
        code, result = self.cli(["event", "ingest", "--file",
                                 self.write_json("deliver-%02d.json" % sequence, payload)])
        self.assertEqual(code, 0, result)
        return result["data"]["data"]["deliveryId"]

    def bootstrap(self):
        task_id = self.create_task()
        attempt_id = self.submit_attempt(task_id)
        delivery_id = self.deliver(task_id, attempt_id)
        return task_id, attempt_id, delivery_id

    def import_report_file(self, path, task_id, attempt_id, delivery_id, extra=()):
        return self.cli(["report", "import", "--task", task_id, "--attempt", attempt_id,
                         "--delivery", delivery_id, "--file", path] + list(extra))

    def report_path(self, task_id, attempt_id, name="report-board.md", body=None, **over):
        """生成一份 report-board.v1 Markdown；来源任务卡夹具必须落在受控根内。"""
        card_path = self.task_card_path()
        return self.write_report(
            name, report_frontmatter(task_id, attempt_id, str(card_path), **over),
            body=body or "正文：这里不承载可信状态。",
        )

    def escape_report(self, name, task_id, attempt_id, card_path="/etc/passwd"):
        """把 sourceCard.path 指到受控根外，验证导入 fail closed。"""
        card = self.task_card_path()
        frontmatter = report_frontmatter(task_id, attempt_id, str(card))
        frontmatter["sourceCard"] = dict(frontmatter["sourceCard"], path=card_path)
        return self.write_report(name, frontmatter)

    def counts(self, task_id):
        store = SqliteStore(str(self.db))
        try:
            return {
                "attempts": len(store.list_attempts(task_id)),
                "deliveries": store.count_deliveries(task_id),
                "reviews": store.count_reviews(task_id),
                "reports": len(store.list_reports(task_id)),
            }
        finally:
            store.close()

    def task_row(self, task_id):
        store = SqliteStore(str(self.db))
        try:
            return dict(store.get_task(task_id))
        finally:
            store.close()


class ImportPositiveTests(D4ReportImportTestCase):
    def test_import_binds_report_to_current_delivery(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        before = self.counts(task_id)
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 0, result)
        data = result["data"]
        self.assertTrue(data["reportId"])
        self.assertEqual(data["deliveryId"], delivery_id)
        self.assertEqual(data["attemptId"], attempt_id)
        self.assertTrue(data["canonicalDigest"].startswith("sha256:"))
        self.assertEqual(data["boardSummary"]["stage"], "报告已经接进本地账本")
        self.assertFalse(data["replayed"])
        # 没有技术正文被塞进看板摘要字段
        self.assertNotIn("boardSummary", data["boardSummary"]["result"])
        after = self.counts(task_id)
        self.assertEqual(after["reports"], before["reports"] + 1)
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["deliveries"], before["deliveries"])
        self.assertEqual(after["reviews"], before["reviews"])

    def test_duplicate_import_replays_first_result(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        first = self.import_report_file(path, task_id, attempt_id, delivery_id)[1]["data"]
        second_code, second = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(second_code, 0, second)
        second_data = second["data"]
        self.assertTrue(second_data["replayed"])
        self.assertEqual(second_data["canonicalDigest"], first["canonicalDigest"])
        self.assertEqual(second_data["importedAt"], first["importedAt"])
        self.assertEqual(self.counts(task_id)["reports"], 1)

    def test_rewrite_same_content_still_replays(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        first_path = self.report_path(task_id, attempt_id, name="r1.md")
        copy_path = self.report_path(task_id, attempt_id, name="r2.md")
        self.assertEqual(
            self.import_report_file(first_path, task_id, attempt_id, delivery_id)[1]["data"]["canonicalDigest"],
            self.import_report_file(copy_path, task_id, attempt_id, delivery_id)[1]["data"]["canonicalDigest"],
        )
        self.assertEqual(self.counts(task_id)["reports"], 1)

    def test_same_revision_different_content_conflicts(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        self.import_report_file(self.report_path(task_id, attempt_id, name="r1.md"),
                                task_id, attempt_id, delivery_id)
        conflicting = self.report_path(
            task_id, attempt_id, name="r2.md",
            evidence=[{"kind": "test", "detail": "改动过的证据", "level": "reported"}],
        )
        code, result = self.import_report_file(conflicting, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "event_conflict")
        # 冲突事实保留，原报告不被覆盖
        store = SqliteStore(str(self.db))
        try:
            rows = store.list_conflicts(task_id)
            self.assertEqual(len(rows), 1)
            self.assertEqual(json.loads(rows[0]["detail_json"])["reportId"],
                             "D4-NEXT-01C-TEST-QA")
            self.assertEqual(len(store.list_reports(task_id)), 1)
            self.assertEqual(json.loads(store.get_report(
                "D4-NEXT-01C-TEST-QA", 1)["evidence_json"])[0]["detail"], "本卡定向测试")
        finally:
            store.close()

    def test_source_card_verification_flags(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        # 显式传入与库内登记一致的期望值：成功且 verified=true
        code, result = self.import_report_file(
            path, task_id, attempt_id, delivery_id,
            extra=["--source-card-id", "D4-NEXT-01C-TEST-SRC", "--source-card-revision", "1"])
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceFile"]["sourceCardVerified"])
        # R1：不传来源参数也走库内硬核对；受控来源匹配则正式成功、verified=true
        other = self.report_path(task_id, attempt_id, name="r2.md", reportId="D4-NEXT-01C-TEST-QA-2")
        code, result = self.import_report_file(other, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["sourceFile"]["sourceCardVerified"])

    def test_source_card_mismatch_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        # 报告声明的来源卡与库内登记不符：不传参数也拒绝（硬核对）
        code, result = self.import_report_file(
            path, task_id, attempt_id, delivery_id,
            extra=["--source-card-id", "OTHER-CARD", "--source-card-revision", "9"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "source_card_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_source_card_half_given_is_usage_error(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report_file(
            path, task_id, attempt_id, delivery_id,
            extra=["--source-card-id", "D4-NEXT-01C"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")


class ImportNegativeTests(D4ReportImportTestCase):
    def test_old_delivery_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        # 走真实返工链：changes_requested -> 新轮次 -> 再次交付，当前交付变成新的
        review = {
            "decision": "changes_requested",
            "deliveryId": delivery_id,
            "note": "补充缺少的部分",
        }
        code, result = self.cli(["review", "submit", "--task", task_id,
                                 "--file", self.write_json("review.json", review)])
        self.assertEqual(code, 0, result)
        new_attempt = result["data"]["resultingAttemptId"]
        self.submit_attempt(task_id, attempt_id=new_attempt)  # 提交当前（新）轮次
        new_delivery = self.deliver(task_id, new_attempt)
        self.assertNotEqual(new_delivery, delivery_id)
        # 旧交付报告：优先命中“当前轮次已变化”，不得写库
        old_path = self.report_path(task_id, attempt_id, name="old.md",
                                    reportId="D4-NEXT-01C-OLD")
        code, result = self.import_report_file(old_path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("review_version_mismatch", "report_binding_mismatch"))
        self.assertEqual(self.counts(task_id)["reports"], 0)
        # 历史交付与新交付保持原状
        store = SqliteStore(str(self.db))
        try:
            row = dict(store.get_task(task_id))
            self.assertEqual(row["current_delivery_id"], new_delivery)
            self.assertEqual(row["current_attempt_id"], new_attempt)
        finally:
            store.close()

    def test_missing_board_summary_field_fails_closed(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id, boardSummary={
            "stage": "缺了字段", "problem": "没有写完整", "nextStep": "补好再来"})
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("document_invalid", "board_summary_missing"))
        self.assertEqual(self.counts(task_id)["reports"], 0)
        self.assertEqual(self.task_row(task_id)["status"], "awaiting_review")

    def test_schema_cross_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id, schemaVersion="task-card-board.v1")
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("document_invalid", "schema_doc_mismatch"))
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_identity_conflict_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id, executedBy={
            "alias": "另一位成员", "role": "qa-reporter", "host": "OpenClaw",
            "agentId": "qa-1", "environmentId": "qclaw-local",
        })
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("document_invalid", "identity_conflict"))
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_path_escape_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        escaped = self.escape_report("escape.md", task_id, attempt_id)
        code, result = self.import_report_file(str(escaped), task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertIn(result["error"]["code"], ("document_invalid", "path_escape"))
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_task_mismatch_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id, taskId="task-another")
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "report_binding_mismatch")
        self.assertEqual(self.counts(task_id)["reports"], 0)

    def test_source_mismatch_task_is_rejected(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        store = SqliteStore(str(self.db))
        store.update_task(task_id, source_id="qclaw-local")
        store.close()
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_body_claiming_completion_does_not_change_anything(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(
            task_id, attempt_id,
            body="本次任务已经完成、已通过检查、可以发布，status: approved。")
        before = self.task_row(task_id)
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 0, result)
        after = self.task_row(task_id)
        self.assertEqual(after["status"], before["status"])
        self.assertEqual(after["updated_at"], before["updated_at"])
        self.assertEqual(self.counts(task_id)["reviews"], 0)
        # 摘要里也不能出现技术词/英文状态
        for value in result["data"]["boardSummary"].values():
            self.assertIsNone(detect_tech_leak(value))

    def test_import_without_controlled_context_fails_closed(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        store = SqliteStore(str(self.db))
        self.assertRaises(Exception, import_report, store, task_id, attempt_id,
                          delivery_id, path, None)
        store.close()
        self.assertEqual(self.counts(task_id)["reports"], 0)


class ProjectionTests(D4ReportImportTestCase):
    def _import_default(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        path = self.report_path(task_id, attempt_id)
        code, result = self.import_report_file(path, task_id, attempt_id, delivery_id)
        self.assertEqual(code, 0, result)
        return task_id, attempt_id, delivery_id

    def test_read_model_returns_plain_summary_and_technical_details(self):
        task_id, _attempt_id, delivery_id = self._import_default()
        detail = board_data(str(self.db), "/api/tasks/" + task_id)
        summary = detail["summary"]
        self.assertFalse(summary["pending"])
        self.assertEqual(set(summary["fields"]), {"stage", "result", "problem", "nextStep"})
        self.assertEqual(summary["fields"]["stage"], "报告已经接进本地账本")
        # 技术详情独立保留给 CoX，不在默认摘要里
        self.assertEqual(summary["report"]["deliveryId"], delivery_id)
        self.assertTrue(summary["report"]["canonicalDigest"].startswith("sha256:"))
        self.assertEqual(summary["report"]["executedBy"]["agentId"], "qa-1")
        self.assertEqual(summary["report"]["businessSummary"]["owner"], "cox")
        self.assertEqual(detail["deliveries"][0]["summary"]["status"], "imported")

    def test_sqlite_reopen_returns_same_summary(self):
        task_id, _attempt_id, _delivery_id = self._import_default()
        store = SqliteStore(str(self.db))
        try:
            first = ReadModels(store).task_detail(task_id)["summary"]
        finally:
            store.close()
        reopened = SqliteStore(str(self.db))
        try:
            second = ReadModels(reopened).task_detail(task_id)["summary"]
        finally:
            reopened.close()
        self.assertEqual(first, second)
        self.assertEqual(first["fields"]["nextStep"], "等负责人复核本卡结果")

    def test_project_summary_carries_task_summary(self):
        task_id, _attempt_id, _delivery_id = self._import_default()
        projects = board_data(str(self.db), "/api/projects")["projects"]
        task_view = projects[0]["tasks"][0]
        self.assertEqual(task_view["summary"]["fields"]["stage"], "报告已经接进本地账本")
        self.assertEqual(task_view["taskId"], task_id)

    def test_pending_summary_when_report_not_imported(self):
        task_id, _attempt_id, _delivery_id = self.bootstrap()
        detail = board_data(str(self.db), "/api/tasks/" + task_id)
        summary = detail["summary"]
        # 交付已带报告路径 -> “报告已生成，尚未回传”（不得显示“已完成”）
        self.assertTrue(summary["pending"])
        self.assertEqual(summary["code"], "summary_pending")
        self.assertEqual(summary["status"], "generated_not_imported")
        self.assertEqual(summary["fields"], {})
        self.assertIsNone(summary["report"])

    def test_legacy_db_without_reports_table_still_reads(self):
        task_id, _attempt_id, _delivery_id = self._import_default()
        writer = SqliteStore(str(self.db))
        writer.conn.execute("DROP TABLE reports")
        writer.close()
        detail = board_data(str(self.db), "/api/tasks/" + task_id)
        self.assertTrue(detail["summary"]["pending"])
        self.assertEqual(detail["summary"]["code"], "summary_pending")
        self.assertEqual(detail["reports"], [])
        # 旧交付摘要缺字段只能标待补充，不得伪造
        self.assertEqual(detail["deliveries"][0]["summary"]["fields"], {})

    def test_board_payload_has_no_internal_ids_in_plain_summary(self):
        task_id, _attempt_id, _delivery_id = self._import_default()
        detail = board_data(str(self.db), "/api/tasks/" + task_id)
        plain = json.dumps(detail["summary"]["fields"], ensure_ascii=False)
        for token in ("taskId", "attemptId", "deliveryId", "agentId", "error_code",
                      "canonicalDigest", "awaiting_review", "d4ctl"):
            self.assertNotIn(token, plain)
        self.assertIsNone(detect_tech_leak(detail["summary"]["fields"]["nextStep"]))

    def test_board_page_does_not_render_internal_ids_by_default(self):
        """看板默认不渲染内部字段；技术详情由委托的 project-view.js 承载。

        D4 053 裁定：board.js 只做板面编排，技术详情渲染委托给
        `ProjectView.taskDetail` / `ProjectView.technicalDetails`。因此不能再要求
        board.js 自己重复出现技术详情源码标记或内部字段——那样既与真实分层不符，
        也会退化成“把整份源码当成 head”的空断言。修正后的断言分两层：
        1) board.js 只做委托，自己不出现任何内部字段（证明不重复渲染）；
        2) 委托目标 project-view.js 的技术详情分节里覆盖 reportId 等内部字段。
        """
        board_source = BOARD_JS.read_text(encoding="utf-8")
        view_source = PROJECT_VIEW_JS.read_text(encoding="utf-8")

        # 1) board.js 是编排层：技术详情交给 ProjectView，自己不渲染内部字段
        self.assertIn("ProjectView.taskDetail", board_source)
        internal_tokens = ("reportId", "reportRevision", "canonicalDigest", "deliveryId",
                           "attemptId", "sourceCard")
        for token in internal_tokens:
            self.assertNotIn(token, board_source)

        # 2) 技术详情渲染落在 project-view.js：分节存在、默认折叠，内部字段在此覆盖
        tech_head, tech_sep, tech_tail = view_source.partition("技术详情")
        self.assertTrue(tech_head, "视图组件缺少技术详情分节")
        self.assertEqual(tech_sep, "技术详情", "视图组件缺少技术详情分节")
        self.assertIn("technicalDetails", view_source)
        self.assertIn("tech-details", view_source)  # 默认折叠的 details 容器
        self.assertIn("reportId", tech_tail)  # 技术详情保留 reportId 给 CoX 展开

    def test_query_after_import_shows_summary(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        self.import_report_file(self.report_path(task_id, attempt_id), task_id, attempt_id,
                                delivery_id)
        code, result = self.cli(["query", "--task", task_id])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["summary"]["fields"]["result"],
                         "导入、记账和看板展示都能跑通")

    def test_receipt_import_path_is_unchanged(self):
        """旧 JSON 事件/回执入口不回归：event import 仍只认本轮 events.json。"""
        task_id, attempt_id, delivery_id = self.bootstrap()
        code, result = self.cli(["event", "import", "--task", task_id, "--attempt", attempt_id,
                                 "--file", str(self.workspace / "report.md")])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "path_escape")
        # 报告导入不受影响
        self.assertEqual(
            self.import_report_file(self.report_path(task_id, attempt_id), task_id, attempt_id,
                                    delivery_id)[0], 0)


class SchemaMigrationTests(D4ReportImportTestCase):
    def test_reports_table_is_created_additively(self):
        self.create_task()
        store = SqliteStore(str(self.db))
        try:
            self.assertTrue(store.has_reports_table())
            columns = {row["name"] for row in
                       store.conn.execute("PRAGMA table_info(reports)").fetchall()}
            for column in ("report_id", "report_revision", "canonical_digest",
                           "board_summary_json", "delivery_id"):
                self.assertIn(column, columns)
        finally:
            store.close()

    def test_reopen_after_manual_table_drop_recreates_table(self):
        task_id, attempt_id, delivery_id = self.bootstrap()
        writer = SqliteStore(str(self.db))
        writer.conn.execute("DROP TABLE reports")
        writer.close()
        rewriter = SqliteStore(str(self.db))  # 写入口自动 additive migration
        self.assertTrue(rewriter.has_reports_table())
        rewriter.close()
        self.assertEqual(
            self.import_report_file(self.report_path(task_id, attempt_id), task_id, attempt_id,
                                    delivery_id)[0], 0)
        store = SqliteStore(str(self.db))
        try:
            self.assertEqual(len(store.list_reports(task_id)), 1)
        finally:
            store.close()

    def test_read_only_open_of_legacy_db_does_not_crash(self):
        task_id, _attempt_id, _delivery_id = self.bootstrap()
        writer = SqliteStore(str(self.db))
        writer.conn.execute("DROP TABLE reports")
        writer.close()
        reader = SqliteStore(str(self.db), read_only=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                reader.conn.executescript("CREATE TABLE forbidden (id TEXT)")
            self.assertEqual(reader.list_reports(task_id), [])
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
