"""planning-snapshot.v1 规划 revision 追加判定纯建议函数（第 027c 轮候选）。

冻结边界（唯一）：``inputs/cox-revision-decision.md``（内容状态：``planned``）；
依据接口 §15.5、§16.1–§16.4 与 §17，开发计划 §11 A。

严格范围：

* 只提供纯函数 :func:`decide_planning_revision`：输入「本来源既有快照行」与「一次受控
  提交」，返回 ``create_first`` / ``replayed`` / ``append_candidate`` 建议与建议的
  ``snapshotRevision``。
* 零文件 / 零数据库 / 零宿主 / 零环境访问；导入本模块无副作用；本模块**没有任何导入**。
* 不生成任何编号、时钟或 UUID：首次 / 追加建议都**不**产出 ``snapshotId``；只有重放才
  回显既有行**原值**的 ``snapshotId``。
* 不实际导入规划、不追加快照、不激活、不派发、不写业务数据、不调用迁移、不连接数据库，
  也不写执行事实（task / attempt / event / delivery / review / Registry）。
* 返回值只是**建议**，不得表述为数据库已保存 / 已激活 / 门禁通过 / 可派发。
* 不解析、不确认任何原文版本；声明与既有元数据一律由受控上层提供，本纯函数不能从声明
  证明来源版本确实存在。完整 MVP / A 阶段未完成，随后受信保存服务接线另卡。

判定顺序（先参数与深度结构校验，再业务判断）：

1. ``batch_drift=True`` -> ``planning_source_drift``，优先于重放与同版本冲突，不给出任何
   revision 建议。
2. 任一既有同 ``documentVersion`` 行的 ``sourceSha256`` / ``sourceBytes`` 与提交不同
   -> ``planning_snapshot_conflict`` / ``reason=same_document_version_different_bytes``；
   ``new_content_reason`` 不能豁免，也不能因为先找到同 digest 重放就忽略另一同版本冲突行。
3. 同 ``canonicalDigest`` 行必须版本 / SHA / 字节全部精确一致才重放原身份；digest 相同而
   元数据不符 -> ``reason=digest_metadata_mismatch``；**任意两个历史行**出现重复 digest
   -> ``planning_revision_invalid``（全历史去重，与本次 incoming digest 是否命中无关，
   也不猜哪一版）。
4. 既有为空 -> ``create_first``、revision 1（未知 / ``unverified`` 版本也可，不需要声明）。
5. 非空且为新内容：版本**精确等于** ``unverified``（大小写精确，不把 ``UNVERIFIED`` 猜成
   ``unverified``）不算明确新版本；只有明确的（非 ``unverified`` 且与所有既有版本都不同的）
   新版本，或有非空受控 ``new_content_reason`` 声明，才可 ``append_candidate``；否则
   ``reason=new_content_declaration_required``。
6. ``append_candidate`` 的 revision = **既有最大值 + 1**（不是 ``len + 1``）。

诊断码仅 ``planning_revision_invalid`` / ``planning_source_drift`` /
``planning_snapshot_conflict``，未知码拒绝；不修改任何全局枚举。

本轮（027c）相对 026 的唯一改动：在深度结构校验 ``_validate_existing`` 中补齐全历史
``canonicalDigest`` 去重，使「任意两个既有行 digest 重复」在业务判断（含 ``batch_drift``）
之前即报 ``planning_revision_invalid``；既有业务判断顺序与语义不变。
"""

__all__ = [
    "DECISION_APPEND_CANDIDATE",
    "DECISION_CREATE_FIRST",
    "DECISION_REPLAYED",
    "ERROR_INVALID",
    "ERROR_SNAPSHOT_CONFLICT",
    "ERROR_SOURCE_DRIFT",
    "EXISTING_FIELDS",
    "INCOMING_FIELDS",
    "PLANNING_REVISION_ERROR_CODES",
    "REASON_BATCH_DRIFT",
    "REASON_BATCH_DRIFT_NOT_BOOL",
    "REASON_DIGEST_METADATA_MISMATCH",
    "REASON_DUPLICATE_CANONICAL_DIGEST",
    "REASON_DUPLICATE_SNAPSHOT_ID",
    "REASON_DUPLICATE_SNAPSHOT_REVISION",
    "REASON_EXISTING_NOT_LIST",
    "REASON_FIELD_TYPE",
    "REASON_INCOMING_NOT_OBJECT",
    "REASON_MISSING_FIELD",
    "REASON_NEW_CONTENT_DECLARATION_REQUIRED",
    "REASON_NEW_CONTENT_REASON_INVALID",
    "REASON_ROW_NOT_OBJECT",
    "REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES",
    "REASON_UNKNOWN_FIELD",
    "RESULT_KEYS",
    "RESULT_REPLAYED_KEYS",
    "UNVERIFIED_DOCUMENT_VERSION",
    "PlanningRevisionError",
    "decide_planning_revision",
]

# ---------------------------------------------------------------------------
# 冻结常量
# ---------------------------------------------------------------------------

ERROR_INVALID = "planning_revision_invalid"
ERROR_SOURCE_DRIFT = "planning_source_drift"
ERROR_SNAPSHOT_CONFLICT = "planning_snapshot_conflict"

PLANNING_REVISION_ERROR_CODES = {
    ERROR_INVALID: "参数 / 既有行 / 受控提交不符合受控形态，拒绝且不建议任何 revision",
    ERROR_SOURCE_DRIFT: "本批来源字节 / 摘要发生漂移，优先拒绝且不建议任何 revision",
    ERROR_SNAPSHOT_CONFLICT: "既有记录与本次提交冲突，拒绝追加且不新建任何编号",
}

_ALLOWED_CODES = frozenset(PLANNING_REVISION_ERROR_CODES)

#: 既有行只接受这六个字段；自报编号 / 身份 / 其他一律拒绝。
EXISTING_FIELDS = (
    "snapshotId",
    "snapshotRevision",
    "canonicalDigest",
    "documentVersion",
    "sourceSha256",
    "sourceBytes",
)

#: 受控提交只接受这四个字段；``snapshotId`` / ``snapshotRevision`` / ``projectId`` 等自报
#: 字段一律拒绝。
INCOMING_FIELDS = (
    "canonicalDigest",
    "documentVersion",
    "sourceSha256",
    "sourceBytes",
)

#: 版本占位值：精确小写匹配；本模块不归一化大小写，也不改原文版本。
UNVERIFIED_DOCUMENT_VERSION = "unverified"

DECISION_CREATE_FIRST = "create_first"
DECISION_REPLAYED = "replayed"
DECISION_APPEND_CANDIDATE = "append_candidate"

#: 业务拒绝的稳定 reason（本卡冻结）。
REASON_BATCH_DRIFT = "batch_drift"
REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES = "same_document_version_different_bytes"
REASON_DIGEST_METADATA_MISMATCH = "digest_metadata_mismatch"
REASON_NEW_CONTENT_DECLARATION_REQUIRED = "new_content_declaration_required"

#: 参数 / 深度结构拒绝的稳定 reason（本卡冻结，仅用于诊断）。
REASON_BATCH_DRIFT_NOT_BOOL = "batch_drift_not_bool"
REASON_NEW_CONTENT_REASON_INVALID = "new_content_reason_invalid"
REASON_EXISTING_NOT_LIST = "existing_not_list"
REASON_INCOMING_NOT_OBJECT = "incoming_not_object"
REASON_ROW_NOT_OBJECT = "existing_row_not_object"
REASON_UNKNOWN_FIELD = "unknown_field"
REASON_MISSING_FIELD = "missing_field"
REASON_FIELD_TYPE = "field_type_invalid"
REASON_DUPLICATE_SNAPSHOT_ID = "duplicate_snapshot_id"
REASON_DUPLICATE_SNAPSHOT_REVISION = "duplicate_snapshot_revision"
REASON_DUPLICATE_CANONICAL_DIGEST = "duplicate_canonical_digest"

#: 返回 dict 的严格键集合：只有重放才额外带既有行原值 ``snapshotId``。
RESULT_KEYS = ("decision", "replayed", "snapshotRevision", "wouldActivate")
RESULT_REPLAYED_KEYS = (
    "decision",
    "replayed",
    "snapshotId",
    "snapshotRevision",
    "wouldActivate",
)

_SHA256_PREFIX = "sha256:"
_SHA256_HEX_LENGTH = 64
_HEX_DIGITS = frozenset("0123456789abcdef")


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class PlanningRevisionError(ValueError):
    """规划 revision 判定被拒绝时抛出的错误。

    ``code`` 只能是本卡冻结的三个诊断码；``details`` 保留稳定 ``reason`` 与字段路径，
    不回显正文 / 标题 / 内容。
    """

    def __init__(self, code, message=None, details=None):
        if code not in _ALLOWED_CODES:
            raise ValueError("unknown planning revision error code: %r" % (code,))
        self.code = code
        self.message = message or PLANNING_REVISION_ERROR_CODES[code]
        self.details = dict(details) if details else {}
        super(PlanningRevisionError, self).__init__(self.message)

    def __str__(self):
        return "{0}: {1}".format(self.code, self.message)


def _fail(code, message, reason, path):
    raise PlanningRevisionError(code, message, {"reason": reason, "path": path})


# ---------------------------------------------------------------------------
# 基础形态校验
# ---------------------------------------------------------------------------


def _is_sha256_text(value):
    if not isinstance(value, str) or not value.startswith(_SHA256_PREFIX):
        return False
    hex_part = value[len(_SHA256_PREFIX):]
    if len(hex_part) != _SHA256_HEX_LENGTH:
        return False
    for char in hex_part:
        if char not in _HEX_DIGITS:
            return False
    return True


def _require_sha256(value, path, label):
    if not _is_sha256_text(value):
        _fail(
            ERROR_INVALID,
            "{0} 形态必须是 sha256:<64位小写十六进制>".format(label),
            REASON_FIELD_TYPE,
            path,
        )


def _require_non_empty_string(value, path, label):
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "{0} 必须是字符串".format(label), REASON_FIELD_TYPE, path)
    if value == "":
        _fail(
            ERROR_INVALID, "{0} 必须是非空字符串".format(label), REASON_FIELD_TYPE, path
        )


def _require_positive_int(value, path, label):
    # 显式排除 bool：``True`` / ``False`` 是 ``int`` 子类，但不是 revision。
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(
            ERROR_INVALID,
            "{0} 必须是正整数（非布尔）".format(label),
            REASON_FIELD_TYPE,
            path,
        )
    if value <= 0:
        _fail(ERROR_INVALID, "{0} 必须 >= 1".format(label), REASON_FIELD_TYPE, path)


def _require_non_negative_int(value, path, label):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(
            ERROR_INVALID,
            "{0} 必须是非负整数（非布尔）".format(label),
            REASON_FIELD_TYPE,
            path,
        )
    if value < 0:
        _fail(ERROR_INVALID, "{0} 必须 >= 0".format(label), REASON_FIELD_TYPE, path)


def _validate_object_fields(container, required_fields, path, label):
    """字段白名单 + 必需字段：未知字段（含自报编号 / 身份）与缺失字段都拒绝。"""
    for key in container:
        if not isinstance(key, str):
            _fail(
                ERROR_INVALID,
                "{0} 的键必须是字符串".format(label),
                REASON_UNKNOWN_FIELD,
                path,
            )
        if key not in required_fields:
            _fail(
                ERROR_INVALID,
                "{0} 含未知字段（自报编号 / 身份 / 其他一律拒绝）".format(label),
                REASON_UNKNOWN_FIELD,
                path + "." + key,
            )
    for field in required_fields:
        if field not in container:
            _fail(
                ERROR_INVALID,
                "{0} 缺少必需字段".format(label),
                REASON_MISSING_FIELD,
                path + "." + field,
            )


# ---------------------------------------------------------------------------
# 深度结构校验（先于任何业务判断）
# ---------------------------------------------------------------------------


def _validate_existing(existing):
    if not isinstance(existing, list):
        _fail(
            ERROR_INVALID,
            "existing 必须是既有快照行数组",
            REASON_EXISTING_NOT_LIST,
            "$.existing",
        )

    seen_ids = set()
    seen_revisions = set()
    seen_digests = set()
    for position in range(len(existing)):
        row = existing[position]
        path = "$.existing[{0}]".format(position)
        if not isinstance(row, dict):
            _fail(
                ERROR_INVALID,
                "每一既有行必须是对象",
                REASON_ROW_NOT_OBJECT,
                path,
            )
        _validate_object_fields(row, EXISTING_FIELDS, path, "既有行")

        snapshot_id = row["snapshotId"]
        _require_non_empty_string(snapshot_id, path + ".snapshotId", "snapshotId")
        revision = row["snapshotRevision"]
        _require_positive_int(revision, path + ".snapshotRevision", "snapshotRevision")
        digest = row["canonicalDigest"]
        _require_sha256(digest, path + ".canonicalDigest", "canonicalDigest")
        _require_non_empty_string(
            row["documentVersion"], path + ".documentVersion", "documentVersion"
        )
        _require_sha256(row["sourceSha256"], path + ".sourceSha256", "sourceSha256")
        _require_non_negative_int(row["sourceBytes"], path + ".sourceBytes", "sourceBytes")

        if snapshot_id in seen_ids:
            _fail(
                ERROR_INVALID,
                "既有行 snapshotId 重复（全局唯一被破坏），拒绝且不猜哪一版",
                REASON_DUPLICATE_SNAPSHOT_ID,
                path + ".snapshotId",
            )
        seen_ids.add(snapshot_id)
        if revision in seen_revisions:
            _fail(
                ERROR_INVALID,
                "本来源既有行 snapshotRevision 重复，拒绝且不猜哪一版",
                REASON_DUPLICATE_SNAPSHOT_REVISION,
                path + ".snapshotRevision",
            )
        seen_revisions.add(revision)
        # 全历史 canonicalDigest 去重：任意两个既有行 digest 相同即结构无效，与本次
        # incoming digest 是否命中无关，也不猜哪一版；该检查先于 batch_drift 业务判断。
        if digest in seen_digests:
            _fail(
                ERROR_INVALID,
                "既有行 canonicalDigest 重复（全历史唯一被破坏），拒绝且不猜哪一版",
                REASON_DUPLICATE_CANONICAL_DIGEST,
                path + ".canonicalDigest",
            )
        seen_digests.add(digest)


def _validate_incoming(incoming):
    if not isinstance(incoming, dict):
        _fail(
            ERROR_INVALID,
            "incoming 必须是受控提交对象",
            REASON_INCOMING_NOT_OBJECT,
            "$.incoming",
        )
    _validate_object_fields(incoming, INCOMING_FIELDS, "$.incoming", "incoming")

    _require_sha256(
        incoming["canonicalDigest"], "$.incoming.canonicalDigest", "canonicalDigest"
    )
    _require_non_empty_string(
        incoming["documentVersion"], "$.incoming.documentVersion", "documentVersion"
    )
    _require_sha256(incoming["sourceSha256"], "$.incoming.sourceSha256", "sourceSha256")
    _require_non_negative_int(
        incoming["sourceBytes"], "$.incoming.sourceBytes", "sourceBytes"
    )


def _validate_parameters(batch_drift, new_content_reason):
    """参数级形态校验：``batch_drift`` 必须 bool；声明必须 None 或非空白字符串。"""
    if not isinstance(batch_drift, bool):
        _fail(
            ERROR_INVALID,
            "batch_drift 必须是布尔值",
            REASON_BATCH_DRIFT_NOT_BOOL,
            "$.batch_drift",
        )
    if new_content_reason is not None:
        if not isinstance(new_content_reason, str):
            _fail(
                ERROR_INVALID,
                "new_content_reason 必须是 None 或非空非空白字符串",
                REASON_NEW_CONTENT_REASON_INVALID,
                "$.new_content_reason",
            )
        # 只判空白，不 strip、不改原文声明。
        if new_content_reason.strip() == "":
            _fail(
                ERROR_INVALID,
                "new_content_reason 必须是非空非空白字符串（None 表示未声明）",
                REASON_NEW_CONTENT_REASON_INVALID,
                "$.new_content_reason",
            )


# ---------------------------------------------------------------------------
# 公开入口（纯函数）
# ---------------------------------------------------------------------------


def decide_planning_revision(existing, incoming, *, batch_drift=False,
                             new_content_reason=None):
    """给出规划 revision 建议：``create_first`` / ``replayed`` / ``append_candidate``。

    严格按冻结顺序判定（见模块 docstring）。只读入参、不改入参、不做任何 I/O；返回值只是
    建议，不生成编号、不写库、不激活。
    """
    # 0) 参数与深度结构校验：全部先于业务判断。
    _validate_parameters(batch_drift, new_content_reason)
    _validate_existing(existing)
    _validate_incoming(incoming)

    incoming_version = incoming["documentVersion"]
    incoming_digest = incoming["canonicalDigest"]
    incoming_sha256 = incoming["sourceSha256"]
    incoming_bytes = incoming["sourceBytes"]

    # 1) 批内漂移优先于重放与同版本冲突。
    if batch_drift:
        raise PlanningRevisionError(
            ERROR_SOURCE_DRIFT,
            "本批来源字节 / 摘要发生漂移，优先拒绝且不给出任何 revision 建议",
            {"reason": REASON_BATCH_DRIFT, "path": "$.batch_drift"},
        )

    # 2) 同 documentVersion 的既有记录字节硬冲突（声明不能豁免）。
    for row in existing:
        if row["documentVersion"] == incoming_version:
            if (
                row["sourceSha256"] != incoming_sha256
                or row["sourceBytes"] != incoming_bytes
            ):
                raise PlanningRevisionError(
                    ERROR_SNAPSHOT_CONFLICT,
                    "同 documentVersion 的既有记录字节 / SHA 与本次提交不同，"
                    "硬冲突且声明不能豁免",
                    {
                        "reason": REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
                        "path": "$.existing",
                    },
                )

    # 3) 同 canonicalDigest 重放：版本 / SHA / 字节必须全部精确一致。
    matches = []
    for row in existing:
        if row["canonicalDigest"] == incoming_digest:
            matches.append(row)
    if len(matches) > 1:
        _fail(
            ERROR_INVALID,
            "既有行出现重复 canonicalDigest，拒绝且不猜哪一版",
            REASON_DUPLICATE_CANONICAL_DIGEST,
            "$.existing",
        )
    if len(matches) == 1:
        row = matches[0]
        if (
            row["documentVersion"] != incoming_version
            or row["sourceSha256"] != incoming_sha256
            or row["sourceBytes"] != incoming_bytes
        ):
            raise PlanningRevisionError(
                ERROR_SNAPSHOT_CONFLICT,
                "canonicalDigest 相同但版本 / SHA / 字节不一致，"
                "视为持久记录损坏或冲突，拒绝重放",
                {
                    "reason": REASON_DIGEST_METADATA_MISMATCH,
                    "path": "$.existing",
                },
            )
        # 重放只回显既有行原值身份与 revision，不新建编号、不写审计建议。
        return {
            "decision": DECISION_REPLAYED,
            "snapshotRevision": row["snapshotRevision"],
            "snapshotId": row["snapshotId"],
            "replayed": True,
            "wouldActivate": False,
        }

    # 4) 既有为空：首版建议。
    if not existing:
        return {
            "decision": DECISION_CREATE_FIRST,
            "snapshotRevision": 1,
            "replayed": False,
            "wouldActivate": False,
        }

    # 5) 非空 + 新内容：明确新版本，或受控声明。
    explicit_new_version = incoming_version != UNVERIFIED_DOCUMENT_VERSION
    if explicit_new_version:
        for row in existing:
            if row["documentVersion"] == incoming_version:
                explicit_new_version = False
                break
    if not explicit_new_version and new_content_reason is None:
        raise PlanningRevisionError(
            ERROR_SNAPSHOT_CONFLICT,
            "既非明确新版本、也没有受控 new_content_reason 声明，拒绝追加",
            {"reason": REASON_NEW_CONTENT_DECLARATION_REQUIRED, "path": "$.incoming"},
        )

    # 6) 追加候选：revision = 既有最大值 + 1（不是 len + 1）。
    highest = 0
    for row in existing:
        if row["snapshotRevision"] > highest:
            highest = row["snapshotRevision"]
    return {
        "decision": DECISION_APPEND_CANDIDATE,
        "snapshotRevision": highest + 1,
        "replayed": False,
        "wouldActivate": False,
    }
