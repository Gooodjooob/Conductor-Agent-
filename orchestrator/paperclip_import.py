"""Read-only Paperclip issue import and frozen Scheduler plan documents."""

import json
import os
import stat
import time
from pathlib import Path

from .gateway import Rejected, identifier, require


ALLOWED_SOURCE_STATES = {"todo", "in_progress", "in_review"}


def private_json(path):
    target = Path(path)
    fd = os.open(str(target), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_mode & 0o077 == 0,
            "PLAN_INPUT_MUST_BE_PRIVATE",
            400,
        )
        return json.load(stream)


def write_private_json(path, value):
    target = Path(path)
    require(target.is_absolute(), "PLAN_OUTPUT_MUST_BE_ABSOLUTE", 400)
    require(not target.exists() and not target.is_symlink(), "PLAN_OUTPUT_EXISTS", 409)
    parent = target.parent
    require(parent.is_dir() and parent.stat().st_uid == os.getuid() and parent.stat().st_mode & 0o077 == 0,
            "PLAN_OUTPUT_PARENT_MUST_BE_PRIVATE", 400)
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            target.unlink()
        except OSError:
            pass
        raise


class PaperclipIssueImporter:
    """Fetch exactly one issue from a configured, authenticated Paperclip."""

    def __init__(self, client):
        self.client = client

    def read_issue(self, issue_id):
        require(identifier(issue_id), "INVALID_PAPERCLIP_ISSUE_ID", 400)
        status, issue = self.client.request("GET", "/issues/" + issue_id)
        require(status == 200 and isinstance(issue, dict), "PAPERCLIP_ISSUE_READ_FAILED", 502)
        require(issue.get("id") == issue_id, "PAPERCLIP_ISSUE_ID_MISMATCH", 502)
        source_status = issue.get("status")
        require(source_status in ALLOWED_SOURCE_STATES, "PAPERCLIP_ISSUE_NOT_DISPATCHABLE", 409)
        title = issue.get("title") or issue.get("name")
        require(isinstance(title, str) and 0 < len(title) <= 200, "PAPERCLIP_ISSUE_TITLE_REQUIRED", 502)
        description = issue.get("description", "")
        require(isinstance(description, str) and len(description) <= 200000, "PAPERCLIP_ISSUE_DESCRIPTION_INVALID", 502)
        # Keep only planning metadata. Never copy arbitrary issue fields into
        # a plan where a token, cookie, or private provider field could leak.
        return {
            "issueId": issue_id,
            "title": title,
            "description": description,
            "status": source_status,
            "projectId": issue.get("projectId") if isinstance(issue.get("projectId"), str) else None,
            "parentId": issue.get("parentId") if isinstance(issue.get("parentId"), str) else None,
        }


def _plan_and_tasks(source):
    require(isinstance(source, dict), "INVALID_PLAN_INPUT", 400)
    plan = source.get("plan", source)
    tasks = source.get("tasks")
    require(isinstance(plan, dict), "INVALID_PLAN", 400)
    require(isinstance(tasks, list) and tasks, "TASK_SPECS_REQUIRED", 400)
    return plan, tasks


def build_plan_document(issue, source):
    """Bind an explicit split plan to one Paperclip issue without writing upstream."""
    require(isinstance(issue, dict) and identifier(issue.get("issueId")), "INVALID_ISSUE_CONTEXT", 400)
    plan, tasks = _plan_and_tasks(source)
    require(plan.get("paperclipIssueId", issue["issueId"]) == issue["issueId"], "PAPERCLIP_ISSUE_BINDING_MISMATCH", 409)
    require(identifier(plan.get("planId")), "INVALID_PLAN_ID", 400)
    require(isinstance(plan.get("title"), str) and 0 < len(plan["title"]) <= 200, "INVALID_PLAN_TITLE", 400)
    require(type(plan.get("revision")) is int and plan["revision"] > 0, "INVALID_PLAN_REVISION", 400)
    nodes = plan.get("nodes")
    require(isinstance(nodes, list) and nodes, "PLAN_NODES_REQUIRED", 400)
    node_ids = [node.get("taskId") for node in nodes if isinstance(node, dict)]
    require(len(node_ids) == len(nodes) and len(set(node_ids)) == len(node_ids) and all(identifier(n) for n in node_ids), "INVALID_PLAN_NODE_IDS", 400)
    specs = {}
    for spec in tasks:
        require(isinstance(spec, dict) and identifier(spec.get("taskId")), "INVALID_TASK_SPEC", 400)
        require(spec["taskId"] not in specs, "DUPLICATE_TASK_SPEC", 400)
        require(spec.get("taskId") in node_ids, "TASK_SPEC_NOT_IN_PLAN", 400)
        require(identifier(spec.get("issueId")), "TASK_ISSUE_ID_REQUIRED", 400)
        require(isinstance(spec.get("workspace"), str) and Path(spec["workspace"]).is_absolute(), "ABSOLUTE_WORKSPACE_REQUIRED", 400)
        require(isinstance(spec.get("artifactPaths"), list) and spec["artifactPaths"], "TASK_ARTIFACTS_REQUIRED", 400)
        require(isinstance(spec.get("children"), list), "TASK_CHILDREN_REQUIRED", 400)
        require(isinstance(spec.get("upstreamPolicy"), dict), "TASK_POLICY_REQUIRED", 400)
        specs[spec["taskId"]] = spec
    require(set(specs) == set(node_ids), "PLAN_TASK_SPEC_MISMATCH", 400)
    for node in nodes:
        spec = specs[node["taskId"]]
        require(spec["issueId"] != issue["issueId"] or len(nodes) == 1, "ROOT_ISSUE_ID_COLLISION", 409)
        require(spec["upstreamPolicy"].get("parentId") in (None, issue.get("parentId"), issue["issueId"]), "UPSTREAM_PARENT_MISMATCH", 409)
    frozen_plan = dict(plan)
    frozen_plan.pop("paperclipIssueId", None)
    return {
        "schema": "paperclip-orchestrator-plan/v1",
        "paperclip": dict(issue, importedAt=time.time()),
        "plan": frozen_plan,
        "tasks": [specs[task_id] for task_id in node_ids],
        "writeBack": "controller-finalize-only",
    }

