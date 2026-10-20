"""planning-snapshot.v1 结构 / 唯一 ID / 显式引用关系定向测试（第 010 轮候选）。

- 只使用标准库 ``unittest``；不访问网络、数据库、环境；除**只读**核对冻结摘要模块
  字节外不读写任何文件（不读原规划文件）。
- 测试期望全部使用**字面量 / 独立标准库编码**构造，不使用被测实现生成期望关系。
- worker 环境 ``bashCommands=[]``：worker 侧一律 NOT_RUN、不自报 PASS；
  由 cox 独立离线执行本文件与既有摘要测试。
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
from contracts.planning_structure import validate_planning_structure  # noqa: E402

PROJECT_ID = "proj-alpha"
PLANNING_SOURCE_ID = "src-001"
FILE_SHA256 = "sha256:" + "a" * 64

EXPECTED_RESULT_KEYS = (
    "valid",
    "payload",
    "canonicalDigest",
    "diagnostics",
    "wouldWrite",
    "wouldDispatch",
    "wouldActivate",
)


def _valid_snapshot():
    """一个通过 digest 冻结检查、且四层引用全部可解析的完整快照。"""
    return {
        "schemaVersion": "planning-snapshot.v1",
        "projectId": PROJECT_ID,
        "planningSourceId": PLANNING_SOURCE_ID,
        "sourceKind": "product",
        "sourceFile": {
            "name": "plan.md",
            "path": "/data/planning/plan.md",
            "documentVersion": "v1.0",
            "sha256": FILE_SHA256,
            "bytes": 1234,
        },
        "sections": [
            {"sectionId": "sec-1", "title": "背景", "anchor": "#背景", "level": 1},
            {"sectionId": "sec-2", "title": "目标", "anchor": "#目标", "level": 2},
        ],
        "stages": [
            {"stageId": "stg-1", "title": "阶段一", "summary": "", "sectionRef": "sec-1"},
        ],
        "businessLines": [
            {
                "businessLineId": "bl-1",
                "title": "业务线一",
                "planStatus": "planned",
                "stageRef": "stg-1",
                "sectionRef": "sec-2",
            },
        ],
        "milestones": [
            {
                "milestoneId": "ms-1",
                "title": "里程碑一",
                "gateCriteria": ["独立审核通过", "证据可追溯"],
                "planStatus": "proposed",
                "businessLineRef": "bl-1",
                "sectionRef": "sec-1",
            },
        ],
        "planStatus": "planned",
        "unresolved": [],
    }


def _section(section_id, title="章节", anchor="#a", level=1):
    return {"sectionId": section_id, "title": title, "anchor": anchor, "level": level}


def _stage(stage_id, section_ref="sec-1", title="阶段", summary=""):
    item = {"stageId": stage_id, "title": title, "summary": summary}
    if section_ref is not _MISSING:
        item["sectionRef"] = section_ref
    return item


def _line(line_id, stage_ref="stg-1", section_ref="sec-1", title="业务线",
          plan_status="planned"):
    item = {"businessLineId": line_id, "title": title, "planStatus": plan_status}
    if stage_ref is not _MISSING:
        item["stageRef"] = stage_ref
    if section_ref is not _MISSING:
        item["sectionRef"] = section_ref
    return item


def _milestone(milestone_id, line_ref="bl-1", section_ref="sec-1", title="里程碑",
               gate_criteria="ok", plan_status="proposed"):
    item = {
        "milestoneId": milestone_id,
        "title": title,
        "gateCriteria": gate_criteria,
        "planStatus": plan_status,
    }
    if line_ref is not _MISSING:
        item["businessLineRef"] = line_ref
    if section_ref is not _MISSING:
        item["sectionRef"] = section_ref
    return item


_MISSING = object()


class _ValidatorTestCase(unittest.TestCase):
    def _validate(self, snapshot, project=PROJECT_ID, source=PLANNING_SOURCE_ID):
        return validate_planning_structure(
            snapshot,
            expected_project_id=project,
            expected_planning_source_id=source,
        )

    def _assert_rejected(
        self, snapshot, code, expected_path=None, project=PROJECT_ID,
        source=PLANNING_SOURCE_ID,
    ):
        before = copy.deepcopy(snapshot)
        with self.assertRaises(PlanningDigestError) as ctx:
            self._validate(snapshot, project=project, source=source)
        error = ctx.exception
        self.assertEqual(error.code, code)
        self.assertTrue(error.details.get("path"))
        if expected_path is not None:
            self.assertEqual(error.details.get("path"), expected_path)
        # 失败路径同样不得修改输入。
        self.assertEqual(snapshot, before)
        return error


class ValidStructureTests(_ValidatorTestCase):
    def test_full_valid_graph_returns_valid_and_frozen_flags(self):
        result = self._validate(_valid_snapshot())
        self.assertEqual(set(result), set(EXPECTED_RESULT_KEYS))
        self.assertIs(result["valid"], True)
        self.assertIs(result["wouldWrite"], False)
        self.assertIs(result["wouldDispatch"], False)
        self.assertIs(result["wouldActivate"], False)
        self.assertEqual(result["diagnostics"], [])

    def test_canonical_digest_equals_reused_function(self):
        snapshot = _valid_snapshot()
        result = self._validate(snapshot)
        self.assertEqual(result["canonicalDigest"], compute_canonical_digest(snapshot))
        digest = result["canonicalDigest"]
        self.assertTrue(digest.startswith("sha256:"))
        hex_part = digest[len("sha256:"):]
        self.assertEqual(len(hex_part), 64)
        self.assertEqual(hex_part, hex_part.lower())
        self.assertTrue(all(char in "0123456789abcdef" for char in hex_part))

    def test_canonical_digest_matches_independent_stdlib_encoding(self):
        snapshot = _valid_snapshot()
        result = self._validate(snapshot)
        encoded = json.dumps(
            result["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        expected = "sha256:" + hashlib.sha256(encoded).hexdigest()
        self.assertEqual(result["canonicalDigest"], expected)

    def test_payload_equals_reused_digest_payload(self):
        snapshot = _valid_snapshot()
        self.assertEqual(self._validate(snapshot)["payload"], digest_payload(snapshot))

    def test_payload_is_independent_deep_copy(self):
        snapshot = _valid_snapshot()
        before = copy.deepcopy(snapshot)
        result = self._validate(snapshot)
        payload = result["payload"]
        self.assertIsNot(payload["sections"], snapshot["sections"])
        self.assertIsNot(payload["sections"][0], snapshot["sections"][0])
        self.assertIsNot(payload["businessLines"][0], snapshot["businessLines"][0])
        # 返回值嵌套修改不得改变输入。
        payload["sections"][0]["title"] = "被改动的标题"
        payload["businessLines"][0]["stageRef"] = "stg-9"
        payload["milestones"][0]["gateCriteria"].append("注入项")
        self.assertEqual(snapshot, before)
        self.assertEqual(snapshot["sections"][0]["title"], "背景")
        self.assertEqual(snapshot["businessLines"][0]["stageRef"], "stg-1")

    def test_input_is_not_mutated_on_success(self):
        snapshot = _valid_snapshot()
        before = copy.deepcopy(snapshot)
        self._validate(snapshot)
        self.assertEqual(snapshot, before)

    def test_result_does_not_contain_generated_identifiers(self):
        result = self._validate(_valid_snapshot())
        for forbidden in ("snapshotId", "snapshotRevision", "taskId", "attemptId",
                          "reviewId", "deliveryId", "eventId", "importedAt"):
            self.assertNotIn(forbidden, result)


class EmptyCollectionsTests(_ValidatorTestCase):
    def _empty_snapshot(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = []
        snapshot["stages"] = []
        snapshot["businessLines"] = []
        snapshot["milestones"] = []
        return snapshot

    def test_empty_four_arrays_is_valid(self):
        result = self._validate(self._empty_snapshot())
        self.assertIs(result["valid"], True)
        self.assertEqual(result["diagnostics"], [])

    def test_empty_collections_do_not_fabricate_content(self):
        result = self._validate(self._empty_snapshot())
        payload = result["payload"]
        self.assertEqual(payload["sections"], [])
        self.assertEqual(payload["stages"], [])
        self.assertEqual(payload["businessLines"], [])
        self.assertEqual(payload["milestones"], [])
        self.assertEqual(payload["unresolved"], [])


class ReferenceResolutionTests(_ValidatorTestCase):
    def test_forward_reference_to_later_item_is_allowed(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = [_section("sec-1", "背景"), _section("sec-2", "目标", "#b", 2)]
        snapshot["stages"] = [
            _stage("stg-1", section_ref="sec-2"),
            _stage("stg-2", section_ref="sec-1"),
        ]
        snapshot["businessLines"] = [
            _line("bl-1", stage_ref="stg-2", section_ref="sec-1"),
            _line("bl-2", stage_ref="stg-1", section_ref="sec-2"),
        ]
        snapshot["milestones"] = [_milestone("ms-1", line_ref="bl-2", section_ref="sec-2")]
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["diagnostics"], [])

    def test_same_title_different_ids_are_not_merged(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = [
            _section("sec-1", "同名章节", "#a", 1),
            _section("sec-2", "同名章节", "#b", 1),
        ]
        snapshot["stages"] = [_stage("stg-1", section_ref="sec-1")]
        snapshot["businessLines"] = [
            _line("bl-1", section_ref="sec-1", title="同名业务线"),
            _line("bl-2", section_ref="sec-2", title="同名业务线"),
        ]
        snapshot["milestones"] = []
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["diagnostics"], [])
        sections = result["payload"]["sections"]
        lines = result["payload"]["businessLines"]
        self.assertEqual(len(sections), 2)
        self.assertEqual([item["sectionId"] for item in sections], ["sec-1", "sec-2"])
        self.assertEqual(sections[0]["title"], sections[1]["title"])
        self.assertEqual(len(lines), 2)
        self.assertEqual([item["businessLineId"] for item in lines], ["bl-1", "bl-2"])

    def test_same_id_across_different_types_is_allowed(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = [_section("shared-1")]
        snapshot["stages"] = [_stage("shared-1", section_ref="shared-1")]
        snapshot["businessLines"] = [_line("shared-1", stage_ref="shared-1",
                                           section_ref="shared-1")]
        snapshot["milestones"] = [_milestone("shared-1", line_ref="shared-1",
                                             section_ref="shared-1")]
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["diagnostics"], [])


class DuplicateIdTests(_ValidatorTestCase):
    def test_duplicate_section_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = [_section("sec-1"), _section("sec-1", "重复", "#b", 2)]
        self._assert_rejected(
            snapshot, "planning_mapping_ambiguous", "$.sections[1].sectionId"
        )

    def test_duplicate_stage_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["stages"] = [_stage("stg-1"), _stage("stg-1")]
        self._assert_rejected(
            snapshot, "planning_mapping_ambiguous", "$.stages[1].stageId"
        )

    def test_duplicate_business_line_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["businessLines"] = [_line("bl-1"), _line("bl-1")]
        self._assert_rejected(
            snapshot, "planning_mapping_ambiguous", "$.businessLines[1].businessLineId"
        )

    def test_duplicate_milestone_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"] = [_milestone("ms-1"), _milestone("ms-1")]
        self._assert_rejected(
            snapshot, "planning_mapping_ambiguous", "$.milestones[1].milestoneId"
        )


class MissingMappingDiagnosticTests(_ValidatorTestCase):
    def test_missing_section_ref_is_non_fatal_single_diagnostic(self):
        snapshot = _valid_snapshot()
        snapshot["stages"] = [_stage("stg-1", section_ref=_MISSING)]
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(
            result["diagnostics"],
            [{"code": "planning_mapping_missing", "path": "$.stages[0].sectionRef"}],
        )

    def test_none_and_empty_refs_are_non_fatal_with_exact_order(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = [_section("sec-1")]
        snapshot["stages"] = [_stage("stg-1", section_ref=_MISSING)]
        snapshot["businessLines"] = [
            _line("bl-1", stage_ref=_MISSING, section_ref=_MISSING),
            _line("bl-2", stage_ref=None, section_ref=""),
        ]
        snapshot["milestones"] = [
            _milestone("ms-1", line_ref=_MISSING, section_ref=""),
        ]
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(
            result["diagnostics"],
            [
                {"code": "planning_mapping_missing", "path": "$.stages[0].sectionRef"},
                {"code": "planning_mapping_missing", "path": "$.businessLines[0].stageRef"},
                {"code": "planning_mapping_missing", "path": "$.businessLines[0].sectionRef"},
                {"code": "planning_mapping_missing", "path": "$.businessLines[1].stageRef"},
                {"code": "planning_mapping_missing", "path": "$.businessLines[1].sectionRef"},
                {"code": "planning_mapping_missing", "path": "$.milestones[0].businessLineRef"},
                {"code": "planning_mapping_missing", "path": "$.milestones[0].sectionRef"},
            ],
        )

    def test_missing_mapping_keeps_input_and_payload_unchanged(self):
        snapshot = _valid_snapshot()
        snapshot["stages"] = [_stage("stg-1", section_ref=_MISSING)]
        before = copy.deepcopy(snapshot)
        result = self._validate(snapshot)
        self.assertEqual(snapshot, before)
        self.assertEqual(result["payload"], digest_payload(snapshot))
        self.assertNotIn("diagnostics", result["payload"])
        self.assertNotIn("sectionRef", result["payload"]["stages"][0])
        # 诊断不参与摘要。
        self.assertEqual(result["canonicalDigest"], compute_canonical_digest(snapshot))

    def test_diagnostics_do_not_touch_unresolved(self):
        snapshot = _valid_snapshot()
        unresolved = [{"note": "待补充", "sectionTitle": "未归类"}]
        snapshot["unresolved"] = copy.deepcopy(unresolved)
        snapshot["stages"] = [_stage("stg-1", section_ref=_MISSING)]
        result = self._validate(snapshot)
        self.assertEqual(result["payload"]["unresolved"], unresolved)
        self.assertEqual(
            result["diagnostics"],
            [{"code": "planning_mapping_missing", "path": "$.stages[0].sectionRef"}],
        )


class DanglingAndInvalidReferenceTests(_ValidatorTestCase):
    def test_dangling_section_ref_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["stages"] = [_stage("stg-1", section_ref="sec-9")]
        self._assert_rejected(
            snapshot, "planning_mapping_invalid", "$.stages[0].sectionRef"
        )

    def test_dangling_stage_ref_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["businessLines"] = [_line("bl-1", stage_ref="stg-9")]
        self._assert_rejected(
            snapshot, "planning_mapping_invalid", "$.businessLines[0].stageRef"
        )

    def test_dangling_business_line_ref_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"] = [_milestone("ms-1", line_ref="bl-9")]
        self._assert_rejected(
            snapshot, "planning_mapping_invalid", "$.milestones[0].businessLineRef"
        )

    def test_illegal_id_shape_reference_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["stages"] = [_stage("stg-1", section_ref="bad id!")]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.stages[0].sectionRef"
        )

    def test_non_string_reference_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["businessLines"] = [_line("bl-1", stage_ref=123)]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.businessLines[0].stageRef"
        )

    def test_dict_reference_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"] = [_milestone("ms-1", line_ref={})]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].businessLineRef"
        )


class ExpectedIdentityTests(_ValidatorTestCase):
    def test_cross_project_expected_rejected(self):
        self._assert_rejected(
            _valid_snapshot(),
            "planning_cross_project_reference",
            "$.projectId",
            project="proj-beta",
        )

    def test_near_miss_project_id_is_still_cross_project(self):
        self._assert_rejected(
            _valid_snapshot(),
            "planning_cross_project_reference",
            "$.projectId",
            project="proj-alphaX",
        )

    def test_source_mismatch_rejected(self):
        self._assert_rejected(
            _valid_snapshot(),
            "planning_source_mismatch",
            "$.planningSourceId",
            source="src-999",
        )

    def test_empty_expected_project_rejected(self):
        self._assert_rejected(
            _valid_snapshot(), "planning_structure_invalid", "$.expected_project_id",
            project="",
        )

    def test_non_string_expected_project_rejected(self):
        self._assert_rejected(
            _valid_snapshot(), "planning_structure_invalid", "$.expected_project_id",
            project=123,
        )

    def test_illegal_shape_expected_project_rejected(self):
        self._assert_rejected(
            _valid_snapshot(), "planning_structure_invalid", "$.expected_project_id",
            project="-bad",
        )

    def test_empty_expected_source_rejected(self):
        self._assert_rejected(
            _valid_snapshot(), "planning_structure_invalid",
            "$.expected_planning_source_id", source="",
        )

    def test_none_expected_source_rejected(self):
        self._assert_rejected(
            _valid_snapshot(), "planning_structure_invalid",
            "$.expected_planning_source_id", source=None,
        )


class ObjectFieldValidationTests(_ValidatorTestCase):
    def test_non_dict_section_item_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"] = [1]
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0]")

    def test_non_dict_stage_item_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["stages"] = ["draft"]
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.stages[0]")

    def test_non_dict_business_line_item_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["businessLines"] = [None]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.businessLines[0]"
        )

    def test_non_dict_milestone_item_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"] = [[]]
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.milestones[0]")

    def test_missing_section_field_rejected(self):
        snapshot = _valid_snapshot()
        del snapshot["sections"][0]["sectionId"]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].sectionId"
        )

    def test_missing_stage_summary_rejected(self):
        snapshot = _valid_snapshot()
        del snapshot["stages"][0]["summary"]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.stages[0].summary"
        )

    def test_missing_business_line_plan_status_rejected(self):
        snapshot = _valid_snapshot()
        del snapshot["businessLines"][0]["planStatus"]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.businessLines[0].planStatus"
        )

    def test_missing_milestone_gate_criteria_rejected(self):
        snapshot = _valid_snapshot()
        del snapshot["milestones"][0]["gateCriteria"]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria"
        )

    def test_unknown_section_field_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["content"] = "正文不得出现在结构对象里"
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].content"
        )

    def test_object_level_project_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["projectId"] = "proj-beta"
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].projectId"
        )

    def test_object_level_source_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["sourceId"] = "src-999"
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].sourceId"
        )

    def test_empty_section_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["sectionId"] = ""
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].sectionId"
        )

    def test_oversized_section_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["sectionId"] = "a" * 129
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].sectionId"
        )

    def test_illegal_character_section_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["sectionId"] = "sec/1"
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].sectionId"
        )

    def test_non_string_section_id_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["sectionId"] = 1
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.sections[0].sectionId"
        )

    def test_bool_level_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["level"] = True
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].level")

    def test_zero_level_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["level"] = 0
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].level")

    def test_seven_level_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["level"] = 7
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].level")

    def test_float_level_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["level"] = 1.0
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].level")

    def test_empty_section_title_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["title"] = ""
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].title")

    def test_non_string_stage_title_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["stages"][0]["title"] = 5
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.stages[0].title")

    def test_empty_section_anchor_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["anchor"] = ""
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].anchor")

    def test_non_string_anchor_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["anchor"] = 3
        self._assert_rejected(snapshot, "planning_structure_invalid", "$.sections[0].anchor")

    def test_empty_business_line_plan_status_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["businessLines"][0]["planStatus"] = ""
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.businessLines[0].planStatus"
        )

    def test_non_string_milestone_plan_status_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["planStatus"] = 7
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].planStatus"
        )

    def test_stage_summary_may_be_empty_string(self):
        snapshot = _valid_snapshot()
        snapshot["stages"][0]["summary"] = ""
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["payload"]["stages"][0]["summary"], "")


class GateCriteriaTests(_ValidatorTestCase):
    def test_string_form_is_preserved(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = "独立审核通过"
        result = self._validate(snapshot)
        value = result["payload"]["milestones"][0]["gateCriteria"]
        self.assertIsInstance(value, str)
        self.assertEqual(value, "独立审核通过")

    def test_list_form_preserves_order_and_type(self):
        snapshot = _valid_snapshot()
        criteria = ["乙", "甲", "丙"]
        snapshot["milestones"][0]["gateCriteria"] = list(criteria)
        result = self._validate(snapshot)
        value = result["payload"]["milestones"][0]["gateCriteria"]
        self.assertIsInstance(value, list)
        self.assertEqual(value, criteria)

    def test_forms_are_not_converted_between_each_other(self):
        as_string = _valid_snapshot()
        as_string["milestones"][0]["gateCriteria"] = "ok"
        as_list = _valid_snapshot()
        as_list["milestones"][0]["gateCriteria"] = ["ok"]
        string_value = self._validate(as_string)["payload"]["milestones"][0]["gateCriteria"]
        list_value = self._validate(as_list)["payload"]["milestones"][0]["gateCriteria"]
        self.assertIsInstance(string_value, str)
        self.assertIsInstance(list_value, list)

    def test_empty_string_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = ""
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria"
        )

    def test_empty_list_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = []
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria"
        )

    def test_list_with_non_string_element_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = ["ok", 3]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria[1]"
        )

    def test_list_with_empty_string_element_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = ["ok", ""]
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria[1]"
        )

    def test_integer_form_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = 5
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria"
        )

    def test_none_form_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = None
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria"
        )

    def test_dict_form_rejected(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["gateCriteria"] = {"passed": True}
        self._assert_rejected(
            snapshot, "planning_structure_invalid", "$.milestones[0].gateCriteria"
        )


class GovernanceStatusTests(_ValidatorTestCase):
    def test_unknown_plan_status_is_preserved_verbatim(self):
        snapshot = _valid_snapshot()
        snapshot["planStatus"] = "review-pending"
        snapshot["businessLines"][0]["planStatus"] = "待补充"
        snapshot["milestones"][0]["planStatus"] = "source-only"
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        payload = result["payload"]
        self.assertEqual(payload["planStatus"], "review-pending")
        self.assertEqual(payload["businessLines"][0]["planStatus"], "待补充")
        self.assertEqual(payload["milestones"][0]["planStatus"], "source-only")

    def test_active_and_validated_do_not_create_execution_facts(self):
        snapshot = _valid_snapshot()
        snapshot["planStatus"] = "active"
        snapshot["businessLines"][0]["planStatus"] = "active"
        snapshot["milestones"][0]["planStatus"] = "validated"
        result = self._validate(snapshot)
        self.assertIs(result["wouldActivate"], False)
        self.assertIs(result["wouldDispatch"], False)
        self.assertIs(result["wouldWrite"], False)
        self.assertEqual(result["payload"]["planStatus"], "active")
        self.assertEqual(result["payload"]["milestones"][0]["planStatus"], "validated")
        for forbidden in ("activation", "activated", "executionStatus", "gatePassed",
                          "approved", "accepted", "taskId", "reviewId"):
            self.assertNotIn(forbidden, result)
            self.assertNotIn(forbidden, result["payload"])

    def test_nested_execution_fact_is_rejected_by_frozen_digest(self):
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["taskId"] = "task-1"
        self._assert_rejected(
            snapshot, "planning_execution_fact_forbidden", "$.sections[0].taskId"
        )

    def test_nested_self_reference_is_rejected_by_frozen_digest(self):
        snapshot = _valid_snapshot()
        snapshot["milestones"][0]["canonicalDigest"] = "sha256:" + "d" * 64
        self._assert_rejected(
            snapshot, "planning_digest_self_reference",
            "$.milestones[0].canonicalDigest",
        )

    def test_unresolved_is_preserved_and_not_validated_by_this_card(self):
        snapshot = _valid_snapshot()
        snapshot["unresolved"] = [
            {"note": "未归类章节", "rawSection": "附录", "planStatus": "unknown-xyz"}
        ]
        result = self._validate(snapshot)
        self.assertIs(result["valid"], True)
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(
            result["payload"]["unresolved"], snapshot["unresolved"]
        )


class DigestModuleFreezeTests(unittest.TestCase):
    """冻结摘要模块的只读复用核对（不修改 digest、不复制摘要逻辑）。

    限制如实披露：本环境 ``bashCommands=[]``，无法离线计算并固化
    ``contracts/planning_digest.py`` 的**期望字节 SHA-256 常量**，因此这里不做
    「精确等于某个硬编码 SHA」的断言（避免编造期望值）；改为断言：
    模块字节在本次运行内稳定且 SHA 形态合法、摘要 API 清单与 schemaVersion 冻结、
    并直接用冻结 golden 字面量验证摘要行为未被弱化。字节 SHA 由 cox 在可执行环境
    独立核对。
    """

    _EXPECTED_API = ("PlanningDigestError", "digest_payload", "compute_canonical_digest")
    _GOLDEN_FILE_SHA256 = "sha256:" + "a" * 64
    GOLDEN_CANONICAL_JSON = (
        '{"businessLines":[],"milestones":[],"planStatus":"planning",'
        '"planningSourceId":"src-001","projectId":"proj-alpha",'
        '"schemaVersion":"planning-snapshot.v1",'
        '"sections":[{"content":"初版计划","sectionId":"s1","title":"背景"}],'
        '"sourceFile":{"bytes":1234,"documentVersion":"v1.0","name":"plan.md",'
        '"path":"/data/planning/plan.md","sha256":"' + _GOLDEN_FILE_SHA256 + '"},'
        '"sourceKind":"product","stages":["draft"],"unresolved":[]}'
    )
    GOLDEN_EXPECTED_DIGEST = (
        "sha256:a7668f3102ddb7ab9e515c5815e63ab16d20d2a31ae75aa9df38a1aa53f650ae"
    )

    def _module_path(self):
        return os.path.join(_CANDIDATE_ROOT, "contracts", "planning_digest.py")

    def _golden_probe_payload(self):
        return {
            "schemaVersion": "planning-snapshot.v1",
            "projectId": "proj-alpha",
            "planningSourceId": "src-001",
            "sourceKind": "product",
            "sourceFile": {
                "name": "plan.md",
                "path": "/data/planning/plan.md",
                "documentVersion": "v1.0",
                "sha256": self._GOLDEN_FILE_SHA256,
                "bytes": 1234,
            },
            "sections": [{"sectionId": "s1", "title": "背景", "content": "初版计划"}],
            "stages": ["draft"],
            "businessLines": [],
            "milestones": [],
            "planStatus": "planning",
            "unresolved": [],
        }

    def test_module_file_digest_is_wellformed_and_stable(self):
        with open(self._module_path(), "rb") as handle:
            first = handle.read()
        with open(self._module_path(), "rb") as handle:
            second = handle.read()
        self.assertGreater(len(first), 0)
        digest = hashlib.sha256(first).hexdigest()
        self.assertEqual(len(digest), 64)
        self.assertEqual(digest, digest.lower())
        self.assertTrue(all(char in "0123456789abcdef" for char in digest))
        self.assertEqual(digest, hashlib.sha256(second).hexdigest())

    def test_frozen_digest_api_surface_unchanged(self):
        from contracts import planning_digest

        self.assertEqual(tuple(planning_digest.__all__), self._EXPECTED_API)
        self.assertEqual(planning_digest.SCHEMA_VERSION, "planning-snapshot.v1")

    def test_frozen_golden_literal_still_matches_frozen_digest(self):
        # 独立字面量：golden JSON 必须与探针载荷一致，且其字节 SHA 等于固定摘要。
        self.assertEqual(json.loads(self.GOLDEN_CANONICAL_JSON), self._golden_probe_payload())
        recomputed = "sha256:" + hashlib.sha256(
            self.GOLDEN_CANONICAL_JSON.encode("utf-8")
        ).hexdigest()
        self.assertEqual(recomputed, self.GOLDEN_EXPECTED_DIGEST)
        # 复用函数仍精确产出该固定摘要 → 摘要 behavior 未被弱化。
        self.assertEqual(
            compute_canonical_digest(self._golden_probe_payload()),
            self.GOLDEN_EXPECTED_DIGEST,
        )


class ErrorContractTests(_ValidatorTestCase):
    def test_error_is_value_error_subclass_with_path_only_details(self):
        self.assertTrue(issubclass(PlanningDigestError, ValueError))
        snapshot = _valid_snapshot()
        snapshot["sections"][0]["title"] = ""
        with self.assertRaises(PlanningDigestError) as ctx:
            self._validate(snapshot)
        error = ctx.exception
        self.assertEqual(error.code, "planning_structure_invalid")
        self.assertEqual(set(error.details), {"path"})
        # 不回显被拒正文 / 标题内容。
        self.assertNotIn("背景", repr(error.details))
        self.assertNotIn("背景", str(error))


if __name__ == "__main__":
    unittest.main()
