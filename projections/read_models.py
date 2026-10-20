"""读模型：把存储层数据投影成看板/查询需要的事实。

页面口径（接口文档 §5.2）：
- 只有 `submitted`、没有 `accepted_by_host` 时，状态是“已发出，等待对方确认”，不能显示“正在处理”。
- 交付通过只代表 cox 审核通过，不代表 QA、用户确认、Git 或发布完成。

D4 MVP 2.0（产品 §23）追加：总体驾驶舱与五层读模型
（`portfolio` / `projects` / `businessLines` / `tasks` / `workers`），见文件末尾
「D4 MVP 2.0 五层读模型」一节。事实分层要求：规划快照、历史资料、真实执行、
成员自报交付、独立审核必须带不同 `recordType` / `evidence` 标记，不互相冒充。
"""
from __future__ import annotations

import json
import sqlite3

from contracts.schemas import ContractError

_JSON_TASK_FIELDS = ("acceptance_json", "permissions_json", "limits_json", "closure_json")
_JSON_DELIVERY_FIELDS = ("changed_files_json", "verification_json", "risks_json", "unfinished_json")
_JSON_REPORT_FIELDS = (
    "source_card_json", "requested_by_json", "executed_by_json", "reviewed_by_json",
    "board_summary_json", "business_summary_json", "evidence_json",
    "source_file_json", "imported_by_json",
)

# D4-NEXT-01C：摘要缺失时的统一标记（沿用 §11 的 summary_pending 语义，不伪造摘要）
SUMMARY_PENDING = "summary_pending"
# 交付已带报告路径但还没有成功导入 -> “报告已生成，尚未回传”（不可用“已完成”冒充）
REPORT_GENERATED_NOT_IMPORTED = "generated_not_imported"

# ---------------------------------------------------------------------------
# D4 MVP 2.0（产品 §23 / 开发计划 §11 C）：五层对象与事实分层词汇
# ---------------------------------------------------------------------------

#: 五层对象键（顺序即页面阅读顺序）。项目不与任务平级；Worker 是独立登记对象。
LAYER_PORTFOLIO = "portfolio"
LAYER_PROJECTS = "projects"
LAYER_BUSINESS_LINES = "businessLines"
LAYER_TASKS = "tasks"
LAYER_WORKERS = "workers"
LAYER_ORDER = (LAYER_PORTFOLIO, LAYER_PROJECTS, LAYER_BUSINESS_LINES, LAYER_TASKS, LAYER_WORKERS)

# 事实分层：不同来源必须用不同 recordType 表示（§23.3）。任何一种都不许升级成另一种。
RECORD_PLANNING_SNAPSHOT = "planning_snapshot"
RECORD_PROJECT_STATUS = "project_status"
RECORD_HISTORICAL_ARCHIVE = "historical_archive"
RECORD_REAL_EXECUTION = "real_execution"
RECORD_SYSTEM_LEDGER = "system_ledger"
RECORD_SELF_REPORTED_DELIVERY = "self_reported_delivery"
RECORD_INDEPENDENT_REVIEW = "independent_review"
RECORD_WORKER_REGISTRY = "worker_registry"
RECORD_PROJECT = "project"
RECORD_PROJECT_UNREGISTERED = "project_unregistered"
RECORD_BUSINESS_LINE = "business_line"
RECORD_TASK = "task"
RECORD_MILESTONE = "milestone"
RECORD_UNKNOWN = "unknown"

RECORD_TYPE_LABELS = {
    RECORD_PLANNING_SNAPSHOT: "规划快照（计划中，未执行）",
    RECORD_PROJECT_STATUS: "项目/业务线登记状态（声明，不是执行证据）",
    RECORD_HISTORICAL_ARCHIVE: "历史资料登记（不代表重新执行）",
    RECORD_REAL_EXECUTION: "真实执行记录（账本事实）",
    RECORD_SYSTEM_LEDGER: "系统入账事实",
    RECORD_SELF_REPORTED_DELIVERY: "执行成员自报交付（未经独立审核）",
    RECORD_INDEPENDENT_REVIEW: "独立审核结论",
    RECORD_WORKER_REGISTRY: "成员登记",
    RECORD_PROJECT: "项目",
    RECORD_PROJECT_UNREGISTERED: "未登记项目",
    RECORD_BUSINESS_LINE: "业务线",
    RECORD_TASK: "任务",
    RECORD_MILESTONE: "里程碑",
    RECORD_UNKNOWN: "未知（资料不足，未做推断）",
}

# 证据等级：没有证据时必须显示 unverified/unknown，不许补造。
EVIDENCE_PLAN_ONLY = "plan_only"
EVIDENCE_DOCUMENT_ARCHIVE = "document_archive"
EVIDENCE_HOST_EVENT = "host_event"
EVIDENCE_SYNTHETIC = "synthetic_demo"
EVIDENCE_WORKER_REPORT = "worker_report"
EVIDENCE_REVIEW_RECORD = "review_record"
EVIDENCE_UNVERIFIED = "unverified"
EVIDENCE_UNKNOWN = "unknown"

EVIDENCE_LABELS = {
    EVIDENCE_PLAN_ONLY: "只有规划资料，没有执行证据",
    EVIDENCE_DOCUMENT_ARCHIVE: "历史资料登记",
    EVIDENCE_HOST_EVENT: "已在真实环境运行（宿主证据）",
    EVIDENCE_SYNTHETIC: "演示/本地核对数据",
    EVIDENCE_WORKER_REPORT: "执行成员自报，未经独立审核",
    EVIDENCE_REVIEW_RECORD: "独立审核记录",
    EVIDENCE_UNVERIFIED: "尚未验证（没有找到证据）",
    EVIDENCE_UNKNOWN: "无法判定（资料或表缺失）",
}

#: 任务记录类型（筛选用闭集）。
TASK_RECORD_TYPES = (
    RECORD_REAL_EXECUTION,
    RECORD_HISTORICAL_ARCHIVE,
    RECORD_PLANNING_SNAPSHOT,
)

#: 页面普通视图的中文状态（不懂技术也能读懂；技术细节另放 technical）。
PLAIN_STATE_LABELS = {
    "created": "已登记，尚未派出执行",
    "ready": "已登记，尚未派出执行",
    "submitted_pending_host": "已发出，等待对方确认",
    "accepted_by_host": "对方已收到",
    "in_progress": "执行成员正在处理",
    "blocked": "遇到问题，暂时无法继续",
    "delivered": "执行成员已交回结果",
    "awaiting_review": "等待负责人检查",
    "changes_requested": "需要补充或修改",
    "approved": "已检查通过",
    "failed": "处理失败",
    "cancelled": "已取消",
    "unverified": "尚未验证",
    "unregistered": "未登记",
    "planned": "计划中 · 未启动",
}

PLAIN_PROJECT_STATUS_LABELS = {
    "planned": "计划中 · 未启动",
    "in_progress": "开发中",
    "delivered": "已交付",
    "awaiting_review": "待审核",
    "approved": "审核通过",
    "verified": "已核验",
    "validated": "限定范围已验证",
    "partial": "部分完成",
    "blocked": "暂时受阻",
    "unverified": "尚未验证",
    "unregistered": "未登记",
    "archive": "历史资料登记",
}

PLAIN_ROLE_LABELS = {
    "sales-architect": "负责人",
    "skill-worker": "开发成员",
    "qa-reporter": "测试成员",
    "release-ops": "发布运维",
}

PLAIN_RUNTIME_LABELS = {
    "unknown": "未验证",
    "unverified": "未验证",
    "pending": "待核验",
    "ok": "最近检查通过",
    "checked": "最近检查通过",
    "verified": "已验证",
}

#: 里程碑门禁状态 -> 页面用词（结论优先看这些）。
GATE_STATE_LABELS = {
    "review_passed": "独立审核已通过",
    "delivered_pending_review": "已自报交付，待独立审核",
    "in_progress": "执行中（有宿主接收证据）",
    "submitted_pending_host": "已发出，等待对方确认",
    "document_only": "只有历史资料",
    "planned": "计划中 · 未启动",
    "blocked": "暂时受阻",
    "unverified": "尚未验证（没有找到证据）",
}

#: 普通视图「下一步做什么」（只看账本状态时用；不自动派发、不自动返工）。
NEXT_STEP_BY_STATE = {
    "created": "等待负责人派出或补充要求。",
    "ready": "等待负责人派出或补充要求。",
    "submitted_pending_host": "等待对方确认真实收到；没有接收证据前不重发。",
    "accepted_by_host": "等待执行成员开始处理并回传进展。",
    "in_progress": "等待执行成员回传进展或交付。",
    "blocked": "先确认阻塞原因和需要谁处理。",
    "delivered": "等待负责人检查这次交付。",
    "awaiting_review": "等待负责人检查这次交付。",
    "changes_requested": "执行成员按补充说明处理，完成后再次检查。",
    "approved": "确认是否需要独立测试或下一步安排（看板不自动派发）。",
    "failed": "确认失败原因后再决定是否重新派发。",
    "cancelled": "如仍需要，请重新提需求。",
    "unverified": "补齐执行或审核证据后再判断。",
}

#: 审核决定 -> 页面用词。approved 只代表独立审核通过，不代表发布。
REVIEW_DECISION_STATES = {
    "approved": ("approved", "独立审核已通过"),
    "changes_requested": ("changes_requested", "独立审核要求返工"),
    "blocked": ("blocked", "独立审核判定暂时无法继续"),
    "request_info": ("request_info", "独立审核要求补充信息"),
}


def _text(value):
    """只读展示用文本：去空白后为空即视为未提供（不补造）。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    return str(value)


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    return [value]


def _json_items(value):
    """把 JSON 文本列解码成列表；坏数据按空列表处理，不让页面崩。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return parsed
    return []


def _gate_text(value):
    items = [str(item) for item in _as_list(value) if _text(item)]
    return "；".join(items) if items else None


def review_state_label(reviews):
    """独立审核状态：没有审核记录就是「尚未独立审核」，绝不当成通过。"""
    if not reviews:
        return "not_reviewed", EVIDENCE_UNVERIFIED, "尚未独立审核"
    last = reviews[-1]
    decision = last.get("decision")
    if decision in REVIEW_DECISION_STATES:
        state, label = REVIEW_DECISION_STATES[decision]
        return state, EVIDENCE_REVIEW_RECORD, label
    return "recorded", EVIDENCE_REVIEW_RECORD, "已有审核记录，决定待核对"


def evidence_marker(evidence_type):
    """把账本里的 evidenceType 映射成页面证据等级（不倒推、不升级）。"""
    if evidence_type == "host":
        return EVIDENCE_HOST_EVENT
    if evidence_type == "synthetic":
        return EVIDENCE_SYNTHETIC
    return EVIDENCE_UNVERIFIED



def _decode(record, fields):
    if record is None:
        return None
    out = dict(record)
    for key in fields:
        value = out.get(key)
        if isinstance(value, str) and value:
            try:
                out[key] = json.loads(value)
            except ValueError:
                pass
    return out


class ReadModels:
    def __init__(self, store):
        self.store = store
        # 五层读模型用的表存在性探测缓存（只读；一次实例化只探测一次）
        self._tables = None

    # -- 派生状态 ---------------------------------------------------------
    @staticmethod
    def display_state(attempt):
        """把执行轮次映射成页面口径。缺少接收证据时绝不返回“执行中”。"""
        if attempt is None:
            return None
        if attempt.get("terminal_kind"):
            return attempt["terminal_kind"]
        if attempt.get("state") == "blocked":
            return "blocked"
        if attempt.get("accepted_by_host_at") is None:
            if attempt.get("submitted_at") is None:
                return "created"
            return "submitted_pending_host"
        if attempt.get("started_at"):
            return "in_progress"
        return "accepted_by_host"

    def attempt_view(self, attempt):
        if attempt is None:
            return None
        view = dict(attempt)
        view["displayState"] = self.display_state(attempt)
        view["acceptanceMissing"] = bool(
            attempt.get("submitted_at") and not attempt.get("accepted_by_host_at")
        )
        blocked = attempt.get("blocked_json")
        if isinstance(blocked, str) and blocked:
            try:
                view["blocked"] = json.loads(blocked)
            except ValueError:
                pass
        return view

    def evidence_type(self, task_id):
        submissions = [json.loads(row["detail_json"] or "{}")
                       for row in self.store.list_activity(task_id)
                       if row["kind"] == "attempt.submitted"]
        if not submissions:
            return "unconfirmed"
        if any(row.get("synthetic") is not False for row in submissions):
            return "synthetic"
        return "host"

    def task_display_state(self, task, attempt):
        if task["status"] in ("approved", "awaiting_review", "changes_requested", "blocked"):
            return task["status"]
        return self.display_state(attempt)

    # -- 报告摘要投影（D4-NEXT-01C） --------------------------------------
    @staticmethod
    def report_view(row):
        """报告记录 -> 受控技术详情（CoX 展开用；默认页面不展示这些内部字段）。"""
        if row is None:
            return None
        view = _decode(row, _JSON_REPORT_FIELDS)
        return {
            "reportId": row["report_id"],
            "reportRevision": row["report_revision"],
            "reportType": row["report_type"],
            "projectId": row["project_id"],
            "taskId": row["task_id"],
            "attemptId": row["attempt_id"],
            "deliveryId": row["delivery_id"],
            "adapter": row["adapter"],
            "sessionId": row["session_id"],
            "hostRunId": row["host_run_id"],
            "sourceCard": view.get("source_card_json") or {},
            "requestedBy": view.get("requested_by_json") or {},
            "executedBy": view.get("executed_by_json") or {},
            "reviewedBy": view.get("reviewed_by_json") or {},
            "boardSummary": view.get("board_summary_json") or {},
            "businessSummary": view.get("business_summary_json") or {},
            "evidence": view.get("evidence_json") or [],
            "canonicalDigest": row["canonical_digest"],
            "sourceFile": view.get("source_file_json") or {},
            "importedAt": row["imported_at"],
            "importedBy": view.get("imported_by_json") or {},
            "synthetic": bool(row["synthetic"]),
        }

    def latest_report_row(self, task_id, delivery_id):
        if not task_id or not delivery_id:
            return None
        rows = self.store.list_reports(task_id, delivery_id)
        return rows[-1] if rows else None

    @staticmethod
    def summary_view(delivery, report_row):
        """普通看板摘要：有报告给 fields，没有只标记 summary_pending，不编造内容。"""
        if report_row is None:
            return {
                "pending": True,
                "code": SUMMARY_PENDING,
                "status": (
                    REPORT_GENERATED_NOT_IMPORTED
                    if delivery and delivery.get("report_path")
                    else "none"
                ),
                "fields": {},
            }
        report = ReadModels.report_view(report_row)
        return {
            "pending": False,
            "code": None,
            "status": "imported",
            "fields": dict(report["boardSummary"]),
        }

    def _current_summary(self, task):
        current_delivery = None
        if task.get("current_delivery_id"):
            current_delivery = self.store.get_delivery(task["current_delivery_id"])
        if current_delivery is not None:
            current_delivery = _decode(current_delivery, _JSON_DELIVERY_FIELDS)
        row = self.latest_report_row(task.get("task_id"), task.get("current_delivery_id"))
        view = self.summary_view(current_delivery, row)
        view["report"] = self.report_view(row)
        return view

    # -- 投影 -------------------------------------------------------------
    def task_detail(self, task_id):
        task = self.store.get_task(task_id)
        if task is None:
            raise ContractError(
                "invalid_task", message="任务不存在：%s" % task_id, details={"taskId": task_id}
            )
        attempt = self.store.get_attempt(task["current_attempt_id"]) if task["current_attempt_id"] else None
        deliveries = []
        for row in self.store.list_deliveries(task_id):
            item = _decode(row, _JSON_DELIVERY_FIELDS)
            item["isCurrent"] = row["delivery_id"] == task["current_delivery_id"]
            delivery_report = self.latest_report_row(task_id, row["delivery_id"])
            item["summary"] = self.summary_view(item, delivery_report)
            item["report"] = self.report_view(delivery_report)
            deliveries.append(item)
        task_view = _decode(task, _JSON_TASK_FIELDS)
        agent = self.store.get_agent(task.get("agent_id")) if task.get("agent_id") else None
        source = self.store.get_source(task.get("source_id")) if task.get("source_id") else None
        task_view["agentLabel"] = agent.get("label") if agent else None
        task_view["sourceLabel"] = source.get("label") if source else None
        project_id = task.get("project_id")
        project = self.project_layer(project_id) if project_id else None
        link = self._first(self._link_rows(), "task_id", task_id)
        card = self._task_item(
            task, link,
            {row["worker_id"]: row for row in self._worker_rows()},
            [row for row in self._workstream_rows() if row.get("project_id") == project_id],
            [row for row in self._milestone_rows() if row.get("project_id") == project_id],
        )
        review_state, review_evidence, review_label = review_state_label(
            [dict(row) for row in self.store.list_reviews(task_id)]
        )
        return {
            "task": task_view,
            "evidenceType": self.evidence_type(task_id),
            "currentAttempt": self.attempt_view(attempt),
            "attempts": [self.attempt_view(row) for row in self.store.list_attempts(task_id)],
            "displayState": self.display_state(attempt),
            "taskDisplayState": self.task_display_state(task, attempt),
            # D4-NEXT-01C：普通摘要（当前交付）+ CoX 技术详情（可能为空）
            "summary": self._current_summary(task),
            "deliveries": deliveries,
            "reports": [self.report_view(row) for row in self.store.list_reports(task_id)],
            "reviews": [dict(r) for r in self.store.list_reviews(task_id)],
            "activity": [dict(a) for a in self.store.list_activity(task_id)],
            "history": [dict(r) for r in self.store.list_reconciliation(task_id)],
            "conflicts": [dict(c) for c in self.store.list_conflicts(task_id)],
            # D4 MVP 2.0 §23：层级归属、事实分层与普通/技术视图分离
            "layer": LAYER_TASKS,
            "recordType": RECORD_REAL_EXECUTION,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_REAL_EXECUTION],
            "breadcrumb": self.task_breadcrumb(task, project),
            "businessLine": self._task_business_line(task),
            "worker": self._task_worker_view(task),
            "independentReview": {
                "recordType": RECORD_INDEPENDENT_REVIEW,
                "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_INDEPENDENT_REVIEW],
                "state": review_state,
                "label": review_label,
                "evidence": review_evidence,
            },
            "evidence": card["evidence"],
            "evidenceLabel": card["evidenceLabel"],
            "stateLabel": card["stateLabel"],
            "plainSummary": card["plainSummary"],
            "technical": card["technical"],
            "unverified": card["unverified"],
        }

    def task_breadcrumb(self, task, project=None):
        """面包屑：总体驾驶舱 → 项目 → 业务线 → 任务。项目与任务不平级。"""
        project_id = (task or {}).get("project_id")
        if project is None and project_id:
            project = self.project_layer(project_id)
        link = self._first(self._link_rows(), "task_id", (task or {}).get("task_id"))
        workstream_id = (link or {}).get("workstream_id")
        workstream = None
        if workstream_id:
            workstream = self._first(
                [row for row in self._workstream_rows() if row.get("project_id") == project_id],
                "workstream_id", workstream_id,
            )
        crumbs = [{"layer": LAYER_PORTFOLIO, "id": None, "label": "总体驾驶舱"},
                  {"layer": LAYER_PROJECTS, "id": project_id,
                   "label": (project or {}).get("name") or project_id}]
        if workstream_id:
            crumbs.append({"layer": LAYER_BUSINESS_LINES, "id": workstream_id,
                           "label": (workstream or {}).get("name") or workstream_id})
        crumbs.append({"layer": LAYER_TASKS, "id": (task or {}).get("task_id"),
                       "label": (task or {}).get("title")})
        return crumbs

    def _task_business_line(self, task):
        link = self._first(self._link_rows(), "task_id", task.get("task_id"))
        workstream_id = (link or {}).get("workstream_id")
        if not workstream_id:
            return {"businessLineId": None, "name": None, "known": False,
                    "note": "没有业务线归属记录。"}
        workstream = self._first(
            [row for row in self._workstream_rows() if row.get("project_id") == task.get("project_id")],
            "workstream_id", workstream_id,
        )
        return {
            "businessLineId": workstream_id,
            "name": (workstream or {}).get("name"),
            "known": workstream is not None,
            "note": None if workstream is not None else "任务指向的业务线未登记，按未登记显示。",
        }

    def _task_worker_view(self, task):
        link = self._first(self._link_rows(), "task_id", task.get("task_id"))
        worker, source_kind = self._resolve_worker(
            task, link, {row["worker_id"]: row for row in self._worker_rows()}
        )
        if worker is None:
            return {"workerId": None, "label": "未登记执行成员", "known": False,
                    "linkSource": source_kind,
                    "note": "没有找到与任务匹配的成员登记，不按名称近似匹配。"}
        view = self.worker_view(worker)
        view["known"] = True
        view["linkSource"] = source_kind
        return view

    def project_summary_legacy(self, project_id):
        """旧版项目任务清单（保留给需要最小字段的调用方；不再是看板主路径）。"""
        items = []
        for task in self.store.list_tasks(project_id):
            attempt = self.store.get_attempt(task["current_attempt_id"]) if task["current_attempt_id"] else None
            items.append({
                "taskId": task["task_id"],
                "title": task["title"],
                "status": task["status"],
                "agentId": task["agent_id"],
                "agentLabel": (self.store.get_agent(task["agent_id"]) or {}).get("label")
                if task["agent_id"] else None,
                "sourceLabel": (self.store.get_source(task["source_id"]) or {}).get("label")
                if task["source_id"] else None,
                "currentAttemptId": task["current_attempt_id"],
                "displayState": self.display_state(attempt),
                "taskDisplayState": self.task_display_state(task, attempt),
                "evidenceType": self.evidence_type(task["task_id"]),
                "summary": self._current_summary(task),
            })
        return {"projectId": project_id, "tasks": items}

    def projects(self):
        return {"projects": self.projects_layer()["projects"]}

    def review_queue(self):
        items = []
        for task in self.store.list_tasks():
            if task["status"] != "awaiting_review":
                continue
            delivery = (
                self.store.get_delivery(task["current_delivery_id"])
                if task["current_delivery_id"] else None
            )
            items.append({
                "taskId": task["task_id"],
                "title": task["title"],
                "agentId": task["agent_id"],
                "agentLabel": (self.store.get_agent(task["agent_id"]) or {}).get("label")
                if task["agent_id"] else None,
                "currentDeliveryId": task["current_delivery_id"],
                "delivery": _decode(delivery, _JSON_DELIVERY_FIELDS),
            })
        return {"items": items}

    def task_history(self, task_id):
        return {
            "taskId": task_id,
            "activity": [dict(a) for a in self.store.list_activity(task_id)],
            "reconciliation": [dict(r) for r in self.store.list_reconciliation(task_id)],
        }

    # =======================================================================
    # D4 MVP 2.0 五层读模型（产品 §23 / 开发计划 §11 C）
    #
    # 只读投影：本段只做 SELECT，不写任何表、不调用宿主、不派发任务。
    # 五层 = portfolio / projects / businessLines / tasks / workers；
    # 项目不与任务平级，任务用稳定 taskId 归属到项目与业务线，跨项目不合并；
    # Worker 是独立登记对象，靠 task link 或稳定 ID 相等建立归属（不按名称猜）。
    # =======================================================================

    # -- 基础设施 -----------------------------------------------------------
    _WORKSPACE_TABLES = (
        "workspace_projects", "workspace_workstreams", "workspace_milestones",
        "workspace_workers", "workspace_task_links", "workspace_plan_sources",
    )
    _PLANNING_TABLES = ("planning_sources", "planning_snapshots")

    def table_names(self):
        """当前库的表名集合（只读探测，用于旧库缺表时安全降级）。"""
        if self._tables is None:
            try:
                rows = self.store.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            except sqlite3.Error:
                rows = []
            self._tables = {str(row[0]) for row in rows}
        return self._tables

    def has_table(self, name):
        return name in self.table_names()

    def layer_availability(self):
        """五层数据来源可用性；缺表时明确标 False，不把「缺表」当成「空」。"""
        availability = {}
        for name in self._WORKSPACE_TABLES + self._PLANNING_TABLES:
            availability[name] = self.has_table(name)
        return availability

    def _rows(self, table, order_by=None, where=None, params=()):
        if not self.has_table(table):
            return []
        sql = "SELECT * FROM %s" % table
        if where:
            sql += " WHERE " + where
        if order_by:
            sql += " ORDER BY " + order_by
        try:
            return [dict(row) for row in self.store.conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []

    @staticmethod
    def _first(rows, key, value):
        if value is None:
            return None
        for row in rows or ():
            if row.get(key) == value:
                return row
        return None

    # -- 项目 / 业务线 / 里程碑 / 成员 原始行 -------------------------------
    def _project_rows(self):
        return self._rows("workspace_projects", "created_at, project_id")

    def _link_rows(self):
        return self._rows("workspace_task_links", "task_id")

    def _worker_rows(self):
        return self._rows("workspace_workers", "worker_id")

    def _workstream_rows(self):
        return self._rows("workspace_workstreams", "project_id, position, workstream_id")

    def _milestone_rows(self):
        return self._rows("workspace_milestones", "project_id, position, milestone_id")

    def _planning_snapshot(self, project_id):
        """项目最新规划快照的只读视图；无快照或表缺失返回 None。

        规划快照只作计划基线，永不升级成执行、交付或审核事实。
        """
        rows = [row for row in self._rows("planning_snapshots", "snapshot_revision, snapshot_id")
                if row.get("project_id") == project_id]
        if not rows:
            return None
        row = rows[-1]
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        source = self._first(
            [item for item in self._rows("planning_sources", "planning_source_id")
             if item.get("project_id") == project_id],
            "planning_source_id", row.get("planning_source_id"),
        ) or {}
        path = _text(source.get("source_path"))
        return {
            "snapshotId": row.get("snapshot_id"),
            "snapshotRevision": row.get("snapshot_revision"),
            "status": row.get("status"),
            "documentVersion": row.get("document_version"),
            "sourceSha256": row.get("source_sha256"),
            "importedAt": row.get("imported_at"),
            "planningSourceId": row.get("planning_source_id"),
            "sourceKind": source.get("source_kind"),
            "sourcePath": path,
            "sourceName": path.rsplit("/", 1)[-1] if path else None,
            "payload": payload,
        }

    def _plan_source_rows(self, project_id):
        return [row for row in self._rows("workspace_plan_sources", "imported_at, source_id")
                if row.get("project_id") == project_id]

    # -- 任务视图 -----------------------------------------------------------
    def _resolve_worker(self, task, link, workers):
        """成员归属：优先 task link，其次稳定 ID 相等；都不匹配返回 None。"""
        if link and _text(link.get("worker_id")):
            return workers.get(link["worker_id"]), "task_link"
        agent_id = _text(task.get("agent_id"))
        if agent_id and agent_id in workers:
            return workers[agent_id], "agent_id"
        return None, "unresolved"

    def _task_facts(self, attempt, deliveries, reviews, report_row):
        accepted = bool(attempt and attempt.get("accepted_by_host_at"))
        submitted = bool(attempt and attempt.get("submitted_at"))
        review_state, _evidence, _label = review_state_label(reviews)
        return {
            "hasAttempt": bool(attempt),
            "submittedPendingHost": bool(submitted and not accepted),
            "hostAccepted": accepted,
            "hasDelivery": bool(deliveries),
            "hasReport": report_row is not None,
            "reviewState": review_state,
            "reviewPassed": review_state == "approved",
        }

    @staticmethod
    def _attempt_blocked_text(attempt):
        if not attempt:
            return None
        blocked = attempt.get("blocked")
        if blocked is None:
            raw = attempt.get("blocked_json")
            if isinstance(raw, str) and raw.strip():
                try:
                    blocked = json.loads(raw)
                except ValueError:
                    blocked = None
        if isinstance(blocked, dict):
            parts = [_text(blocked.get(key)) for key in ("summary", "reason", "detail")]
            parts = [part for part in parts if part]
            return "；".join(parts) or None
        if isinstance(blocked, str):
            return _text(blocked)
        return None

    def _plain_summary(self, task, state, summary, current_delivery, attempt, report_row):
        """普通视图三问：已经做了什么 / 现在遇到什么问题 / 下一步做什么。

        只用已上报事实；没有上报就写「尚未上报」，不用状态词补齐。
        """
        fields = summary.get("fields") or {}
        stage, result = _text(fields.get("stage")), _text(fields.get("result"))
        if not summary.get("pending") and (stage or result):
            if stage and result and result != stage:
                done = "%s；%s" % (stage, result)
            else:
                done = stage or result
            return {
                "source": RECORD_SELF_REPORTED_DELIVERY,
                "sourceLabel": RECORD_TYPE_LABELS[RECORD_SELF_REPORTED_DELIVERY],
                "done": done,
                "problem": _text(fields.get("problem")) or "尚未上报问题",
                "nextStep": _text(fields.get("nextStep")) or "等待下一步安排",
                "pending": False,
            }
        if current_delivery is not None and _text(current_delivery.get("summary")):
            risks = [item for item in (_text(v) for v in _as_list(current_delivery.get("risks_json"))) if item]
            unfinished = [item for item in (_text(v) for v in _as_list(current_delivery.get("unfinished_json"))) if item]
            problem = "；".join(risks) or "；".join(unfinished) or "尚未上报问题"
            if unfinished:
                next_step = "；".join(unfinished)
            elif state in ("delivered", "awaiting_review"):
                next_step = "等待负责人检查这次结果"
            elif state == "approved":
                next_step = "确认是否需要独立测试或下一步安排（看板不自动派发）"
            else:
                next_step = "等待执行成员回执"
            return {
                "source": RECORD_SELF_REPORTED_DELIVERY,
                "sourceLabel": RECORD_TYPE_LABELS[RECORD_SELF_REPORTED_DELIVERY],
                "done": _text(current_delivery.get("summary")),
                "problem": problem,
                "nextStep": next_step,
                "pending": False,
            }
        blocked = self._attempt_blocked_text(attempt)
        if blocked is None and state == "blocked":
            blocked = "执行成员报告遇到问题（未提供文字说明）"
        return {
            "source": "status_only",
            "sourceLabel": "只有账本状态，尚未收到执行成员摘要",
            "done": PLAIN_STATE_LABELS.get(state) or "状态待确认",
            "problem": blocked or ("尚未收到执行成员的结构化摘要" if attempt is None else "尚未上报问题"),
            "nextStep": NEXT_STEP_BY_STATE.get(state, "等待执行成员回执"),
            "pending": True,
        }

    def _task_technical(self, task, attempt, deliveries, report_row, reviews):
        """技术详情（默认折叠）：路径、摘要、命令、原始报错和内部编号。"""
        task_id = task["task_id"]
        paths, digests, changed_files, commands, raw_errors, claimed = [], [], [], [], [], []
        workspace = _text(task.get("workspace"))
        if workspace:
            paths.append(workspace)
        for delivery in deliveries:
            report_path = _text(delivery.get("report_path"))
            if report_path and report_path not in paths:
                paths.append(report_path)
            for item in _as_list(delivery.get("changed_files_json")):
                text = _text(item)
                if text and text not in changed_files:
                    changed_files.append(text)
            for item in _as_list(delivery.get("verification_json")):
                text = _text(item)
                if text and text not in claimed:
                    claimed.append(text)
        if report_row is not None:
            digest = _text(report_row.get("canonical_digest"))
            if digest:
                digests.append({"kind": "报告内容摘要", "value": digest})
            for item in _json_items(report_row.get("evidence_json")):
                if not isinstance(item, dict):
                    continue
                kind, detail = item.get("kind"), _text(item.get("detail"))
                if kind == "command" and detail:
                    commands.append(detail)
                elif kind == "raw-error" and detail:
                    raw_errors.append(detail)
        source_card = self.store.get_source_card(task_id) if self.store.has_source_cards_table() else None
        if source_card is not None:
            sha = _text(source_card.get("source_sha256"))
            if sha:
                digests.append({"kind": "来源任务卡文件摘要", "value": sha})
        for row in self.store.list_activity(task_id):
            if row.get("kind") != "attempt.submitted":
                continue
            try:
                detail = json.loads(row.get("detail_json") or "{}")
            except ValueError:
                detail = {}
            if isinstance(detail, dict):
                for key in ("error", "rawError", "message"):
                    text = _text(detail.get(key))
                    if text and text not in raw_errors:
                        raw_errors.append(text)
        for row in self.store.list_conflicts(task_id):
            text = _text(row.get("detail_json"))
            if text:
                raw_errors.append(text)
        return {
            "recordIds": {
                "taskId": task_id,
                "sourceId": task.get("source_id"),
                "currentAttemptId": task.get("current_attempt_id"),
                "attemptIds": [row["attempt_id"] for row in self.store.list_attempts(task_id)],
                "currentDeliveryId": task.get("current_delivery_id"),
                "deliveryIds": [row["delivery_id"] for row in deliveries],
                "reportId": report_row["report_id"] if report_row is not None else None,
                "reportRevision": report_row["report_revision"] if report_row is not None else None,
                "hostRunId": report_row["host_run_id"] if report_row is not None else None,
                "sessionId": report_row["session_id"] if report_row is not None else None,
                "reviewIds": [row.get("review_id") for row in reviews],
            },
            "paths": paths,
            "changedFiles": changed_files,
            "digests": digests,
            "commands": commands,
            "claimedVerification": claimed,
            "rawErrors": raw_errors,
            "notes": ["技术详情只给负责人核对；普通视图不显示路径、摘要、命令和原始报错。"],
        }

    def _task_item(self, task, link=None, workers=None, workstreams=None, milestones=None):
        task_id = task["task_id"]
        workers = workers if workers is not None else {}
        attempt = self.store.get_attempt(task["current_attempt_id"]) if task.get("current_attempt_id") else None
        agent = self.store.get_agent(task["agent_id"]) if task.get("agent_id") else None
        source = self.store.get_source(task["source_id"]) if task.get("source_id") else None
        reviews = [dict(row) for row in self.store.list_reviews(task_id)]
        deliveries = [_decode(row, _JSON_DELIVERY_FIELDS) for row in self.store.list_deliveries(task_id)]
        report_row = self.latest_report_row(task_id, task.get("current_delivery_id"))
        summary = self._current_summary(task)
        evidence_type = self.evidence_type(task_id)
        state = self.task_display_state(task, attempt)
        current_delivery = self._first(deliveries, "delivery_id", task.get("current_delivery_id"))
        if current_delivery is None and deliveries:
            current_delivery = deliveries[-1]
        worker, worker_source = self._resolve_worker(task, link, workers)
        workstream = self._first(workstreams or [], "workstream_id", (link or {}).get("workstream_id"))
        milestone = self._first(milestones or [], "milestone_id", (link or {}).get("milestone_id"))
        review_state, review_evidence, review_label = review_state_label(reviews)
        facts = self._task_facts(attempt, deliveries, reviews, report_row)
        evidence = evidence_marker(evidence_type)
        unverified = []
        if attempt is None:
            unverified.append({"item": "执行轮次", "evidence": EVIDENCE_UNVERIFIED, "note": "尚未建立执行轮次"})
        if evidence == EVIDENCE_UNVERIFIED:
            unverified.append({"item": "宿主接收", "evidence": EVIDENCE_UNVERIFIED, "note": "没有找到宿主接收或运行证据"})
        if review_state == "not_reviewed":
            unverified.append({"item": "独立审核", "evidence": EVIDENCE_UNVERIFIED, "note": "尚未独立审核"})
        return {
            # 兼容旧字段（页面与 CLI 继续可用）
            "taskId": task_id,
            "title": task["title"],
            "status": task["status"],
            "agentId": task.get("agent_id"),
            "agentLabel": (agent or {}).get("label"),
            "sourceLabel": (source or {}).get("label"),
            "currentAttemptId": task.get("current_attempt_id"),
            "displayState": self.display_state(attempt),
            "taskDisplayState": state,
            "evidenceType": evidence_type,
            "summary": summary,
            # 五层归属（项目 / 业务线 / 里程碑 / Worker）
            "layer": LAYER_TASKS,
            "projectId": task.get("project_id"),
            "businessLineId": (link or {}).get("workstream_id"),
            "businessLineName": (workstream or {}).get("name"),
            "businessLineKnown": workstream is not None,
            "milestoneId": (link or {}).get("milestone_id"),
            "milestoneName": (milestone or {}).get("name"),
            "workerId": (worker or {}).get("worker_id"),
            "workerLabel": self.worker_plain_label(worker) if worker else "未登记执行成员",
            "workerKnown": worker is not None,
            "workerLinkSource": worker_source,
            "hostLabel": _text((worker or {}).get("host")) or "未登记宿主",
            # 事实分层（§23.3）：真实执行 + 系统入账 + 自报 + 独立审核分开
            "recordType": RECORD_REAL_EXECUTION,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_REAL_EXECUTION],
            "ledger": {
                "recordType": RECORD_SYSTEM_LEDGER,
                "recorded": True,
                "note": "任务与事件由受控入口入账；入账不代表执行成功、审核通过或发布完成。",
            },
            "workerReport": {
                "recordType": RECORD_SELF_REPORTED_DELIVERY,
                "state": ("delivered" if deliveries else "none"),
                "note": ("执行成员自报交付，尚未等于独立审核通过。" if deliveries
                         else "尚未收到执行成员交付。"),
            },
            "reviewRecord": {
                "recordType": RECORD_INDEPENDENT_REVIEW,
                "state": review_state,
                "label": review_label,
                "evidence": review_evidence,
                "count": len(reviews),
                "note": "独立审核结论与成员自报完成分开保存和显示。",
            },
            "evidence": evidence,
            "evidenceLabel": EVIDENCE_LABELS[evidence],
            "evidenceTypeRaw": evidence_type,
            "stateLabel": PLAIN_STATE_LABELS.get(state) or "状态待确认",
            "plainSummary": self._plain_summary(task, state, summary, current_delivery, attempt, report_row),
            "facts": facts,
            "technical": self._task_technical(task, attempt, deliveries, report_row, reviews),
            "unverified": unverified,
        }

    # -- Worker 视图 --------------------------------------------------------
    @staticmethod
    def worker_plain_label(worker):
        if not worker:
            return "未登记执行成员"
        name = _text(worker.get("display_name"))
        role = PLAIN_ROLE_LABELS.get(_text(worker.get("role")))
        if name and role:
            return "%s（%s）" % (name, role)
        return name or role or _text(worker.get("worker_id")) or "未登记执行成员"

    def worker_view(self, row, project_ids=None):
        enabled = bool(row.get("enabled"))
        runtime = _text(row.get("runtime_status")) or "unknown"
        registration = _text(row.get("registration_status")) or "unknown"
        return {
            "layer": LAYER_WORKERS,
            "workerId": row.get("worker_id"),
            "displayName": row.get("display_name"),
            "label": self.worker_plain_label(row),
            "role": row.get("role"),
            "roleLabel": PLAIN_ROLE_LABELS.get(_text(row.get("role")), _text(row.get("role")) or "未登记角色"),
            "host": row.get("host"),
            "environment": row.get("environment"),
            "description": _text(row.get("description")),
            "defaultModel": _text(row.get("default_model")) or None,
            "modelNote": ("默认模型为宿主默认策略，不代表已固定某个模型。"
                          if _text(row.get("default_model")) in (None, "host-default")
                          else "登记为固定模型策略，实际模型仍需真实回执核对。"),
            "registrationStatus": registration,
            "registrationLabel": {"registered": "已登记", "active": "已登记", "disabled": "已停用",
                                  "retired": "已停用"}.get(registration, "登记状态待核对（%s）" % registration),
            "runtimeStatus": runtime,
            "runtimeLabel": PLAIN_RUNTIME_LABELS.get(runtime, "待核对（%s）" % runtime),
            "enabled": enabled,
            "acceptingTasks": enabled,
            "acceptingLabel": "可接任务（按登记启用）" if enabled else "暂停接单或未启用",
            "allowedProjects": _json_items(row.get("allowed_projects_json")),
            "readScope": _json_items(row.get("read_scope_json")),
            "writeScope": _json_items(row.get("write_scope_json")),
            "bindingRevision": row.get("binding_revision"),
            "lastCheckedAt": row.get("last_checked_at"),
            "updatedAt": row.get("updated_at"),
            "projects": list(project_ids or []),
            "recordType": RECORD_WORKER_REGISTRY,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_WORKER_REGISTRY],
            "evidence": EVIDENCE_UNKNOWN if runtime in ("unknown", "unverified", None) else EVIDENCE_UNVERIFIED,
            "notes": ["登记状态不等于在线、不等于已验证指定的成员路由。"],
        }

    def _worker_participation(self):
        participation = {}
        for link in self._link_rows():
            worker_id = _text(link.get("worker_id"))
            if not worker_id:
                continue
            projects = participation.setdefault(worker_id, [])
            if link.get("project_id") and link["project_id"] not in projects:
                projects.append(link["project_id"])
        for task in self.store.list_tasks():
            agent_id = _text(task.get("agent_id"))
            if not agent_id:
                continue
            projects = participation.setdefault(agent_id, [])
            if task.get("project_id") and task["project_id"] not in projects:
                projects.append(task["project_id"])
        return participation

    def workers_layer(self):
        available = self.has_table("workspace_workers")
        participation = self._worker_participation()
        rows = self._worker_rows()
        workers = [self.worker_view(row, participation.get(row.get("worker_id"), [])) for row in rows]
        registered = {row.get("worker_id") for row in rows}
        unknown = sorted(item for item in participation if item not in registered)
        notes = (["该数据库没有成员登记表（workspace_workers），成员信息一律显示未知。"]
                 if not available else
                 ["已登记不等于在线；接单资格、宿主和模型验证状态分别显示。"])
        if unknown:
            notes.append("有任务指向未登记的成员编号，已按未登记显示，不做身份推断。")
        return {
            "layer": LAYER_WORKERS,
            "recordType": RECORD_WORKER_REGISTRY,
            "available": available,
            "workers": workers,
            "counts": {
                "total": len(workers),
                "acceptingTasks": sum(1 for item in workers if item["acceptingTasks"]),
                "verified": sum(1 for item in workers
                                if item["runtimeStatus"] not in ("unknown", "unverified", None)),
            },
            "unknownWorkers": unknown,
            "notes": notes,
        }

    # -- 业务线 / 里程碑 / 计划基线 -----------------------------------------
    @staticmethod
    def _plan_section_title(payload, section_ref):
        for item in payload.get("sections") or []:
            if isinstance(item, dict) and item.get("sectionId") == section_ref:
                return _text(item.get("title"))
        return None

    @staticmethod
    def _plan_stage_title(payload, stage_ref):
        for item in payload.get("stages") or []:
            if isinstance(item, dict) and item.get("stageId") == stage_ref:
                return _text(item.get("title"))
        return None

    def _plan_business_lines(self, snapshot, project_id):
        payload = (snapshot or {}).get("payload") or {}
        items = []
        for item in payload.get("businessLines") or []:
            if not isinstance(item, dict):
                continue
            section_ref = _text(item.get("sectionRef"))
            stage_ref = _text(item.get("stageRef"))
            items.append({
                "layer": LAYER_BUSINESS_LINES,
                "businessLineId": _text(item.get("businessLineId")),
                "projectId": project_id,
                "name": _text(item.get("title")),
                "goal": None,
                "status": _text(item.get("planStatus")),
                "statusLabel": "计划状态：%s" % (_text(item.get("planStatus")) or "未标注"),
                "statusSource": "plan",
                "sectionRef": section_ref,
                "sectionTitle": self._plan_section_title(payload, section_ref),
                "stageRef": stage_ref,
                "stageTitle": self._plan_stage_title(payload, stage_ref),
                "sourceDocument": (snapshot or {}).get("sourceName"),
                "sourceSection": section_ref,
                "nextStep": None,
                "milestoneIds": [m.get("milestoneId") for m in (payload.get("milestones") or [])
                                 if isinstance(m, dict) and m.get("businessLineRef") == item.get("businessLineId")],
                "taskIds": [],
                "workerIds": [],
                "gates": [],
                "recordType": RECORD_PLANNING_SNAPSHOT,
                "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_PLANNING_SNAPSHOT],
                "evidence": EVIDENCE_PLAN_ONLY,
                "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_PLAN_ONLY],
                "tasks": [],
                "plannedOnly": True,
            })
        return items

    def _plan_gate_views(self, snapshot, project_id, known_ids):
        payload = (snapshot or {}).get("payload") or {}
        gates = []
        for item in payload.get("milestones") or []:
            if not isinstance(item, dict):
                continue
            milestone_id = _text(item.get("milestoneId"))
            if milestone_id and milestone_id in known_ids:
                continue
            section_ref = _text(item.get("sectionRef"))
            gates.append({
                "layer": LAYER_PROJECTS,
                "gateId": milestone_id,
                "projectId": project_id,
                "name": _text(item.get("title")),
                "gateCriteria": _gate_text(item.get("gateCriteria")),
                "state": "planned",
                "stateLabel": "计划中 · 未启动",
                "evidence": EVIDENCE_PLAN_ONLY,
                "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_PLAN_ONLY],
                "recordType": RECORD_PLANNING_SNAPSHOT,
                "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_PLANNING_SNAPSHOT],
                "declaredStatus": _text(item.get("planStatus")),
                "declaredStatusLabel": "计划状态：%s" % (_text(item.get("planStatus")) or "未标注"),
                "businessLineId": _text(item.get("businessLineRef")),
                "businessLineName": None,
                "sectionRef": section_ref,
                "sectionTitle": self._plan_section_title(payload, section_ref),
                "sourceDocument": (snapshot or {}).get("sourceName"),
                "taskIds": [],
                "note": "这是规划快照里的计划门禁，不代表已满足或已派发。",
            })
        return gates

    @staticmethod
    def _gate_state(linked_tasks):
        if any(item["facts"]["reviewPassed"] for item in linked_tasks):
            return "review_passed", EVIDENCE_REVIEW_RECORD, "独立审核已通过"
        if any(item["facts"]["hasDelivery"] for item in linked_tasks):
            return "delivered_pending_review", EVIDENCE_WORKER_REPORT, "执行成员已自报交付，独立审核尚未通过"
        if any(item["facts"]["hostAccepted"] for item in linked_tasks):
            return "in_progress", EVIDENCE_HOST_EVENT, "已有宿主接收证据，仍在推进"
        if any(item["facts"]["submittedPendingHost"] for item in linked_tasks):
            return "submitted_pending_host", EVIDENCE_UNVERIFIED, "已发出，但缺少宿主接收确认"
        return "unverified", EVIDENCE_UNVERIFIED, "没有执行或审核证据；登记状态只是声明"

    def _gate_views(self, milestones, task_items, snapshot, project_id, workstreams, plan_gates):
        gates = []
        known = set()
        for row in milestones:
            milestone_id = row.get("milestone_id")
            known.add(milestone_id)
            linked = [item for item in task_items if item.get("milestoneId") == milestone_id]
            state, evidence, note = self._gate_state(linked)
            declared = _text(row.get("status"))
            workstream = self._first(workstreams, "workstream_id", row.get("workstream_id"))
            gates.append({
                "layer": LAYER_PROJECTS,
                "gateId": milestone_id,
                "projectId": project_id,
                "name": row.get("name"),
                "gateCriteria": _gate_text(row.get("gate")),
                "state": state,
                "stateLabel": GATE_STATE_LABELS.get(state, "状态待确认"),
                "evidence": evidence,
                "evidenceLabel": EVIDENCE_LABELS[evidence],
                "recordType": RECORD_PROJECT_STATUS,
                "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_PROJECT_STATUS],
                "declaredStatus": declared,
                "declaredStatusLabel": PLAIN_PROJECT_STATUS_LABELS.get(declared, declared or "未登记状态"),
                "businessLineId": row.get("workstream_id"),
                "businessLineName": (workstream or {}).get("name"),
                "sectionRef": (workstream or {}).get("source_section"),
                "sectionTitle": None,
                "sourceDocument": None,
                "taskIds": [item["taskId"] for item in linked],
                "note": note,
            })
        gates.extend(plan_gates)
        return gates

    # -- 项目视图 -----------------------------------------------------------
    def _project_ids(self, project_id=None):
        registered = [row["project_id"] for row in self._project_rows()]
        derived = []
        for task in self.store.list_tasks():
            pid = task.get("project_id")
            if pid and pid not in registered and pid not in derived:
                derived.append(pid)
        ordered = registered + sorted(derived)
        if project_id is None:
            return ordered
        return [project_id] if project_id in ordered else [project_id]

    def _bundle(self, project_id):
        """一次取齐某项目的只读事实，保证同一层内各部分来自同一批查询。"""
        links = {row["task_id"]: row for row in self._link_rows()}
        workers = {row["worker_id"]: row for row in self._worker_rows()}
        workstreams = [row for row in self._workstream_rows() if row.get("project_id") == project_id]
        milestones = [row for row in self._milestone_rows() if row.get("project_id") == project_id]
        snapshot = self._planning_snapshot(project_id)
        tasks = self.store.list_tasks(project_id)
        task_items = [self._task_item(task, links.get(task["task_id"]), workers, workstreams, milestones)
                      for task in tasks]
        plan_gates = self._plan_gate_views(snapshot, project_id,
                                          {row.get("milestone_id") for row in milestones})
        return {
            "projectId": project_id,
            "project": self._first(self._project_rows(), "project_id", project_id),
            "tasks": tasks,
            "taskItems": task_items,
            "links": links,
            "workers": workers,
            "workstreams": workstreams,
            "milestones": milestones,
            "snapshot": snapshot,
            "planGates": plan_gates,
        }

    @staticmethod
    def _derive_project_status(task_items):
        if not task_items:
            return None
        if any(item["facts"]["reviewPassed"] for item in task_items):
            return "approved"
        if any(item["facts"]["hasDelivery"] for item in task_items):
            return "delivered"
        if any(item["taskDisplayState"] in ("in_progress", "accepted_by_host") for item in task_items):
            return "in_progress"
        if any(item["taskDisplayState"] == "blocked" for item in task_items):
            return "blocked"
        return "unverified"

    def _key_issue_views(self, project_id, task_items, milestones, snapshot, workers):
        issues = []
        for item in task_items:
            if item["taskDisplayState"] == "blocked":
                issues.append({
                    "layer": LAYER_PROJECTS,
                    "issueId": "issue-%s" % item["taskId"],
                    "projectId": project_id,
                    "taskId": item["taskId"],
                    "title": item["title"],
                    "problem": item["plainSummary"]["problem"],
                    "impact": "该任务暂时无法继续",
                    "owner": item["workerLabel"] or "处理对象待明确",
                    "nextStep": item["plainSummary"]["nextStep"],
                    "state": "open",
                    "kind": "task_blocked",
                    "recordType": RECORD_REAL_EXECUTION,
                    "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_REAL_EXECUTION],
                    "evidence": item["evidence"],
                    "evidenceLabel": EVIDENCE_LABELS[item["evidence"]],
                })
            if item["facts"]["hasDelivery"] and not item["facts"]["reviewPassed"]:
                issues.append({
                    "layer": LAYER_PROJECTS,
                    "issueId": "pending-review-%s" % item["taskId"],
                    "projectId": project_id,
                    "taskId": item["taskId"],
                    "title": item["title"],
                    "problem": "等待独立审核",
                    "impact": "该任务已自报交付，尚未独立审核",
                    "owner": "负责人（独立审核人）",
                    "nextStep": "由独立审核人检查该交付版本，看板不自动批准。",
                    "state": "open",
                    "kind": "awaiting_independent_review",
                    "recordType": RECORD_SELF_REPORTED_DELIVERY,
                    "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_SELF_REPORTED_DELIVERY],
                    "evidence": EVIDENCE_WORKER_REPORT,
                    "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_WORKER_REPORT],
                })
        for row in milestones:
            if _text(row.get("status")) == "blocked":
                issues.append({
                    "layer": LAYER_PROJECTS,
                    "issueId": "issue-%s" % row.get("milestone_id"),
                    "projectId": project_id,
                    "taskId": None,
                    "title": row.get("name"),
                    "problem": "该里程碑登记为受阻",
                    "impact": "项目/业务线登记状态显示受阻（声明，不是执行证据）",
                    "owner": "处理对象待明确",
                    "nextStep": "确认受阻原因和需要谁处理。",
                    "state": "open",
                    "kind": "milestone_blocked",
                    "recordType": RECORD_PROJECT_STATUS,
                    "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_PROJECT_STATUS],
                    "evidence": EVIDENCE_UNVERIFIED,
                    "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_UNVERIFIED],
                })
        payload = (snapshot or {}).get("payload") or {}
        for position, item in enumerate(payload.get("unresolved") or []):
            if isinstance(item, dict):
                title = _text(item.get("title") or item.get("summary") or item.get("note"))
                text = _text(item.get("detail") or item.get("reason"))
            else:
                title, text = _text(item), None
            issues.append({
                "layer": LAYER_PROJECTS,
                "issueId": "plan-unresolved-%d" % position,
                "projectId": project_id,
                "taskId": None,
                "title": title or "规划资料里未解决的事项",
                "problem": text or "规划快照标记为未解决",
                "impact": "计划层面尚未解决，未派发、未执行",
                "owner": "处理对象待明确",
                "nextStep": "由负责人确认是否拆成任务；看板不自动生成任务。",
                "state": "open",
                "kind": "plan_unresolved",
                "recordType": RECORD_PLANNING_SNAPSHOT,
                "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_PLANNING_SNAPSHOT],
                "evidence": EVIDENCE_PLAN_ONLY,
                "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_PLAN_ONLY],
            })
        return issues

    def _project_review_summary(self, tasks):
        totals = {"total": 0, "approved": 0, "changesRequested": 0, "blocked": 0,
                  "requestInfo": 0, "recorded": 0, "notReviewed": 0}
        latest = None
        for task in tasks:
            reviews = [dict(row) for row in self.store.list_reviews(task["task_id"])]
            if not reviews:
                totals["notReviewed"] += 1
                continue
            for row in reviews:
                totals["total"] += 1
                decision = row.get("decision")
                if decision == "approved":
                    totals["approved"] += 1
                elif decision == "changes_requested":
                    totals["changesRequested"] += 1
                elif decision == "blocked":
                    totals["blocked"] += 1
                elif decision == "request_info":
                    totals["requestInfo"] += 1
                else:
                    totals["recorded"] += 1
            latest = {
                "taskId": task["task_id"],
                "decision": reviews[-1].get("decision"),
                "note": reviews[-1].get("note"),
                "reviewerId": reviews[-1].get("reviewer_id"),
                "at": reviews[-1].get("created_at"),
            }
        return {
            "layer": LAYER_PROJECTS,
            "recordType": RECORD_INDEPENDENT_REVIEW,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_INDEPENDENT_REVIEW],
            "totals": totals,
            "latest": latest,
            "note": "执行成员自报完成不代表独立审核通过；审核结论单独保存。",
        }

    @staticmethod
    def _completion_ratio(task_items):
        total = len(task_items)
        reported = sum(1 for item in task_items
                       if item["facts"]["hasDelivery"] or item["taskDisplayState"] == "approved")
        passed = sum(1 for item in task_items if item["facts"]["reviewPassed"])
        return {
            "tasksTotal": total,
            "agentReportedDone": reported,
            "independentReviewPassed": passed,
            "ratio": (round(reported / total, 4) if total else None),
            "role": "auxiliary",
            "userConfirmed": {"state": "unknown", "note": "本轮读模型没有接入人工确认事实"},
            "note": "任务完成比例只作辅助字段，不代表项目完成、门禁通过或发布完成。",
        }

    def project_layer(self, project_id, include_globals=False):
        bundle = self._bundle(project_id)
        row = bundle["project"]
        task_items = bundle["taskItems"]
        registered = row is not None
        declared = _text((row or {}).get("status"))
        if declared:
            status, status_source = declared, "declared"
        elif task_items:
            status, status_source = self._derive_project_status(task_items), "derived"
        else:
            status, status_source = None, "unknown"
        gates = self._gate_views(bundle["milestones"], task_items, bundle["snapshot"],
                                 project_id, bundle["workstreams"], bundle["planGates"])
        issues = self._key_issue_views(project_id, task_items, bundle["milestones"],
                                       bundle["snapshot"], bundle["workers"])
        review = self._project_review_summary(bundle["tasks"])
        plan_sources = self._plan_source_rows(project_id)
        snapshot = bundle["snapshot"]
        unverified = []
        for gate in gates:
            if gate["evidence"] == EVIDENCE_UNVERIFIED:
                unverified.append({"item": "门禁：%s" % (gate["name"] or gate["gateId"]),
                                   "evidence": EVIDENCE_UNVERIFIED,
                                   "note": gate["note"]})
        review_totals = review["totals"]
        unverified.append({
            "item": "独立审核",
            "evidence": (EVIDENCE_REVIEW_RECORD if review_totals["approved"] else EVIDENCE_UNVERIFIED),
            "note": ("%d 项任务已通过独立审核，%d 项尚未审核。"
                     % (review_totals["approved"], review_totals["notReviewed"])),
        })
        if not task_items:
            unverified.append({"item": "任务执行", "evidence": EVIDENCE_UNVERIFIED,
                               "note": "该项目没有任何任务记录。"})
        if snapshot is None:
            unverified.append({"item": "规划快照", "evidence": EVIDENCE_UNKNOWN,
                               "note": "该项目没有导入规划快照，规划基线未知。"})
        worker_views = []
        for task_item in task_items:
            worker = bundle["workers"].get(task_item.get("workerId"))
            if worker is not None and all(item["workerId"] != worker["worker_id"] for item in worker_views):
                worker_views.append(self.worker_view(worker, [project_id]))
        if not registered:
            registration_note = "该项目未在系统登记项目层级；这是一条已失效的旧链接，或只有历史执行记录。"
        else:
            registration_note = None
        if registered and not task_items:
            empty = {"code": "no_tasks", "text": "该项目已登记，尚未收到任何任务。"}
        elif not registered and not task_items:
            empty = {"code": "unregistered",
                     "text": "该项目未在系统登记项目层级，或这是一条已失效的旧链接。"}
        else:
            empty = None
        layer = {
            "layer": LAYER_PROJECTS,
            "recordType": RECORD_PROJECT if registered else RECORD_PROJECT_UNREGISTERED,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_PROJECT if registered else RECORD_PROJECT_UNREGISTERED],
            "evidenceType": (EVIDENCE_UNVERIFIED if not registered or not task_items else EVIDENCE_HOST_EVENT),
            "registration": "registered" if registered else "execution_only",
            "registrationNote": registration_note,
            "known": bool(registered or task_items),
            "projectId": project_id,
            "name": _text((row or {}).get("name")) or (project_id if registered else None),
            "goal": _text((row or {}).get("goal")),
            "goalLabel": _text((row or {}).get("goal")) or "尚未登记项目目标",
            "status": status,
            "statusLabel": (PLAIN_PROJECT_STATUS_LABELS.get(status)
                            or ("未登记状态" if not status else status)),
            "statusSource": status_source,
            "currentStage": _text((row or {}).get("current_stage")),
            "currentStageLabel": _text((row or {}).get("current_stage")) or "尚未上报当前阶段",
            "ownerId": _text((row or {}).get("owner_id")),
            "nextStep": _text((row or {}).get("next_step")),
            "nextStepLabel": _text((row or {}).get("next_step")) or "尚未上报下一步",
            "planSources": [{
                "sourceId": item.get("source_id"),
                "title": item.get("title"),
                "status": item.get("status"),
                "importedAt": item.get("imported_at"),
            } for item in plan_sources],
            "planningSnapshot": ({
                "snapshotId": snapshot["snapshotId"],
                "snapshotRevision": snapshot["snapshotRevision"],
                "documentVersion": snapshot["documentVersion"],
                "sourceName": snapshot["sourceName"],
                "sourceKind": snapshot["sourceKind"],
                "importedAt": snapshot["importedAt"],
                "status": snapshot["status"],
                "recordType": RECORD_PLANNING_SNAPSHOT,
                "evidence": EVIDENCE_PLAN_ONLY,
                "note": "规划快照只是计划基线，不构成派发、执行、交付或审核事实。",
            } if snapshot else None),
            "gates": gates,
            "keyIssues": issues,
            "independentReview": review,
            "businessLines": [line for line in
                              self.business_lines_layer(project_id)["businessLines"]
                              if line.get("recordType") != RECORD_PLANNING_SNAPSHOT],
            "businessLinesPlanned": [line for line in
                                     self.business_lines_layer(project_id)["businessLines"]
                                     if line.get("recordType") == RECORD_PLANNING_SNAPSHOT],
            "workers": worker_views,
            "tasks": task_items,
            "counts": {
                "tasks": len(task_items),
                "businessLines": len(bundle["workstreams"]),
                "milestones": len(bundle["milestones"]),
                "workers": len(worker_views),
                "openIssues": len(issues),
            },
            "auxiliary": {"taskCompletionRatio": self._completion_ratio(task_items)},
            "emptyState": empty,
            "unverified": unverified,
            "deployment": {"state": "unknown", "note": "本轮读模型不采集部署状态。"},
            "technical": {
                "projectId": project_id,
                "registrationTable": ("workspace_projects" if registered else None),
                "planSourcePaths": [item.get("path") for item in plan_sources],
                "planSourceDigests": [item.get("source_sha256") for item in plan_sources],
                "planningSnapshotPath": (snapshot or {}).get("sourcePath"),
                "planningSnapshotDigest": (snapshot or {}).get("sourceSha256"),
                "taskIds": [item["taskId"] for item in task_items],
            },
        }
        if include_globals:
            layer["portfolio"] = self.portfolio_layer()
            layer["workersRegistry"] = self.workers_layer()
        return layer

    def project_layers(self):
        return [self.project_layer(project_id) for project_id in self._project_ids()]

    def projects_layer(self):
        projects = self.project_layers()
        return {
            "layer": LAYER_PROJECTS,
            "projects": projects,
            "counts": {"total": len(projects),
                       "registered": sum(1 for p in projects if p["registration"] == "registered")},
            "emptyState": (None if projects else
                           {"code": "no_projects", "text": "当前数据库没有任何项目。"}),
        }

    def project_summary(self, project_id):
        """项目详情层（兼容旧入口：仍带 projectId 与 tasks）。

        冻结的看板路由（scripts/d4_board.py）无法新增路由，因此驾驶舱与成员登记
        摘要随项目详情一起返回；它们仍只是只读投影。
        """
        layer = self.project_layer(project_id, include_globals=True)
        return layer

    # -- 业务线层 / 任务层 ---------------------------------------------------
    def business_lines_layer(self, project_id=None):
        available = self.has_table("workspace_workstreams")
        items = []
        for pid in self._project_ids(project_id):
            bundle = self._bundle(pid)
            for row in bundle["workstreams"]:
                linked = [item for item in bundle["taskItems"]
                          if item.get("businessLineId") == row.get("workstream_id")]
                gates = [gate for gate in
                         self._gate_views(bundle["milestones"], bundle["taskItems"], bundle["snapshot"],
                                          pid, bundle["workstreams"], bundle["planGates"])
                         if gate.get("businessLineId") == row.get("workstream_id")]
                state, evidence, note = self._gate_state(linked)
                if gates and all(gate["evidence"] == EVIDENCE_UNVERIFIED for gate in gates):
                    state, evidence = "unverified", EVIDENCE_UNVERIFIED
                items.append({
                    "layer": LAYER_BUSINESS_LINES,
                    "businessLineId": row.get("workstream_id"),
                    "projectId": pid,
                    "name": row.get("name"),
                    "goal": _text(row.get("goal")),
                    "status": _text(row.get("status")),
                    "statusLabel": PLAIN_PROJECT_STATUS_LABELS.get(_text(row.get("status")),
                                                                   _text(row.get("status")) or "未登记状态"),
                    "statusSource": "declared",
                    "sectionRef": _text(row.get("source_section")),
                    "sectionTitle": None,
                    "sourceDocument": None,
                    "nextStep": _text(row.get("next_step")),
                    "nextStepLabel": _text(row.get("next_step")) or "尚未上报下一步",
                    "milestoneIds": [gate["gateId"] for gate in gates if gate.get("gateId")],
                    "taskIds": [item["taskId"] for item in linked],
                    "workerIds": sorted({item["workerId"] for item in linked if item.get("workerId")}),
                    "gates": gates,
                    "state": state,
                    "stateLabel": GATE_STATE_LABELS.get(state, "状态待确认"),
                    "note": note,
                    "recordType": RECORD_BUSINESS_LINE,
                    "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_BUSINESS_LINE],
                    "evidence": evidence,
                    "evidenceLabel": EVIDENCE_LABELS[evidence],
                    "tasks": linked,
                    "plannedOnly": False,
                })
            snapshot = bundle["snapshot"]
            if snapshot is not None:
                items.extend(self._plan_business_lines(snapshot, pid))
        notes = ["业务线是受控登记对象；规划快照里的计划业务线单独列出，不与登记业务线自动合并。"]
        if not available:
            notes.append("该数据库没有业务线表（workspace_workstreams），只显示规划快照里的计划业务线。")
        return {
            "layer": LAYER_BUSINESS_LINES,
            "available": available,
            "businessLines": items,
            "counts": {
                "total": len(items),
                "registered": sum(1 for item in items if item["recordType"] == RECORD_BUSINESS_LINE),
                "planned": sum(1 for item in items if item["recordType"] == RECORD_PLANNING_SNAPSHOT),
            },
            "emptyState": (None if items else {"code": "no_business_lines", "text": "还没有业务线或计划业务线。"}),
            "notes": notes,
        }

    def tasks_layer(self, project_id=None, business_line_id=None, record_type=None,
                    worker_id=None, status=None):
        """任务层（可选筛选）。筛选条件不认识或没有命中时返回安全空结果，不抛错。"""
        unused = {}
        if business_line_id is not None:
            unused["businessLineId"] = business_line_id
        if record_type is not None:
            unused["recordType"] = record_type
        if worker_id is not None:
            unused["workerId"] = worker_id
        if status is not None:
            unused["status"] = status
        unknown_filters = []
        if record_type is not None and record_type not in TASK_RECORD_TYPES:
            unknown_filters.append({"filter": "recordType", "value": record_type,
                                    "allowed": list(TASK_RECORD_TYPES)})
        filtered = project_id is not None or bool(unused)
        items = []
        for pid in self._project_ids(project_id):
            bundle = self._bundle(pid)
            for item in bundle["taskItems"]:
                if business_line_id is not None and item.get("businessLineId") != business_line_id:
                    continue
                if record_type is not None and item["recordType"] != record_type:
                    continue
                if worker_id is not None and item.get("workerId") != worker_id:
                    continue
                if status is not None and item.get("status") != status:
                    continue
                items.append(item)
        if unknown_filters:
            filter_status = "unknown_filter_value"
            items = []
        elif filtered and not items:
            filter_status = "no_match"
        else:
            filter_status = "ok"
        notes = ["任务用稳定 taskId 归属到项目与业务线；同名任务不会跨项目合并。"]
        if filter_status == "no_match":
            notes.append("没有任务符合当前筛选；这不代表项目没有任务。")
        if filter_status == "unknown_filter_value":
            notes.append("筛选条件不在允许集合内，已返回空结果而不是猜一个近似值。")
        return {
            "layer": LAYER_TASKS,
            "available": True,
            "tasks": items,
            "total": len(items),
            "filtered": filtered,
            "filterStatus": filter_status,
            "usedFilters": {"projectId": project_id, "businessLineId": business_line_id,
                            "recordType": record_type, "workerId": worker_id, "status": status},
            "unknownFilters": unknown_filters,
            "emptyState": ({"code": filter_status,
                            "text": "没有符合条件的任务。" if filter_status != "unknown_filter_value"
                                    else "筛选条件不可识别，未返回任务。"} if not items and filtered else None),
            "notes": notes,
        }

    # -- 历史资料（只读目录） ------------------------------------------------
    def _archive_task_item(self, project, task):
        documents = task.get("documents") or []
        return {
            "layer": LAYER_TASKS,
            "taskId": task.get("taskId"),
            "title": task.get("title") or task.get("goal") or task.get("taskId"),
            "status": task.get("status"),
            "statusLabel": task.get("statusLabel"),
            "projectId": project.get("projectId"),
            "businessLineId": None,
            "businessLineName": None,
            "businessLineKnown": False,
            "milestoneId": task.get("milestoneId"),
            "workerId": None,
            "workerLabel": _text(task.get("member")) or "历史登记未注明成员",
            "workerKnown": False,
            "hostLabel": "历史资料未注明宿主",
            "recordType": RECORD_HISTORICAL_ARCHIVE,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_HISTORICAL_ARCHIVE],
            "evidence": EVIDENCE_DOCUMENT_ARCHIVE,
            "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_DOCUMENT_ARCHIVE],
            "workerReport": {"recordType": RECORD_SELF_REPORTED_DELIVERY, "state": "none",
                             "note": "历史资料登记不是本次交付。"},
            "reviewRecord": {"recordType": RECORD_INDEPENDENT_REVIEW, "state": "not_reviewed",
                             "label": "历史资料登记不含独立审核事实", "evidence": EVIDENCE_DOCUMENT_ARCHIVE,
                             "count": 0},
            "stateLabel": task.get("statusLabel") or "历史资料登记",
            "plainSummary": {
                "source": RECORD_HISTORICAL_ARCHIVE,
                "sourceLabel": RECORD_TYPE_LABELS[RECORD_HISTORICAL_ARCHIVE],
                "done": _text(task.get("result")) or "历史资料未登记已完成内容",
                "problem": _text(task.get("problem")) or "历史资料未登记未解决问题",
                "nextStep": _text(task.get("nextStep")) or "历史资料未登记下一步",
                "pending": False,
            },
            "summary": {"pending": True, "code": SUMMARY_PENDING, "status": "none", "fields": {}},
            "facts": {"hasAttempt": False, "hostAccepted": False, "submittedPendingHost": False,
                      "hasDelivery": False, "hasReport": False, "reviewState": "not_reviewed",
                      "reviewPassed": False},
            "documents": documents,
            "documentCount": len(documents),
            "technical": {
                "recordIds": {"taskId": task.get("taskId"), "cardIds": task.get("cardIds") or []},
                "paths": [item.get("sourcePath") for item in documents if item.get("sourcePath")],
                "digests": [{"kind": item.get("name"), "value": item.get("sha256")}
                            for item in documents if item.get("sha256")],
                "changedFiles": [], "commands": [], "claimedVerification": [], "rawErrors": [],
                "notes": ["历史资料登记不等于重新执行，也不代表当前版本。"],
            },
            "unverified": [{"item": "执行与审核", "evidence": EVIDENCE_DOCUMENT_ARCHIVE,
                            "note": "历史资料没有本次执行或审核事实。"}],
        }

    def _archive_project_layer(self, project):
        project = dict(project or {})
        tasks = [self._archive_task_item(project, task) for task in (project.get("tasks") or [])
                 if isinstance(task, dict)]
        gates = [{
            "layer": LAYER_PROJECTS,
            "gateId": milestone.get("id"),
            "projectId": project.get("projectId"),
            "name": milestone.get("title"),
            "gateCriteria": _gate_text(milestone.get("gate")),
            "state": "document_only",
            "stateLabel": "只有历史资料",
            "evidence": EVIDENCE_DOCUMENT_ARCHIVE,
            "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_DOCUMENT_ARCHIVE],
            "recordType": RECORD_HISTORICAL_ARCHIVE,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_HISTORICAL_ARCHIVE],
            "declaredStatus": None,
            "declaredStatusLabel": "历史资料未登记状态",
            "businessLineId": None,
            "businessLineName": None,
            "sectionRef": None,
            "sectionTitle": None,
            "sourceDocument": None,
            "taskIds": [item["taskId"] for item in tasks if item["milestoneId"] == milestone.get("id")],
            "note": "历史资料登记；不构成执行或审核证据。",
        } for milestone in (project.get("milestones") or []) if isinstance(milestone, dict)]
        issues = [{
            "layer": LAYER_PROJECTS,
            "issueId": "archive-issue-%d" % position,
            "projectId": project.get("projectId"),
            "taskId": issue.get("taskId"),
            "title": issue.get("title"),
            "problem": issue.get("impact"),
            "impact": issue.get("impact"),
            "owner": issue.get("owner") or "处理对象待明确",
            "nextStep": issue.get("nextStep") or "按历史资料登记处理。",
            "state": issue.get("status") or "open",
            "kind": "archive_issue",
            "recordType": RECORD_HISTORICAL_ARCHIVE,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_HISTORICAL_ARCHIVE],
            "evidence": EVIDENCE_DOCUMENT_ARCHIVE,
            "evidenceLabel": EVIDENCE_LABELS[EVIDENCE_DOCUMENT_ARCHIVE],
        } for position, issue in enumerate(project.get("issues") or []) if isinstance(issue, dict)]
        return {
            "layer": LAYER_PROJECTS,
            "recordType": RECORD_HISTORICAL_ARCHIVE,
            "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_HISTORICAL_ARCHIVE],
            "evidenceType": EVIDENCE_DOCUMENT_ARCHIVE,
            "registration": "archive",
            "registrationNote": "这是资料登记项目，不代表当前执行状态。",
            "known": True,
            "projectId": project.get("projectId"),
            "name": project.get("name") or project.get("projectId"),
            "goal": _text(project.get("goal")),
            "goalLabel": _text(project.get("goal")) or "历史资料未登记项目目标",
            "status": None,
            "statusLabel": "历史资料登记（不代表当前执行状态）",
            "statusSource": "archive",
            "currentStage": _text(project.get("stage")),
            "currentStageLabel": _text(project.get("stage")) or "历史资料未登记当前阶段",
            "ownerId": _text(project.get("owner")),
            "nextStep": None,
            "nextStepLabel": "历史资料未登记下一步",
            "planSources": [],
            "planningSnapshot": None,
            "gates": gates,
            "keyIssues": issues,
            "independentReview": {"layer": LAYER_PROJECTS, "recordType": RECORD_INDEPENDENT_REVIEW,
                                  "totals": {"total": 0, "approved": 0, "changesRequested": 0,
                                             "blocked": 0, "requestInfo": 0, "recorded": 0,
                                             "notReviewed": len(tasks)},
                                  "latest": None,
                                  "note": "历史资料登记不含独立审核事实。"},
            "businessLines": [],
            "businessLinesPlanned": [],
            "workers": [],
            "tasks": tasks,
            "counts": {"tasks": len(tasks), "businessLines": 0,
                       "milestones": len(project.get("milestones") or []), "workers": 0,
                       "openIssues": len(issues)},
            "auxiliary": {"taskCompletionRatio": {
                "tasksTotal": len(tasks), "agentReportedDone": 0, "independentReviewPassed": 0,
                "ratio": None, "role": "auxiliary",
                "userConfirmed": {"state": "unknown", "note": "历史资料不含人工确认事实"},
                "note": "历史资料登记不能用来算完成比例或项目完成。",
            }},
            "emptyState": None,
            "unverified": [{"item": "执行与审核", "evidence": EVIDENCE_DOCUMENT_ARCHIVE,
                            "note": "历史资料登记没有本次执行、交付或审核证据。"}],
            "deployment": {"state": "unknown", "note": "本轮读模型不采集部署状态。"},
            "technical": {"projectId": project.get("projectId"), "sourceDocument": None},
        }

    # -- 总体驾驶舱 ---------------------------------------------------------
    def portfolio_layer(self, projects=None):
        """总体驾驶舱：结论先看里程碑门禁、关键问题和独立审核；比例只作辅助。"""
        projects = list(projects) if projects is not None else self.project_layers()
        gates, issues, unverified = [], [], []
        review_totals = {"total": 0, "approved": 0, "changesRequested": 0, "blocked": 0,
                         "requestInfo": 0, "recorded": 0, "notReviewed": 0}
        rows = []
        for project in projects:
            pid, name = project.get("projectId"), project.get("name")
            for gate in project.get("gates") or []:
                entry = dict(gate, projectId=pid, projectName=name)
                gates.append(entry)
                if entry["evidence"] in (EVIDENCE_UNVERIFIED, EVIDENCE_UNKNOWN):
                    unverified.append({"item": "门禁：%s" % (entry.get("name") or entry.get("gateId")),
                                       "projectId": pid, "evidence": entry["evidence"],
                                       "note": entry.get("note")})
            for issue in project.get("keyIssues") or []:
                issues.append(dict(issue, projectId=pid, projectName=name))
            totals = (project.get("independentReview") or {}).get("totals") or {}
            for key in review_totals:
                review_totals[key] += totals.get(key, 0)
            for item in project.get("unverified") or []:
                unverified.append(dict(item, projectId=pid))
            gate_summary = {"total": len(project.get("gates") or []),
                            "reviewPassed": sum(1 for g in project.get("gates") or []
                                                if g["state"] == "review_passed"),
                            "unverified": sum(1 for g in project.get("gates") or []
                                              if g["evidence"] in (EVIDENCE_UNVERIFIED, EVIDENCE_UNKNOWN))}
            rows.append({
                "projectId": pid,
                "name": name,
                "registration": project.get("registration"),
                "recordType": project.get("recordType"),
                "status": project.get("status"),
                "statusLabel": project.get("statusLabel"),
                "statusSource": project.get("statusSource"),
                "gateSummary": gate_summary,
                "keyIssueCount": len(project.get("keyIssues") or []),
                "review": totals,
                "tasks": (project.get("counts") or {}).get("tasks", 0),
                "workers": (project.get("counts") or {}).get("workers", 0),
                "nextStep": project.get("nextStepLabel"),
                "auxiliary": project.get("auxiliary"),
            })
        conclusion = []
        if projects:
            passed = [gate for gate in gates if gate["state"] == "review_passed"]
            no_evidence = [gate for gate in gates
                           if gate["evidence"] in (EVIDENCE_UNVERIFIED, EVIDENCE_UNKNOWN)]
            conclusion.append("里程碑门禁：%d 项已通过独立审核，%d 项尚无证据。" % (len(passed), len(no_evidence)))
            conclusion.append("关键问题：%d 项待处理。" % len(issues))
            conclusion.append("独立审核：%d 项已通过，%d 项尚未审核。"
                              % (review_totals["approved"], review_totals["notReviewed"]))
            conclusion.append("任务完成比例只是辅助字段，不能用它推断项目完成或门禁通过。")
        else:
            conclusion.append("尚未登记任何项目，暂无可判断的门禁、关键问题或审核结论。")
        notes = ["结论优先看里程碑门禁、关键问题和独立审核；计划资料与执行事实分开显示。",
                 "没有证据的事项显示为尚未验证，不由读模型补造。"]
        if not self.has_table("workspace_projects"):
            notes.append("该数据库没有项目登记表（workspace_projects），项目只按执行记录列出。")
        if not self.has_table("workspace_workers"):
            notes.append("该数据库没有成员登记表（workspace_workers），成员信息为未知。")
        if not self.has_table("planning_snapshots"):
            notes.append("该数据库没有规划快照表，规划基线为未知。")
        return {
            "layer": LAYER_PORTFOLIO,
            "recordType": "portfolio",
            "recordTypeLabel": "总体驾驶舱（多个项目的汇总视图）",
            "conclusionBasis": ["milestone_gate", "key_issue", "independent_review"],
            "conclusion": conclusion,
            "headline": {
                "gates": gates,
                "keyIssues": issues,
                "independentReview": {
                    "recordType": RECORD_INDEPENDENT_REVIEW,
                    "recordTypeLabel": RECORD_TYPE_LABELS[RECORD_INDEPENDENT_REVIEW],
                    "totals": review_totals,
                },
            },
            "auxiliary": {
                "taskCompletionRatio": {
                    "role": "auxiliary",
                    "byProject": [{"projectId": row["projectId"],
                                   "ratio": (row.get("auxiliary") or {})
                                   .get("taskCompletionRatio", {}).get("ratio")} for row in rows],
                    "note": "任务完成比例只作辅助，不代表项目完成。",
                },
            },
            "projects": rows,
            "unverified": unverified,
            "emptyState": (None if projects else
                           {"code": "no_projects", "text": "尚未登记任何项目。"}),
            "notes": notes,
        }

    # -- 五层集合 -----------------------------------------------------------
    def layer_objects(self, archive_projects=None):
        """明确返回五层对象：portfolio / projects / businessLines / tasks / workers。"""
        projects = self.project_layers()
        archive_layers = []
        for archive in archive_projects or []:
            layer = self._archive_project_layer(archive)
            archive_layers.append(layer)
            projects.append(layer)
        business_lines = self.business_lines_layer()
        tasks = self.tasks_layer()
        for layer in archive_layers:
            tasks["tasks"].extend(layer["tasks"])
        tasks["total"] = len(tasks["tasks"])
        workers = self.workers_layer()
        portfolio = self.portfolio_layer(projects=projects)
        return {
            "layerOrder": list(LAYER_ORDER),
            "portfolio": portfolio,
            "projects": {"layer": LAYER_PROJECTS, "projects": projects},
            "businessLines": business_lines,
            "tasks": tasks,
            "workers": workers,
            "archiveProjects": [layer["projectId"] for layer in archive_layers],
            "availability": self.layer_availability(),
        }

