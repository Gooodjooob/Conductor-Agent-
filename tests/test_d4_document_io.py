"""D4-NEXT-01B 定向测试：Markdown/frontmatter 解析、规范化与只读预检。

覆盖任务卡 §7 要求：正例、缺字段、重复键、YAML 标签/锚点/别名/合并键、类型错误、
未知身份、schema 交叉、摘要技术词、路径越界、符号链接、正文伪造“已通过”、
超大/过深结构、同一输入 digest 一致性、document inspect 无副作用、JSON 兼容回归。

runtime 落点：默认写本卡证据目录 `document-001/20261002-devwkb1-run1/runtime/`，
可用环境变量 `D4_DOC_RUNTIME_DIR` 覆盖（仅用于本地调试，不写正式任务库）。
"""
import json
import os
import sqlite3
import unittest
from pathlib import Path

from contracts.document_io import (
    DocumentContractError,
    canonical_digest,
    inspect_document,
    parse_frontmatter,
    task_card_to_create_payload,
)
from scripts import d4ctl

EVIDENCE_RUNTIME = (
    "/Users/demo/.hermes/skills/conductor/.conductor-local/d4-real-chain-validation/"
    "document-001/20261002-devwkb1-run1/runtime"
)

CLI_AGENT = "dev-1"
CLI_SOURCE = "qclaw-gateway"


def runtime_root():
    base = os.environ.get("D4_DOC_RUNTIME_DIR") or EVIDENCE_RUNTIME
    root = Path(base)
    root.mkdir(parents=True, exist_ok=True)
    return root


def write_doc(name, text):
    path = Path(runtime_root()) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# 受限 YAML 文本生成（测试夹具用；生产解析只走 contracts.document_io）
# ---------------------------------------------------------------------------

def _scalar_text(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % text


def _dump(obj, indent=0):
    lines = []
    pad = " " * indent
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, dict) and value:
                lines.append("%s%s:" % (pad, key))
                lines.append(_dump(value, indent + 2))
            elif isinstance(value, list) and value:
                lines.append("%s%s:" % (pad, key))
                lines.append(_dump(value, indent + 2))
            elif isinstance(value, (dict, list)):
                lines.append("%s%s: %s" % (pad, key, "{}" if isinstance(value, dict) else "[]"))
            else:
                lines.append("%s%s: %s" % (pad, key, _scalar_text(value)))
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                block = _dump(item, indent + 2)
                first, _, rest = block.partition("\n")
                lines.append("%s- %s" % (pad, first.strip()))
                if rest:
                    lines.append(rest)
            else:
                lines.append("%s- %s" % (pad, _scalar_text(item)))
    return "\n".join(lines)


def _workspace():
    return os.path.join(str(runtime_root()), "workspace")


def _task_card_frontmatter(**over):
    base = {
        "schemaVersion": "task-card-board.v1",
        "cardId": "D4-NEXT-01B-TEST",
        "cardRevision": 1,
        "projectId": "conductor-d4",
        "title": "测试任务卡的只读预检",
        "goal": "验证规范化输出和建卡映射",
        "workspace": _workspace(),
        "permissions": {
            "writeScope": ["receipts/"],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        },
        "issuer": {
            "alias": "cox",
            "role": "sales-architect",
            "host": "Codex",
            "agentId": "cox",
        },
        "executor": {
            "alias": "dev-1",
            "role": "skill-worker",
            "host": "QClaw",
            "agentId": "dev-1",
            "environmentId": "qclaw-local",
        },
        "reviewer": {
            "alias": "cox",
            "role": "sales-architect",
        },
        "sessionMode": "new",
        "boardSummary": {
            "title": "测试先检查再建卡",
            "stage": "解析和只读预检已经写完",
            "problem": "写入前还要确认字段是否齐全",
            "nextStep": "等负责人复核测试结果",
        },
    }
    base.update(over)
    return base


def task_card_md(body="正文：技术细节保留在这里。", **over):
    return "---\n%s\n---\n\n%s\n" % (_dump(_task_card_frontmatter(**over)), body)


def _report_frontmatter(**over):
    base = {
        "schemaVersion": "report-board.v1",
        "reportId": "D4-NEXT-01B-TEST-QA",
        "reportRevision": 1,
        "reportType": "qa",
        "projectId": "conductor-d4",
        "taskId": "task-test-001",
        "attemptId": "attempt-test-001",
        "sourceCard": {
            "path": os.path.join(str(runtime_root()), "examples", "task-card.example.md"),
            "cardId": "D4-NEXT-01B-TEST",
            "cardRevision": 1,
        },
        "requestedBy": {
            "alias": "cox",
            "role": "sales-architect",
            "host": "Codex",
            "agentId": "cox",
        },
        "executedBy": {
            "alias": "qa-1",
            "role": "qa-reporter",
            "host": "OpenClaw",
            "agentId": "qa-1",
            "environmentId": "qclaw-local",
        },
        "adapter": "unverified",
        "sessionId": "unverified",
        "hostRunId": "unverified",
        "reviewedBy": {
            "alias": "cox",
            "role": "sales-architect",
        },
        "businessSummary": {
            "summary": "只读预检能输出规范化对象",
            "owner": "cox",
            "unfinished": [],
            "impact": [],
            "nextStep": "等负责人确认后续卡",
        },
        "evidence": [
            {"kind": "command", "detail": "只读预检命令", "level": "reported"},
            {"kind": "test", "detail": "正例与负例测试", "level": "reported"},
        ],
        "boardSummary": {
            "stage": "测试报告已生成",
            "result": "只读检查能输出规范化结果",
            "problem": "回执入账还没有开放",
            "nextStep": "等负责人确认后续卡",
        },
    }
    base.update(over)
    return base


def report_md(body="正文：报告正文不承载可信状态。", **over):
    return "---\n%s\n---\n\n%s\n" % (_dump(_report_frontmatter(**over)), body)


def inspect(path, kind=None, **kwargs):
    return inspect_document(
        path,
        kind=kind,
        allowed_roots=[str(runtime_root())],
        cli_agent=CLI_AGENT,
        cli_source=CLI_SOURCE,
        **kwargs
    )


def assert_fails(test, path, kind=None, code=None):
    with test.assertRaises(DocumentContractError) as ctx:
        inspect(path, kind=kind)
    if code:
        test.assertEqual(ctx.exception.code, code)
    return ctx.exception


# ---------------------------------------------------------------------------
# 1. 受限 YAML 子集：正例
# ---------------------------------------------------------------------------

class RestrictedYamlPositiveTests(unittest.TestCase):
    def test_block_mapping_and_nested_objects(self):
        text = (
            "---\n"
            'schemaVersion: "task-card-board.v1"\n'
            "cardRevision: 3\n"
            "issuer:\n"
            '  alias: "cox"\n'
            '  host: "Codex"\n'
            "---\n\n正文\n"
        )
        obj, body = parse_frontmatter(text)
        self.assertEqual(obj["schemaVersion"], "task-card-board.v1")
        self.assertEqual(obj["cardRevision"], 3)
        self.assertEqual(obj["issuer"]["alias"], "cox")
        self.assertIn("正文", body)

    def test_block_sequence_and_inline_mapping_sequence(self):
        text = (
            "---\n"
            "evidence:\n"
            '  - kind: "command"\n'
            '    detail: "跑了预检"\n'
            '    level: "verified"\n'
            "tags:\n"
            '  - "a"\n'
            '  - "b"\n'
            "---\n\n正文\n"
        )
        obj, _ = parse_frontmatter(text)
        self.assertEqual(obj["evidence"][0]["kind"], "command")
        self.assertEqual(obj["evidence"][0]["level"], "verified")
        self.assertEqual(obj["tags"], ["a", "b"])

    def test_scalar_types_are_explicit(self):
        text = (
            "---\n"
            'yes_string: "yes"\n'
            "enabled: true\n"
            "disabled: false\n"
            "nothing: null\n"
            "tilde: ~\n"
            "empty_list: []\n"
            "empty_map: {}\n"
            "negative: -3\n"
            'quoted_date: "2026-10-02"\n'
            "---\n\n正文\n"
        )
        obj, _ = parse_frontmatter(text)
        self.assertEqual(obj["yes_string"], "yes")
        self.assertIs(obj["enabled"], True)
        self.assertIs(obj["disabled"], False)
        self.assertIsNone(obj["nothing"])
        self.assertIsNone(obj["tilde"])
        self.assertEqual(obj["empty_list"], [])
        self.assertEqual(obj["empty_map"], {})
        self.assertEqual(obj["negative"], -3)
        self.assertEqual(obj["quoted_date"], "2026-10-02")

    def test_quoted_strings_and_limited_escapes(self):
        text = (
            "---\n"
            'single: \'a\'\'b\'\n'
            'double: "a\\"b\\nc"\n'
            'colon: "key: value"\n'
            'hash: "value # not comment"\n'
            "---\n\n正文\n"
        )
        obj, _ = parse_frontmatter(text)
        self.assertEqual(obj["single"], "a'b")
        self.assertEqual(obj["double"], 'a"b\nc')
        self.assertEqual(obj["colon"], "key: value")
        self.assertEqual(obj["hash"], "value # not comment")

    def test_comments_and_blank_lines_are_ignored(self):
        text = "---\n# 注释\n\n" + 'key: "value"\n' + "\n---\n\n正文\n"
        obj, _ = parse_frontmatter(text)
        self.assertEqual(obj, {"key": "value"})


# ---------------------------------------------------------------------------
# 2. 受限 YAML 子集：负例（fail closed）
# ---------------------------------------------------------------------------

class RestrictedYamlNegativeTests(unittest.TestCase):
    def _code(self, frontmatter):
        try:
            parse_frontmatter("---\n%s\n---\n\n正文\n" % frontmatter)
        except DocumentContractError as exc:
            return exc.code
        self.fail("应当拒绝：%s" % frontmatter)

    def test_duplicate_key_rejected(self):
        self.assertEqual(self._code('key: "a"\nkey: "b"'), "duplicate_key")

    def test_nested_duplicate_key_rejected(self):
        self.assertEqual(self._code('issuer:\n  alias: "cox"\n  alias: "dev-1"'), "duplicate_key")

    def test_anchor_rejected(self):
        self.assertEqual(self._code('base: &anchor "x"'), "yaml_anchor_rejected")

    def test_alias_rejected(self):
        self.assertEqual(self._code('use: *a'), "yaml_alias_rejected")

    def test_merge_key_rejected(self):
        self.assertEqual(self._code('child:\n  <<: *a'), "yaml_merge_key_rejected")

    def test_tag_rejected(self):
        self.assertEqual(self._code('value: !!str "x"'), "yaml_tag_rejected")

    def test_python_object_tag_rejected(self):
        self.assertEqual(
            self._code('value: !!python/object/apply:os.system ["echo hi"]'), "yaml_tag_rejected"
        )

    def test_directive_rejected(self):
        self.assertEqual(self._code('%YAML 1.2\nkey: "v"'), "yaml_directive_rejected")

    def test_block_scalar_rejected(self):
        self.assertEqual(self._code('summary: |\n  多行文本'), "yaml_block_scalar_rejected")

    def test_nested_flow_rejected(self):
        self.assertEqual(self._code("items: [[1, 2]]"), "yaml_flow_rejected")

    def test_unknown_escape_rejected(self):
        self.assertEqual(self._code('value: "\\u0041"'), "yaml_escape_rejected")

    def test_ambiguous_bool_rejected(self):
        self.assertEqual(self._code("enabled: yes"), "yaml_type_unresolved")

    def test_unquoted_date_rejected(self):
        self.assertEqual(self._code("issuedAt: 2026-10-02"), "yaml_type_unresolved")

    def test_float_rejected(self):
        self.assertEqual(self._code("ratio: 1.5"), "yaml_type_unresolved")

    def test_hex_and_leading_zero_rejected(self):
        self.assertEqual(self._code("value: 0x10"), "yaml_type_unresolved")
        self.assertEqual(self._code("value: 007"), "yaml_type_unresolved")

    def test_tab_rejected(self):
        self.assertEqual(self._code('issuer:\n\talias: "cox"'), "yaml_tab_rejected")

    def test_sequence_root_rejected(self):
        self.assertEqual(self._code('- "a"\n- "b"'), "root_not_object")

    def test_scalar_root_rejected(self):
        self.assertEqual(self._code('"just a string"'), "root_not_object")

    def test_empty_frontmatter_rejected(self):
        self.assertEqual(self._code("# 只有注释"), "root_not_object")

    def test_indent_error_rejected(self):
        self.assertEqual(self._code('issuer:\n    alias: "cox"\n  host: "Codex"'), "yaml_indent_error")

    def test_inline_comment_in_plain_scalar_rejected(self):
        self.assertEqual(self._code('title: 中文 # 注释'), "yaml_syntax_error")

    def test_too_deep_rejected(self):
        lines = []
        for index in range(12):
            lines.append("%sk%d:" % (" " * (index * 2), index))
        lines.append("%sleaf: 1" % (" " * 24))
        self.assertEqual(self._code("\n".join(lines)), "structure_too_deep")

    def test_too_many_keys_rejected(self):
        lines = ["k%d: %d" % (index, index) for index in range(205)]
        self.assertEqual(self._code("\n".join(lines)), "structure_too_large")

    def test_scalar_too_long_rejected(self):
        self.assertEqual(self._code('value: "%s"' % ("x" * 4001)), "scalar_too_long")

    def test_missing_frontmatter_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            parse_frontmatter("# 没有 frontmatter\n\n正文\n")
        self.assertEqual(ctx.exception.code, "frontmatter_missing")

    def test_unclosed_frontmatter_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            parse_frontmatter('---\nkey: "value"\n\n正文\n')
        self.assertEqual(ctx.exception.code, "frontmatter_unclosed")


# ---------------------------------------------------------------------------
# 3. 规范化与只读预检：正例
# ---------------------------------------------------------------------------

class DocumentInspectPositiveTests(unittest.TestCase):
    def test_task_card_inspect_output(self):
        path = write_doc("ok-task-card.md", task_card_md())
        view = inspect(path, kind="task-card")
        self.assertEqual(view["documentKind"], "task-card")
        self.assertEqual(view["schemaVersion"], "task-card-board.v1")
        self.assertEqual(set(view["boardSummary"]), {"title", "stage", "problem", "nextStep"})
        self.assertEqual(view["sessionMode"], "new")
        self.assertEqual(view["identity"]["executor"]["agentId"], "dev-1")
        self.assertEqual(view["project"]["cardId"], "D4-NEXT-01B-TEST")
        self.assertTrue(view["validation"]["ok"])
        self.assertIs(view["wouldWrite"], False)
        self.assertIs(view["wouldDispatch"], False)
        self.assertTrue(all(not value for value in view["sideEffects"].values()))
        self.assertEqual(view["canonicalDigest"], canonical_digest(view["canonical"]))
        # 受控摘要：正文只留首个非空行，完整正文不进摘要
        self.assertLessEqual(len(view["sourceFile"]["bodyHeadline"]), 120)
        self.assertNotIn("正文：技术细节", view["boardSummary"]["title"])

    def test_report_inspect_output(self):
        path = write_doc("ok-report.md", report_md())
        view = inspect(path, kind="report")
        self.assertEqual(view["documentKind"], "report")
        self.assertEqual(set(view["boardSummary"]), {"stage", "result", "problem", "nextStep"})
        self.assertEqual(view["identity"]["executedBy"]["agentId"], "qa-1")
        self.assertTrue(view["validation"]["ok"])
        # 本卡不做报告入账：报告预检没有写入口
        self.assertEqual(view["writePreview"]["command"], "none")
        self.assertIs(view["wouldWrite"], False)

    def test_kind_auto_detected_from_schema_version(self):
        card = write_doc("auto-task-card.md", task_card_md())
        report = write_doc("auto-report.md", report_md())
        self.assertEqual(inspect(card)["documentKind"], "task-card")
        self.assertEqual(inspect(report)["documentKind"], "report")

    def test_repeated_inspect_is_byte_stable(self):
        path = write_doc("stable.md", task_card_md())
        first = inspect(path, kind="task-card")
        second = inspect(path, kind="task-card")
        self.assertEqual(first["canonicalDigest"], second["canonicalDigest"])
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )

    def test_same_content_different_name_same_digest(self):
        first = inspect(write_doc("same-a.md", task_card_md()), kind="task-card")
        second = inspect(write_doc("same-b.md", task_card_md()), kind="task-card")
        self.assertEqual(first["canonicalDigest"], second["canonicalDigest"])

    def test_write_preview_shows_what_would_be_written(self):
        path = write_doc("preview.md", task_card_md())
        preview = inspect(path, kind="task-card")["writePreview"]
        self.assertTrue(preview["ready"])
        self.assertEqual(preview["payload"]["agentId"], CLI_AGENT)
        self.assertEqual(preview["payload"]["sourceId"], CLI_SOURCE)
        self.assertFalse(preview["payload"]["permissions"]["allowNetwork"])


# ---------------------------------------------------------------------------
# 4. 规范化与只读预检：负例
# ---------------------------------------------------------------------------

class DocumentInspectNegativeTests(unittest.TestCase):
    def test_missing_kind_and_schema_rejected(self):
        path = write_doc("no-kind.md", "---\ncardId: \"X\"\n---\n\n正文\n")
        self.assertEqual(assert_fails(self, path).code, "kind_unknown")

    def test_schema_cross_use_rejected(self):
        # 任务卡声明放进 report 通道：交叉使用必须拒绝
        path = write_doc("cross.md", task_card_md())
        exc = assert_fails(self, path, kind="report")
        self.assertEqual(exc.details["errors"]["schemaVersion"]["code"], "schema_doc_mismatch")

    def test_schema_doc_mismatch_reported(self):
        path = write_doc("cross2.md", report_md())
        exc = assert_fails(self, path, kind="task-card")
        errors = exc.details["errors"]
        self.assertEqual(errors["schemaVersion"]["code"], "schema_doc_mismatch")

    def test_duplicate_key_in_card_rejected(self):
        text = "---\n" + _dump(_task_card_frontmatter()) + '\ncardId: "DUP"\n---\n\n正文\n'
        path = write_doc("dup-card.md", text)
        self.assertEqual(assert_fails(self, path, kind="task-card").code, "duplicate_key")

    def test_missing_identity_rejected(self):
        path = write_doc("no-issuer.md", task_card_md(issuer={"alias": "cox"}))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(exc.code, "document_invalid")
        self.assertIn("issuer", json.dumps(exc.details["errors"], ensure_ascii=False))

    def test_unknown_executor_rejected(self):
        unknown = _task_card_frontmatter()["executor"]
        unknown = dict(unknown, alias="dev-wkb1", agentId="dev-wkb1")
        path = write_doc("unknown-agent.md", task_card_md(executor=unknown))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(exc.details["errors"]["executor.agentId"]["code"], "identity_conflict")

    def test_alias_and_agent_id_conflict_rejected(self):
        conflict = dict(_task_card_frontmatter()["executor"], alias="dev-dsh-1", agentId="dev-1")
        path = write_doc("alias-conflict.md", task_card_md(executor=conflict))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(exc.details["errors"]["executor.alias"]["code"], "identity_conflict")

    def test_declared_source_id_mismatch_rejected(self):
        path = write_doc("source-mismatch.md", task_card_md(sourceId="qclaw-local"))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(exc.details["errors"]["sourceId"]["code"], "identity_conflict")

    def test_board_summary_tech_leak_rejected(self):
        summary = dict(_task_card_frontmatter()["boardSummary"],
                      nextStep="跑 d4ctl document inspect 看结果")
        path = write_doc("tech-leak.md", task_card_md(boardSummary=summary))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(
            exc.details["errors"]["boardSummary.nextStep"]["code"], "board_summary_tech_leak"
        )

    def test_board_summary_unknown_field_rejected(self):
        summary = dict(_task_card_frontmatter()["boardSummary"], owner="cox")
        path = write_doc("unknown-field.md", task_card_md(boardSummary=summary))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(
            exc.details["errors"]["boardSummary.unknown"]["code"], "board_summary_unknown_field"
        )

    def test_session_mode_resume_rejected(self):
        path = write_doc("resume.md", task_card_md(sessionMode="resume"))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(exc.details["errors"]["sessionMode"]["code"], "session_mode_unsupported")

    def test_workspace_escape_rejected(self):
        path = write_doc("escape.md", task_card_md(workspace="/etc"))
        exc = assert_fails(self, path, kind="task-card")
        self.assertEqual(exc.details["errors"]["workspace"]["code"], "path_escape")

    def test_write_scope_escape_rejected(self):
        permissions = {
            "writeScope": ["../../outside"],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        }
        path = write_doc("scope-escape.md", task_card_md(permissions=permissions))
        exc = assert_fails(self, path, kind="task-card")
        codes = json.dumps(exc.details["errors"], ensure_ascii=False)
        self.assertIn("path_escape", codes)

    def test_relative_source_card_path_rejected(self):
        source_card = dict(_report_frontmatter()["sourceCard"], path="docs/task-card.md")
        path = write_doc("relative-path.md", report_md(sourceCard=source_card))
        exc = assert_fails(self, path, kind="report")
        self.assertEqual(exc.details["errors"]["sourceCard.path"]["code"], "path_not_absolute")

    def test_report_body_claiming_passed_does_not_change_status(self):
        body = "已通过 cox review，全部交付完成，可以发布。"
        path = write_doc("fake-passed.md", report_md(body=body, evidence=None))
        exc = assert_fails(self, path, kind="report")
        rendered = json.dumps(exc.to_dict(), ensure_ascii=False)
        self.assertIn("evidence_invalid", rendered)
        self.assertNotIn("已交付", rendered)

    def test_relative_input_path_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            inspect("relative/path.md")
        self.assertEqual(ctx.exception.code, "path_not_absolute")

    def test_dotdot_input_path_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            inspect(os.path.join(str(runtime_root()), "..", "outside.md"))
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_symlink_input_rejected(self):
        target = write_doc("symlink-target.md", task_card_md())
        link = os.path.join(str(runtime_root()), "symlink-card.md")
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        os.symlink(target, link)
        try:
            with self.assertRaises(DocumentContractError) as ctx:
                inspect(link, kind="task-card")
            self.assertEqual(ctx.exception.code, "path_symlink_rejected")
        finally:
            os.remove(link)

    def test_oversized_input_rejected(self):
        path = write_doc("oversize.md", task_card_md())
        with self.assertRaises(DocumentContractError) as ctx:
            inspect(path, kind="task-card", max_bytes=64)
        self.assertEqual(ctx.exception.code, "document_too_large")

    def test_missing_input_file_rejected(self):
        with self.assertRaises(DocumentContractError) as ctx:
            inspect(os.path.join(str(runtime_root()), "does-not-exist.md"))
        self.assertEqual(ctx.exception.code, "document_not_file")

    def test_parser_rejects_yaml_tag_object(self):
        text = "---\n" + _dump(_task_card_frontmatter()) + '\nextra: !!python/object:x {}\n---\n\n正文\n'
        path = write_doc("tag-object.md", text)
        self.assertEqual(assert_fails(self, path, kind="task-card").code, "yaml_tag_rejected")


# ---------------------------------------------------------------------------
# 5. CLI：document inspect 只读无副作用 + task create Markdown / JSON
# ---------------------------------------------------------------------------

class DocumentCliTests(unittest.TestCase):
    def cli(self, argv, db=None, transport=None, synthetic=False):
        root = str(runtime_root())
        db_path = db or os.path.join(root, "d4-document-tests.sqlite")
        full = ["--db", db_path, "--allowed-root", root] + list(argv)
        return d4ctl.run(full, transport=transport, synthetic=synthetic)

    def test_inspect_ok_exit_zero(self):
        path = write_doc("cli-ok.md", task_card_md())
        code, result = self.cli(["document", "inspect", "--file", path, "--kind", "task-card"])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["documentKind"], "task-card")

    def test_inspect_invalid_exit_nonzero(self):
        path = write_doc("cli-bad.md", task_card_md(sessionMode="resume"))
        code, result = self.cli(["document", "inspect", "--file", path, "--kind", "task-card"])
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "document_invalid")
        self.assertIn("sessionMode", json.dumps(result["error"]["details"], ensure_ascii=False))

    def test_inspect_does_not_create_database(self):
        path = write_doc("cli-nodb.md", report_md())
        db = os.path.join(str(runtime_root()), "never-created.sqlite")
        if os.path.exists(db):
            os.remove(db)
        code, result = self.cli(["document", "inspect", "--file", path], db=db)
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertFalse(os.path.exists(db), "只读预检不应创建数据库文件")

    def test_inspect_kind_is_not_guessed(self):
        path = write_doc("cli-no-kind.md", "---\ncardId: \"X\"\n---\n\n正文\n")
        code, result = self.cli(["document", "inspect", "--file", path])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "kind_unknown")

    def test_markdown_task_create_ok(self):
        path = write_doc("cli-create.md", task_card_md())
        code, result = self.cli(["task", "create", "--file", path])
        self.assertEqual(code, 0, result)
        self.assertIn("taskId", result["data"])
        self.assertEqual(result["data"]["agentId"], CLI_AGENT)
        self.assertEqual(result["data"]["document"]["documentKind"], "task-card")

    def test_markdown_task_create_missing_fields_rejected(self):
        card = _task_card_frontmatter()
        card.pop("workspace")
        card.pop("permissions")
        path = write_doc("cli-create-missing.md", "---\n%s\n---\n\n正文\n" % _dump(card))
        db = os.path.join(str(runtime_root()), "missing-fields.sqlite")
        if os.path.exists(db):
            os.remove(db)
        code, result = self.cli(["task", "create", "--file", path], db=db)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "invalid_task")
        missing = json.dumps(result["error"]["details"], ensure_ascii=False)
        self.assertIn("workspace", missing)
        self.assertIn("permissions", missing)
        self.assertEqual(self._task_count(db), 0, "缺字段时不应创建任务")

    def test_markdown_task_create_unknown_agent_rejected(self):
        executor = dict(_task_card_frontmatter()["executor"], alias="dev-wkb1", agentId="dev-wkb1")
        path = write_doc("cli-create-unknown.md", task_card_md(executor=executor))
        db = os.path.join(str(runtime_root()), "unknown-agent.sqlite")
        if os.path.exists(db):
            os.remove(db)
        code, result = self.cli(["task", "create", "--file", path], db=db)
        self.assertEqual(code, 1)
        # 预检阶段即 fail closed：错误码为 document_invalid，详情里给出身份冲突
        self.assertEqual(result["error"]["code"], "document_invalid")
        self.assertIn(
            "identity_conflict", json.dumps(result["error"]["details"], ensure_ascii=False)
        )
        self.assertEqual(self._task_count(db), 0)

    def test_json_task_create_still_works(self):
        payload = {
            "projectId": "conductor-d4",
            "title": "JSON 兼容回归任务",
            "agentId": CLI_AGENT,
            "sourceId": CLI_SOURCE,
            "workspace": _workspace(),
            "goal": "证明旧 JSON 入口行为不变",
            "acceptance": ["创建成功"],
            "permissions": {
                "writeScope": ["receipts/"],
                "allowNetwork": False,
                "allowBusinessWrite": False,
                "allowGit": False,
                "allowPublish": False,
            },
            "limits": {"maxAttempts": 2, "maxReviewRounds": 2, "timeoutMinutes": 60},
        }
        path = write_doc("cli-json-task.json", json.dumps(payload, ensure_ascii=False))
        code, result = self.cli(["task", "create", "--file", path])
        self.assertEqual(code, 0, result)
        self.assertIn("taskId", result["data"])
        self.assertNotIn("document", result["data"], "JSON 入口不应带 Markdown 解析信息")

    def test_json_task_create_missing_fields_semantics_unchanged(self):
        payload = {"projectId": "conductor-d4", "title": "缺字段的 JSON"}
        path = write_doc("cli-json-missing.json", json.dumps(payload, ensure_ascii=False))
        code, result = self.cli(["task", "create", "--file", path])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "invalid_task")

    def test_inspect_accepts_but_ignores_db_argument(self):
        path = write_doc("cli-ignore-db.md", task_card_md())
        code, result = self.cli(["document", "inspect", "--file", path, "--kind", "task-card"])
        self.assertEqual(code, 0)
        data = result["data"]
        self.assertIs(data["sideEffects"]["databaseOpened"], False)
        self.assertIs(data["sideEffects"]["databaseCreated"], False)

    @staticmethod
    def _task_count(db_path):
        if not os.path.exists(db_path):
            return 0
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        except sqlite3.DatabaseError:
            return 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# 6. 建卡映射矩阵（不写库，纯映射）
# ---------------------------------------------------------------------------

class TaskCreateMappingTests(unittest.TestCase):
    def mapping(self, card):
        return task_card_to_create_payload(
            card, cli_agent=CLI_AGENT, cli_source=CLI_SOURCE,
            allowed_roots=[str(runtime_root())],
        )

    def test_full_mapping(self):
        payload, missing, errors = self.mapping(_task_card_frontmatter())
        self.assertEqual(missing, [])
        self.assertEqual(errors, {})
        self.assertEqual(payload["projectId"], "conductor-d4")
        self.assertEqual(payload["agentId"], CLI_AGENT)
        self.assertEqual(payload["sourceId"], CLI_SOURCE)

    def test_missing_required_fields_reported_not_defaulted(self):
        card = _task_card_frontmatter()
        card.pop("workspace")
        card.pop("permissions")
        card.pop("goal")
        payload, missing, errors = self.mapping(card)
        self.assertIn("workspace", missing)
        self.assertIn("permissions", missing)
        self.assertIn("goal", missing)
        self.assertIsNone(payload["permissions"] or None)

    def test_permission_type_error_reported(self):
        permissions = dict(_task_card_frontmatter()["permissions"], allowNetwork="false")
        payload, missing, errors = self.mapping(_task_card_frontmatter(permissions=permissions))
        self.assertTrue(any("permissions" in key for key in errors))

    def test_limits_out_of_range_reported(self):
        payload, missing, errors = self.mapping(_task_card_frontmatter(limits={"maxAttempts": 9}))
        self.assertIn("limits.maxAttempts", errors)

    def test_workspace_escape_reported(self):
        payload, missing, errors = self.mapping(_task_card_frontmatter(workspace="/etc"))
        self.assertEqual(errors["workspace"]["code"], "path_escape")


if __name__ == "__main__":
    unittest.main()
