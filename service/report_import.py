"""D4-NEXT-01C：正式报告的受控导入与摘要记账。

职责边界（对应任务卡 D4-NEXT-01C §本卡范围 1/2）：
- 先复用 D4-NEXT-01B 的 `inspect_document(..., kind="report")` 与它内部的
  `validate_report_document()`；本模块不重新解析 YAML、不从正文猜状态。
- 报告只能**绑定到已存在的当前交付**；不凭报告创建任务、执行轮次、交付或审核结论。
  交付是否产生仍只由受信 `task.delivered` 事件决定。
- 导入只写 `reports` 表和 `activity`；不改 `tasks` / `attempts` / `deliveries` / `reviews`。
- 幂等：同 `reportId + reportRevision + canonicalDigest` 重放首次结果；
  同 `(reportId, reportRevision)` 内容不同则 `event_conflict` 并保留冲突事实。
- 旧交付（`deliveryId` 不是当前交付）沿用 `review_version_mismatch` 语义。
- 失败一律 fail closed：不写摘要、不改任务状态、不新增 processing 轮次。
"""
from __future__ import annotations

import json

from contracts.document_io import inspect_document, resolve_agent_identity
from contracts.schemas import ContractError, is_strict_card_revision

from .context import REVIEW_ROLES, REVIEW_SUBMIT, require_context
from .tasks import now_iso

# 报告的报告身份三块：requestedBy（谁请求）/ executedBy（谁实际写）/ reviewedBy（谁复核）
_IDENTITY_BLOCKS = ("requestedBy", "executedBy", "reviewedBy")


def _loads(text, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# 绑定校验：报告声明 vs 当前受控事实
# ---------------------------------------------------------------------------

def _check_binding(store, task_id, attempt_id, delivery_id, canonical, cli_source=None):
    """校验报告与任务/轮次/当前交付的绑定；返回 (task, attempt, delivery)。

    任何不一致都 fail closed（抛 ContractError），调用方不得继续写入。
    """
    task = store.get_task(task_id)
    if task is None:
        raise ContractError("invalid_task", message="报告引用的任务不存在，拒绝绑定",
                            details={"taskId": task_id})
    if cli_source and task["source_id"] != cli_source:
        raise ContractError(
            "permission_denied",
            message="任务来源与受控入口不一致，拒绝绑定正式报告",
            details={"taskSourceId": task["source_id"], "cliSourceId": cli_source},
        )
    if canonical.get("projectId") != task["project_id"]:
        raise ContractError(
            "report_binding_mismatch",
            message="报告的项目与绑定任务不一致，拒绝写入摘要",
            details={"reportProjectId": canonical.get("projectId"),
                     "taskProjectId": task["project_id"]},
        )
    if canonical.get("taskId") != task_id:
        raise ContractError(
            "report_binding_mismatch",
            message="报告声明的任务与受控入口指定的任务不一致",
            details={"reportTaskId": canonical.get("taskId"), "cliTaskId": task_id},
        )
    if canonical.get("attemptId") != attempt_id:
        raise ContractError(
            "report_binding_mismatch",
            message="报告声明的执行轮次与受控入口指定的轮次不一致",
            details={"reportAttemptId": canonical.get("attemptId"), "cliAttemptId": attempt_id},
        )
    attempt = store.get_attempt(attempt_id)
    if attempt is None or attempt["task_id"] != task_id:
        raise ContractError(
            "report_binding_mismatch",
            message="报告引用的执行轮次不存在或不属于该任务",
            details={"attemptId": attempt_id, "taskId": task_id},
        )
    if task["current_attempt_id"] != attempt_id:
        raise ContractError(
            "report_binding_mismatch",
            message="报告引用的是历史执行轮次，不能绑定当前任务",
            details={"attemptId": attempt_id,
                     "currentAttemptId": task["current_attempt_id"]},
        )
    delivery = store.get_delivery(delivery_id)
    if delivery is None or delivery["task_id"] != task_id:
        raise ContractError(
            "review_version_mismatch",
            message="当前交付不存在或不属于该任务，报告无法绑定",
            details={"deliveryId": delivery_id, "taskId": task_id},
        )
    if task["current_delivery_id"] != delivery_id:
        raise ContractError(
            "review_version_mismatch",
            message="报告引用的是旧交付，请重新指向当前交付",
            details={"deliveryId": delivery_id,
                     "currentDeliveryId": task["current_delivery_id"]},
        )
    if delivery["attempt_id"] != attempt_id:
        raise ContractError(
            "report_binding_mismatch",
            message="当前交付不属于指定执行轮次，拒绝绑定",
            details={"deliveryId": delivery_id, "deliveryAttemptId": delivery["attempt_id"],
                     "cliAttemptId": attempt_id},
        )
    return task, attempt, delivery


def _check_source_card(store, task_id, task, canonical, source_card_id, source_card_revision):
    """来源任务卡硬核对（D4-NEXT-01C-R1，R2 补完整性）。

    以库内冻结的受控来源登记为权威，报告声明必须与之相等；CLI 显式传入的
    期望值只是**成对的额外一致性断言**，不能建立缺失的来源，也不能绕过库核对。

    - 库内无登记来源 -> `source_card_mismatch`（不接受临时补绑）
    - 报告缺 `sourceCard.path/cardId/cardRevision` 或畸形（含字符串/布尔/0/负数冒充版本）
      -> `source_card_mismatch`
    - R2-C：事务内复读登记记录的 `task_id`/`project_id`，必须与当前任务一致，
      防止登记被错绑到其它任务/项目后再被当成权威。
    - 库记录与报告声明不一致 -> `source_card_mismatch`
    - 显式传入期望值时还必须同时匹配库与报告，否则 `source_card_mismatch`

    成功返回 True（正式导入成功必须 sourceCardVerified=true）。
    """
    card = canonical.get("sourceCard")
    if not isinstance(card, dict):
        raise ContractError(
            "source_card_mismatch",
            message="报告缺少来源任务卡声明，拒绝正式导入",
            details={"taskId": task_id},
        )
    report_card_id = card.get("cardId")
    report_revision = card.get("cardRevision")
    if not isinstance(report_card_id, str) or not report_card_id.strip():
        raise ContractError(
            "source_card_mismatch",
            message="报告来源任务卡 cardId 缺失或类型非法，拒绝正式导入",
            details={"cardId": report_card_id},
        )
    if not is_strict_card_revision(report_revision):
        raise ContractError(
            "source_card_mismatch",
            message="报告来源任务卡 cardRevision 必须是 >=1 的整数，拒绝字符串/布尔/0/负数",
            details={"cardRevision": report_revision, "type": type(report_revision).__name__},
        )
    if (source_card_id is None) != (source_card_revision is None):
        raise ContractError(
            "source_card_mismatch",
            message="来源任务卡的 cardId 与 cardRevision 必须成对给出才能核对",
            details={"cardId": source_card_id, "cardRevision": source_card_revision},
        )
    if source_card_id is not None and not is_strict_card_revision(source_card_revision):
        raise ContractError(
            "source_card_mismatch",
            message="受控入口声明的来源版本必须是 >=1 的整数",
            details={"cardRevision": source_card_revision,
                     "type": type(source_card_revision).__name__},
        )

    record = store.get_source_card(task_id)
    if record is None:
        raise ContractError(
            "source_card_mismatch",
            message="该任务没有登记受控来源任务卡，拒绝正式导入（不接受临时补绑）",
            details={"taskId": task_id},
        )
    # R2-C：事务内复读登记记录，核对 task_id 与 project_id 与当前任务一致。
    if record["task_id"] != task_id or record["project_id"] != task["project_id"]:
        raise ContractError(
            "source_card_mismatch",
            message="库内来源登记与当前任务/项目不一致，拒绝正式导入",
            details={"registeredTaskId": record["task_id"], "currentTaskId": task_id,
                     "registeredProjectId": record["project_id"],
                     "currentProjectId": task["project_id"]},
        )
    if record["card_id"] != report_card_id or int(record["card_revision"]) != report_revision:
        raise ContractError(
            "source_card_mismatch",
            message="报告来源任务卡与库内登记来源不一致，拒绝写入摘要",
            details={"registered": {"cardId": record["card_id"],
                                    "cardRevision": int(record["card_revision"])},
                     "declared": {"cardId": report_card_id, "cardRevision": report_revision}},
        )
    if source_card_id is not None:
        if (source_card_id != report_card_id
                or source_card_revision != report_revision):
            raise ContractError(
                "source_card_mismatch",
                message="受控入口声明的来源任务卡与报告/库登记不一致，拒绝绑定",
                details={"controlled": {"cardId": source_card_id,
                                        "cardRevision": source_card_revision},
                         "declared": {"cardId": report_card_id,
                                      "cardRevision": report_revision},
                         "registered": {"cardId": record["card_id"],
                                        "cardRevision": int(record["card_revision"])}},
            )
    return True


# ---------------------------------------------------------------------------
# 记录与结果构造
# ---------------------------------------------------------------------------

def _build_record(document, task_id, attempt_id, delivery_id, context, at,
                  source_card_verified):
    canonical = document["canonical"]
    source_file = document["sourceFile"]
    return {
        "report_id": canonical.get("reportId"),
        "report_revision": canonical.get("reportRevision"),
        "report_type": canonical.get("reportType"),
        "project_id": canonical.get("projectId"),
        "task_id": task_id,
        "attempt_id": attempt_id,
        "delivery_id": delivery_id,
        "source_card_json": json.dumps(canonical.get("sourceCard") or {},
                                       ensure_ascii=False, sort_keys=True),
        "requested_by_json": json.dumps(canonical.get("requestedBy") or {},
                                        ensure_ascii=False, sort_keys=True),
        "executed_by_json": json.dumps(canonical.get("executedBy") or {},
                                       ensure_ascii=False, sort_keys=True),
        "reviewed_by_json": json.dumps(canonical.get("reviewedBy") or {},
                                       ensure_ascii=False, sort_keys=True),
        "adapter": canonical.get("adapter"),
        "session_id": canonical.get("sessionId"),
        "host_run_id": canonical.get("hostRunId"),
        "board_summary_json": json.dumps(canonical.get("boardSummary") or {},
                                         ensure_ascii=False, sort_keys=True),
        "business_summary_json": json.dumps(canonical.get("businessSummary") or {},
                                            ensure_ascii=False, sort_keys=True),
        "evidence_json": json.dumps(canonical.get("evidence") or [],
                                    ensure_ascii=False, sort_keys=True),
        "canonical_digest": document["canonicalDigest"],
        "source_file_json": json.dumps(
            {"sha256": source_file.get("sha256"), "bytes": source_file.get("bytes"),
             "sourceCardVerified": bool(source_card_verified)},
            ensure_ascii=False, sort_keys=True,
        ),
        "imported_at": at,
        "imported_by_json": json.dumps(context.to_dict(), ensure_ascii=False, sort_keys=True),
        "synthetic": 1 if context.synthetic else 0,
    }


def _result_from_record(record, replayed):
    """从已持久化的报告记录重建导入结果（重放必须与首次结果一致）。"""
    source_file = _loads(record.get("source_file_json"), {}) or {}
    return {
        "reportId": record["report_id"],
        "reportRevision": record["report_revision"],
        "reportType": record["report_type"],
        "projectId": record["project_id"],
        "taskId": record["task_id"],
        "attemptId": record["attempt_id"],
        "deliveryId": record["delivery_id"],
        "canonicalDigest": record["canonical_digest"],
        "importedAt": record["imported_at"],
        "replayed": bool(replayed),
        "synthetic": bool(record["synthetic"]),
        "sourceCard": _loads(record.get("source_card_json"), {}) or {},
        "sideEffects": {
            "taskCreated": False,
            "attemptCreated": False,
            "deliveryCreated": False,
            "reviewCreated": False,
            "taskStatusChanged": False,
        },
        "sourceFile": {
            "sha256": source_file.get("sha256"),
            "bytes": source_file.get("bytes"),
            "sourceCardVerified": source_file.get("sourceCardVerified", False),
        },
        "boardSummary": _loads(record.get("board_summary_json"), {}) or {},
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def import_report(store, task_id, attempt_id, delivery_id, file_path, context=None,
                  allowed_roots=None, cli_agent=None, cli_source=None,
                  source_card_id=None, source_card_revision=None):
    """导入一份正式报告并绑定到当前交付。

    返回 dict；失败抛 ContractError / DocumentContractError，调用方必须 fail closed。
    """
    require_context(context, REVIEW_SUBMIT)
    if context.role not in REVIEW_ROLES:
        raise ContractError(
            "permission_denied",
            message="角色 %s 不能导入正式报告" % (context.role,),
            details={"actorId": context.actor_id, "role": context.role,
                     "allowedRoles": list(REVIEW_ROLES)},
        )

    # 1) 01B 整体校验（含 schema 绑定、身份、路径合同）；失败即 DocumentContractError，
    #    不写库、不建任务。
    document = inspect_document(
        file_path, kind="report", allowed_roots=allowed_roots,
        cli_agent=cli_agent, cli_source=cli_source,
    )
    canonical = document["canonical"]

    # 2) 受控身份：别名与稳定成员标识不一致且无受控映射 -> identity_conflict
    for label in _IDENTITY_BLOCKS:
        block = canonical.get(label)
        if isinstance(block, dict):
            resolve_agent_identity(block.get("alias"), block.get("agentId"), label)

    source_card_verified = False
    report_id = canonical.get("reportId")
    report_revision = canonical.get("reportRevision")
    digest = document["canonicalDigest"]
    at = now_iso()

    # 3) 写入事务：事务内复读最新事实，避免用锁前旧快照绑定已被替换的交付。
    #    来源核对也在事务内完成，防止并发登记/交付变更后仍按锁前快照写入。
    #    冲突分支在事务内先落冲突记录，提交后再抛错，保证冲突事实不因回滚丢失。
    with store.transaction():
        task, _attempt, _delivery = _check_binding(
            store, task_id, attempt_id, delivery_id, canonical, cli_source)
        source_card_verified = _check_source_card(
            store, task_id, task, canonical, source_card_id, source_card_revision
        )
        existing = store.get_report(report_id, report_revision)
        conflict = record = None
        if existing is not None:
            if existing["canonical_digest"] == digest:
                return _result_from_record(existing, replayed=True)
            conflict = {
                "reportId": report_id,
                "reportRevision": report_revision,
                "existingDigest": existing["canonical_digest"],
                "incomingDigest": digest,
                "deliveryId": delivery_id,
                "reason": "同一报告同一版本内容不同",
            }
            store.insert_conflict(report_id, task_id, conflict, at)
            store.log_activity(task_id, attempt_id, "report.import_conflict", conflict, at)
        else:
            record = _build_record(document, task_id, attempt_id, delivery_id, context, at,
                                   source_card_verified)
            store.insert_report(record)
            store.log_activity(
                task_id, attempt_id, "report.imported",
                {
                    "reportId": report_id,
                    "reportRevision": report_revision,
                    "deliveryId": delivery_id,
                    "canonicalDigest": digest,
                    "synthetic": bool(context.synthetic),
                    "sourceCardVerified": bool(source_card_verified),
                },
                at,
            )
    if conflict is not None:
        raise ContractError(
            "event_conflict",
            message="同一报告同一版本内容不同，已拒绝并保留冲突记录",
            details=conflict,
        )
    return _result_from_record(record, replayed=False)
