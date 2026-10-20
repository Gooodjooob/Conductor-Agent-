"""Read-only project registration from curated historical documents.

This catalog never creates tasks, attempts, deliveries or trusted host events.
Material views use pinned snapshots below the catalog directory, not arbitrary paths.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote
from projections.project_sources import observe_projects

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_STATUSES = {"verified", "validated", "partial", "in_progress", "blocked", "planned", "awaiting_review"}


class ProjectCatalog:
    def __init__(self, path):
        self.path = Path(path).resolve(strict=True)
        if self.path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("catalog_too_large")
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schemaVersion") != "project-catalog.v1":
            raise ValueError("catalog_schema_invalid")
        self.rows = data.get("projects")
        if not isinstance(self.rows, list):
            raise ValueError("catalog_projects_invalid")
        self.project_ids, self.task_ids = set(), set()
        for project in self.rows:
            self._unique(project.get("projectId"), self.project_ids)
            if not isinstance(project.get("name"), str) or not project["name"].strip():
                raise ValueError("catalog_project_name_invalid")
            stages = project.get("milestones", [])
            if not isinstance(stages, list):
                raise ValueError("catalog_milestones_invalid")
            stage_ids = set()
            for stage in stages:
                self._unique(stage.get("id"), stage_ids)
            tasks = project.get("tasks")
            if not isinstance(tasks, list):
                raise ValueError("catalog_tasks_invalid")
            for task in tasks:
                self._unique(task.get("taskId"), self.task_ids)
                if task.get("status") not in _STATUSES or task.get("milestoneId") not in stage_ids:
                    raise ValueError("catalog_task_classification_invalid")
                if not isinstance(task.get("documents", []), list):
                    raise ValueError("catalog_documents_invalid")
                for document in task.get("documents", []):
                    self._snapshot(document)
            for issue in project.get("issues", []):
                if issue.get("taskId") not in {t["taskId"] for t in tasks}:
                    raise ValueError("catalog_issue_task_unknown")

    @staticmethod
    def _unique(value, known):
        if not isinstance(value, str) or not _ID.fullmatch(value) or value in known:
            raise ValueError("catalog_identifier_invalid")
        known.add(value)

    def _snapshot(self, document):
        value = document.get("snapshot")
        if not isinstance(value, str) or Path(value).is_absolute():
            raise ValueError("catalog_snapshot_invalid")
        path = (self.path.parent / value).resolve(strict=True)
        try:
            path.relative_to(self.path.parent)
        except ValueError as exc:
            raise ValueError("catalog_snapshot_escape") from exc
        if not path.is_file() or path.stat().st_size > 512 * 1024:
            raise ValueError("catalog_snapshot_invalid")
        return path

    @staticmethod
    def _project(project):
        return dict(project, recordType="archive", evidenceType="document_archive")

    def projects(self):
        return observe_projects([self._project(p) for p in self.rows])

    def task(self, task_id):
        for project in self.projects():
            for task in project["tasks"]:
                if task["taskId"] == task_id:
                    return {
                        "recordType": "archive", "evidenceType": "document_archive",
                        "projectId": project["projectId"], "projectName": project["name"],
                        "task": task, "milestone": next(s for s in project["milestones"] if s["id"] == task["milestoneId"]),
                        "issues": [i for i in project.get("issues", []) if i["taskId"] == task_id],
                    }
        raise KeyError(task_id)

    def route(self, route):
        if route.startswith("/api/projects/"):
            project_id = unquote(route[len("/api/projects/"):])
            return next((self._project(p) for p in self.projects() if p["projectId"] == project_id), None)
        if route.startswith("/api/tasks/"):
            task_id = unquote(route[len("/api/tasks/"):])
            return self.task(task_id) if task_id in self.task_ids else None
        if route.startswith("/api/materials/"):
            parts = route[len("/api/materials/"):].split("/")
            if len(parts) != 2 or not parts[1].isdigit():
                raise KeyError(route)
            task = self.task(unquote(parts[0]))["task"]
            index = int(parts[1])
            documents = task.get("documents", [])
            if index >= len(documents):
                raise KeyError(route)
            document = documents[index]
            raw = self._snapshot(document).read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != document.get("sha256"):
                raise ValueError("catalog_snapshot_changed")
            return {"name": document["name"], "kind": document["kind"],
                    "content": raw.decode("utf-8"), "sha256": digest,
                    "sourcePath": document["sourcePath"]}
        return None
