"""service/planning_preflight.py 的只读预检测试（候选）。

覆盖（对齐任务卡 acceptance[2]/[4]）：
- 复用受控读取：断言直接复用 ``contracts.document_io.read_source_file``；
- 越界（允许根外）、相对路径、``..``、符号链接、凭据目录、不可读缺失文件、大小超限；
- 待补充占位符；
- 同一文件两次读取一致；读取期间字节漂移（回读比对）；与已登记期望摘要不一致；
- 汇总结果显式 ``wouldWrite`` / ``wouldImport`` / ``wouldActivate`` 为 False，
  且不出现 snapshotId / revision 与 task / attempt / delivery / review 事实键；
- 预检是只读的：来源文件内容与 mtime 不变。

合成数据全部落在本测试目录下的临时目录内（受 writeScope 约束），不留残余。
运行（卡内唯一允许的精确入口，从 workspace 根目录执行）：
``python3 -m unittest discover -s tests -p test_planning_preflight.py``
"""

import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
import unittest.mock

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSPACE_ROOT = os.path.dirname(TESTS_DIR)


def _resolve_contracts_root():
    override = os.environ.get("D4_CONTRACTS_ROOT")
    probe = os.path.join("contracts", "document_io.py")
    if isinstance(override, str) and override.strip():
        if os.path.isfile(os.path.join(override, probe)):
            return override
    current = WORKSPACE_ROOT
    for _ in range(12):
        candidate = os.path.join(current, "shared", "agent-orchestrator")
        if os.path.isfile(os.path.join(candidate, probe)):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


CONTRACTS_ROOT = _resolve_contracts_root()

if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)
if CONTRACTS_ROOT and CONTRACTS_ROOT not in sys.path:
    sys.path.insert(0, CONTRACTS_ROOT)


def _load_candidate_module():
    """优先按包名导入；若 ``service`` 名称被受控契约目录的同名包遮蔽，则按文件路径加载。"""
    candidate = os.path.join(WORKSPACE_ROOT, "service", "planning_preflight.py")
    try:
        import service.planning_preflight as module
        if os.path.abspath(module.__file__) == os.path.abspath(candidate):
            return module
    except ImportError:
        pass
    spec = importlib.util.spec_from_file_location("planning_preflight_candidate", candidate)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


planning_preflight = _load_candidate_module()

FORBIDDEN_FACT_KEYS = frozenset((
    "snapshotId", "snapshotRevision", "revision", "canonicalDigest",
    "taskId", "attemptId", "deliveryId", "reviewId", "eventId",
    "runtimeTaskId", "approved", "accepted", "acceptedByHost", "importedAt", "importedBy",
))


def _collect_keys(value, out):
    if isinstance(value, dict):
        for key, sub in value.items():
            if isinstance(key, str):
                out.add(key)
            _collect_keys(sub, out)
    elif isinstance(value, list):
        for sub in value:
            _collect_keys(sub, out)
    return out


class _FixtureCase(unittest.TestCase):
    """统一的临时目录与合成文件工具；所有临时目录都建在 tests/ 之下。"""

    def setUp(self):
        self._dirs = []
        self.root = self._new_dir()
        self.roots = [self.root]

    def tearDown(self):
        for handle in self._dirs:
            handle.cleanup()

    def _new_dir(self):
        handle = tempfile.TemporaryDirectory(dir=TESTS_DIR)
        self._dirs.append(handle)
        return handle.name

    def _write(self, name, text, encoding="utf-8"):
        path = os.path.join(self.root, name)
        parent = os.path.dirname(path)
        if not os.path.isdir(parent):
            os.makedirs(parent)
        with open(path, "wb") as handle:
            handle.write(text.encode(encoding) if isinstance(text, str) else text)
        return path


class ReuseAndReadableTests(_FixtureCase):
    def setUp(self):
        super(ReuseAndReadableTests, self).setUp()
        self.source = self._write("planning-snapshot.md", "# 规划来源\n内容 A\n")

    def test_reuses_contracts_document_io_reader(self):
        """复用而非重写：必须是同一个受控读取函数对象。"""
        self.assertIsNotNone(CONTRACTS_ROOT, "未找到受控契约目录，无法证明复用")
        from contracts import document_io
        self.assertIs(planning_preflight.read_source_file, document_io.read_source_file)
        self.assertIs(planning_preflight.bytes_digest, document_io.bytes_digest)

    def test_readable_records_actual_path_bytes_and_sha256(self):
        item = {"pointer": "sources[0]", "role": "planning-source", "path": self.source}
        result = planning_preflight.preflight_source(item, self.roots)

        self.assertEqual("read", result["status"])
        self.assertIsNone(result["reason"])
        self.assertEqual(os.path.realpath(self.source), result["absolutePath"])
        self.assertEqual(os.path.getsize(self.source), result["bytes"])
        with open(self.source, "rb") as handle:
            raw = handle.read()
        self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(), result["sha256"])
        self.assertIs(True, result["stable"])
        self.assertIs(True, result["regularFile"])
        self.assertIsNone(result["drift"])

    def test_preflight_is_read_only(self):
        stat_before = os.stat(self.source)
        with open(self.source, "rb") as handle:
            content_before = handle.read()
        planning_preflight.preflight_source({"path": self.source}, self.roots)
        planning_preflight.preflight_sources([{"path": self.source}], self.roots)
        with open(self.source, "rb") as handle:
            content_after = handle.read()
        stat_after = os.stat(self.source)
        self.assertEqual(content_before, content_after)
        self.assertEqual(stat_before.st_mtime_ns, stat_after.st_mtime_ns)
        self.assertEqual(stat_before.st_size, stat_after.st_size)

    def test_same_file_read_twice_is_consistent(self):
        first = planning_preflight.preflight_source({"path": self.source}, self.roots)
        second = planning_preflight.preflight_source({"path": self.source}, self.roots)

        self.assertEqual("read", first["status"])
        self.assertEqual("read", second["status"])
        self.assertEqual(first["bytes"], second["bytes"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["absolutePath"], second["absolutePath"])

        comparison = planning_preflight.compare_reads(first, second)
        self.assertIs(True, comparison["same"])
        self.assertEqual([], comparison["changedFields"])

    def test_batch_digest_is_deterministic(self):
        sources = [
            {"pointer": "sources[0]", "role": "planning-source", "path": self.source},
            {"pointer": "sources[1]", "role": "annex", "path": self._write("annex.md", "附注\n")},
        ]
        first = planning_preflight.preflight_sources(sources, self.roots)
        second = planning_preflight.preflight_sources(sources, self.roots)
        self.assertEqual(first["planningSourcePreflight"]["digest"],
                         second["planningSourcePreflight"]["digest"])
        self.assertIs(True, first["planningSourcePreflight"]["allReadable"])


class DriftTests(_FixtureCase):
    def setUp(self):
        super(DriftTests, self).setUp()
        self.source = self._write("planning-snapshot.md", "原始内容\n")

    def test_byte_drift_during_read_is_detected(self):
        """回读比对发现字节变化：状态 drift，保留原始 bytes / sha256 供追溯。"""
        baseline = planning_preflight.preflight_source({"path": self.source}, self.roots)
        self.assertEqual("read", baseline["status"])

        tampered = "被改字节".encode("utf-8")
        with unittest.mock.patch.object(planning_preflight, "_readback", return_value=tampered):
            result = planning_preflight.preflight_source({"path": self.source}, self.roots)

        self.assertEqual("drift", result["status"])
        self.assertEqual("planning_source_drift", result["reason"])
        self.assertIs(False, result["stable"])
        self.assertEqual(baseline["sha256"], result["sha256"])
        drift = result["drift"]
        self.assertEqual(baseline["bytes"], drift["beforeBytes"])
        self.assertEqual(baseline["sha256"], drift["beforeSha256"])
        self.assertNotEqual(drift["beforeSha256"], drift["afterSha256"])
        self.assertEqual(len(tampered), drift["afterBytes"])
        self.assertEqual(planning_preflight.bytes_digest(tampered), drift["afterSha256"])

    def test_readback_failure_is_drift(self):
        with unittest.mock.patch.object(
            planning_preflight, "_readback", side_effect=OSError("readback failed")
        ):
            result = planning_preflight.preflight_source({"path": self.source}, self.roots)
        self.assertEqual("drift", result["status"])
        self.assertEqual("planning_source_drift", result["reason"])
        self.assertEqual("OSError", result["drift"]["readbackError"])

    def test_expected_digest_mismatch_is_drift(self):
        result = planning_preflight.preflight_source(
            {"path": self.source}, self.roots,
            expected_sha256="sha256:" + "0" * 64,
        )
        self.assertEqual("drift", result["status"])
        self.assertEqual("planning_source_mismatch", result["reason"])
        self.assertEqual(["sha256"], result["drift"]["mismatch"])
        self.assertEqual("sha256:" + "0" * 64, result["drift"]["expected"]["sha256"])

    def test_expected_bytes_match_is_still_readable(self):
        size = os.path.getsize(self.source)
        with open(self.source, "rb") as handle:
            digest = planning_preflight.bytes_digest(handle.read())
        result = planning_preflight.preflight_source(
            {"path": self.source}, self.roots,
            expected_bytes=size, expected_sha256=digest,
        )
        self.assertEqual("read", result["status"])
        self.assertEqual([], result["drift"] or [])


class PathAndPermissionTests(_FixtureCase):
    def setUp(self):
        super(PathAndPermissionTests, self).setUp()
        self.outside = self._new_dir()

    def test_placeholder_is_pending(self):
        for claimed in ("<planning-source>", "{{source_path}}", "TODO", "待补充"):
            result = planning_preflight.preflight_source({"path": claimed}, self.roots)
            self.assertEqual("pending", result["status"], claimed)
            self.assertEqual("template_placeholder", result["reason"], claimed)
            self.assertIsNone(result["sha256"], claimed)

    def test_relative_path_is_denied(self):
        result = planning_preflight.preflight_source({"path": "docs/plan.md"}, self.roots)
        self.assertEqual("rejected", result["status"])
        self.assertEqual("path_not_absolute", result["reason"])

    def test_dotdot_escape_is_denied(self):
        claimed = os.path.join(self.root, "..", "outside.md")
        result = planning_preflight.preflight_source({"path": claimed}, self.roots)
        self.assertEqual("rejected", result["status"])
        self.assertEqual("path_dotdot_rejected", result["reason"])

    def test_outside_allowed_roots_is_out_of_scope(self):
        outside_file = os.path.join(self.outside, "elsewhere.md")
        with open(outside_file, "wb") as handle:
            handle.write(b"outside\n")
        result = planning_preflight.preflight_source({"path": outside_file}, self.roots)
        self.assertEqual("out_of_scope", result["status"])
        self.assertEqual("path_escape", result["reason"])
        self.assertIsNone(result["sha256"])

    def test_symlink_is_denied(self):
        target = self._write("target.md", "target\n")
        link = os.path.join(self.root, "link.md")
        os.symlink(target, link)
        result = planning_preflight.preflight_source({"path": link}, self.roots)
        self.assertEqual("rejected", result["status"])
        self.assertEqual("path_symlink_rejected", result["reason"])

    def test_credential_directory_is_denied(self):
        secret = self._write(os.path.join(".ssh", "id_rsa"), "PRIVATE\n")
        result = planning_preflight.preflight_source({"path": secret}, self.roots)
        self.assertEqual("rejected", result["status"])
        self.assertEqual("credential_or_private_source_denied", result["reason"])
        self.assertIsNone(result["sha256"])

    def test_missing_file_is_unreadable(self):
        result = planning_preflight.preflight_source(
            {"path": os.path.join(self.root, "missing.md")}, self.roots
        )
        self.assertEqual("unreadable", result["status"])
        self.assertEqual("document_not_file", result["reason"])
        self.assertIsNone(result["sha256"])

    def test_directory_is_unreadable(self):
        result = planning_preflight.preflight_source({"path": self.root}, self.roots)
        self.assertEqual("unreadable", result["status"])
        self.assertEqual("document_not_file", result["reason"])

    def test_oversize_file_is_unreadable_with_bytes_recorded(self):
        big = self._write("big.md", "x" * 4096)
        result = planning_preflight.preflight_source({"path": big}, self.roots, max_bytes=1024)
        self.assertEqual("unreadable", result["status"])
        self.assertEqual("document_too_large", result["reason"])
        self.assertEqual(4096, result["bytes"])
        self.assertIsNone(result["sha256"])

    def test_non_utf8_file_is_unreadable(self):
        bad = os.path.join(self.root, "bad.md")
        with open(bad, "wb") as handle:
            handle.write(b"\xff\xfe\x00\x01")
        result = planning_preflight.preflight_source({"path": bad}, self.roots)
        self.assertEqual("unreadable", result["status"])
        self.assertEqual("document_encoding", result["reason"])

    def test_missing_path_field_is_unreadable_without_read(self):
        result = planning_preflight.preflight_source({"pointer": "sources[9]"}, self.roots)
        self.assertEqual("unreadable", result["status"])
        self.assertEqual("source_path_invalid", result["reason"])

    def test_all_statuses_are_from_frozen_vocabulary(self):
        covered = set()
        samples = (
            {"path": "<placeholder>"},
            {"path": "relative.md"},
            {"path": os.path.join(self.root, "..", "escape.md")},
            {"path": os.path.join(self.outside, "x.md")},
            {"path": os.path.join(self.root, ".aws", "credentials")},
            {"path": os.path.join(self.root, "missing.md")},
        )
        for sample in samples:
            result = planning_preflight.preflight_source(sample, self.roots)
            self.assertIn(result["status"], planning_preflight.PLANNING_SOURCE_STATUSES)
            covered.add(result["status"])
        self.assertEqual({"pending", "rejected", "out_of_scope", "unreadable"}, covered)


class ConfigurationAndAggregateTests(_FixtureCase):
    def setUp(self):
        super(ConfigurationAndAggregateTests, self).setUp()
        self.good = self._write("planning-snapshot.md", "# 规划来源\n")

    def test_missing_or_relative_allowed_roots_fail_closed(self):
        for roots in (None, [], ["relative/root"], [""], [123]):
            with self.assertRaises(planning_preflight.PlanningPreflightError) as ctx:
                planning_preflight.preflight_sources([{"path": self.good}], roots)
            self.assertEqual("planning_preflight_not_configured", ctx.exception.code)

    def test_aggregate_declares_no_write_import_or_activation(self):
        sources = [
            {"pointer": "sources[0]", "role": "planning-source", "path": self.good},
            {"pointer": "sources[1]", "role": "annex", "path": os.path.join(self.root, "nope.md")},
        ]
        result = planning_preflight.preflight_sources(sources, self.roots)

        self.assertIs(False, result["wouldWrite"])
        self.assertIs(False, result["wouldImport"])
        self.assertIs(False, result["wouldActivate"])
        for flag in result["sideEffects"].values():
            self.assertIs(False, flag)
        self.assertEqual([], result["generatedIdentifiers"])
        self.assertEqual([], result["executionFacts"])
        self.assertIs(False, result["applicable"])
        self.assertIn("sources_unreadable", result["notApplicableReasons"])
        self.assertEqual(1, result["counts"]["read"])
        self.assertEqual(1, result["counts"]["blocked"])

    def test_no_snapshot_id_or_revision_or_execution_fact_keys(self):
        sources = [
            {"pointer": "sources[0]", "role": "planning-source", "path": self.good},
            {"path": "<pending-source>"},
            {"path": "relative.md"},
        ]
        with unittest.mock.patch.object(
            planning_preflight, "_readback", return_value=b"drifted"
        ):
            result = planning_preflight.preflight_sources(sources, self.roots)

        keys = _collect_keys(result, set())
        self.assertEqual(set(), keys & FORBIDDEN_FACT_KEYS, sorted(keys & FORBIDDEN_FACT_KEYS))
        self.assertIs(True, result["planningSourcePreflight"]["driftDetected"])
        self.assertIn("sources_drifted", result["notApplicableReasons"])

    def test_empty_source_list_is_not_applicable(self):
        result = planning_preflight.preflight_sources([], self.roots)
        self.assertEqual(["no_declared_sources"], result["notApplicableReasons"])
        self.assertIs(False, result["applicable"])
        self.assertIs(True, result["planningSourcePreflight"]["allReadable"])
        self.assertEqual(0, result["counts"]["declared"])

    def test_bad_max_bytes_and_bad_item_fail_closed(self):
        with self.assertRaises(planning_preflight.PlanningPreflightError):
            planning_preflight.preflight_source({"path": self.good}, self.roots, max_bytes=0)
        with self.assertRaises(planning_preflight.PlanningPreflightError):
            planning_preflight.preflight_source({"path": self.good}, self.roots, max_bytes=True)
        with self.assertRaises(planning_preflight.PlanningPreflightError):
            planning_preflight.preflight_source("not-an-object", self.roots)
        with self.assertRaises(planning_preflight.PlanningPreflightError):
            planning_preflight.preflight_sources("not-a-list", self.roots)


class CompareReadsTests(_FixtureCase):
    def test_compare_reads_reports_changed_fields(self):
        before = {
            "absolutePath": "/tmp/a.md", "bytes": 10, "sha256": "sha256:" + "a" * 64,
        }
        after = {
            "absolutePath": "/tmp/a.md", "bytes": 11, "sha256": "sha256:" + "b" * 64,
        }
        comparison = planning_preflight.compare_reads(before, after)
        self.assertIs(False, comparison["same"])
        self.assertEqual(["bytes", "sha256"], comparison["changedFields"])
        self.assertEqual("sha256:" + "a" * 64, comparison["before"]["sha256"])
        self.assertEqual("sha256:" + "b" * 64, comparison["after"]["sha256"])


if __name__ == "__main__":
    unittest.main()
