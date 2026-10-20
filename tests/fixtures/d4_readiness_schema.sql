
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
