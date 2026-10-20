"""D4-NEXT-02A3：源 MD 名册只读预检纯合同（接口文档 §13.9，cox 已冻结）。

只实现一个纯函数 ``inspect_legacy_registry_markdown(text, *, source_ref)``：读取来源
MD 名册文本并输出**只读预检**，不落盘、不联网、不读文件 / env / DB / 宿主。

- 只解析两处二级标题「当前 Agent 列表」「计划中的 Agent」下的主表；
- 忽略其他表格、部署记录、变更记录与 fenced 代码块（含 fenced 假表）；
- 结构歧义（标题缺失 / 重复、主表缺失 / 重复、数据区出现重复表头 / 分隔行、
  表头 / 分隔 / 列数不符、未闭合 fence、主表后出现非表格非空内容）一律受控拒绝，
  绝不静默跳行或猜测复杂 Markdown。

约定（严格遵循 §13.9 与 cox 确定性补充）：

- 失败统一抛既有 ``contracts.schemas.ContractError``，``code="invalid_task"``，
  ``details`` 含稳定 ``field`` / ``reason``；不新增或注册全局错误码。
- ``source_ref`` 只做纯词法绝对 POSIX 路径校验（复用 02A1 私有词法 helper），
  不 resolve / stat；未知 ref 不读外部。
- 所有输出 ``wouldWrite=false`` / ``wouldDispatch=false`` /
  ``authorityUnchanged=true`` / ``readyToApply=false``；声明一律不作为验证结果，
  不生成可直接 apply 的 ``agent-registration`` payload。
- 源名册当前声明的 current / planned 数量只是探针时点事实，代码不硬编码数量或名字。
"""
from __future__ import annotations

import hashlib
import re

from contracts.agent_registry import (
    ROLE_VOCABULARY,
    _check_absolute_posix_path,
    normalize_agent_alias,
)
from contracts.schemas import ContractError

# ---------------------------------------------------------------------------
# 版本、标题、表头与常量
# ---------------------------------------------------------------------------

REGISTRY_INTAKE_SCHEMA_VERSION = "registry-intake-preview.v1"

# text 只接受 str，UTF-8 字节数上限 1 MiB（与 02A1 同口径）。
REGISTRY_MARKDOWN_MAX_BYTES = 1024 * 1024

# 两处目标二级标题。
CURRENT_TABLE_TITLE = "当前 Agent 列表"
PLANNED_TABLE_TITLE = "计划中的 Agent"

# 各自主表表头（本节精确比对，七列；计划表第五列为「计划负责范围」）。
CURRENT_TABLE_HEADER = ("Agent", "平台", "模型", "角色", "负责范围", "权限", "状态")
PLANNED_TABLE_HEADER = ("Agent", "平台", "模型", "角色", "计划负责范围", "权限", "状态")

# status 只识别这三个受控值及其括号注释（中文或英文括号），不把备注当验证结果。
STATUS_VOCABULARY = ("active", "disabled", "planned")

# 每条 entry 至少显式声明仍待服务 / 派发核验的字段；不从职责文字或状态推导。
ENTRY_UNRESOLVED_FIELDS = (
    "environmentId",
    "allowedRoots",
    "permissionCeiling",
    "modelProfileId",
    "binding",
)

# 受控 ID（与 02A1 同形，完整匹配，不留尾 LF 误判）。
_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

# ATX 标题：忽略边沿空白后判断（``## Title``）。
_ATX_RE = re.compile(r"^(#+)[ \t]+(.*)$")

# fenced 代码块（反引号或波浪号，缩进 ≤3 空格）。
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")

# 分隔行单元格：可选左 / 右冒号 + 至少三横线。
_SEPARATOR_CELL_RE = re.compile(r":?-{3,}:?\Z")

# status：active / disabled / planned，可选**同类完整配对**的括号注释。
# 未闭合、混合配对（（…)/(…)）、闭合后尾随文本都不算可识别，一律走 status_unknown 诊断（不丢行）。
_STATUS_RE = re.compile(
    r"\A(active|disabled|planned)(?:[ \t]*(?:（[^（）]*）|\([^()]*\)))?\Z"
)

_TABLE_LABELS = {
    "current": CURRENT_TABLE_TITLE,
    "planned": PLANNED_TABLE_TITLE,
}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _invalid(field, reason):
    """统一失败出口：既有 ContractError，code=invalid_task，details.field/reason。"""
    raise ContractError(
        "invalid_task",
        message="源名册预检失败：%s" % reason,
        details={"field": field, "reason": reason},
    )


def _label(table_name):
    return _TABLE_LABELS[table_name]


def _split_physical_lines(text):
    """把文本拆成 ``[(raw_line_without_eol, eol), ...]``。

    保留原始行尾字节：LF / CRLF / 单独 CR 各自成对；末行无行尾时 eol 为空串。
    """
    lines = []
    start = 0
    index = 0
    total = len(text)
    while index < total:
        char = text[index]
        if char == "\r":
            if index + 1 < total and text[index + 1] == "\n":
                lines.append((text[start:index], "\r\n"))
                index += 2
            else:
                lines.append((text[start:index], "\r"))
                index += 1
            start = index
        elif char == "\n":
            lines.append((text[start:index], "\n"))
            index += 1
            start = index
        else:
            index += 1
    if start < total:
        lines.append((text[start:], ""))
    return lines


def _mark_fences(lines):
    """标记 fenced 代码块内的物理行；未闭合 fence 受控拒绝。

    返回与 ``lines`` 等长的布尔列表：``True`` 表示该行位于 fenced 块内（含围栏行）。
    """
    in_code = [False] * len(lines)
    fence_char = None
    fence_len = 0
    for index, (content, _eol) in enumerate(lines):
        if fence_char is None:
            match = _FENCE_RE.match(content)
            if match is None:
                continue
            marker = match.group(2)
            char = marker[0]
            info = match.group(3)
            # 反引号围栏的 info string 不允许再含反引号（不猜非法 fence）。
            if char == "`" and "`" in info:
                continue
            fence_char = char
            fence_len = len(marker)
            in_code[index] = True
        else:
            in_code[index] = True
            match = _FENCE_RE.match(content)
            if match is None:
                continue
            marker = match.group(2)
            if (
                marker[0] == fence_char
                and len(marker) >= fence_len
                and match.group(3).strip() == ""
            ):
                fence_char = None
                fence_len = 0
    if fence_char is not None:
        _invalid("markdown", "fenced 代码块未闭合，拒绝解析以避免歧义")
    return in_code


def _parse_atx(content):
    """忽略边沿空白后的 ATX 标题；返回 ``(level, title)`` 或 ``None``。"""
    stripped = content.strip()
    if not stripped.startswith("#"):
        return None
    match = _ATX_RE.match(stripped)
    if match is None:
        return None
    level = len(match.group(1))
    if level > 6:
        return None
    return level, match.group(2).strip()


def _collect_headings(lines, in_code):
    headings = []
    for index, (content, _eol) in enumerate(lines):
        if in_code[index]:
            continue
        parsed = _parse_atx(content)
        if parsed is not None:
            headings.append((index, parsed[0], parsed[1]))
    return headings


def _section_bounds(headings, heading_index, total_lines):
    """目标标题的范围：从标题下一行到下一个一级 / 二级标题（不含）。"""
    end = total_lines
    for index, level, _title in headings:
        if index > heading_index and level <= 2:
            end = index
            break
    return heading_index + 1, end


def _is_table_row(line):
    """行首尾为管道（忽略边沿空白）；至少两个字符。"""
    stripped = line.strip()
    return len(stripped) >= 2 and stripped.startswith("|") and stripped.endswith("|")


def _split_row_cells(row):
    """按未转义管道拆列；反斜杠转义管道按奇偶反斜杠区分并保留原文本。

    入参为已去边沿空白的整行（首尾均有管道）。
    """
    inner = row[1:-1]
    cells = []
    buffer = []
    index = 0
    total = len(inner)
    while index < total:
        char = inner[index]
        if char == "\\":
            buffer.append(char)
            index += 1
            if index < total:
                buffer.append(inner[index])
                index += 1
            continue
        if char == "|":
            cells.append("".join(buffer))
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    cells.append("".join(buffer))
    return cells


def _parse_separator_cells(line):
    """分隔行校验：必须是表格行且每 cell 为可选冒号 + ≥3 横线；否则 ``None``。"""
    if not _is_table_row(line):
        return None
    cells = [cell.strip() for cell in _split_row_cells(line.strip())]
    if not cells:
        return None
    for cell in cells:
        if _SEPARATOR_CELL_RE.match(cell) is None:
            return None
    return cells


def _unwrap_single_backticks(value):
    """仅去掉一对完整单 backtick 包裹；其余格式原样保留，不擅自解码。"""
    if (
        len(value) >= 2
        and value[0] == "`"
        and value[-1] == "`"
        and "`" not in value[1:-1]
    ):
        return value[1:-1]
    return value


def _is_known_status(text):
    return _STATUS_RE.match(text) is not None


def _diag(kind, field, line_numbers, reason):
    return {
        "kind": kind,
        "field": field,
        "lineNumbers": sorted(line_numbers),
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# 主表解析
# ---------------------------------------------------------------------------

def _parse_main_table(table_name, body_span, lines, in_code):
    """解析目标范围内唯一主表；结构歧义一律受控拒绝，不静默跳行。"""
    label = _label(table_name)
    expected_header = (
        CURRENT_TABLE_HEADER if table_name == "current" else PLANNED_TABLE_HEADER
    )
    body_start, body_end = body_span
    sequence = [
        (index, lines[index][0])
        for index in range(body_start, body_end)
        if not in_code[index]
    ]

    position = 0
    while position < len(sequence) and sequence[position][1].strip() == "":
        position += 1
    if position >= len(sequence):
        _invalid("markdown", "%s 下缺少主表" % label)

    _header_index, header_line = sequence[position]
    if not _is_table_row(header_line):
        _invalid("markdown", "%s 主表表头行缺少首尾管道" % label)
    header_cells = [cell.strip() for cell in _split_row_cells(header_line.strip())]
    if len(header_cells) != len(expected_header):
        _invalid(
            "markdown",
            "%s 主表表头列数为 %d，应为 %d" % (label, len(header_cells), len(expected_header)),
        )
    for column, (found, want) in enumerate(zip(header_cells, expected_header)):
        if found != want:
            _invalid(
                "markdown",
                "%s 主表表头第 %d 列名不符，应为「%s」" % (label, column + 1, want),
            )
    position += 1

    if position >= len(sequence):
        _invalid("markdown", "%s 主表缺少分隔行" % label)
    _separator_index, separator_line = sequence[position]
    separator_cells = _parse_separator_cells(separator_line)
    if separator_cells is None:
        _invalid("markdown", "%s 主表分隔行形态不符" % label)
    if len(separator_cells) != len(expected_header):
        _invalid(
            "markdown",
            "%s 主表分隔行列数为 %d，应为 %d"
            % (label, len(separator_cells), len(expected_header)),
        )
    position += 1

    data_rows = []
    while position < len(sequence) and _is_table_row(sequence[position][1]):
        index, content = sequence[position]
        # 数据区出现分隔行：紧贴的第一张表之后又来一行「| --- | … |」，禁止当成员行。
        if _parse_separator_cells(content) is not None:
            _invalid(
                "markdown",
                "%s 主表数据区出现重复分隔行（第 %d 行），拒绝以避免结构歧义"
                % (label, index + 1),
            )
        # 数据区出现重复表头：整行七列与本节表头完全一致才判为第二张表头；
        # 只比对整行，不因某一 cell 恰为「Agent」等合法文字就误拒成员行。
        row_cells = [cell.strip() for cell in _split_row_cells(content.strip())]
        if row_cells == list(expected_header):
            _invalid(
                "markdown",
                "%s 主表数据区出现重复表头行（第 %d 行），拒绝以避免结构歧义"
                % (label, index + 1),
            )
        data_rows.append(sequence[position])
        position += 1

    # 主表后只允许空行；任何非空非表格内容都视为结构歧义（第二张表 / 损坏行）。
    for index, content in sequence[position:]:
        if content.strip() != "":
            _invalid(
                "markdown",
                "%s 主表后出现非表格非空内容（第 %d 行），拒绝以避免结构歧义"
                % (label, index + 1),
            )
    return table_name, data_rows


def _build_entry(table_name, index, content, eol):
    """由一条数据行构造 entry 与对应的行级 diagnostics。"""
    line_no = index + 1
    label = _label(table_name)
    cells = [cell.strip() for cell in _split_row_cells(content.strip())]
    if len(cells) != 7:
        _invalid(
            "markdown",
            "%s 主表数据行列数为 %d，应为 7（第 %d 行）" % (label, len(cells), line_no),
        )

    diagnostics = []

    legacy_alias = _unwrap_single_backticks(cells[0])
    try:
        normalized_alias = normalize_agent_alias(legacy_alias)
    except ContractError:
        normalized_alias = None
        diagnostics.append(
            _diag(
                "alias_unusable",
                "legacyAlias",
                [line_no],
                "Agent 别名无法规范化为 normalizedAlias（空 / 控制字符 / 超长）",
            )
        )

    proposed_agent_id = legacy_alias if _ID_RE.match(legacy_alias) else None
    if proposed_agent_id is None:
        diagnostics.append(
            _diag(
                "agent_id_invalid",
                "cells[0]",
                [line_no],
                "Agent 不是合法受控 ID，保留原文但不作为候选身份",
            )
        )

    role_candidate = _unwrap_single_backticks(cells[3])
    if role_candidate in ROLE_VOCABULARY:
        declared_roles = [role_candidate]
    else:
        declared_roles = None
        diagnostics.append(
            _diag(
                "role_unknown",
                "cells[3]",
                [line_no],
                "角色列去一对完整反引号后不匹配受控词表",
            )
        )

    if not _is_known_status(cells[6]):
        diagnostics.append(
            _diag(
                "status_unknown",
                "cells[6]",
                [line_no],
                "状态原文不属于 active/disabled/planned 及其括号注释",
            )
        )

    entry = {
        "sourceTable": table_name,
        "lineRange": {"start": line_no, "end": line_no},
        "rawLine": content,
        "rawLineDigest": "sha256:%s"
        % hashlib.sha256((content + eol).encode("utf-8")).hexdigest(),
        "cells": cells,
        "legacyAlias": legacy_alias,
        "normalizedAlias": normalized_alias,
        "proposedAgentId": proposed_agent_id,
        "declaredRoles": declared_roles,
        "suggestedEnabled": False,
        "declared": {
            "platformText": cells[1],
            "modelText": cells[2],
            "roleText": cells[3],
            "scopeText": cells[4],
            "permissionText": cells[5],
            "statusText": cells[6],
        },
        "unresolvedFields": list(ENTRY_UNRESOLVED_FIELDS),
    }
    return entry, diagnostics


def _detect_conflicts(entries):
    """两主表全局按 normalizedAlias 与候选 ID 登记冲突；保留所有行不合并。"""
    diagnostics = []
    for field, kind, reason in (
        (
            "normalizedAlias",
            "normalized_alias_conflict",
            "normalizedAlias 在两主表内重复，保留所有来源行不合并",
        ),
        (
            "proposedAgentId",
            "proposed_agent_id_conflict",
            "候选 Agent ID 在两主表内重复，保留所有来源行不合并",
        ),
    ):
        groups = {}
        for entry in entries:
            value = entry[field]
            if value is None:
                continue
            groups.setdefault(value, []).append(entry["lineRange"]["start"])
        for value in sorted(groups):
            line_numbers = sorted(groups[value])
            if len(line_numbers) > 1:
                diagnostics.append(_diag(kind, field, line_numbers, reason))
    return diagnostics


# ---------------------------------------------------------------------------
# 公开纯函数
# ---------------------------------------------------------------------------

def inspect_legacy_registry_markdown(text, *, source_ref):
    """源 MD 名册只读预检：只解析两处目标主表，输出 preview 结构。

    - ``text`` 严格 str、UTF-8 ≤ 1 MiB；编码异常受控 ``invalid_task``。
    - ``source_ref`` 为纯词法绝对 POSIX 路径，不 resolve / stat。
    - 结构 / 字段错误抛既有 ``ContractError``（``invalid_task`` + ``details.field/reason``）。
    - 返回 ``registry-intake-preview.v1``；``readyToApply`` 恒为 false。
    """
    if not isinstance(text, str):
        _invalid("text", "只接受字符串输入")
    try:
        payload = text.encode("utf-8")
    except UnicodeEncodeError:
        _invalid("text", "字符串无法以 UTF-8 编码（含未配对代理项）")
    if len(payload) > REGISTRY_MARKDOWN_MAX_BYTES:
        _invalid("text", "markdown 超过 1 MiB 上限")

    source_ref = _check_absolute_posix_path(source_ref, "source_ref")

    lines = _split_physical_lines(text)
    in_code = _mark_fences(lines)
    headings = _collect_headings(lines, in_code)

    collected = []
    for title, table_name in (
        (CURRENT_TABLE_TITLE, "current"),
        (PLANNED_TABLE_TITLE, "planned"),
    ):
        matches = [item for item in headings if item[1] == 2 and item[2] == title]
        if not matches:
            _invalid("markdown", "缺少二级标题「%s」" % title)
        if len(matches) > 1:
            _invalid("markdown", "二级标题「%s」出现多次" % title)
        body_span = _section_bounds(headings, matches[0][0], len(lines))
        _selected, data_rows = _parse_main_table(table_name, body_span, lines, in_code)
        for index, content in data_rows:
            entry, row_diagnostics = _build_entry(
                table_name, index, content, lines[index][1]
            )
            collected.append((entry["lineRange"]["start"], entry, row_diagnostics))

    collected.sort(key=lambda item: item[0])
    entries = [item[1] for item in collected]
    diagnostics = [diag for item in collected for diag in item[2]]
    diagnostics.extend(_detect_conflicts(entries))

    return {
        "schemaVersion": REGISTRY_INTAKE_SCHEMA_VERSION,
        "source": {
            "ref": source_ref,
            "digest": "sha256:%s" % hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
        },
        "wouldWrite": False,
        "wouldDispatch": False,
        "authorityUnchanged": True,
        "readyToApply": False,
        "entries": entries,
        "diagnostics": diagnostics,
    }


__all__ = [
    "REGISTRY_INTAKE_SCHEMA_VERSION",
    "REGISTRY_MARKDOWN_MAX_BYTES",
    "CURRENT_TABLE_TITLE",
    "PLANNED_TABLE_TITLE",
    "CURRENT_TABLE_HEADER",
    "PLANNED_TABLE_HEADER",
    "STATUS_VOCABULARY",
    "ENTRY_UNRESOLVED_FIELDS",
    "inspect_legacy_registry_markdown",
]
