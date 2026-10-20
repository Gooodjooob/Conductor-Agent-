"""cox 审核服务：通过 / 要求返工 / 阻塞 / 补信息。

对齐接口文档 §5.5 与 R1 返工卡“必须修正 #1/#2/#3/#8” 与 R2 返工卡“必须修正 #1”：
- 审核者身份必须来自受控入口注入的上下文；worker 与未知身份不能 approved，且记录 reviewer 身份。
- 只能审核“当前版本”交付；旧交付审核 -> review_version_mismatch（fail closed）。
- 未提交/未获接收确认的交付不能被通过（approved）。
- changes_requested 生成新的执行轮次，旧 deliveryId 仍可查询但不再 isCurrent，不能继续审核。
- approved 只改变审核和任务状态，closure 明确记录“未执行 QA / 用户确认 / Git / 发布”。
- 关键检查与写入在同一写事务内**复读**最新事实，避免并发下用旧快照覆盖新审核/终态。
"""
from __future__ import annotations

import json

from contracts.schemas import ContractError, validate_review_decision

from .context import REVIEW_ROLES, REVIEW_SUBMIT, require_context
from .tasks import new_id, now_iso

_FALSE_CLOSURE = {"qaExecuted": False, "userConfirmed": False, "git": False, "published": False}


class ReviewService:
    def __init__(self, store, task_service):
        self.store = store
        self.task_service = task_service

    def submit_review(self, task_id, payload, context=None):
        # 1) 受控身份：无上下文/非审核角色一律拒绝（不信 payload 自报）
        require_context(context, REVIEW_SUBMIT)
        if context.role not in REVIEW_ROLES:
            raise ContractError(
                "permission_denied",
                message="角色 %s 不能提交审核决定" % (context.role,),
                details={"actorId": context.actor_id, "role": context.role,
                         "allowedRoles": list(REVIEW_ROLES)},
            )

        errors = validate_review_decision(payload)
        if errors:
            raise ContractError("invalid_review", details=errors)

        delivery_id = payload["deliveryId"]
        # 预读（不持写锁）：早期 fail-closed；并发测试也在此挂接快照。
        self._check_reviewable(task_id, delivery_id)

        decision = payload["decision"]
        note = payload.get("note")
        at = now_iso()
        reviewer_id = context.actor_id

        # 2) 关键检查 + 写入放进同一个写事务，事务内复读最新事实再做权威判定。
        with self.store.transaction():
            task, delivery = self._check_reviewable(task_id, delivery_id)

            # 只有已提交且已获接收确认的交付才能通过或返工
            if decision in ("approved", "changes_requested"):
                attempt = self.store.get_attempt(delivery["attempt_id"])
                if (attempt is None or attempt["submitted_at"] is None
                        or attempt["accepted_by_host_at"] is None):
                    raise ContractError(
                        "invalid_transition",
                        message="交付缺少提交或接收确认证据，不能通过或返工",
                        details={"deliveryId": delivery_id,
                                 "attemptId": delivery["attempt_id"]},
                    )

            review_round = self.store.count_reviews(task_id) + 1
            review_id = new_id("rev")
            result = {
                "reviewId": review_id,
                "taskId": task_id,
                "deliveryId": delivery_id,
                "decision": decision,
                "reviewRound": review_round,
                "reviewerId": reviewer_id,
            }

            if decision == "approved":
                self.store.insert_review(self._record(
                    review_id, task_id, delivery_id, decision, note, review_round, None,
                    reviewer_id, at,
                ))
                self.store.update_task(
                    task_id, status="approved",
                    closure_json=json.dumps(_FALSE_CLOSURE, ensure_ascii=False, sort_keys=True),
                    updated_at=at,
                )
                self.store.log_activity(
                    task_id, task["current_attempt_id"], "review.approved",
                    {"note": note, "reviewerId": reviewer_id, "closure": _FALSE_CLOSURE}, at,
                )
                result["taskStatus"] = "approved"
                result["closure"] = dict(_FALSE_CLOSURE)
                return result

            if decision == "changes_requested":
                self.store.update_task(task_id, status="rework_requested", updated_at=at)
                new_attempt = self.task_service.create_attempt(
                    task_id, note="返工说明：%s" % (note,), context=context
                )
                self.store.insert_review(self._record(
                    review_id, task_id, delivery_id, decision, note, review_round,
                    new_attempt["attemptId"], reviewer_id, at,
                ))
                self.store.log_activity(
                    task_id, new_attempt["attemptId"], "review.changes_requested",
                    {"note": note, "previousDeliveryId": delivery_id,
                     "reviewerId": reviewer_id}, at,
                )
                result["taskStatus"] = "active"
                result["resultingAttemptId"] = new_attempt["attemptId"]
                return result

            if decision == "blocked":
                self.store.insert_review(self._record(
                    review_id, task_id, delivery_id, decision, note, review_round, None,
                    reviewer_id, at,
                ))
                self.store.update_task(task_id, status="blocked", updated_at=at)
                self.store.log_activity(
                    task_id, task["current_attempt_id"], "review.blocked",
                    {"note": note, "reviewerId": reviewer_id}, at,
                )
                result["taskStatus"] = "blocked"
                return result

            # request_info：只补信息，不开始新执行，任务留在待审核（可恢复）
            self.store.insert_review(self._record(
                review_id, task_id, delivery_id, decision, note, review_round, None,
                reviewer_id, at,
            ))
            self.store.log_activity(
                task_id, task["current_attempt_id"], "review.request_info",
                {"note": note, "reviewerId": reviewer_id}, at,
            )
            result["taskStatus"] = task["status"]
            return result

    def _check_reviewable(self, task_id, delivery_id):
        """复读任务与交付，校验当前版本/状态；返回 (task, delivery)。"""
        task = self.store.get_task(task_id)
        if task is None:
            raise ContractError(
                "invalid_review", message="任务不存在：%s" % task_id, details={"taskId": task_id}
            )
        delivery = self.store.get_delivery(delivery_id)
        if delivery is None or delivery["task_id"] != task_id:
            raise ContractError(
                "review_version_mismatch",
                message="交付不存在或不属于该任务",
                details={"deliveryId": delivery_id, "taskId": task_id},
            )
        if task["current_delivery_id"] != delivery_id:
            raise ContractError(
                "review_version_mismatch",
                message="审核的是旧交付，请重新打开当前任务详情",
                details={"deliveryId": delivery_id,
                         "currentDeliveryId": task["current_delivery_id"]},
            )
        if task["status"] != "awaiting_review":
            raise ContractError(
                "invalid_transition",
                message="当前任务状态 %s 不能审核" % (task["status"],),
                details={"status": task["status"], "allowed": ["awaiting_review"]},
            )
        return task, delivery

    @staticmethod
    def _record(review_id, task_id, delivery_id, decision, note, review_round,
                resulting_attempt_id, reviewer_id, at):
        return {
            "review_id": review_id,
            "task_id": task_id,
            "delivery_id": delivery_id,
            "decision": decision,
            "note": note,
            "review_round": review_round,
            "resulting_attempt_id": resulting_attempt_id,
            "reviewer_id": reviewer_id,
            "created_at": at,
        }
