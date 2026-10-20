"""D4-NEXT-02A1：成员与模型登记纯合同（接口文档 §13.7，cox 已冻结）。

只实现四个纯函数，不做服务 / CLI / 库 / 宿主，无 fs / env / db / network / host 副作用：

- ``parse_registry_json(text)``：解析登记 JSON 文本，做重复键 / NaN / 超限 / 根类型检查。
- ``normalize_agent_alias(value)``：别名 Unicode NFC → strip → casefold，返回 normalizedAlias。
- ``validate_agent_registration(payload)``：``agent-registration.v1`` 结构校验。
- ``validate_model_profile(payload)``：``model-profile.v1`` 结构校验。

约定（严格遵循 §13.7）：

- 所有失败统一抛既有 ``contracts.schemas.ContractError``，``code="invalid_task"``，
  ``details`` 含稳定 ``field`` / ``reason``；不新增或注册全局错误码，不改 ``schemas.py``。
- 成功返回**新对象**，不修改输入；``normalizedAlias`` 由本模块生成，不接受输入自报。
- 只做纯词法 / 结构校验：真实存在性、realpath、no-symlink、项目 / 环境交集与
  宿主认证均由服务 / 派发检查再核，本卡不宣称 sandbox 或可运行。
- 仅登记请求，不代表验证或授权。
"""
from __future__ import annotations

import copy
import json
import re
import unicodedata

from contracts.schemas import ContractError

# ---------------------------------------------------------------------------
# 版本、词表与常量
# ---------------------------------------------------------------------------

AGENT_REGISTRATION_V1 = "agent-registration.v1"
MODEL_PROFILE_V1 = "model-profile.v1"

# parse 只接受 str，UTF-8 字节数上限 1 MiB。
REGISTRY_JSON_MAX_BYTES = 1024 * 1024

# 通用字符串长度上限（displayName 单独放宽到 256）。
STRING_MAX_LEN = 128
DISPLAY_NAME_MAX_LEN = 256

# 受控 ID / AdapterType：完整匹配，无 URL / 路径 / argv / 换行 / 代码插入。
# 用 fullmatch（而非 "$" 结尾）以免尾 LF 被 "$" 误判为合法。
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

# configDigest 完整字段，不 strip；尾 LF 必须拒绝。
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")

ROLE_VOCABULARY = (
    "sales-architect",
    "skill-worker",
    "qa-reporter",
    "docs-worker",
    "release-ops",
)
CAPABILITY_VOCABULARY = ("read", "develop", "test", "document", "review")

PERMISSION_FIELDS = (
    "writeScope",
    "allowNetwork",
    "allowBusinessWrite",
    "allowGit",
    "allowPublish",
)
PERMISSION_BOOL_FIELDS = (
    "allowNetwork",
    "allowBusinessWrite",
    "allowGit",
    "allowPublish",
)
# 业务 / Git / 发布必须为 false；allowNetwork 为 true 仅声明管理上限。
PERMISSION_MUST_BE_FALSE = ("allowBusinessWrite", "allowGit", "allowPublish")

REGISTRATION_FIELDS = (
    "schemaVersion",
    "agentId",
    "agentAlias",
    "displayName",
    "roles",
    "capabilities",
    "environmentId",
    "allowedRoots",
    "permissionCeiling",
    "modelProfileId",
    "binding",
    "enabled",
)

BINDING_FIELDS = (
    "environmentId",
    "adapterType",
    "connectorConfigRef",
    "executionProfileRef",
    "hostAgentId",
    "configDigest",
)

MODEL_PROFILE_BASE_FIELDS = ("schemaVersion", "modelProfileId", "environmentId", "strategy")
MODEL_PROFILE_FIXED_FIELDS = ("provider", "modelId")
MODEL_STRATEGIES = ("fixed", "host-default")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _invalid(field, reason):
    """统一失败出口：既有 ContractError，code=invalid_task，details.field/reason。"""
    raise ContractError(
        "invalid_task",
        message="登记数据不合法：%s" % reason,
        details={"field": field, "reason": reason},
    )


def _has_forbidden_char(value):
    """含 Unicode 控制类别 Cc/Cf/Cs 即拒绝。"""
    return any(unicodedata.category(ch) in ("Cc", "Cf", "Cs") for ch in value)


def _check_string(value, field, max_len=STRING_MAX_LEN):
    if not isinstance(value, str):
        _invalid(field, "必须是字符串")
    if _has_forbidden_char(value):
        _invalid(field, "不能包含 Unicode 控制字符")
    if not value:
        _invalid(field, "不能为空")
    if len(value) > max_len:
        _invalid(field, "长度不能超过 %d" % max_len)
    return value


def _check_id(value, field):
    """受控 ID / AdapterType：完整匹配 ID 规则，不做 Unicode / 空白自动修正。"""
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _invalid(field, "必须是受控 ID（[A-Za-z0-9][A-Za-z0-9_.-]{0,127}）")
    return value


def _check_unknown_fields(payload, allowed, prefix=""):
    """未知字段一律拒绝。

    nested 层（如 ``permissionCeiling`` / ``binding``）携带 ``prefix``，
    返回完整点路径（如 ``binding.argv``、``permissionCeiling.extra``）；
    根层不传 ``prefix``，保留原 key。
    """
    for key in payload:
        if key not in allowed:
            field = "%s.%s" % (prefix, key) if prefix else key
            _invalid(field, "出现未冻结字段，拒绝以避免歧义")
    return payload


def _check_absolute_posix_path(value, field):
    """纯词法绝对 POSIX 路径校验，不触发 Path.resolve / stat。

    拒绝非绝对、NUL / 控制字符、``..`` / ``.`` 段、重复 ``//``，
    以及除根 ``/`` 外的尾 ``/``。
    """
    if not isinstance(value, str) or not value.startswith("/"):
        _invalid(field, "必须是严格的绝对 POSIX 路径")
    if _has_forbidden_char(value):
        _invalid(field, "路径不能包含 NUL 或控制字符")
    if value == "/":
        return value
    if value.endswith("/"):
        _invalid(field, "除根外不允许尾斜杠")
    for segment in value.split("/")[1:]:
        if segment in ("", ".", ".."):
            _invalid(field, "路径不允许空段 / '.' / '..'")
    return value


def _path_within(path, root):
    """按路径段判断包含，避免字符串前缀把 /a2 误判进 /a。"""
    if root == "/":
        return True
    return path == root or path.startswith(root + "/")


def _check_vocab_list(value, field, vocabulary, allow_empty):
    if not isinstance(value, list):
        _invalid(field, "必须是数组")
    if not value and not allow_empty:
        _invalid(field, "不能为空数组")
    seen = set()
    for index, item in enumerate(value):
        position = "%s[%d]" % (field, index)
        if not isinstance(item, str) or item not in vocabulary:
            _invalid(position, "不在受控词表内")
        if item in seen:
            _invalid(position, "列表元素不得重复")
        seen.add(item)
    return value


def _check_root_list(value, field):
    if not isinstance(value, list):
        _invalid(field, "必须是数组")
    if not value:
        _invalid(field, "至少需要一个允许根")
    seen = set()
    for index, item in enumerate(value):
        position = "%s[%d]" % (field, index)
        path = _check_absolute_posix_path(item, position)
        if path in seen:
            _invalid(position, "允许根不得重复")
        seen.add(path)
    return value


def _check_permission_ceiling(value, allowed_roots):
    if not isinstance(value, dict):
        _invalid("permissionCeiling", "必须是对象")
    _check_unknown_fields(value, set(PERMISSION_FIELDS), "permissionCeiling")
    for key in PERMISSION_FIELDS:
        if key not in value:
            _invalid("permissionCeiling.%s" % key, "必填字段缺失")

    scope = value["writeScope"]
    if not isinstance(scope, list):
        _invalid("permissionCeiling.writeScope", "必须是数组")
    seen = set()
    for index, item in enumerate(scope):
        position = "permissionCeiling.writeScope[%d]" % index
        path = _check_absolute_posix_path(item, position)
        if path in seen:
            _invalid(position, "写入范围不得重复")
        seen.add(path)
        if not any(_path_within(path, root) for root in allowed_roots):
            _invalid(position, "必须按路径段包含于至少一个 allowedRoot")

    for key in PERMISSION_BOOL_FIELDS:
        if not isinstance(value[key], bool):
            _invalid("permissionCeiling.%s" % key, "必须是布尔值")
    for key in PERMISSION_MUST_BE_FALSE:
        if value[key] is True:
            _invalid("permissionCeiling.%s" % key, "业务 / Git / 发布权限必须为 false")
    return value


def _check_binding(value, environment_id):
    if not isinstance(value, dict):
        _invalid("binding", "必须是对象")
    _check_unknown_fields(value, set(BINDING_FIELDS), "binding")
    for key in BINDING_FIELDS:
        if key not in value:
            _invalid("binding.%s" % key, "必填字段缺失")

    if value["environmentId"] != environment_id:
        _invalid("binding.environmentId", "必须与顶层 environmentId 一致")
    _check_id(value["adapterType"], "binding.adapterType")
    _check_id(value["connectorConfigRef"], "binding.connectorConfigRef")

    profile_ref = value["executionProfileRef"]
    host_agent_id = value["hostAgentId"]
    if profile_ref is not None:
        _check_id(profile_ref, "binding.executionProfileRef")
    if host_agent_id is not None:
        _check_id(host_agent_id, "binding.hostAgentId")
    if profile_ref is None and host_agent_id is None:
        _invalid("binding", "executionProfileRef 与 hostAgentId 不能同时为 null")

    digest = value["configDigest"]
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        _invalid("binding.configDigest", "必须是完整 sha256:<64 位小写十六进制>")
    return value


# ---------------------------------------------------------------------------
# 公开纯函数
# ---------------------------------------------------------------------------

def _object_pairs_hook(pairs):
    """JSON 对象钩子：任意层级出现重复键即拒绝（进入无效输入，不保留后值）。"""
    seen = set()
    for key, _value in pairs:
        if key in seen:
            _invalid(key, "JSON 出现重复键（含嵌套）")
        seen.add(key)
    return dict(pairs)


def _reject_constant(name):
    _invalid("$", "JSON 不接受 %s" % name)


def parse_registry_json(text):
    """解析登记 JSON：只接受 str，UTF-8 ≤ 1 MiB。

    拒绝重复键（含嵌套）、NaN / Infinity、不完整 JSON、根非 object；
    只解析，具体 schema 由相应 validator 校验。
    """
    if not isinstance(text, str):
        _invalid("text", "只接受字符串输入")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        # 未配对代理项等无法以 UTF-8 编码：受控 invalid_task，不回显输入。
        # 不使用 replace / ignore，正常 UTF-8 输入保持逐字节保真。
        _invalid("text", "字符串无法以 UTF-8 编码（含未配对代理项）")
    if encoded_size > REGISTRY_JSON_MAX_BYTES:
        _invalid("text", "JSON 超过 1 MiB 上限")

    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_object_pairs_hook,
            parse_constant=_reject_constant,
        )
    except ContractError:
        raise
    except RecursionError:
        # 极深嵌套触发解析器递归上限：与语法 / 编码错误分开的受控原因，
        # 不调整全局 recursionlimit，不宽泛吞异常。
        _invalid("text", "JSON 嵌套层级过深，超过解析器递归上限")
    except (ValueError, TypeError):
        _invalid("text", "JSON 语法不完整或非法")

    if not isinstance(parsed, dict):
        _invalid("$", "JSON 根必须是对象")
    return parsed


def normalize_agent_alias(value):
    """别名规范化：Unicode NFC → strip → casefold，返回 normalizedAlias。

    - 只接受字符串；拒绝 Unicode 控制字符、空 / 纯空白、超长。
    - 不能只用 ``lower()``（casefold 处理更广的大小写折叠）。
    - 输入 alias 原值由调用方保存；冲突由服务按 normalizedAlias 唯一键拒绝。
    """
    _check_string(value, "agentAlias", STRING_MAX_LEN)
    normalized = unicodedata.normalize("NFC", value).strip().casefold()
    if not normalized:
        _invalid("agentAlias", "别名去除空白后不能为空")
    return normalized


def validate_agent_registration(payload):
    """``agent-registration.v1`` 纯结构校验，返回新对象（附 normalizedAlias）。

    - 恰好 12 个必填字段；未知字段（含嵌套）一律拒绝。
    - ``enabled`` 首次注册必须严格 ``false``（``true`` 不能抢占启用动作）。
    - ``permissionCeiling`` 严格 5 字段，四 toggle 严格 bool，业务 / Git / 发布必须 false。
    - ``binding`` 严格 6 字段，environmentId 与顶层一致，profile / hostAgentId
      至少一个非 null，configDigest 为完整 SHA。
    - ``normalizedAlias`` 由本函数生成，输入自报会被当未知字段拒绝。
    """
    if not isinstance(payload, dict):
        _invalid("$", "登记请求必须是对象")
    _check_unknown_fields(payload, set(REGISTRATION_FIELDS))
    for field in REGISTRATION_FIELDS:
        if field not in payload:
            _invalid(field, "必填字段缺失")

    if payload["schemaVersion"] != AGENT_REGISTRATION_V1:
        _invalid("schemaVersion", "必须是 %s" % AGENT_REGISTRATION_V1)

    _check_id(payload["agentId"], "agentId")

    alias = _check_string(payload["agentAlias"], "agentAlias", STRING_MAX_LEN)
    normalized_alias = normalize_agent_alias(alias)

    display_name = _check_string(payload["displayName"], "displayName", DISPLAY_NAME_MAX_LEN)
    if not display_name.strip():
        _invalid("displayName", "不能只包含空白")

    _check_vocab_list(payload["roles"], "roles", ROLE_VOCABULARY, allow_empty=False)
    _check_vocab_list(
        payload["capabilities"], "capabilities", CAPABILITY_VOCABULARY, allow_empty=True
    )

    _check_id(payload["environmentId"], "environmentId")
    allowed_roots = _check_root_list(payload["allowedRoots"], "allowedRoots")

    _check_permission_ceiling(payload["permissionCeiling"], allowed_roots)

    _check_id(payload["modelProfileId"], "modelProfileId")
    _check_binding(payload["binding"], payload["environmentId"])

    enabled = payload["enabled"]
    if not isinstance(enabled, bool) or enabled is not False:
        _invalid("enabled", "首次注册必须严格 enabled=false")

    result = copy.deepcopy(payload)
    result["normalizedAlias"] = normalized_alias
    return result


def validate_model_profile(payload):
    """``model-profile.v1`` 纯结构校验，返回新对象。

    - 根严格必填 schemaVersion / modelProfileId / environmentId / strategy。
    - ``strategy`` 为 ``fixed`` 或 ``host-default``。
    - ``fixed`` 另外恰好必填 provider / modelId（安全 ID）；provider 只是请求声明，
      不当作 observed provider。
    - ``host-default`` 禁止出现 provider / modelId（含 null）。
    - ``profileRevision`` 由服务生成，输入自报会被当未知字段拒绝。
    """
    if not isinstance(payload, dict):
        _invalid("$", "模型 profile 必须是对象")
    for field in MODEL_PROFILE_BASE_FIELDS:
        if field not in payload:
            _invalid(field, "必填字段缺失")

    if payload["schemaVersion"] != MODEL_PROFILE_V1:
        _invalid("schemaVersion", "必须是 %s" % MODEL_PROFILE_V1)

    strategy = payload["strategy"]
    if strategy not in MODEL_STRATEGIES:
        _invalid("strategy", "必须是 fixed 或 host-default")

    allowed = set(MODEL_PROFILE_BASE_FIELDS)
    if strategy == "fixed":
        allowed |= set(MODEL_PROFILE_FIXED_FIELDS)
    # host-default：provider / modelId 不在允许集内，出现（含 null）即拒绝。
    _check_unknown_fields(payload, allowed)

    _check_id(payload["modelProfileId"], "modelProfileId")
    _check_id(payload["environmentId"], "environmentId")

    if strategy == "fixed":
        for field in MODEL_PROFILE_FIXED_FIELDS:
            if field not in payload:
                _invalid(field, "fixed 策略必须提供 %s" % field)
        _check_id(payload["provider"], "provider")
        _check_id(payload["modelId"], "modelId")

    return copy.deepcopy(payload)
