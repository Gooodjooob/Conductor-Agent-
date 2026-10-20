"""SQLite 持久化：任务 / 执行轮次 / 事件 / 交付 / 审核 / 活动历史。

设计要点（对齐本卡 §0.2 与接口文档 §3）：
- 单库多表，sqlite3 事务（BEGIN IMMEDIATE / COMMIT / ROLLBACK），关键表用主键做幂等/去重约束。
- 事件表以 event_id 为主键，实现“同 eventId 只收一次”。
- 旧事件、迟到结果写入 reconciliation（对账/历史），不回退当前状态。
- 存储层只做读写，不含业务判断。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id       TEXT PRIMARY KEY,
    label           TEXT,
    capability_json TEXT,
    registered_at   TEXT
);
CREATE TABLE IF NOT EXISTS agents (
    agent_id      TEXT PRIMARY KEY,
    label         TEXT,
    note          TEXT,
    registered_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id             TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    title               TEXT,
    agent_id            TEXT,
    source_id           TEXT,
    workspace           TEXT,
    baseline            TEXT,
    goal                TEXT,
    acceptance_json     TEXT,
    permissions_json    TEXT,
    limits_json         TEXT,
    status              TEXT,
    revision            INTEGER,
    current_attempt_id  TEXT,
    current_delivery_id TEXT,
    closure_json        TEXT,
    created_at          TEXT,
    updated_at          TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id         TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    ordinal            INTEGER,
    state              TEXT,
    created_at         TEXT,
    submitted_at       TEXT,
    accepted_by_host_at TEXT,
    started_at         TEXT,
    terminal_at        TEXT,
    terminal_kind      TEXT,
    blocked_json       TEXT,
    note               TEXT,
    submit_state       TEXT,
    submission_json    TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id     TEXT PRIMARY KEY,
    task_id      TEXT,
    attempt_id   TEXT,
    project_id   TEXT,
    source_id    TEXT,
    agent_id     TEXT,
    sequence     INTEGER,
    type         TEXT,
    occurred_at  TEXT,
    received_at  TEXT,
    content_hash TEXT,
    outcome      TEXT,
    outcome_code TEXT,
    reason       TEXT,
    result_json  TEXT,
    synthetic    INTEGER
);
CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id        TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL,
    attempt_id         TEXT,
    summary            TEXT,
    changed_files_json TEXT,
    verification_json  TEXT,
    risks_json         TEXT,
    report_path        TEXT,
    unfinished_json    TEXT,
    source_delivery_id TEXT,
    created_at         TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id            TEXT PRIMARY KEY,
    task_id              TEXT,
    delivery_id          TEXT,
    decision             TEXT,
    note                 TEXT,
    review_round         INTEGER,
    resulting_attempt_id TEXT,
    reviewer_id          TEXT,
    created_at           TEXT
);
CREATE TABLE IF NOT EXISTS activity (
    activity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    attempt_id  TEXT,
    at          TEXT,
    kind        TEXT,
    detail_json TEXT
);
CREATE TABLE IF NOT EXISTS reconciliation (
    recon_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    event_id    TEXT,
    attempt_id  TEXT,
    sequence    INTEGER,
    kind        TEXT,
    detail_json TEXT,
    at          TEXT
);
CREATE TABLE IF NOT EXISTS conflicts (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT,
    task_id     TEXT,
    detail_json TEXT,
    at          TEXT
);
-- D4-NEXT-01C：正式报告记账（additive migration，只新增表，不改旧交付字段）。
-- 主键 (report_id, report_revision) 承担幂等：同 reportId+revision 只允许一条记录，
-- 重复导入按 digest 判定 replay 或 event_conflict，不覆盖首次结果。
CREATE TABLE IF NOT EXISTS reports (
    report_id            TEXT NOT NULL,
    report_revision      INTEGER NOT NULL,
    report_type          TEXT,
    project_id           TEXT,
    task_id              TEXT,
    attempt_id           TEXT,
    delivery_id          TEXT,
    source_card_json     TEXT,
    requested_by_json    TEXT,
    executed_by_json     TEXT,
    reviewed_by_json     TEXT,
    adapter              TEXT,
    session_id           TEXT,
    host_run_id          TEXT,
    board_summary_json   TEXT,
    business_summary_json TEXT,
    evidence_json        TEXT,
    canonical_digest     TEXT,
    source_file_json     TEXT,
    imported_at          TEXT,
    imported_by_json     TEXT,
    synthetic            INTEGER,
    PRIMARY KEY (report_id, report_revision)
);
CREATE INDEX IF NOT EXISTS idx_reports_task ON reports (task_id, delivery_id);
-- D4-NEXT-01C-R1：来源任务卡冻结登记（建卡时随任务在同一事务内写入，task_id 唯一）。
-- 只允许受信 TASK_CREATE 上下文原子写入；报告导入只读该登记，不做临时补绑。
-- 旧库只读连接不建此表（get_source_card 先探测）；写模式 additive 建表，不动旧数据。
CREATE TABLE IF NOT EXISTS task_source_cards (
    task_id          TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    card_id          TEXT NOT NULL,
    card_revision    INTEGER NOT NULL,
    source_path      TEXT,
    source_sha256    TEXT,
    canonical_digest TEXT,
    registered_json  TEXT,
    registered_by_json TEXT,
    created_at       TEXT
);
-- D4 MVP 2.0：项目驾驶舱层级（仅新增，不覆盖旧任务账本）。
CREATE TABLE IF NOT EXISTS workspace_projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    current_stage TEXT,
    owner_id TEXT,
    next_step TEXT,
    source_document TEXT,
    source_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_workstreams (
    workstream_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    goal TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    position INTEGER NOT NULL DEFAULT 0,
    source_section TEXT,
    next_step TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES workspace_projects(project_id)
);
CREATE TABLE IF NOT EXISTS workspace_milestones (
    milestone_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workstream_id TEXT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    gate TEXT,
    position INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES workspace_projects(project_id),
    FOREIGN KEY (workstream_id) REFERENCES workspace_workstreams(workstream_id)
);
CREATE TABLE IF NOT EXISTS workspace_workers (
    worker_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    host TEXT NOT NULL,
    environment TEXT,
    description TEXT,
    default_model TEXT,
    registration_status TEXT NOT NULL DEFAULT 'registered',
    runtime_status TEXT NOT NULL DEFAULT 'unknown',
    enabled INTEGER NOT NULL DEFAULT 0,
    read_scope_json TEXT,
    write_scope_json TEXT,
    allowed_projects_json TEXT,
    binding_revision INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_task_links (
    task_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    workstream_id TEXT,
    milestone_id TEXT,
    worker_id TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id)
);
CREATE TABLE IF NOT EXISTS workspace_plan_sources (
    source_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'snapshot'
);
CREATE INDEX IF NOT EXISTS idx_workspace_workstreams_project ON workspace_workstreams(project_id, position);
CREATE INDEX IF NOT EXISTS idx_workspace_milestones_project ON workspace_milestones(project_id, position);
CREATE INDEX IF NOT EXISTS idx_workspace_task_links_project ON workspace_task_links(project_id);
"""

# additive 迁移：旧库可能已建过不带 registered_by_json 的 task_source_cards。
# 写模式打开时补列（ALTER TABLE ADD COLUMN），不改动旧数据；只读连接不建/不改表。
_SOURCE_CARD_ADDED_COLUMNS = (
    ("registered_by_json", "TEXT"),
)

_TASK_COLUMNS = {
    "title", "agent_id", "source_id", "workspace", "baseline", "goal",
    "acceptance_json", "permissions_json", "limits_json", "status", "revision",
    "current_attempt_id", "current_delivery_id", "closure_json", "updated_at",
}
_ATTEMPT_COLUMNS = {
    "state", "submitted_at", "accepted_by_host_at", "started_at", "terminal_at",
    "terminal_kind", "blocked_json", "note", "submit_state", "submission_json",
}


class SqliteStore:
    """轻量 SQLite 存储。所有写操作通过 transaction() 保持原子性。"""

    def __init__(self, path, read_only=False):
        self.path = str(path)
        # isolation_level=None -> 关闭隐式事务，由 transaction() 显式控制。
        self.read_only = read_only
        if read_only:
            from pathlib import Path

            uri = Path(self.path).resolve().as_uri() + "?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, isolation_level=None)
            self.conn.execute("PRAGMA query_only = ON")
        else:
            self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if not read_only:
            self.conn.executescript(_SCHEMA)
            self._migrate_source_cards()
        self._depth = 0  # 事务嵌套深度，用于 SAVEPOINT 重入

    def _migrate_source_cards(self):
        """additive 迁移：给旧库已有的 task_source_cards 补新列，不改旧数据。

        新库由 _SCHEMA 直接建全列；旧库（R1 冻结登记）可能缺 registered_by_json，
        写模式打开时补列（ALTER TABLE ADD COLUMN）。只读连接不建/不改表。
        """
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_source_cards'"
        ).fetchone()
        if row is None:
            return
        existing = {r[1] for r in self.conn.execute("PRAGMA table_info(task_source_cards)")}
        for name, decl in _SOURCE_CARD_ADDED_COLUMNS:
            if name not in existing:
                self.conn.execute(
                    "ALTER TABLE task_source_cards ADD COLUMN %s %s" % (name, decl)
                )

    # -- 基础设施 ---------------------------------------------------------
    def close(self):
        self.conn.close()

    @contextmanager
    def transaction(self):
        """可重入事务：外层 BEGIN IMMEDIATE，内层 SAVEPOINT，任一失败整体回滚。"""
        depth = self._depth
        if depth == 0:
            self.conn.execute("BEGIN IMMEDIATE")
        else:
            self.conn.execute("SAVEPOINT sp_%d" % depth)
        self._depth = depth + 1
        try:
            yield self
        except Exception:
            self._depth = depth
            if depth == 0:
                self.conn.execute("ROLLBACK")
            else:
                self.conn.execute("ROLLBACK TO sp_%d" % depth)
                self.conn.execute("RELEASE sp_%d" % depth)
            raise
        else:
            self._depth = depth
            if depth == 0:
                self.conn.execute("COMMIT")
            else:
                self.conn.execute("RELEASE sp_%d" % depth)

    @staticmethod
    def _dict(row):
        return dict(row) if row is not None else None

    def _all(self, sql, params=()):
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def _one(self, sql, params=()):
        return self._dict(self.conn.execute(sql, params).fetchone())

    # -- 来源 / 成员登记 --------------------------------------------------
    def register_source(self, source_id, label=None, capability=None, at=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO sources (source_id, label, capability_json, registered_at) "
            "VALUES (?, ?, ?, ?)",
            (source_id, label, _json(capability), at),
        )
        return self.get_source(source_id)

    def get_source(self, source_id):
        return self._one("SELECT * FROM sources WHERE source_id = ?", (source_id,))

    def register_agent(self, agent_id, label=None, note=None, at=None):
        self.conn.execute(
            "INSERT OR IGNORE INTO agents (agent_id, label, note, registered_at) VALUES (?, ?, ?, ?)",
            (agent_id, label, note, at),
        )
        return self.get_agent(agent_id)

    def get_agent(self, agent_id):
        return self._one("SELECT * FROM agents WHERE agent_id = ?", (agent_id,))

    # -- 任务 -------------------------------------------------------------
    def insert_task(self, record):
        self.conn.execute(
            "INSERT INTO tasks (task_id, project_id, title, agent_id, source_id, workspace, "
            "baseline, goal, acceptance_json, permissions_json, limits_json, status, revision, "
            "current_attempt_id, current_delivery_id, closure_json, created_at, updated_at) "
            "VALUES (:task_id, :project_id, :title, :agent_id, :source_id, :workspace, "
            ":baseline, :goal, :acceptance_json, :permissions_json, :limits_json, :status, "
            ":revision, :current_attempt_id, :current_delivery_id, :closure_json, "
            ":created_at, :updated_at)",
            record,
        )

    def get_task(self, task_id):
        return self._one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))

    def list_tasks(self, project_id=None):
        if project_id is None:
            return self._all("SELECT * FROM tasks ORDER BY created_at, task_id")
        return self._all(
            "SELECT * FROM tasks WHERE project_id = ? ORDER BY created_at, task_id", (project_id,)
        )

    def update_task(self, task_id, **fields):
        self._update("tasks", "task_id", task_id, fields, _TASK_COLUMNS)

    # -- 执行轮次 ---------------------------------------------------------
    def insert_attempt(self, record):
        record = dict(record)
        record.setdefault("submit_state", "created")
        record.setdefault("submission_json", None)
        self.conn.execute(
            "INSERT INTO attempts (attempt_id, task_id, ordinal, state, created_at, submitted_at, "
            "accepted_by_host_at, started_at, terminal_at, terminal_kind, blocked_json, note, "
            "submit_state, submission_json) "
            "VALUES (:attempt_id, :task_id, :ordinal, :state, :created_at, :submitted_at, "
            ":accepted_by_host_at, :started_at, :terminal_at, :terminal_kind, :blocked_json, :note, "
            ":submit_state, :submission_json)",
            record,
        )

    def begin_submission(self, attempt_id):
        """原子占位：仅当仍为 created 且尚未提交时置 submit_state='submitting'。

        返回 True 表示本次调用抢到了占位（可以去调用宿主）；False 表示已有提交记录/进行中。
        必须在事务内调用（BEGIN IMMEDIATE）以保证跨连接串行。
        """
        cur = self.conn.execute(
            "UPDATE attempts SET submit_state = 'submitting', submission_json = NULL "
            "WHERE attempt_id = ? AND submit_state = 'created' AND submitted_at IS NULL",
            (attempt_id,),
        )
        return cur.rowcount == 1

    def set_submission(self, attempt_id, state, result_json, submitted_at=None,
                       accepted_by_host_at=None):
        fields = {"submit_state": state, "submission_json": result_json}
        if submitted_at is not None:
            fields["submitted_at"] = submitted_at
        if accepted_by_host_at is not None:
            fields["accepted_by_host_at"] = accepted_by_host_at
        self.update_attempt(attempt_id, **fields)

    def get_attempt(self, attempt_id):
        return self._one("SELECT * FROM attempts WHERE attempt_id = ?", (attempt_id,))

    def list_attempts(self, task_id):
        return self._all(
            "SELECT * FROM attempts WHERE task_id = ? ORDER BY ordinal", (task_id,)
        )

    def count_attempts(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0]

    def update_attempt(self, attempt_id, **fields):
        self._update("attempts", "attempt_id", attempt_id, fields, _ATTEMPT_COLUMNS)

    # -- 事件 -------------------------------------------------------------
    def insert_event(self, record):
        record = dict(record)
        record.setdefault("outcome_code", None)
        record.setdefault("synthetic", 0)
        self.conn.execute(
            "INSERT INTO events (event_id, task_id, attempt_id, project_id, source_id, agent_id, "
            "sequence, type, occurred_at, received_at, content_hash, outcome, outcome_code, reason, "
            "result_json, synthetic) VALUES (:event_id, :task_id, :attempt_id, :project_id, "
            ":source_id, :agent_id, :sequence, :type, :occurred_at, :received_at, :content_hash, "
            ":outcome, :outcome_code, :reason, :result_json, :synthetic)",
            record,
        )

    def get_event(self, event_id):
        return self._one("SELECT * FROM events WHERE event_id = ?", (event_id,))

    def max_sequence(self, task_id, attempt_id, types=None):
        """已生效（applied）事件的最大序号。被拒绝或迟到的事件不推进序号。

        传入 types 时只统计这些事件类型，用于“独立里程碑序号”：例如接收确认
        （task.accepted）不能被执行类消息（task.progress）的序号永久压制。
        """
        sql = (
            "SELECT MAX(sequence) FROM events "
            "WHERE task_id = ? AND attempt_id = ? AND outcome = 'applied'"
        )
        params = [task_id, attempt_id]
        if types:
            placeholders = ", ".join("?" for _ in types)
            sql += " AND type IN (%s)" % placeholders
            params.extend(types)
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row and row[0] is not None else 0

    # -- 交付 -------------------------------------------------------------
    def insert_delivery(self, record):
        self.conn.execute(
            "INSERT INTO deliveries (delivery_id, task_id, attempt_id, summary, changed_files_json, "
            "verification_json, risks_json, report_path, unfinished_json, source_delivery_id, created_at) "
            "VALUES (:delivery_id, :task_id, :attempt_id, :summary, :changed_files_json, "
            ":verification_json, :risks_json, :report_path, :unfinished_json, :source_delivery_id, "
            ":created_at)",
            record,
        )

    def get_delivery(self, delivery_id):
        return self._one("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))

    def list_deliveries(self, task_id):
        return self._all(
            "SELECT * FROM deliveries WHERE task_id = ? ORDER BY created_at, delivery_id", (task_id,)
        )

    def count_deliveries(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM deliveries WHERE task_id = ?", (task_id,)
        ).fetchone()[0]

    # -- 审核 -------------------------------------------------------------
    def insert_review(self, record):
        record = dict(record)
        record.setdefault("reviewer_id", None)
        self.conn.execute(
            "INSERT INTO reviews (review_id, task_id, delivery_id, decision, note, review_round, "
            "resulting_attempt_id, reviewer_id, created_at) VALUES (:review_id, :task_id, "
            ":delivery_id, :decision, :note, :review_round, :resulting_attempt_id, :reviewer_id, "
            ":created_at)",
            record,
        )

    def list_reviews(self, task_id):
        return self._all(
            "SELECT * FROM reviews WHERE task_id = ? ORDER BY created_at, review_id", (task_id,)
        )

    def count_reviews(self, task_id):
        return self.conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE task_id = ?", (task_id,)
        ).fetchone()[0]

    # -- 活动 / 对账 / 冲突 ----------------------------------------------
    def log_activity(self, task_id, attempt_id, kind, detail=None, at=None):
        self.conn.execute(
            "INSERT INTO activity (task_id, attempt_id, at, kind, detail_json) VALUES (?, ?, ?, ?, ?)",
            (task_id, attempt_id, at, kind, _json(detail)),
        )

    def list_activity(self, task_id):
        return self._all(
            "SELECT * FROM activity WHERE task_id = ? ORDER BY activity_id", (task_id,)
        )

    def insert_reconciliation(self, task_id, event_id, attempt_id, sequence, kind, detail=None, at=None):
        self.conn.execute(
            "INSERT INTO reconciliation (task_id, event_id, attempt_id, sequence, kind, detail_json, at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, event_id, attempt_id, sequence, kind, _json(detail), at),
        )

    def list_reconciliation(self, task_id):
        return self._all(
            "SELECT * FROM reconciliation WHERE task_id = ? ORDER BY recon_id", (task_id,)
        )

    def insert_conflict(self, event_id, task_id, detail=None, at=None):
        self.conn.execute(
            "INSERT INTO conflicts (event_id, task_id, detail_json, at) VALUES (?, ?, ?, ?)",
            (event_id, task_id, _json(detail), at),
        )

    def list_conflicts(self, task_id=None):
        if task_id is None:
            return self._all("SELECT * FROM conflicts ORDER BY conflict_id")
        return self._all(
            "SELECT * FROM conflicts WHERE task_id = ? ORDER BY conflict_id", (task_id,)
        )

    # -- 报告记账（D4-NEXT-01C，只读日程表，不参与任务状态机） ------------
    def insert_report(self, record):
        self.conn.execute(
            "INSERT INTO reports (report_id, report_revision, report_type, project_id, task_id, "
            "attempt_id, delivery_id, source_card_json, requested_by_json, executed_by_json, "
            "reviewed_by_json, adapter, session_id, host_run_id, board_summary_json, "
            "business_summary_json, evidence_json, canonical_digest, source_file_json, "
            "imported_at, imported_by_json, synthetic) "
            "VALUES (:report_id, :report_revision, :report_type, :project_id, :task_id, "
            ":attempt_id, :delivery_id, :source_card_json, :requested_by_json, :executed_by_json, "
            ":reviewed_by_json, :adapter, :session_id, :host_run_id, :board_summary_json, "
            ":business_summary_json, :evidence_json, :canonical_digest, :source_file_json, "
            ":imported_at, :imported_by_json, :synthetic)",
            record,
        )

    def has_reports_table(self):
        """旧库兼容探针：read-only 打开的历史库可能还没有 reports 表。"""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reports'"
        ).fetchone()
        return row is not None

    def get_report(self, report_id, report_revision):
        if not self.has_reports_table():
            return None
        return self._one(
            "SELECT * FROM reports WHERE report_id = ? AND report_revision = ?",
            (report_id, report_revision),
        )

    def list_reports(self, task_id=None, delivery_id=None):
        """报告清单；历史库（无 reports 表）返回空列表，由读模型标记 summary_pending。"""
        if not self.has_reports_table():
            return []
        if task_id is None and delivery_id is None:
            return self._all("SELECT * FROM reports ORDER BY imported_at, report_id")
        if delivery_id is not None:
            return self._all(
                "SELECT * FROM reports WHERE task_id = ? AND delivery_id = ? "
                "ORDER BY imported_at, report_id",
                (task_id, delivery_id),
            )
        return self._all(
            "SELECT * FROM reports WHERE task_id = ? ORDER BY imported_at, report_id", (task_id,)
        )

    # -- 来源任务卡冻结登记（D4-NEXT-01C-R1） ------------------------------
    def has_source_cards_table(self):
        """旧库兼容探针：read-only 打开的历史库可能还没有 task_source_cards 表。"""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_source_cards'"
        ).fetchone()
        return row is not None

    def insert_source_card(self, record):
        """写入来源任务卡登记。

        主键 task_id 唯一：重复写入直接抛 IntegrityError（不做 OR REPLACE），
        防止覆盖既有冻结来源。调用方负责在同一事务内与 insert_task 一起提交。
        registered_by_json 记录受控入口身份（context），旧调用可不传。
        """
        record = dict(record)
        record.setdefault("registered_by_json", None)
        self.conn.execute(
            "INSERT INTO task_source_cards (task_id, project_id, card_id, card_revision, "
            "source_path, source_sha256, canonical_digest, registered_json, "
            "registered_by_json, created_at) "
            "VALUES (:task_id, :project_id, :card_id, :card_revision, :source_path, "
            ":source_sha256, :canonical_digest, :registered_json, "
            ":registered_by_json, :created_at)",
            record,
        )

    def get_source_card(self, task_id):
        if not self.has_source_cards_table():
            return None
        return self._one(
            "SELECT * FROM task_source_cards WHERE task_id = ?", (task_id,)
        )

    # -- 内部 -------------------------------------------------------------
    def _update(self, table, key_column, key_value, fields, allowed):
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        assignments = ", ".join("%s = :%s" % (col, col) for col in clean)
        params = dict(clean)
        params[key_column] = key_value
        self.conn.execute(
            "UPDATE %s SET %s WHERE %s = :%s" % (table, assignments, key_column, key_column),
            params,
        )


def _json(value):
    import json

    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
