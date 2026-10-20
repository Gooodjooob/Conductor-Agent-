"""planning-snapshot.v1 确定性摘要纯函数（第 009 轮返工候选实现，基于第 008 轮）。

契约来源：
- ``evidence/digest-contract.md`` §15.2.4（确定性摘要载荷与 JSON 编码，冻结）
- 任务卡 ``docs/development/task-cards/2026-10-10-d4-mvp20-planning-digest-codebuddy.md``

本卡范围（严格限定）：
- 只实现「摘要载荷字段检查 + 规范化 JSON 编码 + SHA256」纯函数。
- 不做任何文件 / 网络 / 数据库 / 环境访问，导入时无副作用。
- 不实现导入、存储、来源登记、激活、CLI、派发，也不实现完整 snapshot
  字段校验与引用完整性校验；本模块通过**不代表**完整 planning-snapshot.v1
  校验通过。
- 只使用标准库 ``copy`` / ``hashlib`` / ``json``（Python 3.9 兼容）。

关键约定：
- 参与摘要字段严格按 §15.2.4 清单；受控元数据（``canonicalDigest`` / ``snapshotId`` /
  ``snapshotRevision`` / ``importedAt`` / ``importedBy``）允许出现在顶层，但**必须排除**
  在摘要载荷之外，因此修改元数据不改变摘要。
- 摘要不是原文件字节 SHA：``sourceFile.sha256`` 用于原文字节漂移检测，二者不可互相替代。
- 编码冻结参数：UTF-8、``ensure_ascii=False``、``sort_keys=True``、
  ``separators=(',', ':')``、``allow_nan=False``；对象键按字典序排序，数组保持原顺序，
  不做 Unicode / 换行 / 空白归一化。
- 拒绝优先级（fail-closed）：先做保留字段扫描（自引用 / 运行事实），再做顶层字段白名单、
  必需字段、字段取值校验，最后做整棵输入树的 JSON 可编码校验。
- 009 返工只收边界拒绝，不改摘要字段清单与编码参数：``sourceKind`` 先做 ``str`` 类型
  检查再判枚举成员（``[]`` / ``{}`` / ``None`` / 整数必须落到 ``planning_digest_invalid``，
  不得抛 ``TypeError``）；载荷中的字符串值与对象键必须是合法 UTF-8 可编码文本（孤立
  surrogate 拒绝且不回显内容，合法非 ASCII 与 NFC/NFD 原样保留，不替换、不归一化）；
  循环引用不属于 JSON 树，按非 JSON 值拒绝。
"""

import copy
import hashlib
import json

__all__ = [
    "PlanningDigestError",
    "digest_payload",
    "compute_canonical_digest",
]

SCHEMA_VERSION = "planning-snapshot.v1"

# §15.2.4 参与摘要计算的字段（唯一清单）。
PARTICIPATING_FIELDS = (
    "schemaVersion",
    "projectId",
    "planningSourceId",
    "sourceKind",
    "sourceFile",
    "sections",
    "stages",
    "businessLines",
    "milestones",
    "planStatus",
    "unresolved",
)

# 必需字段；``unresolved`` 可省略，缺省规范为 []。
REQUIRED_FIELDS = tuple(f for f in PARTICIPATING_FIELDS if f != "unresolved")

# 顶层允许但必须排除在摘要载荷之外的受控元数据字段。
EXCLUDED_METADATA_FIELDS = (
    "canonicalDigest",
    "snapshotId",
    "snapshotRevision",
    "importedAt",
    "importedBy",
)

_ALLOWED_TOP_LEVEL_FIELDS = frozenset(PARTICIPATING_FIELDS + EXCLUDED_METADATA_FIELDS)

# sourceFile 只允许这五个清单字段。
SOURCE_FILE_FIELDS = ("name", "path", "documentVersion", "sha256", "bytes")
_SOURCE_FILE_FIELD_SET = frozenset(SOURCE_FILE_FIELDS)

SOURCE_KINDS = frozenset(("product", "development-plan", "other-registered"))

LIST_FIELDS = ("sections", "stages", "businessLines", "milestones", "unresolved")

# 不允许出现在摘要载荷任何嵌套位置的运行事实 / 授权字段。
EXECUTION_FACT_KEYS = frozenset(
    (
        "taskId",
        "attemptId",
        "deliveryId",
        "eventId",
        "reviewId",
        "runtimeTaskId",
        "approved",
        "accepted",
        "acceptedByHost",
        "dispatch",
        "submitted",
        "bindingRevision",
        "modelProfile",
        "permissions",
        "workspace",
    )
)

# 摘要不得自引用；除顶层受控元数据外，任何嵌套位置出现该键均拒绝。
SELF_REFERENCE_KEY = "canonicalDigest"

_HEX_DIGITS = frozenset("0123456789abcdef")
_SHA256_PREFIX = "sha256:"
_SHA256_HEX_LENGTH = 64

_POSITIVE_INFINITY = float("inf")
_NEGATIVE_INFINITY = float("-inf")

# 诊断码（本卡冻结）。
ERROR_INVALID = "planning_digest_invalid"
ERROR_SELF_REFERENCE = "planning_digest_self_reference"
ERROR_EXECUTION_FACT = "planning_execution_fact_forbidden"


class PlanningDigestError(ValueError):
    """摘要载荷被拒绝时抛出的错误。

    ``code`` 为诊断码；``details`` 保留字段路径（不回显敏感内容）。
    """

    def __init__(self, code, message, details=None):
        super(PlanningDigestError, self).__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details) if details else {}

    def __str__(self):
        return "{0}: {1}".format(self.code, self.message)


def _fail(code, message, path):
    raise PlanningDigestError(code, message, {"path": path})


def _join(base, key):
    return base + "." + key


def _index(base, position):
    return "{0}[{1}]".format(base, position)


def _check_utf8_text(text, path):
    """str 值 / 对象键必须是合法 UTF-8 可编码文本（拒绝孤立 surrogate）。

    只捕获 :class:`UnicodeEncodeError` 这一种精确异常，不做笼统 except；
    合法非 ASCII、NFC / NFD 原样保留，不替换、不归一化、不 strip。
    """
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        _fail(ERROR_INVALID, "字符串必须是合法 UTF-8 可编码文本", path)


def _check_no_cycle(value, path, ancestors):
    """容器只允许沿当前祖先链出现一次；真正的循环引用按非 JSON 树拒绝。

    祖先链按不可变元组向下传递，因此同一对象作为**兄弟节点**重复出现
    （合法 DAG / 共享引用）不会被误判。
    """
    identity = id(value)
    if identity in ancestors:
        _fail(ERROR_INVALID, "载荷包含循环引用，不属于 JSON 树", path)
    return ancestors + (identity,)


def _require_non_empty_string(value, path):
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "字段必须是字符串", path)
    if value == "":
        _fail(ERROR_INVALID, "字段必须是非空字符串", path)


def _validate_source_path(value, path):
    if not value.startswith("/"):
        _fail(ERROR_INVALID, "sourceFile.path 必须是绝对路径", path)
    if ".." in value.split("/"):
        _fail(ERROR_INVALID, "sourceFile.path 不允许包含 .. 路径分量", path)


def _validate_source_sha256(value, path):
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "sourceFile.sha256 必须是字符串", path)
    if not value.startswith(_SHA256_PREFIX):
        _fail(ERROR_INVALID, "sourceFile.sha256 形态必须是 sha256:<64位小写十六进制>", path)
    hex_part = value[len(_SHA256_PREFIX):]
    if len(hex_part) != _SHA256_HEX_LENGTH:
        _fail(ERROR_INVALID, "sourceFile.sha256 形态必须是 sha256:<64位小写十六进制>", path)
    for char in hex_part:
        if char not in _HEX_DIGITS:
            _fail(ERROR_INVALID, "sourceFile.sha256 形态必须是 sha256:<64位小写十六进制>", path)


def _validate_source_bytes(value, path):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(ERROR_INVALID, "sourceFile.bytes 必须是非负整数（非布尔）", path)
    if value < 0:
        _fail(ERROR_INVALID, "sourceFile.bytes 必须 >= 0", path)


def _validate_source_file(source_file):
    path = "$.sourceFile"
    if not isinstance(source_file, dict):
        _fail(ERROR_INVALID, "sourceFile 必须是 JSON 对象", path)

    for key in source_file:
        if not isinstance(key, str):
            _fail(ERROR_INVALID, "sourceFile 的键必须是字符串", path)
        if key not in _SOURCE_FILE_FIELD_SET:
            _fail(ERROR_INVALID, "sourceFile 不允许多余字段", _join(path, key))

    for field in SOURCE_FILE_FIELDS:
        if field not in source_file:
            _fail(ERROR_INVALID, "sourceFile 缺少必需字段", _join(path, field))

    for field in ("name", "path", "documentVersion"):
        _require_non_empty_string(source_file[field], _join(path, field))

    _validate_source_path(source_file["path"], _join(path, "path"))
    _validate_source_sha256(source_file["sha256"], _join(path, "sha256"))
    _validate_source_bytes(source_file["bytes"], _join(path, "bytes"))


def _validate_participating_fields(snapshot):
    if snapshot["schemaVersion"] != SCHEMA_VERSION:
        _fail(ERROR_INVALID, "schemaVersion 必须为 planning-snapshot.v1", "$.schemaVersion")

    for field in ("projectId", "planningSourceId"):
        _require_non_empty_string(snapshot[field], _join("$", field))

    # sourceKind：先做类型检查，再做枚举成员判断；非字符串（[] / {} / None / 整数）
    # 必须落到 planning_digest_invalid，而不是 set membership 抛出的 TypeError。
    source_kind = snapshot["sourceKind"]
    if not isinstance(source_kind, str):
        _fail(ERROR_INVALID, "sourceKind 必须是字符串", "$.sourceKind")
    if source_kind not in SOURCE_KINDS:
        _fail(ERROR_INVALID, "sourceKind 不在允许取值内", "$.sourceKind")

    _validate_source_file(snapshot["sourceFile"])

    for field in LIST_FIELDS:
        if field not in snapshot:
            continue
        if not isinstance(snapshot[field], list):
            _fail(ERROR_INVALID, "字段必须是数组", _join("$", field))

    _require_non_empty_string(snapshot["planStatus"], "$.planStatus")


def _reject_forbidden_keys(value, path, is_root, ancestors=()):
    """递归扫描保留字段：摘要自引用与运行事实 / 授权字段。

    同时在此处校验对象键的 UTF-8 可编码性（非字符串键与非法键都在此拒绝，
    错误只记录**容器**路径，不回显键内容），并拒绝真正的循环引用。
    """
    if isinstance(value, dict):
        ancestors = _check_no_cycle(value, path, ancestors)
        for key, sub_value in value.items():
            if isinstance(key, str):
                _check_utf8_text(key, path)
                if key == SELF_REFERENCE_KEY and not is_root:
                    _fail(
                        ERROR_SELF_REFERENCE,
                        "摘要载荷任何嵌套位置不得包含 canonicalDigest 自引用",
                        _join(path, key),
                    )
                if key in EXECUTION_FACT_KEYS:
                    _fail(
                        ERROR_EXECUTION_FACT,
                        "摘要载荷不得嵌入运行事实或授权字段",
                        _join(path, key),
                    )
                child_path = _join(path, key)
            else:
                child_path = path
            _reject_forbidden_keys(sub_value, child_path, False, ancestors)
    elif isinstance(value, list):
        ancestors = _check_no_cycle(value, path, ancestors)
        for position, sub_value in enumerate(value):
            _reject_forbidden_keys(sub_value, _index(path, position), False, ancestors)


def _validate_json_tree(value, path, ancestors=()):
    """递归校验输入是合法 JSON 值（allow_nan=False 语义）。"""
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, str):
        _check_utf8_text(value, path)
        return
    if isinstance(value, float):
        if value != value or value == _POSITIVE_INFINITY or value == _NEGATIVE_INFINITY:
            _fail(ERROR_INVALID, "浮点值不允许 NaN 或 Infinity", path)
        return
    if isinstance(value, list):
        ancestors = _check_no_cycle(value, path, ancestors)
        for position, sub_value in enumerate(value):
            _validate_json_tree(sub_value, _index(path, position), ancestors)
        return
    if isinstance(value, dict):
        ancestors = _check_no_cycle(value, path, ancestors)
        for key, sub_value in value.items():
            if not isinstance(key, str):
                _fail(ERROR_INVALID, "JSON 对象键必须是字符串", path)
            _check_utf8_text(key, path)
            _validate_json_tree(sub_value, _join(path, key), ancestors)
        return
    _fail(ERROR_INVALID, "包含非 JSON 值", path)


def digest_payload(snapshot):
    """校验快照并返回**独立深拷贝**的摘要载荷（不改变输入）。"""
    if not isinstance(snapshot, dict):
        _fail(ERROR_INVALID, "快照必须是 JSON 对象", "$")

    # 1) 保留字段优先扫描：自引用 / 运行事实必须整批拒绝。
    _reject_forbidden_keys(snapshot, "$", True)

    # 2) 顶层白名单：未知字段（含 activation / 人工 adjustment）不得被静默丢弃。
    for key in snapshot:
        if not isinstance(key, str):
            _fail(ERROR_INVALID, "顶层键必须是字符串", "$")
        if key not in _ALLOWED_TOP_LEVEL_FIELDS:
            _fail(ERROR_INVALID, "不允许的快照字段", _join("$", key))

    # 3) 必需字段。
    for field in REQUIRED_FIELDS:
        if field not in snapshot:
            _fail(ERROR_INVALID, "缺少必需字段", _join("$", field))

    # 4) 字段取值校验。
    _validate_participating_fields(snapshot)

    # 5) 整棵输入树必须 JSON 可编码（含受控元数据，保持 fail-closed）。
    _validate_json_tree(snapshot, "$")

    # 6) 只挑参与字段，逐项深拷贝，确保输入与返回值互不影响。
    payload = {}
    for field in PARTICIPATING_FIELDS:
        if field == "unresolved":
            payload[field] = copy.deepcopy(snapshot.get("unresolved", []))
        else:
            payload[field] = copy.deepcopy(snapshot[field])
    return payload


def compute_canonical_digest(snapshot):
    """返回 ``sha256:`` + 64 位小写十六进制的内容摘要（不改变输入）。"""
    payload = digest_payload(snapshot)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _SHA256_PREFIX + hashlib.sha256(encoded).hexdigest()
