"""D4-DSH-DEV-CONTEXT-001 定向测试：离线开发上下文装配（接口合同 §14.1）。

R1 覆盖：有效三源装配、contextDigest 确定性与可复算、摘要漂移、路径越界/相对路径/
`..`、缺失/空/目录/symlink/非法 UTF-8、单文件与合计大小上限、expected_digests
形状错误、错误输入类型、错误详情不回显文件正文；R1 回归：中央伪角色（同尾段异根）、
任务卡父目录 symlink、allowed root symlink、FIFO 安全读边界、恰好上限、
摘要尾换行 fullmatch 拒绝、系统别名窄例外（其余 realpath 变化不放行）。
R2 回归（本卡 D4-DSH-DEV-CONTEXT-001-R2）：四种路径输入（workspace/
task_card_path/role_path/allowed_task_roots）内嵌 NUL 与无法被文件系统编码的
字符串在任何 OS 调用之前统一拒绝为 ContractError('invalid_task',
reason=context_path_rejected)；修复前裸 ValueError 绕过统一错误合同。
R1 全部 25 项测试与断言意图原样保留。

runtime 落点：所有 fixture 写入本模块所在受控 receipts 目录
`shared/agent-orchestrator/receipts/d4-dsh-context-fixtures/`（默认落在
`D4_DSH_CONTEXT_FIXTURE_DIR` 环境变量缺省时），可用该环境变量覆盖（仅本地调试用）；
默认不写项目 `.conductor-local` 或其它仓库根外目录。

夹具规约（R1 修正）：`build()` 默认摘要一律取自真实任务卡（存在的文件），
不在调用被测函数前读取/摘要任何负例 fixture；负例由产品在路径/形态校验阶段拒绝。
漂移负例使用正确 fixture 根 + 真实任务卡旧摘要，专测 digest_mismatch。

本套测试不联网、不建库、不调宿主；fixture 由测试自身创建并在结束时清理。
"""
import hashlib
import json
import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from contracts import development_context as dc
from contracts.development_context import build_development_context
from contracts.schemas import ContractError

WORKSPACE = "/Users/demo/.hermes/skills/conductor"
ROLE_PATH = WORKSPACE + "/sales-dev/references/dsh-development-role.md"
PROJECT_RULES_PATH = WORKSPACE + "/AGENTS.md"
REAL_TASK_CARD = (
    WORKSPACE + "/docs/development/task-cards/"
    "2026-10-09-d4-dsh-development-context.md"
)
TASK_CARD_ROOT = os.path.dirname(REAL_TASK_CARD)

# 默认 fixture/runtime 根固定落在本模块所在受控 receipts 目录内；
# 仅本地调试可用环境变量覆盖，默认不写项目 .conductor-local 或其它仓库根外目录。
FIXTURE_BASE = os.environ.get("D4_DSH_CONTEXT_FIXTURE_DIR") or (
    "/Users/demo/.hermes/skills/conductor/shared/agent-orchestrator/"
    "receipts/d4-dsh-context-fixtures"
)

ALLOWED_REASONS = {
    "context_input_invalid",
    "context_path_rejected",
    "context_file_missing",
    "context_file_invalid",
    "context_digest_mismatch",
    "context_too_large",
}


def sha256_file(path):
    with open(path, "rb") as handle:
        return "sha256:" + hashlib.sha256(handle.read()).hexdigest()


def default_digests(task_card_path):
    """基准摘要：仅对确认存在的文件调用（真实任务卡）；负例路径不在此读取。"""
    return {
        "role": sha256_file(ROLE_PATH),
        "project": sha256_file(PROJECT_RULES_PATH),
        "taskCard": sha256_file(task_card_path),
    }


def build(task_card_path=None, allowed_task_roots=None, expected_digests=None,
          **overrides):
    """构造被测调用；expected_digests 未提供时固定用真实任务卡基准摘要，
    保证缺失/目录/相对路径等负例在调用 build_development_context 前不做文件读取。"""
    if task_card_path is None:
        task_card_path = REAL_TASK_CARD
    if allowed_task_roots is None:
        allowed_task_roots = [TASK_CARD_ROOT]
    if expected_digests is None:
        expected_digests = default_digests(REAL_TASK_CARD)
    params = dict(
        workspace=WORKSPACE,
        task_card_path=task_card_path,
        role_path=ROLE_PATH,
        allowed_task_roots=allowed_task_roots,
        expected_digests=expected_digests,
    )
    params.update(overrides)
    return build_development_context(**params)


class DevelopmentContextTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tag = time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid())
        cls.run_dir = Path(FIXTURE_BASE) / tag
        cls.run_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.run_dir, ignore_errors=True)

    def fixture(self, name, data):
        """在受控 fixture 目录写入文件；data 为 str 时按 UTF-8 编码。"""
        path = self.run_dir / name
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return str(path)

    def fixture_task_card(self, name="task-card.md", tweak=False):
        """真实任务卡的副本；tweak=True 时追加一个字节制造漂移。"""
        with open(REAL_TASK_CARD, "rb") as handle:
            raw = handle.read()
        if tweak:
            raw = raw + b"\n"
        return self.fixture(name, raw)

    def assertInvalid(self, reason, field=None, fn=None):
        with self.assertRaises(ContractError) as ctx:
            fn()
        exc = ctx.exception
        self.assertEqual(exc.code, "invalid_task")
        self.assertEqual(set(exc.details.keys()), {"reason", "field"})
        self.assertEqual(exc.details["reason"], reason)
        self.assertIn(exc.details["reason"], ALLOWED_REASONS)
        if field is not None:
            self.assertEqual(exc.details["field"], field)
        return exc

    # ------------------------------------------------------------------
    # 1) 有效三源装配
    # ------------------------------------------------------------------

    def test_valid_three_source_assembly(self):
        result = build()
        self.assertEqual(result["schemaVersion"], "dsh-development-context.v1")
        self.assertEqual(result["role"], "skill-worker")
        self.assertEqual(
            [item["kind"] for item in result["sources"]],
            ["role", "project", "taskCard"],
        )
        for item in result["sources"]:
            self.assertEqual(set(item.keys()), {"kind", "path", "sha256", "bytes"})
            self.assertTrue(item["path"].startswith("/"))
            self.assertGreater(item["bytes"], 0)
        self.assertTrue(result["systemPrompt"].startswith("# DSH 开发角色说明"))
        self.assertIn("固定权限边界", result["systemPrompt"])
        with open(PROJECT_RULES_PATH, "rb") as handle:
            self.assertEqual(
                result["taskContext"]["projectRules"]["text"],
                handle.read().decode("utf-8"),
            )
        with open(REAL_TASK_CARD, "rb") as handle:
            self.assertEqual(
                result["taskContext"]["taskCard"]["text"],
                handle.read().decode("utf-8"),
            )

    def test_context_digest_deterministic_and_recomputable(self):
        first = build()
        second = build()
        self.assertEqual(first["contextDigest"], second["contextDigest"])
        manual = {k: v for k, v in first.items() if k != "contextDigest"}
        canonical = json.dumps(
            manual, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        expected = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(first["contextDigest"], expected)

    # ------------------------------------------------------------------
    # 2) 摘要漂移（正确 fixture 根 + 真实任务卡旧摘要）
    # ------------------------------------------------------------------

    def test_digest_drift_rejected(self):
        drifted = self.fixture_task_card("drifted.md", tweak=True)
        self.assertInvalid(
            "context_digest_mismatch",
            "expected_digests.taskCard",
            fn=lambda: build(
                task_card_path=drifted,
                allowed_task_roots=[str(self.run_dir)],
            ),
        )

    # ------------------------------------------------------------------
    # 3) 路径越界 / 相对路径 / ..
    # ------------------------------------------------------------------

    def test_task_card_outside_roots_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(
                task_card_path=PROJECT_RULES_PATH,
                allowed_task_roots=[TASK_CARD_ROOT],
            ),
        )

    def test_dotdot_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(
                task_card_path=TASK_CARD_ROOT + "/../task-cards/2026-10-09-d4-dsh-development-context.md"
            ),
        )

    def test_relative_path_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(task_card_path="docs/development/task-cards/x.md"),
        )

    # ------------------------------------------------------------------
    # 4) 缺失 / 空 / 目录 / symlink / 非法 UTF-8
    # ------------------------------------------------------------------

    def test_missing_file_rejected(self):
        missing = self.fixture("missing.md", "placeholder-not-used")
        os.remove(missing)
        self.assertInvalid(
            "context_file_missing",
            "task_card_path",
            fn=lambda: build(task_card_path=missing, allowed_task_roots=[str(self.run_dir)]),
        )

    def test_empty_file_rejected(self):
        empty = self.fixture("empty.md", "")
        self.assertInvalid(
            "context_file_invalid",
            "task_card_path",
            fn=lambda: build(task_card_path=empty, allowed_task_roots=[str(self.run_dir)]),
        )

    def test_directory_as_file_rejected(self):
        self.assertInvalid(
            "context_file_invalid",
            "task_card_path",
            fn=lambda: build(
                task_card_path=str(self.run_dir), allowed_task_roots=[str(self.run_dir)]
            ),
        )

    def test_symlink_rejected(self):
        target = self.fixture_task_card("link-target.md")
        link = str(self.run_dir / "link.md")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(target, link)
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(task_card_path=link, allowed_task_roots=[str(self.run_dir)]),
        )

    def test_invalid_utf8_rejected(self):
        bad = self.fixture("bad-utf8.md", b"# \xff\xfe invalid utf-8\n")
        self.assertInvalid(
            "context_file_invalid",
            "task_card_path",
            fn=lambda: build(task_card_path=bad, allowed_task_roots=[str(self.run_dir)]),
        )

    # ------------------------------------------------------------------
    # 5) 大小上限
    # ------------------------------------------------------------------

    def test_single_file_over_limit_rejected(self):
        big = self.fixture("big.md", b"# big\n" + b"a" * (dc.MAX_FILE_BYTES + 1))
        self.assertInvalid(
            "context_too_large",
            "task_card_path",
            fn=lambda: build(task_card_path=big, allowed_task_roots=[str(self.run_dir)]),
        )

    def test_exact_size_limit_accepted(self):
        payload = b"# exact\n" + b"a" * (dc.MAX_FILE_BYTES - 8)
        exact = self.fixture("exact.md", payload)
        digests = default_digests(REAL_TASK_CARD)
        digests["taskCard"] = "sha256:" + hashlib.sha256(payload).hexdigest()
        result = build(
            task_card_path=exact,
            allowed_task_roots=[str(self.run_dir)],
            expected_digests=digests,
        )
        self.assertEqual(result["sources"][2]["bytes"], dc.MAX_FILE_BYTES)

    def test_total_limit_rejected(self):
        with mock.patch.object(dc, "MAX_TOTAL_BYTES", 1024):
            self.assertInvalid(
                "context_too_large",
                "total_bytes",
                fn=lambda: build(),
            )

    # ------------------------------------------------------------------
    # 6) 角色中央路径（R1：固定到本模块所在 conductor 根，全等比对）
    # ------------------------------------------------------------------

    def test_role_path_must_be_central(self):
        self.assertInvalid(
            "context_path_rejected",
            "role_path",
            fn=lambda: build(role_path=REAL_TASK_CARD),
        )

    def test_role_same_tail_elsewhere_rejected(self):
        """R1 回归：别处同尾段文件不能冒充中央角色（修复前仅比对尾段可通过）。"""
        fake_role_dir = self.run_dir / "sales-dev" / "references"
        fake_role_dir.mkdir(parents=True, exist_ok=True)
        fake_role = fake_role_dir / "dsh-development-role.md"
        fake_role.write_text("# 伪中央角色\n", encoding="utf-8")
        self.assertInvalid(
            "context_path_rejected",
            "role_path",
            fn=lambda: build(role_path=str(fake_role)),
        )

    def test_role_symlink_rejected(self):
        link = str(self.run_dir / "role-link.md")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(ROLE_PATH, link)
        self.assertInvalid(
            "context_path_rejected",
            "role_path",
            fn=lambda: build(role_path=link),
        )

    # ------------------------------------------------------------------
    # 7) 父目录符号链接（R1 回归：修复前 realpath 消掉父链接后被接受）
    # ------------------------------------------------------------------

    def test_task_card_parent_symlink_rejected(self):
        sub = self.run_dir / "sub"
        sub.mkdir(exist_ok=True)
        (sub / "card.md").write_text("# 卡\n", encoding="utf-8")
        link = self.run_dir / "alias"
        if os.path.lexists(str(link)):
            os.remove(str(link))
        os.symlink(str(sub), str(link))
        # 词法上 link/card.md 在 run_dir 受控根内，但中间段是用户符号链接，必须拒绝
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(
                task_card_path=str(link / "card.md"),
                allowed_task_roots=[str(self.run_dir)],
            ),
        )

    def test_allowed_root_symlink_rejected(self):
        sub = self.run_dir / "sub2"
        sub.mkdir(exist_ok=True)
        link = self.run_dir / "alias2"
        if os.path.lexists(str(link)):
            os.remove(str(link))
        os.symlink(str(sub), str(link))
        self.assertInvalid(
            "context_path_rejected",
            "allowed_task_roots",
            fn=lambda: build(allowed_task_roots=[str(link)]),
        )

    def test_system_alias_exception_narrowly_allowed(self):
        """R1 回归：仅固定系统别名（/tmp -> /private/tmp）放行；
        其余 realpath 变化一律按用户符号链接拒绝。"""
        with mock.patch.object(
            dc.os.path, "islink", side_effect=lambda p: p == "/tmp"
        ), mock.patch.object(
            dc.os.path, "realpath", side_effect=lambda p: "/private/tmp"
        ):
            dc._policy_check_no_user_symlink("/tmp/fixtures/x.md", "workspace")  # 不抛
        with mock.patch.object(
            dc.os.path, "islink", side_effect=lambda p: p == "/Users/demo/evil-link"
        ), mock.patch.object(
            dc.os.path, "realpath", side_effect=lambda p: "/Users/demo/evil-target"
        ):
            with self.assertRaises(ContractError) as ctx:
                dc._policy_check_no_user_symlink(
                    "/Users/demo/evil-link/x.md", "workspace"
                )
            self.assertEqual(
                ctx.exception.details["reason"], "context_path_rejected"
            )

    # ------------------------------------------------------------------
    # 8) 安全读边界（R1 回归：无先检查后打开的竞态模式）
    # ------------------------------------------------------------------

    def test_fifo_rejected_without_blocking(self):
        """FIFO 不是 regular file：以 O_NOFOLLOW|O_NONBLOCK 打开后由 fstat 拒绝，
        既不阻塞也不进入不受限 read。"""
        fifo = self.run_dir / "fifo.md"
        if os.path.exists(str(fifo)):
            os.remove(str(fifo))
        os.mkfifo(str(fifo))
        self.assertInvalid(
            "context_file_invalid",
            "task_card_path",
            fn=lambda: build(
                task_card_path=str(fifo),
                allowed_task_roots=[str(self.run_dir)],
            ),
        )

    # ------------------------------------------------------------------
    # 9) expected_digests 形态（fullmatch，拒绝尾换行）
    # ------------------------------------------------------------------

    def test_digest_trailing_newline_rejected(self):
        """R1 回归：`$` 锚点会放过尾换行，fullmatch 必须拒绝。"""
        digests = default_digests(REAL_TASK_CARD)
        digests["taskCard"] = digests["taskCard"] + "\n"
        self.assertInvalid(
            "context_input_invalid",
            "expected_digests.taskCard",
            fn=lambda: build(expected_digests=dict(digests)),
        )

    def test_digest_shape_errors(self):
        bad_values = [
            "sha256:XYZ",
            "md5:" + "0" * 64,
            "sha256:" + "A" * 64,
            "sha256:" + "0" * 63,
            None,
            123,
        ]
        for value in bad_values:
            digests = default_digests(REAL_TASK_CARD)
            digests["taskCard"] = value
            self.assertInvalid(
                "context_input_invalid",
                "expected_digests.taskCard",
                fn=lambda v=value: build(expected_digests=dict(digests)),
            )
        for broken in [
            {"role": "x", "project": "y"},                    # 缺项
            dict(default_digests(REAL_TASK_CARD), extra="z"),  # 多项
            "not-a-dict",                                      # 非对象
        ]:
            self.assertInvalid(
                "context_input_invalid",
                "expected_digests",
                fn=lambda b=broken: build(expected_digests=broken),
            )

    # ------------------------------------------------------------------
    # 10) 错误输入类型
    # ------------------------------------------------------------------

    def test_input_type_errors(self):
        self.assertInvalid(
            "context_path_rejected",
            "workspace",
            fn=lambda: build(workspace=None),
        )
        self.assertInvalid(
            "context_input_invalid",
            "allowed_task_roots",
            fn=lambda: build(allowed_task_roots=[]),
        )
        self.assertInvalid(
            "context_input_invalid",
            "allowed_task_roots",
            fn=lambda: build(allowed_task_roots="docs/development/task-cards"),
        )

    # ------------------------------------------------------------------
    # 11) 错误详情不回显文件正文
    # ------------------------------------------------------------------

    def test_error_details_do_not_leak_content(self):
        marker = "CONTEXT-SECRET-MARKER-9d31f2"
        leaked = self.fixture("leaky.md", "# 卡片\n标记：" + marker + "\n")
        digests = default_digests(REAL_TASK_CARD)  # 故意用别的文件的摘要
        exc = self.assertInvalid(
            "context_digest_mismatch",
            "expected_digests.taskCard",
            fn=lambda: build(
                task_card_path=leaked,
                allowed_task_roots=[str(self.run_dir)],
                expected_digests=digests,
            ),
        )
        self.assertNotIn(marker, str(exc))
        self.assertNotIn(marker, json.dumps(exc.details, ensure_ascii=False))

    # ------------------------------------------------------------------
    # 12) NUL 与不可编码路径（R2 回归：修复前裸 ValueError 绕过统一错误合同）
    # ------------------------------------------------------------------

    def test_nul_in_workspace_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "workspace",
            fn=lambda: build(workspace=WORKSPACE + "\x00"),
        )

    def test_nul_in_task_card_path_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(task_card_path=REAL_TASK_CARD + "\x00"),
        )

    def test_nul_in_role_path_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "role_path",
            fn=lambda: build(role_path=ROLE_PATH + "\x00"),
        )

    def test_nul_in_allowed_task_roots_rejected(self):
        self.assertInvalid(
            "context_path_rejected",
            "allowed_task_roots",
            fn=lambda: build(allowed_task_roots=[TASK_CARD_ROOT + "\x00"]),
        )

    def test_unencodable_paths_rejected(self):
        """UTF-8 文件系统上，孤立代理字符（如 U+D800）无法被 os.fsencode 编码，
        四种路径输入都必须在任何 OS 调用之前统一拒绝，不泄漏裸 ValueError。"""
        evil = "/\ud800evil"
        cases = [
            ("workspace", lambda: build(workspace=WORKSPACE + evil)),
            ("task_card_path",
             lambda: build(task_card_path=REAL_TASK_CARD + evil)),
            ("role_path", lambda: build(role_path=ROLE_PATH + evil)),
            ("allowed_task_roots",
             lambda: build(allowed_task_roots=[TASK_CARD_ROOT + evil])),
        ]
        for field, fn in cases:
            self.assertInvalid("context_path_rejected", field, fn=fn)


# @@R1-SYSTEM-ALIAS-BOUNDARY@@
# ---------------------------------------------------------------------------
# D4-DSH-CONTEXT-SYSTEM-ALIAS-R1 新增段
#
# 本段之外的文件内容必须与冻结原文件逐字一致：测试用例
# FrozenSourceGateTest.test_test_file_without_added_section_matches_frozen
# 用两处边界标记切掉本段后做 SHA-256 比对，等于卡内正文冻结值才通过。
# 原 30 项断言（DevelopmentContextTestCase）未作任何修改。
#
# 本段内容：
#   1) 冻结来源核对：hashlib 复算两源码 SHA-256 与卡内正文冻结值比对，漂移即失败。
#   2) 真实系统别名打开回归：以根目录 fd 直接调用 _open_component 打开真实
#      /tmp、/var，核实目标 inode 后立即关闭 fd，不向其中写任何文件。
#   3) 负例：普通非目录段 ENOTDIR、用户自建别名、别名 inode 不一致。
# ---------------------------------------------------------------------------

import stat  # noqa: E402  本卡新增段内的模块级导入

FROZEN_PRODUCT_SHA256 = (
    "sha256:97427413662e5f8318b7ba5f809f373450e9c078c0ff217db97dcc4c3ad2e665"
)
FROZEN_TEST_FILE_SHA256 = (
    "sha256:e528aca751496dc47e0cfa20ea9957008aa10b58e49f61aefdf876edfbfc683d"
)
PRODUCT_SOURCE_PATH = (
    WORKSPACE + "/shared/agent-orchestrator/contracts/development_context.py"
)
# 补丁后实测值：补丁落地后按运行记录回填（Run B 实测）；补丁前保持 None，只认冻结值。
POST_PATCH_PRODUCT_SHA256 = (
    "sha256:ef3cacc46819a15541c94813af81f9e6ca8d8a6099fff3ca9371d02d37539c03"
)

_BOUNDARY = b"# @@R1-SYSTEM-ALIAS" + b"-BOUNDARY@@\n"


class FrozenSourceGateTest(unittest.TestCase):
    """开工前冻结来源核对：SHA 漂移则本卡零产品编辑并上报。"""

    def test_test_file_without_added_section_matches_frozen(self):
        raw = Path(__file__).read_bytes()
        parts = raw.split(_BOUNDARY)
        self.assertEqual(len(parts), 3, "新增段边界标记数量异常，无法还原冻结原文件")
        restored = parts[0] + parts[2]
        actual = "sha256:" + hashlib.sha256(restored).hexdigest()
        self.assertEqual(actual, FROZEN_TEST_FILE_SHA256)

    def test_product_source_matches_frozen_or_recorded_patch(self):
        actual = sha256_file(PRODUCT_SOURCE_PATH)
        allowed = {FROZEN_PRODUCT_SHA256}
        if POST_PATCH_PRODUCT_SHA256 is not None:
            allowed.add(POST_PATCH_PRODUCT_SHA256)
        self.assertIn(actual, allowed, "产品源码 SHA 与冻结/记录值不一致：%s" % actual)

    def test_record_current_source_shas(self):
        """记录本轮实测 SHA（打印输出供报告取证）。"""
        product = sha256_file(PRODUCT_SOURCE_PATH)
        raw = Path(__file__).read_bytes()
        test_file = "sha256:" + hashlib.sha256(raw).hexdigest()
        print(
            "\n[R1-SYSTEM-ALIAS] product_sha256=%s test_file_sha256=%s"
            % (product, test_file)
        )
        self.assertTrue(product.startswith("sha256:"))


class SystemAliasOpenRegressionTest(unittest.TestCase):
    """真实系统别名打开回归与负例（fixture 全部落授权 receipts 目录）。"""

    @classmethod
    def setUpClass(cls):
        tag = time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()) + "-alias"
        cls.run_dir = Path(FIXTURE_BASE) / tag
        cls.run_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.run_dir, ignore_errors=True)

    def fixture(self, name, data):
        path = self.run_dir / name
        if isinstance(data, str):
            path.write_text(data, encoding="utf-8")
        else:
            path.write_bytes(data)
        return str(path)

    def assertInvalid(self, reason, field, fn):
        with self.assertRaises(ContractError) as ctx:
            fn()
        exc = ctx.exception
        self.assertEqual(exc.code, "invalid_task")
        self.assertEqual(exc.details["reason"], reason)
        self.assertEqual(exc.details["field"], field)
        return exc

    def _open_alias_component(self, component):
        """以根目录 fd 直接打开真实系统别名目录段（不落任何文件）。"""
        root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            return dc._open_component(
                component, root_fd, False, "task_card_path", [component], 0
            )
        finally:
            os.close(root_fd)

    def test_real_tmp_alias_directory_openable(self):
        """真实 /tmp：旧模块 O_DIRECTORY|O_NOFOLLOW 得到 ENOTDIR 直接失败；
        修复后打开同一目录 inode 并立即关闭，不向 /tmp 写文件。"""
        fd = self._open_alias_component("tmp")
        try:
            opened = os.fstat(fd)
            expected = os.stat("/tmp")
            self.assertTrue(stat.S_ISDIR(opened.st_mode))
            self.assertEqual(
                (opened.st_dev, opened.st_ino), (expected.st_dev, expected.st_ino)
            )
        finally:
            os.close(fd)

    def test_real_var_alias_directory_openable(self):
        """真实 /var -> /private/var：同 /tmp 的 ENOTDIR 回归。"""
        fd = self._open_alias_component("var")
        try:
            opened = os.fstat(fd)
            expected = os.stat("/var")
            self.assertTrue(stat.S_ISDIR(opened.st_mode))
            self.assertEqual(
                (opened.st_dev, opened.st_ino), (expected.st_dev, expected.st_ino)
            )
        finally:
            os.close(fd)

    def test_enotdir_plain_file_segment_not_alias(self):
        """普通非目录段（真实文件充当中间段）触发 ENOTDIR，但不是固定别名：
        不得进入别名例外，最终仍被拒绝。"""
        plain = self.fixture("plain-segment.md", "# 普通文件\n")
        self.assertInvalid(
            "context_file_invalid",
            "task_card_path",
            fn=lambda: build(
                task_card_path=plain + "/sub.md",
                allowed_task_roots=[str(self.run_dir)],
            ),
        )

    def test_user_alias_symlink_still_rejected(self):
        """用户自建符号链接（realpath 变化但不是固定别名对）仍拒绝。"""
        link = str(self.run_dir / "user-alias")
        if os.path.lexists(link):
            os.remove(link)
        os.symlink("/private/tmp", link)
        self.assertInvalid(
            "context_path_rejected",
            "task_card_path",
            fn=lambda: build(
                task_card_path=link + "/x.md",
                allowed_task_roots=[str(self.run_dir)],
            ),
        )

    def test_alias_inode_mismatch_rejected_and_fd_closed(self):
        """合成负例：别名分支打开成功但 dev/ino 核验不一致 → 拒绝且 fd 完整关闭。
        真实 /tmp、/var 指向同一 inode，无法自然制造不一致；这里只替换 fstat 的
        观察值，不 mock 别名判定所依赖的 open/realpath 等系统调用。"""
        root_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        seen = {}
        real_fstat = os.fstat

        def fake_fstat(fd):
            st = real_fstat(fd)
            seen["fd"] = fd
            return os.stat_result(
                (
                    st.st_mode, st.st_ino + 1, st.st_dev, st.st_nlink, st.st_uid,
                    st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime,
                )
            )

        try:
            with mock.patch.object(dc.os, "fstat", side_effect=fake_fstat):
                with self.assertRaises(ContractError) as ctx:
                    dc._open_component(
                        "tmp", root_fd, False, "task_card_path", ["tmp"], 0
                    )
        finally:
            os.close(root_fd)
        self.assertEqual(ctx.exception.code, "invalid_task")
        self.assertEqual(ctx.exception.details["reason"], "context_path_rejected")
        self.assertIn("fd", seen)
        with self.assertRaises(OSError):
            os.fstat(seen["fd"])  # 已关闭 → EBADF

# @@R1-SYSTEM-ALIAS-BOUNDARY@@
if __name__ == "__main__":
    unittest.main()
