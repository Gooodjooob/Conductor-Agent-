"""D4 无模型 heartbeat controller（任务卡 D4-AUTOMATION-GUARD-IMPLEMENTATION-01）。

边界（automation-cost-control.md）：
- collector 只读：受信 workflow 文件、runner config/state/result、host runtime、
  SQLite（read_only=True）与本轮回执普通文件摘要；不写 workflow/DB/runtime/receipt。
- host runtime 身份/模型回显与 workflow 当前 modelPolicy 只做证据校验：sessionId/workspace/
  requestedModel/observed 字段与冻结 config 精确一致才采纳，错绑定/漂移/撤销/未来时间判
  结构性异常先 RECONCILE。复用 runner.validate_model_policy（只读导入），不修改 runner。
- watch 目录只写自身目录（state/directive 缓存/锁文件）；fcntl 锁 + 原子保存。
- 官方暂停动作由 cox 执行；本模块只输出 PAUSE intent（platformAction），
  不调用官方 automation 工具，不伪称平台已验证。
- evaluate_tick / validate_resume 为纯函数；重复 tick 回放缓存 directive，
  重复通知不发生；重启保持计数（计数持久化在 watch state）。
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.file_receipts import receipt_directory
from storage.sqlite_store import SqliteStore

SNAPSHOT_SCHEMA = "d4-heartbeat-snapshot.v1"
DIRECTIVE_SCHEMA = "d4-heartbeat-directive.v1"
WATCH_STATE_SCHEMA = "d4-heartbeat-watch-state.v1"
WORKFLOW_SCHEMA = "d4-workflow.v1"

# 对账类错误：先 RECONCILE，同 fingerprint 对账确认后才允许 PAUSE。
RECONCILE_ERROR_CODES = frozenset({
    "WORKFLOW_IDENTITY_MISSING", "WORKFLOW_IDENTITY_CONFLICT",
    "WORKFLOW_INVALID", "WORKFLOW_BINDING_INVALID",
    "DB_TASK_MISSING", "DB_ATTEMPT_MISSING",
    "DB_RELATIONSHIP_CONFLICT", "DB_MISMATCH", "BINDING_NOT_ALLOWED",
    "RUNNER_EVIDENCE_UNREADABLE",
    "RUNNER_PATH_CONFLICT", "EXECUTION_UNKNOWN", "ORPHAN_SUBMISSION",
    "MODEL_MISMATCH", "MODEL_POLICY_NOT_ALLOWED",
    "MODEL_POLICY_MISSING", "MODEL_POLICY_DRIFT",
    "HOST_RUNTIME_BINDING_INVALID", "HOST_RUNTIME_EVIDENCE_MISSING",
    "QUOTA_EXHAUSTED",
})
# 结构性身份/DB冲突：其他判定（REVIEW/NEXT）在解决前不可信，必须最先 RECONCILE。
STRUCTURAL_IDENTITY_CODES = frozenset({
    "WORKFLOW_IDENTITY_MISSING", "WORKFLOW_IDENTITY_CONFLICT", "WORKFLOW_INVALID",
    "DB_TASK_MISSING", "DB_ATTEMPT_MISSING", "DB_RELATIONSHIP_CONFLICT",
})
# 宿主身份/模型证据与模型授权（acceptance[3][5]）：错误绑定/漂移/撤销/未来时间属结构性
# 异常，必须先于 REVIEW/NEXT 对账；否则会把非当前轮的模型回显、终态或已撤销授权当有效证据。
STRUCTURAL_EVIDENCE_CODES = frozenset({
    "MODEL_MISMATCH", "MODEL_POLICY_MISSING", "MODEL_POLICY_DRIFT",
    "MODEL_POLICY_NOT_ALLOWED",
    "HOST_RUNTIME_BINDING_INVALID", "HOST_RUNTIME_EVIDENCE_MISSING",
})
# collector 已识别的结构身份/DB/runner 证据异常（R11）：这些码此前已在 RECONCILE_ERROR_CODES
# 但不在 STRUCTURAL_BLOCKING_CODES，导致存在 new_unreviewed/approved delivery 时仍先走
# REVIEW/NEXT。它们与身份/DB 冲突同级：解决前 REVIEW/NEXT 不可信，必须最先 RECONCILE。
# 不改变 collector 读取/选择 anomaly 的方式，也不把异常清空迎合 fixture；scope 仅限这 5 类
# 结构冲突，非结构对账码（EXECUTION_UNKNOWN / ORPHAN_SUBMISSION / QUOTA_EXHAUSTED）与
# 合法 delivery 的 REVIEW/NEXT 优先语义保持不变。
STRUCTURAL_COLLECTOR_CODES = frozenset({
    "WORKFLOW_BINDING_INVALID", "DB_MISMATCH", "BINDING_NOT_ALLOWED",
    "RUNNER_PATH_CONFLICT", "RUNNER_EVIDENCE_UNREADABLE",
})
# 阻断性结构异常全集：解决前其他判定（REVIEW/NEXT）不可信，必须最先 RECONCILE。
STRUCTURAL_BLOCKING_CODES = (STRUCTURAL_IDENTITY_CODES | STRUCTURAL_EVIDENCE_CODES
                             | STRUCTURAL_COLLECTOR_CODES)
QUOTA_ERROR_CODES = frozenset({
    "QUOTA_EXHAUSTED", "INSUFFICIENT_QUOTA", "QUOTA_EXCEEDED",
    "CREDIT_EXHAUSTED", "RATE_LIMIT_QUOTA",
})
NO_PROGRESS_CODE = "NO_PROGRESS_AFTER_DEADLINE"

# 同一确定错误：首 tick 落基线（count=1，不计失败），后连续 2 个独立 tick 达到 PAUSE。
ERROR_PAUSE_THRESHOLD = 3
# deadline 后无有效进展：首 tick 落基线，后连续 3 个独立 tick 达到 PAUSE。
DEADLINE_PAUSE_THRESHOLD = 4

# 对账证据结构（冻结）：外部 cox 写真实证据，本模块只校验结构，不生成事实。
RECONCILIATION_EVIDENCE_KEYS = ("taskId", "attemptId", "fingerprint",
                                "processChecked", "sourceChecked", "receiptsChecked")
# 官方暂停回执：既有三字段规范回执（冻结键，owner 由回执自身 strict 校验）；
# 原始官方 automation_update 工具回执另行结构校验（见 _extract_official_tool_payload）。
OFFICIAL_PAUSE_RECEIPT_KEYS = ("automationId", "ownerThreadId", "status")
# durable 记录必须包含的冻结键（子集）；真实完整 automation.toml 允许 version/name/prompt/
# rrule/created_at/updated_at 等额外标量字段。字段存在 / 时间戳更新都不等于已暂停：
# 必须 exact id == automationId、kind == heartbeat、target_thread_id == owner、status == PAUSED。
DURABLE_PAUSE_KEYS = ("id", "kind", "target_thread_id", "status")
RESUME_AUTHORIZATION_KEYS = frozenset({
    "ownerThreadId", "automationId", "userExplicitContinue",
    "maxRuns", "intervalMinutes", "reconciliationComplete",
})


# ---------------------------------------------------------------------------
# 路径与通用工具
# ---------------------------------------------------------------------------

def require_real_absolute(path, label):
    """绝对、无符号链接（含祖先）、resolve 后等于自身的路径；否则拒绝。"""
    if isinstance(path, Path):
        candidate = path
    else:
        candidate = Path(str(path))
    if not candidate.is_absolute() or candidate.is_symlink() or candidate.resolve() != candidate:
        raise ValueError("%s 必须是无符号链接的绝对真实路径" % label)
    return candidate


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _path_has_symlink_component(path):
    """末级或任一祖先为符号链接则 True（逐段 lstat，不跟随）。

    即使 symlink 解析目标仍位于允许根内，只要路径中存在 symlink 段就判定为真，
    保证所有证据/写目标都拒绝软链接（acceptance[3][4]）。
    """
    parts = Path(path).parts
    if not parts:
        return False
    current = Path(parts[0])
    if current.is_symlink():
        return True
    for part in parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _require_no_symlink_components(path, label):
    """写目标（含祖先）不得为符号链接；违规抛受控异常，不静默继续。"""
    if _path_has_symlink_component(path):
        raise ValueError("SYMLINK_REFUSED:%s" % (Path(path).name or label))


def _read_regular_file(path, root=None):
    """普通文件安全读取；拒绝末级与任一祖先符号链接，并拒绝越出 root。"""
    p = Path(path)
    if _path_has_symlink_component(p) or not p.is_file():
        return None
    resolved = p.resolve()
    if root is not None:
        root = Path(root).resolve()
        if resolved != root and root not in resolved.parents:
            return None
    return p.read_bytes()


def _try_real_dir(raw, label):
    """受信绝对真实路径解析；非字符串/空/敌意路径返回 None（不抛错）。"""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return require_real_absolute(raw, label)
    except ValueError:
        return None


def resolve_runner_dir(workflow):
    """runner 目录解析（当前指针接线）。

    优先受信 currentRunner，兼容仅 runtimeDir；两者都提供且不一致 → (None, True)，
    交由 RECONCILE 处理，不猜最近任务。返回 (Path|None, conflict: bool)。
    """
    raw_current = workflow.get("currentRunner")
    raw_runtime = workflow.get("runtimeDir")
    has_current = isinstance(raw_current, str) and bool(raw_current)
    has_runtime = isinstance(raw_runtime, str) and bool(raw_runtime)
    current = _try_real_dir(raw_current, "currentRunner") if has_current else None
    runtime = _try_real_dir(raw_runtime, "runtimeDir") if has_runtime else None
    if has_current and has_runtime:
        if current is None or runtime is None or current != runtime:
            return None, True
        return current, False
    if has_current:
        return current, False
    if has_runtime:
        return runtime, False
    return None, False


def _write_json_atomic(path, value):
    """0600 临时文件 + fsync + os.replace 原子保存；目标/临时文件及祖先拒绝符号链接。"""
    path = Path(path)
    _require_no_symlink_components(path, "write-target")
    temporary = path.with_suffix(".tmp")
    _require_no_symlink_components(temporary, "write-temp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    os.replace(temporary, path)


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


# ---------------------------------------------------------------------------
# workflow 文件与身份
# ---------------------------------------------------------------------------

def load_workflow(workflow_path):
    """读取受信 workflow 文件；路径敌意或内容损坏直接拒绝（fail closed）。"""
    p = require_real_absolute(workflow_path, "workflow")
    raw = _read_regular_file(p)
    if not p.is_file() or raw is None:
        raise ValueError("workflow 必须是已存在的普通文件")
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise ValueError("WORKFLOW_JSON_INVALID:%s" % exc) from exc
    if not isinstance(data, dict):
        raise ValueError("workflow 必须是 JSON 对象")
    if data.get("schemaVersion") != WORKFLOW_SCHEMA:
        raise ValueError("WORKFLOW_SCHEMA_UNSUPPORTED")
    return p, data


def _fingerprint(code, task_id, attempt_id):
    return "%s:%s/%s" % (code, task_id or "-", attempt_id or "-")


def _fingerprint_identity(fingerprint):
    """从 fingerprint（"CODE:task/attempt"）解析 task/attempt；不可解析返回 (None, None)。"""
    if not isinstance(fingerprint, str) or ":" not in fingerprint:
        return None, None
    _, _, rest = fingerprint.partition(":")
    task, sep, attempt = rest.partition("/")
    if not sep:
        return None, None
    return task, attempt


def _anomaly(code, task_id, attempt_id, **detail):
    return {"code": code, "fingerprint": _fingerprint(code, task_id, attempt_id), "detail": detail}


# ---------------------------------------------------------------------------
# collector：只读收集证据快照
# ---------------------------------------------------------------------------

def _read_runner_evidence(runtime_dir):
    """只读 runner config/state/result 与 runner.lock 活性；不写任何文件。"""
    config_path = runtime_dir / "config.json"
    state_path = runtime_dir / "state.json"
    config_raw = _read_regular_file(config_path, root=runtime_dir)
    state_raw = _read_regular_file(state_path, root=runtime_dir)
    if config_raw is None or state_raw is None:
        return None
    try:
        config = json.loads(config_raw.decode("utf-8"))
        state = json.loads(state_raw.decode("utf-8"))
    except (ValueError, OSError):
        return None
    if not isinstance(config, dict) or not isinstance(state, dict):
        return None
    alive = False
    lock_path = runtime_dir / "runner.lock"
    if lock_path.is_file() and not lock_path.is_symlink():
        try:
            fd = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(fd, fcntl.LOCK_UN)
                    alive = False
                except BlockingIOError:
                    alive = True
            finally:
                os.close(fd)
        except OSError:
            alive = False
    result = {"config": config, "state": state, "runnerAlive": alive}
    result_raw = _read_regular_file(runtime_dir / "result.json", root=runtime_dir)
    if result_raw is not None:
        try:
            result["result"] = json.loads(result_raw.decode("utf-8"))
        except ValueError:
            result["result"] = None
    started = runtime_dir / "started"
    if started.is_file() and not started.is_symlink():
        result["startedAtEpoch"] = os.stat(started).st_mtime
    return result


def _safe_attempt_component(attempt_id):
    return (isinstance(attempt_id, str) and attempt_id
            and "/" not in attempt_id and attempt_id not in (".", ".."))


def _load_policy_validator():
    """只读复用 runner 现有模型策略验证函数（不修改 runner）。

    导入失败或缺少目标函数时返回 None，调用方按 fail-closed 处理（禁止静默跳过授权校验）。
    """
    try:
        from scripts import d4_workbuddy_run as runner
    except Exception:
        return None
    if not hasattr(runner, "validate_model_policy"):
        return None
    return runner


def _validate_model_policy_evidence(workflow, config, now):
    """校验 workflow 当前 modelPolicy 与冻结 config 一致且通过运行时授权门禁。

    返回 anomaly dict 或 None。缺失、撤销、漂移、非法格式或未来时间均为结构性 RECONCILE，
    优先于 REVIEW/NEXT（acceptance[5]）。复用 runner.validate_model_policy（只读导入，
    不修改 runner），覆盖 schema / 生效与授权时间 / allowedModels 含 config.model /
    primary / agent / source / root 权限 / transparentFallback=false。
    """
    task_id = workflow.get("taskId")
    attempt_id = workflow.get("attemptId")
    frozen_policy = config.get("modelPolicy")
    frozen_sha = config.get("modelPolicySha256")
    if not isinstance(frozen_policy, dict) or not isinstance(frozen_sha, str) or not frozen_sha:
        return _anomaly("MODEL_POLICY_MISSING", task_id, attempt_id, field="config.modelPolicy")
    current = workflow.get("modelPolicy")
    if not isinstance(current, dict):
        return _anomaly("MODEL_POLICY_MISSING", task_id, attempt_id, field="workflow.modelPolicy")
    current_sha = sha256_bytes(canonical_json(current).encode("utf-8"))
    # 冻结 SHA 与冻结 policy 内容都必须与 workflow 当前 policy 精确一致，否则判定漂移。
    if current_sha != frozen_sha or current != frozen_policy:
        return _anomaly("MODEL_POLICY_DRIFT", task_id, attempt_id,
                        frozenSha256=frozen_sha, currentSha256=current_sha)
    validator = _load_policy_validator()
    if validator is None:
        return _anomaly("MODEL_POLICY_NOT_ALLOWED", task_id, attempt_id,
                        reason="policy_validator_unavailable")
    requested_model = config.get("model")
    agent_id = config.get("agentId") or getattr(validator, "WORKBUDDY_AGENT_ID", None)
    try:
        validator.validate_model_policy(current, requested_model, now=now, agent_id=agent_id)
    except Exception as exc:
        return _anomaly("MODEL_POLICY_NOT_ALLOWED", task_id, attempt_id,
                        reason=type(exc).__name__, message=str(exc))
    return None


def _resolve_host_home(config, task_id, attempt_id):
    """解析冻结 config.home：必须是无符号链接的真实绝对路径（acceptance[3]）。

    缺失 / 空 / 非字符串 / 相对路径 / 含 .. / 直接或祖先 symlink 一律返回专用结构性
    anomaly（HOST_RUNTIME_BINDING_INVALID），禁止从未授权路径读取 runtime；合法则返回
    (Path, None)。既不 resolve 放宽权限，也不因 accepted/delivery 缺证据而跳过校验。
    """
    home = config.get("home")
    if not isinstance(home, str) or not home:
        return None, _anomaly("HOST_RUNTIME_BINDING_INVALID", task_id, attempt_id,
                              reason="home_missing")
    if _path_has_symlink_component(home):
        return None, _anomaly("HOST_RUNTIME_BINDING_INVALID", task_id, attempt_id,
                              reason="home_symlink", observed=home)
    try:
        home_path = require_real_absolute(home, "config.home")
    except ValueError:
        return None, _anomaly("HOST_RUNTIME_BINDING_INVALID", task_id, attempt_id,
                              reason="home_invalid", observed=home)
    return home_path, None


def _resolve_authorized_root(raw, label):
    """授权根解析：真实无符号链接绝对路径；否则 None（不静默 resolve 放宽权限）。"""
    if not isinstance(raw, str) or not raw:
        return None
    if _path_has_symlink_component(raw):
        return None
    try:
        return require_real_absolute(raw, label)
    except ValueError:
        return None


def _path_contains(root, child):
    """child 是否等于 root 或位于 root 之下（acceptance[4]）。

    按路径组件逐段比较，拒绝字符串前缀误判（如 /a/b 与 /a/bc 不是包含关系）。
    """
    root_parts = Path(root).parts
    child_parts = Path(child).parts
    if not root_parts or len(root_parts) > len(child_parts):
        return False
    return tuple(child_parts[:len(root_parts)]) == tuple(root_parts)


def _validate_workspace_authorization(config, task, task_id, attempt_id):
    """read_only DB task.workspace 必须被冻结 config.allowedRoot 与至少一个
    modelPolicy.allowedRoots 授权（acceptance[4]）。

    根无效 / 缺失 / 不覆盖 workspace → 专用结构性 RECONCILE（优先 REVIEW/NEXT）。
    task.workspace 与各根都必须是真实无符号链接绝对路径；按组件包含而非字符串前缀；
    不 resolve 悄悄扩大权限；不要求 home 在 allowedRoot 下。
    """
    def bad(reason, **detail):
        return _anomaly("HOST_RUNTIME_BINDING_INVALID", task_id, attempt_id,
                        reason=reason, **detail)

    workspace_raw = task.get("workspace") if isinstance(task, dict) else None
    workspace = None
    if (isinstance(workspace_raw, str) and workspace_raw
            and not _path_has_symlink_component(workspace_raw)):
        try:
            workspace = require_real_absolute(workspace_raw, "task.workspace")
        except ValueError:
            workspace = None
    if workspace is None:
        return bad("workspace_invalid", observed=workspace_raw)

    allowed_root = _resolve_authorized_root(config.get("allowedRoot"), "config.allowedRoot")
    if allowed_root is None or not _path_contains(allowed_root, workspace):
        return bad("allowed_root_not_authorized",
                   allowedRoot=config.get("allowedRoot"), workspace=str(workspace))

    policy = config.get("modelPolicy")
    if not isinstance(policy, dict):
        # 冻结 policy 缺失 / 漂移由 _validate_model_policy_evidence 负责，避免重复判定。
        return None
    roots = policy.get("allowedRoots")
    if not isinstance(roots, list) or not roots:
        return bad("model_policy_roots_missing", workspace=str(workspace))
    covered = False
    for root in roots:
        resolved = _resolve_authorized_root(root, "modelPolicy.allowedRoots")
        if resolved is not None and _path_contains(resolved, workspace):
            covered = True
            break
    if not covered:
        return bad("model_policy_roots_not_authorized",
                   allowedRoots=roots, workspace=str(workspace))
    return None


def _validate_host_runtime(config, workflow, task, accepted, has_delivery):
    """host runtime 只读校验；返回 (observedModel, hostState, anomaly)。

    冻结 config.home 必须先为非空、无符号链接的真实绝对路径：缺失 / 空 / 非字符串 /
    相对路径 / 含 .. / 直接或祖先 symlink 判 HOST_RUNTIME_BINDING_INVALID（结构性 RECONCILE，
    优先 REVIEW/NEXT），禁止从未授权路径读取 runtime，也不借 accepted/delivery 缺证据跳过。
    仅当 runtime 为 dict 且 canonical sessionId 与当前 attempt、workspace 为真实绝对路径且
    等于 read_only DB 的 task.workspace、requestedModel 与冻结 config.model 均精确一致时才采纳
    observedModel；observedSessionId / observedWorkspace / observedModel 若提供也必须一致，
    禁止 custom / fallback 别名。字段错类型或错绑定判 HOST_RUNTIME_BINDING_INVALID；observedModel
    不符判 MODEL_MISMATCH（均为结构性 RECONCILE）。合法真实 home 但未 accepted / 无合法 delivery
    且 runtime 尚不存在时不伪造 observed 字段（返回全空、无异常，保持初始化 WAIT）；已 accepted
    或存在合法 delivery 时 runtime 缺失 / 损坏判 HOST_RUNTIME_EVIDENCE_MISSING。
    """
    task_id = workflow.get("taskId")
    attempt_id = workflow.get("attemptId")
    frozen_model = config.get("model")
    if not isinstance(task, dict):
        return None, None, None
    # 冻结 config.home 门禁：先于任何 runtime 读取，非法即结构性 RECONCILE。
    home, home_anomaly = _resolve_host_home(config, task_id, attempt_id)
    if home_anomaly is not None:
        return None, None, home_anomaly
    # attemptId 必须是可安全拼接的单一组件；否则拒绝拼接 runtime 路径（fail closed）。
    if not _safe_attempt_component(attempt_id):
        return None, None, _anomaly("HOST_RUNTIME_BINDING_INVALID", task_id, attempt_id,
                                    reason="attemptId_unsafe", observed=attempt_id)
    runtime_json = home / "d4-workbuddy-store" / "attempts" / attempt_id / "runtime.json"
    runtime_raw = _read_regular_file(runtime_json, root=home)
    required = bool(accepted) or bool(has_delivery)
    if runtime_raw is None:
        if required:
            return None, None, _anomaly("HOST_RUNTIME_EVIDENCE_MISSING", task_id, attempt_id,
                                        reason="missing")
        return None, None, None
    try:
        host_runtime = json.loads(runtime_raw.decode("utf-8"))
    except (ValueError, OSError):
        host_runtime = None
    if not isinstance(host_runtime, dict):
        if required:
            return None, None, _anomaly("HOST_RUNTIME_EVIDENCE_MISSING", task_id, attempt_id,
                                        reason="damaged")
        return None, None, None

    def bad(reason, **detail):
        return _anomaly("HOST_RUNTIME_BINDING_INVALID", task_id, attempt_id,
                        reason=reason, **detail)

    try:
        task_workspace = require_real_absolute(task.get("workspace"), "task.workspace")
    except ValueError:
        task_workspace = None
    # canonical sessionId 必须与当前 attempt 精确一致：不属于当前轮的 runtime（含其模型回显 /
    # 终态）一律不采纳。
    session = host_runtime.get("sessionId")
    if not isinstance(session, str) or not session or session != attempt_id:
        return None, None, bad("sessionId", observed=host_runtime.get("sessionId"))
    # workspace 必须是无符号链接的真实绝对路径，且等于 read_only DB 的 task.workspace。
    workspace_raw = host_runtime.get("workspace")
    if task_workspace is None or not isinstance(workspace_raw, str) or not workspace_raw:
        return None, None, bad("workspace", observed=workspace_raw)
    try:
        workspace = require_real_absolute(workspace_raw, "host.workspace")
    except ValueError:
        return None, None, bad("workspace_path", observed=workspace_raw)
    if workspace != task_workspace:
        return None, None, bad("workspace_binding", observed=workspace_raw,
                               expected=str(task_workspace))
    # observedSessionId / observedWorkspace 若提供（非 None）也必须一致。
    if host_runtime.get("observedSessionId") is not None:
        observed_session = host_runtime.get("observedSessionId")
        if not isinstance(observed_session, str) or observed_session != attempt_id:
            return None, None, bad("observedSessionId", observed=observed_session)
    if host_runtime.get("observedWorkspace") is not None:
        observed_workspace = host_runtime.get("observedWorkspace")
        try:
            observed_workspace_path = require_real_absolute(observed_workspace,
                                                            "host.observedWorkspace")
        except (ValueError, TypeError):
            return None, None, bad("observedWorkspace_path", observed=observed_workspace)
        if observed_workspace_path != task_workspace:
            return None, None, bad("observedWorkspace_binding", observed=observed_workspace)
    # requestedModel 必须等于冻结 config.model，禁止 custom / fallback 别名。
    requested = host_runtime.get("requestedModel")
    if not isinstance(requested, str) or not requested or requested != frozen_model:
        return None, None, bad("requestedModel", observed=requested, expected=frozen_model)
    # observedModel 若提供必须与 requestedModel 精确一致（不做别名 / fallback 归一）。
    observed_model = host_runtime.get("observedModel")
    if observed_model is not None:
        if not isinstance(observed_model, str) or not observed_model:
            return None, None, bad("observedModel_type", observed=observed_model)
        if observed_model != requested:
            return None, None, _anomaly("MODEL_MISMATCH", task_id, attempt_id,
                                        requested=requested, observed=observed_model)
    return observed_model, host_runtime.get("state"), None


def collect_snapshot(workflow_path, automation_id, owner_thread_id, now=None):
    """只读收集快照；身份缺失/冲突等业务异常以 anomaly 返回（RECONCILE），不抛错。

    now 为 modelPolicy 生效 / 授权时间的比较基准（timezone-aware）；缺省取当前 UTC 时间。
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now 必须是 timezone-aware datetime")
    for value, label in ((automation_id, "automation_id"), (owner_thread_id, "owner_thread_id")):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s 必须是非空字符串" % label)
    p, workflow = load_workflow(workflow_path)
    snapshot = {
        "schemaVersion": SNAPSHOT_SCHEMA,
        "workflowPath": str(p),
        "automationId": automation_id,
        "ownerThreadId": owner_thread_id,
        "taskId": workflow.get("taskId"),
        "attemptId": workflow.get("attemptId"),
        "taskStatus": None, "submitState": None,
        "runnerAlive": False, "runnerStatus": None,
        "deadlineAt": None, "requestedModel": None, "observedModel": None,
        "hostRuntimeState": None,
        "eventCount": 0, "deliveryId": None, "reviewId": None,
        "reviewDecision": None, "deliveryState": "none",
        "attemptAcceptedAt": None,
        "errorCode": None, "anomaly": None,
        "receiptsPath": None, "receiptDigest": None,
        "modelPolicy": None, "modelPolicySha256": None,
        "reconciliationRequired": False,
        "progressSignature": None,
    }
    policy = workflow.get("modelPolicy")
    if isinstance(policy, dict):
        snapshot["modelPolicy"] = policy
        snapshot["modelPolicySha256"] = sha256_bytes(canonical_json(policy).encode("utf-8"))

    # -- 身份：exact automationId / ownerThreadId，缺失或冲突 → RECONCILE 类异常 --
    identity_anomaly = None
    for field, expected, given in (("automationId", automation_id, workflow.get("automationId")),
                                   ("ownerThreadId", owner_thread_id, workflow.get("ownerThreadId"))):
        if given is None:
            identity_anomaly = _anomaly("WORKFLOW_IDENTITY_MISSING", snapshot["taskId"],
                                        snapshot["attemptId"], field=field)
            break
        if given != expected:
            identity_anomaly = _anomaly("WORKFLOW_IDENTITY_CONFLICT", snapshot["taskId"],
                                        snapshot["attemptId"], field=field,
                                        expected=expected, observed=given)
            break
    for field in ("taskId", "attemptId"):
        if identity_anomaly:
            break
        value = workflow.get(field)
        if not isinstance(value, str) or not value.strip():
            identity_anomaly = _anomaly("WORKFLOW_IDENTITY_MISSING", snapshot["taskId"],
                                        snapshot["attemptId"], field=field)
    if identity_anomaly:
        snapshot["errorCode"] = identity_anomaly["code"]
        snapshot["anomaly"] = identity_anomaly
        snapshot["reconciliationRequired"] = True
        return snapshot

    anomalies = []

    # -- workflow 顶层绑定字段（db/allowedAgents/allowedSource）：缺字段/错误类型 → 受控 RECONCILE。
    #    这些字段是权威绑定来源；modelPolicy 成员集不替代顶层。
    db_field = workflow.get("db")
    if not (isinstance(db_field, str) and db_field.strip()):
        anomalies.append(_anomaly("WORKFLOW_BINDING_INVALID", snapshot["taskId"],
                                  snapshot["attemptId"], field="db"))
    agents_field = workflow.get("allowedAgents")
    if not (isinstance(agents_field, list) and agents_field
            and all(isinstance(item, str) and item for item in agents_field)):
        anomalies.append(_anomaly("WORKFLOW_BINDING_INVALID", snapshot["taskId"],
                                  snapshot["attemptId"], field="allowedAgents"))
    source_field = workflow.get("allowedSource")
    if not (isinstance(source_field, str) and source_field.strip()):
        anomalies.append(_anomaly("WORKFLOW_BINDING_INVALID", snapshot["taskId"],
                                  snapshot["attemptId"], field="allowedSource"))

    # -- runner 证据（只读）：优先受信 currentRunner，兼容仅 runtimeDir --
    runtime_dir, runner_conflict = resolve_runner_dir(workflow)
    runner = None
    if runner_conflict:
        anomalies.append(_anomaly("RUNNER_PATH_CONFLICT", snapshot["taskId"],
                                  snapshot["attemptId"],
                                  currentRunner=workflow.get("currentRunner"),
                                  runtimeDir=workflow.get("runtimeDir")))
    elif runtime_dir is None or not runtime_dir.is_dir():
        anomalies.append(_anomaly("RUNNER_EVIDENCE_UNREADABLE", snapshot["taskId"],
                                  snapshot["attemptId"], reason="runtime_dir_invalid"))
    else:
        runner = _read_runner_evidence(runtime_dir)
        if runner is None:
            anomalies.append(_anomaly("RUNNER_EVIDENCE_UNREADABLE", snapshot["taskId"],
                                      snapshot["attemptId"], reason="config_or_state_unreadable"))

    db_path = None
    config = runner["config"] if runner else None
    if config:
        snapshot["requestedModel"] = config.get("model")
        snapshot["runnerAlive"] = runner["runnerAlive"]
        status = runner["state"].get("status")
        # 镜像 inspect_run 语义：runner 已退出但无终态结果 → execution_unknown。
        if not runner["runnerAlive"] and status not in ("finished", "attention_required"):
            snapshot["runnerStatus"] = "execution_unknown"
            anomalies.append(_anomaly("EXECUTION_UNKNOWN", snapshot["taskId"],
                                      snapshot["attemptId"], runnerStatus=status))
        else:
            snapshot["runnerStatus"] = status
        # deadline 用 runner started 时刻 + 冻结 timeout，不用每轮更新的 at。
        started_epoch = runner.get("startedAtEpoch")
        timeout_ms = config.get("timeoutMs")
        receipt_wait = config.get("receiptWaitSeconds") or 0
        if started_epoch is not None and _is_int(timeout_ms) and timeout_ms > 0:
            seconds = timeout_ms / 1000.0 + (receipt_wait if _is_int(receipt_wait) else 0)
            deadline = datetime.fromtimestamp(started_epoch + seconds, tz=timezone.utc)
            snapshot["deadlineAt"] = deadline.isoformat()
        db_raw = config.get("db")
        if isinstance(db_raw, str) and db_raw:
            try:
                db_path = require_real_absolute(db_raw, "db")
            except ValueError:
                db_path = None
        if db_path is None or not db_path.is_file():
            anomalies.append(_anomaly("RUNNER_EVIDENCE_UNREADABLE", snapshot["taskId"],
                                      snapshot["attemptId"], reason="db_invalid"))
        # runner 与 workflow 的 task/attempt 绑定必须一致。
        for key in ("taskId", "attemptId"):
            if config.get(key) != workflow.get(key):
                anomalies.append(_anomaly("DB_RELATIONSHIP_CONFLICT", snapshot["taskId"],
                                          snapshot["attemptId"],
                                          field="runnerConfig.%s" % key,
                                          observed=config.get(key)))
        # workflow 顶层 db 必须与冻结 runner config 的 db 精确一致（错配 → RECONCILE）。
        if isinstance(db_field, str) and db_field.strip():
            workflow_db = _try_real_dir(db_field, "workflow.db")
            if workflow_db is None or db_path is None or workflow_db != db_path:
                anomalies.append(_anomaly("DB_MISMATCH", snapshot["taskId"],
                                          snapshot["attemptId"],
                                          workflowDb=db_field, runnerDb=config.get("db")))
        # 模型授权门禁（acceptance[5]）：workflow 当前 modelPolicy 必须与冻结 config 的
        # modelPolicySha256 / modelPolicy 一致并通过现有授权验证；缺失 / 撤销 / 漂移 /
        # 非法格式 / 未来时间都是结构性 RECONCILE，阻断错误 REVIEW/NEXT。只读复用验证函数。
        policy_anomaly = _validate_model_policy_evidence(workflow, config, now)
        if policy_anomaly is not None:
            anomalies.append(policy_anomaly)
        result = runner.get("result")
        if isinstance(result, dict):
            submission = result.get("submission")
            if isinstance(submission, dict) and submission.get("ok") is False:
                error = submission.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                if code in QUOTA_ERROR_CODES:
                    policy = snapshot["modelPolicy"]
                    allowed = policy.get("allowedModels") if isinstance(policy, dict) else None
                    backups = [m for m in (allowed or []) if m != snapshot["requestedModel"]]
                    if not backups:
                        anomalies.append(_anomaly("QUOTA_EXHAUSTED", snapshot["taskId"],
                                                  snapshot["attemptId"], model=snapshot["requestedModel"]))

    # -- 数据库关系（read_only）--
    delivery_id = review_id = review_decision = None
    delivery_state = "none"
    db_task = None
    if db_path is not None and db_path.is_file():
        store = SqliteStore(db_path, read_only=True)
        try:
            task = store.get_task(workflow["taskId"])
            attempt = store.get_attempt(workflow["attemptId"])
            if task is None:
                anomalies.append(_anomaly("DB_TASK_MISSING", snapshot["taskId"], snapshot["attemptId"]))
            elif attempt is None:
                anomalies.append(_anomaly("DB_ATTEMPT_MISSING", snapshot["taskId"], snapshot["attemptId"]))
            elif attempt["task_id"] != task["task_id"]:
                anomalies.append(_anomaly("DB_RELATIONSHIP_CONFLICT", snapshot["taskId"],
                                          snapshot["attemptId"], field="attempt.task_id"))
            else:
                db_task = task
                snapshot["taskStatus"] = task["status"]
                snapshot["submitState"] = attempt["submit_state"]
                snapshot["attemptAcceptedAt"] = attempt["accepted_by_host_at"]
                snapshot["eventCount"] = store.max_sequence(task["task_id"], attempt["attempt_id"])
                if attempt["submit_state"] == "unknown":
                    anomalies.append(_anomaly("ORPHAN_SUBMISSION", snapshot["taskId"],
                                              snapshot["attemptId"]))
                # workflow 顶层 allowedAgents/allowedSource（权威绑定，modelPolicy 不替代）
                # 必须覆盖冻结 runner config 与只读 DB task 观察到的 agent/source。
                valid_agents = (isinstance(agents_field, list) and agents_field
                                and all(isinstance(item, str) and item for item in agents_field))
                valid_source = isinstance(source_field, str) and bool(source_field.strip())
                observed_agents = []
                observed_sources = []
                if isinstance(config, dict):
                    if isinstance(config.get("agentId"), str) and config.get("agentId"):
                        observed_agents.append(config["agentId"])
                    config_binding = config.get("binding")
                    if isinstance(config_binding, dict) \
                            and isinstance(config_binding.get("agentId"), str) \
                            and config_binding.get("agentId"):
                        observed_agents.append(config_binding["agentId"])
                    if isinstance(config.get("sourceId"), str) and config.get("sourceId"):
                        observed_sources.append(config["sourceId"])
                if isinstance(task.get("agent_id"), str) and task.get("agent_id"):
                    observed_agents.append(task["agent_id"])
                if isinstance(task.get("source_id"), str) and task.get("source_id"):
                    observed_sources.append(task["source_id"])
                if valid_agents:
                    for agent in dict.fromkeys(observed_agents):
                        if agent not in agents_field:
                            anomalies.append(_anomaly("BINDING_NOT_ALLOWED",
                                                      snapshot["taskId"], snapshot["attemptId"],
                                                      field="allowedAgents", observed=agent))
                if valid_source:
                    for source in dict.fromkeys(observed_sources):
                        if source != source_field:
                            anomalies.append(_anomaly("BINDING_NOT_ALLOWED",
                                                      snapshot["taskId"], snapshot["attemptId"],
                                                      field="allowedSource", observed=source))
                # 交付与审核只信数据库，不推导 receipt 文件存在为 delivery。
                deliveries = [d for d in store.list_deliveries(task["task_id"])
                              if d["attempt_id"] == attempt["attempt_id"]]
                if deliveries:
                    latest = deliveries[-1]
                    delivery_id = latest["delivery_id"]
                    reviews = [r for r in store.list_reviews(task["task_id"])
                               if r["delivery_id"] == delivery_id]
                    if reviews:
                        latest_review = reviews[-1]
                        review_id = latest_review["review_id"]
                        review_decision = latest_review["decision"]
                        delivery_state = ("approved" if review_decision == "approved"
                                          else "reviewed")
                    else:
                        delivery_state = "new_unreviewed"
                # 回执普通文件摘要（只读）。
                workspace_raw = task["workspace"]
                try:
                    workspace = require_real_absolute(workspace_raw, "workspace")
                    receipts_dir = workspace / receipt_directory(task["task_id"], attempt["attempt_id"])
                    snapshot["receiptsPath"] = str(receipts_dir)
                    events_raw = _read_regular_file(receipts_dir / "events.json", root=workspace)
                    if events_raw is not None:
                        snapshot["receiptDigest"] = {
                            "sha256": sha256_bytes(events_raw), "size": len(events_raw)}
                except ValueError:
                    pass
        finally:
            store.close()
    snapshot["deliveryId"] = delivery_id
    snapshot["reviewId"] = review_id
    snapshot["reviewDecision"] = review_decision
    snapshot["deliveryState"] = delivery_state

    # -- host runtime 只读校验（acceptance[3][4]）：需要 read_only DB 的 task.workspace 以及
    #    accepted / delivery 事实；只在 DB 中 task 与 attempt 均合法时采纳宿主身份证据，否则
    #    以结构性异常对账，避免把错轮 runtime 的模型回显 / 终态当成有效证据。
    if isinstance(config, dict) and isinstance(db_task, dict):
        # workspace 授权根门禁（acceptance[4]）：task.workspace 必须同时被冻结
        # config.allowedRoot 与至少一个 modelPolicy.allowedRoots 覆盖；根无效 / 不覆盖
        # 判结构性 RECONCILE，优先 REVIEW/NEXT。
        roots_anomaly = _validate_workspace_authorization(
            config, db_task, snapshot["taskId"], snapshot["attemptId"])
        if roots_anomaly is not None:
            anomalies.append(roots_anomaly)
        observed_model, host_state, host_anomaly = _validate_host_runtime(
            config, workflow, db_task, snapshot.get("attemptAcceptedAt"),
            snapshot.get("deliveryState") != "none")
        if host_anomaly is not None:
            anomalies.append(host_anomaly)
        else:
            snapshot["observedModel"] = observed_model
            snapshot["hostRuntimeState"] = host_state
            if host_state == "unknown":
                anomalies.append(_anomaly("EXECUTION_UNKNOWN", snapshot["taskId"],
                                          snapshot["attemptId"], source="hostRuntime"))

    # -- 异常优先级：结构身份 > DB 关系 > 模型授权 > 宿主证据 > 执行未知 > 其他 --
    priority = ["WORKFLOW_IDENTITY_MISSING", "WORKFLOW_IDENTITY_CONFLICT", "WORKFLOW_INVALID",
                "WORKFLOW_BINDING_INVALID",
                "DB_TASK_MISSING", "DB_ATTEMPT_MISSING", "DB_RELATIONSHIP_CONFLICT",
                "DB_MISMATCH", "BINDING_NOT_ALLOWED",
                "MODEL_POLICY_MISSING", "MODEL_POLICY_DRIFT", "MODEL_POLICY_NOT_ALLOWED",
                "HOST_RUNTIME_BINDING_INVALID", "HOST_RUNTIME_EVIDENCE_MISSING",
                "RUNNER_PATH_CONFLICT", "RUNNER_EVIDENCE_UNREADABLE", "EXECUTION_UNKNOWN",
                "ORPHAN_SUBMISSION", "MODEL_MISMATCH",
                "QUOTA_EXHAUSTED"]
    chosen = None
    for code in priority:
        for candidate in anomalies:
            if candidate["code"] == code:
                chosen = candidate
                break
        if chosen:
            break
    if chosen is None and anomalies:
        chosen = anomalies[0]
    snapshot["anomaly"] = chosen
    snapshot["errorCode"] = chosen["code"] if chosen else None
    snapshot["reconciliationRequired"] = bool(chosen and chosen["code"] in RECONCILE_ERROR_CODES)

    # -- 进展签名：只含结构化执行事实，不含时间戳/pid/计划文件变化 --
    signature_source = [snapshot["taskStatus"], snapshot["submitState"], snapshot["eventCount"],
                        snapshot["deliveryId"], snapshot["reviewId"], snapshot["reviewDecision"],
                        snapshot["runnerStatus"], snapshot["deliveryState"]]
    snapshot["progressSignature"] = sha256_bytes(canonical_json(signature_source).encode("utf-8"))
    return snapshot


# ---------------------------------------------------------------------------
# 纯函数：evaluate_tick / validate_resume
# ---------------------------------------------------------------------------

def _new_state(binding, snapshot):
    return {
        "schemaVersion": WATCH_STATE_SCHEMA,
        "binding": dict(binding),
        "attemptId": snapshot.get("attemptId"),
        "lastSignature": snapshot.get("progressSignature"),
        "error": None,
        "noProgress": None,
        "reconciled": {},
        "pauseConfirmed": {},
        "notified": {},
        "updatedAt": None,
    }


def _notify_once(state, key):
    if key in state["notified"]:
        return False
    state["notified"][key] = True
    return True


def _directive(action, reason, notify_user, data, platform_action=None):
    return {
        "schemaVersion": DIRECTIVE_SCHEMA,
        "action": action,
        "reason": reason,
        "notifyUser": bool(notify_user),
        "data": data,
        "platformAction": platform_action,
    }


def _compact_data(snapshot, state, extra=None):
    data = {
        "taskId": snapshot.get("taskId"),
        "attemptId": snapshot.get("attemptId"),
        "taskStatus": snapshot.get("taskStatus"),
        "submitState": snapshot.get("submitState"),
        "runnerStatus": snapshot.get("runnerStatus"),
        "deadlineAt": snapshot.get("deadlineAt"),
        "deliveryId": snapshot.get("deliveryId"),
        "reviewId": snapshot.get("reviewId"),
        "receiptsPath": snapshot.get("receiptsPath"),
        "errorCode": snapshot.get("errorCode"),
        "errorFingerprint": state["error"]["fingerprint"] if state.get("error") else None,
        "errorCount": state["error"]["count"] if state.get("error") else 0,
        "noProgressCount": state["noProgress"]["count"] if state.get("noProgress") else 0,
    }
    if extra:
        data.update(extra)
    return data


def _handle_error_tick(snapshot, state, tick_id, anomaly):
    """对账类错误：同 fingerprint 连续观测计数；基线 + 2 个独立 tick 且对账已确认才 PAUSE。"""
    fingerprint = anomaly["fingerprint"]
    error = state.get("error")
    if error and error.get("fingerprint") == fingerprint:
        error["count"] += 1
        error["lastTickId"] = tick_id
    else:
        error = {"fingerprint": fingerprint, "count": 1,
                 "firstTickId": tick_id, "lastTickId": tick_id}
    state["error"] = error
    code = anomaly["code"]
    if fingerprint in state.get("pauseConfirmed", {}):
        return _directive("WAIT", "pause_confirmed_waiting_user_resume", False,
                          _compact_data(snapshot, state)), state
    confirmed = fingerprint in state.get("reconciled", {})
    if error["count"] >= ERROR_PAUSE_THRESHOLD and confirmed:
        platform_action = {
            "kind": "pause_automation",
            "automationId": snapshot["automationId"],
            "ownerThreadId": snapshot["ownerThreadId"],
            "reason": code,
            "notifyUser": True,
        }
        notify = _notify_once(state, "PAUSE:" + fingerprint)
        return _directive("PAUSE", code, notify,
                          _compact_data(snapshot, state, {"reconciliationConfirmed": True}),
                          platform_action), state
    notify = _notify_once(state, "RECONCILE:" + fingerprint)
    reason = code if confirmed else "%s:reconciliation_confirmation_required" % code
    return _directive("RECONCILE", reason, notify,
                      _compact_data(snapshot, state, {
                          "reconciliationConfirmed": confirmed,
                          "evidenceContract": list(RECONCILIATION_EVIDENCE_KEYS),
                      })), state


def evaluate_tick(snapshot, previous, tick_id, now):
    """纯函数：snapshot + 上一持久状态 → (directive, nextState)。不读写文件。"""
    if not isinstance(tick_id, str) or not tick_id.strip():
        raise ValueError("tick_id 必须是非空字符串")
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now 必须是 timezone-aware datetime")
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != SNAPSHOT_SCHEMA:
        raise ValueError("snapshot schema 不受支持")

    if isinstance(previous, dict) and previous.get("schemaVersion") == WATCH_STATE_SCHEMA:
        state = json.loads(json.dumps(previous, ensure_ascii=False))
    else:
        state = _new_state({}, snapshot)

    # attempt 变化重建基线；错误 fingerprint 内含 task/attempt，跨身份不会误续计数。
    if state.get("attemptId") != snapshot.get("attemptId"):
        state["error"] = None
        state["noProgress"] = None
        state["lastSignature"] = None
        state["attemptId"] = snapshot.get("attemptId")

    anomaly = snapshot.get("anomaly")
    structural = bool(anomaly and anomaly["code"] in STRUCTURAL_BLOCKING_CODES)
    error_anomaly = anomaly if (anomaly and anomaly["code"] in RECONCILE_ERROR_CODES) else None

    # 1. 结构性身份/DB 冲突：其他判定不可信，必须最先 RECONCILE。
    if structural:
        directive, state = _handle_error_tick(snapshot, state, tick_id, anomaly)
        state["updatedAt"] = now.isoformat()
        return directive, state

    # 2. 有新合法未审 delivery：优先 REVIEW（附异常诊断），不自动重新派发。
    if snapshot.get("deliveryState") == "new_unreviewed":
        state["noProgress"] = None
        if error_anomaly:
            _, state = _handle_error_tick(snapshot, state, tick_id, error_anomaly)
        elif anomaly is None:
            state["error"] = None
        directive = _directive("REVIEW", "new_delivery_awaiting_review", False,
                               _compact_data(snapshot, state))
        state["lastSignature"] = snapshot.get("progressSignature")
        state["updatedAt"] = now.isoformat()
        return directive, state

    # 3. 其余对账类异常（host crash / execution unknown / 模型不符 / 额度 / orphan）。
    if error_anomaly:
        directive, state = _handle_error_tick(snapshot, state, tick_id, error_anomaly)
        state["updatedAt"] = now.isoformat()
        return directive, state
    if anomaly is None:
        state["error"] = None

    # 4. 合法 approved delivery → NEXT 候选（不自动 launch）。
    if snapshot.get("deliveryState") == "approved":
        review_id = snapshot.get("reviewId") or "unknown"
        notify = _notify_once(state, "NEXT:" + review_id)
        directive = _directive("NEXT", "approved_delivery_next_candidate", notify,
                               _compact_data(snapshot, state, {"autoLaunch": False}))
        state["lastSignature"] = snapshot.get("progressSignature")
        state["updatedAt"] = now.isoformat()
        return directive, state

    # 5. deadline 判定：超过冻结 deadline 且无有效进展才累计，首 tick 落基线。
    progressed = (state.get("lastSignature") is not None
                  and snapshot.get("progressSignature") != state.get("lastSignature"))
    deadline_raw = snapshot.get("deadlineAt")
    deadline = None
    if isinstance(deadline_raw, str) and deadline_raw:
        try:
            deadline = datetime.fromisoformat(deadline_raw)
        except ValueError:
            deadline = None
    if deadline is not None and now > deadline and not progressed:
        fingerprint = _fingerprint(NO_PROGRESS_CODE, snapshot.get("taskId"), snapshot.get("attemptId"))
        if fingerprint in state.get("pauseConfirmed", {}):
            directive = _directive("WAIT", "pause_confirmed_waiting_user_resume", False,
                                   _compact_data(snapshot, state))
            state["updatedAt"] = now.isoformat()
            return directive, state
        no_progress = state.get("noProgress")
        if no_progress and no_progress.get("fingerprint") == fingerprint:
            no_progress["count"] += 1
            no_progress["lastTickId"] = tick_id
        else:
            no_progress = {"fingerprint": fingerprint, "count": 1,
                           "firstTickId": tick_id, "lastTickId": tick_id}
        state["noProgress"] = no_progress
        if no_progress["count"] >= DEADLINE_PAUSE_THRESHOLD:
            platform_action = {
                "kind": "pause_automation",
                "automationId": snapshot["automationId"],
                "ownerThreadId": snapshot["ownerThreadId"],
                "reason": NO_PROGRESS_CODE,
                "notifyUser": True,
            }
            notify = _notify_once(state, "PAUSE:" + fingerprint)
            # data.errorFingerprint 必须为已观察 noProgress 的稳定非空指纹，
            # 与 acknowledge_pause 接收的指纹一致（deadline 暂停确认全链）。
            directive = _directive("PAUSE", NO_PROGRESS_CODE, notify,
                                   _compact_data(snapshot, state,
                                                 {"errorFingerprint": fingerprint,
                                                  "errorCount": no_progress["count"]}),
                                   platform_action)
        else:
            notify = _notify_once(state, "WAIT:" + fingerprint)
            directive = _directive("WAIT", "overdue_without_valid_progress", notify,
                                   _compact_data(snapshot, state))
        state["updatedAt"] = now.isoformat()
        return directive, state

    # 6. 正常运行（deadline 内）→ WAIT；有进展则清空无进展基线。
    if progressed:
        state["noProgress"] = None
    state["lastSignature"] = snapshot.get("progressSignature")
    reason = ("runner_running_within_deadline" if snapshot.get("runnerAlive")
              else "runner_idle_within_deadline")
    directive = _directive("WAIT", reason, False, _compact_data(snapshot, state))
    state["updatedAt"] = now.isoformat()
    return directive, state


def validate_resume(snapshot, authorization):
    """纯函数恢复门：只验证新预算/新轮条件；不启动 worker 或 scheduler。"""
    reject = {"ok": False, "reason": None}
    if not isinstance(authorization, dict) or set(authorization) != RESUME_AUTHORIZATION_KEYS:
        reject["reason"] = "AUTHORIZATION_SCHEMA_INVALID"
        return reject
    if not isinstance(snapshot, dict) or snapshot.get("schemaVersion") != SNAPSHOT_SCHEMA:
        reject["reason"] = "SNAPSHOT_SCHEMA_INVALID"
        return reject
    if authorization["ownerThreadId"] != snapshot.get("ownerThreadId"):
        reject["reason"] = "OWNER_THREAD_ID_MISMATCH"
        return reject
    if authorization["automationId"] != snapshot.get("automationId"):
        reject["reason"] = "AUTOMATION_ID_MISMATCH"
        return reject
    if authorization["userExplicitContinue"] is not True:
        reject["reason"] = "USER_CONTINUE_NOT_EXPLICIT"
        return reject
    if not _is_int(authorization["maxRuns"]) or authorization["maxRuns"] <= 0:
        reject["reason"] = "MAX_RUNS_INVALID"
        return reject
    if not _is_int(authorization["intervalMinutes"]) or authorization["intervalMinutes"] < 5:
        reject["reason"] = "INTERVAL_TOO_SHORT"
        return reject
    if authorization["reconciliationComplete"] is not True:
        reject["reason"] = "RECONCILIATION_INCOMPLETE"
        return reject
    anomaly = snapshot.get("anomaly")
    if snapshot.get("runnerStatus") == "execution_unknown" or snapshot.get("hostRuntimeState") == "unknown":
        reject["reason"] = "OLD_UNKNOWN_STILL_PRESENT"
        return reject
    if anomaly is not None:
        reject["reason"] = "SNAPSHOT_RECONCILIATION_REQUIRED"
        return reject
    return {"ok": True, "reason": None}


# ---------------------------------------------------------------------------
# watch 目录：锁、状态持久化、指令缓存
# ---------------------------------------------------------------------------

def _prepare_watch_dir(watch_dir):
    p = require_real_absolute(watch_dir, "watch-dir")
    if not p.is_dir():
        raise ValueError("watch-dir 必须是已存在的目录")
    stat = p.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ValueError("watch-dir 必须是当前用户的 0700 目录")
    return p


def _ensure_no_overlap(watch, protected):
    for raw in protected:
        if not raw:
            continue
        root = Path(raw)
        if watch == root or watch in root.parents or root in watch.parents:
            raise ValueError("WATCH_DIR_OVERLAPS_PROTECTED_ROOT:%s" % root)


def _load_watch_state(watch):
    path = watch / "state.json"
    if path.is_symlink():
        raise ValueError("WATCH_STATE_SYMLINK_REFUSED")
    raw = _read_regular_file(path, root=watch)
    if raw is None:
        return None
    state = json.loads(raw.decode("utf-8"))
    if not isinstance(state, dict) or state.get("schemaVersion") != WATCH_STATE_SCHEMA:
        raise ValueError("WATCH_STATE_SCHEMA_UNSUPPORTED")
    return state


def _check_binding(state, binding):
    recorded = state.get("binding") or {}
    for key in ("workflowPath", "automationId", "ownerThreadId", "taskId"):
        expected, observed = binding.get(key), recorded.get(key)
        if expected is not None and observed is not None and expected != observed:
            raise ValueError("WATCH_IDENTITY_MISMATCH:%s" % key)


def _directive_cache_path(watch, tick_id):
    digest = hashlib.sha256(tick_id.encode("utf-8")).hexdigest()
    return watch / "directives" / ("%s.json" % digest)


def _load_cached_directive(watch, tick_id):
    path = _directive_cache_path(watch, tick_id)
    if path.is_symlink():
        raise ValueError("DIRECTIVE_CACHE_SYMLINK_REFUSED")
    raw = _read_regular_file(path, root=watch)
    if raw is None:
        return None
    directive = json.loads(raw.decode("utf-8"))
    if not isinstance(directive, dict) or directive.get("schemaVersion") != DIRECTIVE_SCHEMA:
        raise ValueError("DIRECTIVE_CACHE_INVALID")
    return directive


class _WatchLock(object):
    """fcntl 排它锁；锁文件固定在 watch 目录内。"""

    def __init__(self, watch):
        self.path = watch / "lock"
        self.fd = None

    def __enter__(self):
        if self.path.is_symlink():
            raise ValueError("WATCH_LOCK_SYMLINK_REFUSED")
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None
        return False


def _protected_roots(workflow_path_obj, workflow):
    """watch 禁写根（准确边界）。

    只保护：workflow 文件本身、DB 文件及 -wal/-shm、runner 目录、真实 host attempt
    目录、本轮 receipts 目录。不保护 workflow/DB 父目录，也不把整个 workspace/HOME
    当禁写根（同父独立 0700 兄弟目录可用）。边界由 read_only DB 任务 workspace 与冻结
    config 确定，不用任意 host workspace 扩大或放宽；结构关系读不到则 fail closed。
    """
    roots = [str(workflow_path_obj)]
    runner_dirs = []
    for key in ("currentRunner", "runtimeDir"):
        candidate = _try_real_dir(workflow.get(key), key)
        if candidate is not None and candidate not in runner_dirs:
            runner_dirs.append(candidate)
    if not runner_dirs:
        raise ValueError("PROTECTED_ROOTS_UNRESOLVED:runnerDir")
    roots.extend(str(directory) for directory in runner_dirs)
    config = None
    for directory in runner_dirs:
        config_raw = _read_regular_file(directory / "config.json", root=directory)
        if config_raw is None:
            continue
        try:
            parsed = json.loads(config_raw.decode("utf-8"))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            config = parsed
            break
    if config is None:
        raise ValueError("PROTECTED_ROOTS_UNRESOLVED:config")
    db_path = _try_real_dir(config.get("db"), "db")
    if db_path is None or not db_path.is_file():
        raise ValueError("PROTECTED_ROOTS_UNRESOLVED:db")
    for suffix in ("", "-wal", "-shm"):
        roots.append(str(db_path) + suffix)
    home = config.get("home")
    attempt_id = workflow.get("attemptId")
    if isinstance(home, str) and home and _safe_attempt_component(attempt_id):
        try:
            attempt_dir = (Path(home) / "d4-workbuddy-store" / "attempts" / attempt_id).resolve()
        except OSError:
            attempt_dir = None
        if attempt_dir is not None:
            roots.append(str(attempt_dir))
    store = SqliteStore(db_path, read_only=True)
    try:
        task = store.get_task(workflow.get("taskId"))
    finally:
        store.close()
    workspace_raw = task.get("workspace") if isinstance(task, dict) else None
    if not isinstance(workspace_raw, str) or not workspace_raw:
        raise ValueError("PROTECTED_ROOTS_UNRESOLVED:workspace")
    workspace = require_real_absolute(workspace_raw, "workspace")
    receipts = workspace / receipt_directory(workflow.get("taskId"), workflow.get("attemptId"))
    roots.append(str(receipts))
    return roots


def _binding_of(workflow_path_obj, workflow, automation_id, owner_thread_id):
    return {
        "workflowPath": str(workflow_path_obj),
        "automationId": automation_id,
        "ownerThreadId": owner_thread_id,
        "taskId": workflow.get("taskId"),
        "attemptId": workflow.get("attemptId"),
    }


def tick(workflow_path, automation_id, owner_thread_id, watch_dir, tick_id, now=None):
    """collector + 纯函数 + 锁下持久化；重复 tick 回放缓存 directive，不重复通知。"""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now 必须是 timezone-aware datetime")
    if not isinstance(tick_id, str) or not tick_id.strip():
        raise ValueError("tick_id 必须是非空字符串")
    watch = _prepare_watch_dir(watch_dir)
    workflow_path_obj, workflow = load_workflow(workflow_path)
    binding = _binding_of(workflow_path_obj, workflow, automation_id, owner_thread_id)
    # 受保护根重叠检查：任何写入发生前完成，保证 watch 只写自身目录。
    _ensure_no_overlap(watch, _protected_roots(workflow_path_obj, workflow))
    with _WatchLock(watch):
        # directives 子目录：mkdir/open/写缓存前严格校验真实路径。
        # 检查/创建必须置于同 watch 排它锁内、且在任何 directives/state/cache 写入之前：
        # 否则多个首次 tick 会在锁外并发观察到目录缺失并各自 mkdir，产生 FileExistsError
        # 竞争（acceptance[3]）。缺失可安全创建；已存在 symlink（指向外部或内部）立即拒，
        # 外部零新增文件。symlink / 非目录门禁与 protected-root 重叠检查保持不变。
        directives_dir = watch / "directives"
        if directives_dir.is_symlink():
            raise ValueError("DIRECTIVE_DIR_SYMLINK_REFUSED")
        if directives_dir.exists():
            if not directives_dir.is_dir():
                raise ValueError("DIRECTIVE_DIR_INVALID")
        else:
            directives_dir.mkdir(mode=0o700)
        state = _load_watch_state(watch)
        if state is not None:
            _check_binding(state, binding)
            cached = _load_cached_directive(watch, tick_id)
            if cached is not None:
                return cached
        snapshot = collect_snapshot(str(workflow_path_obj), automation_id, owner_thread_id,
                                    now=now)
        directive, new_state = evaluate_tick(snapshot, state, tick_id, now)
        new_state["binding"] = binding
        new_state["updatedAt"] = now.isoformat()
        _write_json_atomic(_directive_cache_path(watch, tick_id), directive)
        _write_json_atomic(watch / "state.json", new_state)
        return directive


# ---------------------------------------------------------------------------
# 对账 / 暂停确认（watch 只记录待 cox 核验的引用，不自行调用官方工具）
# ---------------------------------------------------------------------------

def acknowledge_reconciliation(workflow_path, automation_id, owner_thread_id, watch_dir,
                               fingerprint, evidence_path):
    """同 task/attempt/fingerprint 验证对账证据；结构冻结，缺一不可。"""
    for value, label in ((fingerprint, "fingerprint"),):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("%s 必须是非空字符串" % label)
    watch = _prepare_watch_dir(watch_dir)
    workflow_path_obj, workflow = load_workflow(workflow_path)
    binding = _binding_of(workflow_path_obj, workflow, automation_id, owner_thread_id)
    _ensure_no_overlap(watch, _protected_roots(workflow_path_obj, workflow))
    evidence_raw = _read_regular_file(Path(evidence_path))
    if evidence_raw is None:
        raise ValueError("EVIDENCE_FILE_UNREADABLE")
    evidence = json.loads(evidence_raw.decode("utf-8"))
    if not isinstance(evidence, dict) or tuple(sorted(evidence)) != tuple(sorted(RECONCILIATION_EVIDENCE_KEYS)):
        raise ValueError("EVIDENCE_SCHEMA_INVALID")
    if evidence["taskId"] != workflow.get("taskId") or evidence["attemptId"] != workflow.get("attemptId"):
        raise ValueError("EVIDENCE_IDENTITY_MISMATCH")
    if evidence["fingerprint"] != fingerprint:
        raise ValueError("EVIDENCE_FINGERPRINT_MISMATCH")
    for key in ("processChecked", "sourceChecked", "receiptsChecked"):
        if evidence[key] is not True:
            raise ValueError("EVIDENCE_CHECK_INCOMPLETE:%s" % key)
    with _WatchLock(watch):
        state = _load_watch_state(watch)
        if state is None:
            raise ValueError("WATCH_STATE_MISSING")
        _check_binding(state, binding)
        error = state.get("error") or {}
        if error.get("fingerprint") != fingerprint and fingerprint not in state.get("reconciled", {}):
            raise ValueError("FINGERPRINT_NOT_OBSERVED")
        state.setdefault("reconciled", {})[fingerprint] = {
            "evidenceSha256": sha256_bytes(evidence_raw), "confirmedAt": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(watch / "state.json", state)
    return {"ok": True, "acknowledged": "reconciliation", "fingerprint": fingerprint,
            "taskId": workflow.get("taskId"), "attemptId": workflow.get("attemptId")}


# TOML basic string 单字符转义（等价于 py 字面，纯查表，不 eval）。
_TOML_STRING_ESCAPES = {
    "b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
    '"': '"', "\\": "\\",
}
# Unicode scalar 非法区间（代理区间 D800–DFFF）：\u / \U / 直接字符均须拒绝，
# 不 normalize、不吞异常。
_SURROGATE_MIN = 0xD800
_SURROGATE_MAX = 0xDFFF
# TOML basic string 为单行字符串：未转义控制字符 U+0000–U+0008、U+000A–U+001F、U+007F
# 非法；literal TAB U+0009 允许。标准转义 \\b \\t \\n \\f \\r 产生的控制字符不受此限。


def _parse_toml_basic_string(raw_value, lineno):
    """解析单行双引号 TOML basic string（含转义）；不执行内容，非法一律拒绝。

    支持标准转义 \\b \\t \\n \\f \\r \\" \\\\ \\uXXXX \\UXXXXXXXX；未知转义、未闭合、
    字符串外多余内容、非法 Unicode 码点均抛受控 ValueError。不使用 eval/exec。
    额外按 TOML basic string 单行规则拒绝：非法 Unicode scalar（代理区间 U+D800–U+DFFF，
    \\u / \\U / 直接字符）与未转义控制字符（U+0000–U+0008、U+000A–U+001F、U+007F，
    literal TAB U+0009 允许）。只拒绝，不吞异常，也不把非法字符 normalize 成合法。
    """
    if not raw_value.startswith('"'):
        raise ValueError("DURABLE_RECORD_INVALID_STRING:%d" % lineno)
    chars = []
    index = 1
    length = len(raw_value)
    while index < length:
        char = raw_value[index]
        if char == '"':
            if index != length - 1:
                raise ValueError("DURABLE_RECORD_INVALID_STRING:%d" % lineno)
            return "".join(chars)
        if char == "\\":
            index += 1
            if index >= length:
                raise ValueError("DURABLE_RECORD_INVALID_STRING:%d" % lineno)
            escape = raw_value[index]
            if escape in _TOML_STRING_ESCAPES:
                chars.append(_TOML_STRING_ESCAPES[escape])
            elif escape in ("u", "U"):
                width = 4 if escape == "u" else 8
                digits = raw_value[index + 1:index + 1 + width]
                if len(digits) != width or not re.match(r'^[0-9A-Fa-f]{%d}$' % width, digits):
                    raise ValueError("DURABLE_RECORD_INVALID_ESCAPE:%d" % lineno)
                codepoint = int(digits, 16)
                if codepoint > 0x10FFFF:
                    raise ValueError("DURABLE_RECORD_INVALID_ESCAPE:%d" % lineno)
                if _SURROGATE_MIN <= codepoint <= _SURROGATE_MAX:
                    raise ValueError("DURABLE_RECORD_INVALID_UNICODE:%d" % lineno)
                chars.append(chr(codepoint))
                index += width
            else:
                raise ValueError("DURABLE_RECORD_INVALID_ESCAPE:%d" % lineno)
        else:
            code = ord(char)
            if _SURROGATE_MIN <= code <= _SURROGATE_MAX:
                raise ValueError("DURABLE_RECORD_INVALID_UNICODE:%d" % lineno)
            if code < 0x20 and code != 0x09:
                raise ValueError("DURABLE_RECORD_INVALID_CONTROL:%d" % lineno)
            if code == 0x7F:
                raise ValueError("DURABLE_RECORD_INVALID_CONTROL:%d" % lineno)
            chars.append(char)
        index += 1
    raise ValueError("DURABLE_RECORD_INVALID_STRING:%d" % lineno)


def _parse_restricted_toml(text):
    """受限 TOML 键解析（兼容 Python 3.9）：接受 key = "string" / 整数 / bool。

    只解析本模块需要的标量键；数组 / 表头 / 内联表 / 多行 / 日期等结构一律拒绝，不做
    部分容忍。双引号字符串走 _parse_toml_basic_string（支持标准转义）。允许真实完整
    automation.toml 的额外标量字段（version/name/prompt/rrule/created_at/updated_at 等），
    不要求恰好四字段。重复 key / 坏语法 / 不支持结构明确拒绝。不 eval/exec，不装依赖。
    """
    parsed = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r'^([A-Za-z0-9_.\-]+)\s*=\s*(.+)$', stripped)
        if not match:
            raise ValueError("DURABLE_RECORD_INVALID_LINE:%d" % lineno)
        key, raw_value = match.group(1), match.group(2).strip()
        if key in parsed:
            raise ValueError("DURABLE_RECORD_DUPLICATE_KEY:%s" % key)
        if raw_value.startswith('"'):
            parsed[key] = _parse_toml_basic_string(raw_value, lineno)
        elif re.match(r'^-?\d+$', raw_value):
            parsed[key] = int(raw_value)
        elif raw_value in ("true", "false"):
            parsed[key] = raw_value == "true"
        else:
            raise ValueError("DURABLE_RECORD_INVALID_VALUE:%d" % lineno)
    return parsed


class _DuplicateKeyError(Exception):
    """JSON 对象出现重复键（避免 json.loads 最后键覆盖，把歧义静默接受）。"""

    def __init__(self, key):
        super().__init__(key)
        self.key = key


def _object_pairs_no_duplicate_key(pairs):
    """object_pairs_hook：任一对象出现重复键即抛 _DuplicateKeyError（不静默 last-wins）。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _extract_official_tool_payload(receipt):
    """从原始官方 automation_update 工具回执中提取唯一明确的 JSON 文本对象。

    顶层结构 {"content": [...], "isError": false}：只扫描 content 中 type == "text" 的
    文本块，统计其中**所有**能整块成功解析为 JSON 对象的块（普通非 JSON 说明文本忽略）。
    恰好一个对象且含 automationId/mode/status 才采纳；出现第二个对象（即使不含
    automationId，如 {"status": "ACTIVE"}）判 OFFICIAL_RECEIPT_PAYLOAD_AMBIGUOUS。
    任一对象含重复键（含 automationId/status/mode）→ OFFICIAL_RECEIPT_JSON_DUPLICATE_KEY，
    避免 json.loads 最后键覆盖把歧义静默接受。错误回执 (isError != false) / 无对象 /
    唯一对象缺字段一律拒绝；不做嵌入文字模糊抽取。
    原工具不返回 ownerThreadId，本函数不得编造 owner；owner 由受信 workflow 绑定与
    durable exact target_thread_id 验证。
    """
    if not isinstance(receipt, dict):
        raise ValueError("OFFICIAL_RECEIPT_SCHEMA_INVALID")
    if "content" not in receipt or "isError" not in receipt:
        raise ValueError("OFFICIAL_RECEIPT_SCHEMA_INVALID")
    if receipt["isError"] is not False:
        raise ValueError("OFFICIAL_RECEIPT_TOOL_ERROR")
    content = receipt["content"]
    if not isinstance(content, list) or not content:
        raise ValueError("OFFICIAL_RECEIPT_SCHEMA_INVALID")
    objects = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            value = json.loads(text, object_pairs_hook=_object_pairs_no_duplicate_key)
        except _DuplicateKeyError as exc:
            raise ValueError("OFFICIAL_RECEIPT_JSON_DUPLICATE_KEY:%s" % exc.key)
        except ValueError:
            # 整块不是 JSON：视为普通说明文本，忽略（不做嵌入抽取）。
            continue
        if isinstance(value, dict):
            objects.append(value)
    if not objects:
        raise ValueError("OFFICIAL_RECEIPT_PAYLOAD_MISSING")
    if len(objects) > 1:
        raise ValueError("OFFICIAL_RECEIPT_PAYLOAD_AMBIGUOUS")
    payload = objects[0]
    for key in ("automationId", "mode", "status"):
        if key not in payload:
            raise ValueError("OFFICIAL_RECEIPT_PAYLOAD_INCOMPLETE")
    return payload


def acknowledge_pause(workflow_path, automation_id, owner_thread_id, watch_dir, fingerprint,
                      official_receipt_path, durable_record_path):
    """官方回执 + durable 记录都精确 PAUSED 才记录 confirmed；失败不吞、不改状态。

    回执支持两种形态：既有三字段规范回执（automationId/ownerThreadId/status，owner 由
    回执自身 strict 校验），或保留在文件里的原始官方 automation_update 工具 JSON 回执
    （顶层 content 文本块 + isError=false，唯一 JSON payload，exact automationId/status/mode；
    原工具不返回 owner，owner 由受信 workflow 绑定与 durable exact target_thread_id 验证）。
    durable 允许真实完整 automation.toml 的额外字段，但必须 exact
    id == automationId、kind == heartbeat、target_thread_id == owner、status == PAUSED。
    任何拒绝都不写 pauseConfirmed，也不吞异常；保存原始两文件的 SHA256。
    """
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError("fingerprint 必须是非空字符串")
    watch = _prepare_watch_dir(watch_dir)
    workflow_path_obj, workflow = load_workflow(workflow_path)
    binding = _binding_of(workflow_path_obj, workflow, automation_id, owner_thread_id)
    _ensure_no_overlap(watch, _protected_roots(workflow_path_obj, workflow))
    receipt_raw = _read_regular_file(Path(official_receipt_path))
    if receipt_raw is None:
        raise ValueError("OFFICIAL_RECEIPT_UNREADABLE")
    receipt = json.loads(receipt_raw.decode("utf-8"))
    if not isinstance(receipt, dict):
        raise ValueError("OFFICIAL_RECEIPT_SCHEMA_INVALID")
    if set(receipt) == set(OFFICIAL_PAUSE_RECEIPT_KEYS):
        # 既有三字段规范回执：owner 由回执自身 strict 校验，保留原语义。
        if receipt["automationId"] != automation_id or receipt["ownerThreadId"] != owner_thread_id:
            raise ValueError("OFFICIAL_RECEIPT_IDENTITY_MISMATCH")
        if receipt["status"] != "PAUSED":
            raise ValueError("OFFICIAL_RECEIPT_NOT_PAUSED")
    elif "content" in receipt and "isError" in receipt:
        # 原始官方工具回执：exact automationId / status / mode 核验；不伪造 owner。
        payload = _extract_official_tool_payload(receipt)
        if payload["automationId"] != automation_id:
            raise ValueError("OFFICIAL_RECEIPT_IDENTITY_MISMATCH")
        if payload["status"] != "PAUSED":
            raise ValueError("OFFICIAL_RECEIPT_NOT_PAUSED")
        if payload["mode"] != "update":
            raise ValueError("OFFICIAL_RECEIPT_MODE_INVALID")
    else:
        raise ValueError("OFFICIAL_RECEIPT_SCHEMA_INVALID")
    durable_raw = _read_regular_file(Path(durable_record_path))
    if durable_raw is None:
        raise ValueError("DURABLE_RECORD_UNREADABLE")
    durable = _parse_restricted_toml(durable_raw.decode("utf-8"))
    for key in DURABLE_PAUSE_KEYS:
        if key not in durable:
            raise ValueError("DURABLE_RECORD_SCHEMA_INVALID")
    if not (isinstance(durable["id"], str) and isinstance(durable["kind"], str)
            and isinstance(durable["target_thread_id"], str) and isinstance(durable["status"], str)):
        raise ValueError("DURABLE_RECORD_MISMATCH")
    if durable["id"] != automation_id:
        raise ValueError("DURABLE_RECORD_IDENTITY_MISMATCH")
    if durable["kind"] != "heartbeat" or durable["status"] != "PAUSED" \
            or durable["target_thread_id"] != owner_thread_id:
        raise ValueError("DURABLE_RECORD_MISMATCH")
    with _WatchLock(watch):
        state = _load_watch_state(watch)
        if state is None:
            raise ValueError("WATCH_STATE_MISSING")
        _check_binding(state, binding)
        # 指纹内嵌 task/attempt 必须与当前 workflow 绑定一致（错 task/attempt 均拒）。
        fp_task, fp_attempt = _fingerprint_identity(fingerprint)
        if fp_task is not None and (fp_task != workflow.get("taskId")
                                    or fp_attempt != workflow.get("attemptId")):
            raise ValueError("FINGERPRINT_IDENTITY_MISMATCH")
        error = state.get("error") or {}
        no_progress = state.get("noProgress") or {}
        observed = {error.get("fingerprint"), no_progress.get("fingerprint")}
        if fingerprint not in observed and fingerprint not in state.get("pauseConfirmed", {}):
            raise ValueError("FINGERPRINT_NOT_OBSERVED")
        state.setdefault("pauseConfirmed", {})[fingerprint] = {
            "officialReceiptSha256": sha256_bytes(receipt_raw),
            "durableRecordSha256": sha256_bytes(durable_raw),
            "confirmedAt": datetime.now(timezone.utc).isoformat(),
        }
        _write_json_atomic(watch / "state.json", state)
    return {"ok": True, "acknowledged": "pause", "fingerprint": fingerprint,
            "automationId": automation_id, "ownerThreadId": owner_thread_id}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _add_binding_args(parser):
    parser.add_argument("--workflow", type=Path, required=True)
    parser.add_argument("--automation-id", required=True)
    parser.add_argument("--owner-thread-id", required=True)
    parser.add_argument("--watch-dir", type=Path, required=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="D4 无模型 heartbeat controller（只读证据 + 受控 directive）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    tick_parser = sub.add_parser("tick")
    _add_binding_args(tick_parser)
    tick_parser.add_argument("--tick-id", required=True)
    recon = sub.add_parser("acknowledge-reconciliation")
    _add_binding_args(recon)
    recon.add_argument("--fingerprint", required=True)
    recon.add_argument("--evidence", type=Path, required=True)
    pause = sub.add_parser("acknowledge-pause")
    _add_binding_args(pause)
    pause.add_argument("--fingerprint", required=True)
    pause.add_argument("--official-receipt", type=Path, required=True)
    pause.add_argument("--durable-record", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "tick":
            result = tick(str(args.workflow), args.automation_id, args.owner_thread_id,
                          str(args.watch_dir), args.tick_id)
        elif args.cmd == "acknowledge-reconciliation":
            result = acknowledge_reconciliation(str(args.workflow), args.automation_id,
                                                args.owner_thread_id, str(args.watch_dir),
                                                args.fingerprint, str(args.evidence))
        else:
            result = acknowledge_pause(str(args.workflow), args.automation_id,
                                       args.owner_thread_id, str(args.watch_dir),
                                       args.fingerprint, str(args.official_receipt),
                                       str(args.durable_record))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False,
                          "error": {"type": type(exc).__name__, "message": str(exc)}},
                         ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
