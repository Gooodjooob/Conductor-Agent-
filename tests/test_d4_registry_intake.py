"""D4-NEXT-02A3 定向测试：源 MD 名册只读预检纯合同（接口文档 §13.9）。

只测纯函数，不读文件、不落盘、不联网、不触 DB / 宿主：

- 只解析两处二级标题「当前 Agent 列表」「计划中的 Agent」下的主表；忽略其他表与
  fenced 代码块（含 fenced 假表）；
- 结构歧义（标题 / 表头 / 分隔 / 列数 / fence / 主表后非表格内容）受控拒绝；
- 行号 / rawLineDigest / source.digest 逐行对拍，CRLF / LF / 末行无行尾保真；
- escaped pipe、Unicode 别名冲突、非法 ID、未知 role / status、Auto / 多模型 /
  active 含 UNVERIFIED 保真；声明一律不作为验证结果，readyToApply 恒为 false；
- patch open / env / sqlite / socket / subprocess 为异常仍纯调用通过。

表驱动、精简实现，不写镜像断言。
"""
import contextlib
import hashlib
import os  # noqa: F401  （仅用于 patch 目标）
import socket  # noqa: F401
import sqlite3  # noqa: F401
import subprocess  # noqa: F401
import unittest
from unittest import mock

from contracts.registry_intake import (
    CURRENT_TABLE_HEADER,
    PLANNED_TABLE_HEADER,
    REGISTRY_INTAKE_SCHEMA_VERSION,
    inspect_legacy_registry_markdown,
)
from contracts.schemas import ContractError

SOURCE_REF = "/Users/demo/.hermes/skills/conductor/docs/development/agent-registry.md"

CURRENT_HEADER_LINE = "| Agent | 平台 | 模型 | 角色 | 负责范围 | 权限 | 状态 |"
CURRENT_SEP_LINE = "|-------|------|------|------|----------|------|------|"
PLANNED_HEADER_LINE = "| Agent | 平台 | 模型 | 角色 | 计划负责范围 | 权限 | 状态 |"
PLANNED_SEP_LINE = "|-------|------|------|------|--------------|------|------|"


def row(agent, platform, model, role, scope, permission, status):
    return "| %s | %s | %s | %s | %s | %s | %s |" % (
        agent, platform, model, role, scope, permission, status,
    )


DEFAULT_CURRENT_ROW = row(
    "cox", "Codex", "GPT-5.5", "`sales-architect`", "架构与 review", "决策审查", "active",
)
DEFAULT_PLANNED_ROW = row(
    "glm-worker-1", "Trae", "GLM5.2", "`skill-worker`", "实现代码", "不允许发布", "planned",
)


def build_minimal(*, current_rows=(DEFAULT_CURRENT_ROW,), planned_rows=(DEFAULT_PLANNED_ROW,)):
    """最小名册：计划表数据行即最后一行（用于行尾保真测试）。"""
    lines = ["# R", "", "## 当前 Agent 列表", "", CURRENT_HEADER_LINE, CURRENT_SEP_LINE]
    lines.extend(current_rows)
    lines += ["", "## 计划中的 Agent", "", PLANNED_HEADER_LINE, PLANNED_SEP_LINE]
    lines.extend(planned_rows)
    return "\n".join(lines)


def build_md(
    *,
    current_title="## 当前 Agent 列表",
    current_header=CURRENT_HEADER_LINE,
    current_sep=CURRENT_SEP_LINE,
    current_rows=(DEFAULT_CURRENT_ROW,),
    planned_title="## 计划中的 Agent",
    planned_header=PLANNED_HEADER_LINE,
    planned_sep=PLANNED_SEP_LINE,
    planned_rows=(DEFAULT_PLANNED_ROW,),
    tail="\n",
):
    """合成一份包含目标主表、其他表与 fenced 假表的最小名册。"""
    lines = ["# Agent Registry", "", "## 目标", "", "示例说明段落。", "", current_title, ""]
    if current_header is not None:
        lines.append(current_header)
    if current_sep is not None:
        lines.append(current_sep)
    lines.extend(current_rows)
    lines += ["", planned_title, ""]
    if planned_header is not None:
        lines.append(planned_header)
    if planned_sep is not None:
        lines.append(planned_sep)
    lines.extend(planned_rows)
    lines += [
        "",
        "## 权限规则",
        "",
        "- 只读说明。",
        "",
        "## Agent 变更记录",
        "",
        "| 日期 | 变更 | 说明 |",
        "|------|------|------|",
        "| 2026-10-08 | 示例 | 其他表须被忽略 |",
        "",
        "## 新 Agent 登记模板",
        "",
        "```markdown",
        CURRENT_HEADER_LINE,
        CURRENT_SEP_LINE,
        row("fake-agent", "Fenced", "fake-model", "`skill-worker`", "假表", "无", "planned"),
        "```",
        "",
    ]
    return "\n".join(lines) + tail


class ContractErrorMixin(object):
    def assert_contract_error(self, fn, field=None):
        with self.assertRaises(ContractError) as ctx:
            fn()
        self.assertEqual(ctx.exception.code, "invalid_task")
        if field is not None:
            self.assertEqual(ctx.exception.details.get("field"), field)
        return ctx.exception


class PreviewEnvelopeTests(ContractErrorMixin, unittest.TestCase):
    def test_minimal_valid_envelope(self):
        text = build_md()
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual(result["schemaVersion"], REGISTRY_INTAKE_SCHEMA_VERSION)
        self.assertEqual(result["source"]["ref"], SOURCE_REF)
        self.assertEqual(result["source"]["bytes"], len(text.encode("utf-8")))
        self.assertEqual(
            result["source"]["digest"],
            "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
        self.assertFalse(result["wouldWrite"])
        self.assertFalse(result["wouldDispatch"])
        self.assertTrue(result["authorityUnchanged"])
        self.assertFalse(result["readyToApply"])
        self.assertEqual(
            [entry["sourceTable"] for entry in result["entries"]], ["current", "planned"]
        )
        self.assertEqual(result["diagnostics"], [])

    def test_headers_match_contract(self):
        self.assertEqual(CURRENT_TABLE_HEADER[4], "负责范围")
        self.assertEqual(PLANNED_TABLE_HEADER[4], "计划负责范围")
        self.assertEqual(len(CURRENT_TABLE_HEADER), 7)
        self.assertEqual(len(PLANNED_TABLE_HEADER), 7)

    def test_ignores_other_tables_and_fenced_fake_table(self):
        result = inspect_legacy_registry_markdown(build_md(), source_ref=SOURCE_REF)
        agents = [entry["legacyAlias"] for entry in result["entries"]]
        self.assertNotIn("fake-agent", agents)
        self.assertEqual(agents, ["cox", "glm-worker-1"])

    def test_ready_to_apply_false_even_without_diagnostics(self):
        result = inspect_legacy_registry_markdown(build_md(), source_ref=SOURCE_REF)
        self.assertEqual(result["diagnostics"], [])
        self.assertIs(result["readyToApply"], False)


class EntryFieldTests(ContractErrorMixin, unittest.TestCase):
    def test_alias_unwrap_normalize_and_proposed_id(self):
        text = build_md(
            current_rows=(
                row("`COX`", "Codex", "GPT-5.5", "`sales-architect`", "架构", "决策", "active"),
            ),
            planned_rows=(),
        )
        entry = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)["entries"][0]
        self.assertEqual(entry["cells"][0], "`COX`")
        self.assertEqual(entry["legacyAlias"], "COX")
        self.assertEqual(entry["normalizedAlias"], "cox")
        self.assertEqual(entry["proposedAgentId"], "COX")
        self.assertEqual(entry["declaredRoles"], ["sales-architect"])
        self.assertIs(entry["suggestedEnabled"], False)
        self.assertEqual(
            entry["unresolvedFields"],
            ["environmentId", "allowedRoots", "permissionCeiling", "modelProfileId", "binding"],
        )
        self.assertEqual(
            sorted(entry["declared"].keys()),
            ["modelText", "permissionText", "platformText", "roleText", "scopeText", "statusText"],
        )
        self.assertEqual(entry["declared"]["roleText"], "`sales-architect`")
        self.assertEqual(entry["declared"]["scopeText"], "架构")

    def test_line_numbers_and_raw_line_digest(self):
        target = row("dev-1", "QClaw", "qclaw/pool-glm-5.2", "`skill-worker`", "实现", "允许写", "active")
        text = build_md(current_rows=(target,), planned_rows=())
        entry = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)["entries"][0]
        self.assertEqual(entry["lineRange"]["start"], entry["lineRange"]["end"])
        self.assertEqual(entry["rawLine"], target)
        self.assertEqual(
            entry["rawLineDigest"],
            "sha256:" + hashlib.sha256((target + "\n").encode("utf-8")).hexdigest(),
        )

    def test_crlf_and_lf_and_missing_trailing_newline_preserved(self):
        lf_text = build_minimal() + "\n"
        crlf_text = lf_text.replace("\n", "\r\n")
        no_tail_text = build_minimal()

        lf = inspect_legacy_registry_markdown(lf_text, source_ref=SOURCE_REF)
        crlf = inspect_legacy_registry_markdown(crlf_text, source_ref=SOURCE_REF)
        no_tail = inspect_legacy_registry_markdown(no_tail_text, source_ref=SOURCE_REF)

        self.assertNotEqual(lf["source"]["digest"], crlf["source"]["digest"])
        self.assertEqual(
            crlf["source"]["digest"],
            "sha256:" + hashlib.sha256(crlf_text.encode("utf-8")).hexdigest(),
        )
        # 内容行相同（行尾不同），rawLine 不含行尾
        self.assertEqual(lf["entries"][0]["rawLine"], crlf["entries"][0]["rawLine"])
        self.assertNotEqual(
            lf["entries"][0]["rawLineDigest"], crlf["entries"][0]["rawLineDigest"]
        )
        self.assertEqual(
            lf["entries"][0]["rawLineDigest"],
            "sha256:" + hashlib.sha256(
                (lf["entries"][0]["rawLine"] + "\n").encode("utf-8")
            ).hexdigest(),
        )
        self.assertEqual(
            crlf["entries"][0]["rawLineDigest"],
            "sha256:" + hashlib.sha256(
                (crlf["entries"][0]["rawLine"] + "\r\n").encode("utf-8")
            ).hexdigest(),
        )
        # 末行无行尾：rawLineDigest 只含原字节
        last = no_tail["entries"][-1]
        self.assertEqual(last["sourceTable"], "planned")
        self.assertEqual(
            last["rawLineDigest"],
            "sha256:" + hashlib.sha256(last["rawLine"].encode("utf-8")).hexdigest(),
        )

    def test_escaped_pipe_kept_in_single_cell(self):
        target = row("dev-1", "QClaw", "m1", "`skill-worker`", "a \\| b", "p", "active")
        text = build_md(current_rows=(target,), planned_rows=())
        entry = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)["entries"][0]
        self.assertEqual(len(entry["cells"]), 7)
        self.assertEqual(entry["cells"][4], "a \\| b")

    def test_role_unknown_is_diagnostic_not_drop(self):
        text = build_md(
            current_rows=(row("x1", "P", "m", "`wizard`", "s", "p", "active"),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual(len(result["entries"]), 1)
        self.assertIsNone(result["entries"][0]["declaredRoles"])
        kinds = [d["kind"] for d in result["diagnostics"]]
        self.assertIn("role_unknown", kinds)

    def test_illegal_agent_id_diagnostic_row_kept(self):
        text = build_md(
            current_rows=(row("not a valid id", "P", "m", "`skill-worker`", "s", "p", "active"),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        entry = result["entries"][0]
        self.assertEqual(len(result["entries"]), 1)
        self.assertIsNone(entry["proposedAgentId"])
        self.assertEqual(entry["normalizedAlias"], "not a valid id")
        self.assertIn("agent_id_invalid", [d["kind"] for d in result["diagnostics"]])

    def test_unusable_alias_diagnostic_row_kept(self):
        text = build_md(
            current_rows=(row("\ufeffbad", "P", "m", "`skill-worker`", "s", "p", "active"),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        entry = result["entries"][0]
        self.assertEqual(len(result["entries"]), 1)
        self.assertIsNone(entry["normalizedAlias"])
        self.assertIn("alias_unusable", [d["kind"] for d in result["diagnostics"]])

    def test_unknown_status_diagnostic_row_kept(self):
        text = build_md(
            current_rows=(row("x1", "P", "m", "`skill-worker`", "s", "p", "retired"),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        entry = result["entries"][0]
        self.assertEqual(len(result["entries"]), 1)
        self.assertFalse(entry["suggestedEnabled"])
        self.assertIn("status_unknown", [d["kind"] for d in result["diagnostics"]])

    def test_auto_multi_model_and_active_unverified_preserved(self):
        model = "Auto（多模型：K3 / Deepseek V4 Flash / Hunyuan 3）"
        status = "active（runtime canary 已通过；宿主 Agent 身份与工具禁用仍 UNVERIFIED）"
        text = build_md(
            current_rows=(row("qa-3", "WorkBuddy", model, "`qa-reporter`", "只读 QA", "只读", status),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        entry = result["entries"][0]
        self.assertEqual(entry["declared"]["modelText"], model)
        self.assertEqual(entry["declared"]["statusText"], status)
        self.assertIn("UNVERIFIED", entry["declared"]["statusText"])
        self.assertEqual(entry["declaredRoles"], ["qa-reporter"])
        self.assertFalse(entry["suggestedEnabled"])
        self.assertFalse(result["readyToApply"])
        # 状态带括号注释视为可识别，不产生 status_unknown
        self.assertEqual(result["diagnostics"], [])
        # entry 字段固定：不含任何"已验证 / 启用=true"字段
        self.assertEqual(
            sorted(entry.keys()),
            [
                "cells", "declared", "declaredRoles", "legacyAlias", "lineRange",
                "normalizedAlias", "proposedAgentId", "rawLine", "rawLineDigest",
                "sourceTable", "suggestedEnabled", "unresolvedFields",
            ],
        )

    def test_disabled_status_recognized(self):
        text = build_md(
            current_rows=(row("qa-3", "P", "m", "`qa-reporter`", "s", "p", "disabled"),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual(result["diagnostics"], [])
        self.assertFalse(result["entries"][0]["suggestedEnabled"])


class ConflictTests(ContractErrorMixin, unittest.TestCase):
    def test_normalized_alias_conflict_across_tables_keeps_all_rows(self):
        text = build_md(
            current_rows=(row("dev-1", "QClaw", "m", "`skill-worker`", "s", "p", "active"),),
            planned_rows=(row("DEV-1", "Trae", "m", "`skill-worker`", "s", "p", "planned"),),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual(len(result["entries"]), 2)
        conflicts = [d for d in result["diagnostics"] if d["kind"] == "normalized_alias_conflict"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["lineNumbers"], sorted(conflicts[0]["lineNumbers"]))
        self.assertEqual(len(conflicts[0]["lineNumbers"]), 2)
        self.assertFalse(result["readyToApply"])

    def test_unicode_nfc_casefold_conflict(self):
        text = build_md(
            current_rows=(
                row("Caf\u00e9", "P", "m", "`skill-worker`", "s", "p", "active"),
                row("CAFE\u0301", "P", "m", "`skill-worker`", "s", "p", "active"),
            ),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual(len(result["entries"]), 2)
        self.assertIn(
            "normalized_alias_conflict", [d["kind"] for d in result["diagnostics"]]
        )

    def test_conflict_diagnostic_does_not_echo_full_input(self):
        text = build_md(
            current_rows=(row("dev-1", "P", "m", "`skill-worker`", "s", "p", "active"),),
            planned_rows=(row("dev-1", "P", "m", "`skill-worker`", "s", "p", "planned"),),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        for diag in result["diagnostics"]:
            self.assertNotIn(text, diag["reason"])
            self.assertLessEqual(len(diag["reason"]), 200)


class StructuralRejectionTests(ContractErrorMixin, unittest.TestCase):
    def test_missing_current_heading_rejected(self):
        text = build_md(current_title="## 当前 Agent 列表(改)")
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_missing_planned_heading_rejected(self):
        text = build_md(planned_title="## 计划中的 Agent(改)")
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_duplicate_heading_rejected(self):
        text = build_md(planned_title="## 当前 Agent 列表")
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_missing_table_rejected(self):
        text = build_md(current_header=None, current_sep=None, current_rows=())
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_wrong_header_name_rejected(self):
        bad = "| Agent | 平台 | 模型 | 角色 | 范围 | 权限 | 状态 |"
        text = build_md(current_header=bad)
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_wrong_header_count_rejected(self):
        bad = "| Agent | 平台 | 模型 | 角色 | 负责范围 | 状态 |"
        text = build_md(current_header=bad)
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_bad_separator_rejected(self):
        bad = "|--|--|--|--|--|--|--|"  # 每列不足三横线
        text = build_md(current_sep=bad)
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_planned_scope_column_required(self):
        # 计划表第五列必须是「计划负责范围」，用「负责范围」应拒绝
        text = build_md(
            planned_header="| Agent | 平台 | 模型 | 角色 | 负责范围 | 权限 | 状态 |"
        )
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_data_row_wrong_column_count_rejected(self):
        text = build_md(
            current_rows=(DEFAULT_CURRENT_ROW, "| a | b | c | d | e | f |"),
            planned_rows=(),
        )
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_broken_row_missing_trailing_pipe_rejected(self):
        text = build_md(
            current_rows=(DEFAULT_CURRENT_ROW, "| broken | x | y | z | q | w | active"),
            planned_rows=(),
        )
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_unclosed_fence_rejected(self):
        text = build_md() + "```\n"
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_fenced_fake_table_inside_target_section_ignored(self):
        lines = [
            "# Agent Registry",
            "",
            "## 当前 Agent 列表",
            "",
            CURRENT_HEADER_LINE,
            CURRENT_SEP_LINE,
            row("real-agent", "P", "m", "`skill-worker`", "s", "p", "active"),
            "",
            "```markdown",
            CURRENT_HEADER_LINE,
            CURRENT_SEP_LINE,
            row("fake-agent", "P", "m", "`skill-worker`", "s", "p", "active"),
            "```",
            "",
            "## 计划中的 Agent",
            "",
            PLANNED_HEADER_LINE,
            PLANNED_SEP_LINE,
            DEFAULT_PLANNED_ROW,
            "",
        ]
        result = inspect_legacy_registry_markdown("\n".join(lines) + "\n", source_ref=SOURCE_REF)
        agents = [entry["legacyAlias"] for entry in result["entries"]]
        self.assertEqual(agents, ["real-agent", "glm-worker-1"])
        self.assertNotIn("fake-agent", agents)


class SourceRefTests(ContractErrorMixin, unittest.TestCase):
    def test_valid_absolute_path_without_stat(self):
        text = build_md()
        result = inspect_legacy_registry_markdown(
            text, source_ref="/nonexistent/root/agent-registry.md"
        )
        self.assertEqual(result["source"]["ref"], "/nonexistent/root/agent-registry.md")

    def test_reject_relative_url_dot_and_empty_segments(self):
        text = build_md()
        bad_refs = [
            "docs/development/agent-registry.md",
            "https://example.com/agent-registry.md",
            "/a/b/",
            "/a/../b",
            "/a//b",
            "/a/./b",
            "/a/\x00b",
        ]
        for ref in bad_refs:
            with self.subTest(ref=ref):
                self.assert_contract_error(
                    lambda r=ref: inspect_legacy_registry_markdown(text, source_ref=r),
                    "source_ref",
                )

    def test_reject_non_string_source_ref(self):
        text = build_md()
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=123), "source_ref"
        )


class TextInputTests(ContractErrorMixin, unittest.TestCase):
    def test_non_string_input_rejected(self):
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(b"# x", source_ref=SOURCE_REF), "text"
        )
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(None, source_ref=SOURCE_REF), "text"
        )

    def test_oversized_text_rejected(self):
        text = "x" * (1024 * 1024 + 1)
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "text"
        )

    def test_unpaired_surrogate_encoding_controlled(self):
        text = build_md() + "\ud800"
        error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "text"
        )
        self.assertNotIn("\ud800", str(error))

    def test_encoding_and_markdown_reasons_are_distinguished(self):
        encoding_error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(build_md() + "\ud800", source_ref=SOURCE_REF),
            "text",
        )
        structure_error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(
                build_md(current_title="## 改"), source_ref=SOURCE_REF
            ),
            "markdown",
        )
        self.assertNotEqual(encoding_error.details["reason"], structure_error.details["reason"])


class NoExternalReadTests(unittest.TestCase):
    def test_pure_function_has_no_fs_env_db_network_host_side_effects(self):
        boom = "纯函数不应触发外部读取"
        patches = [
            mock.patch("builtins.open", side_effect=AssertionError(boom)),
            mock.patch("os.stat", side_effect=AssertionError(boom)),
            mock.patch("os.lstat", side_effect=AssertionError(boom)),
            mock.patch("os.listdir", side_effect=AssertionError(boom)),
            mock.patch("os.scandir", side_effect=AssertionError(boom)),
            mock.patch("os.getenv", side_effect=AssertionError(boom)),
            mock.patch("socket.socket", side_effect=AssertionError(boom)),
            mock.patch("sqlite3.connect", side_effect=AssertionError(boom)),
            mock.patch("subprocess.run", side_effect=AssertionError(boom)),
            mock.patch("subprocess.Popen", side_effect=AssertionError(boom)),
        ]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            result = inspect_legacy_registry_markdown(build_md(), source_ref=SOURCE_REF)
            self.assertEqual(len(result["entries"]), 2)
            self.assertFalse(result["readyToApply"])


class ReworkR1BoundaryTests(ContractErrorMixin, unittest.TestCase):
    """02A3-R1 窄返工新增回归：失败于旧候选、通过新候选，原 37 项断言保留不动。"""

    # --- F1：连续 / 间隔重复主表与重复分隔行，受控拒绝 -------------------

    def test_contiguous_second_table_in_current_rejected(self):
        text = build_md(
            current_rows=(
                DEFAULT_CURRENT_ROW,
                CURRENT_HEADER_LINE,
                CURRENT_SEP_LINE,
                row("dev-c", "P", "m", "`skill-worker`", "s", "p", "active"),
            ),
        )
        error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )
        self.assertIn("重复表头", error.details["reason"])

    def test_contiguous_second_table_in_planned_rejected(self):
        text = build_md(
            planned_rows=(
                DEFAULT_PLANNED_ROW,
                PLANNED_HEADER_LINE,
                PLANNED_SEP_LINE,
                row("qa-z", "P", "m", "`qa-reporter`", "s", "p", "planned"),
            ),
        )
        error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )
        self.assertIn("重复表头", error.details["reason"])

    def test_contiguous_second_separator_rejected(self):
        text = build_md(current_rows=(DEFAULT_CURRENT_ROW, CURRENT_SEP_LINE))
        error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )
        self.assertIn("重复分隔行", error.details["reason"])

    def test_blank_line_separated_second_table_rejected(self):
        text = build_md(
            current_rows=(
                DEFAULT_CURRENT_ROW,
                "",
                CURRENT_HEADER_LINE,
                CURRENT_SEP_LINE,
                row("dev-c", "P", "m", "`skill-worker`", "s", "p", "active"),
            ),
        )
        self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )

    def test_no_fake_agent_or_separator_member_created(self):
        # 旧候选会把重复表头 / 分隔行当成员行，产出 Agent / --- 假成员
        text = build_md(
            current_rows=(DEFAULT_CURRENT_ROW, CURRENT_HEADER_LINE, CURRENT_SEP_LINE),
            planned_rows=(DEFAULT_PLANNED_ROW, PLANNED_SEP_LINE),
        )
        error = self.assert_contract_error(
            lambda: inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF), "markdown"
        )
        self.assertNotIn("---", error.details["reason"])

    def test_normal_multi_row_table_and_escaped_pipe_still_parsed(self):
        escaped = row("dev-2", "P", "m", "`skill-worker`", "a \\| b", "p", "active")
        text = build_md(current_rows=(DEFAULT_CURRENT_ROW, escaped), planned_rows=())
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual([e["legacyAlias"] for e in result["entries"]], ["cox", "dev-2"])
        self.assertEqual(result["entries"][1]["cells"][4], "a \\| b")
        self.assertEqual(result["diagnostics"], [])

    def test_legitimate_agent_literal_cell_not_rejected(self):
        # 合法成员行的单 cell 文字为「Agent」时不得被粗暴拒绝（整行七列比对）
        text = build_md(
            current_rows=(row("Agent", "P", "m", "`skill-worker`", "s", "p", "active"),),
            planned_rows=(),
        )
        result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
        self.assertEqual(len(result["entries"]), 1)
        self.assertEqual(result["entries"][0]["legacyAlias"], "Agent")

    # --- F2：非法状态括号只诊断、不丢行 -----------------------------------

    def test_bad_status_bracket_variants_diagnostic_row_kept(self):
        bad_statuses = (
            "active（note",            # 未闭合
            "disabled(note）",         # 混合配对
            "planned(note) trailing",  # 闭合后尾随
        )
        for status in bad_statuses:
            with self.subTest(status=status):
                text = build_md(
                    current_rows=(row("x1", "P", "m", "`skill-worker`", "s", "p", status),),
                    planned_rows=(),
                )
                result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
                entry = result["entries"][0]
                self.assertEqual(len(result["entries"]), 1)
                self.assertIs(entry["suggestedEnabled"], False)
                self.assertIs(result["readyToApply"], False)
                self.assertEqual(entry["declared"]["statusText"], status)
                unknown = [d for d in result["diagnostics"] if d["kind"] == "status_unknown"]
                self.assertEqual(len(unknown), 1)
                self.assertEqual(unknown[0]["field"], "cells[6]")
                self.assertEqual(unknown[0]["lineNumbers"], [entry["lineRange"]["start"]])

    def test_normal_status_bracket_still_recognized(self):
        good_statuses = ("active", "active（note）", "disabled(note)", "planned（待启用）")
        for status in good_statuses:
            with self.subTest(status=status):
                text = build_md(
                    current_rows=(row("x1", "P", "m", "`skill-worker`", "s", "p", status),),
                    planned_rows=(),
                )
                result = inspect_legacy_registry_markdown(text, source_ref=SOURCE_REF)
                self.assertEqual(result["diagnostics"], [])
                self.assertIs(result["entries"][0]["suggestedEnabled"], False)


if __name__ == "__main__":
    unittest.main()
