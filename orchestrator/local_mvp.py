"""本机 MVP 运行时：loopback Gateway、durable Scheduler 和只读 Dashboard。

默认只提供本地 demo fixture。它用于验证调度、DAG、快照和看板闭环，
不会调用任何真实宿主，也不会把 fixture 状态当成真实执行证据。
"""

import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path

from .dashboard import make_dashboard
from .scheduler import Scheduler
from .server import load_config, make_server


class DemoState:
    """Synthetic task store used only by ``--demo-plan``."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.workspaces = self.root / "demo-workspaces"
        self.workspaces.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.records = {}
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self._create_records()

    def _create_records(self):
        members = {
            "codex-dev": ("codex", "developer"),
            "workbuddy-dev": ("workbuddy", "developer"),
            "qclaw-qa": ("qclaw", "reviewer"),
            "qclaw-controller": ("qclaw", "controller"),
        }
        for task_id, member_id in (
            ("demo-01-codex", "codex-dev"),
            ("demo-02-workbuddy", "workbuddy-dev"),
            ("demo-03-qclaw", "qclaw-qa"),
            ("demo-04-integration", "qclaw-controller"),
        ):
            workspace = self.workspaces / task_id
            workspace.mkdir(mode=0o700, exist_ok=True)
            host, role = members[member_id]
            self.records[task_id] = {
                "taskId": task_id,
                "state": "todo",
                "attemptId": None,
                "syncState": "not_synced",
                "verdicts": {},
                "contract": {
                    "taskId": task_id,
                    "workerId": member_id,
                    "workspace": str(workspace),
                    "revision": 1,
                    "issueId": "demo-issue-" + task_id,
                },
                "host": host,
                "role": role,
            }

    def read_task(self, task_id):
        with self.lock:
            record = self.records[task_id]
            return json.loads(json.dumps(record))

    def launch(self, node, _member, attempt_id):
        task_id = node["taskId"]
        with self.lock:
            record = self.records[task_id]
            record["attemptId"] = attempt_id
            record["state"] = "running"

        # Each branch is deterministic and intentionally synthetic.
        if task_id == "demo-01-codex":
            with self.lock:
                record.update(
                    state="done",
                    syncState="synced",
                    verdicts={"qa": "pass", "reviewer": "pass", "controller": "pass"},
                )
            return
        if task_id == "demo-03-qclaw":
            with self.lock:
                record.update(state="failed", syncState="not_synced")
            return

        # Keep one card visibly running for a short period, then leave it at
        # awaiting_validation. The stop event prevents shutdown from hanging.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline and not self.stop_event.wait(0.2):
            pass
        with self.lock:
            if self.stop_event.is_set():
                record.update(state="unknown", syncState="not_synced")
            else:
                record.update(state="awaiting_validation", syncState="not_synced")

    def stop(self, _task_id, _attempt_id):
        self.stop_event.set()

    def plan(self):
        members = [
            {"memberId": "codex-dev", "host": "codex", "roles": ["developer"], "maxConcurrency": 1},
            {"memberId": "workbuddy-dev", "host": "workbuddy", "roles": ["developer"], "maxConcurrency": 1},
            {"memberId": "qclaw-qa", "host": "qclaw", "roles": ["reviewer"], "maxConcurrency": 1},
            {"memberId": "qclaw-controller", "host": "qclaw", "roles": ["controller"], "maxConcurrency": 1},
        ]
        return {
            "planId": "local-demo-plan",
            "title": "本机 MVP 合成调度演示",
            "revision": 1,
            "maxConcurrency": 2,
            "members": members,
            "nodes": [
                {"taskId": "demo-01-codex", "title": "Codex：完成只读实现", "memberId": "codex-dev", "role": "developer", "dependsOn": []},
                {"taskId": "demo-02-workbuddy", "title": "WorkBuddy：等待独立验收", "memberId": "workbuddy-dev", "role": "developer", "dependsOn": []},
                {"taskId": "demo-03-qclaw", "title": "QClaw：展示失败闭环", "memberId": "qclaw-qa", "role": "reviewer", "dependsOn": []},
                {"taskId": "demo-04-integration", "title": "中枢：等待上游同步后集成", "memberId": "qclaw-controller", "role": "controller", "dependsOn": ["demo-01-codex", "demo-02-workbuddy"]},
            ],
        }


class ImportedPlanState:
    """Frozen Paperclip plan projected into Scheduler without dispatch."""

    def __init__(self, document):
        if not isinstance(document, dict) or document.get("schema") != "paperclip-orchestrator-plan/v1":
            raise ValueError("INVALID_IMPORTED_PLAN_SCHEMA")
        plan = document.get("plan")
        tasks = document.get("tasks")
        if not isinstance(plan, dict) or not isinstance(tasks, list) or not tasks:
            raise ValueError("INVALID_IMPORTED_PLAN")
        self.document = document
        self.records = {}
        for spec in tasks:
            if not isinstance(spec, dict) or spec.get("taskId") in self.records:
                raise ValueError("INVALID_IMPORTED_TASK_SPEC")
            workspace = Path(spec.get("workspace", ""))
            if not workspace.is_absolute() or not workspace.is_dir() or workspace.is_symlink() or str(workspace.resolve()) != str(workspace):
                raise ValueError("IMPORTED_WORKSPACE_NOT_READY")
            self.records[spec["taskId"]] = {
                "taskId": spec["taskId"],
                "state": "todo",
                "attemptId": None,
                "syncState": "not_synced",
                "verdicts": {},
                "contract": spec,
            }

    def read_task(self, task_id):
        record = self.records[task_id]
        return json.loads(json.dumps(record))

    def launch(self, *_args):
        raise RuntimeError("PLAN_PREVIEW_ONLY")

    def stop(self, *_args):
        return None

    def plan(self):
        return self.document["plan"]


def _token_sha256(token):
    return hashlib.sha256(token.encode()).hexdigest()


class LocalMvp:
    """Owns all local services and their shutdown order."""

    def __init__(self, state_dir, gateway_port=0, dashboard_port=0, demo_plan=False, plan_document=None):
        self.root = Path(state_dir).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
            raise ValueError("LOCAL_MVP_STATE_MUST_BE_PRIVATE")
        if any(self.root.iterdir()):
            raise ValueError("LOCAL_MVP_STATE_MUST_BE_EMPTY")
        self.gateway_root = self.root / "gateway"
        self.scheduler_root = self.root / "scheduler"
        self.config_path = self.root / "gateway.json"
        self.gateway_token = secrets.token_urlsafe(32)
        self.dashboard_token = secrets.token_urlsafe(32)
        if demo_plan and plan_document is not None:
            raise ValueError("DEMO_AND_IMPORTED_PLAN_ARE_EXCLUSIVE")
        self.demo = DemoState(self.root) if demo_plan else None
        self.imported = ImportedPlanState(plan_document) if plan_document is not None else None
        config = {
            "stateDir": str(self.gateway_root),
            "principals": [{
                "id": "local-admin",
                "role": "admin",
                "tasks": ["*"],
                "tokenSha256": _token_sha256(self.gateway_token),
            }],
            "paperclip": None,
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        os.chmod(self.config_path, 0o600)
        self.gateway = make_server(load_config(str(self.config_path)), gateway_port)
        if self.demo:
            self.scheduler = Scheduler(
                str(self.scheduler_root), self.demo.read_task, self.demo.launch,
                max_concurrency=2, stop=self.demo.stop,
            )
        elif self.imported:
            self.scheduler = Scheduler(
                str(self.scheduler_root), self.imported.read_task, self.imported.launch,
                max_concurrency=self.imported.plan().get("maxConcurrency", 1), stop=self.imported.stop,
            )
        else:
            self.scheduler = Scheduler(str(self.scheduler_root), lambda _task: {"state": "todo"}, lambda *_args: None)
        self.dashboard = make_dashboard(self.snapshot, self.dashboard_token, dashboard_port)
        self.gateway_thread = None
        self.dashboard_thread = None
        self.scheduler_thread = None
        self.stop_event = threading.Event()

    def snapshot(self):
        result = self.scheduler.snapshot()
        result.update({
            "live": True,
            "mode": "demo" if self.demo else "plan-preview" if self.imported else "empty",
            "evidenceType": "demo/fixture" if self.demo else "paperclip/plan-preview" if self.imported else "local-empty",
        })
        if self.imported:
            result["paperclip"] = {
                key: self.imported.document["paperclip"].get(key)
                for key in ("issueId", "title", "status", "projectId")
                if self.imported.document.get("paperclip", {}).get(key) is not None
            }
        return result

    def _tick_loop(self):
        while not self.stop_event.wait(0.5):
            if not self.demo:
                continue
            try:
                self.scheduler.tick()
            except Exception:
                # The Scheduler records per-card errors. Keep the local control
                # plane alive so the dashboard can show the last safe snapshot.
                pass

    def start(self):
        self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, name="mvp-gateway", daemon=True)
        self.dashboard_thread = threading.Thread(target=self.dashboard.serve_forever, name="mvp-dashboard", daemon=True)
        self.gateway_thread.start()
        self.dashboard_thread.start()
        if self.demo:
            self.scheduler.submit_plan(self.demo.plan())
        elif self.imported:
            self.scheduler.submit_plan(self.imported.plan())
        self.scheduler_thread = threading.Thread(target=self._tick_loop, name="mvp-scheduler", daemon=True)
        self.scheduler_thread.start()
        if self.demo:
            self.scheduler.tick()
        return self

    def close(self):
        self.stop_event.set()
        if self.demo:
            self.demo.stop_event.set()
        if self.scheduler_thread:
            self.scheduler_thread.join(timeout=3)
        self.dashboard.shutdown()
        self.gateway.shutdown()
        self.dashboard.server_close()
        self.gateway.server_close()
        self.scheduler.close(wait=True)

    @property
    def addresses(self):
        return {
            "gateway": "http://127.0.0.1:" + str(self.gateway.server_port),
            "dashboard": "http://127.0.0.1:" + str(self.dashboard.server_port),
        }
