"""D4-MVP20-032 候选测试：规划来源版本核验（只修测试加载：加载前冻结依赖 SHA 门 + 显式 spec 加载）。

被测文件（本轮实现）：``workspace/service/planning_source_version_validate.py``

本文件相对 031 的唯一改动维度 —— **测试加载方式**（031 冻结的 65 项用例与断言逐字保留，
行为口径一字未动）：
- 031 缺陷（cox 探针 ``FAIL_LOADER_REACHED_BEFORE_SHA_REJECTION``）：冻结依赖 ``sha256``
  不一致时只把 mismatch 记进报告，**仍然继续做 spec 加载**，属于「先加载后核验」的顺序缺陷。
- 032 修复：新增模块级**加载前 SHA 门** ``_require_frozen_baseline_intact`` —— 固定清单绑定、
  冻结文件全量只读对拍、本轮实现摘要核验全部通过之后，才允许注入路径与执行
  ``importlib.util.spec_from_file_location`` 加载；任一摘要不一致直接 ``ImportError``，
  绝不进入加载阶段，无重试、无 fallback、不读任何环境覆盖、不向上搜索候选根。
- 冻结基线只从固定路径 ``workspace/inputs/baseline-manifest.json`` 读取：``formalRoot``
  与每个冻结文件的 ``sha256`` 都取自该清单；加载阶段不读取任何环境变量、不向父目录
  向上搜索、没有任何 fallback 分支（清单缺失 / 形态不合法即 fail closed 报错）。
- 本轮实现用 ``importlib.util.spec_from_file_location`` **显式按文件路径**加载（不靠
  ``sys.path`` 隐式 import），同时把正式实现
  ``<formalRoot>/service/planning_source_version_validate.py`` 也按文件路径显式加载，
  保证本文件被接纳进正式根之后仍然加载正式实现（同源同摘要须一致）。
- 031 行为用例逐字保留（45 保留 + 12 新增 = 57），不 skip、不放宽任何断言；本轮只在
  加载路径上加围栏，不触碰任何行为口径，也不修改产品实现（实现 SHA 保持
  ``candidateSha256`` 不变）。

本文件结构：
- ``DeclarationExplicitnessTests`` / ``PathPolicyTests`` / ``DriftTests`` /
  ``VersionComparisonTests`` / ``ZeroSideEffectTests`` /
  ``SameVersionConflictPriorityTests`` / ``FrozenDependencyTests``：031 冻结的 57 项行为用例，
  逐字保留（含 cox 独立探针的等价用例与冻结依赖身份断言）；
- ``FrozenBaselineBindingTests``：031 冻结的 8 项加载围栏（固定清单路径、无环境覆盖 /
  无向上搜索 / 无 fallback、spec 显式加载证明、本轮实现与正式实现同摘要、冻结基线全量
  SHA 对拍、57 项行为用例计数围栏），逐字保留；
- ``PreloadShaGateTests``：032 新增加载前 SHA 门的围栏（7 项）—— 模块级加载顺序自证、门函数对
  冻结依赖 / 本轮实现摘要不一致的 fail closed、篡改依赖摘要后「加载器不可达」的实证、固定
  清单缺失时的 fail closed、门函数无 fallback 分支、以及 031 冻结 65 项用例名逐条不变的清单围栏。

只使用标准库（``hashlib`` / ``importlib.machinery`` / ``importlib.util`` / ``inspect`` /
``json`` / ``os`` / ``sys`` / ``tempfile`` / ``unittest`` / ``unittest.mock``）；
测试自身在临时目录内构造合成来源文件 / 合成被篡改清单，不写任何真实业务数据、不建库、
不联网、不调宿主。

⚠️ 状态：Worker 本轮 bashCommands 为空，**未运行**任何测试；本文件由 cox 独立执行并判定。
031 冻结的 65 项用例（7 个行为类 57 项 + 加载围栏类 8 项）与本轮新增的 7 项加载门用例，
作者均**未运行**，结果 **NOT_RUN**；本文件共 72 个用例（65 逐字保留 + 7 新增）。
"""

import hashlib
import importlib.machinery
import importlib.util
import inspect
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_WORKSPACE = os.path.dirname(_HERE)
_SERVICE_DIR = os.path.join(_WORKSPACE, "service")

#: 本轮实现（被测文件）与正式实现的固定相对位置。
_MODULE_PATH = os.path.join(_SERVICE_DIR, "planning_source_version_validate.py")
_FORMAL_MODULE_RELATIVE = os.path.join("service", "planning_source_version_validate.py")

#: 冻结基线的唯一来源：固定相对路径的清单文件（不查环境变量、不向上搜索、无 fallback）。
_BASELINE_MANIFEST_PATH = os.path.join(_WORKSPACE, "inputs", "baseline-manifest.json")

#: 加载前 SHA 门的稳定错误标记（只用于自证「拒绝发生在加载之前」）。
_FROZEN_BASELINE_SHA_MISMATCH = "FROZEN_BASELINE_SHA_MISMATCH"

#: 模块级加载顺序自证（只读记录）：SHA 门必须先于任何 spec 加载。
_LOAD_SEQUENCE = []


# ---------------------------------------------------------------------------
# 冻结基线绑定：固定清单 -> formalRoot -> 全量只读 SHA 对拍（加载前完成，无兜底）
# ---------------------------------------------------------------------------


def _file_sha256(path):
    """只读计算文件 sha256（64 位小写十六进制，不带前缀）。"""
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def _load_baseline_manifest():
    """从固定清单读冻结基线；清单缺失或形态不合法一律直接失败，不做任何兜底。

    这里刻意只认固定路径：既不读环境变量覆盖，也不向上级目录搜索候选根，也不在失败后
    退回任何默认值 —— 避免「测试环境覆盖 / 父目录搜索」再次绕过来源卡冻结的输入。
    """
    if not os.path.isfile(_BASELINE_MANIFEST_PATH):
        raise ImportError(
            "缺少冻结基线清单（固定路径）：%s；本测试不做环境覆盖、不做向上搜索、无 fallback"
            % _BASELINE_MANIFEST_PATH
        )
    with open(_BASELINE_MANIFEST_PATH, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise ImportError("冻结基线清单必须是 JSON 对象：%s" % _BASELINE_MANIFEST_PATH)

    formal_root = manifest.get("formalRoot")
    if not isinstance(formal_root, str) or not formal_root.startswith("/"):
        raise ImportError("冻结基线清单 formalRoot 必须是非空绝对路径字符串")
    if not os.path.isdir(formal_root):
        raise ImportError("冻结基线清单 formalRoot 不是目录：%r" % (formal_root,))

    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise ImportError("冻结基线清单 files 必须是非空对象（相对路径 -> sha256）")
    for relative, digest in files.items():
        if (
            not isinstance(relative, str)
            or not relative
            or relative.startswith("/")
            or ".." in relative.split("/")
        ):
            raise ImportError("冻结基线清单 files 的相对路径非法：%r" % (relative,))
        if not isinstance(digest, str) or len(digest) != 64:
            raise ImportError("冻结基线清单 files 的 sha256 形态非法：%r" % (relative,))
    if _FORMAL_MODULE_RELATIVE not in files:
        raise ImportError(
            "冻结基线清单必须列出本轮实现 %s" % _FORMAL_MODULE_RELATIVE
        )

    candidate_sha256 = manifest.get("candidateSha256")
    if not isinstance(candidate_sha256, str) or len(candidate_sha256) != 64:
        raise ImportError("冻结基线清单 candidateSha256 形态非法（须为 64 位十六进制）")
    return manifest


_MANIFEST = _load_baseline_manifest()
_LOAD_SEQUENCE.append("baseline_manifest_loaded")
_FORMAL_ROOT = _MANIFEST["formalRoot"]
_FORMAL_IMPL_PATH = os.path.join(_FORMAL_ROOT, _FORMAL_MODULE_RELATIVE)
_CANDIDATE_SHA256 = _MANIFEST["candidateSha256"]
#: 本轮实现的实际摘要（只读计算，不加载任何模块）。
_ROUND_IMPLEMENTATION_DIGEST = _file_sha256(_MODULE_PATH)


def _audit_frozen_baseline():
    """对清单内每个冻结文件做只读 SHA 对拍；不写任何文件，缺失即记 mismatch。"""
    report = {}
    for relative in sorted(_MANIFEST["files"]):
        expected = _MANIFEST["files"][relative]
        path = os.path.join(_FORMAL_ROOT, relative)
        exists = os.path.isfile(path)
        actual = _file_sha256(path) if exists else None
        report[relative] = {
            "path": path,
            "expected": expected,
            "exists": exists,
            "actual": actual,
            "match": exists and actual == expected,
        }
    return report


_BASELINE_DIGEST_REPORT = _audit_frozen_baseline()
_BASELINE_MISMATCHES = sorted(
    relative for relative, item in _BASELINE_DIGEST_REPORT.items() if not item["match"]
)
_LOAD_SEQUENCE.append("frozen_baseline_sha_audited")


def _require_frozen_baseline_intact(manifest, digest_report, round_implementation_digest,
                                    expected_candidate_sha256):
    """加载前 SHA 门：摘要不一致一律 ``ImportError``，且必须在任何 spec 加载之前调用。

    - 只使用调用方已算好的**只读**摘要：本函数不重算、不捕获、不重试、没有 fallback；
    - 冻结依赖摘要与固定清单不一致（或对拍未覆盖清单的文件集）-> 拒绝；
    - 本轮实现摘要与 ``candidateSha256`` 不一致 -> 同样拒绝；
    - 拒绝发生在加载**之前**，因此不存在「依赖 SHA 错误仍进入 spec 加载」的顺序缺陷
      （031 缺陷：只记录 mismatch，仍继续加载）。

    通过时返回 ``True``；失败时抛出的 ``ImportError`` 文本以
    ``_FROZEN_BASELINE_SHA_MISMATCH`` 开头，便于独立审核判定拒绝原因。
    """
    if not isinstance(manifest, dict) or not isinstance(digest_report, dict):
        raise ImportError(
            "%s: 加载前 SHA 门入参不合法（必须给出固定清单与只读对拍报告）"
            % _FROZEN_BASELINE_SHA_MISMATCH
        )

    problems = []
    declared = sorted(manifest.get("files") or {})
    if declared != sorted(digest_report):
        problems.append("frozen baseline audit does not cover the fixed manifest file set")
    mismatched = sorted(
        relative for relative, item in digest_report.items()
        if not isinstance(item, dict) or not item.get("match")
    )
    if mismatched:
        problems.append("frozen dependency sha256 mismatch: %s" % ", ".join(mismatched))
    if round_implementation_digest != expected_candidate_sha256:
        problems.append(
            "round implementation sha256 mismatch: %s" % (round_implementation_digest,)
        )
    if problems:
        raise ImportError(
            "%s: %s" % (_FROZEN_BASELINE_SHA_MISMATCH, "; ".join(problems))
        )
    return True


# 加载前 SHA 门：本轮唯一修复点。任何冻结依赖 / 本轮实现摘要不一致，直接在这里 fail closed，
# 不进入下方任何 spec 加载。
_FROZEN_BASELINE_GATE_PASSED = _require_frozen_baseline_intact(
    _MANIFEST, _BASELINE_DIGEST_REPORT, _ROUND_IMPLEMENTATION_DIGEST, _CANDIDATE_SHA256
)
_LOAD_SEQUENCE.append("preload_sha_gate_passed")

# 只把清单给出的正式根注入 sys.path（供本轮实现的冻结依赖 import 解析）；不注入其它任何
# 路径 —— 本轮实现本身只按文件路径显式加载，不靠 sys.path 隐式 import。
if _FORMAL_ROOT not in sys.path:
    sys.path.insert(0, _FORMAL_ROOT)


_CANDIDATE_MODULE_NAME = "d4_mvp20_032_planning_source_version_validate_round"
_FORMAL_MODULE_NAME = "d4_mvp20_032_planning_source_version_validate_formal"


def _load_module_from_path(module_name, path):
    """显式按文件路径加载模块：只用 importlib.util.spec_from_file_location，不搜索、不 fallback。"""
    if not os.path.isfile(path):
        raise ImportError("显式加载目标文件不存在：%s" % path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError("无法为 %s 构造 import spec" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 本轮实现：显式 spec 加载（被测对象），不复制正式实现、不靠裸模块名 import。
candidate = _load_module_from_path(_CANDIDATE_MODULE_NAME, _MODULE_PATH)
_LOAD_SEQUENCE.append("round_implementation_spec_loaded")
# 正式实现：同样显式 spec 加载；本文件被接纳进正式根后依然加载正式实现。
formal_implementation = _load_module_from_path(_FORMAL_MODULE_NAME, _FORMAL_IMPL_PATH)
_LOAD_SEQUENCE.append("formal_implementation_spec_loaded")


# ---------------------------------------------------------------------------
# 测试辅助（只读 / 只在本测试进程内使用）
# ---------------------------------------------------------------------------


def _write_source(directory, name="planning-source.md", body="# 规划来源\n\n受控来源正文。\n"):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(body)
    return path


def _write_text(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


def _remove_tree(base):
    """清理本测试自建临时目录：只删本进程创建的路径，不触碰任何真实数据。"""
    if not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):
        for filename in filenames:
            target = os.path.join(dirpath, filename)
            if os.path.islink(target):
                os.unlink(target)
            else:
                os.remove(target)
        for dirname in dirnames:
            target = os.path.join(dirpath, dirname)
            if os.path.islink(target):
                os.unlink(target)
            else:
                os.rmdir(target)
    os.rmdir(base)


def _digest(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    return "sha256:" + hashlib.sha256(raw).hexdigest(), len(raw)


def _declaration(path, kind="product", name="planning-source.md", version="D4.5"):
    return {
        "path": path,
        "sourceKind": kind,
        "name": name,
        "documentVersion": version,
    }


class _CandidateTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="d4-030-allowed-")
        self.other_root = tempfile.mkdtemp(prefix="d4-030-other-")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for base in (self.root, self.other_root):
            for dirpath, dirnames, filenames in os.walk(base, topdown=False):
                for filename in filenames:
                    target = os.path.join(dirpath, filename)
                    if os.path.islink(target):
                        os.unlink(target)
                    else:
                        os.remove(target)
                for dirname in dirnames:
                    os.rmdir(os.path.join(dirpath, dirname))
            os.rmdir(base)

    def declare(self, path, **overrides):
        payload = _declaration(path)
        payload.update(overrides)
        return payload

    def assertErrorCode(self, expected, func, *args, **kwargs):
        with self.assertRaises(candidate.PlanningSourceVersionError) as ctx:
            func(*args, **kwargs)
        self.assertEqual(ctx.exception.code, expected)
        return ctx.exception


# ---------------------------------------------------------------------------
# acceptance[1]：显式声明、路径必须在允许根内的普通文件、禁止按标题猜来源
# ---------------------------------------------------------------------------


class DeclarationExplicitnessTests(_CandidateTestCase):
    def test_missing_document_version_key_is_rejected(self):
        path = _write_source(self.root)
        declaration = self.declare(path)
        del declaration["documentVersion"]
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            declaration,
            [self.root],
        )
        self.assertEqual(error.details["reason"], "missing_field")
        self.assertEqual(error.details["path"], "$.registeredSource.documentVersion")

    def test_keyword_form_requires_all_four_fields(self):
        path = _write_source(self.root)
        # 关键字写法缺 documentVersion（根本没传）-> 拒绝，不能猜一个默认版本
        self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            None,
            [self.root],
            path=path,
            sourceKind="product",
            name="planning-source.md",
        )

    def test_explicit_none_version_yields_conclusion_only(self):
        path = _write_source(self.root)
        result = candidate.validate_planning_source_version(
            self.declare(path, documentVersion=None), [self.root]
        )
        self.assertEqual(result["status"], candidate.STATUS_MISSING)
        self.assertFalse(result["versionVerified"])
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_ORDER_3_UNDECLARED)
        self.assertFalse(result["appendCandidateEligible"])
        self.assertIsNone(result["declaredDocumentVersion"])

    def test_mixed_declaration_forms_rejected(self):
        path = _write_source(self.root)
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            self.declare(path),
            [self.root],
            path=path,
        )
        self.assertEqual(error.details["reason"], "mixed_declaration_forms")

    def test_unknown_declaration_field_rejected(self):
        path = _write_source(self.root)
        declaration = self.declare(path)
        declaration["sha256"] = "sha256:" + "0" * 64  # 自报摘要不可信，一律拒绝
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            declaration,
            [self.root],
        )
        self.assertEqual(error.details["reason"], "unknown_field")
        self.assertEqual(error.details["path"], "$.registeredSource.sha256")

    def test_declaration_must_be_object(self):
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            "planning-source.md",
            [self.root],
        )
        self.assertEqual(error.details["reason"], "declaration_not_object")

    def test_source_kind_closed_set(self):
        path = _write_source(self.root)
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            self.declare(path, sourceKind="screenshot"),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "field_type_invalid")
        self.assertIn("product", error.details["allowed"])

    def test_version_type_must_be_string_or_none(self):
        path = _write_source(self.root)
        self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            self.declare(path, documentVersion=3),
            [self.root],
        )

    def test_allowed_roots_required_and_validated(self):
        path = _write_source(self.root)
        declaration = self.declare(path)
        self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            declaration,
            [],
        )
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            declaration,
            ["relative-root"],
        )
        self.assertEqual(error.details["reason"], "allowed_root_invalid")

    def test_no_path_never_falls_back_to_name(self):
        # 只给 name（且 name 恰好是真实文件名）也必须拒绝：禁止按标题 / 文件名猜来源
        _write_source(self.root)
        declaration = self.declare(os.path.join(self.root, "planning-source.md"))
        del declaration["path"]
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            declaration,
            [self.root],
        )
        self.assertEqual(error.details["reason"], "missing_field")
        self.assertEqual(error.details["path"], "$.registeredSource.path")

    def test_declared_name_is_echoed_but_never_used_for_lookup(self):
        path = _write_source(self.root, name="actual-source.md")
        result = candidate.validate_planning_source_version(
            self.declare(path, name="完全不同的标题.md"), [self.root]
        )
        self.assertEqual(result["resolvedFrom"], candidate.RESOLVED_FROM_DECLARED_PATH)
        self.assertFalse(result["nameUsedForLookup"])
        self.assertEqual(result["source"]["declaredName"], "完全不同的标题.md")
        self.assertEqual(result["source"]["fileName"], "actual-source.md")
        self.assertEqual(result["source"]["path"], os.path.realpath(path))


class PathPolicyTests(_CandidateTestCase):
    def test_directory_path_is_unreadable(self):
        error = self.assertErrorCode(
            "planning_source_unreadable",
            candidate.validate_planning_source_version,
            self.declare(self.root),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "document_not_file")

    def test_relative_path_rejected(self):
        error = self.assertErrorCode(
            "planning_source_rejected",
            candidate.validate_planning_source_version,
            self.declare("relative/source.md"),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "path_not_absolute")

    def test_dotdot_escape_rejected(self):
        error = self.assertErrorCode(
            "planning_source_rejected",
            candidate.validate_planning_source_version,
            self.declare(os.path.join(self.root, "..", "escaped.md")),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "path_dotdot_rejected")

    def test_outside_allowed_roots_is_out_of_scope(self):
        path = _write_source(self.other_root)
        error = self.assertErrorCode(
            "planning_source_out_of_scope",
            candidate.validate_planning_source_version,
            self.declare(path),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "path_escape")

    def test_symlink_rejected(self):
        target = _write_source(self.root, name="real-source.md")
        link = os.path.join(self.root, "linked-source.md")
        os.symlink(target, link)
        error = self.assertErrorCode(
            "planning_source_rejected",
            candidate.validate_planning_source_version,
            self.declare(link),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "path_symlink_rejected")

    def test_credential_directory_rejected(self):
        private = os.path.join(self.root, ".ssh")
        os.makedirs(private)
        path = _write_source(private, name="source.md")
        error = self.assertErrorCode(
            "planning_source_rejected",
            candidate.validate_planning_source_version,
            self.declare(path),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "credential_or_private_source_denied")

    def test_missing_file_is_unreadable(self):
        error = self.assertErrorCode(
            "planning_source_unreadable",
            candidate.validate_planning_source_version,
            self.declare(os.path.join(self.root, "not-there.md")),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "document_not_file")

    def test_template_placeholder_path_rejected(self):
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            self.declare("<planning-source>"),
            [self.root],
        )
        self.assertEqual(error.details["reason"], "template_placeholder")


# ---------------------------------------------------------------------------
# acceptance[2]：读取期间两次核对 bytes / sha256，变化即 planning_source_drift
# ---------------------------------------------------------------------------


class DriftTests(_CandidateTestCase):
    def _fake_meta(self, path, sha256, bytes_count):
        return {"path": path, "bytes": bytes_count, "sha256": sha256}, ""

    def test_two_identical_reads_are_reported(self):
        path = _write_source(self.root)
        digest, size = _digest(path)
        result = candidate.validate_planning_source_version(self.declare(path), [self.root])
        self.assertEqual(result["reads"]["count"], 2)
        self.assertTrue(result["reads"]["bytesStable"])
        self.assertTrue(result["reads"]["sha256Stable"])
        self.assertEqual(result["reads"]["first"]["bytes"], size)
        self.assertEqual(result["reads"]["first"]["sha256"], digest)
        self.assertEqual(result["reads"]["second"]["sha256"], digest)

    def test_second_read_digest_change_is_drift(self):
        path = _write_source(self.root)
        digest, size = _digest(path)
        changed = "sha256:" + "a" * 64
        # 第一次读由冻结预检完成（真实读取，摘要 = digest）；这里只替换第二次受控复读。
        with mock.patch.object(
            candidate,
            "read_source_file",
            return_value=self._fake_meta(path, changed, size),
        ):
            error = self.assertErrorCode(
                "planning_source_drift",
                candidate.validate_planning_source_version,
                self.declare(path),
                [self.root],
            )
        self.assertEqual(error.code, "planning_source_drift")
        self.assertEqual(error.details["reason"], "bytes_or_sha256_changed_between_reads")
        self.assertEqual(error.details["order"], candidate.CHANGE_ORDER_ORDER_1_DRIFT)
        self.assertEqual(error.details["first"]["sha256"], digest)
        self.assertEqual(error.details["second"]["sha256"], changed)

    def test_second_read_bytes_change_is_drift(self):
        path = _write_source(self.root)
        digest, size = _digest(path)
        with mock.patch.object(
            candidate,
            "read_source_file",
            return_value=self._fake_meta(path, digest, size + 1),
        ):
            self.assertErrorCode(
                "planning_source_drift",
                candidate.validate_planning_source_version,
                self.declare(path),
                [self.root],
            )

    def test_second_read_failure_is_drift(self):
        path = _write_source(self.root)
        failure = candidate.DocumentContractError(
            "document_not_file", details={"path": path}
        )
        with mock.patch.object(candidate, "read_source_file", side_effect=failure):
            error = self.assertErrorCode(
                "planning_source_drift",
                candidate.validate_planning_source_version,
                self.declare(path),
                [self.root],
            )
        self.assertEqual(error.details["reason"], "second_controlled_read_failed")

    def test_preflight_reported_drift_is_drift_and_writes_nothing(self):
        path = _write_source(self.root)
        before = sorted(os.listdir(self.root))
        preflight_drift = {
            "status": "drift",
            "reason": "planning_source_drift",
            "absolutePath": path,
            "bytes": 10,
            "sha256": "sha256:" + "b" * 64,
            "regularFile": True,
            "stable": False,
            "drift": {"beforeBytes": 10, "afterBytes": 11},
        }
        with mock.patch.object(candidate, "preflight_source", return_value=preflight_drift):
            error = self.assertErrorCode(
                "planning_source_drift",
                candidate.validate_planning_source_version,
                self.declare(path),
                [self.root],
            )
        self.assertEqual(error.details["reason"], "planning_source_drift")
        self.assertEqual(sorted(os.listdir(self.root)), before)

    def test_inspect_reports_drift_without_saving_or_appending(self):
        path = _write_source(self.root)
        digest, size = _digest(path)
        with mock.patch.object(
            candidate,
            "read_source_file",
            return_value=self._fake_meta(path, "sha256:" + "c" * 64, size),
        ):
            result = candidate.inspect_planning_source_version(self.declare(path), [self.root])
        self.assertEqual(result["status"], "planning_source_drift")
        self.assertFalse(result["versionVerified"])
        self.assertFalse(result["wouldSave"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertFalse(result["wouldActivate"])
        self.assertEqual(result["generatedIdentifiers"], [])
        self.assertEqual(result["executionFacts"], [])

    def test_inspect_forbids_save_on_conflict(self):
        path = _write_source(self.root)
        result = candidate.inspect_planning_source_version(
            self.declare(path, documentVersion="D4.5"),
            [self.root],
            registered_versions=[
                {"documentVersion": "D4.5", "sha256": "sha256:" + "d" * 64, "bytes": 1}
            ],
        )
        self.assertEqual(result["status"], "planning_snapshot_conflict")
        self.assertFalse(result["wouldSave"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# acceptance[3]：版本不归一化；缺失 / unverified 只给结论；同版本摘要冲突硬拒
# ---------------------------------------------------------------------------


class VersionComparisonTests(_CandidateTestCase):
    def setUp(self):
        super().setUp()
        self.path = _write_source(self.root)
        self.digest, self.size = _digest(self.path)

    def registered(self, version, sha256=None, bytes_count=None):
        return [
            {
                "documentVersion": version,
                "sha256": sha256 if sha256 is not None else self.digest,
                "bytes": bytes_count if bytes_count is not None else self.size,
            }
        ]

    def test_same_version_same_digest_is_matched(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="D4.5"),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertEqual(result["status"], candidate.STATUS_MATCHED)
        self.assertTrue(result["versionVerified"])
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_NONE)
        self.assertFalse(result["appendCandidateEligible"])
        self.assertEqual(result["matchedRegisteredVersion"]["documentVersion"], "D4.5")
        self.assertEqual(result["registeredVersionsChecked"], 1)

    def test_same_version_different_digest_is_hard_conflict(self):
        error = self.assertErrorCode(
            "planning_snapshot_conflict",
            candidate.validate_planning_source_version,
            self.declare(self.path, documentVersion="D4.5"),
            [self.root],
            registered_versions=self.registered("D4.5", sha256="sha256:" + "e" * 64),
        )
        self.assertEqual(
            error.details["reason"], "same_document_version_different_bytes"
        )
        self.assertEqual(error.details["declaredDocumentVersion"], "D4.5")
        self.assertEqual(error.details["registered"]["sha256"], "sha256:" + "e" * 64)
        self.assertEqual(error.details["actual"]["sha256"], self.digest)

    def test_same_version_different_bytes_is_hard_conflict(self):
        self.assertErrorCode(
            "planning_snapshot_conflict",
            candidate.validate_planning_source_version,
            self.declare(self.path, documentVersion="D4.5"),
            [self.root],
            registered_versions=self.registered("D4.5", bytes_count=self.size + 1),
        )

    def test_new_concrete_version_marks_order_two_but_never_appends(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="D4.9"),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertEqual(result["status"], candidate.STATUS_NEW_VERSION)
        self.assertFalse(result["versionVerified"])
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_ORDER_2_NEW_VERSION)
        self.assertTrue(result["appendCandidateEligible"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertFalse(result["sideEffects"]["revisionAppended"])
        self.assertIsNone(result["matchedRegisteredVersion"])

    def test_exact_unverified_literal_only_yields_conclusion(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="unverified"),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertEqual(result["status"], candidate.STATUS_UNVERIFIED)
        self.assertFalse(result["versionVerified"])
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_ORDER_3_UNDECLARED)
        self.assertFalse(result["appendCandidateEligible"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertEqual(
            result["recommendedAction"], "controlled_version_declaration_required"
        )

    def test_unverified_is_not_case_folded(self):
        # registered 行里放的是小写 unverified；声明的 UNVERIFIED 必须按原样字符串处理，
        # 既不算 unverified，也不能命中已登记版本 -> 明确新版本
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="UNVERIFIED"),
            [self.root],
            registered_versions=self.registered("unverified"),
        )
        self.assertEqual(result["status"], candidate.STATUS_NEW_VERSION)
        self.assertNotEqual(result["status"], candidate.STATUS_UNVERIFIED)
        self.assertFalse(result["versionVerified"])
        self.assertFalse(result["documentVersionNormalized"])

    def test_version_is_not_whitespace_trimmed(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion=" D4.5 "),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertEqual(result["status"], candidate.STATUS_NEW_VERSION)
        self.assertEqual(result["declaredDocumentVersion"], " D4.5 ")
        self.assertFalse(result["documentVersionNormalized"])

    def test_version_is_not_unicode_normalized(self):
        composed = "D4.\u00e9"      # é (NFC)
        decomposed = "D4.e\u0301"   # e + 组合重音 (NFD)
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion=decomposed),
            [self.root],
            registered_versions=self.registered(composed),
        )
        self.assertEqual(result["status"], candidate.STATUS_NEW_VERSION)
        self.assertEqual(result["declaredDocumentVersion"], decomposed)

    def test_empty_version_string_is_missing(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion=""),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertEqual(result["status"], candidate.STATUS_MISSING)
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_ORDER_3_UNDECLARED)

    def test_missing_version_without_history_never_auto_appends(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion=None), [self.root]
        )
        self.assertEqual(result["registeredVersionsChecked"], 0)
        self.assertFalse(result["appendCandidateEligible"])
        self.assertFalse(result["wouldAppendRevision"])

    def test_concrete_version_without_history_is_order_two(self):
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="D4.5"), [self.root]
        )
        self.assertEqual(result["status"], candidate.STATUS_NEW_VERSION)
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_ORDER_2_NEW_VERSION)
        self.assertFalse(result["wouldAppendRevision"])

    def test_registered_row_shape_rejected(self):
        cases = [
            {"documentVersion": "D4.5", "sha256": self.digest},                        # 缺 bytes
            {"documentVersion": "D4.5", "sha256": self.digest, "bytes": self.size, "snapshotId": "snap-1"},
            {"documentVersion": "D4.5", "sha256": "not-a-digest", "bytes": self.size},
            {"documentVersion": "D4.5", "sha256": self.digest, "bytes": True},
            {"documentVersion": "", "sha256": self.digest, "bytes": self.size},
            {"documentVersion": "D4.5", "sha256": self.digest, "bytes": -1},
        ]
        for row in cases:
            with self.subTest(row=row):
                self.assertErrorCode(
                    "planning_source_version_invalid",
                    candidate.validate_planning_source_version,
                    self.declare(self.path, documentVersion="D4.5"),
                    [self.root],
                    registered_versions=[row],
                )

    def test_duplicate_registered_version_rejected(self):
        rows = self.registered("D4.5") + self.registered("D4.5")
        error = self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            self.declare(self.path, documentVersion="D4.5"),
            [self.root],
            registered_versions=rows,
        )
        self.assertEqual(error.details["reason"], "duplicate_document_version")

    def test_registered_versions_must_be_list(self):
        self.assertErrorCode(
            "planning_source_version_invalid",
            candidate.validate_planning_source_version,
            self.declare(self.path, documentVersion="D4.5"),
            [self.root],
            registered_versions={"documentVersion": "D4.5"},
        )


# ---------------------------------------------------------------------------
# acceptance[4]：零数据库写、零快照写、零激活、零执行事实（含源码级围栏）
# ---------------------------------------------------------------------------


class ZeroSideEffectTests(_CandidateTestCase):
    FORBIDDEN_TOKENS = (
        "sqlite3",
        "INSERT",
        "UPDATE",
        "DELETE",
        "COMMIT",
        "ROLLBACK",
        "subprocess",
        "requests",
        "urllib",
        "shutil",
        "os.remove",
        "os.rename",
        "os.unlink",
        "makedirs",
        "mkdir",
        "open(",
    )

    def test_module_source_has_no_write_or_network_primitive(self):
        with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        for token in self.FORBIDDEN_TOKENS:
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_entrypoints_accept_no_database_handle(self):
        for entry in (
            candidate.validate_planning_source_version,
            candidate.inspect_planning_source_version,
        ):
            parameters = inspect.signature(entry).parameters
            self.assertNotIn("connection", parameters)
            self.assertNotIn("database", parameters)
            self.assertNotIn("snapshot", parameters)

    def test_success_result_declares_zero_side_effects_and_no_identifiers(self):
        path = _write_source(self.root)
        result = candidate.validate_planning_source_version(
            self.declare(path), [self.root]
        )
        self.assertFalse(result["wouldSave"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertFalse(result["wouldModifySnapshot"])
        self.assertFalse(result["wouldActivate"])
        self.assertEqual(result["generatedIdentifiers"], [])
        self.assertEqual(result["executionFacts"], [])
        for key, value in result["sideEffects"].items():
            with self.subTest(key=key):
                self.assertFalse(value)
        for forbidden in ("snapshotId", "snapshotRevision", "planningSourceId", "auditId",
                          "taskId", "attemptId", "deliveryId", "reviewId"):
            self.assertNotIn(forbidden, result)

    def test_successful_verification_writes_nothing_to_source_tree(self):
        path = _write_source(self.root, name="source-a.md")
        _write_source(self.root, name="source-b.md")
        before = sorted(os.listdir(self.root))
        before_files = {
            name: _digest(os.path.join(self.root, name)) for name in before
        }
        candidate.validate_planning_source_version(self.declare(path), [self.root])
        candidate.inspect_planning_source_version(self.declare(path), [self.root])
        self.assertEqual(sorted(os.listdir(self.root)), before)
        for name in before:
            with self.subTest(name=name):
                self.assertEqual(_digest(os.path.join(self.root, name)), before_files[name])

    def test_reads_use_controlled_frozen_reader(self):
        # 证明读取复用冻结契约函数对象，而不是本模块自造读取逻辑
        from service import planning_preflight

        self.assertIs(candidate.read_source_file, planning_preflight.read_source_file)
        self.assertIs(candidate.preflight_source, planning_preflight.preflight_source)
        self.assertEqual(
            candidate.DEFAULT_MAX_BYTES, planning_preflight.DEFAULT_MAX_BYTES
        )


# ---------------------------------------------------------------------------
# 030 返工回归：同版本字节 / 摘要冲突优先于 missing / unverified 占位结论
# ---------------------------------------------------------------------------


class SameVersionConflictPriorityTests(_CandidateTestCase):
    """cox 027 轮探针 ``unverified_same_version_byte_conflict`` 的等价回归。

    029 实测 ``status=unverified``（期望 ``planning_snapshot_conflict``）；030 修复后
    同版本冲突核验前移，占位结论（``missing`` / ``unverified``）不再豁免硬冲突。
    """

    def setUp(self):
        super().setUp()
        self.path = _write_source(self.root)
        self.digest, self.size = _digest(self.path)

    def registered(self, version, sha256=None, bytes_count=None):
        return [
            {
                "documentVersion": version,
                "sha256": sha256 if sha256 is not None else self.digest,
                "bytes": bytes_count if bytes_count is not None else self.size,
            }
        ]

    def test_unverified_same_version_byte_conflict_is_hard_conflict(self):
        # cox 探针等价用例：声明 unverified、已登记行同为 unverified、bytes 不同
        error = self.assertErrorCode(
            "planning_snapshot_conflict",
            candidate.validate_planning_source_version,
            self.declare(self.path, documentVersion="unverified"),
            [self.root],
            registered_versions=self.registered("unverified", bytes_count=self.size + 1),
        )
        self.assertEqual(error.details["reason"], "same_document_version_different_bytes")
        self.assertEqual(error.details["declaredDocumentVersion"], "unverified")
        self.assertTrue(error.details["rejectedBeforePlaceholderConclusion"])
        self.assertEqual(error.details["registered"]["documentVersion"], "unverified")
        self.assertEqual(error.details["actual"]["sha256"], self.digest)
        self.assertEqual(error.details["actual"]["bytes"], self.size)

    def test_unverified_same_version_digest_conflict_is_hard_conflict(self):
        # bytes 相同但 sha256 不同，同样是同版本字节冲突，不得被 unverified 放过
        error = self.assertErrorCode(
            "planning_snapshot_conflict",
            candidate.validate_planning_source_version,
            self.declare(self.path, documentVersion="unverified"),
            [self.root],
            registered_versions=self.registered(
                "unverified", sha256="sha256:" + "f" * 64
            ),
        )
        self.assertEqual(error.details["reason"], "same_document_version_different_bytes")
        self.assertEqual(error.details["registered"]["bytes"], self.size)

    def test_unverified_same_version_same_bytes_stays_unverified(self):
        # 修复不能过度触发：同版本且字节 / 摘要一致时，占位版本仍只给 unverified 结论，
        # 不升级为 matched / versionVerified，也不追加 revision
        result = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="unverified"),
            [self.root],
            registered_versions=self.registered("unverified"),
        )
        self.assertEqual(result["status"], candidate.STATUS_UNVERIFIED)
        self.assertFalse(result["versionVerified"])
        self.assertEqual(result["changeOrder"], candidate.CHANGE_ORDER_ORDER_3_UNDECLARED)
        self.assertFalse(result["appendCandidateEligible"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertFalse(result["wouldSave"])
        self.assertIsNone(result["matchedRegisteredVersion"])
        self.assertTrue(result["sameVersionBytesChecked"])
        self.assertTrue(result["sameVersionConflictCheckedFirst"])

    def test_missing_version_never_triggers_same_version_conflict(self):
        # 显式无版本（None / 空字符串）不可能与已登记的非空版本相等 -> 只给 missing 结论
        rows = self.registered("unverified") + self.registered("D4.5")
        for declared in (None, ""):
            with self.subTest(declared=declared):
                result = candidate.validate_planning_source_version(
                    self.declare(self.path, documentVersion=declared),
                    [self.root],
                    registered_versions=rows,
                )
                self.assertEqual(result["status"], candidate.STATUS_MISSING)
                self.assertEqual(
                    result["changeOrder"], candidate.CHANGE_ORDER_ORDER_3_UNDECLARED
                )
                self.assertFalse(result["sameVersionBytesChecked"])
                self.assertFalse(result["appendCandidateEligible"])
                self.assertEqual(result["registeredVersionsChecked"], 2)

    def test_drift_precedes_same_version_conflict(self):
        # 序 1 不可改判：同版本存在硬冲突的同时若本批两次读取不一致，先报 drift
        def _fake_meta(path, sha256, bytes_count):
            return {"path": path, "bytes": bytes_count, "sha256": sha256}, ""

        with mock.patch.object(
            candidate,
            "read_source_file",
            return_value=_fake_meta(self.path, "sha256:" + "a" * 64, self.size),
        ):
            error = self.assertErrorCode(
                "planning_source_drift",
                candidate.validate_planning_source_version,
                self.declare(self.path, documentVersion="unverified"),
                [self.root],
                registered_versions=self.registered(
                    "unverified", bytes_count=self.size + 3
                ),
            )
        self.assertEqual(error.details["order"], candidate.CHANGE_ORDER_ORDER_1_DRIFT)
        self.assertEqual(
            error.details["reason"], "bytes_or_sha256_changed_between_reads"
        )

    def test_inspect_reports_same_version_conflict_without_saving(self):
        result = candidate.inspect_planning_source_version(
            self.declare(self.path, documentVersion="unverified"),
            [self.root],
            registered_versions=self.registered("unverified", bytes_count=self.size + 7),
        )
        self.assertEqual(result["status"], "planning_snapshot_conflict")
        self.assertFalse(result["versionVerified"])
        self.assertFalse(result["wouldSave"])
        self.assertFalse(result["wouldModifySnapshot"])
        self.assertFalse(result["wouldAppendRevision"])
        self.assertFalse(result["wouldActivate"])
        self.assertEqual(result["generatedIdentifiers"], [])
        self.assertEqual(result["executionFacts"], [])
        for key, value in result["sideEffects"].items():
            with self.subTest(key=key):
                self.assertFalse(value)
        self.assertEqual(
            result["error"]["details"]["reason"],
            "same_document_version_different_bytes",
        )

    def test_same_version_conflict_check_is_reported_on_clean_results(self):
        # 结论型返回必须显式声明「同版本核验是否发生、是否优先」，便于独立审核
        matched = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="D4.5"),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertTrue(matched["sameVersionBytesChecked"])
        self.assertTrue(matched["sameVersionConflictCheckedFirst"])
        new_version = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="D4.9"),
            [self.root],
            registered_versions=self.registered("D4.5"),
        )
        self.assertTrue(new_version["sameVersionBytesChecked"])
        no_history = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion="D4.5"), [self.root]
        )
        self.assertTrue(no_history["sameVersionBytesChecked"])
        missing = candidate.validate_planning_source_version(
            self.declare(self.path, documentVersion=None), [self.root]
        )
        self.assertFalse(missing["sameVersionBytesChecked"])


# ---------------------------------------------------------------------------
# 030 新增：显式冻结依赖加载与回归（不复制规则、不静默退化）
# ---------------------------------------------------------------------------


class FrozenDependencyTests(_CandidateTestCase):
    """冻结依赖一律 direct import；常量 / 函数身份必须与冻结契约一致。"""

    def _module_source(self):
        with open(_MODULE_PATH, "r", encoding="utf-8") as handle:
            return handle.read()

    def test_source_kinds_reused_from_frozen_digest_contract(self):
        from contracts import planning_digest

        self.assertIs(candidate.SOURCE_KINDS, planning_digest.SOURCE_KINDS)
        self.assertEqual(
            sorted(candidate.SOURCE_KINDS),
            ["development-plan", "other-registered", "product"],
        )

    def test_revision_constants_reused_from_frozen_contract(self):
        from contracts import planning_revision

        self.assertEqual(
            candidate.ERROR_SOURCE_DRIFT, planning_revision.ERROR_SOURCE_DRIFT
        )
        self.assertEqual(
            candidate.ERROR_SNAPSHOT_CONFLICT, planning_revision.ERROR_SNAPSHOT_CONFLICT
        )
        self.assertEqual(
            candidate.UNVERIFIED_DOCUMENT_VERSION,
            planning_revision.UNVERIFIED_DOCUMENT_VERSION,
        )
        self.assertEqual(
            candidate.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
            planning_revision.REASON_SAME_DOCUMENT_VERSION_DIFFERENT_BYTES,
        )
        self.assertIs(candidate._is_sha256_text, planning_revision._is_sha256_text)

    def test_module_imports_frozen_dependencies_directly_without_fallback(self):
        source = self._module_source()
        for expected in (
            "from contracts.planning_digest import SOURCE_KINDS",
            "from contracts.planning_revision import (",
            "from service.planning_preflight import (",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)
        # 显式围栏：不允许 except ImportError 静默退回本地实现
        self.assertNotIn("except ImportError", source)

    def test_module_does_not_reimplement_reading_or_hashing(self):
        source = self._module_source()
        for token in (
            "hashlib",
            "def read_source_file",
            "def preflight_source",
            "def bytes_digest",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)

    def test_error_code_set_is_exactly_the_frozen_contract_set(self):
        self.assertEqual(
            sorted(candidate.PLANNING_SOURCE_VERSION_ERROR_CODES),
            [
                "planning_snapshot_conflict",
                "planning_source_drift",
                "planning_source_out_of_scope",
                "planning_source_rejected",
                "planning_source_unreadable",
                "planning_source_version_invalid",
            ],
        )


# ---------------------------------------------------------------------------
# 031 加载围栏（固定清单绑定 / 显式 spec 加载 / 冻结基线 SHA）——不触碰行为口径
# ---------------------------------------------------------------------------


class FrozenBaselineBindingTests(unittest.TestCase):
    """本轮只修测试加载：证明基线来源固定、加载显式、SHA 一致、57 项行为用例未缩水。"""

    #: 030 保留的 7 个行为用例类与逐类用例数（不 skip、不放宽断言的总量围栏）。
    _PRESERVED_BEHAVIOR_SUITES = (
        ("DeclarationExplicitnessTests", 11),
        ("PathPolicyTests", 8),
        ("DriftTests", 7),
        ("VersionComparisonTests", 14),
        ("ZeroSideEffectTests", 5),
        ("SameVersionConflictPriorityTests", 7),
        ("FrozenDependencyTests", 5),
    )

    def _module_source(self):
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as handle:
            return handle.read()

    def test_baseline_manifest_is_the_fixed_workspace_file(self):
        self.assertEqual(
            _BASELINE_MANIFEST_PATH,
            os.path.join(_WORKSPACE, "inputs", "baseline-manifest.json"),
        )
        self.assertTrue(os.path.isfile(_BASELINE_MANIFEST_PATH))
        self.assertFalse(os.path.islink(_BASELINE_MANIFEST_PATH))
        self.assertEqual(_FORMAL_ROOT, _MANIFEST["formalRoot"])
        self.assertTrue(os.path.isdir(_FORMAL_ROOT))

    def test_loader_has_no_env_override_upward_search_or_fallback(self):
        # 围栏 token 用 "a" + "b" 拼接，避免断言自身在源码里制造命中。
        source = self._module_source()
        for token in (
            "os." + "environ",
            "get" + "env",
            "expand" + "user",
            "D4_" + "FORMAL_ROOT",
            "D4_" + "CONTRACTS_ROOT",
            "_candidate_" + "formal_roots",
            "_resolve_" + "formal_root",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, source)
        # 加载只允许两条明确路径：固定清单 + 显式 spec（没有被搜索逻辑替换）。
        self.assertIn("baseline-manifest.json", source)
        self.assertIn("spec_from_file_location", source)

    def test_round_implementation_is_loaded_explicitly_from_its_own_file(self):
        self.assertIsNotNone(candidate.__spec__)
        self.assertEqual(candidate.__spec__.name, _CANDIDATE_MODULE_NAME)
        self.assertEqual(
            os.path.realpath(candidate.__file__), os.path.realpath(_MODULE_PATH)
        )
        self.assertEqual(
            os.path.realpath(candidate.__spec__.origin), os.path.realpath(_MODULE_PATH)
        )
        self.assertIsInstance(
            candidate.__spec__.loader, importlib.machinery.SourceFileLoader
        )
        self.assertIs(candidate.__loader__, candidate.__spec__.loader)
        # 没走裸模块名的隐式 import：否则无法证明是显式按文件路径 spec 加载。
        self.assertNotIn("planning_source_version_validate", sys.modules)

    def test_formal_implementation_still_loads_from_formal_root(self):
        self.assertEqual(
            os.path.realpath(_FORMAL_IMPL_PATH),
            os.path.realpath(
                os.path.join(_FORMAL_ROOT, "service", "planning_source_version_validate.py")
            ),
        )
        self.assertIsNotNone(formal_implementation.__spec__)
        self.assertEqual(
            os.path.realpath(formal_implementation.__file__),
            os.path.realpath(_FORMAL_IMPL_PATH),
        )
        self.assertIsInstance(
            formal_implementation.__spec__.loader, importlib.machinery.SourceFileLoader
        )
        # 接纳后同源：公开 API 与错误码集合必须一致（同摘要同源，不靠复制）。
        self.assertEqual(set(formal_implementation.__all__), set(candidate.__all__))
        self.assertEqual(
            sorted(formal_implementation.PLANNING_SOURCE_VERSION_ERROR_CODES),
            sorted(candidate.PLANNING_SOURCE_VERSION_ERROR_CODES),
        )

    def test_round_implementation_sha_is_the_frozen_candidate_sha(self):
        self.assertEqual(_file_sha256(_MODULE_PATH), _CANDIDATE_SHA256)
        self.assertEqual(_ROUND_IMPLEMENTATION_DIGEST, _CANDIDATE_SHA256)
        self.assertEqual(_MANIFEST["files"][_FORMAL_MODULE_RELATIVE], _CANDIDATE_SHA256)

    def test_formal_implementation_sha_equals_round_implementation_sha(self):
        self.assertEqual(_file_sha256(_FORMAL_IMPL_PATH), _ROUND_IMPLEMENTATION_DIGEST)
        self.assertEqual(
            _BASELINE_DIGEST_REPORT[_FORMAL_MODULE_RELATIVE]["actual"],
            _ROUND_IMPLEMENTATION_DIGEST,
        )

    def test_every_frozen_baseline_file_matches_manifest_sha256(self):
        self.assertTrue(_BASELINE_DIGEST_REPORT)
        for relative, item in sorted(_BASELINE_DIGEST_REPORT.items()):
            with self.subTest(file=relative):
                self.assertTrue(item["exists"], "冻结文件缺失：%s" % relative)
                self.assertEqual(item["actual"], item["expected"])
                self.assertTrue(item["match"])
        self.assertEqual(_BASELINE_MISMATCHES, [])

    def test_preserved_behavior_suites_keep_exactly_57_cases(self):
        loader = unittest.TestLoader()
        total = 0
        for name, expected in self._PRESERVED_BEHAVIOR_SUITES:
            suite = loader.loadTestsFromTestCase(globals()[name])
            with self.subTest(suite=name):
                self.assertEqual(suite.countTestCases(), expected)
            total += suite.countTestCases()
        self.assertEqual(total, 57)


# ---------------------------------------------------------------------------
# 032 新增：加载前 SHA 门（先核 SHA、后 spec 加载）——只围栏加载方式，不触碰行为口径
# ---------------------------------------------------------------------------


class PreloadShaGateTests(unittest.TestCase):
    """本轮唯一修复点：冻结依赖 SHA 核验必须在 spec 加载**之前**完成并 fail closed。

    031 缺陷（cox 探针 ``FAIL_LOADER_REACHED_BEFORE_SHA_REJECTION``）：依赖 SHA 不一致
    只被记录，加载阶段照旧执行。032 用模块级 SHA 门把「拒绝」放在「加载」之前，本类只
    围栏这件事，不改任何行为断言。
    """

    #: 031 冻结的 65 项用例逐类清单（7 个行为类 57 项 + 加载围栏类 8 项），逐条不变。
    _PRESERVED_031_CASES = {
        "DeclarationExplicitnessTests": (
            "test_missing_document_version_key_is_rejected",
            "test_keyword_form_requires_all_four_fields",
            "test_explicit_none_version_yields_conclusion_only",
            "test_mixed_declaration_forms_rejected",
            "test_unknown_declaration_field_rejected",
            "test_declaration_must_be_object",
            "test_source_kind_closed_set",
            "test_version_type_must_be_string_or_none",
            "test_allowed_roots_required_and_validated",
            "test_no_path_never_falls_back_to_name",
            "test_declared_name_is_echoed_but_never_used_for_lookup",
        ),
        "PathPolicyTests": (
            "test_directory_path_is_unreadable",
            "test_relative_path_rejected",
            "test_dotdot_escape_rejected",
            "test_outside_allowed_roots_is_out_of_scope",
            "test_symlink_rejected",
            "test_credential_directory_rejected",
            "test_missing_file_is_unreadable",
            "test_template_placeholder_path_rejected",
        ),
        "DriftTests": (
            "test_two_identical_reads_are_reported",
            "test_second_read_digest_change_is_drift",
            "test_second_read_bytes_change_is_drift",
            "test_second_read_failure_is_drift",
            "test_preflight_reported_drift_is_drift_and_writes_nothing",
            "test_inspect_reports_drift_without_saving_or_appending",
            "test_inspect_forbids_save_on_conflict",
        ),
        "VersionComparisonTests": (
            "test_same_version_same_digest_is_matched",
            "test_same_version_different_digest_is_hard_conflict",
            "test_same_version_different_bytes_is_hard_conflict",
            "test_new_concrete_version_marks_order_two_but_never_appends",
            "test_exact_unverified_literal_only_yields_conclusion",
            "test_unverified_is_not_case_folded",
            "test_version_is_not_whitespace_trimmed",
            "test_version_is_not_unicode_normalized",
            "test_empty_version_string_is_missing",
            "test_missing_version_without_history_never_auto_appends",
            "test_concrete_version_without_history_is_order_two",
            "test_registered_row_shape_rejected",
            "test_duplicate_registered_version_rejected",
            "test_registered_versions_must_be_list",
        ),
        "ZeroSideEffectTests": (
            "test_module_source_has_no_write_or_network_primitive",
            "test_entrypoints_accept_no_database_handle",
            "test_success_result_declares_zero_side_effects_and_no_identifiers",
            "test_successful_verification_writes_nothing_to_source_tree",
            "test_reads_use_controlled_frozen_reader",
        ),
        "SameVersionConflictPriorityTests": (
            "test_unverified_same_version_byte_conflict_is_hard_conflict",
            "test_unverified_same_version_digest_conflict_is_hard_conflict",
            "test_unverified_same_version_same_bytes_stays_unverified",
            "test_missing_version_never_triggers_same_version_conflict",
            "test_drift_precedes_same_version_conflict",
            "test_inspect_reports_same_version_conflict_without_saving",
            "test_same_version_conflict_check_is_reported_on_clean_results",
        ),
        "FrozenDependencyTests": (
            "test_source_kinds_reused_from_frozen_digest_contract",
            "test_revision_constants_reused_from_frozen_contract",
            "test_module_imports_frozen_dependencies_directly_without_fallback",
            "test_module_does_not_reimplement_reading_or_hashing",
            "test_error_code_set_is_exactly_the_frozen_contract_set",
        ),
        "FrozenBaselineBindingTests": (
            "test_baseline_manifest_is_the_fixed_workspace_file",
            "test_loader_has_no_env_override_upward_search_or_fallback",
            "test_round_implementation_is_loaded_explicitly_from_its_own_file",
            "test_formal_implementation_still_loads_from_formal_root",
            "test_round_implementation_sha_is_the_frozen_candidate_sha",
            "test_formal_implementation_sha_equals_round_implementation_sha",
            "test_every_frozen_baseline_file_matches_manifest_sha256",
            "test_preserved_behavior_suites_keep_exactly_57_cases",
        ),
    }

    #: 若 SHA 门失效、加载器真的被触达，本毒丸模块被 exec 时会抛此标记。
    _LOADER_REACHED_MARKER = "LOADER_REACHED_BEFORE_SHA_REJECTION"

    _POISON_IMPLEMENTATION = (
        '"""毒丸：加载前 SHA 门失效时，本文件被 exec 即暴露「先加载后核 SHA」回归。"""\n'
        'raise RuntimeError("%s")\n'
    ) % _LOADER_REACHED_MARKER

    def _module_source(self):
        with open(os.path.abspath(__file__), "r", encoding="utf-8") as handle:
            return handle.read()

    def _build_tampered_workspace(self, mutate_manifest, implementation_text):
        """在本测试自建临时目录里复刻 workspace 布局，用于验证加载阶段 fail closed。

        ``mutate_manifest`` 在清单里制造受控篡改；``candidateSha256`` 一律按临时目录里
        实际写入的实现文件摘要写，保证「被拒绝」的唯一原因就是冻结依赖 SHA 不一致。
        """
        root = tempfile.mkdtemp(prefix="d4-032-preload-")
        self.addCleanup(_remove_tree, root)
        tests_dir = os.path.join(root, "tests")
        service_dir = os.path.join(root, "service")
        inputs_dir = os.path.join(root, "inputs")
        os.makedirs(tests_dir)
        os.makedirs(service_dir)
        os.makedirs(inputs_dir)
        implementation_path = _write_text(
            os.path.join(service_dir, "planning_source_version_validate.py"),
            implementation_text,
        )
        payload = json.loads(json.dumps(_MANIFEST))
        payload["candidateSha256"] = _file_sha256(implementation_path)
        mutate_manifest(payload)
        _write_text(
            os.path.join(inputs_dir, "baseline-manifest.json"), json.dumps(payload)
        )
        test_copy = _write_text(
            os.path.join(tests_dir, os.path.basename(__file__)), self._module_source()
        )
        return payload, implementation_path, test_copy

    def test_gate_runs_before_any_spec_load_in_module_sequence(self):
        # 模块级顺序自证：清单 -> 冻结 SHA 对拍 -> 加载前 SHA 门 -> 才 spec 加载
        self.assertEqual(
            _LOAD_SEQUENCE,
            [
                "baseline_manifest_loaded",
                "frozen_baseline_sha_audited",
                "preload_sha_gate_passed",
                "round_implementation_spec_loaded",
                "formal_implementation_spec_loaded",
            ],
        )
        self.assertTrue(_FROZEN_BASELINE_GATE_PASSED)
        self.assertEqual(_BASELINE_MISMATCHES, [])
        self.assertTrue(_BASELINE_DIGEST_REPORT)
        # 门通过后才加载：两个模块都来自显式文件路径
        self.assertEqual(
            os.path.realpath(candidate.__file__), os.path.realpath(_MODULE_PATH)
        )
        self.assertEqual(
            os.path.realpath(formal_implementation.__file__),
            os.path.realpath(_FORMAL_IMPL_PATH),
        )

    def test_gate_helper_rejects_sha_mismatch_before_load(self):
        clean_report = {relative: {"match": True} for relative in _MANIFEST["files"]}
        self.assertTrue(
            _require_frozen_baseline_intact(
                _MANIFEST, clean_report, _ROUND_IMPLEMENTATION_DIGEST, _CANDIDATE_SHA256
            )
        )

        # 1) 冻结依赖摘要不一致 -> 加载前即拒绝，且报出被篡改的相对路径
        tampered_dependency = dict(clean_report)
        tampered_dependency["contracts/planning_digest.py"] = {"match": False}
        with self.assertRaises(ImportError) as ctx:
            _require_frozen_baseline_intact(
                _MANIFEST,
                tampered_dependency,
                _ROUND_IMPLEMENTATION_DIGEST,
                _CANDIDATE_SHA256,
            )
        message = str(ctx.exception)
        self.assertIn(_FROZEN_BASELINE_SHA_MISMATCH, message)
        self.assertIn("contracts/planning_digest.py", message)

        # 2) 本轮实现自身摘要与 candidateSha256 不一致 -> 同样加载前拒绝
        with self.assertRaises(ImportError) as ctx:
            _require_frozen_baseline_intact(
                _MANIFEST, clean_report, "0" * 64, _CANDIDATE_SHA256
            )
        self.assertIn(_FROZEN_BASELINE_SHA_MISMATCH, str(ctx.exception))

        # 3) 对拍未覆盖固定清单的文件集（少核文件）-> 也算 SHA 门失效，不得放行
        narrowed = dict(clean_report)
        del narrowed["contracts/planning_digest.py"]
        with self.assertRaises(ImportError) as ctx:
            _require_frozen_baseline_intact(
                _MANIFEST, narrowed, _ROUND_IMPLEMENTATION_DIGEST, _CANDIDATE_SHA256
            )
        self.assertIn(_FROZEN_BASELINE_SHA_MISMATCH, str(ctx.exception))

    def test_gate_helper_rejects_invalid_inputs_without_fallback(self):
        with self.assertRaises(ImportError) as ctx:
            _require_frozen_baseline_intact(None, {}, _ROUND_IMPLEMENTATION_DIGEST,
                                            _CANDIDATE_SHA256)
        self.assertIn(_FROZEN_BASELINE_SHA_MISMATCH, str(ctx.exception))
        with self.assertRaises(ImportError):
            _require_frozen_baseline_intact(_MANIFEST, "not-a-report",
                                            _ROUND_IMPLEMENTATION_DIGEST,
                                            _CANDIDATE_SHA256)

    def test_import_refuses_before_loader_when_dependency_sha_tampered(self):
        # 合成被篡改 workspace：固定清单里依赖摘要被改坏，candidate 摘要与毒丸文件一致，
        # 因此「唯一」可导致拒绝的原因就是冻结依赖 SHA 不一致。
        def _tamper_dependency(payload):
            payload["files"]["contracts/planning_digest.py"] = "0" * 64

        payload, implementation_path, test_copy = self._build_tampered_workspace(
            _tamper_dependency, self._POISON_IMPLEMENTATION
        )
        self.assertEqual(_file_sha256(implementation_path), payload["candidateSha256"])
        self.assertEqual(
            payload["files"]["contracts/planning_digest.py"], "0" * 64
        )

        with self.assertRaises(ImportError) as ctx:
            _load_module_from_path(
                "d4_mvp20_032_tampered_baseline_self_check", test_copy
            )
        message = str(ctx.exception)
        self.assertIn(_FROZEN_BASELINE_SHA_MISMATCH, message)
        self.assertIn("contracts/planning_digest.py", message)
        # 毒丸没被 exec：证明拒绝发生在任何 spec 加载之前
        self.assertNotIn(self._LOADER_REACHED_MARKER, message)

    def test_import_refuses_when_fixed_manifest_is_missing(self):
        root = tempfile.mkdtemp(prefix="d4-032-nomanifest-")
        self.addCleanup(_remove_tree, root)
        tests_dir = os.path.join(root, "tests")
        os.makedirs(tests_dir)
        test_copy = _write_text(
            os.path.join(tests_dir, os.path.basename(__file__)), self._module_source()
        )
        with self.assertRaises(ImportError) as ctx:
            _load_module_from_path(
                "d4_mvp20_032_missing_manifest_self_check", test_copy
            )
        self.assertIn("baseline-manifest.json", str(ctx.exception))

    def test_gate_helper_has_no_fallback_branch(self):
        source = inspect.getsource(_require_frozen_baseline_intact)
        self.assertIn("raise ImportError", source)
        self.assertNotIn("except", source)
        self.assertNotIn("return False", source)

    def test_preserved_031_cases_keep_exactly_the_same_65_methods(self):
        loader = unittest.TestLoader()
        total = 0
        for name, expected in sorted(self._PRESERVED_031_CASES.items()):
            suite = loader.loadTestsFromTestCase(globals()[name])
            actual = sorted(test._testMethodName for test in suite)
            with self.subTest(suite=name):
                self.assertEqual(actual, sorted(expected))
            total += suite.countTestCases()
        self.assertEqual(total, 65)


if __name__ == "__main__":
    unittest.main()
