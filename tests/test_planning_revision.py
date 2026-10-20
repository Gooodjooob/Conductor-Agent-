# -*- coding: utf-8 -*-
"""规划 revision 追加判定纯函数的隔离单元测试（第 027c 轮候选）。

状态：**NOT_RUN**。Worker 无 bash 权限（``bashCommands`` 为空），本轮未运行任何命令；
全部结果由 cox 独立执行以下入口后判定：

    /usr/bin/python3 -m unittest discover -s tests -p test_planning_revision.py -v

约定（与 ``inputs/cox-revision-decision.md`` 一致）：

* 只用标准库 ``unittest``（含 ``subTest``）/ ``ast`` / ``copy`` / ``importlib`` /
  ``inspect`` / ``json`` / ``os`` / ``sys``；不引入 pytest、不 ``skip``、不安装依赖。
* 候选实现按**本 workspace 相对路径**加载（``spec_from_file_location``），不猜父目录、
  不读环境变量；本卡候选没有 formal 依赖，因此不依赖 formal 是否存在同名模块。
* 本卡产品零文件 / 零数据库访问：本文件不建库、不连库、不写测试数据，也不使用
  ``sqlite3`` / ``time`` / ``uuid``；唯一文件读取是为了对候选源码做「无导入 / 无 I/O /
  无时钟 / 无编号来源」的静态核验。
* 每个用例只断言纯函数返回值 / 异常与「输入未被修改」，不产生任何落盘副作用。

本轮（027c）相对 026 的改动：新增
``ParameterAndShapeRejectionCase.test_duplicate_history_digest_precedes_drift``，用于回归
「任意两个历史行 canonicalDigest 重复（本次 incoming 为第三个 digest）即使叠加
``batch_drift=True`` 也必须先报 ``planning_revision_invalid``」。其余用例沿用 026。
"""

import ast
import copy
import importlib.util
import inspect
import json
import os
import sys
import unittest

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATE_PATH = os.path.join(WORKSPACE_ROOT, "contracts", "planning_revision.py")

_CANDIDATE_NAME = "planning_revision_candidate"
_SPEC = importlib.util.spec_from_file_location(_CANDIDATE_NAME, CANDIDATE_PATH)
CANDIDATE = importlib.util.module_from_spec(_SPEC)
sys.modules[_CANDIDATE_NAME] = CANDIDATE
_SPEC.loader.exec_module(CANDIDATE)

decide_planning_revision = CANDIDATE.decide_planning_revision
PlanningRevisionError = CANDIDATE.PlanningRevisionError

SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
SHA_D = "sha256:" + "d" * 64
DIGEST_1 = "sha256:" + "1" * 64
DIGEST_2 = "sha256:" + "2" * 64
DIGEST_3 = "sha256:" + "3" * 64
DIGEST_4 = "sha256:" + "4" * 64


def _row(snapshot_id="snap-1", revision=1, digest=DIGEST_1, version="v1",
         sha=SHA_A, size=10):
    return {
        "snapshotId": snapshot_id,
        "snapshotRevision": revision,
        "canonicalDigest": digest,
        "documentVersion": version,
        "sourceSha256": sha,
        "sourceBytes": size,
    }


def _incoming(digest=DIGEST_2, version="v2", sha=SHA_B, size=20):
    return {
        "canonicalDigest": digest,
        "documentVersion": version,
        "sourceSha256": sha,
        "sourceBytes": size,
    }


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RevisionDecisionCase(unittest.TestCase):
    def _expect_error(self, code, existing, incoming, **kwargs):
        with self.assertRaises(PlanningRevisionError) as caught:
            decide_planning_revision(existing, incoming, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertIn("reason", caught.exception.details)
        return caught.exception


class FirstRevisionCase(RevisionDecisionCase):
    """首次建议：空既有 -> revision 1，未知 / unverified 版本也可。"""

    def test_empty_existing_creates_first_revision_one(self):
        for version in ("v1", "unverified", "UNVERIFIED", "2026-10-10-draft", "未知版本"):
            with self.subTest(documentVersion=version):
                incoming = _incoming(version=version)
                incoming_before = copy.deepcopy(incoming)
                result = decide_planning_revision([], incoming)
                self.assertEqual(result["decision"], CANDIDATE.DECISION_CREATE_FIRST)
                self.assertEqual(result["snapshotRevision"], 1)
                self.assertIs(result["replayed"], False)
                self.assertIs(result["wouldActivate"], False)
                self.assertNotIsInstance(result["snapshotRevision"], bool)
                self.assertEqual(sorted(result), sorted(CANDIDATE.RESULT_KEYS))
                # 首次建议不产出编号，也不声称已保存 / 已激活。
                self.assertNotIn("snapshotId", result)
                self.assertEqual(incoming, incoming_before)

    def test_empty_existing_ignores_declaration_and_keeps_revision_one(self):
        result = decide_planning_revision(
            [], _incoming(), new_content_reason="受控修订声明"
        )
        self.assertEqual(result["decision"], CANDIDATE.DECISION_CREATE_FIRST)
        self.assertEqual(result["snapshotRevision"], 1)


class AppendRevisionCase(RevisionDecisionCase):
    """追加建议：revision = 既有最大值 + 1（不是 len + 1）。"""

    def test_append_uses_max_revision_plus_one_for_non_contiguous_revisions(self):
        variants = (
            ([_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
              _row("snap-b", 4, DIGEST_2, "v2", SHA_B, 20)], 5),
            ([_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
              _row("snap-b", 4, DIGEST_2, "v2", SHA_B, 20),
              _row("snap-c", 7, DIGEST_3, "v3", SHA_C, 30)], 8),
            ([_row("snap-a", 6, DIGEST_1, "v1", SHA_A, 10)], 7),
        )
        for existing, expected in variants:
            with self.subTest(revisions=[row["snapshotRevision"] for row in existing]):
                incoming = _incoming(digest=DIGEST_4, version="v9", sha=SHA_D, size=40)
                existing_before = copy.deepcopy(existing)
                result = decide_planning_revision(existing, incoming)
                self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)
                self.assertEqual(result["snapshotRevision"], expected)
                self.assertNotEqual(result["snapshotRevision"], len(existing) + 1)
                self.assertIs(result["replayed"], False)
                self.assertIs(result["wouldActivate"], False)
                self.assertNotIsInstance(result["snapshotRevision"], bool)
                self.assertEqual(sorted(result), sorted(CANDIDATE.RESULT_KEYS))
                # 追加建议同样不生成编号。
                self.assertNotIn("snapshotId", result)
                self.assertEqual(existing, existing_before)

    def test_explicit_new_document_version_appends_candidate(self):
        existing = [
            _row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
            _row("snap-b", 3, DIGEST_2, "v2", SHA_B, 20),
        ]
        incoming = _incoming(digest=DIGEST_3, version="v3", sha=SHA_C, size=30)
        result = decide_planning_revision(existing, incoming)
        self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)
        self.assertEqual(result["snapshotRevision"], 4)
        self.assertEqual(sorted(result), sorted(CANDIDATE.RESULT_KEYS))


class ReplayCase(RevisionDecisionCase):
    """重放建议：回显既有行原身份与原 revision，不新建编号。"""

    def test_replay_returns_original_identity_and_revision(self):
        existing = [
            _row("snap-existing", 4, DIGEST_1, "v1", SHA_A, 10),
            _row("snap-other", 1, DIGEST_2, "v0", SHA_B, 20),
        ]
        incoming = _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=10)
        existing_before = copy.deepcopy(existing)
        result = decide_planning_revision(existing, incoming)
        self.assertEqual(result["decision"], CANDIDATE.DECISION_REPLAYED)
        self.assertIs(result["replayed"], True)
        self.assertEqual(result["snapshotId"], "snap-existing")
        self.assertEqual(result["snapshotRevision"], 4)
        self.assertIs(result["wouldActivate"], False)
        self.assertEqual(sorted(result), sorted(CANDIDATE.RESULT_REPLAYED_KEYS))
        # 重放不新建 revision（既有最大值 + 1 = 5）。
        self.assertNotEqual(result["snapshotRevision"], 5)
        self.assertEqual(existing, existing_before)


class DriftPriorityCase(RevisionDecisionCase):
    """批内漂移优先于重放与同版本冲突。"""

    def test_batch_drift_wins_over_replay_and_same_version_conflict(self):
        replay_row = _row("snap-existing", 2, DIGEST_1, "v1", SHA_A, 10)
        conflict_row = _row("snap-conflict", 3, DIGEST_2, "v1", SHA_B, 20)
        incoming = _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=10)
        for existing in ([], [replay_row], [conflict_row], [replay_row, conflict_row]):
            with self.subTest(rows=len(existing)):
                existing_before = copy.deepcopy(existing)
                error = self._expect_error(
                    CANDIDATE.ERROR_SOURCE_DRIFT, existing, incoming, batch_drift=True
                )
                self.assertEqual(error.details.get("reason"), CANDIDATE.REASON_BATCH_DRIFT)
                self.assertEqual(error.details.get("path"), "$.batch_drift")
                self.assertEqual(existing, existing_before)

        # 即使同时给出受控声明，也不豁免漂移。
        self._expect_error(
            CANDIDATE.ERROR_SOURCE_DRIFT,
            [conflict_row],
            incoming,
            batch_drift=True,
            new_content_reason="受控修订声明",
        )


class SameVersionByteConflictCase(RevisionDecisionCase):
    """同 documentVersion 的字节 / SHA 硬冲突，声明不能豁免。"""

    def test_same_document_version_different_bytes_is_a_hard_conflict(self):
        existing = [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)]
        variants = (
            # 同版本、同 SHA、仅字节不同：必须硬冲突（字节回归）。
            ("same_sha_different_bytes",
             _incoming(digest=DIGEST_2, version="v1", sha=SHA_A, size=11)),
            ("different_sha",
             _incoming(digest=DIGEST_2, version="v1", sha=SHA_B, size=10)),
            # digest 命中也要先走同版本字节冲突（不能被重放遮蔽）。
            ("digest_match_bytes_conflict",
             _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=11)),
        )
        for label, incoming in variants:
            with self.subTest(case=label):
                existing_before = copy.deepcopy(existing)
                error = self._expect_error(
                    CANDIDATE.ERROR_SNAPSHOT_CONFLICT, existing, incoming
                )
                self.assertEqual(
                    error.details.get("reason"),
                    CANDIDATE.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
                )
                # 受控声明不能豁免第 2 步硬冲突。
                error = self._expect_error(
                    CANDIDATE.ERROR_SNAPSHOT_CONFLICT,
                    existing,
                    incoming,
                    new_content_reason="受控修订声明",
                )
                self.assertEqual(
                    error.details.get("reason"),
                    CANDIDATE.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
                )
                self.assertEqual(existing, existing_before)

    def test_conflicting_same_version_row_is_not_shadowed_by_a_replay_match(self):
        existing = [
            _row("snap-match", 1, DIGEST_1, "v1", SHA_A, 10),
            _row("snap-conflict", 2, DIGEST_2, "v1", SHA_B, 20),
        ]
        incoming = _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=10)
        error = self._expect_error(CANDIDATE.ERROR_SNAPSHOT_CONFLICT, existing, incoming)
        self.assertEqual(
            error.details.get("reason"),
            CANDIDATE.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
        )


class DigestMetadataCase(RevisionDecisionCase):
    """同 digest 但元数据不一致：视为损坏 / 冲突，拒绝重放。"""

    def test_digest_match_with_mismatched_metadata_is_a_conflict(self):
        existing = [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)]
        variants = (
            ("different_version",
             _incoming(digest=DIGEST_1, version="v2", sha=SHA_A, size=10)),
            ("different_sha",
             _incoming(digest=DIGEST_1, version="v2", sha=SHA_B, size=10)),
            ("different_bytes",
             _incoming(digest=DIGEST_1, version="v2", sha=SHA_A, size=99)),
        )
        for label, incoming in variants:
            with self.subTest(case=label):
                error = self._expect_error(
                    CANDIDATE.ERROR_SNAPSHOT_CONFLICT, existing, incoming
                )
                self.assertEqual(
                    error.details.get("reason"), CANDIDATE.REASON_DIGEST_METADATA_MISMATCH
                )
                # 受控声明不改变该拒绝。
                error = self._expect_error(
                    CANDIDATE.ERROR_SNAPSHOT_CONFLICT,
                    existing,
                    incoming,
                    new_content_reason="受控修订声明",
                )
                self.assertEqual(
                    error.details.get("reason"), CANDIDATE.REASON_DIGEST_METADATA_MISMATCH
                )


class DeclarationCase(RevisionDecisionCase):
    """同版本同字节但结构化映射变化：需要受控声明才能追加候选。"""

    def test_same_version_same_bytes_mapping_change_requires_declaration(self):
        existing = [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)]
        incoming = _incoming(digest=DIGEST_2, version="v1", sha=SHA_A, size=10)
        existing_before = copy.deepcopy(existing)
        error = self._expect_error(CANDIDATE.ERROR_SNAPSHOT_CONFLICT, existing, incoming)
        self.assertEqual(
            error.details.get("reason"),
            CANDIDATE.REASON_NEW_CONTENT_DECLARATION_REQUIRED,
        )
        result = decide_planning_revision(
            existing, incoming, new_content_reason="受控修订版本声明"
        )
        self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)
        self.assertEqual(result["snapshotRevision"], 2)
        self.assertIs(result["wouldActivate"], False)
        self.assertEqual(existing, existing_before)


class UnverifiedVersionCase(RevisionDecisionCase):
    """unverified 占位版本：无受控声明拒绝；同版本不同字节仍是硬冲突。"""

    def test_same_unverified_version_with_different_bytes_is_a_hard_conflict(self):
        existing = [_row("snap-a", 1, DIGEST_1, "unverified", SHA_A, 10)]
        incoming = _incoming(digest=DIGEST_2, version="unverified", sha=SHA_B, size=20)
        error = self._expect_error(CANDIDATE.ERROR_SNAPSHOT_CONFLICT, existing, incoming)
        self.assertEqual(
            error.details.get("reason"),
            CANDIDATE.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
        )
        error = self._expect_error(
            CANDIDATE.ERROR_SNAPSHOT_CONFLICT,
            existing,
            incoming,
            new_content_reason="受控修订声明",
        )
        self.assertEqual(
            error.details.get("reason"),
            CANDIDATE.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
        )

    def test_unverified_version_without_declaration_is_rejected_otherwise(self):
        existing = [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)]
        incoming = _incoming(digest=DIGEST_2, version="unverified", sha=SHA_B, size=20)
        error = self._expect_error(CANDIDATE.ERROR_SNAPSHOT_CONFLICT, existing, incoming)
        self.assertEqual(
            error.details.get("reason"),
            CANDIDATE.REASON_NEW_CONTENT_DECLARATION_REQUIRED,
        )
        result = decide_planning_revision(
            existing, incoming, new_content_reason="受控修订声明"
        )
        self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)
        self.assertEqual(result["snapshotRevision"], 2)

    def test_unverified_placeholder_is_case_exact(self):
        # 大小写精确：``UNVERIFIED`` 不是占位值，按未知但明确的版本字符串处理。
        self.assertEqual(CANDIDATE.UNVERIFIED_DOCUMENT_VERSION, "unverified")
        existing = [_row("snap-a", 1, DIGEST_1, "unverified", SHA_A, 10)]
        incoming = _incoming(digest=DIGEST_2, version="UNVERIFIED", sha=SHA_B, size=20)
        result = decide_planning_revision(existing, incoming)
        self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)
        self.assertEqual(result["snapshotRevision"], 2)


class ParameterAndShapeRejectionCase(RevisionDecisionCase):
    """参数 / 自报字段 / 类型 / 非 JSON 值 / 重复项一律拒绝。"""

    def test_batch_drift_and_declaration_parameters_are_strict(self):
        for value in (1, 0, "true", None, [], {}, 1.0):
            with self.subTest(batch_drift=repr(value)):
                self._expect_error(
                    CANDIDATE.ERROR_INVALID, [], _incoming(), batch_drift=value
                )
        for value in ("", "   ", "\t\n", 1, [], {}, True):
            with self.subTest(new_content_reason=repr(value)):
                self._expect_error(
                    CANDIDATE.ERROR_INVALID, [], _incoming(), new_content_reason=value
                )
        # 带前后空白的声明可用，且原样保留（不 strip、不归一化）。
        existing = [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)]
        incoming = _incoming(digest=DIGEST_2, version="v1", sha=SHA_A, size=10)
        result = decide_planning_revision(
            existing, incoming, new_content_reason="  受控声明带前后空白  "
        )
        self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)

    def test_existing_shape_and_self_reported_fields_are_rejected(self):
        for value in ({}, (), "x", None, 1, 1.0):
            with self.subTest(existing=repr(value)):
                self._expect_error(CANDIDATE.ERROR_INVALID, value, _incoming())

        for value in ([], "row", None, 1, ("snap-1",)):
            with self.subTest(row=repr(value)):
                self._expect_error(CANDIDATE.ERROR_INVALID, [value], _incoming())

        for field in (
            "projectId",
            "planningSourceId",
            "sourceKind",
            "status",
            "revision",
            "digest",
            "taskId",
            "attemptId",
            "eventId",
            "importedAt",
            "importedBy",
            "content",
            "planStatus",
        ):
            with self.subTest(unknown_field=field):
                row = _row()
                row[field] = "自报值"
                self._expect_error(CANDIDATE.ERROR_INVALID, [row], _incoming())

        for field in CANDIDATE.EXISTING_FIELDS:
            with self.subTest(missing_field=field):
                row = _row()
                del row[field]
                self._expect_error(CANDIDATE.ERROR_INVALID, [row], _incoming())

    def test_existing_row_field_types_are_strict(self):
        cases = (
            ("snapshotId", ""),
            ("snapshotId", None),
            ("snapshotId", 1),
            ("snapshotId", True),
            ("snapshotId", ["snap-1"]),
            ("snapshotId", {"id": "snap-1"}),
            ("snapshotId", object()),
            ("snapshotRevision", 0),
            ("snapshotRevision", -1),
            ("snapshotRevision", True),
            ("snapshotRevision", 1.0),
            ("snapshotRevision", "1"),
            ("snapshotRevision", None),
            ("canonicalDigest", "sha256:" + "A" * 64),
            ("canonicalDigest", "sha256:" + "1" * 63),
            ("canonicalDigest", "sha256:" + "1" * 65),
            ("canonicalDigest", "1" * 64),
            ("canonicalDigest", "SHA256:" + "1" * 64),
            ("canonicalDigest", "sha256:" + "g" * 64),
            ("canonicalDigest", None),
            ("canonicalDigest", ["sha256:" + "1" * 64]),
            ("documentVersion", ""),
            ("documentVersion", None),
            ("documentVersion", 1),
            ("documentVersion", True),
            ("sourceSha256", "sha256:" + "0" * 63),
            ("sourceSha256", "sha256:" + "F" * 64),
            ("sourceSha256", "0" * 64),
            ("sourceSha256", None),
            ("sourceSha256", 0),
            ("sourceBytes", -1),
            ("sourceBytes", True),
            ("sourceBytes", 1.5),
            ("sourceBytes", float("nan")),
            ("sourceBytes", "10"),
            ("sourceBytes", {1, 2}),
            ("sourceBytes", None),
        )
        for field, value in cases:
            with self.subTest(field=field, value=repr(value)):
                row = _row()
                row[field] = value
                error = self._expect_error(CANDIDATE.ERROR_INVALID, [row], _incoming())
                self.assertEqual(error.details.get("reason"), CANDIDATE.REASON_FIELD_TYPE)

    def test_incoming_shape_and_self_reported_fields_are_rejected(self):
        for value in ([], (), "x", None, 1, 1.0):
            with self.subTest(incoming=repr(value)):
                self._expect_error(CANDIDATE.ERROR_INVALID, [], value)

        for field in (
            "snapshotId",
            "snapshotRevision",
            "projectId",
            "planningSourceId",
            "sourceKind",
            "planStatus",
            "taskId",
            "attemptId",
            "newContentReason",
            "status",
        ):
            with self.subTest(self_reported_field=field):
                incoming = _incoming()
                incoming[field] = "自报值"
                self._expect_error(CANDIDATE.ERROR_INVALID, [], incoming)

        for field in CANDIDATE.INCOMING_FIELDS:
            with self.subTest(missing_field=field):
                incoming = _incoming()
                del incoming[field]
                self._expect_error(CANDIDATE.ERROR_INVALID, [], incoming)

        bad_types = (
            ("canonicalDigest", "sha256:" + "1" * 63),
            ("canonicalDigest", None),
            ("documentVersion", ""),
            ("documentVersion", 2),
            ("sourceSha256", "sha256:" + "1" * 64 + "0"),
            ("sourceSha256", True),
            ("sourceBytes", True),
            ("sourceBytes", -5),
            ("sourceBytes", 2.0),
            ("sourceBytes", None),
        )
        for field, value in bad_types:
            with self.subTest(field=field, value=repr(value)):
                incoming = _incoming()
                incoming[field] = value
                self._expect_error(CANDIDATE.ERROR_INVALID, [], incoming)

    def test_canonical_digest_and_source_sha256_do_not_substitute(self):
        existing = [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)]
        # 把既有 sourceSha256 当作 incoming canonicalDigest：形态合法但语义不同，
        # 不能被当成同 digest 重放。
        incoming = _incoming(digest=SHA_A, version="v9", sha=SHA_B, size=20)
        result = decide_planning_revision(existing, incoming)
        self.assertEqual(result["decision"], CANDIDATE.DECISION_APPEND_CANDIDATE)

    def test_duplicate_ids_revisions_and_digests_are_rejected(self):
        cases = (
            (
                "duplicate_snapshot_id",
                [_row("snap-dup", 1, DIGEST_1, "v1", SHA_A, 10),
                 _row("snap-dup", 2, DIGEST_2, "v2", SHA_B, 20)],
                _incoming(digest=DIGEST_3, version="v3", sha=SHA_C, size=30),
                CANDIDATE.REASON_DUPLICATE_SNAPSHOT_ID,
            ),
            (
                "duplicate_snapshot_revision",
                [_row("snap-a", 3, DIGEST_1, "v1", SHA_A, 10),
                 _row("snap-b", 3, DIGEST_2, "v2", SHA_B, 20)],
                _incoming(digest=DIGEST_3, version="v3", sha=SHA_C, size=30),
                CANDIDATE.REASON_DUPLICATE_SNAPSHOT_REVISION,
            ),
            (
                "duplicate_canonical_digest_identical_metadata",
                [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
                 _row("snap-b", 2, DIGEST_1, "v1", SHA_A, 10)],
                _incoming(digest=DIGEST_3, version="v3", sha=SHA_C, size=30),
                CANDIDATE.REASON_DUPLICATE_CANONICAL_DIGEST,
            ),
            (
                "duplicate_canonical_digest_conflicting_metadata",
                [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
                 _row("snap-b", 2, DIGEST_1, "v2", SHA_B, 20)],
                _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=10),
                CANDIDATE.REASON_DUPLICATE_CANONICAL_DIGEST,
            ),
        )
        for label, existing, incoming, expected_reason in cases:
            with self.subTest(case=label):
                error = self._expect_error(
                    CANDIDATE.ERROR_INVALID, existing, incoming
                )
                self.assertEqual(error.details.get("reason"), expected_reason)

    def test_duplicate_history_digest_precedes_drift(self):
        """回归：任意两个历史行 digest 重复，即使 incoming 是第三个 digest 且叠加
        ``batch_drift=True`` / 受控声明，结构校验也必须先于业务判断报
        ``planning_revision_invalid``（reason=duplicate_canonical_digest）。"""
        existing = [
            # 两个既有行 canonicalDigest 相同（元数据相同与冲突两种形态都覆盖）。
            _row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
            _row("snap-b", 2, DIGEST_1, "v2", SHA_B, 20),
        ]
        conflicting_metadata = [
            _row("snap-c", 1, DIGEST_2, "v1", SHA_A, 10),
            _row("snap-d", 5, DIGEST_2, "v5", SHA_C, 30),
        ]
        # 本次 incoming 是第三个 digest，不命中任何既有 digest。
        incoming = _incoming(digest=DIGEST_3, version="v3", sha=SHA_C, size=30)
        variant_kwargs = (
            {},
            {"batch_drift": True},
            {"batch_drift": True, "new_content_reason": "受控修订声明"},
            {"new_content_reason": "受控修订声明"},
        )
        for label, rows in (("identical_metadata", existing),
                            ("conflicting_metadata", conflicting_metadata)):
            for kwargs in variant_kwargs:
                with self.subTest(rows=label, kwargs=sorted(kwargs)):
                    rows_before = copy.deepcopy(rows)
                    error = self._expect_error(
                        CANDIDATE.ERROR_INVALID, rows, incoming, **kwargs
                    )
                    self.assertEqual(
                        error.details.get("reason"),
                        CANDIDATE.REASON_DUPLICATE_CANONICAL_DIGEST,
                    )
                    self.assertEqual(rows, rows_before)

    def test_unknown_error_codes_are_rejected(self):
        for code in ("planning_revision_ok", "planning_snapshot_invalid", "", None):
            with self.subTest(code=repr(code)):
                with self.assertRaises(ValueError):
                    PlanningRevisionError(code, "x")


class PurityAndInputIntegrityCase(RevisionDecisionCase):
    """输入不变、返回值为标量、无 I/O / 时钟 / 编号来源 / 数据库。"""

    def test_inputs_are_never_modified_on_any_path(self):
        existing = [
            _row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10),
            _row("snap-b", 4, DIGEST_2, "v2", SHA_B, 20),
        ]
        incoming = _incoming(digest=DIGEST_3, version="v3", sha=SHA_C, size=30)
        paths = (
            ("append", {}, None),
            ("append_by_declaration", {"new_content_reason": "声明"}, None),
            ("drift", {"batch_drift": True}, CANDIDATE.ERROR_SOURCE_DRIFT),
            ("invalid", {"batch_drift": "yes"}, CANDIDATE.ERROR_INVALID),
        )
        for label, kwargs, expected_code in paths:
            with self.subTest(path=label):
                existing_before = copy.deepcopy(existing)
                incoming_before = copy.deepcopy(incoming)
                existing_text = _canonical(existing)
                incoming_text = _canonical(incoming)
                if expected_code is None:
                    decide_planning_revision(existing, incoming, **kwargs)
                else:
                    self._expect_error(expected_code, existing, incoming, **kwargs)
                self.assertEqual(existing, existing_before)
                self.assertEqual(incoming, incoming_before)
                self.assertEqual(_canonical(existing), existing_text)
                self.assertEqual(_canonical(incoming), incoming_text)

        # 冲突 / 重放 / 首次路径同样不改输入。
        scenarios = (
            ([_row("snap-a", 2, DIGEST_1, "v1", SHA_A, 10)],
             _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=10)),
            ([_row("snap-a", 2, DIGEST_1, "v1", SHA_A, 10)],
             _incoming(digest=DIGEST_2, version="v1", sha=SHA_B, size=20)),
            ([], _incoming()),
        )
        for existing_arg, incoming_arg in scenarios:
            with self.subTest(scenario=_canonical(incoming_arg)):
                existing_before = copy.deepcopy(existing_arg)
                incoming_before = copy.deepcopy(incoming_arg)
                try:
                    decide_planning_revision(existing_arg, incoming_arg)
                except PlanningRevisionError:
                    pass
                self.assertEqual(existing_arg, existing_before)
                self.assertEqual(incoming_arg, incoming_before)

    def test_returned_values_are_immutable_scalars_and_carry_no_facts(self):
        results = (
            decide_planning_revision([], _incoming()),
            decide_planning_revision(
                [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)],
                _incoming(digest=DIGEST_2, version="v2", sha=SHA_B, size=20),
            ),
            decide_planning_revision(
                [_row("snap-a", 1, DIGEST_1, "v1", SHA_A, 10)],
                _incoming(digest=DIGEST_1, version="v1", sha=SHA_A, size=10),
            ),
        )
        for result in results:
            with self.subTest(decision=result["decision"]):
                for key, value in result.items():
                    self.assertIsInstance(value, (str, int, bool), key)
                self.assertIs(result["wouldActivate"], False)
                self.assertIn(
                    sorted(result), (sorted(CANDIDATE.RESULT_KEYS),
                                     sorted(CANDIDATE.RESULT_REPLAYED_KEYS)),
                )
                for forbidden in (
                    "status",
                    "approved",
                    "activated",
                    "accepted",
                    "acceptedByHost",
                    "taskId",
                    "attemptId",
                    "eventId",
                    "deliveryId",
                    "reviewId",
                    "modelProfile",
                    "permissions",
                    "workspace",
                    "written",
                ):
                    self.assertNotIn(forbidden, result)

    def test_signature_is_frozen(self):
        signature = inspect.signature(decide_planning_revision)
        self.assertEqual(
            list(signature.parameters),
            ["existing", "incoming", "batch_drift", "new_content_reason"],
        )
        self.assertEqual(
            signature.parameters["existing"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        self.assertEqual(
            signature.parameters["incoming"].kind,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for name, default in (("batch_drift", False), ("new_content_reason", None)):
            with self.subTest(parameter=name):
                parameter = signature.parameters[name]
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameter.default, default)


class ProductPurityStaticCase(unittest.TestCase):
    """静态核验：候选无导入、无 I/O、无时钟、无编号来源、无数据库 / 执行事实。"""

    _FORBIDDEN_MODULES = frozenset(
        (
            "os",
            "sys",
            "io",
            "json",
            "time",
            "datetime",
            "uuid",
            "random",
            "sqlite3",
            "subprocess",
            "socket",
            "shutil",
            "glob",
            "pathlib",
            "tempfile",
            "pickle",
            "hashlib",
            "requests",
            "urllib",
        )
    )
    _FORBIDDEN_CALLS = frozenset(
        (
            "open",
            "exec",
            "eval",
            "compile",
            "__import__",
            "input",
            "connect",
            "now",
            "time",
            "uuid4",
            "random",
        )
    )
    _FORBIDDEN_TOKENS = (
        "datetime",
        "uuid",
        "time.time",
        "sqlite3",
        "open(",
        "subprocess",
        "os.environ",
    )

    def _load_source(self):
        with open(CANDIDATE_PATH, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_candidate_module_has_no_imports_and_no_side_effect_primitives(self):
        source = self._load_source()
        tree = ast.parse(source)

        imports = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        self.assertEqual(imports, [])

        used_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used_names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                used_names.add(node.value.id)
        self.assertEqual(sorted(used_names & self._FORBIDDEN_MODULES), [])

        called_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    called_names.add(func.id)
                elif isinstance(func, ast.Attribute):
                    called_names.add(func.attr)
        self.assertEqual(sorted(called_names & self._FORBIDDEN_CALLS), [])

        for token in self._FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_candidate_module_exposes_no_module_objects_and_no_io_helpers(self):
        for name, value in vars(CANDIDATE).items():
            if name.startswith("__"):
                continue
            with self.subTest(global_name=name):
                self.assertFalse(inspect.ismodule(value))

        # 结果只描述建议，不含「已写入 / 已激活 / 可派发」等结论字段。
        result = decide_planning_revision([], _incoming())
        self.assertEqual(sorted(result), sorted(CANDIDATE.RESULT_KEYS))
        self.assertIs(result["wouldActivate"], False)

    def test_candidate_path_binding_is_unique(self):
        self.assertEqual(
            os.path.realpath(CANDIDATE.__file__), os.path.realpath(CANDIDATE_PATH)
        )
        self.assertIs(sys.modules[_CANDIDATE_NAME], CANDIDATE)
        duplicates = sorted(
            name
            for name, module in list(sys.modules.items())
            if name != _CANDIDATE_NAME
            and getattr(module, "__file__", None)
            and os.path.realpath(module.__file__) == os.path.realpath(CANDIDATE_PATH)
        )
        self.assertEqual(duplicates, [])


if __name__ == "__main__":
    unittest.main()
