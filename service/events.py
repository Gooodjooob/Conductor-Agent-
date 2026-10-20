"""事件接收服务：来源绑定、eventId 去重、序号检查、迟到结果对账、终态保护。

对齐接口文档 §5.3 与 §6，以及 R1 返工卡“必须修正 #1/#2/#3/#5”与
R2 返工卡“必须修正 #1/#3/#4/#5”：
- 调用者身份/来源由受控入口注入；事件的 sourceId 必须等于调用者、等于 task.source_id。
- 相同 eventId + 相同内容 -> 返回**第一次的接收结果**（包括拒绝结果，如 stale_sequence）。
- 相同 eventId + 不同内容 -> 拒绝并记录冲突（event_conflict）。
- 旧序号 / 迟到结果 / 终态后的事件 -> 只入历史，不回退当前状态。
- 关键检查与写入在同一写事务内**复读**最新事实，避免用锁前旧快照覆盖途中生效的接收/结束事实。
- 未提交时不产生接收事实：提前到达的确认只留待对账证据，提交落库后才能生效。
- 已绑定 task/attempt 的事件若被业务拒绝，其首次拒绝结果**持久化**；同内容重发复现同一拒绝码。
- 接收确认使用独立里程碑序号，不被进度类序号永久压制。
"""
from __future__ import annotations

import hashlib
import json

from contracts.paths import assert_within_scope, resolve_within
from contracts.schemas import ContractError, validate_event

from .context import EVENT_INGEST, require_context
from .tasks import now_iso

_STATE_CHANGING = (
    "task.accepted", "task.started", "task.progress", "task.blocked", "task.delivered",
)


def _canonical(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(payload):
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


def _nonblank_str(value):
    """非空、去空白后仍非空的字符串才视为有效证据。"""
    return isinstance(value, str) and bool(value.strip())


class EventService:
    def __init__(self, store):
        self.store = store

    # -- 主入口 -----------------------------------------------------------
    def ingest(self, payload, context=None):
        require_context(context, EVENT_INGEST)

        errors = validate_event(payload)
        if errors:
            raise ContractError("invalid_event", details=errors)

        # 调用者来源与事件来源必须一致（不信 payload 自报来源）
        if context.actor_id != payload["sourceId"]:
            raise ContractError(
                "permission_denied",
                message="调用者来源与事件 sourceId 不一致",
                details={"actorId": context.actor_id, "sourceId": payload["sourceId"]},
            )
        if self.store.get_source(payload["sourceId"]) is None:
            raise ContractError("unknown_source", details={"sourceId": payload["sourceId"]})

        # 绑定/授权预读（此时不持写锁）：未知身份/来源与未绑定拼盘不占 eventId。
        self._precheck_binding(payload)

        synthetic = 1 if context.synthetic else 0
        # 关键检查 + 写入放进**同一个写事务**，事务内复读最新事实，避免锁前旧快照覆盖新事实。
        with self.store.transaction():
            kind, value = self._ingest_locked(payload, context, synthetic)
        if kind == "error":
            raise value
        return value

    def _precheck_binding(self, payload):
        """绑定/授权预读：失败即拒绝且不写任何历史（不为未知身份伪造输入占 eventId）。"""
        task = self.store.get_task(payload["taskId"])
        if task is None:
            raise ContractError(
                "invalid_event",
                message="事件引用的任务不存在",
                details={"taskId": payload["taskId"]},
            )
        if payload["projectId"] != task["project_id"]:
            raise ContractError(
                "invalid_event",
                message="事件的 projectId 与任务不一致",
                details={"eventProjectId": payload["projectId"],
                         "taskProjectId": task["project_id"]},
            )
        if payload["agentId"] != task["agent_id"]:
            raise ContractError(
                "permission_denied",
                message="上报 Agent 与任务指定 Agent 不一致",
                details={"eventAgentId": payload["agentId"], "taskAgentId": task["agent_id"]},
            )
        if payload["sourceId"] != task["source_id"]:
            raise ContractError(
                "permission_denied",
                message="事件来源与任务指定来源不一致",
                details={"eventSourceId": payload["sourceId"], "taskSourceId": task["source_id"]},
            )
        attempt = self.store.get_attempt(payload["attemptId"])
        if attempt is None or attempt["task_id"] != task["task_id"]:
            raise ContractError(
                "invalid_event",
                message="事件引用的执行轮次不存在或不属于该任务",
                details={"attemptId": payload["attemptId"]},
            )

    def _ingest_locked(self, payload, context, synthetic):
        """在写事务内完成：复读事实 -> 去重/序号/终态判定 -> 应用或记账。

        返回 (\"ok\", result) 或 (\"error\", ContractError)。业务拒绝会**先在事务内落库**，
        再于提交后抛错，使首次拒绝结果可被同内容重发复现。
        """
        task = self.store.get_task(payload["taskId"])
        if task is None:
            raise ContractError(
                "invalid_event",
                message="事件引用的任务不存在",
                details={"taskId": payload["taskId"]},
            )
        if payload["projectId"] != task["project_id"]:
            raise ContractError(
                "invalid_event",
                message="事件的 projectId 与任务不一致",
                details={"eventProjectId": payload["projectId"],
                         "taskProjectId": task["project_id"]},
            )
        if payload["agentId"] != task["agent_id"]:
            raise ContractError(
                "permission_denied",
                message="上报 Agent 与任务指定 Agent 不一致",
                details={"eventAgentId": payload["agentId"], "taskAgentId": task["agent_id"]},
            )
        if payload["sourceId"] != task["source_id"]:
            raise ContractError(
                "permission_denied",
                message="事件来源与任务指定来源不一致",
                details={"eventSourceId": payload["sourceId"], "taskSourceId": task["source_id"]},
            )
        attempt = self.store.get_attempt(payload["attemptId"])
        if attempt is None or attempt["task_id"] != task["task_id"]:
            raise ContractError(
                "invalid_event",
                message="事件引用的执行轮次不存在或不属于该任务",
                details={"attemptId": payload["attemptId"]},
            )

        content_hash = _hash(payload)

        # 1) eventId 去重：相同内容返回首次接收结果（含拒绝结果），不同内容记冲突
        existing = self.store.get_event(payload["eventId"])
        if existing is not None:
            if existing["content_hash"] == content_hash:
                return self._replay(existing)
            self.store.insert_conflict(
                payload["eventId"], task["task_id"],
                {"existingHash": existing["content_hash"], "incomingHash": content_hash,
                 "reason": "同一 eventId 内容不同"},
                now_iso(),
            )
            return ("error", ContractError(
                "event_conflict",
                message="同一 eventId 内容不同，已拒绝并保留冲突记录",
                details={"eventId": payload["eventId"]},
            ))

        # 2) 序号检查。接收确认（task.accepted）用**独立里程碑序号**，
        #    避免先到的进度类序号（如 seq=2）永久压制合法的接收确认（seq=1）。
        sequence = payload["sequence"]
        if sequence < 1:
            return ("error", ContractError(
                "invalid_event", message="sequence 必须 >= 1", details={"sequence": sequence}
            ))
        if payload["type"] == "task.accepted":
            last = self.store.max_sequence(
                task["task_id"], payload["attemptId"], types=("task.accepted",)
            )
        else:
            last = self.store.max_sequence(task["task_id"], payload["attemptId"])
        if sequence <= last:
            return self._stale(
                payload, content_hash, "旧序号，已进入对账历史，不回退当前状态",
                {"lastSequence": last},
            )

        # 3) 迟到结果：非当前执行轮次
        if payload["attemptId"] != task["current_attempt_id"]:
            return self._stale(
                payload, content_hash, "迟到结果：不属于当前执行轮次，已进入对账历史",
                {"currentAttemptId": task["current_attempt_id"]},
            )

        # 4) 终态保护：任务已通过或执行轮次已结束时，状态类事件只入历史（先落库再拒绝）
        if task["status"] == "approved" or attempt["terminal_kind"] is not None:
            if payload["type"] in _STATE_CHANGING:
                return self._reject(
                    payload, content_hash, "invalid_transition",
                    "任务/执行轮次已结束，该事件只入历史，不回退状态",
                    {"taskStatus": task["status"], "terminalKind": attempt["terminal_kind"]},
                    synthetic,
                )

        # 5) 按类型应用
        return self._apply(payload, task, attempt, content_hash, context, synthetic)

    def _replay(self, existing):
        outcome = existing["outcome"]
        if outcome in ("applied", "unverified"):
            return ("ok", {
                "ok": True,
                "replayed": True,
                "eventId": existing["event_id"],
                "data": _loads(existing["result_json"], {}) or {},
            })
        code = existing["outcome_code"] or "stale_sequence"
        return ("error", ContractError(
            code,
            message=existing["reason"] or "该事件此前已被拒绝，保持原拒绝结果",
            details=_loads(existing["result_json"], {}) or {},
        ))

    # -- 应用 -------------------------------------------------------------
    def _apply(self, payload, task, attempt, content_hash, context, synthetic):
        event_type = payload["type"]
        at = now_iso()
        result = {"eventId": payload["eventId"], "type": event_type, "appliedAt": at}

        if event_type == "task.accepted":
            body = payload.get("payload") or {}
            evidence = body.get("hostReceipt")
            if not _nonblank_str(evidence):
                evidence = body.get("sourceNote")
            if not _nonblank_str(evidence):
                # 无接收证据（含空白字符串/非法类型）：业务拒绝，先落库再拒绝
                return self._reject(
                    payload, content_hash, "host_unconfirmed",
                    "task.accepted 缺少宿主确认号或来源说明（须为非空字符串）",
                    {"reason": "缺少宿主确认号或来源说明"}, synthetic,
                )
            if attempt["submitted_at"] is None:
                # 未提交/提交中提前到达的确认：只留待对账证据，不生效、不占 eventId；
                # 待提交落库后，同一事件可再次对账而成为接收事实。
                self.store.insert_reconciliation(
                    task["task_id"], payload["eventId"], payload["attemptId"],
                    payload["sequence"], "unverified_accept",
                    {"reason": "尚未提交，接收确认待提交后对账"}, at,
                )
                self.store.log_activity(
                    task["task_id"], payload["attemptId"], "event.unconfirmed_accept",
                    {"evidence": evidence, "synthetic": bool(synthetic)}, at,
                )
                result["pending"] = True
                return ("ok", {"ok": True, "replayed": False, "data": result})
            if attempt["accepted_by_host_at"] is None:
                self.store.update_attempt(payload["attemptId"], accepted_by_host_at=at)
            self._record_event(payload, content_hash, "applied", "applied", None, result, at,
                               synthetic)
            self.store.log_activity(
                task["task_id"], payload["attemptId"], "event.task.accepted",
                {"evidence": evidence, "synthetic": bool(synthetic)}, at,
            )
            return ("ok", {"ok": True, "replayed": False, "data": result})

        if event_type == "task.started":
            if attempt["accepted_by_host_at"] is None:
                # 无接收确认：只留“未核实历史”，不推进生效序号、不进入执行中
                result["unverified"] = True
                self._record_event(payload, content_hash, "unverified", "unverified_start",
                                   "缺少接收确认的 started 上报", result, at, synthetic)
                self.store.insert_reconciliation(
                    task["task_id"], payload["eventId"], payload["attemptId"],
                    payload["sequence"], "unverified_start",
                    {"reason": "缺少接收确认，未推进状态"}, at,
                )
                return ("ok", {"ok": True, "replayed": False, "data": result})
            if attempt["started_at"] is None:
                self.store.update_attempt(payload["attemptId"], started_at=at, state="started")
            result["attemptState"] = "started"
            self._record_event(payload, content_hash, "applied", "applied", None, result, at,
                               synthetic)
            self.store.log_activity(
                task["task_id"], payload["attemptId"], "event.task.started",
                {"note": (payload.get("payload") or {}).get("note")}, at,
            )
            return ("ok", {"ok": True, "replayed": False, "data": result})

        if event_type == "task.progress":
            step = (payload.get("payload") or {}).get("step")
            note = (payload.get("payload") or {}).get("note")
            # 未确认阶段的进度只作为执行历史保留，并显式标记“接收确认待对账”；
            # 它不成为接收里程碑，但也不会阻止后续合法的接收确认（见序号独立里程碑）。
            acceptance_pending = attempt["accepted_by_host_at"] is None
            if acceptance_pending:
                result["acceptancePending"] = True
            self._record_event(payload, content_hash, "applied", "applied", None, result, at,
                               synthetic)
            self.store.log_activity(
                task["task_id"], payload["attemptId"], "event.task.progress",
                {"step": step, "note": note, "acceptancePending": acceptance_pending}, at,
            )
            return ("ok", {"ok": True, "replayed": False, "data": result})

        if event_type == "task.blocked":
            body = payload.get("payload") or {}
            blocked = {"reason": body.get("reason"), "owner": body.get("owner"),
                       "resumeWhen": body.get("resumeWhen")}
            self.store.update_attempt(
                payload["attemptId"], state="blocked",
                blocked_json=json.dumps(blocked, ensure_ascii=False, sort_keys=True),
            )
            self.store.update_task(task["task_id"], status="blocked", updated_at=at)
            self._record_event(payload, content_hash, "applied", "applied", None, result, at,
                               synthetic)
            self.store.log_activity(
                task["task_id"], payload["attemptId"], "event.task.blocked", blocked, at,
            )
            result["taskStatus"] = "blocked"
            return ("ok", {"ok": True, "replayed": False, "data": result})

        if event_type == "task.delivered":
            if attempt["submitted_at"] is None or attempt["accepted_by_host_at"] is None:
                # 交付前必须已提交且已获接收确认；否则业务拒绝，先落库再拒绝
                return self._reject(
                    payload, content_hash, "invalid_transition",
                    "未提交或未获接收确认，不能交付",
                    {"attemptId": payload["attemptId"],
                     "submitted": attempt["submitted_at"] is not None,
                     "accepted": attempt["accepted_by_host_at"] is not None},
                    synthetic,
                )
            body = payload.get("payload") or {}
            try:
                self._validate_delivery_paths(task, body)
            except ContractError as exc:
                return self._reject(
                    payload, content_hash, exc.code, str(exc),
                    {"deliveryPath": True, **(exc.details or {})}, synthetic,
                )
            delivery_id = "del-%s-v%d" % (
                task["task_id"], self.store.count_deliveries(task["task_id"]) + 1
            )
            result["deliveryId"] = delivery_id
            result["taskStatus"] = "awaiting_review"
            record = {
                "delivery_id": delivery_id,
                "task_id": task["task_id"],
                "attempt_id": payload["attemptId"],
                "summary": body.get("summary"),
                "changed_files_json": json.dumps(body.get("changedFiles") or [], ensure_ascii=False),
                "verification_json": json.dumps(body.get("verification") or [], ensure_ascii=False),
                "risks_json": json.dumps(body.get("risks") or [], ensure_ascii=False),
                "report_path": body.get("reportPath"),
                "unfinished_json": json.dumps(body.get("unfinished") or [], ensure_ascii=False),
                "source_delivery_id": body.get("sourceDeliveryId"),
                "created_at": at,
            }
            self.store.insert_delivery(record)
            self.store.update_attempt(
                payload["attemptId"], terminal_at=at, terminal_kind="delivered", state="delivered"
            )
            self.store.update_task(
                task["task_id"], status="awaiting_review",
                current_delivery_id=delivery_id, updated_at=at,
            )
            self._record_event(payload, content_hash, "applied", "applied", None, result, at,
                               synthetic)
            self.store.log_activity(
                task["task_id"], payload["attemptId"], "event.task.delivered",
                {"deliveryId": delivery_id, "reportPath": body.get("reportPath"),
                 "synthetic": bool(synthetic)}, at,
            )
            return ("ok", {"ok": True, "replayed": False, "data": result})

        if event_type == "source.heartbeat":
            self._record_event(payload, content_hash, "applied", "applied", None, result, at,
                               synthetic)
            self.store.log_activity(
                task["task_id"], payload["attemptId"], "event.source.heartbeat", {}, at,
            )
            return ("ok", {"ok": True, "replayed": False, "data": result})

        raise ContractError("invalid_event", details={"type": event_type})

    def _validate_delivery_paths(self, task, body):
        workspace = task["workspace"]
        permissions = _loads(task["permissions_json"], {}) or {}
        scopes = permissions.get("writeScope") or []
        report_path = body.get("reportPath")
        resolve_within(workspace, report_path)
        for entry in body.get("changedFiles") or []:
            assert_within_scope(workspace, scopes, entry)

    # -- 辅助 -------------------------------------------------------------
    def _record_event(self, payload, content_hash, outcome, outcome_code, reason, result, at,
                      synthetic=0):
        self.store.insert_event({
            "event_id": payload["eventId"],
            "task_id": payload["taskId"],
            "attempt_id": payload["attemptId"],
            "project_id": payload["projectId"],
            "source_id": payload["sourceId"],
            "agent_id": payload["agentId"],
            "sequence": payload["sequence"],
            "type": payload["type"],
            "occurred_at": payload.get("occurredAt"),
            "received_at": at,
            "content_hash": content_hash,
            "outcome": outcome,
            "outcome_code": outcome_code,
            "reason": reason,
            "result_json": json.dumps(result, ensure_ascii=False, sort_keys=True),
            "synthetic": synthetic,
        })

    def _stale(self, payload, content_hash, reason, extra):
        at = now_iso()
        result = {"eventId": payload["eventId"], "type": payload["type"],
                  "stale": True, "reason": reason}
        self._record_event(payload, content_hash, "stale", "stale_sequence", reason, result, at)
        self.store.insert_reconciliation(
            payload["taskId"], payload["eventId"], payload["attemptId"], payload["sequence"],
            "stale_sequence", dict(extra, reason=reason), at,
        )
        self.store.log_activity(
            payload["taskId"], payload["attemptId"], "event.stale_sequence", extra, at,
        )
        return ("error", ContractError("stale_sequence", message=reason,
                                       details=dict(extra, eventId=payload["eventId"])))

    def _reject(self, payload, content_hash, code, reason, extra, synthetic=0):
        """终态后/非法事件：写入历史后拒绝；重复同内容将复现同一拒绝码。"""
        at = now_iso()
        result = {"eventId": payload["eventId"], "type": payload["type"],
                  "rejected": True, "code": code, "reason": reason}
        self._record_event(payload, content_hash, "rejected", code, reason, result, at, synthetic)
        self.store.insert_reconciliation(
            payload["taskId"], payload["eventId"], payload["attemptId"], payload["sequence"],
            code, dict(extra, reason=reason), at,
        )
        self.store.log_activity(
            payload["taskId"], payload["attemptId"], "event.rejected", dict(extra, code=code), at,
        )
        return ("error", ContractError(code, message=reason,
                                       details=dict(extra, eventId=payload["eventId"])))
