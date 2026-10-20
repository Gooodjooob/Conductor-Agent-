"""planning-snapshot.v1 结构、唯一 ID 与显式引用关系纯校验（第 010 轮候选）。

契约来源：
- ``evidence/structure-contract.md``（§15.2 / §15.2.1 / §15.2.2 / §15.2.3 / §15.3）
- 任务卡 ``docs/development/task-cards/2026-10-10-d4-mvp20-planning-structure-codebuddy.md``
- 冻结摘要函数 ``contracts/planning_digest.py``（只读复用，不复制、不弱化）

本卡范围（严格限定）：
- 只做 ``sections`` / ``stages`` / ``businessLines`` / ``milestones`` 四类对象的字段
  白名单、项目内同类型唯一 ID、显式引用关系校验；摘要检查直接复用已审核函数。
- 不做任何文件 / 网络 / 数据库 / 环境访问；导入时无副作用；只读入参、不改入参。
- 不登记项目、不登记来源、不生成任何编号（快照 / 任务 / 尝试 / 审核 / 派发 /
  审核），不推进状态、不激活快照、不派发、不落盘、不写业务数据。
- ``expected_project_id`` / ``expected_planning_source_id`` 由受控上层提供；本函数只做
  **精确字符串匹配**，不查询项目登记库、不核实来源登记，也不授予任何身份权限。
- 仅凭 snapshot 内部引用与 expected 项目一致**不能证明**外部多项目数据库隔离；
  跨项目硬错误只能覆盖「快照内自证」的部分。

返回 dict：
- ``valid``：本卡恒为 ``True``（失败一律抛 :class:`PlanningDigestError`）。
- ``payload``：``digest_payload`` 的**独立深拷贝**规范载荷。
- ``canonicalDigest``：复用 ``compute_canonical_digest`` 计算，不重复摘要逻辑。
- ``diagnostics``：按数组出现顺序的**缺映射**路径列表（非致命，不进 payload、不参与摘要）。
- ``wouldWrite`` / ``wouldDispatch`` / ``wouldActivate``：恒为 ``False``。

失败沿用 ``PlanningDigestError(code, message, details)``；``details`` 只保留字段路径，
不回显正文、标题或 ID 内容。诊断码：
- ``planning_structure_invalid``：字段 / 类型 / 白名单 / ID 形态 / 受控期望参数非法
- ``planning_cross_project_reference``：``projectId`` 与受控期望不一致
- ``planning_source_mismatch``：``planningSourceId`` 与受控期望不一致
- ``planning_mapping_missing``：**非致命**缺映射诊断
- ``planning_mapping_invalid``：引用形态合法但目标不存在（dangling）
- ``planning_mapping_ambiguous``：同一对象类型内 ID 重复
"""

import re

from contracts.planning_digest import (
    PlanningDigestError,
    compute_canonical_digest,
    digest_payload,
)

__all__ = ["validate_planning_structure"]

# ---- 诊断码（本卡冻结） ----------------------------------------------------

ERROR_INVALID = "planning_structure_invalid"
ERROR_CROSS_PROJECT = "planning_cross_project_reference"
ERROR_SOURCE_MISMATCH = "planning_source_mismatch"
ERROR_MAPPING_MISSING = "planning_mapping_missing"
ERROR_MAPPING_INVALID = "planning_mapping_invalid"
ERROR_MAPPING_AMBIGUOUS = "planning_mapping_ambiguous"

# ---- 冻结形态 --------------------------------------------------------------

# ID 形态冻结：首字符字母/数字，其余字母/数字/_ . -，总长 1..128。
_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")

_MIN_LEVEL = 1
_MAX_LEVEL = 6

_SECTION_REQUIRED = ("sectionId", "title", "anchor", "level")
_SECTION_ALLOWED = frozenset(_SECTION_REQUIRED)

_STAGE_REQUIRED = ("stageId", "title", "summary")
_STAGE_ALLOWED = frozenset(("stageId", "title", "summary", "sectionRef"))

_BUSINESS_LINE_REQUIRED = ("businessLineId", "title", "planStatus")
_BUSINESS_LINE_ALLOWED = frozenset(
    ("businessLineId", "title", "planStatus", "stageRef", "sectionRef")
)

_MILESTONE_REQUIRED = ("milestoneId", "title", "gateCriteria", "planStatus")
_MILESTONE_ALLOWED = frozenset(
    ("milestoneId", "title", "gateCriteria", "planStatus", "businessLineRef", "sectionRef")
)


# ---- 基础工具 --------------------------------------------------------------


def _fail(code, message, path):
    raise PlanningDigestError(code, message, {"path": path})


def _join(base, key):
    return base + "." + key


def _position_path(base, position):
    return "{0}[{1}]".format(base, position)


def _require_expected_identifier(value, label):
    """受控期望身份必须是「非空、合法形态」的字符串 ID。

    只做字符串形态校验；**不**查询登记库、**不**核实来源登记。
    """
    path = "$." + label
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "{0} 必须是字符串".format(label), path)
    if value == "":
        _fail(ERROR_INVALID, "{0} 必须是非空字符串".format(label), path)
    if _ID_PATTERN.fullmatch(value) is None:
        _fail(
            ERROR_INVALID,
            "{0} 形态必须是 [A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}".format(label),
            path,
        )


def _require_valid_id(value, path, label):
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "{0} 必须是字符串".format(label), path)
    if _ID_PATTERN.fullmatch(value) is None:
        _fail(
            ERROR_INVALID,
            "{0} 形态必须是 [A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}".format(label),
            path,
        )


def _require_text(value, path, label, allow_empty):
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "{0} 必须是字符串".format(label), path)
    if not allow_empty and value == "":
        _fail(ERROR_INVALID, "{0} 必须是非空字符串".format(label), path)


def _require_level(value, path):
    # 显式排除 bool：``True`` / ``False`` 是 ``int`` 子类，但不是层级整数。
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(ERROR_INVALID, "section.level 必须是非布尔整数", path)
    if value < _MIN_LEVEL or value > _MAX_LEVEL:
        _fail(ERROR_INVALID, "section.level 必须在 1..6（含）之间", path)


def _require_gate_criteria(value, path):
    """``gateCriteria`` 接受非空字符串或非空字符串数组；保留原类型 / 顺序，不互转。"""
    if isinstance(value, str):
        if value == "":
            _fail(ERROR_INVALID, "milestone.gateCriteria 不能是空字符串", path)
        return
    if isinstance(value, list):
        if len(value) == 0:
            _fail(ERROR_INVALID, "milestone.gateCriteria 不能是空数组", path)
        for position, item in enumerate(value):
            if not isinstance(item, str) or item == "":
                _fail(
                    ERROR_INVALID,
                    "milestone.gateCriteria 数组元素必须是非空字符串",
                    _position_path(path, position),
                )
        return
    _fail(ERROR_INVALID, "milestone.gateCriteria 必须是非空字符串或非空字符串数组", path)


def _check_object_shape(item, path, required_fields, allowed_fields):
    if not isinstance(item, dict):
        _fail(ERROR_INVALID, "数组项必须是 JSON 对象", path)
    for key in item:
        if not isinstance(key, str):
            _fail(ERROR_INVALID, "对象键必须是字符串", path)
        if key not in allowed_fields:
            # 含对象级 projectId / sourceId 等任何多余字段。
            _fail(ERROR_INVALID, "不允许的对象字段", _join(path, key))
    for field in required_fields:
        if field not in item:
            _fail(ERROR_INVALID, "缺少必需字段", _join(path, field))


# ---- 四类对象索引（字段白名单 + 项目内同类型唯一 ID） ------------------------


def _index_sections(snapshot):
    ids = set()
    for position, item in enumerate(snapshot["sections"]):
        path = _position_path("$.sections", position)
        _check_object_shape(item, path, _SECTION_REQUIRED, _SECTION_ALLOWED)
        id_value = item["sectionId"]
        id_path = _join(path, "sectionId")
        _require_valid_id(id_value, id_path, "section.sectionId")
        _require_text(item["title"], _join(path, "title"), "section.title", False)
        _require_text(item["anchor"], _join(path, "anchor"), "section.anchor", False)
        _require_level(item["level"], _join(path, "level"))
        if id_value in ids:
            _fail(ERROR_MAPPING_AMBIGUOUS, "同一项目内 sectionId 重复", id_path)
        ids.add(id_value)
    return ids


def _index_stages(snapshot):
    ids = set()
    for position, item in enumerate(snapshot["stages"]):
        path = _position_path("$.stages", position)
        _check_object_shape(item, path, _STAGE_REQUIRED, _STAGE_ALLOWED)
        id_value = item["stageId"]
        id_path = _join(path, "stageId")
        _require_valid_id(id_value, id_path, "stage.stageId")
        _require_text(item["title"], _join(path, "title"), "stage.title", False)
        # summary 为字符串，可为空。
        _require_text(item["summary"], _join(path, "summary"), "stage.summary", True)
        if id_value in ids:
            _fail(ERROR_MAPPING_AMBIGUOUS, "同一项目内 stageId 重复", id_path)
        ids.add(id_value)
    return ids


def _index_business_lines(snapshot):
    ids = set()
    for position, item in enumerate(snapshot["businessLines"]):
        path = _position_path("$.businessLines", position)
        _check_object_shape(item, path, _BUSINESS_LINE_REQUIRED, _BUSINESS_LINE_ALLOWED)
        id_value = item["businessLineId"]
        id_path = _join(path, "businessLineId")
        _require_valid_id(id_value, id_path, "businessLine.businessLineId")
        _require_text(item["title"], _join(path, "title"), "businessLine.title", False)
        # planStatus 是规划治理标签：只要求非空字符串，未知声明原样保留，不转执行状态。
        _require_text(
            item["planStatus"], _join(path, "planStatus"), "businessLine.planStatus", False
        )
        if id_value in ids:
            _fail(ERROR_MAPPING_AMBIGUOUS, "同一项目内 businessLineId 重复", id_path)
        ids.add(id_value)
    return ids


def _index_milestones(snapshot):
    ids = set()
    for position, item in enumerate(snapshot["milestones"]):
        path = _position_path("$.milestones", position)
        _check_object_shape(item, path, _MILESTONE_REQUIRED, _MILESTONE_ALLOWED)
        id_value = item["milestoneId"]
        id_path = _join(path, "milestoneId")
        _require_valid_id(id_value, id_path, "milestone.milestoneId")
        _require_text(item["title"], _join(path, "title"), "milestone.title", False)
        _require_gate_criteria(item["gateCriteria"], _join(path, "gateCriteria"))
        _require_text(
            item["planStatus"], _join(path, "planStatus"), "milestone.planStatus", False
        )
        if id_value in ids:
            _fail(ERROR_MAPPING_AMBIGUOUS, "同一项目内 milestoneId 重复", id_path)
        ids.add(id_value)
    return ids


# ---- 显式引用检查（先收集索引，再检引用；合法前向引用通过） ------------------


def _check_reference(container, key, path, target_ids, diagnostics):
    """检查单个映射字段。

    - 缺失 / ``None`` / 空字符串：**非致命**，只追加 ``planning_mapping_missing`` 诊断，
      保持原输入不变，不猜关联。
    - 非空但非字符串或非合法 ID：硬错误 ``planning_structure_invalid``。
    - 合法 ID 但目标不存在：硬错误 ``planning_mapping_invalid``（dangling）。
    """
    if key not in container:
        diagnostics.append({"code": ERROR_MAPPING_MISSING, "path": path})
        return
    value = container[key]
    if value is None or (isinstance(value, str) and value == ""):
        diagnostics.append({"code": ERROR_MAPPING_MISSING, "path": path})
        return
    if not isinstance(value, str):
        _fail(ERROR_INVALID, "引用必须是非空字符串 ID", path)
    if _ID_PATTERN.fullmatch(value) is None:
        _fail(
            ERROR_INVALID,
            "引用形态必须是 [A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}",
            path,
        )
    if value not in target_ids:
        _fail(ERROR_MAPPING_INVALID, "引用的目标在同类型集合内不存在", path)


# ---- 公开入口 --------------------------------------------------------------


def validate_planning_structure(
    snapshot, *, expected_project_id, expected_planning_source_id
):
    """校验快照四类对象的结构与显式引用关系（纯函数，无任何 I/O 与副作用）。

    ``expected_project_id`` / ``expected_planning_source_id`` 由受控上层提供，本函数只做
    精确字符串匹配：不一致分别抛 ``planning_cross_project_reference`` /
    ``planning_source_mismatch``；本身非法（非字符串 / 空 / 形态不符）抛
    ``planning_structure_invalid``。
    """
    # 0) 受控期望参数：参数级形态校验（不查询登记库、不核实来源登记）。
    _require_expected_identifier(expected_project_id, "expected_project_id")
    _require_expected_identifier(expected_planning_source_id, "expected_planning_source_id")

    # 1) 先复用已审核摘要函数做冻结检查，并取得独立深拷贝载荷（不复制、不弱化规则）。
    payload = digest_payload(snapshot)

    # 2) 受控期望身份精确匹配。
    if snapshot["projectId"] != expected_project_id:
        _fail(ERROR_CROSS_PROJECT, "快照 projectId 与受控期望项目不一致", "$.projectId")
    if snapshot["planningSourceId"] != expected_planning_source_id:
        _fail(
            ERROR_SOURCE_MISMATCH,
            "快照 planningSourceId 与受控期望来源不一致",
            "$.planningSourceId",
        )

    # 3) 先完整收集四类对象索引（字段白名单 / 同类型唯一 ID；合法前向引用不误判）。
    section_ids = _index_sections(snapshot)
    stage_ids = _index_stages(snapshot)
    business_line_ids = _index_business_lines(snapshot)
    _index_milestones(snapshot)

    # 4) 再按固定数组顺序检查显式引用；缺映射只记非致命诊断，不猜关联。
    diagnostics = []
    for position, stage in enumerate(snapshot["stages"]):
        _check_reference(
            stage,
            "sectionRef",
            _join(_position_path("$.stages", position), "sectionRef"),
            section_ids,
            diagnostics,
        )
    for position, business_line in enumerate(snapshot["businessLines"]):
        base = _position_path("$.businessLines", position)
        _check_reference(
            business_line, "stageRef", _join(base, "stageRef"), stage_ids, diagnostics
        )
        _check_reference(
            business_line, "sectionRef", _join(base, "sectionRef"), section_ids, diagnostics
        )
    for position, milestone in enumerate(snapshot["milestones"]):
        base = _position_path("$.milestones", position)
        _check_reference(
            milestone,
            "businessLineRef",
            _join(base, "businessLineRef"),
            business_line_ids,
            diagnostics,
        )
        _check_reference(
            milestone, "sectionRef", _join(base, "sectionRef"), section_ids, diagnostics
        )

    # 5) 摘要复用已通过函数；diagnostics 不进 payload、不参与摘要。
    canonical_digest = compute_canonical_digest(snapshot)

    return {
        "valid": True,
        "payload": payload,
        "canonicalDigest": canonical_digest,
        "diagnostics": diagnostics,
        "wouldWrite": False,
        "wouldDispatch": False,
        "wouldActivate": False,
    }
