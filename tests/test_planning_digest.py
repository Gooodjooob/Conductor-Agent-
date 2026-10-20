"""planning-snapshot.v1 确定性摘要定向测试（第 009 轮返工候选，基于第 008 轮）。

只使用标准库 unittest，不读取真实文件、不访问网络/数据库。
本文件被 cox 独立离线执行；若未执行，回执必须标记 NOT_RUN。

golden 说明：
- ``GOLDEN_CANONICAL_JSON`` 是**预先写死**的规范 JSON 字面量（对象键字典序、
  ``separators=(',',':')``、UTF-8、非 ASCII 不转义）。
- ``GOLDEN_EXPECTED_DIGEST`` 是 cox 独立编码 / 字面量算出的固定值，本文件写死，
  必须真实 assert 精确输出；不使用被测函数生成期望，也不再保留 skip 路径。

009 返工覆盖：``sourceKind`` 非字符串类型拒绝、字符串值与对象键的 UTF-8 可编码性、
「输入未修改」断言改用不依赖 ``==`` 的结构指纹（保留 sentinel 身份检查）、
固定 golden 精确断言、循环引用拒绝。
"""

import copy
import hashlib
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CANDIDATE_ROOT = os.path.dirname(_HERE)
if _CANDIDATE_ROOT not in sys.path:
    sys.path.insert(0, _CANDIDATE_ROOT)

from contracts.planning_digest import (  # noqa: E402  (测试内引导 sys.path 后再导入)
    PlanningDigestError,
    compute_canonical_digest,
    digest_payload,
)

_GOLDEN_FILE_SHA256 = "sha256:" + "a" * 64

# 预先写死的规范 JSON 字面量（对应 _golden_payload()）。
GOLDEN_CANONICAL_JSON = (
    '{"businessLines":[],"milestones":[],"planStatus":"planning",'
    '"planningSourceId":"src-001","projectId":"proj-alpha",'
    '"schemaVersion":"planning-snapshot.v1",'
    '"sections":[{"content":"初版计划","sectionId":"s1","title":"背景"}],'
    '"sourceFile":{"bytes":1234,"documentVersion":"v1.0","name":"plan.md",'
    '"path":"/data/planning/plan.md","sha256":"' + _GOLDEN_FILE_SHA256 + '"},'
    '"sourceKind":"product","stages":["draft"],"unresolved":[]}'
)

# cox 独立编码 / 字面量算出的固定值（不由被测函数生成）。
GOLDEN_EXPECTED_DIGEST = (
    "sha256:a7668f3102ddb7ab9e515c5815e63ab16d20d2a31ae75aa9df38a1aa53f650ae"
)


def _golden_payload():
    return {
        "schemaVersion": "planning-snapshot.v1",
        "projectId": "proj-alpha",
        "planningSourceId": "src-001",
        "sourceKind": "product",
        "sourceFile": {
            "name": "plan.md",
            "path": "/data/planning/plan.md",
            "documentVersion": "v1.0",
            "sha256": _GOLDEN_FILE_SHA256,
            "bytes": 1234,
        },
        "sections": [
            {"sectionId": "s1", "title": "背景", "content": "初版计划"},
        ],
        "stages": ["draft"],
        "businessLines": [],
        "milestones": [],
        "planStatus": "planning",
        "unresolved": [],
    }


def _fingerprint(value):
    """不依赖 ``==`` 的结构指纹，供「输入未修改」断言使用。

    非 JSON 对象（例如 ``object()``）不支持值相等，``copy.deepcopy`` 又会改变
    ``id``；这里对这类对象只记录 ``id``，因此既保留了 sentinel 身份不变检查，
    又不会把夹具自身的比较方式当成被测实现的失败。
    """
    if isinstance(value, dict):
        return ("dict", tuple((key, _fingerprint(item)) for key, item in value.items()))
    if isinstance(value, list):
        return ("list", tuple(_fingerprint(item) for item in value))
    if isinstance(value, tuple):
        return ("tuple", tuple(_fingerprint(item) for item in value))
    if value is None or isinstance(value, (str, bytes, bool, int, float)):
        return ("scalar", type(value).__name__, repr(value))
    return ("object", id(value))


class GoldenDigestTests(unittest.TestCase):
    def test_frozen_canonical_json_matches_golden_payload(self):
        # 先保证预先写死的字面量与 golden 载荷一致，避免字面量自身笔误。
        self.assertEqual(json.loads(GOLDEN_CANONICAL_JSON), _golden_payload())

    def test_golden_digest_matches_frozen_canonical_json(self):
        expected = "sha256:" + hashlib.sha256(
            GOLDEN_CANONICAL_JSON.encode("utf-8")
        ).hexdigest()
        self.assertEqual(compute_canonical_digest(_golden_payload()), expected)

    def test_golden_digest_matches_independent_stdlib_encoding(self):
        payload = digest_payload(_golden_payload())
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        self.assertEqual(compute_canonical_digest(_golden_payload()), expected)

    def test_golden_digest_matches_precomputed_constant(self):
        # 固定值由 cox 独立给定；这里必须真实断言精确输出，不得 skip。
        self.assertEqual(
            compute_canonical_digest(_golden_payload()),
            GOLDEN_EXPECTED_DIGEST,
            "compute_canonical_digest 必须精确等于 cox 独立给定的固定摘要",
        )

    def test_precomputed_constant_matches_frozen_canonical_json_bytes(self):
        # 独立复算：固定值必须能由预先写死的规范 JSON 字面量算出。
        recomputed = "sha256:" + hashlib.sha256(
            GOLDEN_CANONICAL_JSON.encode("utf-8")
        ).hexdigest()
        self.assertEqual(
            recomputed,
            GOLDEN_EXPECTED_DIGEST,
            "固定摘要必须与冻结的规范 JSON 字面量字节一致",
        )

    def test_digest_format_is_sha256_lowercase_hex(self):
        digest = compute_canonical_digest(_golden_payload())
        self.assertTrue(digest.startswith("sha256:"))
        hex_part = digest[len("sha256:"):]
        self.assertEqual(len(hex_part), 64)
        self.assertEqual(hex_part, hex_part.lower())
        self.assertTrue(all(char in "0123456789abcdef" for char in hex_part))


class DeterminismTests(unittest.TestCase):
    def test_top_level_and_nested_key_order_does_not_change_digest(self):
        base = _golden_payload()
        reordered = {
            "unresolved": [],
            "planStatus": "planning",
            "milestones": [],
            "businessLines": [],
            "stages": ["draft"],
            "sections": [
                {"title": "背景", "content": "初版计划", "sectionId": "s1"},
            ],
            "sourceFile": {
                "bytes": 1234,
                "sha256": _GOLDEN_FILE_SHA256,
                "documentVersion": "v1.0",
                "path": "/data/planning/plan.md",
                "name": "plan.md",
            },
            "sourceKind": "product",
            "planningSourceId": "src-001",
            "projectId": "proj-alpha",
            "schemaVersion": "planning-snapshot.v1",
        }
        self.assertEqual(
            compute_canonical_digest(base), compute_canonical_digest(reordered)
        )

    def test_array_order_changes_digest(self):
        first = _golden_payload()
        first["stages"] = ["draft", "review"]
        second = _golden_payload()
        second["stages"] = ["review", "draft"]
        self.assertNotEqual(
            compute_canonical_digest(first), compute_canonical_digest(second)
        )

    def test_metadata_changes_do_not_change_digest(self):
        base = _golden_payload()
        with_metadata = _golden_payload()
        with_metadata.update(
            {
                "canonicalDigest": "sha256:" + "f" * 64,
                "snapshotId": "snap-1",
                "snapshotRevision": 3,
                "importedAt": "2026-10-10T00:00:00+08:00",
                "importedBy": "cox",
            }
        )
        self.assertEqual(
            compute_canonical_digest(base), compute_canonical_digest(with_metadata)
        )
        self.assertEqual(digest_payload(base), digest_payload(with_metadata))
        for field in (
            "canonicalDigest",
            "snapshotId",
            "snapshotRevision",
            "importedAt",
            "importedBy",
        ):
            self.assertNotIn(field, digest_payload(with_metadata))

    def test_participating_content_change_changes_digest(self):
        base = _golden_payload()
        changed = _golden_payload()
        changed["planStatus"] = "planned"
        self.assertNotEqual(
            compute_canonical_digest(base), compute_canonical_digest(changed)
        )

    def test_missing_unresolved_equals_empty_list(self):
        without = _golden_payload()
        del without["unresolved"]
        self.assertEqual(digest_payload(without)["unresolved"], [])
        self.assertEqual(
            compute_canonical_digest(_golden_payload()),
            compute_canonical_digest(without),
        )


class BytesShaAndEncodingTests(unittest.TestCase):
    def test_content_digest_is_not_source_file_bytes_sha(self):
        payload = _golden_payload()
        digest = compute_canonical_digest(payload)
        self.assertNotEqual(digest, payload["sourceFile"]["sha256"])
        # sourceFile.sha256 是参与字段，改动它必须改变内容摘要（但不是搬运该值）。
        other = _golden_payload()
        other["sourceFile"]["sha256"] = "sha256:" + "c" * 64
        self.assertNotEqual(digest, compute_canonical_digest(other))

    def test_line_feed_and_whitespace_are_not_normalized(self):
        base = _golden_payload()
        base["sections"] = [{"content": "a\nb"}]
        with_space = _golden_payload()
        with_space["sections"] = [{"content": "a b"}]
        trailing_lf = _golden_payload()
        trailing_lf["sections"] = [{"content": "a\nb\n"}]
        self.assertNotEqual(
            compute_canonical_digest(base), compute_canonical_digest(with_space)
        )
        self.assertNotEqual(
            compute_canonical_digest(base), compute_canonical_digest(trailing_lf)
        )

    def test_unicode_sequences_are_not_normalized(self):
        nfc = _golden_payload()
        nfc["sections"] = [{"content": "caf\u00e9"}]
        nfd = _golden_payload()
        nfd["sections"] = [{"content": "cafe\u0301"}]
        self.assertNotEqual(
            compute_canonical_digest(nfc), compute_canonical_digest(nfd)
        )

    def test_input_is_not_mutated_and_payload_is_independent(self):
        snapshot = _golden_payload()
        snapshot["unresolved"] = ["待确认"]
        before = copy.deepcopy(snapshot)

        payload = digest_payload(snapshot)
        compute_canonical_digest(snapshot)

        self.assertEqual(snapshot, before)
        self.assertIsNot(payload["sections"], snapshot["sections"])
        self.assertIsNot(payload["sections"][0], snapshot["sections"][0])

        payload["sections"][0]["title"] = "被改动的标题"
        self.assertEqual(snapshot, before)


class RejectionTests(unittest.TestCase):
    def _assert_rejected(self, snapshot, code=None):
        # 用结构指纹比较，避免 object()/deepcopy 造成的误报；断言本身不删除。
        before = _fingerprint(snapshot)
        with self.assertRaises(PlanningDigestError) as ctx:
            digest_payload(snapshot)
        self.assertEqual(_fingerprint(snapshot), before)
        if code is not None:
            self.assertEqual(ctx.exception.code, code)
        self.assertTrue(ctx.exception.details.get("path"))
        # compute_canonical_digest 必须同样拒绝。
        with self.assertRaises(PlanningDigestError):
            compute_canonical_digest(snapshot)
        return ctx.exception

    def test_error_is_value_error_subclass(self):
        self.assertTrue(issubclass(PlanningDigestError, ValueError))

    def test_missing_required_field_rejected(self):
        snapshot = _golden_payload()
        del snapshot["projectId"]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_unknown_top_level_field_rejected(self):
        snapshot = _golden_payload()
        snapshot["activation"] = {"state": "active"}
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_unknown_top_level_field_not_silently_dropped(self):
        snapshot = _golden_payload()
        snapshot["manualAdjustment"] = {"order": 1}
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_wrong_schema_version_rejected(self):
        snapshot = _golden_payload()
        snapshot["schemaVersion"] = "planning-snapshot.v2"
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_invalid_source_kind_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceKind"] = "unregistered"
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_empty_project_id_rejected(self):
        snapshot = _golden_payload()
        snapshot["projectId"] = ""
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_empty_plan_status_rejected(self):
        snapshot = _golden_payload()
        snapshot["planStatus"] = ""
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_bool_bytes_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["bytes"] = True
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_negative_bytes_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["bytes"] = -1
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_float_bytes_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["bytes"] = 1234.0
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_uppercase_sha256_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["sha256"] = "sha256:" + "A" * 64
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_short_sha256_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["sha256"] = "sha256:abc"
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_parent_traversal_path_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["path"] = "/data/../etc/passwd"
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_relative_path_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["path"] = "data/plan.md"
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_source_file_extra_field_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceFile"]["mtime"] = 123
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_source_file_missing_field_rejected(self):
        snapshot = _golden_payload()
        del snapshot["sourceFile"]["sha256"]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_unresolved_non_list_rejected(self):
        snapshot = _golden_payload()
        snapshot["unresolved"] = {"a": 1}
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_nan_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{"content": float("nan")}]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_infinity_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{"content": float("inf")}]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_non_string_key_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{1: "x"}]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_top_level_non_string_key_rejected(self):
        snapshot = _golden_payload()
        snapshot[123] = "x"
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_non_json_value_rejected(self):
        sentinel = object()
        snapshot = _golden_payload()
        snapshot["sections"] = [{"content": sentinel}]
        before = _fingerprint(snapshot)
        self._assert_rejected(snapshot, "planning_digest_invalid")
        # 保留 object 身份不变检查：拒绝路径不得替换、重建或修改输入对象。
        self.assertIs(snapshot["sections"][0]["content"], sentinel)
        self.assertEqual(_fingerprint(snapshot), before)

    def test_tuple_value_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{"content": ("a", "b")}]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_nested_self_reference_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{"canonicalDigest": "sha256:" + "d" * 64}]
        error = self._assert_rejected(snapshot, "planning_digest_self_reference")
        self.assertIn("canonicalDigest", error.details["path"])

    def test_top_level_metadata_canonical_digest_allowed(self):
        snapshot = _golden_payload()
        snapshot["canonicalDigest"] = "sha256:" + "d" * 64
        self.assertEqual(
            compute_canonical_digest(snapshot),
            compute_canonical_digest(_golden_payload()),
        )

    def test_nested_execution_fact_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{"taskId": "task-1"}]
        self._assert_rejected(snapshot, "planning_execution_fact_forbidden")

    def test_nested_execution_fact_workspace_rejected(self):
        snapshot = _golden_payload()
        snapshot["milestones"] = [{"workspace": "/tmp/x"}]
        self._assert_rejected(snapshot, "planning_execution_fact_forbidden")

    def test_nested_execution_fact_approved_rejected(self):
        snapshot = _golden_payload()
        snapshot["stages"] = [{"approved": True}]
        self._assert_rejected(snapshot, "planning_execution_fact_forbidden")

    def test_missing_source_file_rejected(self):
        snapshot = _golden_payload()
        del snapshot["sourceFile"]
        self._assert_rejected(snapshot, "planning_digest_invalid")

    def test_non_object_snapshot_rejected(self):
        self._assert_rejected([], "planning_digest_invalid")


class SourceKindTypeRegressionTests(unittest.TestCase):
    """009 回归：sourceKind 非字符串必须是 planning_digest_invalid，而不是 TypeError。"""

    def _assert_both_functions_reject(self, snapshot):
        for func in (digest_payload, compute_canonical_digest):
            with self.assertRaises(PlanningDigestError) as ctx:
                func(snapshot)
            error = ctx.exception
            self.assertEqual(error.code, "planning_digest_invalid")
            self.assertEqual(error.details.get("path"), "$.sourceKind")
            self.assertNotIsInstance(error, TypeError)

    def test_source_kind_list_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceKind"] = ["product"]
        self._assert_both_functions_reject(snapshot)

    def test_source_kind_dict_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceKind"] = {"kind": "product"}
        self._assert_both_functions_reject(snapshot)

    def test_source_kind_none_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceKind"] = None
        self._assert_both_functions_reject(snapshot)

    def test_source_kind_integer_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceKind"] = 3
        self._assert_both_functions_reject(snapshot)

    def test_source_kind_bool_rejected(self):
        snapshot = _golden_payload()
        snapshot["sourceKind"] = True
        self._assert_both_functions_reject(snapshot)


class Utf8EncodabilityRegressionTests(unittest.TestCase):
    """009 回归：孤立 surrogate 拒绝且不回显输入；合法 Unicode 原样保留。"""

    _SURROGATE = "\ud800"

    def _assert_both_functions_reject(self, snapshot, expected_path, forbidden_text):
        for func in (digest_payload, compute_canonical_digest):
            with self.assertRaises(PlanningDigestError) as ctx:
                func(snapshot)
            error = ctx.exception
            self.assertEqual(error.code, "planning_digest_invalid")
            self.assertEqual(error.details.get("path"), expected_path)
            self.assertNotIsInstance(error, TypeError)
            # 不回显输入内容：message 与 details 都不得包含被拒文本片段。
            self.assertNotIn(forbidden_text, error.message)
            self.assertNotIn(forbidden_text, repr(error.details))

    def test_lone_surrogate_in_participating_string_rejected(self):
        snapshot = _golden_payload()
        snapshot["planStatus"] = "STATUS-" + self._SURROGATE
        self._assert_both_functions_reject(snapshot, "$.planStatus", "STATUS-")

    def test_lone_surrogate_in_nested_string_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [
            {"title": "背景", "content": "NOTE-" + self._SURROGATE},
        ]
        self._assert_both_functions_reject(snapshot, "$.sections[0].content", "NOTE-")

    def test_lone_surrogate_in_nested_key_rejected(self):
        snapshot = _golden_payload()
        snapshot["sections"] = [{"KEY-" + self._SURROGATE: "值"}]
        self._assert_both_functions_reject(snapshot, "$.sections[0]", "KEY-")

    def test_lone_surrogate_in_top_level_key_rejected(self):
        snapshot = _golden_payload()
        snapshot["TOP-" + self._SURROGATE] = "值"
        self._assert_both_functions_reject(snapshot, "$", "TOP-")

    def test_valid_non_ascii_unicode_still_encodable_and_preserved(self):
        text = "caf\u00e9 \u4e2d\u6587 \u00c5"
        snapshot = _golden_payload()
        snapshot["sections"] = [{"content": text}]
        payload = digest_payload(snapshot)
        self.assertEqual(payload["sections"][0]["content"], text)
        digest = compute_canonical_digest(snapshot)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertEqual(len(digest), len("sha256:") + 64)

    def test_nfc_and_nfd_stay_different_after_utf8_check(self):
        nfc = _golden_payload()
        nfc["sections"] = [{"content": "caf\u00e9"}]
        nfd = _golden_payload()
        nfd["sections"] = [{"content": "cafe\u0301"}]
        self.assertNotEqual(compute_canonical_digest(nfc), compute_canonical_digest(nfd))


class CyclicStructureTests(unittest.TestCase):
    """009 回归：循环引用不属于 JSON 树，必须拒绝且不得 RecursionError。"""

    def test_self_referencing_list_rejected(self):
        snapshot = _golden_payload()
        cyclic = []
        cyclic.append(cyclic)
        snapshot["sections"] = cyclic
        for func in (digest_payload, compute_canonical_digest):
            with self.assertRaises(PlanningDigestError) as ctx:
                func(snapshot)
            self.assertEqual(ctx.exception.code, "planning_digest_invalid")
            self.assertTrue(ctx.exception.details.get("path"))
        self.assertIs(snapshot["sections"][0], snapshot["sections"])

    def test_shared_sibling_object_is_not_treated_as_cycle(self):
        snapshot = _golden_payload()
        shared = {"title": "共享对象"}
        snapshot["sections"] = [shared, shared]
        digest = compute_canonical_digest(snapshot)
        self.assertTrue(digest.startswith("sha256:"))
        self.assertIs(snapshot["sections"][0], snapshot["sections"][1])


if __name__ == "__main__":
    unittest.main()
