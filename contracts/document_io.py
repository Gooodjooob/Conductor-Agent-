"""D4-NEXT-01B：任务卡与回执文件的确定性解析、规范化与只读预检。

对应接口文档《D4 接口与数据约定》§10.2（两种文件载体、一份内部合同）与任务卡
D4-NEXT-01B。本模块只做「文件 -> 规范化对象」的确定性转换和只读预检，不含业务规则。

安全边界（冻结）：
- 只接受 **Markdown + 唯一一段 YAML frontmatter**；正文保留为技术正文，
  不从自由文本猜测状态、身份或审核结果。
- 只实现明确受限的 YAML 子集：块映射、块序列、受限标量、受限流集合。
  拒绝锚点、别名、合并键、标签对象、指令、多文档、块标量、复杂键和无法确定类型的语法。
- 拒绝重复键、非对象根、过深嵌套、超大集合、超长标量和超出文件大小上限的输入。
- 不调用 shell / Python 表达式 / 模板引擎 / 外部网络；不安装依赖。
- 解析或校验失败一律 fail closed：抛稳定错误码，不产生规范化结果，
  不写数据库、不创建任务、不创建执行轮次、不调用宿主。
- 解析成功后必须调用 D4-NEXT-01A-R2 的整体校验函数
  （`validate_task_card_document` / `validate_report_document`），不得只检查 `boardSummary`。
"""
from __future__ import annotations

import hashlib
import json
import os
import re

from contracts.board_summary import (
    BOARD_ERROR_CODES,
    REPORT_BOARD_V1,
    TASK_CARD_BOARD_V1,
    detect_duplicate_keys,
    validate_report_document,
    validate_task_card_document,
)
from contracts.schemas import (
    ERROR_CODES,
    is_strict_card_revision,
    validate_limits,
    validate_permissions,
)

# ---------------------------------------------------------------------------
# 文档类型与 schema 绑定（任务卡/报告各自只认自己的 v1，交叉拒绝）
# ---------------------------------------------------------------------------

DOC_KINDS = ("task-card", "report")
KIND_SCHEMA_VERSIONS = {
    "task-card": TASK_CARD_BOARD_V1,
    "report": REPORT_BOARD_V1,
}
SCHEMA_VERSION_KINDS = {
    TASK_CARD_BOARD_V1: "task-card",
    REPORT_BOARD_V1: "report",
}

# ---------------------------------------------------------------------------
# 受限子集的大小上限（fail closed，超限直接拒绝解析）
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 1024 * 1024          # 单文件 1 MiB
MAX_FRONTMATTER_LINES = 500           # frontmatter 行数
MAX_DEPTH = 8                         # 嵌套深度
MAX_MAPPING_KEYS = 200                # 单个映射键数
MAX_SEQUENCE_ITEMS = 200              # 单个序列项数
MAX_SCALAR_CHARS = 4000               # 单个标量字符数
BODY_HEADLINE_CHARS = 120             # 正文受控摘要（首行标题）截断长度

# ---------------------------------------------------------------------------
# 受控身份映射：alias -> agentId
#
# 合同 §10.1/§11.4：别名只给人看，agentId/host/environmentId 才是绑定依据；
# 两者同时出现且不一致、停用、未知或缺少受控映射时一律拒绝，不做模糊匹配。
# 当前 D4 受控面只有 CLI 常量注入的成员（见 scripts/d4ctl.py 的
# D4_LOGICAL_AGENT_ID / D4_SOURCE_ID），因此映射表为空：
# 别名必须与稳定 agentId 同值，或在此表显式登记后才算可解析。
# ---------------------------------------------------------------------------

CONTROLLED_ALIAS_MAP = {}

# YAML 子集里「无法确定类型」的写法：YAML 1.1 会把它们解析成布尔/日期/浮点等，
# 本子集一律拒绝并要求显式加引号（fail closed，不做隐式推断）。
_AMBIGUOUS_BOOL_TOKENS = (
    "yes", "no", "on", "off", "y", "n",
    "true", "false",   # 大小写混写（True/TRUE/False）也拒绝，只接受全小写
)
_DATE_LIKE = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}(?:[Tt ].*)?$|^\d{1,2}:\d{2}(?::\d{2})?.*$")
_NUMBER_LIKE = re.compile(r"^[+-]?[0-9]")
_STRICT_INT = re.compile(r"^[+-]?(?:0|[1-9][0-9]*)$")
_NON_DECIMAL_NUMBER = re.compile(r"^[+-]?(?:0[xXoObB][0-9a-fA-F_]+|[0-9][0-9_]*(?:\.[0-9]*)?(?:[eE][+-]?[0-9]+)?|[0-9]+\.[0-9]*)$")

# 受限转义：双引号标量只接受这几个转义，其余（\u、\x、\0 等）拒绝。
_ALLOWED_ESCAPES = {"\\": "\\", '"': '"', "n": "\n", "t": "\t", "r": "\r", "/": "/"}

# 标量开头的 YAML 指示符：出现即拒绝（锚点/别名/标签/指令/块标量等）。
_FORBIDDEN_SCALAR_PREFIXES = ("&", "*", "!", "%", "@", "`", "|", ">", "<<", "?")

# ---------------------------------------------------------------------------
# 稳定错误码（本卡新增；与 board / schemas 既有码共用同一命名空间）
# ---------------------------------------------------------------------------

DOC_ERROR_CODES = {
    "document_not_file": "输入路径不是普通文件，拒绝读取",
    "document_too_large": "文件超过大小上限，拒绝解析",
    "document_encoding": "文件不是合法 UTF-8 文本，拒绝解析",
    "document_invalid": "文件已解析但整体校验未通过，不产生规范化结果",
    "frontmatter_missing": "文件开头没有唯一的 --- frontmatter 段",
    "frontmatter_unclosed": "frontmatter 没有闭合的 --- 分隔行",
    "kind_unknown": "无法在没有歧义的前提下判断文档类型，需显式 --kind",
    "yaml_tab_rejected": "frontmatter 出现制表符，受限子集只接受空格缩进",
    "yaml_indent_error": "缩进无法解析（不一致或层级错乱）",
    "yaml_syntax_error": "frontmatter 语法不在受限子集内，拒绝解析",
    "yaml_anchor_rejected": "frontmatter 出现锚点（&），受限子集拒绝",
    "yaml_alias_rejected": "frontmatter 出现别名（*），受限子集拒绝",
    "yaml_merge_key_rejected": "frontmatter 出现合并键（<<），受限子集拒绝",
    "yaml_tag_rejected": "frontmatter 出现标签（! / !!），受限子集拒绝",
    "yaml_directive_rejected": "frontmatter 出现指令或多文档标记（% / ...），拒绝",
    "yaml_block_scalar_rejected": "frontmatter 出现块标量（| / >），受限子集拒绝",
    "yaml_flow_rejected": "流集合写法超出受限子集（嵌套、空项或多余逗号），拒绝",
    "yaml_escape_rejected": "双引号标量出现未允许的转义序列，拒绝",
    "yaml_type_unresolved": "标量类型无法确定（需显式加引号或改成受控写法）",
    "scalar_too_long": "标量超出长度上限，拒绝解析",
    "root_not_object": "frontmatter 根节点不是对象，拒绝解析",
    "structure_too_deep": "frontmatter 嵌套层数超过上限，拒绝解析",
    "structure_too_large": "frontmatter 集合规模超过上限，拒绝解析",
    "path_not_absolute": "路径必须是绝对路径，不接受相对路径或空值",
    "path_symlink_rejected": "输入文件本身是符号链接，拒绝读取",
}

_KNOWN_CODES = set(DOC_ERROR_CODES) | set(BOARD_ERROR_CODES) | set(ERROR_CODES)


class DocumentContractError(Exception):
    """文档解析/校验错误：调用方必须 fail closed（不写库、不建任务、不调宿主）。"""

    def __init__(self, code, message=None, details=None):
        if code not in _KNOWN_CODES:
            raise ValueError("unknown document error code: %r" % (code,))
        self.code = code
        self.details = details or {}
        super().__init__(message or DOC_ERROR_CODES.get(code) or BOARD_ERROR_CODES.get(code)
                         or ERROR_CODES.get(code) or code)

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


# ---------------------------------------------------------------------------
# 稳定摘要工具（同一字节输入重复计算必须完全一致）
# ---------------------------------------------------------------------------

def canonical_json(value):
    """稳定 JSON：键排序、数组保持原序、非 ASCII 不转义、无多余空格。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_digest(value):
    """规范化对象的稳定摘要（sha256，带算法前缀）。"""
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def bytes_digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 受控读取：绝对路径、拒绝符号链接、拒绝 .. 逃逸、拒绝越出允许根
# ---------------------------------------------------------------------------

def _within_any(candidate, roots):
    """candidate（绝对路径）是否落在任一受控 root 内；roots 为空表示不施加限制。"""
    if not roots:
        return True
    target = os.path.realpath(candidate)
    for root in roots:
        root_real = os.path.realpath(root)
        if target == root_real or target.startswith(root_real + os.sep):
            return True
    return False


def _assert_no_escape(path, allowed_roots):
    """路径安全：绝对路径 + 无 `..` 段 + 落在允许根内。"""
    if not isinstance(path, str) or not path.strip():
        raise DocumentContractError("path_not_absolute", details={"path": path})
    if not path.startswith("/"):
        raise DocumentContractError("path_not_absolute", details={"path": path})
    if ".." in path.split("/"):
        raise DocumentContractError("path_escape", details={"path": path})
    if not _within_any(path, allowed_roots):
        raise DocumentContractError(
            "path_escape",
            details={"path": path, "allowedRoots": [os.path.realpath(r) for r in allowed_roots or []]},
        )


def read_source_file(path, allowed_roots=None, max_bytes=MAX_FILE_BYTES):
    """受控读取输入文件，返回 (meta, text)。只读取该文件本身。"""
    _assert_no_escape(path, allowed_roots)
    if os.path.islink(path):
        raise DocumentContractError("path_symlink_rejected", details={"path": path})
    real = os.path.realpath(path)
    if not os.path.isfile(real):
        raise DocumentContractError("document_not_file", details={"path": path})
    size = os.path.getsize(real)
    if size > max_bytes:
        raise DocumentContractError(
            "document_too_large",
            details={"path": real, "bytes": size, "limit": max_bytes},
        )
    with open(real, "rb") as handle:
        raw = handle.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentContractError("document_encoding", details={"path": real}) from exc
    if text.startswith("\ufeff"):
        text = text[1:]
    meta = {
        "path": real,
        "bytes": size,
        "sha256": bytes_digest(raw),
    }
    return meta, text


# ---------------------------------------------------------------------------
# frontmatter 拆分与行切分
# ---------------------------------------------------------------------------

class _Line(object):
    __slots__ = ("indent", "content", "number")

    def __init__(self, indent, content, number):
        self.indent = indent
        self.content = content
        self.number = number


def split_frontmatter(text):
    """拆出唯一一段 frontmatter；返回 (frontmatter_lines, body_lines)。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        raise DocumentContractError(
            "frontmatter_missing", details={"firstLine": lines[0][:80] if lines else ""}
        )
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], lines[index + 1:]
    raise DocumentContractError("frontmatter_unclosed", details={"lines": len(lines)})


def _tokenize(fm_lines):
    """把 frontmatter 行切分为 (缩进, 内容, 行号)；跳过空行与整行注释。"""
    if len(fm_lines) > MAX_FRONTMATTER_LINES:
        raise DocumentContractError(
            "structure_too_large",
            details={"frontmatterLines": len(fm_lines), "limit": MAX_FRONTMATTER_LINES},
        )
    out = []
    for offset, raw in enumerate(fm_lines):
        number = offset + 2  # frontmatter 从第 2 行开始
        if "\t" in raw:
            raise DocumentContractError("yaml_tab_rejected", details={"line": number})
        line = raw.rstrip("\r")
        stripped_left = line.lstrip(" ")
        indent = len(line) - len(stripped_left)
        content = line.strip()
        if not content or content.startswith("#"):
            continue
        if content.startswith("%") or content == "...":
            # 指令与多文档标记：在切分阶段就拒绝，避免被当成键/标量处理
            raise DocumentContractError(
                "yaml_directive_rejected", details={"line": number, "token": content[:40]}
            )
        out.append(_Line(indent, content, number))
    return out


# ---------------------------------------------------------------------------
# 键行识别
# ---------------------------------------------------------------------------

def _split_key(content):
    """把 `key: value` / `key:` 拆成 (key, rest)；不是键行返回 None。"""
    index = 0
    while True:
        pos = content.find(":", index)
        if pos == -1:
            return None
        if pos + 1 == len(content) or content[pos + 1] == " ":
            return content[:pos].strip(), content[pos + 1:].strip()
        index = pos + 1


def _reject_indicator(token, number):
    """标量开头的 YAML 指示符：按类型给出稳定错误码。"""
    if token.startswith("<<"):
        raise DocumentContractError("yaml_merge_key_rejected", details={"line": number, "token": token[:40]})
    if token.startswith("&"):
        raise DocumentContractError("yaml_anchor_rejected", details={"line": number, "token": token[:40]})
    if token.startswith("*"):
        raise DocumentContractError("yaml_alias_rejected", details={"line": number, "token": token[:40]})
    if token.startswith("!"):
        raise DocumentContractError("yaml_tag_rejected", details={"line": number, "token": token[:40]})
    if token.startswith("%") or token == "...":
        raise DocumentContractError("yaml_directive_rejected", details={"line": number, "token": token[:40]})
    if token[0] in ("|", ">"):
        raise DocumentContractError("yaml_block_scalar_rejected", details={"line": number, "token": token[:40]})
    raise DocumentContractError("yaml_syntax_error", details={"line": number, "token": token[:40]})


def _validate_plain_text(text, number, role="scalar"):
    """受限纯文本：拒绝锚点/别名/标签/指令/` #` 注释/`: ` 歧义/空值。"""
    if not text:
        raise DocumentContractError("yaml_syntax_error", details={"line": number, "role": role})
    if text[0] in _FORBIDDEN_SCALAR_PREFIXES or text.startswith("<<"):
        _reject_indicator(text, number)
    if " #" in text:
        raise DocumentContractError(
            "yaml_syntax_error",
            message="受限子集不接受行内 ` #` 注释，请给值加引号",
            details={"line": number, "role": role},
        )
    if ": " in text or text.endswith(":"):
        raise DocumentContractError(
            "yaml_syntax_error",
            message="受限子集不接受值里出现 `: `，请给值加引号",
            details={"line": number, "role": role},
        )


# ---------------------------------------------------------------------------
# 标量解析（字符串 / 整数 / 布尔 / 空值）
# ---------------------------------------------------------------------------

def _parse_single_quoted(token, number):
    if len(token) < 2 or token[-1] != "'":
        raise DocumentContractError("yaml_syntax_error", details={"line": number, "token": token[:40]})
    inner = token[1:-1]
    if "'" in inner.replace("''", ""):
        raise DocumentContractError("yaml_syntax_error", details={"line": number, "reason": "单引号未成对"})
    value = inner.replace("''", "'")
    _check_scalar_length(value, number)
    return value


def _parse_double_quoted(token, number):
    if len(token) < 2 or token[-1] != '"':
        raise DocumentContractError("yaml_syntax_error", details={"line": number, "token": token[:40]})
    inner = token[1:-1]
    out = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\":
            nxt = inner[index + 1] if index + 1 < len(inner) else ""
            if nxt not in _ALLOWED_ESCAPES:
                raise DocumentContractError(
                    "yaml_escape_rejected", details={"line": number, "escape": "\\" + nxt}
                )
            out.append(_ALLOWED_ESCAPES[nxt])
            index += 2
            continue
        out.append(char)
        index += 1
    value = "".join(out)
    _check_scalar_length(value, number)
    return value


def _check_scalar_length(value, number):
    if isinstance(value, str) and len(value) > MAX_SCALAR_CHARS:
        raise DocumentContractError(
            "scalar_too_long",
            details={"line": number, "length": len(value), "limit": MAX_SCALAR_CHARS},
        )


def _parse_plain(token, number):
    _validate_plain_text(token, number)
    lowered = token.lower()
    if token == "true":
        return True
    if token == "false":
        return False
    if token in ("null", "~"):
        return None
    if lowered in _AMBIGUOUS_BOOL_TOKENS:
        raise DocumentContractError(
            "yaml_type_unresolved",
            message="%r 在不同 YAML 版本里可能是布尔，请写 true/false 或加引号" % token,
            details={"line": number, "token": token},
        )
    if _STRICT_INT.match(token):
        return int(token)
    if _NON_DECIMAL_NUMBER.match(token) or _NUMBER_LIKE.match(token):
        raise DocumentContractError(
            "yaml_type_unresolved",
            message="受限子集只接受十进制整数；其他数字写法请加引号",
            details={"line": number, "token": token},
        )
    if _DATE_LIKE.match(token) or token in (".inf", "-.inf", "+.inf", ".nan", ".NaN"):
        raise DocumentContractError(
            "yaml_type_unresolved",
            message="日期/时间/特殊浮点写法会被推断成非字符串，请加引号",
            details={"line": number, "token": token},
        )
    _check_scalar_length(token, number)
    return token


def _parse_scalar_token(token, number):
    token = token.strip()
    if not token:
        return None
    if token[0] in _FORBIDDEN_SCALAR_PREFIXES or token.startswith("<<"):
        _reject_indicator(token, number)
    if token[0] == "'":
        return _parse_single_quoted(token, number)
    if token[0] == '"':
        return _parse_double_quoted(token, number)
    if token[0] in "[{":
        return _parse_flow(token, number)
    return _parse_plain(token, number)


def _parse_flow(token, number):
    """受限流集合：`[]` / `{}` / 纯标量序列与映射；嵌套一律拒绝。"""
    if token.startswith("["):
        if not token.endswith("]"):
            raise DocumentContractError("yaml_flow_rejected", details={"line": number, "token": token[:60]})
        inner = token[1:-1].strip()
        if not inner:
            return []
        parts = [part.strip() for part in inner.split(",")]
        if any(not part for part in parts):
            raise DocumentContractError("yaml_flow_rejected", details={"line": number, "reason": "空项或多余逗号"})
        if len(parts) > MAX_SEQUENCE_ITEMS:
            raise DocumentContractError("structure_too_large", details={"line": number})
        return [_parse_scalar_token(part, number) for part in parts]
    if token.startswith("{"):
        if not token.endswith("}"):
            raise DocumentContractError("yaml_flow_rejected", details={"line": number, "token": token[:60]})
        inner = token[1:-1].strip()
        if not inner:
            return {}
        result = {}
        for part in inner.split(","):
            split = _split_key(part.strip())
            if split is None:
                raise DocumentContractError("yaml_flow_rejected", details={"line": number, "token": part[:60]})
            key, rest = split
            _validate_plain_text(key, number, role="key")
            result[key] = _parse_scalar_token(rest, number)
        return result
    raise DocumentContractError("yaml_flow_rejected", details={"line": number, "token": token[:60]})


# ---------------------------------------------------------------------------
# 块结构解析（映射 / 序列）
# ---------------------------------------------------------------------------

def _indent_error(line, expected):
    return DocumentContractError(
        "yaml_indent_error",
        details={"line": line.number, "indent": line.indent, "expected": expected},
    )


def _parse_node(lines, index, indent, depth):
    if depth > MAX_DEPTH:
        raise DocumentContractError("structure_too_deep", details={"limit": MAX_DEPTH})
    if index >= len(lines):
        return None, index
    line = lines[index]
    if line.indent != indent:
        raise _indent_error(line, indent)
    content = line.content
    if content == "-" or content.startswith("- "):
        return _parse_sequence(lines, index, indent, depth)
    if _split_key(content) is not None:
        return _parse_mapping(lines, index, indent, depth)
    raise DocumentContractError(
        "yaml_syntax_error",
        message="受限子集不接受独立标量块（多行纯文本请用引号写成一行）",
        details={"line": line.number},
    )


def _parse_mapping(lines, index, indent, depth):
    if depth > MAX_DEPTH:
        raise DocumentContractError("structure_too_deep", details={"limit": MAX_DEPTH})
    result = {}
    pairs = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _indent_error(line, indent)
        content = line.content
        if content == "-" or content.startswith("- "):
            break  # 序列项出现在映射同层：交给上层处理
        split = _split_key(content)
        if split is None:
            raise DocumentContractError(
                "yaml_syntax_error", details={"line": line.number, "content": content[:80]}
            )
        key, rest = split
        _validate_plain_text(key, line.number, role="key")
        if key in result:
            raise DocumentContractError(
                "duplicate_key",
                details={
                    "line": line.number,
                    "key": key,
                    "duplicates": detect_duplicate_keys(pairs + [(key, None)]),
                },
            )
        if len(result) >= MAX_MAPPING_KEYS:
            raise DocumentContractError("structure_too_large", details={"line": line.number})
        pairs.append((key, None))
        if rest:
            result[key] = _parse_scalar_token(rest, line.number)
            index += 1
            continue
        index += 1
        nxt = lines[index] if index < len(lines) else None
        if nxt is not None and nxt.indent > indent:
            result[key], index = _parse_node(lines, index, nxt.indent, depth + 1)
        elif nxt is not None and nxt.indent == indent and (
            nxt.content == "-" or nxt.content.startswith("- ")
        ):
            # YAML 允许序列与它的键同缩进
            result[key], index = _parse_sequence(lines, index, indent, depth + 1)
        else:
            result[key] = None
    return result, index


def _parse_sequence(lines, index, indent, depth):
    if depth > MAX_DEPTH:
        raise DocumentContractError("structure_too_deep", details={"limit": MAX_DEPTH})
    items = []
    while index < len(lines):
        line = lines[index]
        if line.indent < indent:
            break
        if line.indent > indent:
            raise _indent_error(line, indent)
        content = line.content
        if not (content == "-" or content.startswith("- ")):
            break
        if len(items) >= MAX_SEQUENCE_ITEMS:
            raise DocumentContractError("structure_too_large", details={"line": line.number})
        rest = content[1:].strip()
        if not rest:
            index += 1
            nxt = lines[index] if index < len(lines) else None
            if nxt is not None and nxt.indent > indent:
                value, index = _parse_node(lines, index, nxt.indent, depth + 1)
            else:
                value = None
            items.append(value)
            continue
        split = _split_key(rest)
        if split is not None:
            # `- kind: "command"`：把该行改写为 indent+2 的键行，继续按映射解析
            lines[index] = _Line(indent + 2, rest, line.number)
            value, index = _parse_mapping(lines, index, indent + 2, depth + 1)
        else:
            value = _parse_scalar_token(rest, line.number)
            index += 1
        items.append(value)
    return items, index


def parse_frontmatter(text):
    """解析 frontmatter，返回 (规范化对象, 正文文本)。根节点必须是对象。"""
    fm_lines, body_lines = split_frontmatter(text)
    lines = _tokenize(fm_lines)
    if not lines:
        raise DocumentContractError("root_not_object", details={"reason": "frontmatter 为空"})
    first = lines[0]
    if first.indent != 0:
        raise _indent_error(first, 0)
    if first.content == "-" or first.content.startswith("- ") or _split_key(first.content) is None:
        raise DocumentContractError(
            "root_not_object", details={"line": first.number, "content": first.content[:80]}
        )
    value, index = _parse_node(lines, 0, 0, 1)
    if index != len(lines):
        raise _indent_error(lines[index], 0)
    if not isinstance(value, dict):
        raise DocumentContractError("root_not_object", details={"type": type(value).__name__})
    return value, "\n".join(body_lines)


# ---------------------------------------------------------------------------
# 文档类型判定：不允许模糊猜测
# ---------------------------------------------------------------------------

def resolve_document_kind(obj, declared_kind=None):
    """判定文档类型：显式 --kind 优先；缺省时只有 schemaVersion 明确才成立。"""
    schema_version = obj.get("schemaVersion") if isinstance(obj, dict) else None
    if declared_kind:
        if declared_kind not in DOC_KINDS:
            raise DocumentContractError("kind_unknown", details={"kind": declared_kind})
        return declared_kind
    if not isinstance(schema_version, str) or schema_version not in SCHEMA_VERSION_KINDS:
        raise DocumentContractError(
            "kind_unknown",
            message="缺省 --kind 时只能按文件声明的 schemaVersion 判断，无法判断即拒绝",
            details={"schemaVersion": schema_version, "allowed": list(SCHEMA_VERSION_KINDS)},
        )
    return SCHEMA_VERSION_KINDS[schema_version]


# ---------------------------------------------------------------------------
# 受控身份解析（不做模糊匹配）
# ---------------------------------------------------------------------------

def resolve_agent_identity(alias, agent_id, label):
    """alias 与 agentId 同时出现时必须一致或存在受控映射；否则拒绝。"""
    if alias and agent_id and alias != agent_id:
        mapped = CONTROLLED_ALIAS_MAP.get(alias)
        if mapped is None or mapped != agent_id:
            raise DocumentContractError(
                "identity_conflict",
                message="%s 的别名与稳定成员标识不一致，且没有受控映射" % label,
                details={"label": label, "alias": alias, "agentId": agent_id},
            )
    return agent_id or alias


def _validate_controlled_identity(obj, kind, cli_agent=None, cli_source=None):
    """受控成员/来源校验：未知或缺少受控映射一律拒绝，不模糊匹配。"""
    errors = {}
    declared_source = obj.get("sourceId")
    if declared_source is not None and cli_source and declared_source != cli_source:
        errors["sourceId"] = {
            "code": "identity_conflict",
            "reason": "文件声明的 sourceId 与受控入口不一致",
            "declared": declared_source,
            "controlled": cli_source,
        }
    if kind != "task-card":
        return errors
    executor = obj.get("executor") if isinstance(obj.get("executor"), dict) else {}
    agent_id = executor.get("agentId")
    try:
        resolved = resolve_agent_identity(executor.get("alias"), agent_id, "executor")
    except DocumentContractError as exc:
        errors["executor.alias"] = exc.to_dict()
        resolved = agent_id
    target_alias = obj.get("targetAgentAlias")
    if target_alias:
        try:
            resolve_agent_identity(target_alias, agent_id, "targetAgentAlias")
        except DocumentContractError as exc:
            errors["targetAgentAlias"] = exc.to_dict()
    if cli_agent and resolved and resolved != cli_agent:
        errors["executor.agentId"] = {
            "code": "identity_conflict",
            "reason": "任务卡执行成员不是当前受控入口登记的成员，无法解析受控映射",
            "declared": resolved,
            "controlled": cli_agent,
        }
    return errors


# ---------------------------------------------------------------------------
# 路径合同：workspace / writeScope / attachments / sourceCard.path
# 不接受 Markdown 中的任意绝对路径来扩大写入范围
# ---------------------------------------------------------------------------

def validate_path_contract(obj, allowed_roots=None):
    """只对出现过的路径键做受控校验；返回 {字段: 原因}（空表示通过）。"""
    errors = {}
    workspace = obj.get("workspace")
    if workspace is not None:
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            errors["workspace"] = {"code": "path_not_absolute"}
        elif not _within_any(workspace, allowed_roots):
            errors["workspace"] = {"code": "path_escape", "path": workspace}
    write_scope = obj.get("writeScope")
    if write_scope is not None:
        if not isinstance(write_scope, list) or not write_scope:
            errors["writeScope"] = {"code": "path_not_absolute", "reason": "必须是非空路径数组"}
        else:
            base = workspace if isinstance(workspace, str) and workspace.startswith("/") else None
            for index, entry in enumerate(write_scope):
                if not isinstance(entry, str) or not entry.strip():
                    errors["writeScope[%d]" % index] = {"code": "path_not_absolute"}
                    continue
                candidate = entry if entry.startswith("/") else (
                    os.path.join(base, entry) if base else None
                )
                if candidate is None:
                    errors["writeScope[%d]" % index] = {
                        "code": "path_not_absolute",
                        "reason": "相对路径必须配合受控 workspace 使用",
                    }
                    continue
                if ".." in candidate.split("/") or not _within_any(candidate, allowed_roots):
                    errors["writeScope[%d]" % index] = {"code": "path_escape", "path": entry}
                elif base and not _within_any(candidate, [base]):
                    errors["writeScope[%d]" % index] = {"code": "path_escape", "path": entry}
    # 权限块内的 writeScope 同样受控：不接受文件用任意绝对路径扩大写入范围
    permissions = obj.get("permissions")
    if isinstance(permissions, dict) and isinstance(permissions.get("writeScope"), list):
        nested = validate_path_contract(
            {"workspace": workspace, "writeScope": permissions["writeScope"]}, allowed_roots
        )
        for key, value in nested.items():
            errors["permissions.%s" % key] = value
    attachments = obj.get("attachments")
    if attachments is not None:
        if not isinstance(attachments, list):
            errors["attachments"] = {"code": "path_not_absolute", "reason": "必须是数组"}
        else:
            for index, item in enumerate(attachments):
                if not isinstance(item, dict):
                    errors["attachments[%d]" % index] = {"code": "path_not_absolute"}
                    continue
                entry = item.get("path")
                if entry is None:
                    continue
                if not isinstance(entry, str) or not entry.startswith("/"):
                    errors["attachments[%d].path" % index] = {"code": "path_not_absolute"}
                elif ".." in entry.split("/") or not _within_any(entry, allowed_roots):
                    errors["attachments[%d].path" % index] = {"code": "path_escape", "path": entry}
    source_card = obj.get("sourceCard")
    if isinstance(source_card, dict) and source_card.get("path") is not None:
        entry = source_card.get("path")
        if not isinstance(entry, str) or not entry.startswith("/"):
            errors["sourceCard.path"] = {"code": "path_not_absolute"}
        elif ".." in entry.split("/") or not _within_any(entry, allowed_roots):
            errors["sourceCard.path"] = {"code": "path_escape", "path": entry}
    return errors


# ---------------------------------------------------------------------------
# 规范化视图
# ---------------------------------------------------------------------------

def _pick(obj, key):
    return obj.get(key) if isinstance(obj, dict) else None


def _identity_view(obj, kind):
    if kind == "task-card":
        return {
            "issuer": _pick(obj, "issuer"),
            "executor": _pick(obj, "executor"),
            "reviewer": _pick(obj, "reviewer"),
            "targetAgentAlias": _pick(obj, "targetAgentAlias"),
        }
    return {
        "requestedBy": _pick(obj, "requestedBy"),
        "executedBy": _pick(obj, "executedBy"),
        "reviewedBy": _pick(obj, "reviewedBy"),
    }


def _project_view(obj, kind):
    project = {"projectId": _pick(obj, "projectId")}
    if kind == "task-card":
        project.update({
            "cardId": _pick(obj, "cardId"),
            "cardRevision": _pick(obj, "cardRevision"),
            "title": _pick(obj, "title"),
        })
    else:
        project.update({
            "reportId": _pick(obj, "reportId"),
            "reportRevision": _pick(obj, "reportRevision"),
            "reportType": _pick(obj, "reportType"),
            "taskId": _pick(obj, "taskId"),
        })
    return project


def _relations_view(obj, kind):
    if kind == "task-card":
        return {
            "sourceTaskId": _pick(obj, "sourceTaskId"),
            "attemptId": _pick(obj, "attemptId"),
            "adapter": _pick(obj, "adapter"),
        }
    return {
        "taskId": _pick(obj, "taskId"),
        "attemptId": _pick(obj, "attemptId"),
        "adapter": _pick(obj, "adapter"),
        "sessionId": _pick(obj, "sessionId"),
        "hostRunId": _pick(obj, "hostRunId"),
        "sourceCard": _pick(obj, "sourceCard"),
    }


def _body_headline(body):
    for line in body.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:BODY_HEADLINE_CHARS]
    return ""


def _has_dispatch_hint(obj):
    """已派发卡（含 attemptId/adapter）按派发口径校验：两者都必填。"""
    return bool(_pick(obj, "attemptId")) or bool(_pick(obj, "adapter"))


def _validate_document(obj, kind):
    if kind == "task-card":
        return dict(validate_task_card_document(obj, dispatched=_has_dispatch_hint(obj)))
    return dict(validate_report_document(obj))


def build_write_preview(obj, cli_agent=None, cli_source=None, allowed_roots=None):
    """任务卡专用：预演 `task create --file` 会写什么、缺什么。只读，不写库。"""
    preview = {
        "command": "d4ctl task create --file <this file>",
        "ready": False,
        "payload": {},
        "missing": [],
        "errors": {},
        "blockers": [],
    }
    try:
        payload, missing, errors = task_card_to_create_payload(
            obj, cli_agent=cli_agent, cli_source=cli_source, allowed_roots=allowed_roots
        )
    except DocumentContractError as exc:
        preview["blockers"].append(exc.to_dict())
        return preview
    preview["payload"] = payload
    preview["missing"] = missing
    preview["errors"] = errors
    preview["ready"] = not missing and not errors
    return preview


def inspect_document(path, kind=None, allowed_roots=None, cli_agent=None, cli_source=None,
                     max_bytes=MAX_FILE_BYTES):
    """只读预检：解析 + 规范化 + 整体校验，不做任何写入或宿主调用。

    成功返回规范化视图（含 canonicalDigest）；失败抛 DocumentContractError，
    不产生规范化结果。
    """
    meta, text = read_source_file(path, allowed_roots=allowed_roots, max_bytes=max_bytes)
    obj, body = parse_frontmatter(text)
    document_kind = resolve_document_kind(obj, kind)

    errors = _validate_document(obj, document_kind)
    path_errors = validate_path_contract(obj, allowed_roots)
    for key, value in path_errors.items():
        errors.setdefault(key, value)
    for key, value in _validate_controlled_identity(
        obj, document_kind, cli_agent=cli_agent, cli_source=cli_source
    ).items():
        errors.setdefault(key, value)

    if errors:
        raise DocumentContractError(
            "document_invalid",
            message="文件已解析，但整体校验未通过；未写入任何状态",
            details={
                "documentKind": document_kind,
                "errorCount": len(errors),
                "errors": errors,
            },
        )

    body_bytes = len(body.encode("utf-8"))
    result = {
        "documentKind": document_kind,
        "schemaVersion": obj.get("schemaVersion"),
        "canonical": obj,
        "canonicalDigest": canonical_digest(obj),
        "sourceFile": {
            "path": meta["path"],
            "bytes": meta["bytes"],
            "sha256": meta["sha256"],
            "bodyBytes": body_bytes,
            "bodyLines": len(body.split("\n")),
            "bodyDigest": bytes_digest(body.encode("utf-8")),
            # 受控摘要：只取正文首个非空行并截断，不把完整技术正文塞进看板摘要
            "bodyHeadline": _body_headline(body),
        },
        "boardSummary": _pick(obj, "boardSummary"),
        "identity": _identity_view(obj, document_kind),
        "project": _project_view(obj, document_kind),
        "relations": _relations_view(obj, document_kind),
        "sessionMode": _pick(obj, "sessionMode"),
        "pathContract": {
            "checked": True,
            "workspace": _pick(obj, "workspace"),
            "writeScope": _pick(obj, "writeScope"),
            "errors": path_errors,
        },
        "validation": {"ok": True, "errorCount": 0, "errors": {}},
        "wouldWrite": False,
        "wouldDispatch": False,
        "sideEffects": {
            "databaseOpened": False,
            "databaseCreated": False,
            "taskCreated": False,
            "attemptCreated": False,
            "hostCalled": False,
            "eventWritten": False,
        },
    }
    if document_kind == "task-card":
        result["writePreview"] = build_write_preview(
            obj, cli_agent=cli_agent, cli_source=cli_source, allowed_roots=allowed_roots
        )
    else:
        # 本卡不做报告入账：只输出规范化对象，不写 delivery/event
        result["writePreview"] = {
            "command": "none",
            "ready": False,
            "reason": "本卡不提供 report import 写入口；回执入账留给 D4-NEXT-01C",
        }
    return result


# ---------------------------------------------------------------------------
# Markdown 任务卡 -> TaskService.create_task 入参映射
# 缺当前服务必填字段时拒绝创建，不自动补默认身份、目录、权限或成员
# ---------------------------------------------------------------------------

TASK_CREATE_REQUIRED_FROM_CARD = ("projectId", "title", "goal", "workspace", "permissions")


def task_card_to_create_payload(card, cli_agent=None, cli_source=None, allowed_roots=None):
    """把规范化任务卡映射为建卡入参。

    返回 (payload, missing, errors)：
    - missing/errors 任一非空 -> 调用方必须拒绝创建；
    - 身份或路径硬错误 -> 抛 DocumentContractError。
    """
    if not isinstance(card, dict):
        raise DocumentContractError("root_not_object", details={"type": type(card).__name__})

    missing = []
    errors = {}

    project_id = _pick(card, "projectId")
    if not isinstance(project_id, str) or not project_id.strip():
        missing.append("projectId")

    title = _pick(card, "title")
    if not isinstance(title, str) or len(title.strip()) < 2:
        missing.append("title")

    goal = _pick(card, "goal")
    if not isinstance(goal, str) or not goal.strip():
        missing.append("goal")

    workspace = _pick(card, "workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        missing.append("workspace")
    elif not workspace.startswith("/"):
        errors["workspace"] = {"code": "path_not_absolute"}
    elif ".." in workspace.split("/") or not _within_any(workspace, allowed_roots):
        errors["workspace"] = {"code": "path_escape", "path": workspace}

    permissions = _pick(card, "permissions")
    if not isinstance(permissions, dict):
        missing.append("permissions")
    else:
        for key, reason in validate_permissions(permissions).items():
            errors["permissions.%s" % key] = {"code": "invalid_task", "reason": reason}

    limits = _pick(card, "limits")
    if limits is not None:
        for key, reason in validate_limits(limits).items():
            errors[key] = {"code": "invalid_task", "reason": reason}

    acceptance = _pick(card, "acceptance")
    if acceptance is not None and not isinstance(acceptance, list):
        errors["acceptance"] = {"code": "invalid_task", "reason": "acceptance 必须是数组"}

    executor = _pick(card, "executor") if isinstance(_pick(card, "executor"), dict) else {}
    agent_id = resolve_agent_identity(executor.get("alias"), executor.get("agentId"), "executor")
    if not agent_id:
        missing.append("agentId")
    elif cli_agent and agent_id != cli_agent:
        raise DocumentContractError(
            "identity_conflict",
            message="任务卡执行成员不是受控入口登记的成员，拒绝建卡",
            details={"declared": agent_id, "controlled": cli_agent},
        )

    declared_source = _pick(card, "sourceId")
    if declared_source is not None and cli_source and declared_source != cli_source:
        raise DocumentContractError(
            "identity_conflict",
            message="任务卡声明的 sourceId 与受控入口不一致，拒绝建卡",
            details={"declared": declared_source, "controlled": cli_source},
        )

    path_errors = validate_path_contract(card, allowed_roots)
    for key, value in path_errors.items():
        errors.setdefault(key, value)

    payload = {
        "projectId": project_id,
        "title": title,
        "agentId": agent_id,
        "sourceId": cli_source or declared_source or "",
        "workspace": workspace,
        "goal": goal,
        "acceptance": acceptance if isinstance(acceptance, list) else [],
        "permissions": permissions if isinstance(permissions, dict) else {},
    }
    if isinstance(limits, dict):
        payload["limits"] = limits
    return payload, missing, errors


# ---------------------------------------------------------------------------
# D4-NEXT-01C-R1：从已校验任务卡构造「受控来源引用」
# 摘要一律由实际读取的字节计算，不信任卡内自报 SHA；cardId/cardRevision 必须是
# 已验证的稳定类型，畸形（含字符串/布尔冒充版本）拒绝。
# ---------------------------------------------------------------------------

def build_task_card_source_reference(document, path=None):
    """返回可交给 TaskService 的受控来源对象（cardId/cardRevision/path/摘要）。"""
    if not isinstance(document, dict):
        raise DocumentContractError(
            "identity_missing", message="来源任务卡文档必须是已校验的规范化对象"
        )
    canonical = document.get("canonical")
    if not isinstance(canonical, dict):
        raise DocumentContractError(
            "identity_missing", message="来源任务卡缺少规范化根对象，拒绝登记"
        )
    card_id = canonical.get("cardId")
    card_revision = canonical.get("cardRevision")
    project_id = canonical.get("projectId")
    if not isinstance(card_id, str) or not card_id.strip():
        raise DocumentContractError(
            "identity_missing",
            message="来源任务卡缺少 cardId，无法登记受控来源",
            details={"cardId": card_id},
        )
    # bool 是 int 子类：显式拒绝，让字符串/布尔/0/负数冒充版本在登记前就 fail closed。
    if not is_strict_card_revision(card_revision):
        raise DocumentContractError(
            "source_card_mismatch",
            message="来源任务卡 cardRevision 必须是 >=1 的整数，拒绝字符串/布尔/0/负数",
            details={"cardRevision": card_revision, "type": type(card_revision).__name__},
        )
    source_file = document.get("sourceFile") if isinstance(document.get("sourceFile"), dict) else {}
    return {
        "projectId": project_id,
        "cardId": card_id,
        "cardRevision": card_revision,
        "path": path or source_file.get("path"),
        "sha256": source_file.get("sha256"),
        "canonicalDigest": document.get("canonicalDigest"),
    }
