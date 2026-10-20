"""任务服务：创建任务、创建执行轮次、提交到宿主适配器。

边界（本卡 §0 / §6 与 R1 返工卡“必须修正 #1/#3/#4/#5”）：
- 所有入口必须由受控入口注入 `CallerContext`；默认无身份拒绝。
- 创建任务必须指定已登记来源与已登记 Agent；权限开关必须是真 bool 且全为假；
  writeScope 是显式路径数组，规范化后必须落在 workspace 上限内。
- 一个 taskId 对应一个 projectId；返工不改 taskId，只新增 attemptId，并清掉旧 current_delivery_id。
- 创建执行轮次受 limits.maxAttempts 约束，禁止自动循环返工。
- 提交外部宿主前先原子占位（幂等键 = attemptId），重复提交只返回既有结果或明确未知，
  绝不在长数据库事务内等待外部调用。
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from contracts.paths import resolve_scopes, resolve_workspace
from adapters.task_permissions import MEMBERS, build_policy
from contracts.schemas import (
    FORBIDDEN_PERMISSION_KEYS,
    ContractError,
    is_strict_card_revision,
    validate_create_task,
)

from .context import REVIEW_SUBMIT, TASK_CREATE, TASK_EXECUTE, require_context

DEFAULT_LIMITS = {"maxAttempts": 2, "maxReviewRounds": 2, "timeoutMinutes": 60}
_TRUSTED_EVIDENCE = ("OFFICIAL", "LOCAL")
# 受控来源摘要必须是完整 sha256 形态（sha256: + 64 位小写十六进制）。
# 只做形态防御校验；这不代表已重新读取并验证原件，登记仍以受控入口的实际读取摘要为准。
# R4 窄返工：用 \Z 而非 $ 锚定整串。Python 的 $ 允许在结尾换行前匹配，会把
# "sha256:<64hex>\n"（尾 LF）误放；\Z 只匹配整串末尾，尾 LF/CR 等一律拒绝。
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix):
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TaskService:
    def __init__(self, store, adapter=None, allowed_roots=None):
        self.store = store
        self.adapter = adapter
        # 受控服务配置：workspace 只允许落在这些隔离根内；缺省为空则 fail closed。
        self.allowed_roots = list(allowed_roots) if allowed_roots else None

    # -- 创建任务 ---------------------------------------------------------
    def create_task(self, payload, context=None, source_card=None):
        require_context(context, TASK_CREATE)

        errors = validate_create_task(payload)
        if errors:
            raise ContractError("invalid_task", details=errors)

        permissions = payload["permissions"]
        # workspace 上限由服务配置注入的 allowed roots 决定 + writeScope 路径包含校验
        # （拒绝调用者自定上限、`..` 逃逸、绝对路径逃逸与符号链接逃逸）
        workspace = resolve_workspace(payload["workspace"], self.allowed_roots)
        resolve_scopes(workspace, permissions["writeScope"])
        if payload["agentId"] in MEMBERS:
            if MEMBERS[payload["agentId"]][0] != payload["sourceId"]:
                raise ContractError("permission_denied", message="成员与宿主来源不匹配")
            try:
                build_policy(payload["agentId"], workspace, permissions, allowed_roots=self.allowed_roots)
            except ValueError as exc:
                raise ContractError("permission_denied", message=str(exc)) from exc

        forbidden = [k for k in FORBIDDEN_PERMISSION_KEYS if permissions.get(k)]
        if forbidden:
            raise ContractError(
                "permission_denied",
                message="验证切片不允许权限：%s" % ", ".join(forbidden),
                details={
                    "forbidden": forbidden,
                    "hint": "验证任务必须 allowNetwork/allowBusinessWrite/allowGit/allowPublish 全为 False",
                },
            )
        if self.store.get_source(payload["sourceId"]) is None:
            raise ContractError("unknown_source", details={"sourceId": payload["sourceId"]})
        if self.store.get_agent(payload["agentId"]) is None:
            raise ContractError(
                "permission_denied",
                message="执行成员未登记：%s" % (payload["agentId"],),
                details={"agentId": payload["agentId"], "hint": "先在 agents 登记该成员"},
            )

        limits = dict(DEFAULT_LIMITS)
        if isinstance(payload.get("limits"), dict):
            limits.update({k: v for k, v in payload["limits"].items() if v is not None})

        # D4-NEXT-01C-R1：来源任务卡只允许在受信 TASK_CREATE 上下文内随任务原子冻结。
        # source_card 由受控入口（d4ctl）在整体校验后构造；服务层做防御性结构校验，
        # 不接受畸形对象，也不允许后续由 worker/报告导入补登记。
        source_record = self._validate_source_card(source_card, payload)

        at = now_iso()
        task_id = new_id("task")
        record = {
            "task_id": task_id,
            "project_id": payload["projectId"],
            "title": payload["title"],
            "agent_id": payload["agentId"],
            "source_id": payload["sourceId"],
            "workspace": workspace,
            "baseline": payload.get("baseline"),
            "goal": payload["goal"],
            "acceptance_json": _dump(payload.get("acceptance") or []),
            "permissions_json": _dump(permissions),
            "limits_json": _dump(limits),
            "status": "ready",
            "revision": 1,
            "current_attempt_id": None,
            "current_delivery_id": None,
            "closure_json": _dump(
                {"qaExecuted": False, "userConfirmed": False, "git": False, "published": False}
            ),
            "created_at": at,
            "updated_at": at,
        }
        with self.store.transaction():
            self.store.insert_task(record)
            if source_record is not None:
                source_record = dict(source_record)
                source_record["task_id"] = task_id
                source_record["created_at"] = at
                # R2-C：来源登记稳定关联受控入口身份（context），不接受 payload 自报 issuer。
                source_record["registered_by_json"] = _dump(context.to_dict())
                self.store.insert_source_card(source_record)
            self.store.log_activity(
                task_id, None, "task.created",
                {"agentId": payload["agentId"], "projectId": payload["projectId"],
                 "workspace": workspace, "writeScope": permissions.get("writeScope"),
                 "sourceCard": ({"cardId": source_record["card_id"],
                                 "cardRevision": source_record["card_revision"]}
                                if source_record is not None else None),
                 "createdBy": context.to_dict()},
                at,
            )
        return {
            "taskId": task_id,
            "status": "ready",
            "revision": 1,
            "agentId": payload["agentId"],
            "projectId": payload["projectId"],
            "workspace": workspace,
            "permissions": permissions,
            "sourceCard": ({"cardId": source_record["card_id"],
                            "cardRevision": source_record["card_revision"],
                            "verified": True} if source_record is not None else None),
            "sourceCardRegistered": source_record is not None,
        }

    def _validate_source_card(self, source_card, payload):
        """受控来源卡对象的防御性校验；畸形一律 source_card_mismatch。

        返回可直接落库的字段字典（不含 task_id/created_at，由 create_task 补齐），
        或 None（未登记来源，兼容旧 JSON 建卡）。
        """
        if source_card is None:
            return None
        if not isinstance(source_card, dict):
            raise ContractError(
                "source_card_mismatch",
                message="受控来源卡对象必须是对象",
                details={"type": type(source_card).__name__},
            )
        card_id = source_card.get("cardId")
        card_revision = source_card.get("cardRevision")
        project_id = source_card.get("projectId")
        path = source_card.get("path")
        sha256 = source_card.get("sha256")
        canonical_digest = source_card.get("canonicalDigest")
        if not isinstance(card_id, str) or not card_id.strip():
            raise ContractError(
                "source_card_mismatch",
                message="受控来源卡缺少 cardId，拒绝建卡",
                details={"cardId": card_id},
            )
        # bool 是 int 子类：显式拒绝，防止 True/False 冒充版本号；版本必须 >=1。
        if not is_strict_card_revision(card_revision):
            raise ContractError(
                "source_card_mismatch",
                message="受控来源卡 cardRevision 必须是 >=1 的整数，拒绝字符串/布尔/0/负数",
                details={"cardRevision": card_revision, "type": type(card_revision).__name__},
            )
        if not isinstance(path, str) or not path.startswith("/"):
            raise ContractError(
                "source_card_mismatch",
                message="受控来源卡需要受控绝对路径",
                details={"path": path},
            )
        # 防御性形态校验：摘要必须是完整 sha256（sha256: + 64 位十六进制）。
        # 这不代表已重新读取并验证原件；实际摘要由受控入口读取计算后注入。
        if not isinstance(sha256, str) or not _SHA256_RE.match(sha256):
            raise ContractError(
                "source_card_mismatch",
                message="受控来源卡摘要必须是完整 sha256 形态，拒绝信任卡内自报值",
                details={"sha256": sha256},
            )
        if not isinstance(project_id, str) or not project_id.strip():
            raise ContractError(
                "source_card_mismatch",
                message="受控来源卡缺少 projectId，拒绝建卡",
                details={"projectId": project_id},
            )
        if project_id != payload["projectId"]:
            raise ContractError(
                "source_card_mismatch",
                message="来源任务卡项目与建卡项目不一致，拒绝建卡",
                details={"sourceProjectId": project_id, "taskProjectId": payload["projectId"]},
            )
        return {
            "project_id": project_id,
            "card_id": card_id,
            "card_revision": card_revision,
            "source_path": path,
            "source_sha256": sha256,
            "canonical_digest": canonical_digest,
            "registered_json": _dump({
                "cardId": card_id,
                "cardRevision": card_revision,
                "projectId": project_id,
                "path": path,
                "sha256": sha256,
                "canonicalDigest": canonical_digest,
            }),
        }

    # -- 创建执行轮次 -----------------------------------------------------
    def _authorize_attempt(self, task, context):
        """执行轮次可由 worker（绑定 task.agent_id）或 reviewer/owner（创建返工）发起。"""
        require_context(context, None)
        if context.has(REVIEW_SUBMIT):
            return
        if context.has(TASK_EXECUTE) and context.actor_id == task["agent_id"]:
            return
        raise ContractError(
            "permission_denied",
            message="调用者不能为任务 %s 创建执行轮次" % (task["task_id"],),
            details={"actorId": context.actor_id, "taskAgentId": task["agent_id"]},
        )

    def create_attempt(self, task_id, note=None, context=None):
        task = self.store.get_task(task_id)
        if task is None:
            raise ContractError(
                "invalid_task", message="任务不存在：%s" % task_id, details={"taskId": task_id}
            )
        self._authorize_attempt(task, context)
        if task["status"] not in ("ready", "rework_requested", "blocked"):
            raise ContractError(
                "invalid_transition",
                message="当前任务状态 %s 不能创建新的执行轮次" % (task["status"],),
                details={"status": task["status"],
                         "allowed": ["ready", "rework_requested", "blocked"]},
            )

        limits = json.loads(task["limits_json"] or "{}")
        max_attempts = int(limits.get("maxAttempts", DEFAULT_LIMITS["maxAttempts"]))

        at = now_iso()
        attempt_id = new_id("att")
        record = {
            "attempt_id": attempt_id,
            "task_id": task_id,
            "ordinal": None,
            "state": "created",
            "created_at": at,
            "submitted_at": None,
            "accepted_by_host_at": None,
            "started_at": None,
            "terminal_at": None,
            "terminal_kind": None,
            "blocked_json": None,
            "note": note,
            "submit_state": "created",
            "submission_json": None,
        }
        with self.store.transaction():
            # 轮次上限在写事务内复核，避免并发下双开（跨连接 CAS）；同时复读任务事实
            fresh = self.store.get_task(task_id)
            if fresh is None:
                raise ContractError(
                    "invalid_task", message="任务不存在：%s" % task_id, details={"taskId": task_id}
                )
            self._authorize_attempt(fresh, context)
            if fresh["status"] not in ("ready", "rework_requested", "blocked"):
                raise ContractError(
                    "invalid_transition",
                    message="当前任务状态 %s 不能创建新的执行轮次" % (fresh["status"],),
                    details={"status": fresh["status"],
                             "allowed": ["ready", "rework_requested", "blocked"]},
                )
            count = self.store.count_attempts(task_id)
            if count >= max_attempts:
                raise ContractError(
                    "invalid_transition",
                    message="已达到最大执行轮次上限 %d，不能自动继续返工" % max_attempts,
                    details={"attempts": count, "maxAttempts": max_attempts,
                             "hint": "禁止自动循环返工"},
                )
            ordinal = count + 1
            record["ordinal"] = ordinal
            self.store.insert_attempt(record)
            # 新执行轮次开始后，旧交付不再是当前交付（旧交付仍可查，但不能继续审核）
            self.store.update_task(
                task_id, current_attempt_id=attempt_id, current_delivery_id=None,
                status="active", updated_at=at,
            )
            self.store.log_activity(
                task_id, attempt_id, "attempt.created",
                {"ordinal": ordinal, "note": note, "by": context.actor_id}, at,
            )
        return {"taskId": task_id, "attemptId": attempt_id, "ordinal": ordinal, "state": "created"}

    # -- 提交到宿主 -------------------------------------------------------
    def submit_attempt(self, task_id, attempt_id, context=None):
        require_context(context, TASK_EXECUTE)
        task = self.store.get_task(task_id)
        attempt = self.store.get_attempt(attempt_id)
        if task is None or attempt is None or attempt["task_id"] != task_id:
            raise ContractError(
                "invalid_task",
                message="任务或执行轮次不存在",
                details={"taskId": task_id, "attemptId": attempt_id},
            )
        if context.actor_id != task["agent_id"]:
            raise ContractError(
                "permission_denied",
                message="调用者与任务指定 Agent 不一致，不能提交",
                details={"actorId": context.actor_id, "taskAgentId": task["agent_id"]},
            )
        if attempt["terminal_at"] is not None:
            raise ContractError(
                "invalid_transition",
                message="执行轮次已结束，不能重复提交",
                details={"attemptId": attempt_id, "terminalKind": attempt["terminal_kind"]},
            )
        if self.adapter is None:
            raise ContractError(
                "invalid_transition", message="未配置宿主适配器", details={"allowed": []}
            )

        # 1) 原子占位（幂等键 = attemptId）。整个占位在一个写事务内完成。
        with self.store.transaction():
            # 占位前在同一写事务内复读最新事实：轮次/终态/身份，避免拿锁前旧快照覆盖新事实
            fresh_task = self.store.get_task(task_id)
            fresh_attempt = self.store.get_attempt(attempt_id)
            if (fresh_task is None or fresh_attempt is None
                    or fresh_attempt["task_id"] != task_id):
                raise ContractError(
                    "invalid_task",
                    message="任务或执行轮次不存在",
                    details={"taskId": task_id, "attemptId": attempt_id},
                )
            if context.actor_id != fresh_task["agent_id"]:
                raise ContractError(
                    "permission_denied",
                    message="调用者与任务指定 Agent 不一致，不能提交",
                    details={"actorId": context.actor_id, "taskAgentId": fresh_task["agent_id"]},
                )
            if fresh_task["current_attempt_id"] != attempt_id:
                raise ContractError(
                    "invalid_transition",
                    message="不是当前执行轮次，不能提交",
                    details={"attemptId": attempt_id,
                             "currentAttemptId": fresh_task["current_attempt_id"]},
                )
            if fresh_attempt["terminal_at"] is not None:
                raise ContractError(
                    "invalid_transition",
                    message="执行轮次已结束，不能重复提交",
                    details={"attemptId": attempt_id,
                             "terminalKind": fresh_attempt["terminal_kind"]},
                )
            if fresh_task["agent_id"] in MEMBERS:
                permissions = json.loads(fresh_task["permissions_json"])
                try:
                    build_policy(fresh_task["agent_id"], fresh_task["workspace"], permissions,
                                 allowed_roots=self.allowed_roots)
                except ValueError as exc:
                    raise ContractError("permission_denied", message=str(exc)) from exc
            claimed = self.store.begin_submission(attempt_id)
            if not claimed:
                state = fresh_attempt["submit_state"]
                if state == "submitted" and fresh_attempt["submission_json"]:
                    stored = json.loads(fresh_attempt["submission_json"])
                    return {
                        "taskId": task_id,
                        "attemptId": attempt_id,
                        "submittedAt": fresh_attempt["submitted_at"],
                        "submission": stored,
                        "replayed": True,
                    }
                code = "submit_unknown" if state in ("unknown", None) else "submit_in_progress"
                raise ContractError(
                    code,
                    message="该执行轮次已提交或提交结果未知，不重复调用宿主",
                    details={"attemptId": attempt_id, "submitState": state},
                )
            task, attempt = fresh_task, fresh_attempt

        # 2) 事务外调用外部宿主（绝不在长数据库事务内等待外部调用）
        try:
            result = self.adapter.submit(task, attempt)
        except ContractError as exc:
            self._record_unknown(attempt_id, {"errorCode": exc.code, "message": str(exc)})
            raise
        except Exception as exc:  # noqa: BLE001 - 外部调用异常一律记为未知，不自动重试
            self._record_unknown(attempt_id, {"error": type(exc).__name__, "message": str(exc)})
            raise ContractError(
                "submit_unknown",
                message="提交过程中异常，结果未知，不自动重试",
                details={"attemptId": attempt_id, "error": type(exc).__name__},
            )

        # 3) 核对宿主返回：来源一致 + 接收证据可信
        at = now_iso()
        try:
            self._validate_adapter_result(task, result)
        except ContractError:
            self._record_unknown(attempt_id, result.to_dict())
            raise

        accepted = result.status == "accepted_by_host"
        stale_error = None
        with self.store.transaction():
            fresh_task = self.store.get_task(task_id)
            fresh_attempt = self.store.get_attempt(attempt_id)
            current = (
                fresh_task is not None and fresh_attempt is not None
                and fresh_attempt["task_id"] == task_id
                and fresh_task["current_attempt_id"] == attempt_id
                and fresh_task["status"] == "active"
                and fresh_attempt["terminal_at"] is None
                and fresh_attempt["terminal_kind"] is None
                and fresh_attempt["submit_state"] == "submitting"
                and fresh_attempt["submitted_at"] is None
                and fresh_attempt["accepted_by_host_at"] is None
            )
            if not current:
                detail = {
                    "attemptId": attempt_id,
                    "currentAttemptId": fresh_task["current_attempt_id"] if fresh_task else None,
                    "taskStatus": fresh_task["status"] if fresh_task else None,
                    "submitState": fresh_attempt["submit_state"] if fresh_attempt else None,
                    "terminalKind": fresh_attempt["terminal_kind"] if fresh_attempt else None,
                }
                if fresh_attempt and fresh_attempt["submit_state"] == "submitting":
                    self.store.set_submission(
                        attempt_id, "unknown",
                        _dump({"reason": "stale_submission", "result": result.to_dict(), **detail}),
                    )
                self.store.log_activity(
                    task_id, attempt_id, "attempt.submit_stale",
                    {**detail, "submission": result.to_dict(), "synthetic": context.synthetic}, at,
                )
                stale_error = ContractError(
                    "submit_unknown",
                    message="宿主调用已返回，但执行轮次或任务状态已变化，需人工对账，不自动重试",
                    details=detail,
                )
            else:
                self.store.set_submission(
                    attempt_id, "submitted", _dump(result.to_dict()),
                    submitted_at=at,
                    accepted_by_host_at=at if accepted else None,
                )
                self.store.update_task(task_id, updated_at=at)
                self.store.log_activity(
                    task_id, attempt_id, "attempt.submitted",
                    {"status": result.status, "sourceId": result.source_id,
                     "hostReceipt": result.host_receipt, "note": result.note,
                     "evidenceLevel": result.evidence_level,
                     "by": context.actor_id, "synthetic": context.synthetic},
                    at,
                )
        if stale_error is not None:
            raise stale_error
        return {
            "taskId": task_id,
            "attemptId": attempt_id,
            "submittedAt": at,
            "replayed": False,
            "submission": result.to_dict(),
        }

    def _validate_adapter_result(self, task, result):
        if result.source_id != task["source_id"]:
            raise ContractError(
                "permission_denied",
                message="适配器返回的来源与任务指定来源不一致",
                details={"adapterSource": result.source_id, "taskSource": task["source_id"]},
            )
        if result.status not in ("submitted", "accepted_by_host"):
            raise ContractError(
                "invalid_transition",
                message="适配器返回未知 status：%r" % (result.status,),
                details={"status": result.status},
            )
        if result.status == "accepted_by_host":
            if not (isinstance(result.host_receipt, str) and result.host_receipt.strip()):
                raise ContractError(
                    "host_unconfirmed",
                    message="适配器声称已接收但没有回执号，不予采信",
                    details={"attemptId": None},
                )
            if result.evidence_level not in _TRUSTED_EVIDENCE:
                raise ContractError(
                    "host_unconfirmed",
                    message="适配器证据等级 %r 不足，不能记为已接收" % (result.evidence_level,),
                    details={"evidenceLevel": result.evidence_level},
                )

    def _record_unknown(self, attempt_id, detail):
        with self.store.transaction():
            self.store.set_submission(attempt_id, "unknown", _dump(detail))
