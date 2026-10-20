"""D4-MVP20-030：规划来源版本核验（只读候选实现，030 冲突优先级返工候选）。

冻结依据：
- ``docs/modules/agent-task-control-plane-d4-interface-contract.md``
  §15.2（``sourceFile.name`` / ``path`` / ``documentVersion`` / ``sha256`` / ``bytes``）、
  §15.4 S05（预检后原文被改 -> ``planning_source_drift``）、S06（缺版本只写
  ``unverified``，不猜版本）、S12（同版本不同字节 -> ``planning_snapshot_conflict``）、
  §15.5（只读预检语义：``wouldWrite=false`` / ``wouldActivate=false``，不写审计 / 快照 / DB，
  不生成任何编号）、§16.1–§16.4（来源版本变化判定顺序：序 1 批内漂移优先且不可改判；
  序 2 只在新批追加；序 3 只提示；同版本不同字节一律硬拒，不得当作来源更新）。

本卡（030 返工）相对 029 的唯一行为改动 —— **只修判定优先级**：
- 029 缺陷：``_classify_version`` 先返回 ``missing`` / ``unverified`` 占位结论，之后才做
  同版本字节冲突核验；于是「声明版本与已登记版本**同版本**但 ``sha256`` / ``bytes`` 不同」
  在 ``documentVersion`` 精确等于 ``unverified`` 时被放过。cox 独立探针
  ``unverified_same_version_byte_conflict`` 实测 ``status=unverified``，期望
  ``planning_snapshot_conflict``，故为可复现缺陷。
- 030 修复：同版本 ``sha256`` / ``bytes`` 冲突核验**前移**，先于 ``missing`` /
  ``unverified`` 占位结论。只要声明版本是**原样非空字符串**且与某已登记版本行
  ``documentVersion`` 精确相等，就先比 ``sha256`` / ``bytes``；不一致一律
  ``planning_snapshot_conflict``（``reason=same_document_version_different_bytes``）。
  ``unverified`` 占位只表示「版本未核验」，**不是**同版本字节冲突的豁免，也不能被改判成
  「来源更新」。
- 其余口径与 029 完全一致，**未放宽任何受控规则**：路径安全、允许根、普通文件、两次只读
  对拍、序 1 漂移优先、版本字符串不归一化、``missing`` / ``unverified`` 只给结论不追加
  revision、零数据库写 / 零快照写 / 零激活 / 零执行事实。

本卡只做一件事（只读核验，不落任何状态）：
- 调用方**显式**传入一份已登记来源声明（``path`` / ``sourceKind`` / ``name`` /
  ``documentVersion``）与受控允许根；本模块在受控允许根内只读该**普通文件**一次以上，
  计算实际 ``bytes`` / ``sha256``，再与调用方提供的**已登记版本行**核对受控
  ``documentVersion``，输出结构化核验结论。
- 返回结论只描述「本次只读核验的事实」：``status`` / ``versionVerified`` /
  ``changeOrder`` / 实际字节与摘要 / 两次读取对拍结果。**不返回**激活、验收、可派发、
  门禁通过等任何结论。

判定顺序（本卡冻结；序 1 优先于同版本冲突，同版本冲突优先于任何占位结论）：
1. 声明 / 允许根 / 来源类型 / 版本行形态校验（全部先于任何文件读取）；
2. 受控只读预检（复用冻结 ``preflight_source``）+ 第二次受控复读并两次对拍
   ``bytes`` / ``sha256``：任一变化 -> ``planning_source_drift``（序 1，不可改判）；
3. 同版本字节 / 摘要冲突核验（本卡前移）：声明版本原样非空字符串且与已登记版本精确相等
   时，``sha256`` / ``bytes`` 不同 -> ``planning_snapshot_conflict``（§16.4 硬拒）；
4. 结论判定：显式无版本（``None`` / 空字符串）-> ``missing``；精确等于
   ``unverified``（小写字面量，不折叠大小写）-> ``unverified``；两者都只产出验证结论，
   ``changeOrder`` = 序 3 且 ``appendCandidateEligible=False``；精确命中且字节一致 ->
   ``matched``；未命中任何已登记版本 -> ``new_version``（序 2 提示，本模块仍不追加）。

复用而不重造（本模块不复制、不弱化任何受控规则；依赖一律**直接 import**，加载失败即
``ImportError`` fail closed，**不退化**成本地实现）：
- 路径安全与受控读取复用冻结 ``service.planning_preflight.preflight_source``
  （绝对路径、``..``、符号链接、普通文件、凭据 / 私有目录拒绝、允许根、
  大小上限、UTF-8 解码，以及其自带的读取后对拍）；
- 实际字节摘要复用冻结 ``read_source_file`` 计算的 ``sha256`` / ``bytes``；
- 来源类型闭集复用 ``contracts.planning_digest.SOURCE_KINDS``；
- 漂移 / 冲突诊断码与 ``unverified`` 字面量、同版本冲突 ``reason`` 复用
  ``contracts.planning_revision`` 的冻结常量（不新造同义码、不改全局枚举）；
- ``sha256`` 文本形态判定复用冻结 ``contracts.planning_revision._is_sha256_text``，
  保证与 revision 层口径一致；本模块**不自行**做散列计算。

失败一律 fail closed（抛 :class:`PlanningSourceVersionError`，不返回乐观结论）：
- 声明形态 / 允许根 / 来源类型 / 版本行形态不合法 -> ``planning_source_version_invalid``；
- 路径越出允许根 -> ``planning_source_out_of_scope``；
- 相对路径 / ``..`` / 符号链接 / 凭据私有目录 -> ``planning_source_rejected``；
- 不存在 / 非普通文件 / 不可读 / 非 UTF-8 / 超限 -> ``planning_source_unreadable``；
- 同一批「读取前 -> 读取后」字节或摘要变化（含预检内部对拍失败、二次只读失败）
  -> ``planning_source_drift``（序 1，优先于同版本冲突与任何版本结论）；
- 声明版本与已登记版本相同但 ``sha256`` / ``bytes`` 不同 -> ``planning_snapshot_conflict``，
  ``reason=same_document_version_different_bytes``（§16.4 硬拒，声明不能豁免，不得改判成
  「来源更新」；030 起该判定前移到占位结论之前）。

本模块明确不做（一律不做，也不提供入口）：
- 不打开 / 不建 / 不迁移任何数据库，不接收连接参数；不写审计、不写快照、不追加
  revision、不建表、不改激活指针、不激活规划；
- 不生成任何受控编号（``snapshotId`` / ``snapshotRevision`` / ``planningSourceId`` /
  审计编号）与任何执行事实（task / attempt / delivery / review / event / Registry）；
- 不按标题、文件名、正文或相似度查找 / 猜测来源；``name`` 只作为**声明标签原样回显**，
  绝不参与路径解析（``resolvedFrom`` 恒为 ``declared_path``）；
- 不查登记库、不核实来源归属（登记核验属写入口职责，另卡）；不解析原文正文、
  不从正文猜 ``documentVersion``；
- 不归一化版本字符串（不 strip、不大小写折叠、不 Unicode 归一化），也不把
  ``UNVERIFIED`` 猜成 ``unverified``；
- 不决定 revision 是否追加：``unverified`` / 缺版本只产出「验证结论 + 序 3 提示」，
  ``wouldAppendRevision`` 恒为 ``False``；真正追加由 revision 保存入口另行受控确认。

状态：实现候选（030 返工）；Worker 本轮 bashCommands 为空，测试全部 **NOT_RUN**，
由 cox 独立运行并判定。完整 MVP / A 阶段**未完成**。
"""

from __future__ import annotations

import os

# 显式冻结依赖加载：以下三组 import 全部是 direct import，缺依赖即 ImportError
# fail closed；本模块没有任何导入兜底分支，绝不吞掉导入失败再退回本地实现
# （本地实现会弱化受控规则）。
from contracts.planning_digest import SOURCE_KINDS
from contracts.planning_revision import (
    ERROR_SNAPSHOT_CONFLICT,
    ERROR_SOURCE_DRIFT,
    UNVERIFIED_DOCUMENT_VERSION,
    REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
    # 只读复用冻结形态判定，保证 sha256 口径与 revision 层完全一致（不复制规则）。
    _is_sha256_text,
)
from service.planning_preflight import (
    DEFAULT_MAX_BYTES,
    DocumentContractError,
    PlanningPreflightError,
    preflight_source,
    read_source_file,
)

__all__ = [
    "CHANGE_ORDER_NONE",
    "CHANGE_ORDER_ORDER_1_DRIFT",
    "CHANGE_ORDER_ORDER_2_NEW_VERSION",
    "CHANGE_ORDER_ORDER_3_UNDECLARED",
    "DEFAULT_MAX_BYTES",
    "PLANNING_SOURCE_VERSION_ERROR_CODES",
    "PlanningSourceVersionError",
    "REGISTERED_VERSION_FIELDS",
    "RESOLVED_FROM_DECLARED_PATH",
    "SOURCE_DECLARATION_FIELDS",
    "STATUS_MATCHED",
    "STATUS_MISSING",
    "STATUS_NEW_VERSION",
    "STATUS_UNVERIFIED",
    "VERSION_STATUSES",
    "inspect_planning_source_version",
    "validate_planning_source_version",
]


# ---------------------------------------------------------------------------
# 本卡冻结的常量（不改全局 ERROR_CODES，不改任何既有服务行为）
# ---------------------------------------------------------------------------

#: 声明必须**显式**给出的四个字段；缺任何一个都拒绝（不允许按标题 / 文件名补猜来源）。
SOURCE_DECLARATION_FIELDS = ("path", "sourceKind", "name", "documentVersion")

#: 已登记版本行只允许这三个字段；自报编号 / 身份 / 摘要以外的字段一律拒绝。
REGISTERED_VERSION_FIELDS = ("documentVersion", "sha256", "bytes")

#: 版本核验结论（只描述本次只读事实，不是执行状态）。
STATUS_MATCHED = "matched"          # 声明版本与已登记版本同版本且字节 / 摘要一致
STATUS_NEW_VERSION = "new_version"  # 声明了明确的新版本（与所有已登记版本都不同）
STATUS_MISSING = "missing"          # 调用方显式声明「无版本」（None / 空字符串）
STATUS_UNVERIFIED = "unverified"    # 调用方显式声明字面量 unverified（精确小写）

VERSION_STATUSES = (
    STATUS_MATCHED,
    STATUS_NEW_VERSION,
    STATUS_MISSING,
    STATUS_UNVERIFIED,
)

#: §16.1 变化顺序标签：只表达「本批看到的是哪种来源变化」，不授权任何写入。
CHANGE_ORDER_NONE = "none"                                    # 与已登记版本一致，无变化
CHANGE_ORDER_ORDER_1_DRIFT = "order_1_in_batch_drift"         # 序 1：批内漂移，优先拒绝
CHANGE_ORDER_ORDER_2_NEW_VERSION = "order_2_new_version"      # 序 2：明确声明新版本
CHANGE_ORDER_ORDER_3_UNDECLARED = "order_3_undeclared"        # 序 3：未声明 / unverified

#: 路径只从声明的 ``path`` 解析；本模块没有任何按名称查找的路径。
RESOLVED_FROM_DECLARED_PATH = "declared_path"

#: 稳定 reason（本卡冻结，仅用于诊断；不回显正文 / 标题 / 文件内容）。
REASON_DECLARATION_NOT_OBJECT = "declaration_not_object"
REASON_MIXED_DECLARATION_FORMS = "mixed_declaration_forms"
REASON_UNKNOWN_FIELD = "unknown_field"
REASON_MISSING_FIELD = "missing_field"
REASON_FIELD_TYPE = "field_type_invalid"
REASON_ALLOWED_ROOTS_NOT_CONFIGURED = "allowed_roots_not_configured"
REASON_ALLOWED_ROOT_INVALID = "allowed_root_invalid"
REASON_MAX_BYTES_INVALID = "max_bytes_invalid"
REASON_REGISTERED_NOT_LIST = "registered_versions_not_list"
REASON_REGISTERED_ROW_NOT_OBJECT = "registered_version_row_not_object"
REASON_DUPLICATE_DOCUMENT_VERSION = "duplicate_document_version"
REASON_PREFLIGHT_STATUS_UNKNOWN = "preflight_status_unknown"
REASON_PREFLIGHT_REJECTED = "preflight_not_read"
REASON_SECOND_READ_FAILED = "second_controlled_read_failed"
REASON_READS_DISAGREE = "bytes_or_sha256_changed_between_reads"

#: 预检结论 -> 本模块错误码（未列出的状态一律按合同错误拒绝，不猜结论）。
_CODE_BY_PREFLIGHT_STATUS = {
    "pending": "planning_source_version_invalid",
    "out_of_scope": "planning_source_out_of_scope",
    "rejected": "planning_source_rejected",
    "unreadable": "planning_source_unreadable",
    "drift": ERROR_SOURCE_DRIFT,
}

PLANNING_SOURCE_VERSION_ERROR_CODES = {
    "planning_source_version_invalid": (
        "声明 / 允许根 / 来源类型 / 版本行形态不符合受控形态，拒绝且不读取文件"
    ),
    "planning_source_out_of_scope": "声明的来源路径越出受控允许根，拒绝读取",
    "planning_source_rejected": (
        "相对路径、`..` 逃逸、符号链接或凭据 / 私有目录，明确拒绝读取"
    ),
    "planning_source_unreadable": (
        "不存在 / 不是普通文件 / 不可读 / 非 UTF-8 / 超出大小上限，拒绝继续核验"
    ),
    ERROR_SOURCE_DRIFT: (
        "同一批内来源字节或 sha256 发生变化（序 1），优先拒绝：不保存快照、不追加 revision"
    ),
    ERROR_SNAPSHOT_CONFLICT: (
        "声明版本与已登记版本相同但 sha256 / bytes 不同，硬冲突且声明不能豁免"
    ),
}

#: 未显式传入时的占位（用于区分「显式传 None」与「根本没传」）。
_UNSET = object()


class PlanningSourceVersionError(ValueError):
    """来源版本核验被拒绝时抛出的错误。

    调用方必须 fail closed：不保存快照、不追加 revision、不激活、不写任何执行事实，
    也不自动重试。``details`` 只保留稳定 ``reason``、字段路径、字节数与摘要，不回显
    正文 / 标题 / 文件内容。
    """

    def __init__(self, code, message=None, details=None):
        if code not in PLANNING_SOURCE_VERSION_ERROR_CODES:
            raise ValueError("unknown planning source version error code: %r" % (code,))
        self.code = code
        self.details = dict(details) if details else {}
        super(PlanningSourceVersionError, self).__init__(
            message or PLANNING_SOURCE_VERSION_ERROR_CODES[code]
        )

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def _fail(code, message, reason, path, extra=None):
    details = {"reason": reason, "path": path}
    if extra:
        details.update(extra)
    raise PlanningSourceVersionError(code, message=message, details=details)


# ---------------------------------------------------------------------------
# 零副作用声明（每次返回都逐项声明未发生，而不是省略字段）
# ---------------------------------------------------------------------------


def _no_side_effects():
    return {
        "databaseOpened": False,
        "databaseCreated": False,
        "schemaMigrated": False,
        "snapshotWritten": False,
        "snapshotRevisionAppended": False,
        "revisionAppended": False,
        "auditWritten": False,
        "activationPointerChanged": False,
        "planningActivated": False,
        "sourceFileWritten": False,
        "taskCreated": False,
        "attemptCreated": False,
        "eventWritten": False,
        "deliveryWritten": False,
        "reviewWritten": False,
        "registryWritten": False,
        "hostCalled": False,
    }


def _no_write_conclusion():
    return {
        "wouldSave": False,
        "wouldAppendRevision": False,
        "wouldModifySnapshot": False,
        "wouldActivate": False,
        "generatedIdentifiers": [],
        "executionFacts": [],
    }


# ---------------------------------------------------------------------------
# 1) 声明形态与受控配置（全部先于任何文件读取）
# ---------------------------------------------------------------------------


def _declaration_from_arguments(registered_source, path, source_kind, name,
                                document_version):
    """把两种等价写法收敛成一个声明：整对象 **或** 四个显式关键字，不允许混用。

    - 整对象：``{"path", "sourceKind", "name", "documentVersion"}``，未知字段整体拒绝；
    - 关键字：四个必须全部显式给出；``documentVersion=None`` 表示**显式声明无版本**，
      与「根本没传」严格区分（后者拒绝）。
    """
    keyword_form = [path, source_kind, name, document_version]
    keyword_given = [value is not _UNSET for value in keyword_form]

    if registered_source is not None and any(keyword_given):
        _fail(
            "planning_source_version_invalid",
            "声明只允许一种写法：整对象或四个显式关键字，混用一律拒绝",
            REASON_MIXED_DECLARATION_FORMS,
            "$.registeredSource",
        )

    if registered_source is not None:
        if not isinstance(registered_source, dict):
            _fail(
                "planning_source_version_invalid",
                "已登记来源声明必须是对象",
                REASON_DECLARATION_NOT_OBJECT,
                "$.registeredSource",
                {"type": type(registered_source).__name__},
            )
        for key in registered_source:
            if not isinstance(key, str):
                _fail(
                    "planning_source_version_invalid",
                    "已登记来源声明的键必须是字符串",
                    REASON_UNKNOWN_FIELD,
                    "$.registeredSource",
                )
            if key not in SOURCE_DECLARATION_FIELDS:
                _fail(
                    "planning_source_version_invalid",
                    "已登记来源声明含未知字段（自报摘要 / 编号 / 身份一律拒绝）",
                    REASON_UNKNOWN_FIELD,
                    "$.registeredSource." + key,
                )
        for field in SOURCE_DECLARATION_FIELDS:
            if field not in registered_source:
                _fail(
                    "planning_source_version_invalid",
                    "已登记来源声明缺少必需字段（必须显式给出，不按标题猜来源）",
                    REASON_MISSING_FIELD,
                    "$.registeredSource." + field,
                )
        return (
            registered_source["path"],
            registered_source["sourceKind"],
            registered_source["name"],
            registered_source["documentVersion"],
        )

    # 关键字写法：四个必须全部显式给出（documentVersion 可显式为 None）。
    for field, value, given in zip(
        SOURCE_DECLARATION_FIELDS, keyword_form, keyword_given
    ):
        if not given:
            _fail(
                "planning_source_version_invalid",
                "关键字写法必须显式传入全部四个字段（缺字段不按标题猜来源）",
                REASON_MISSING_FIELD,
                "$.registeredSource." + field,
            )
    return path, source_kind, name, document_version


def _validate_declaration(declared_path, source_kind, name, document_version):
    """校验四个字段类型：路径 / 来源类型 / 名称非空字符串；版本允许 None 或字符串。"""
    for field, value in (("path", declared_path), ("sourceKind", source_kind),
                         ("name", name)):
        if not isinstance(value, str):
            _fail(
                "planning_source_version_invalid",
                "%s 必须是字符串" % field,
                REASON_FIELD_TYPE,
                "$.registeredSource." + field,
                {"type": type(value).__name__},
            )
        if value == "":
            _fail(
                "planning_source_version_invalid",
                "%s 必须是非空字符串" % field,
                REASON_FIELD_TYPE,
                "$.registeredSource." + field,
            )

    if source_kind not in SOURCE_KINDS:
        _fail(
            "planning_source_version_invalid",
            "sourceKind 不在受控闭集内，拒绝",
            REASON_FIELD_TYPE,
            "$.registeredSource.sourceKind",
            {"sourceKind": source_kind, "allowed": sorted(SOURCE_KINDS)},
        )

    # 版本：允许 None（显式无版本）或字符串；bool / 数字 / 容器一律拒绝。
    # 字符串原样保留，不 strip、不大小写折叠（版本不归一化）。
    if document_version is not None and not isinstance(document_version, str):
        _fail(
            "planning_source_version_invalid",
            "documentVersion 必须是字符串或 None（显式声明无版本）",
            REASON_FIELD_TYPE,
            "$.registeredSource.documentVersion",
            {"type": type(document_version).__name__},
        )

    return declared_path, source_kind, name, document_version


def _validate_allowed_roots(allowed_roots):
    """受控允许根只来自受控函数参数；缺配置或不合法一律拒绝，不从声明取。"""
    if not isinstance(allowed_roots, (list, tuple)) or not allowed_roots:
        _fail(
            "planning_source_version_invalid",
            "缺少受控允许根（必须由受控函数参数注入，不来自声明）",
            REASON_ALLOWED_ROOTS_NOT_CONFIGURED,
            "$.allowedRoots",
        )
    roots = []
    for position in range(len(allowed_roots)):
        entry = allowed_roots[position]
        if not isinstance(entry, str) or entry == "" or not entry.startswith("/"):
            _fail(
                "planning_source_version_invalid",
                "受控允许根必须是非空绝对路径字符串",
                REASON_ALLOWED_ROOT_INVALID,
                "$.allowedRoots[%d]" % position,
            )
        roots.append(entry)
    return roots


def _validate_max_bytes(max_bytes):
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        _fail(
            "planning_source_version_invalid",
            "max_bytes 必须是正整数",
            REASON_MAX_BYTES_INVALID,
            "$.maxBytes",
            {"maxBytes": max_bytes},
        )
    return max_bytes


# ---------------------------------------------------------------------------
# 2) 已登记版本行形态（不查登记库：只校验调用方显式提供的行）
# ---------------------------------------------------------------------------


def _validate_registered_versions(registered_versions):
    """校验已登记版本行：字段白名单 + 精确类型 + 版本唯一 + sha256 形态。

    同 ``documentVersion`` 出现多行即结构无效，一律拒绝且不猜哪一行（与 revision 层
    「全历史唯一」口径一致）。本模块只读入参，不做任何登记查询。
    """
    if registered_versions is None:
        return []
    if not isinstance(registered_versions, (list, tuple)):
        _fail(
            "planning_source_version_invalid",
            "registered_versions 必须是已登记版本行数组",
            REASON_REGISTERED_NOT_LIST,
            "$.registeredVersions",
            {"type": type(registered_versions).__name__},
        )

    rows = []
    seen_versions = set()
    for position in range(len(registered_versions)):
        row = registered_versions[position]
        row_path = "$.registeredVersions[%d]" % position
        if not isinstance(row, dict):
            _fail(
                "planning_source_version_invalid",
                "每一已登记版本行必须是对象",
                REASON_REGISTERED_ROW_NOT_OBJECT,
                row_path,
            )
        for key in row:
            if not isinstance(key, str) or key not in REGISTERED_VERSION_FIELDS:
                _fail(
                    "planning_source_version_invalid",
                    "已登记版本行含未知字段（编号 / 身份 / 其他一律拒绝）",
                    REASON_UNKNOWN_FIELD,
                    row_path + "." + str(key),
                )
        for field in REGISTERED_VERSION_FIELDS:
            if field not in row:
                _fail(
                    "planning_source_version_invalid",
                    "已登记版本行缺少必需字段",
                    REASON_MISSING_FIELD,
                    row_path + "." + field,
                )

        version = row["documentVersion"]
        if not isinstance(version, str) or version == "":
            _fail(
                "planning_source_version_invalid",
                "已登记版本行的 documentVersion 必须是非空字符串",
                REASON_FIELD_TYPE,
                row_path + ".documentVersion",
            )
        if not _is_sha256_text(row["sha256"]):
            _fail(
                "planning_source_version_invalid",
                "已登记版本行的 sha256 形态必须是 sha256:<64位小写十六进制>",
                REASON_FIELD_TYPE,
                row_path + ".sha256",
            )
        source_bytes = row["bytes"]
        if isinstance(source_bytes, bool) or not isinstance(source_bytes, int):
            _fail(
                "planning_source_version_invalid",
                "已登记版本行的 bytes 必须是非负整数（非布尔）",
                REASON_FIELD_TYPE,
                row_path + ".bytes",
            )
        if source_bytes < 0:
            _fail(
                "planning_source_version_invalid",
                "已登记版本行的 bytes 必须 >= 0",
                REASON_FIELD_TYPE,
                row_path + ".bytes",
            )

        if version in seen_versions:
            _fail(
                "planning_source_version_invalid",
                "已登记版本行出现重复 documentVersion，拒绝且不猜哪一行",
                REASON_DUPLICATE_DOCUMENT_VERSION,
                row_path + ".documentVersion",
            )
        seen_versions.add(version)
        rows.append(
            {"documentVersion": version, "sha256": row["sha256"], "bytes": source_bytes}
        )
    return rows


# ---------------------------------------------------------------------------
# 3) 受控只读：预检 + 再次受控复读（两次对拍 bytes / sha256）
# ---------------------------------------------------------------------------


def _controlled_read(path, roots, max_bytes):
    """受控只读一次：复用冻结 ``read_source_file``（允许根 / 符号链接 / 普通文件 /
    大小上限 / UTF-8 解码），只返回 ``path`` / ``bytes`` / ``sha256``。

    不写任何文件、不建库、不生成编号；正文只用于满足受控解码校验，不参与返回。
    """
    meta, _text = read_source_file(path, allowed_roots=roots, max_bytes=max_bytes)
    return {"path": meta["path"], "bytes": meta["bytes"], "sha256": meta["sha256"]}


def _preflight_declared_source(declared_path, roots, max_bytes):
    """复用冻结 ``preflight_source``；结论不是 ``read`` 一律 fail closed。"""
    try:
        result = preflight_source({"path": declared_path}, roots, max_bytes)
    except PlanningPreflightError as exc:
        raise PlanningSourceVersionError(
            "planning_source_version_invalid",
            message="来源只读预检入参 / 允许根不合法，拒绝核验：%s" % exc,
            details={
                "reason": REASON_PREFLIGHT_REJECTED,
                "path": "$.registeredSource.path",
                "cause": getattr(exc, "to_dict", lambda: {"code": type(exc).__name__})(),
            },
        ) from exc

    status = result.get("status")
    if status == "read":
        return result

    code = _CODE_BY_PREFLIGHT_STATUS.get(status)
    if code is None:
        _fail(
            "planning_source_version_invalid",
            "只读预检返回未知结论，拒绝且不猜结论",
            REASON_PREFLIGHT_STATUS_UNKNOWN,
            "$.registeredSource.path",
            {"preflightStatus": status, "preflightReason": result.get("reason")},
        )
    raise PlanningSourceVersionError(
        code,
        message="来源只读预检未通过（status=%s），拒绝核验且不写任何状态" % status,
        details={
            "reason": result.get("reason") or REASON_PREFLIGHT_REJECTED,
            "path": "$.registeredSource.path",
            "declaredPath": declared_path,
            "preflightStatus": status,
            "absolutePath": result.get("absolutePath"),
            "regularFile": result.get("regularFile"),
            "drift": result.get("drift"),
        },
    )


def _verify_two_reads(actual_path, roots, max_bytes, first):
    """第二次受控复读并与第一次对拍：``bytes`` / ``sha256`` 任一变化即序 1 漂移。

    复用预检自带的读取后对拍（第一次），本函数提供第二次独立受控复读；两次都不写文件。
    序 1 漂移优先于同版本冲突与任何版本结论，不可改判。
    """
    try:
        second = _controlled_read(actual_path, roots, max_bytes)
    except (DocumentContractError, OSError) as exc:
        raise PlanningSourceVersionError(
            ERROR_SOURCE_DRIFT,
            message="第二次受控复读失败，按序 1 来源漂移拒绝（不保存快照、不追加 revision）",
            details={
                "reason": REASON_SECOND_READ_FAILED,
                "path": "$.registeredSource.path",
                "order": CHANGE_ORDER_ORDER_1_DRIFT,
                "first": dict(first),
                "readbackError": type(exc).__name__,
            },
        ) from exc

    if second["bytes"] != first["bytes"] or second["sha256"] != first["sha256"]:
        raise PlanningSourceVersionError(
            ERROR_SOURCE_DRIFT,
            message=(
                "读取期间 bytes / sha256 发生变化，按序 1 来源漂移拒绝："
                "不保存快照、不追加 revision、不更新展示"
            ),
            details={
                "reason": REASON_READS_DISAGREE,
                "path": "$.registeredSource.path",
                "order": CHANGE_ORDER_ORDER_1_DRIFT,
                "first": dict(first),
                "second": dict(second),
            },
        )
    return second


# ---------------------------------------------------------------------------
# 4) 版本判定（冲突优先；不归一化；unverified / 缺版本只给结论）
# ---------------------------------------------------------------------------


def _find_registered_version(registered, document_version):
    """按**原样字符串**精确匹配已登记版本行；不 strip、不折叠大小写、不 Unicode 归一化。

    校验阶段已保证版本行不重复，故命中至多一行。
    """
    for row in registered:
        if row["documentVersion"] == document_version:
            return row
    return None


def _raise_same_version_byte_conflict(document_version, matched, actual_sha256,
                                      actual_bytes, actual_path):
    """同 ``documentVersion`` 但 ``sha256`` / ``bytes`` 不同：§16.4 硬冲突拒绝。

    声明不能豁免，也不得改判成「来源更新」；``unverified`` 占位同样不豁免。
    """
    raise PlanningSourceVersionError(
        ERROR_SNAPSHOT_CONFLICT,
        message=(
            "声明版本与已登记版本相同但 sha256 / bytes 不同，硬冲突且声明不能豁免："
            "拒绝，不保存快照、不追加 revision，也不得改判成来源更新"
        ),
        details={
            "reason": REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
            "path": "$.registeredSource.documentVersion",
            "declaredDocumentVersion": document_version,
            # 本卡（030）新增审计标记：该判定发生在占位结论（missing / unverified）之前。
            "rejectedBeforePlaceholderConclusion": True,
            "registered": dict(matched),
            "actual": {
                "path": actual_path,
                "sha256": actual_sha256,
                "bytes": actual_bytes,
            },
        },
    )


def _classify_version(document_version, registered, actual_sha256, actual_bytes,
                      actual_path):
    """返回 (status, versionVerified, changeOrder, matchedRow, appendCandidateEligible,
    sameVersionBytesChecked)。

    判定顺序（030 修复后；序 1 漂移已在调用前处理）：
    0. **同版本字节 / 摘要冲突核验（前移，本卡修复）**：声明版本是原样非空字符串且与某
       已登记版本行精确相等时，先比 ``sha256`` / ``bytes``；不同 -> 硬拒
       ``planning_snapshot_conflict``。该步先于 ``missing`` / ``unverified`` 占位结论，
       占位版本不是同版本字节冲突的豁免。
    1. 显式无版本（None / 空字符串）-> ``missing``：只给结论，序 3，不自动追加；
    2. 精确等于 ``unverified``（不折叠大小写）-> ``unverified``：同上；即使同版本字节
       一致也不升级为 ``matched`` / 已验证（占位版本不构成可核验版本）；
    3. 精确命中且字节 / 摘要一致 -> ``matched``，结论已验证；
    4. 未命中任何已登记版本 -> ``new_version``（序 2 提示；本模块仍不追加）。
    """
    declared_is_string_version = (
        isinstance(document_version, str) and document_version != ""
    )

    matched = None
    if declared_is_string_version:
        matched = _find_registered_version(registered, document_version)
        if matched is not None and (
            matched["sha256"] != actual_sha256 or matched["bytes"] != actual_bytes
        ):
            _raise_same_version_byte_conflict(
                document_version, matched, actual_sha256, actual_bytes, actual_path
            )

    if document_version is None or document_version == "":
        return STATUS_MISSING, False, CHANGE_ORDER_ORDER_3_UNDECLARED, None, False, False

    if document_version == UNVERIFIED_DOCUMENT_VERSION:
        return (
            STATUS_UNVERIFIED,
            False,
            CHANGE_ORDER_ORDER_3_UNDECLARED,
            None,
            False,
            declared_is_string_version,
        )

    if matched is not None:
        return STATUS_MATCHED, True, CHANGE_ORDER_NONE, matched, False, True

    # 序 2 只是「本批看到明确新版本」的提示；是否追加由 revision 保存入口另行判定。
    return (
        STATUS_NEW_VERSION,
        False,
        CHANGE_ORDER_ORDER_2_NEW_VERSION,
        None,
        True,
        declared_is_string_version,
    )


# ---------------------------------------------------------------------------
# 5) 公开只读入口
# ---------------------------------------------------------------------------


def _validate(registered_source, allowed_roots, path, source_kind, name,
              document_version, registered_versions, max_bytes):
    raw = _declaration_from_arguments(
        registered_source, path, source_kind, name, document_version
    )
    declared_path, declared_kind, declared_name, declared_version = _validate_declaration(
        raw[0], raw[1], raw[2], raw[3]
    )
    roots = _validate_allowed_roots(allowed_roots)
    limit = _validate_max_bytes(max_bytes)
    rows = _validate_registered_versions(registered_versions)

    # 1) 只读预检：路径形态、允许根、普通文件、凭据拒绝、读取后对拍（冻结实现）。
    preflight = _preflight_declared_source(declared_path, roots, limit)
    actual_path = preflight.get("absolutePath")
    first_bytes = preflight.get("bytes")
    first_sha256 = preflight.get("sha256")
    if (
        not isinstance(actual_path, str)
        or not actual_path
        or not isinstance(first_sha256, str)
        or not first_sha256
        or isinstance(first_bytes, bool)
        or not isinstance(first_bytes, int)
    ):
        _fail(
            "planning_source_unreadable",
            "只读预检结论为 read 但缺少实际路径 / 字节 / 摘要，拒绝且不猜",
            REASON_PREFLIGHT_REJECTED,
            "$.registeredSource.path",
            {"absolutePath": actual_path},
        )

    first = {
        "path": actual_path,
        "bytes": first_bytes,
        "sha256": first_sha256,
        "readIndex": 1,
        "source": "planning_preflight",
    }

    # 2) 第二次受控复读 + 两次对拍（序 1 漂移优先于同版本冲突与任何版本结论）。
    second_read = _verify_two_reads(actual_path, roots, limit, first)
    second = {
        "path": second_read["path"],
        "bytes": second_read["bytes"],
        "sha256": second_read["sha256"],
        "readIndex": 2,
        "source": "controlled_readback",
    }

    # 3) 版本判定：同版本字节冲突优先，其次 missing / unverified 占位，再命中 / 新版本。
    (
        status,
        verified,
        change_order,
        matched,
        eligible,
        same_version_bytes_checked,
    ) = _classify_version(
        declared_version, rows, first_sha256, first_bytes, actual_path
    )

    recommended = {
        STATUS_MATCHED: "registered_version_consistent",
        STATUS_NEW_VERSION: "new_batch_revision_decision_required",
        STATUS_MISSING: "controlled_version_declaration_required",
        STATUS_UNVERIFIED: "controlled_version_declaration_required",
    }[status]

    result = {
        "checked": True,
        "status": status,
        "versionVerified": verified,
        "declaredDocumentVersion": declared_version,
        "documentVersionNormalized": False,
        "registeredVersionsChecked": len(rows),
        # 本卡（030）新增：同版本字节 / 摘要核验是否适用于本次声明（占位结论不豁免）。
        "sameVersionBytesChecked": same_version_bytes_checked,
        "sameVersionConflictCheckedFirst": True,
        "matchedRegisteredVersion": dict(matched) if matched else None,
        "changeOrder": change_order,
        "appendCandidateEligible": eligible,
        "recommendedAction": recommended,
        "resolvedFrom": RESOLVED_FROM_DECLARED_PATH,
        "nameUsedForLookup": False,
        "source": {
            "path": actual_path,
            "declaredPath": declared_path,
            "sourceKind": declared_kind,
            "declaredName": declared_name,
            "fileName": os.path.basename(actual_path),
            "bytes": first_bytes,
            "sha256": first_sha256,
        },
        "reads": {
            "count": 2,
            "bytesStable": True,
            "sha256Stable": True,
            "first": first,
            "second": second,
        },
    }
    result.update(_no_write_conclusion())
    result["sideEffects"] = _no_side_effects()
    return result


def validate_planning_source_version(registered_source=None, allowed_roots=None, *,
                                     path=_UNSET, sourceKind=_UNSET, name=_UNSET,
                                     documentVersion=_UNSET,
                                     registered_versions=None,
                                     max_bytes=DEFAULT_MAX_BYTES):
    """只读核验一份已登记规划来源的版本：返回结论 dict，失败即抛结构化错误。

    - ``registered_source``：声明对象，键固定为 ``path`` / ``sourceKind`` / ``name`` /
      ``documentVersion``（四个都必须显式给出）；或用四个显式关键字写法，二选一、不许混用。
    - ``allowed_roots``：受控允许根，只来自本函数参数。
    - ``documentVersion``：受控声明版本，**原样使用、不归一化**；显式传 ``None`` 表示
      「声明无版本」（只产出结论，不自动追加 revision），与「没传」严格区分。
    - ``registered_versions``：调用方显式提供的已登记版本行（``documentVersion`` /
      ``sha256`` / ``bytes``），只用于同版本摘要冲突判定；本模块不查登记库。
    - ``max_bytes``：受控读取上限，默认沿用冻结只读预检口径。

    返回值只描述本次只读核验事实（``status`` / ``versionVerified`` / ``changeOrder`` /
    实际 ``bytes`` / ``sha256`` / 两次读取对拍结果 / 零副作用声明），**不含**任何编号、
    激活、验收或可派发结论。失败一律抛 :class:`PlanningSourceVersionError`，调用方必须
    fail closed。
    """
    return _validate(
        registered_source,
        allowed_roots,
        path,
        sourceKind,
        name,
        documentVersion,
        registered_versions,
        max_bytes,
    )


def inspect_planning_source_version(registered_source=None, allowed_roots=None, *,
                                    path=_UNSET, sourceKind=_UNSET, name=_UNSET,
                                    documentVersion=_UNSET,
                                    registered_versions=None,
                                    max_bytes=DEFAULT_MAX_BYTES):
    """与 :func:`validate_planning_source_version` 同口径，但**不抛异常**。

    失败时返回 ``status`` = 稳定错误码（例如 ``planning_source_drift`` /
    ``planning_snapshot_conflict``）并保留 ``error`` 结构化详情；无论成功或失败，
    ``wouldSave`` / ``wouldAppendRevision`` / ``wouldActivate`` 恒为 ``False``，
    ``sideEffects`` 逐项声明未发生。只读，不写库、不落盘、不生成编号。
    """
    try:
        return validate_planning_source_version(
            registered_source,
            allowed_roots,
            path=path,
            sourceKind=sourceKind,
            name=name,
            documentVersion=documentVersion,
            registered_versions=registered_versions,
            max_bytes=max_bytes,
        )
    except PlanningSourceVersionError as exc:
        if documentVersion is not _UNSET:
            declared = documentVersion
        elif isinstance(registered_source, dict):
            declared = registered_source.get("documentVersion")
        else:
            declared = None
        result = {
            "checked": True,
            "status": exc.code,
            "versionVerified": False,
            "declaredDocumentVersion": declared,
            "documentVersionNormalized": False,
            "changeOrder": None,
            "appendCandidateEligible": False,
            "error": exc.to_dict(),
        }
        result.update(_no_write_conclusion())
        result["sideEffects"] = _no_side_effects()
        return result
