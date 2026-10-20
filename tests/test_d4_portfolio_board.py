"""D4-MVP20-C 定向测试：五层读模型与总体驾驶舱看板。

交付来源：task-demo-98324abad0 / att-demo-3f4fe68c21（原候选，只作来源记录，不重放）。
本轮校正：task-demo-901b4f8579 / att-demo-5336e46e87（只改本文件与 board/board.css）。

覆盖任务卡验收：
1. 双项目隔离（任务、业务线、成员、问题互不串项目；同名任务不合并）
2. 业务线归属（登记业务线与计划业务线分开；失效业务线引用安全显示）
3. Worker 归属（独立登记对象；未登记成员显示未知，不按近似名称匹配）
4. 空项目（不伪造阶段、当前工作或建议）
5. unknown / 旧任务链接 / 筛选（安全空结果，fail closed）
6. 计划 / 真实 / 自报 / 独立审核记录区分（recordType + evidence）
7. 普通视图三问与技术详情分离（路径、摘要、命令、原始报错只在技术详情）
8. 看板静态合同（五层导航、面包屑、折叠技术详情、空态文案）

只读边界：
- fixture 是 ``tempfile`` 下的隔离临时 SQLite（合成数据），不写任何业务/验证库；
- 读模型一律通过 ``SqliteStore(..., read_only=True)`` 访问，不派发宿主、不调用 Adapter；
- 只读依赖文件摘要与开工基线比对，漂移即失败。
"""
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from contracts.schemas import ContractError
from projections.read_models import (
    EVIDENCE_DOCUMENT_ARCHIVE,
    EVIDENCE_PLAN_ONLY,
    EVIDENCE_REVIEW_RECORD,
    EVIDENCE_UNVERIFIED,
    EVIDENCE_WORKER_REPORT,
    LAYER_BUSINESS_LINES,
    LAYER_ORDER,
    LAYER_PORTFOLIO,
    LAYER_PROJECTS,
    LAYER_TASKS,
    LAYER_WORKERS,
    RECORD_HISTORICAL_ARCHIVE,
    RECORD_INDEPENDENT_REVIEW,
    RECORD_PLANNING_SNAPSHOT,
    RECORD_PROJECT_STATUS,
    RECORD_REAL_EXECUTION,
    RECORD_SELF_REPORTED_DELIVERY,
    RECORD_SYSTEM_LEDGER,
    ReadModels,
    evidence_marker,
)
from storage.planning_schema import migrate_planning_schema
from storage.sqlite_store import SqliteStore

ROOT = Path(__file__).resolve().parents[1]

#: 本轮允许修改的现有产品文件（writeScope；开工基线见交付报告）。
CHANGED_PRODUCT_FILES = (
    "projections/read_models.py",
    "board/index.html",
    "board/board.js",
    "board/project-view.js",
    "board/board.css",
    "scripts/d4_board.py",
)

#: 只读依赖：开工时冻结的摘要，任一漂移即说明有并行改动，必须停止核对。
READ_ONLY_DEPENDENCIES = (
    "storage/sqlite_store.py",
    "storage/planning_schema.py",
    "projections/project_catalog.py",
    "contracts/planning_structure.py",
    "tests/test_d4_board.py",
    "tests/test_d4_board_summary_contract.py",
)

#: 开工基线（2026-10-11 首次运行本文件时记录，修改产品文件之前）。
FROZEN_READ_ONLY_DIGESTS = {
    "storage/sqlite_store.py": "b32f979cfa3a27c4be5d68852844df3fe1c196419c41d7ea4a2b0199d9e29e29",
    "storage/planning_schema.py": "422b6a4f70482ffb22dc8469bd939b07e9a76d1e53ac9de0c9576b30db7ab61f",
    "projections/project_catalog.py": "df7e744537af833e7f34f0b16029b971727223d05ee0844fb335240f2cd7c108",
    "contracts/planning_structure.py": "0d5c8319a9cd88ff0dc5de4ef722127ecc3489399db52f78f82fed2ac7261550",
    "tests/test_d4_board.py": "ecbc43be5900b735d862ccc6b8b27334b6b0cfb420ca22ecb494261ffe1a336f",
    "tests/test_d4_board_summary_contract.py": "60b627fab61a5c52e3ad6039b29a0d0672f01bf5259e2567fdd85c2adc91892e",
}

T0 = "2026-10-11T10:00:00+08:00"

PLAN_PAYLOAD = {
    "schemaVersion": "planning-snapshot.v1",
    "projectId": "proj-alpha",
    "planningSourceId": "src-alpha-plan",
    "sourceKind": "development-plan",
    "sourceFile": {
        "name": "agent-task-control-plane-d4-development-plan.md",
        "path": "/abs/docs/agent-task-control-plane-d4-development-plan.md",
        "documentVersion": "D4.7",
        "sha256": "sha256:plan-fixture",
        "bytes": 1234,
    },
    "sections": [{"sectionId": "s-11", "title": "MVP 2.0 总体驾驶舱与五层看板", "anchor": "11", "level": 2}],
    "stages": [{"stageId": "st-c", "title": "五层看板", "summary": "只读数据合同与页面结构", "sectionRef": "s-11"}],
    "businessLines": [{
        "businessLineId": "bl-board",
        "title": "总体驾驶舱与五层导航",
        "planStatus": "planned",
        "stageRef": "st-c",
        "sectionRef": "s-11",
    }],
    "milestones": [{
        "milestoneId": "msm-board-ready",
        "title": "五层导航可用",
        "gateCriteria": ["至少两个项目互不串数据", "计划与真实记录标识分明"],
        "planStatus": "planned",
        "businessLineRef": "bl-board",
        "sectionRef": "s-11",
    }],
    "planStatus": "planned",
    "unresolved": [{"title": "窄屏回归未安排", "reason": "本轮只做只读结构"}],
}


def _task(task_id, project_id, title, status, **over):
    record = {
        "task_id": task_id,
        "project_id": project_id,
        "title": title,
        "agent_id": "dev-ckb1",
        "source_id": "codebuddy-cli",
        "workspace": "/tmp/d4-fixture/ws/%s" % task_id,
        "baseline": None,
        "goal": "目标：%s" % title,
        "acceptance_json": json.dumps(["验收项"], ensure_ascii=False),
        "permissions_json": json.dumps({"allowGit": False}, ensure_ascii=False),
        "limits_json": json.dumps({"allowNetwork": False}, ensure_ascii=False),
        "status": status,
        "revision": 1,
        "current_attempt_id": None,
        "current_delivery_id": None,
        "closure_json": None,
        "created_at": T0,
        "updated_at": T0,
    }
    record.update(over)
    return record


def _attempt(attempt_id, task_id, ordinal, state, **over):
    record = {
        "attempt_id": attempt_id,
        "task_id": task_id,
        "ordinal": ordinal,
        "state": state,
        "created_at": T0,
        "submitted_at": None,
        "accepted_by_host_at": None,
        "started_at": None,
        "terminal_at": None,
        "terminal_kind": None,
        "blocked_json": None,
        "note": None,
        "submit_state": "created",
        "submission_json": None,
    }
    record.update(over)
    return record


def _delivery(delivery_id, task_id, attempt_id, **over):
    record = {
        "delivery_id": delivery_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "summary": None,
        "changed_files_json": json.dumps([], ensure_ascii=False),
        "verification_json": json.dumps([], ensure_ascii=False),
        "risks_json": json.dumps([], ensure_ascii=False),
        "report_path": None,
        "unfinished_json": json.dumps([], ensure_ascii=False),
        "source_delivery_id": None,
        "created_at": T0,
    }
    record.update(over)
    return record


def build_fixture(path):
    """构建隔离临时库：合成数据，不写业务库、不派发宿主。"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(path)
    try:
        # 1) 基础登记：项目必须先存在（planning_sources 有外键）
        store.conn.execute("PRAGMA foreign_keys = ON")
        store.register_agent("dev-ckb1", "dev-ckb1")
        store.register_agent("qa-ckb1", "qa-ckb1")
        store.register_agent("dev-1", "legacy 逻辑成员")
        store.register_source("codebuddy-cli", "CodeBuddy CLI")
        store.register_source("workbuddy-local", "WorkBuddy")
        for row in (
            ("proj-alpha", "Alpha 项目", "把五层驾驶舱读完", "in_progress", "五层读模型", "cox",
             "等负责人检查五层看板", "docs/modules/agent-task-control-plane-d4-development-plan.md",
             "sha256:plan-fixture"),
            ("proj-beta", "Beta 项目", "验证第二个项目隔离", "planned", None, "cox", None, None, None),
            ("proj-empty", "空项目", "登记后还没有任务", "planned", None, "cox", None, None, None),
        ):
            store.conn.execute(
                "INSERT INTO workspace_projects (project_id, name, goal, status, current_stage, "
                "owner_id, next_step, source_document, source_sha256, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row + (T0, T0),
            )
        # 2) 规划快照用冻结迁移建表（只读依赖，摘要已冻结）
        migrate_planning_schema(store.conn)

        for row in (
            ("ws-alpha-api", "proj-alpha", "接口与数据", "冻结五层读模型合同", "in_progress", 0,
             "§11.C", "继续补页面"),
            ("ws-alpha-board", "proj-alpha", "看板与导航", "补齐五层导航", "planned", 1,
             "§23.2", "等待负责人检查"),
            ("ws-beta-core", "proj-beta", "第二项目核心", "验证项目隔离", "planned", 0,
             "§23.2", None),
        ):
            store.conn.execute(
                "INSERT INTO workspace_workstreams (workstream_id, project_id, name, goal, status, "
                "position, source_section, next_step, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row + (T0,),
            )
        for row in (
            ("ms-alpha-api-1", "proj-alpha", "ws-alpha-api", "接口冻结", "in_progress",
             "定向测试全绿且证据可复核", 0),
            ("ms-alpha-board-1", "proj-alpha", "ws-alpha-board", "五层导航可用",
             "in_progress", "五个层级和面包屑都可点", 1),
            ("ms-beta-core-1", "proj-beta", "ws-beta-core", "第二项目隔离", "planned",
             "任务与业务线不串项目", 0),
            ("ms-beta-unverified", "proj-beta", "ws-beta-core", "第二项目独立验收", "planned",
             "双成员双项目独立验收通过", 1),
        ):
            store.conn.execute(
                "INSERT INTO workspace_milestones (milestone_id, project_id, workstream_id, name, "
                "status, gate, position, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                row + (T0,),
            )
        for row in (
            ("dev-ckb1", "dev-ckb1", "skill-worker", "CodeBuddy", "codebuddy-cli", "开发成员",
             "host-default", "registered", "unknown", 1,
             json.dumps(["proj-alpha", "proj-beta"], ensure_ascii=False),
             json.dumps(["/workspace"], ensure_ascii=False),
             json.dumps(["tests"], ensure_ascii=False), 3, None),
            ("qa-ckb1", "qa-ckb1", "qa-reporter", "WorkBuddy", "workbuddy-local", "测试成员",
             "host-default", "registered", "unknown", 1,
             json.dumps(["proj-alpha"], ensure_ascii=False),
             json.dumps(["/workspace"], ensure_ascii=False), json.dumps([], ensure_ascii=False), 1, None),
            ("retired-1", "retired-1", "skill-worker", "WorkBuddy", "workbuddy-local", "已停用成员",
             "host-default", "disabled", "unknown", 0, json.dumps([], ensure_ascii=False),
             json.dumps([], ensure_ascii=False), json.dumps([], ensure_ascii=False), 1, None),
        ):
            store.conn.execute(
                "INSERT INTO workspace_workers (worker_id, display_name, role, host, environment, "
                "description, default_model, registration_status, runtime_status, enabled, "
                "allowed_projects_json, read_scope_json, write_scope_json, binding_revision, "
                "last_checked_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row + (T0,),
            )
        store.conn.execute(
            "INSERT INTO workspace_plan_sources (source_id, project_id, title, path, source_sha256, "
            "imported_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ps-alpha-plan", "proj-alpha", "D4 开发计划",
             "/abs/docs/agent-task-control-plane-d4-development-plan.md",
             "sha256:plan-fixture", T0, "snapshot"),
        )
        store.conn.execute(
            "INSERT INTO planning_sources (project_id, planning_source_id, source_kind, source_path, "
            "allowed_root, registered_by_json, registered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("proj-alpha", "src-alpha-plan", "development-plan",
             PLAN_PAYLOAD["sourceFile"]["path"], "/abs/docs",
             json.dumps({"actorId": "cox"}, ensure_ascii=False), T0),
        )
        store.conn.execute(
            "INSERT INTO planning_snapshots (snapshot_id, project_id, planning_source_id, "
            "snapshot_revision, document_version, source_sha256, source_bytes, canonical_digest, "
            "payload_json, imported_by_json, imported_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("snap-alpha-1", "proj-alpha", "src-alpha-plan", 1, "D4.7", "sha256:plan-fixture",
             1234, "sha256:snap-alpha-1",
             json.dumps(PLAN_PAYLOAD, ensure_ascii=False),
             json.dumps({"actorId": "cox"}, ensure_ascii=False), T0, "candidate"),
        )

        # 3) 真实执行账本：两个项目 + 一个旧任务（项目层级未登记）
        store.insert_task(_task(
            "t-alpha-1", "proj-alpha", "冻结五层读模型合同", "approved",
            agent_id="dev-ckb1", current_attempt_id="a-alpha-1", current_delivery_id="d-alpha-1",
            closure_json=json.dumps({"qaExecuted": True, "published": False}, ensure_ascii=False),
        ))
        store.insert_task(_task(
            "t-alpha-2", "proj-alpha", "补总体驾驶舱页面", "awaiting_review",
            agent_id="dev-ckb1", current_attempt_id="a-alpha-2", current_delivery_id="d-alpha-2",
        ))
        store.insert_task(_task(
            "t-beta-1", "proj-beta", "冻结五层读模型合同", "running",
            agent_id="dev-ckb1", current_attempt_id="b-beta-1",
        ))
        store.insert_task(_task(
            "t-beta-2", "proj-beta", "接入第二宿主（计划外）", "blocked",
            agent_id="qa-ckb1", current_attempt_id="b-beta-2",
        ))
        store.insert_task(_task(
            "t-legacy", "proj-legacy", "旧任务（项目层级未登记）", "ready",
            agent_id="dev-1", source_id="workbuddy-local",
        ))

        store.insert_attempt(_attempt(
            "a-alpha-1", "t-alpha-1", 1, "delivered", submitted_at=T0,
            accepted_by_host_at=T0, started_at=T0, terminal_at=T0, terminal_kind="delivered",
            submit_state="submitted",
        ))
        store.insert_attempt(_attempt(
            "a-alpha-2", "t-alpha-2", 1, "running", submitted_at=T0,
            accepted_by_host_at=T0, started_at=T0, submit_state="submitted",
        ))
        store.insert_attempt(_attempt(
            "b-beta-1", "t-beta-1", 1, "running", submitted_at=T0,
            accepted_by_host_at=T0, started_at=T0, submit_state="submitted",
        ))
        store.insert_attempt(_attempt(
            "b-beta-2", "t-beta-2", 1, "submitted", submitted_at=T0, submit_state="submitted",
            blocked_json=json.dumps({"summary": "等待负责人确认接口范围",
                                     "reason": "范围未定"}, ensure_ascii=False),
        ))

        store.insert_delivery(_delivery(
            "d-alpha-1", "t-alpha-1", "a-alpha-1",
            summary="五层读模型已实现并通过定向测试",
            changed_files_json=json.dumps(
                ["projections/read_models.py", "tests/test_d4_portfolio_board.py"],
                ensure_ascii=False),
            verification_json=json.dumps(
                ["python3 -m unittest discover -s tests -p test_d4_portfolio_board.py"],
                ensure_ascii=False),
            report_path="/tmp/d4-fixture/receipts/t-alpha-1/report.md",
        ))
        store.insert_delivery(_delivery(
            "d-alpha-2", "t-alpha-2", "a-alpha-2",
            summary="看板五层导航已补齐",
            report_path="/tmp/d4-fixture/receipts/t-alpha-2/report.md",
            unfinished_json=json.dumps(["深链接回跳未验证"], ensure_ascii=False),
            risks_json=json.dumps(["窄屏未实测"], ensure_ascii=False),
        ))

        store.insert_review({
            "review_id": "r-alpha-1", "task_id": "t-alpha-1", "delivery_id": "d-alpha-1",
            "decision": "approved", "note": "独立复核通过", "review_round": 1,
            "resulting_attempt_id": None, "reviewer_id": "cox", "created_at": T0,
        })
        store.insert_report({
            "report_id": "rpt-alpha-2", "report_revision": 1, "report_type": "development",
            "project_id": "proj-alpha", "task_id": "t-alpha-2", "attempt_id": "a-alpha-2",
            "delivery_id": "d-alpha-2",
            "source_card_json": json.dumps({"cardId": "D4-MVP20-C", "cardRevision": 1},
                                           ensure_ascii=False),
            "requested_by_json": json.dumps({"alias": "cox", "role": "sales-architect"},
                                            ensure_ascii=False),
            "executed_by_json": json.dumps(
                {"alias": "dev-ckb1", "role": "skill-worker", "host": "CodeBuddy"},
                ensure_ascii=False),
            "reviewed_by_json": json.dumps({"alias": "cox"}, ensure_ascii=False),
            "adapter": "codebuddy-cli", "session_id": "unverified", "host_run_id": "unverified",
            "board_summary_json": json.dumps({
                "stage": "已经接好五层导航",
                "result": "看板可以按驾驶舱、项目、业务线、任务浏览",
                "problem": "深链接回跳还没验证",
                "nextStep": "等负责人检查这次交付",
            }, ensure_ascii=False),
            "business_summary_json": json.dumps(
                {"summary": "补齐五层读模型", "owner": "dev-ckb1", "unfinished": ["深链接回跳"],
                 "impact": ["看板可以按项目分级浏览"], "nextStep": "交 cox review"},
                ensure_ascii=False),
            "evidence_json": json.dumps([
                {"kind": "command",
                 "detail": "python3 -m unittest discover -s tests -p test_d4_portfolio_board.py",
                 "level": "reported"},
                {"kind": "raw-error", "detail": "示例：第一次跑时导入名写错", "level": "unverified"},
            ], ensure_ascii=False),
            "canonical_digest": "sha256:fixture-alpha-2",
            "source_file_json": json.dumps({"path": "/abs/receipts/t-alpha-2/report.md"},
                                           ensure_ascii=False),
            "imported_at": T0,
            "imported_by_json": json.dumps({"actorId": "cox"}, ensure_ascii=False),
            "synthetic": 1,
        })

        links = (
            ("t-alpha-1", "proj-alpha", "ws-alpha-api", "ms-alpha-api-1", "dev-ckb1"),
            ("t-alpha-2", "proj-alpha", "ws-alpha-board", "ms-alpha-board-1", "dev-ckb1"),
            ("t-beta-1", "proj-beta", "ws-beta-core", "ms-beta-core-1", "dev-ckb1"),
            ("t-beta-2", "proj-beta", "ws-beta-core", "ms-beta-core-1", "w-ghost"),
            ("t-legacy", "proj-legacy", "ws-missing", "ms-missing", "w-ghost"),
        )
        for link in links:
            store.conn.execute(
                "INSERT INTO workspace_task_links (task_id, project_id, workstream_id, "
                "milestone_id, worker_id, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                link + (T0,),
            )

        for task_id, attempt_id in (("t-alpha-1", "a-alpha-1"), ("t-alpha-2", "a-alpha-2"),
                                    ("t-beta-1", "b-beta-1"), ("t-beta-2", "b-beta-2"),
                                    ("t-legacy", None)):
            store.log_activity(task_id, attempt_id, "task.created",
                               {"synthetic": True}, at=T0)
            if attempt_id:
                store.log_activity(task_id, attempt_id, "attempt.submitted",
                                   {"synthetic": True}, at=T0)
    finally:
        store.close()
    return path


class PortfolioFixtureCase(unittest.TestCase):
    """隔离临时库 + 只读读模型脚手架。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "portfolio.sqlite3"
        build_fixture(self.db)
        self.store = SqliteStore(self.db, read_only=True)
        self.addCleanup(self.store.close)
        self.models = ReadModels(self.store)

    def layers(self):
        return self.models.layer_objects()

    def project(self, project_id):
        return {item["projectId"]: item for item in self.layers()["projects"]["projects"]}[project_id]

    def task(self, project_id, task_id):
        return {item["taskId"]: item for item in self.project(project_id)["tasks"]}[task_id]


class FiveLayerTests(PortfolioFixtureCase):
    def test_read_model_returns_five_layers(self):
        layers = self.layers()
        for name in (LAYER_PORTFOLIO, LAYER_PROJECTS, LAYER_BUSINESS_LINES, LAYER_TASKS,
                     LAYER_WORKERS):
            self.assertIn(name, layers)
        self.assertEqual(tuple(layers["layerOrder"]), LAYER_ORDER)
        self.assertEqual(layers["portfolio"]["layer"], LAYER_PORTFOLIO)
        self.assertTrue(layers["projects"]["projects"])
        self.assertTrue(layers["businessLines"]["businessLines"])
        self.assertTrue(layers["tasks"]["tasks"])
        self.assertEqual(layers["workers"]["layer"], LAYER_WORKERS)

    def test_projects_are_not_peers_of_tasks(self):
        layers = self.layers()
        for project in layers["projects"]["projects"]:
            self.assertIn("tasks", project)
            self.assertNotIn("taskId", project)
            for item in project["tasks"]:
                self.assertEqual(item["layer"], LAYER_TASKS)
                self.assertEqual(item["projectId"], project["projectId"])
        for row in layers["portfolio"]["projects"]:
            self.assertNotIn("taskId", row)
        for item in layers["tasks"]["tasks"]:
            self.assertNotIn("projects", item)

    def test_task_ids_stay_stable_and_do_not_merge_across_projects(self):
        layers = self.layers()
        task_ids = [item["taskId"] for item in layers["tasks"]["tasks"]]
        self.assertEqual(len(task_ids), len(set(task_ids)))
        self.assertIn("t-alpha-1", task_ids)
        self.assertIn("t-beta-1", task_ids)
        # 同名任务（两个项目都叫「冻结五层读模型合同」）不合并
        same_title = [item for item in layers["tasks"]["tasks"]
                      if item["title"] == "冻结五层读模型合同"]
        self.assertEqual(len(same_title), 2)
        self.assertEqual({item["projectId"] for item in same_title}, {"proj-alpha", "proj-beta"})

    def test_project_and_worker_layers_do_not_mix(self):
        layers = self.layers()
        alpha = {item["projectId"]: item for item in layers["projects"]["projects"]}["proj-alpha"]
        beta = {item["projectId"]: item for item in layers["projects"]["projects"]}["proj-beta"]
        self.assertEqual({t["taskId"] for t in alpha["tasks"]}, {"t-alpha-1", "t-alpha-2"})
        self.assertEqual({t["taskId"] for t in beta["tasks"]}, {"t-beta-1", "t-beta-2"})
        self.assertEqual({t["projectId"] for t in alpha["tasks"]}, {"proj-alpha"})
        self.assertEqual({t["projectId"] for t in beta["tasks"]}, {"proj-beta"})


class BusinessLineTests(PortfolioFixtureCase):
    def test_business_line_attribution(self):
        lines = {item["businessLineId"]: item
                 for item in self.layers()["businessLines"]["businessLines"]}
        api = lines["ws-alpha-api"]
        self.assertEqual(api["projectId"], "proj-alpha")
        self.assertEqual(api["taskIds"], ["t-alpha-1"])
        self.assertEqual(api["workerIds"], ["dev-ckb1"])
        self.assertEqual(api["milestoneIds"], ["ms-alpha-api-1"])
        self.assertEqual(api["taskIds"], [t["taskId"] for t in api["tasks"]])
        self.assertEqual(lines["ws-beta-core"]["projectId"], "proj-beta")
        self.assertNotIn("t-alpha-1", lines["ws-beta-core"]["taskIds"])

    def test_unregistered_business_line_reference_is_safe(self):
        layers = self.layers()
        legacy_task = {item["taskId"]: item for item in layers["tasks"]["tasks"]}["t-legacy"]
        self.assertEqual(legacy_task["businessLineId"], "ws-missing")
        self.assertIsNone(legacy_task["businessLineName"])
        self.assertFalse(legacy_task["businessLineKnown"])
        for line in layers["businessLines"]["businessLines"]:
            self.assertNotIn("t-legacy", line["taskIds"])

    def test_planned_business_lines_are_kept_separate_from_registered(self):
        lines = self.layers()["businessLines"]["businessLines"]
        planned = [item for item in lines if item["recordType"] == RECORD_PLANNING_SNAPSHOT]
        registered = [item for item in lines if item["recordType"] != RECORD_PLANNING_SNAPSHOT]
        self.assertEqual([item["businessLineId"] for item in planned], ["bl-board"])
        self.assertEqual(planned[0]["evidence"], EVIDENCE_PLAN_ONLY)
        self.assertTrue(planned[0]["plannedOnly"])
        self.assertEqual(planned[0]["sourceSection"], "s-11")
        self.assertEqual(planned[0]["sectionTitle"], "MVP 2.0 总体驾驶舱与五层看板")
        self.assertNotIn("bl-board", [item["businessLineId"] for item in registered])
        self.assertEqual(self.layers()["businessLines"]["counts"]["planned"], 1)
        self.assertEqual(self.layers()["businessLines"]["counts"]["registered"], 3)


class WorkerAttributionTests(PortfolioFixtureCase):
    def test_worker_attribution_is_registry_only(self):
        workers = {item["workerId"]: item for item in self.layers()["workers"]["workers"]}
        alpha = self.project("proj-alpha")
        beta = self.project("proj-beta")
        self.assertEqual({item["workerId"] for item in alpha["workers"]}, {"dev-ckb1"})
        self.assertEqual({item["workerId"] for item in beta["workers"]}, {"dev-ckb1"})
        self.assertNotIn("qa-ckb1", {item["workerId"] for item in beta["workers"]})
        self.assertEqual(sorted(workers["dev-ckb1"]["projects"]), ["proj-alpha", "proj-beta"])
        self.assertTrue(workers["dev-ckb1"]["acceptingTasks"])
        self.assertEqual(workers["dev-ckb1"]["runtimeLabel"], "未验证")
        self.assertEqual(workers["dev-ckb1"]["roleLabel"], "开发成员")
        self.assertFalse(workers["retired-1"]["acceptingTasks"])
        self.assertEqual(workers["retired-1"]["acceptingLabel"], "暂停接单或未启用")

    def test_unknown_worker_reference_is_not_guessed(self):
        layers = self.layers()
        beta_task = {item["taskId"]: item for item in layers["tasks"]["tasks"]}["t-beta-2"]
        self.assertFalse(beta_task["workerKnown"])
        self.assertIsNone(beta_task["workerId"])
        self.assertEqual(beta_task["workerLabel"], "未登记执行成员")
        self.assertIn("w-ghost", layers["workers"]["unknownWorkers"])
        # dev-1 只在账本里出现过，没有成员登记 -> 不按名称近似匹配
        legacy_task = {item["taskId"]: item for item in layers["tasks"]["tasks"]}["t-legacy"]
        self.assertFalse(legacy_task["workerKnown"])
        self.assertIn("dev-1", layers["workers"]["unknownWorkers"])


class EmptyProjectTests(PortfolioFixtureCase):
    def test_empty_project_shows_honest_empty_state(self):
        empty = self.project("proj-empty")
        self.assertEqual(empty["registration"], "registered")
        self.assertEqual(empty["counts"]["tasks"], 0)
        self.assertEqual(empty["emptyState"]["code"], "no_tasks")
        self.assertIn("尚未收到任何任务", empty["emptyState"]["text"])
        self.assertEqual(empty["status"], "planned")
        self.assertEqual(empty["statusSource"], "declared")
        self.assertEqual(empty["statusLabel"], "计划中 · 未启动")
        self.assertEqual(empty["currentStageLabel"], "尚未上报当前阶段")
        self.assertEqual(empty["nextStepLabel"], "尚未上报下一步")
        self.assertIsNone(empty["auxiliary"]["taskCompletionRatio"]["ratio"])
        self.assertEqual(empty["gates"], [])
        self.assertEqual(empty["keyIssues"], [])
        self.assertEqual(empty["independentReview"]["totals"]["notReviewed"], 0)
        unverified = {item["item"] for item in empty["unverified"]}
        self.assertIn("任务执行", unverified)

    def test_project_without_registration_row_is_not_invented(self):
        ids = [item["projectId"] for item in self.layers()["projects"]["projects"]]
        self.assertNotIn("proj-ghost", ids)
        self.assertIn("proj-legacy", ids)
        legacy = self.project("proj-legacy")
        self.assertEqual(legacy["registration"], "execution_only")
        self.assertIn("未在系统登记项目层级", legacy["registrationNote"])
        self.assertEqual(legacy["projectId"], "proj-legacy")
        self.assertIsNone(legacy["name"])
        # 三个语义事实分开断言，不要求整句完全匹配：文案可读性调整不应造成误报
        note = legacy["registrationNote"]
        self.assertIn("未在系统登记项目层级", note)  # 1) 未登记项目层级
        self.assertIn("只有历史执行记录", note)  # 2) 仅有历史执行记录
        self.assertIn("失效的旧链接", note)  # 3) 旧链接已失效


class UnknownAndFilterTests(PortfolioFixtureCase):
    def test_unknown_project_summary_is_safe(self):
        layer = self.models.project_summary("proj-ghost")
        self.assertFalse(layer["known"])
        self.assertEqual(layer["registration"], "execution_only")
        self.assertEqual(layer["tasks"], [])
        self.assertEqual(layer["gates"], [])
        self.assertEqual(layer["emptyState"]["code"], "unregistered")
        self.assertEqual(layer["auxiliary"]["taskCompletionRatio"]["tasksTotal"], 0)

    def test_unknown_task_detail_fails_closed(self):
        with self.assertRaises(ContractError):
            self.models.task_detail("task-ghost")

    def test_filters_return_safe_empty_results(self):
        layers = self.layers()
        no_match = self.models.tasks_layer(project_id="proj-ghost")
        self.assertEqual(no_match["total"], 0)
        self.assertEqual(no_match["filterStatus"], "no_match")
        self.assertEqual(no_match["emptyState"]["code"], "no_match")
        alpha_only = self.models.tasks_layer(project_id="proj-alpha")
        self.assertEqual({item["taskId"] for item in alpha_only["tasks"]},
                         {"t-alpha-1", "t-alpha-2"})
        by_line = self.models.tasks_layer(business_line_id="ws-beta-core")
        self.assertEqual({item["taskId"] for item in by_line["tasks"]}, {"t-beta-1", "t-beta-2"})
        by_worker = self.models.tasks_layer(worker_id="dev-ckb1")
        self.assertEqual({item["taskId"] for item in by_worker["tasks"]},
                         {"t-alpha-1", "t-alpha-2", "t-beta-1"})
        by_status = self.models.tasks_layer(status="blocked")
        self.assertEqual([item["taskId"] for item in by_status["tasks"]], ["t-beta-2"])
        unknown = self.models.tasks_layer(record_type="does-not-exist")
        self.assertEqual(unknown["total"], 0)
        self.assertEqual(unknown["filterStatus"], "unknown_filter_value")
        self.assertEqual(len(unknown["unknownFilters"]), 1)
        self.assertEqual(layers["tasks"]["filterStatus"], "ok")

    def test_legacy_database_without_layer_tables_degrades_safely(self):
        legacy = self.root / "legacy.sqlite3"
        legacy.write_bytes(self.db.read_bytes())
        conn = sqlite3.connect(str(legacy))
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in ("planning_snapshots", "planning_sources", "workspace_task_links",
                          "workspace_milestones", "workspace_workstreams",
                          "workspace_plan_sources", "workspace_workers", "workspace_projects"):
                conn.execute("DROP TABLE IF EXISTS %s" % table)
            conn.commit()
        finally:
            conn.close()
        store = SqliteStore(legacy, read_only=True)
        try:
            models = ReadModels(store)
            layers = models.layer_objects()
            self.assertFalse(layers["availability"]["workspace_projects"])
            self.assertFalse(layers["workers"]["available"])
            self.assertEqual(layers["workers"]["workers"], [])
            self.assertEqual(layers["businessLines"]["businessLines"], [])
            ids = {item["projectId"] for item in layers["projects"]["projects"]}
            self.assertEqual(ids, {"proj-alpha", "proj-beta", "proj-legacy"})
            self.assertTrue(any("没有项目登记表" in note for note in layers["portfolio"]["notes"]))
            self.assertTrue(any(item["item"] == "规划快照" for item in layers["portfolio"]["unverified"]))
        finally:
            store.close()


class PlanVersusRealTests(PortfolioFixtureCase):
    def test_planning_snapshot_is_marked_as_plan_only(self):
        alpha = self.project("proj-alpha")
        snapshot = alpha["planningSnapshot"]
        self.assertEqual(snapshot["recordType"], RECORD_PLANNING_SNAPSHOT)
        self.assertEqual(snapshot["evidence"], EVIDENCE_PLAN_ONLY)
        self.assertEqual(snapshot["documentVersion"], "D4.7")
        self.assertEqual(snapshot["snapshotRevision"], 1)
        plan_gates = [gate for gate in alpha["gates"]
                      if gate["recordType"] == RECORD_PLANNING_SNAPSHOT]
        self.assertEqual([gate["gateId"] for gate in plan_gates], ["msm-board-ready"])
        self.assertEqual(plan_gates[0]["state"], "planned")
        self.assertEqual(plan_gates[0]["evidence"], EVIDENCE_PLAN_ONLY)
        self.assertEqual(plan_gates[0]["gateCriteria"], "至少两个项目互不串数据；计划与真实记录标识分明")
        for gate in plan_gates:
            self.assertNotIn(gate["evidence"],
                             (EVIDENCE_REVIEW_RECORD, EVIDENCE_WORKER_REPORT, "host_event"))
        self.assertEqual(alpha["technical"]["planningSnapshotDigest"], "sha256:plan-fixture")
        # 规划来源的路径/摘要只在技术详情里
        self.assertNotIn(PLAN_PAYLOAD["sourceFile"]["path"], str(alpha["nextStepLabel"]))

    def test_real_execution_and_ledger_markers(self):
        task = self.task("proj-alpha", "t-alpha-1")
        self.assertEqual(task["recordType"], RECORD_REAL_EXECUTION)
        self.assertEqual(task["ledger"]["recordType"], RECORD_SYSTEM_LEDGER)
        self.assertEqual(task["ledger"]["recorded"], True)
        self.assertEqual(task["businessLineId"], "ws-alpha-api")
        self.assertEqual(task["milestoneId"], "ms-alpha-api-1")
        self.assertEqual(task["workerId"], "dev-ckb1")

    def test_self_reported_delivery_does_not_become_approved(self):
        task = self.task("proj-alpha", "t-alpha-2")
        self.assertEqual(task["workerReport"]["recordType"], RECORD_SELF_REPORTED_DELIVERY)
        self.assertEqual(task["reviewRecord"]["recordType"], RECORD_INDEPENDENT_REVIEW)
        self.assertEqual(task["reviewRecord"]["state"], "not_reviewed")
        self.assertEqual(task["reviewRecord"]["label"], "尚未独立审核")
        self.assertFalse(task["facts"]["reviewPassed"])
        self.assertEqual(task["summary"]["fields"]["stage"], "已经接好五层导航")
        alpha = self.project("proj-alpha")
        pending = [issue for issue in alpha["keyIssues"]
                   if issue["kind"] == "awaiting_independent_review"]
        self.assertEqual([issue["taskId"] for issue in pending], ["t-alpha-2"])
        self.assertEqual(pending[0]["evidence"], EVIDENCE_WORKER_REPORT)
        self.assertEqual(pending[0]["recordType"], RECORD_SELF_REPORTED_DELIVERY)

    def test_independent_review_is_separate_and_only_for_delivered_task(self):
        approved = self.task("proj-alpha", "t-alpha-1")
        self.assertEqual(approved["reviewRecord"]["state"], "approved")
        self.assertEqual(approved["reviewRecord"]["evidence"], EVIDENCE_REVIEW_RECORD)
        self.assertTrue(approved["facts"]["reviewPassed"])
        alpha = self.project("proj-alpha")
        self.assertEqual(alpha["independentReview"]["totals"]["approved"], 1)
        self.assertEqual(alpha["independentReview"]["totals"]["notReviewed"], 1)
        gate = {item["gateId"]: item for item in alpha["gates"]}["ms-alpha-api-1"]
        self.assertEqual(gate["state"], "review_passed")
        self.assertEqual(gate["evidence"], EVIDENCE_REVIEW_RECORD)
        board_gate = {item["gateId"]: item for item in alpha["gates"]}["ms-alpha-board-1"]
        self.assertEqual(board_gate["state"], "delivered_pending_review")
        self.assertEqual(board_gate["evidence"], EVIDENCE_WORKER_REPORT)

    def test_history_archive_is_a_separate_record_type(self):
        archive = {
            "projectId": "archive-project", "name": "历史项目", "goal": "早期验证",
            "stage": "已归档", "owner": "cox",
            "milestones": [{"id": "foundation", "title": "基础"}],
            "tasks": [{"taskId": "archive-task", "title": "旧任务", "status": "verified",
                       "milestoneId": "foundation", "member": "旧成员",
                       "result": "早期结果", "problem": "早期问题", "nextStep": "早期下一步",
                       "documents": [{"name": "旧报告", "kind": "report",
                                      "sourcePath": "/original/report.md",
                                      "sha256": "sha256:old"}],
                       "cardIds": ["OLD-1"]}],
            "issues": [{"taskId": "archive-task", "title": "历史遗留", "impact": "无",
                        "owner": "cox", "nextStep": "保持登记", "status": "deferred"}],
        }
        baseline = self.models.portfolio_layer(projects=[
            item for item in self.layers()["projects"]["projects"]
            if item["registration"] != "archive"])
        layers = self.models.layer_objects(archive_projects=[archive])
        archived = [item for item in layers["projects"]["projects"]
                    if item["recordType"] == RECORD_HISTORICAL_ARCHIVE]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["registration"], "archive")
        self.assertEqual(archived[0]["evidenceType"], EVIDENCE_DOCUMENT_ARCHIVE)
        archived_task = archived[0]["tasks"][0]
        self.assertEqual(archived_task["recordType"], RECORD_HISTORICAL_ARCHIVE)
        self.assertEqual(archived_task["evidence"], EVIDENCE_DOCUMENT_ARCHIVE)
        self.assertEqual(archived_task["reviewRecord"]["state"], "not_reviewed")
        self.assertEqual(archived_task["technical"]["paths"], ["/original/report.md"])
        # 历史资料不抬高独立审核计数，也不新增批准
        self.assertEqual(layers["portfolio"]["headline"]["independentReview"]["totals"]["approved"],
                         baseline["headline"]["independentReview"]["totals"]["approved"])
        archived_gate = archived[0]["gates"][0]
        self.assertEqual(archived_gate["evidence"], EVIDENCE_DOCUMENT_ARCHIVE)
        self.assertEqual(archived_gate["state"], "document_only")

    def test_evidence_marker_mapping_is_conservative(self):
        self.assertEqual(evidence_marker("host"), "host_event")
        self.assertEqual(evidence_marker("synthetic"), "synthetic_demo")
        self.assertEqual(evidence_marker("unconfirmed"), EVIDENCE_UNVERIFIED)
        self.assertEqual(evidence_marker("whatever"), EVIDENCE_UNVERIFIED)


class PortfolioConclusionTests(PortfolioFixtureCase):
    def test_conclusion_prioritises_gates_issues_and_review(self):
        portfolio = self.layers()["portfolio"]
        self.assertEqual(portfolio["conclusionBasis"],
                         ["milestone_gate", "key_issue", "independent_review"])
        text = " ".join(portfolio["conclusion"])
        self.assertIn("里程碑门禁", text)
        self.assertIn("关键问题", text)
        self.assertIn("独立审核", text)
        self.assertEqual(portfolio["auxiliary"]["taskCompletionRatio"]["role"], "auxiliary")
        headline = portfolio["headline"]
        self.assertEqual(len(headline["keyIssues"]), 3)
        self.assertEqual({item["kind"] for item in headline["keyIssues"]},
                         {"task_blocked", "awaiting_independent_review", "plan_unresolved"})
        self.assertEqual(headline["independentReview"]["totals"]["approved"], 1)
        self.assertTrue(any(item["item"].startswith("门禁：") for item in portfolio["unverified"]))
        alpha_row = [row for row in portfolio["projects"] if row["projectId"] == "proj-alpha"][0]
        self.assertEqual(alpha_row["gateSummary"]["reviewPassed"], 1)
        self.assertEqual(alpha_row["auxiliary"]["taskCompletionRatio"]["role"], "auxiliary")
        self.assertEqual(alpha_row["auxiliary"]["taskCompletionRatio"]["userConfirmed"]["state"],
                         "unknown")
        self.assertEqual(alpha_row["review"]["approved"], 1)
        self.assertEqual(alpha_row["review"]["notReviewed"], 1)

    def test_completion_ratio_is_auxiliary_only(self):
        alpha = self.project("proj-alpha")
        ratio = alpha["auxiliary"]["taskCompletionRatio"]
        self.assertEqual(ratio["tasksTotal"], 2)
        self.assertEqual(ratio["agentReportedDone"], 2)
        self.assertEqual(ratio["independentReviewPassed"], 1)
        self.assertEqual(ratio["role"], "auxiliary")
        self.assertEqual(ratio["userConfirmed"]["state"], "unknown")
        self.assertIn("辅助", ratio["note"])
        self.assertEqual(self.project("proj-beta")["auxiliary"]["taskCompletionRatio"]["ratio"], 0.0)

    def test_no_evidence_is_reported_as_unverified(self):
        portfolio = self.layers()["portfolio"]
        beta = [row for row in portfolio["projects"] if row["projectId"] == "proj-beta"][0]
        self.assertEqual(beta["gateSummary"]["total"], 2)
        self.assertEqual(beta["gateSummary"]["unverified"], 1)
        beta_project = self.project("proj-beta")
        unlinked = {item["gateId"]: item for item in beta_project["gates"]}["ms-beta-unverified"]
        self.assertEqual(unlinked["state"], "unverified")
        self.assertEqual(unlinked["evidence"], EVIDENCE_UNVERIFIED)
        self.assertEqual(unlinked["taskIds"], [])
        self.assertIn("没有执行或审核证据", unlinked["note"])
        unverified = [item for item in portfolio["unverified"]
                      if item.get("projectId") == "proj-beta"]
        self.assertTrue(unverified)
        self.assertTrue(all(item["evidence"] == EVIDENCE_UNVERIFIED for item in unverified
                            if item["item"].startswith("门禁：")))
        self.assertTrue(any(item["item"] == "门禁：第二项目独立验收" for item in unverified))
        workers = self.layers()["workers"]
        self.assertTrue(all(item["notes"] for item in workers["workers"]))


class PlainViewTests(PortfolioFixtureCase):
    def test_plain_summary_answers_three_questions(self):
        reported = self.task("proj-alpha", "t-alpha-2")
        plain = reported["plainSummary"]
        self.assertEqual(sorted(plain), ["done", "nextStep", "pending", "problem", "source",
                                         "sourceLabel"])
        self.assertEqual(plain["source"], RECORD_SELF_REPORTED_DELIVERY)
        self.assertEqual(plain["done"], "已经接好五层导航；看板可以按驾驶舱、项目、业务线、任务浏览")
        self.assertEqual(plain["problem"], "深链接回跳还没验证")
        self.assertEqual(plain["nextStep"], "等负责人检查这次交付")
        self.assertFalse(plain["pending"])

        no_report = self.task("proj-beta", "t-beta-1")
        self.assertEqual(no_report["plainSummary"]["source"], "status_only")
        self.assertEqual(no_report["plainSummary"]["done"], "执行成员正在处理")
        self.assertEqual(no_report["plainSummary"]["nextStep"], "等待执行成员回传进展或交付。")
        self.assertTrue(no_report["plainSummary"]["pending"])

        blocked = self.task("proj-beta", "t-beta-2")
        self.assertEqual(blocked["plainSummary"]["done"], "遇到问题，暂时无法继续")
        self.assertIn("等待负责人确认接口范围", blocked["plainSummary"]["problem"])
        self.assertEqual(blocked["stateLabel"], "遇到问题，暂时无法继续")

    def test_paths_digests_commands_and_errors_only_in_technical(self):
        task = self.task("proj-alpha", "t-alpha-2")
        plain = json.dumps(task["plainSummary"], ensure_ascii=False)
        for forbidden in ("/tmp/d4-fixture", "sha256", "unittest discover", "raw-error"):
            self.assertNotIn(forbidden, plain)
        for key in ("workspace", "baseline", "permissions_json", "limits_json", "closure_json"):
            self.assertNotIn(key, task)
        technical = task["technical"]
        self.assertIn("/tmp/d4-fixture/ws/t-alpha-2", technical["paths"])
        self.assertIn("sha256:fixture-alpha-2",
                      [item["value"] for item in technical["digests"]])
        self.assertEqual(technical["commands"],
                         ["python3 -m unittest discover -s tests -p test_d4_portfolio_board.py"])
        self.assertEqual(technical["rawErrors"], ["示例：第一次跑时导入名写错"])
        self.assertEqual(technical["recordIds"]["reportId"], "rpt-alpha-2")
        self.assertTrue(technical["notes"])

    def test_task_detail_separates_plain_and_technical(self):
        detail = self.models.task_detail("t-alpha-2")
        self.assertEqual(detail["task"]["task_id"], "t-alpha-2")
        self.assertEqual(detail["recordType"], RECORD_REAL_EXECUTION)
        self.assertEqual(detail["plainSummary"]["done"],
                         "已经接好五层导航；看板可以按驾驶舱、项目、业务线、任务浏览")
        self.assertEqual(detail["technical"]["recordIds"]["taskId"], "t-alpha-2")
        self.assertEqual(detail["independentReview"]["state"], "not_reviewed")
        self.assertEqual(detail["businessLine"]["name"], "看板与导航")
        self.assertEqual(detail["worker"]["workerId"], "dev-ckb1")
        self.assertEqual([crumb["layer"] for crumb in detail["breadcrumb"]],
                         [LAYER_PORTFOLIO, LAYER_PROJECTS, LAYER_BUSINESS_LINES, LAYER_TASKS])
        self.assertEqual(detail["breadcrumb"][-1]["id"], "t-alpha-2")
        self.assertEqual(detail["task"]["agentLabel"], "dev-ckb1")
        # 旧字段仍可用
        self.assertEqual(detail["deliveries"][0]["delivery_id"], "d-alpha-2")
        self.assertEqual(detail["summary"]["fields"]["stage"], "已经接好五层导航")
        self.assertEqual(detail["reviews"], [])
        self.assertEqual(detail["taskDisplayState"], "awaiting_review")


class ReadOnlyBoundaryTests(PortfolioFixtureCase):
    def test_layer_reads_do_not_write_anything(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.models.layer_objects()
        self.models.task_detail("t-alpha-1")
        self.models.task_detail("t-alpha-2")
        self.models.workers_layer()
        self.models.tasks_layer(status="blocked")
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_read_only_connection_rejects_writes(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.models.table_names() and self.store.conn.execute(
                "INSERT INTO workspace_workers (worker_id, display_name, role, host, enabled, "
                "updated_at) VALUES ('x', 'x', 'x', 'x', 1, 'now')")
        with self.assertRaises(sqlite3.OperationalError):
            self.store.conn.executescript("CREATE TABLE forbidden (id TEXT)")

    def test_fixture_and_outputs_stay_inside_temporary_directory(self):
        self.assertTrue(str(self.db).startswith(str(self.root)))
        self.assertEqual(list(self.root.glob("*.sqlite3")), [self.db])


class BoardStaticContractTests(unittest.TestCase):
    """看板前端静态合同（无浏览器运行；渲染结果未在本轮实测）。"""

    def read(self, relative):
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_index_has_five_layers_and_breadcrumb(self):
        html = self.read("board/index.html")
        for layer in LAYER_ORDER:
            self.assertIn('data-layer="%s"' % layer, html)
        self.assertIn('id="breadcrumb"', html)
        self.assertIn('id="portfolio"', html)
        self.assertIn('id="workers"', html)
        self.assertIn('id="projects"', html)
        self.assertIn('id="task-detail"', html)
        self.assertIn('id="project-filter"', html)

    def test_board_script_delegates_layers_and_technical_details(self):
        js = self.read("board/board.js")
        for layer in LAYER_ORDER:
            self.assertIn("'%s'" % layer, js)
        # 真实用户行为：点五层导航按钮切换层级，当前层级写入 aria-current
        self.assertIn("dataset.layer", js)
        self.assertIn("aria-current", js)
        self.assertIn("breadcrumb", js)
        # 模块边界：board.js 只负责取数、选择与导航，渲染和中文文案委托 ProjectView
        for entry in ("ProjectView.breadcrumb", "ProjectView.taskDetail", "ProjectView.portfolio",
                      "ProjectView.projectDetail", "ProjectView.businessLines",
                      "ProjectView.workerList", "ProjectView.taskRow", "ProjectView.filterTasks"):
            self.assertIn(entry, js)
        # 技术详情的 <details>、technical class 与中文摘要由 project-view.js 实现，
        # board.js 不重复渲染这套标记（覆盖见 test_board_css_and_project_view_support_navigation）
        self.assertNotIn("node('details'", js)  # 读取展开状态允许，创建详情节点仍由ProjectView负责
        self.assertNotIn("技术详情", js)
        # 取数入口与“未登记 / 旧链接失效 / 空结果”提示仍由 board.js 负责
        self.assertIn("/api/projects", js)
        self.assertIn("/api/tasks/", js)
        self.assertIn("未登记", js)
        self.assertIn("已经失效", js)
        self.assertIn("没有符合条件的任务", js)

    def test_board_css_and_project_view_support_navigation(self):
        css = self.read("board/board.css")
        for selector in (".layer-nav", ".breadcrumb", ".portfolio", ".worker-list", ".technical"):
            self.assertIn(selector, css)
        view = self.read("board/project-view.js")
        self.assertIn("breadcrumb", view)
        self.assertIn("技术详情", view)
        self.assertIn("'details'", view)
        # 折叠技术详情的容器类名由 project-view.js 承担
        self.assertIn("technical", view)


class ScopeGuardTests(unittest.TestCase):
    def test_read_only_dependencies_match_open_baseline(self):
        drifted = {}
        for relative, expected in FROZEN_READ_ONLY_DIGESTS.items():
            actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
            if actual != expected:
                drifted[relative] = actual
        self.assertEqual(drifted, {}, "只读依赖发生漂移，必须停止并与并行改动核对")

    def test_record_current_product_digests(self):
        for relative in CHANGED_PRODUCT_FILES:
            path = ROOT / relative
            self.assertTrue(path.is_file(), relative)
            print("CURRENT_SHA256 %s %s" % (
                relative, hashlib.sha256(path.read_bytes()).hexdigest()))
        # 本文件自身的摘要同样记录，供交付报告核对“改前 / 改后”SHA
        print("CURRENT_SHA256 %s %s" % (
            "tests/test_d4_portfolio_board.py",
            hashlib.sha256(Path(__file__).read_bytes()).hexdigest()))


if __name__ == "__main__":
    unittest.main()
