"""Local file bridge: worker-authored events, never inferred from chat text.

The controlled caller binds task/attempt; this is not production authentication.

交付文件边界（fail closed）：
- 报告文件（``reportPath``，以及约定名的 ``delivery-report.md`` / ``result.md``）必须属于
  本轮的 ``receipts/<taskId>/<attemptId>/`` 目录，跨轮次、越界、符号链接、目录、超大文件一律拒绝。
- ``changedFiles`` 允许列出任务 ``permissions.writeScope`` 内的源码路径（不只是回执目录），
  但仍须通过 ``assert_within_scope``（含 realpath 逃逸判定）与 ``read_regular``（独立普通文件 +
  大小上限 + 读取期间不可变）检查；writeScope 之外或 workspace 之外的路径一律拒绝。
"""
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path

from contracts.paths import assert_within_scope
from contracts.schemas import ContractError, validate_event
from service.context import EVENT_INGEST, require_context
from service.tasks import now_iso

WORKER_EVENTS = ("task.started", "task.progress", "task.delivered", "task.blocked")
MAX_BYTES = 1024 * 1024

#: 报告文件约定名：即使写在 changedFiles 里，也不得离开本轮回执目录。
REPORT_FILENAMES = ("delivery-report.md", "result.md")


def receipt_directory(task_id, attempt_id):
    return "receipts/%s/%s" % (task_id, attempt_id)


def delivery_files(payload):
    """交付事件里需要校验的文件清单：``(entry, is_report)``。

    ``reportPath`` 恒为报告文件；``changedFiles`` 默认只是产出文件（允许 writeScope 内的源码路径），
    但约定报告名（见 ``REPORT_FILENAMES``）仍按报告文件处理，防止把报告语义挪出回执目录。
    """
    files = [(payload["reportPath"], True)]
    files.extend((entry, False) for entry in payload.get("changedFiles", []))
    return files


def read_regular(workspace, entry):
    root = Path(workspace)
    target = Path(entry) if Path(entry).is_absolute() else root / entry
    try:
        parts = target.relative_to(root).parts
    except ValueError:
        raise ContractError("path_escape", message="回执文件越出 workspace")
    if not parts or any(p in (".", "..") for p in parts):
        raise ContractError("path_escape", message="回执文件路径无效")
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_BYTES:
                raise ContractError("invalid_event", message="回执必须是大小受限的独立普通文件")
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                data = handle.read(MAX_BYTES + 1)
            after = os.fstat(file_fd)
            if (len(data) > MAX_BYTES or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns):
                raise ContractError("invalid_event", message="读取过程中回执变化，停止导入")
            return data
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise ContractError("path_escape", message="回执文件不可安全读取：%s" % exc)
    finally:
        os.close(fd)


def import_receipt(store, events, task_id, attempt_id, file_path, context):
    require_context(context, EVENT_INGEST)
    task, attempt = store.get_task(task_id), store.get_attempt(attempt_id)
    if not task or not attempt or attempt["task_id"] != task_id:
        raise ContractError("invalid_task", message="回执任务/轮次不存在或不匹配")
    if context.actor_id != task["source_id"]:
        raise ContractError("permission_denied", message="回执来源不匹配")
    if not attempt["submitted_at"] or not attempt["accepted_by_host_at"]:
        raise ContractError("host_unconfirmed", message="轮次尚未获得真实接收记录")
    expected = Path(task["workspace"]) / receipt_directory(task_id, attempt_id) / "events.json"
    if Path(file_path) != expected:
        raise ContractError("path_escape", message="只允许导入指定任务/轮次的 events.json")
    raw = read_regular(task["workspace"], str(expected))
    try:
        bundle = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ContractError("invalid_event", message="回执不是合法 JSON")
    submission = json.loads(attempt["submission_json"] or "{}")
    activity = store.list_activity(task_id)
    if any(row["attempt_id"] == attempt_id and row["kind"] == "attempt.submitted"
           and json.loads(row["detail_json"]).get("synthetic") for row in activity):
        context = replace(context, synthetic=True)
    if (not isinstance(bundle, dict) or bundle.get("hostRunId") != submission.get("host_receipt")
            or not bundle.get("hostRunId")):
        raise ContractError("host_unconfirmed", message="回执 hostRunId 与可信接收记录不匹配")
    items = bundle.get("events")
    if not isinstance(items, list) or not 1 <= len(items) <= 16:
        raise ContractError("invalid_event", message="回执 events 必须是 1..16 条数组")
    hashes, digests, ids, previous_sequence = {}, {}, set(), 0
    for item in items:
        errors = validate_event(item)
        if errors:
            raise ContractError("invalid_event", details=errors)
        expected_bindings = {"taskId": task_id, "attemptId": attempt_id,
                             "projectId": task["project_id"], "sourceId": task["source_id"],
                             "agentId": task["agent_id"]}
        if any(item.get(k) != v for k, v in expected_bindings.items()):
            raise ContractError("permission_denied", message="回执事件绑定与受控任务不一致")
        if item["type"] not in WORKER_EVENTS:
            raise ContractError("permission_denied", message="回执只能上报 worker 事件")
        if item["eventId"] in ids or item["sequence"] <= previous_sequence:
            raise ContractError("invalid_event", message="回执内部事件编号/顺序冲突")
        ids.add(item["eventId"])
        previous_sequence = item["sequence"]
        if item["type"] == "task.delivered":
            payload = item["payload"]
            permissions = json.loads(task["permissions_json"])
            scopes = permissions.get("writeScope") or []
            receipt_root = Path(os.path.realpath(str(expected.parent)))
            for entry, is_report in delivery_files(payload):
                # writeScope 声明校验：changedFiles 里的源码路径同样必须在授权范围内。
                assert_within_scope(task["workspace"], scopes, entry)
                target = Path(entry) if Path(entry).is_absolute() else Path(task["workspace"]) / entry
                resolved = Path(os.path.realpath(str(target)))
                # 报告文件始终留在本轮回执目录；源码产出文件只需在 writeScope 内（见模块说明）。
                if is_report or resolved.name in REPORT_FILENAMES:
                    if resolved != receipt_root and receipt_root not in resolved.parents:
                        raise ContractError("path_escape", message="交付文件必须属于本轮回执目录")
                key = str(target)
                if key in hashes:
                    continue  # 同一路径重复出现：不重复读取，哈希语义不变
                if resolved in digests:
                    hashes[key] = digests[resolved]  # 同一文件的不同写法：复用已读取内容
                    continue
                data = read_regular(task["workspace"], str(target))
                digests[resolved] = hashes[key] = hashlib.sha256(data).hexdigest()
    receipt_hash = hashlib.sha256(raw).hexdigest()
    for row in activity:
        if row["attempt_id"] == attempt_id and row["kind"] == "file_receipt.imported":
            prior = json.loads(row["detail_json"])
            recorded = prior.get("fileSha256", {})
            if recorded and hashes and recorded != hashes:
                raise ContractError("event_conflict", message="已交付的文件内容变化，不接受重新绑定")
            if prior.get("receiptSha256") == receipt_hash:
                return {**prior, "replayed": True}
    # Each event retains the service's durable first-result semantics. Partial imports
    # can be replayed with the identical file; never rewrite already imported events.
    results = [events.ingest(item, context) for item in items]
    result = {"taskId": task_id, "attemptId": attempt_id, "hostRunId": bundle["hostRunId"],
              "receiptSha256": receipt_hash, "fileSha256": hashes,
              "events": results, "synthetic": context.synthetic}
    with store.transaction():
        for row in store.list_activity(task_id):
            if (row["attempt_id"] == attempt_id and row["kind"] == "file_receipt.imported"
                    and json.loads(row["detail_json"]).get("receiptSha256") == receipt_hash):
                return {**json.loads(row["detail_json"]), "replayed": True}
        store.log_activity(task_id, attempt_id, "file_receipt.imported", result, now_iso())
    return result
