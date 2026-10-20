"""Detached single-attempt WorkBuddy runner; persistent inspection, never resubmits.

任务卡 D4-AUTOMATION-GUARD-IMPLEMENTATION-01：start 新增必需 --workflow；
start 与 _execute 都在宿主调用前读取受信 workflow 的最新 modelPolicy 并校验
模型授权 / 生效时间 / transparentFallback=false / 绑定关系；策略冻结进 config，
_execute 重新核验 SHA，漂移或撤销即拒绝，不自动接受新策略，不提供透明 fallback。

本卡增量：start 与 _execute 共同校验 workflow 顶层 db / allowedAgents / allowedSource
绑定（与冻结 args/config 精确一致，缺失即拒绝，不以 modelPolicy 内字段替代）；execute
以 read_only get_attempt 复核冻结 attempt 归属；重复 execute 先抛 FileExistsError 且不改
动旧 state/result；模型/绑定拒绝持久化 attention_required 并直接 return 1（cox 裁定）。

本卡（r4）增量：start 对 args.allowed_root、_execute 对 config.allowedRoot 复用已有一
_real_absolute，直接 symlink 或含 symlink 祖先的路径以专用错误码 ALLOWED_ROOT_INVALID
拒绝；start 的校验先于建 runtime 目录 / create_attempt / Popen，execute 的拒绝走既有
attention_required 分支（return 1，不建 started、不调 submit / import）。相对路径仍拒绝，
真实正常根仍可启动。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.workbuddy import (ALLOWED_WORKBUDDY_MODELS, DEFAULT_WORKBUDDY_MODEL, WORKBUDDY_AGENT_ID, WORKBUDDY_AGENT_IDS,
                               WORKBUDDY_SOURCE_ID, workbuddy_binding)
from scripts import d4ctl
from service.context import worker_context
from service.events import EventService
from service.file_receipts import import_receipt, receipt_directory
from service.context import source_context
from service.tasks import TaskService, now_iso
from storage.sqlite_store import SqliteStore
from adapters.task_permissions import build_policy

WORKFLOW_SCHEMA = "d4-workflow.v1"
MODEL_POLICY_SCHEMA = "d4-model-policy.v1"


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_workflow_file(workflow_path):
    """读取受信 workflow 文件：绝对、无符号链接、普通文件、JSON 对象。"""
    if not workflow_path:
        raise ValueError("WORKFLOW_PATH_MISSING")
    path = Path(workflow_path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("workflow 必须是无符号链接的绝对真实路径")
    if not path.is_file():
        raise ValueError("workflow 文件必须已存在")
    try:
        workflow = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ValueError("WORKFLOW_JSON_INVALID:%s" % exc) from exc
    if not isinstance(workflow, dict):
        raise ValueError("workflow 必须是 JSON 对象")
    if workflow.get("schemaVersion") != WORKFLOW_SCHEMA:
        raise ValueError("WORKFLOW_SCHEMA_UNSUPPORTED")
    return path, workflow


def validate_workflow_binding(workflow, expected_db, expected_task, agent_id=WORKBUDDY_AGENT_ID):
    """校验受信 workflow 的顶层 db / agent / source / task 绑定。

    只用 workflow 顶层字段判定；绝不以 modelPolicy 内字段替代 workflow 字段。
    任一缺失、类型错误或与冻结值不一致都拒绝（start 与 execute 共用）。
    """
    bound_db = workflow.get("db")
    if not isinstance(bound_db, str) or not bound_db:
        raise ValueError("WORKFLOW_DB_BINDING_MISMATCH")
    # workflow.db 必须是无符号链接的绝对真实路径，且与 args/config.db 精确一致。
    candidate = Path(bound_db)
    if not candidate.is_absolute() or candidate.is_symlink() or candidate.resolve() != candidate:
        raise ValueError("WORKFLOW_DB_BINDING_MISMATCH")
    if bound_db != str(expected_db):
        raise ValueError("WORKFLOW_DB_BINDING_MISMATCH")
    agents = workflow.get("allowedAgents")
    if not isinstance(agents, list) or agent_id not in WORKBUDDY_AGENT_IDS or agent_id not in agents:
        raise ValueError("WORKFLOW_ALLOWED_AGENTS_INVALID")
    if workflow.get("allowedSource") != WORKBUDDY_SOURCE_ID:
        raise ValueError("WORKFLOW_SOURCE_BINDING_MISMATCH")
    if workflow.get("taskId") != expected_task:
        raise ValueError("WORKFLOW_TASK_BINDING_MISMATCH")
    return workflow


def _parse_policy_time(value, code):
    if not isinstance(value, str) or not value:
        raise ValueError(code)
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(code) from exc
    if moment.tzinfo is None:
        raise ValueError(code)
    return moment


def validate_model_policy(policy, requested_model, now=None, agent_id=WORKBUDDY_AGENT_ID):
    """宿主调用前的模型授权门禁；任何不满足都以专用错误码拒绝。"""
    now = now or datetime.now(timezone.utc)
    if not isinstance(policy, dict):
        raise ValueError("MODEL_POLICY_MISSING")
    if policy.get("schemaVersion") != MODEL_POLICY_SCHEMA:
        raise ValueError("MODEL_POLICY_SCHEMA_UNSUPPORTED")
    primary = policy.get("primary")
    allowed = policy.get("allowedModels")
    if not isinstance(allowed, list) or not allowed \
            or not all(isinstance(m, str) and m in ALLOWED_WORKBUDDY_MODELS for m in allowed):
        raise ValueError("MODEL_POLICY_MODELS_INVALID")
    if requested_model not in allowed:
        raise ValueError("MODEL_NOT_AUTHORIZED:%s" % requested_model)
    if primary not in allowed:
        raise ValueError("MODEL_POLICY_PRIMARY_NOT_ALLOWED")
    if policy.get("transparentFallback") is not False:
        raise ValueError("MODEL_POLICY_FALLBACK_NOT_DISABLED")
    if _parse_policy_time(policy.get("effectiveAt"), "MODEL_POLICY_TIME_INVALID") > now:
        raise ValueError("MODEL_POLICY_FUTURE_EFFECTIVE")
    if _parse_policy_time(policy.get("authorizedAt"), "MODEL_POLICY_TIME_INVALID") > now:
        raise ValueError("MODEL_POLICY_AUTHORIZATION_FUTURE")
    agents = policy.get("allowedAgents")
    if not isinstance(agents, list) or agent_id not in WORKBUDDY_AGENT_IDS or agent_id not in agents:
        raise ValueError("MODEL_POLICY_AGENT_NOT_ALLOWED")
    sources = policy.get("allowedSources")
    if not isinstance(sources, list) or WORKBUDDY_SOURCE_ID not in sources:
        raise ValueError("MODEL_POLICY_SOURCE_NOT_ALLOWED")
    roots = policy.get("allowedRoots")
    if not isinstance(roots, list) or not roots:
        raise ValueError("MODEL_POLICY_ROOTS_INVALID")
    for root in roots:
        # 根必须是绝对真实路径；符号链接祖先或非真实绝对路径一律拒绝，
        # 以免借符号链接把执行权限扩到 args.allowed-root 之外。
        if not isinstance(root, str) or not root:
            raise ValueError("MODEL_POLICY_ROOTS_INVALID")
        candidate = Path(root)
        if not candidate.is_absolute() or candidate.is_symlink() or candidate.resolve() != candidate:
            raise ValueError("MODEL_POLICY_ROOTS_INVALID")
    return policy


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def private_directory(path):
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError("runtime-dir 必须是无符号链接的绝对路径")
    if not path.is_dir() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise ValueError("runtime-dir 必须是当前用户的 0700 目录")


def _real_absolute(path, code):
    """路径门禁：必须是无符号链接的绝对真实路径（拒绝符号链接祖先与相对路径）。"""
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path:
        raise ValueError(code)
    return path


def _within(path, root):
    """path 是否等于 root 或位于 root 之下（两者都先解析为真实路径）。"""
    path = Path(path).resolve()
    root = Path(root).resolve()
    return path == root or root in path.parents


def inspect_run(directory):
    private_directory(directory)
    config = json.loads((directory / "config.json").read_text())
    state = json.loads((directory / "state.json").read_text())
    lock_fd = os.open(directory / "runner.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            alive = False
        except BlockingIOError:
            alive = True
    finally:
        os.close(lock_fd)
    result = {"taskId": config["taskId"], "attemptId": config["attemptId"],
              "runnerAlive": alive, "state": state,
              "model": config["model"], "binding": config["binding"]}
    result_path = directory / "result.json"
    if result_path.is_file():
        result["result"] = json.loads(result_path.read_text())
    runtime_path = (Path(config["home"]) / "d4-workbuddy-store/attempts" /
                    config["attemptId"] / "runtime.json")
    if runtime_path.is_file():
        result["hostRuntime"] = json.loads(runtime_path.read_text())
    if not alive and state["status"] not in ("finished", "attention_required"):
        result["state"] = {**state, "status": "execution_unknown",
                           "message": "运行器已退出但没有最终结果；先核查进程和产物，禁止重发"}
    return result


def collect_attempt(config):
    store = SqliteStore(config["db"])
    try:
        task = store.get_task(config["taskId"])
        attempt = store.get_attempt(config["attemptId"])
        if (not task or not attempt or attempt["task_id"] != task["task_id"]
                or task["source_id"] != WORKBUDDY_SOURCE_ID
                or task["agent_id"] != config.get("agentId", WORKBUDDY_AGENT_ID)
                or not attempt["accepted_by_host_at"]):
            return {"status": "skipped", "reason": "host_unconfirmed"}
        path = Path(task["workspace"]) / receipt_directory(task["task_id"], attempt["attempt_id"]) / "events.json"
        if not path.exists() and not path.is_symlink():
            return {"status": "waiting", "reason": "receipt_missing"}
        imported = import_receipt(store, EventService(store), task["task_id"],
                                  attempt["attempt_id"], str(path),
                                  source_context(WORKBUDDY_SOURCE_ID, entry="d4_workbuddy_run"))
        return {"status": "unchanged" if imported.get("replayed") else "imported",
                "eventCount": len(imported["events"]), "taskStatus": store.get_task(task["task_id"])["status"]}
    finally:
        store.close()


def recover_collection(directory):
    """Only re-import an accepted attempt's files; never calls submit or changes attempts."""
    private_directory(directory)
    fd = os.open(directory / "runner.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((directory / "config.json").read_text())
        collection = collect_attempt(config)
        complete = collection.get("taskStatus") in ("awaiting_review", "approved")
        write_json(directory / "state.json", {"status": "finished" if complete else "attention_required",
                   "at": now_iso(), "collection": collection, "recovery": "collection_only",
                   "message": "报告已交付，等待审核" if complete else "仅重采集；未重发任务"})
        return collection
    finally:
        os.close(fd)


def _verify_policy_for_execution(config):
    """_execute 宿主调用前的最终门禁：策略漂移 / 撤销 / 绑定漂移一律拒绝。

    在调用宿主前复核 task / db / source / agent / workspace 绑定与策略 SHA；
    读取 DB 一律 read_only 且不迁移。工作流任务切换或权限收窄即停止，
    不猜最新任务、不提供透明 fallback。
    """
    policy = config.get("modelPolicy")
    if not isinstance(policy, dict):
        # 旧 runtime 没有 policy：inspect/collect 仍可读，但禁止重新 _execute。
        raise ValueError("MODEL_POLICY_MISSING")
    # db 绑定：冻结时写入的数据库路径必须仍是无符号链接的绝对真实路径。
    db = _real_absolute(config.get("db"), "DB_PATH_INVALID")
    # allowedRoot 冻结值同样必须是无符号链接的绝对真实路径：直接 symlink 或含 symlink
    # 祖先的路径用专用错误码拒绝，进入宿主调用前即停；拒绝路径由 execute 持久化
    # attention_required 并直接 return 1，不建 started、不调 submit / import。
    _real_absolute(config.get("allowedRoot"), "ALLOWED_ROOT_INVALID")
    _, workflow = load_workflow_file(config.get("workflowPath"))
    # workflow 顶层 db / agent / source / task 绑定必须与冻结 config 精确一致，缺失即拒绝。
    validate_workflow_binding(workflow, config.get("db"), config.get("taskId"), config.get("agentId", WORKBUDDY_AGENT_ID))
    # workflow.attemptId 只允许 null 或与冻结 attempt 精确相等；空串 / False 等类型拒绝。
    bound_attempt = workflow.get("attemptId")
    if bound_attempt is not None and bound_attempt != config.get("attemptId"):
        raise ValueError("WORKFLOW_ATTEMPT_BINDING_MISMATCH")
    current = workflow.get("modelPolicy")
    if not isinstance(current, dict) \
            or sha256_text(_canonical_json(current)) != config.get("modelPolicySha256"):
        raise ValueError("MODEL_POLICY_DRIFT")
    validate_model_policy(current, config.get("model"), agent_id=config.get("agentId", WORKBUDDY_AGENT_ID))
    allowed_root = config.get("allowedRoot")
    # task / source / agent / attempt / workspace 绑定：read_only 复核，不迁移、不写库。
    store = SqliteStore(db, read_only=True)
    try:
        task = store.get_task(config.get("taskId"))
        if (not task or task["task_id"] != config.get("taskId")
                or task["source_id"] != WORKBUDDY_SOURCE_ID
                or task["agent_id"] != config.get("agentId", WORKBUDDY_AGENT_ID)):
            raise ValueError("EXECUTION_BINDING_MISMATCH")
        # 冻结 attempt 必须真实存在且 task_id 归属冻结 task；缺失或归属错误即拒绝。
        attempt = store.get_attempt(config.get("attemptId"))
        if not attempt or attempt["task_id"] != config.get("taskId"):
            raise ValueError("EXECUTION_ATTEMPT_BINDING_MISMATCH")
        workspace = task["workspace"]
        permissions = json.loads(task["permissions_json"])
        build_policy(task["agent_id"], workspace, permissions, allowed_roots=[config["allowedRoot"]])
        if config.get("taskPermissionsSha256") and sha256_text(_canonical_json(permissions)) != config["taskPermissionsSha256"]:
            raise ValueError("TASK_PERMISSIONS_DRIFT")
        if not _within(workspace, allowed_root) \
                or not any(_within(workspace, root) for root in policy["allowedRoots"]):
            raise ValueError("EXECUTION_BINDING_MISMATCH")
    finally:
        store.close()


def execute(directory):
    private_directory(directory)
    fd = os.open(directory / "runner.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = json.loads((directory / "config.json").read_text())
        # 去重优先：已有 started 标记时先抛 FileExistsError，且在写任何文件之前抛出，
        # 保证重复 execute（含 policy 漂移 / 缺失）绝不覆写旧 state / result。
        marker_path = directory / "started"
        if marker_path.exists() or marker_path.is_symlink():
            raise FileExistsError("本轮已执行，禁止重发：%s" % marker_path)
        # 模型 policy / 绑定门禁：在任何标记 / 宿主调用之前，漂移、撤销或绑定漂移即拒绝。
        # 拒绝路径持久化 attention_required 并直接 return 1（cox 裁定），绝不创建
        # started 标记、不调用 submit / import、不补 accepted。只包住策略核验，
        # 不吞其他异常而破坏 started 的 FileExistsError 去重。
        try:
            _verify_policy_for_execution(config)
        except Exception as exc:
            write_json(directory / "state.json", {"status": "attention_required", "at": now_iso(),
                                                  "errorType": type(exc).__name__, "message": str(exc)})
            return 1
        # Restarting this entry never replays the external call, even after a crash.
        marker = os.open(marker_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(marker)
        state = {"status": "running", "pid": os.getpid(), "at": now_iso()}
        write_json(directory / "state.json", state)
        try:
            if workbuddy_binding(config.get("agentId", WORKBUDDY_AGENT_ID)) != config["binding"]:
                raise ValueError("专家绑定已变化，未提交")
            argv = ["--db", config["db"], "--allowed-root", config["allowedRoot"],
                    "--source", WORKBUDDY_SOURCE_ID, "--agent", config.get("agentId", WORKBUDDY_AGENT_ID),
                    "attempt", "submit", "--task", config["taskId"],
                    "--attempt", config["attemptId"], "--workbuddy-bin", config["binary"],
                    "--workbuddy-home", config["home"], "--workbuddy-model", config["model"],
                    "--workbuddy-timeout-ms", str(config["timeoutMs"])]
            code, submission = d4ctl.run(argv)
            write_json(directory / "result.json", {"exitCode": code, "submission": submission})
            collection = {"status": "skipped", "reason": "submission_failed"}
            if code == 0:
                deadline = time.monotonic() + config["receiptWaitSeconds"]
                while True:
                    collection = collect_attempt(config)
                    write_json(directory / "state.json", {**state, "status": "collecting",
                                                          "collection": collection, "at": now_iso()})
                    if (collection["status"] != "waiting"
                            and collection.get("taskStatus") not in ("running", "submitted")):
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(1)
            completed = code == 0 and collection.get("taskStatus") in ("awaiting_review", "approved")
            write_json(directory / "state.json", {
                "status": "finished" if completed else "attention_required",
                "pid": os.getpid(), "at": now_iso(), "exitCode": code, "collection": collection,
                "message": "报告已交付，等待审核" if completed else "需要检查执行结果或回执；未自动重发"})
            return 0 if completed else 1
        except Exception as exc:
            write_json(directory / "state.json", {"status": "attention_required", "at": now_iso(),
                                                  "errorType": type(exc).__name__, "message": str(exc)})
            return 1
    finally:
        os.close(fd)


def start(args):
    model = getattr(args, "model", DEFAULT_WORKBUDDY_MODEL)
    if model not in ALLOWED_WORKBUDDY_MODELS:
        raise ValueError("WORKBUDDY_MODEL_NOT_ALLOWED")
    for path in (args.db, args.allowed_root, args.home, args.binary, args.runtime_dir, args.workflow):
        if not path.is_absolute():
            raise ValueError("所有路径必须为绝对路径")
    # 数据库必须是无符号链接的绝对真实路径（与 args.db 精确一致，不做解析漂移）。
    _real_absolute(args.db, "DB_PATH_INVALID")
    # allowed-root 必须是无符号链接的绝对真实路径：直接 symlink 或含 symlink 祖先的
    # 路径一律以专用错误码拒绝，且先于建 runtime 目录 / create_attempt / Popen，
    # 以免借链接把执行根扩到 args.allowed-root 之外；真实正常根仍可启动。
    _real_absolute(args.allowed_root, "ALLOWED_ROOT_INVALID")
    # 受信 workflow 与 modelPolicy 门禁：没有 policy、未知模型、未来授权、
    # fallback 未禁用、绑定不一致都在建轮次与宿主调用之前拒绝。
    workflow_path, workflow = load_workflow_file(args.workflow)
    # workflow 顶层 db / agent / source / task 绑定先于任何建轮次动作校验；
    # 缺失或错配一律拒绝，绝不使用 modelPolicy 内字段替代 workflow 字段。
    identity_store = SqliteStore(args.db, read_only=True)
    try:
        identity_task = identity_store.get_task(args.task)
        if not identity_task or identity_task["agent_id"] not in WORKBUDDY_AGENT_IDS:
            raise ValueError("WORKBUDDY_MEMBER_NOT_BOUND")
        agent_id = identity_task["agent_id"]
    finally:
        identity_store.close()
    validate_workflow_binding(workflow, args.db, args.task, agent_id)
    # workflow.attemptId 只有 null 或缺失可启动；空串、False 及任何其他类型都必须在
    # 创建新轮次之前拒绝，绝不先 create 再留下孤儿 attempt。
    if workflow.get("attemptId") is not None:
        raise ValueError("WORKFLOW_ATTEMPT_BINDING_MISMATCH")
    policy = validate_model_policy(workflow.get("modelPolicy"), model, agent_id=agent_id)
    if not args.db.is_file() or not args.binary.is_file() or not os.access(args.binary, os.X_OK):
        raise ValueError("数据库和 CLI 必须已存在，CLI 必须可执行")
    if not args.home.is_dir() or not args.allowed_root.is_dir():
        raise ValueError("HOME 和 allowed-root 必须是已存在的目录")
    if args.timeout_ms <= 0 or not 0 <= args.receipt_wait_seconds <= 300:
        raise ValueError("timeout 必须为正数，回执等待范围 0..300 秒")
    binding = workbuddy_binding(agent_id)
    store = SqliteStore(args.db, read_only=True)
    try:
        task = store.get_task(args.task)
        if not task or task["agent_id"] != agent_id or task["source_id"] != WORKBUDDY_SOURCE_ID:
            raise ValueError("任务必须登记为受控 WorkBuddy dev/QA 成员 / workbuddy-codebuddy")
        if store.list_attempts(args.task):
            raise ValueError("任务已有轮次；请核查既有结果，不自动创建下一轮")
        # task workspace 必须同时落在 args.allowed-root 与 policy.allowedRoots 之下；
        # 允许的 root 不得扩大 args.allowed-root 的执行权限。
        workspace = task["workspace"]
        permissions = json.loads(task["permissions_json"])
        build_policy(agent_id, workspace, permissions, allowed_roots=[str(args.allowed_root)])
        if not _within(workspace, args.allowed_root):
            raise ValueError("TASK_WORKSPACE_OUTSIDE_ALLOWED_ROOT")
        if not any(_within(workspace, root) for root in policy["allowedRoots"]):
            raise ValueError("TASK_WORKSPACE_OUTSIDE_ALLOWED_ROOTS")
    finally:
        store.close()
    args.runtime_dir.mkdir(mode=0o700)
    private_directory(args.runtime_dir)
    store = SqliteStore(args.db)
    try:
        attempt = TaskService(store, allowed_roots=[str(args.allowed_root)]).create_attempt(
            args.task, context=worker_context(agent_id, entry="d4_workbuddy_run start"))
    finally:
        store.close()
    config = {"agentId": agent_id, "db": str(args.db), "allowedRoot": str(args.allowed_root), "taskId": args.task,
              "attemptId": attempt["attemptId"], "home": str(args.home), "binary": str(args.binary),
              "model": model, "binding": binding, "timeoutMs": args.timeout_ms,
              "taskPermissionsSha256": sha256_text(_canonical_json(permissions)),
              "receiptWaitSeconds": args.receipt_wait_seconds,
              "workflowPath": str(workflow_path), "modelPolicy": policy,
              "modelPolicySha256": sha256_text(_canonical_json(policy))}
    write_json(args.runtime_dir / "config.json", config)
    write_json(args.runtime_dir / "state.json", {"status": "launching", "at": now_iso()})
    lock = os.open(args.runtime_dir / "runner.lock", os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
    os.close(lock)
    log = os.open(args.runtime_dir / "runner.log", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        child = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "_execute",
                                  "--runtime-dir", str(args.runtime_dir)], stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=log, start_new_session=True, close_fds=True)
    except OSError:
        write_json(args.runtime_dir / "state.json", {"status": "attention_required", "at": now_iso(),
                                                    "message": "runner 启动失败，宿主未调用"})
        raise
    finally:
        os.close(log)
    return {"runnerPid": child.pid, "runtimeDir": str(args.runtime_dir), **config}


def main(argv=None):
    parser = argparse.ArgumentParser(description="D4 WorkBuddy 单轮启动与持久状态查询")
    sub = parser.add_subparsers(dest="cmd", required=True)
    launch = sub.add_parser("start")
    for name in ("db", "allowed-root", "home", "binary", "runtime-dir", "workflow"):
        launch.add_argument("--" + name, type=Path, required=True)
    launch.add_argument("--task", required=True)
    launch.add_argument("--model", default=DEFAULT_WORKBUDDY_MODEL,
                        choices=ALLOWED_WORKBUDDY_MODELS)
    launch.add_argument("--timeout-ms", type=int, default=600000)
    launch.add_argument("--receipt-wait-seconds", type=int, default=30)
    for name in ("status", "collect", "_execute"):
        sub.add_parser(name).add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "_execute":
            return execute(args.runtime_dir)
        if args.cmd == "start":
            result = start(args)
        elif args.cmd == "collect":
            result = recover_collection(args.runtime_dir)
        else:
            result = inspect_run(args.runtime_dir)
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
