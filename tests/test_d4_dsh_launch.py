"""D4 离线测试：``orchestrator.dsh_launch.prepare_dsh_launch``。

不启动 DSH、不调用模型、不联网、不执行任何子进程（全程 mock ``subprocess.Popen``）。

覆盖范围（对应任务卡 acceptance）：
- [2] 冻结输入 SHA：``isolation.py`` / ``dsh.py`` / ``dsh_sdk.py`` / ``d4ctl.py``
      在测试内用 hashlib 核验，漂移即失败停止。
- [3] 接口形状：仅关键字参数、固定 ``[node, sdk_entry, --profile, sdk-minimal]``、
      无 command/profile 覆盖入口、无自动 fallback。
- [4] 路径形状：绝对普通文件、拒 ``..``/NUL/用户 symlink/父目录 symlink、Node 可执行、
      只放过 ``/tmp`` ``/var`` 系统别名；``workspace`` / ``state_dir`` /
      ``runtime_roots`` 三个目录参数同样在任何 ``resolve`` 之前逐段拒绝用户
      symlink（含断裂软链接），系统别名只在 ``realpath`` 精确等于
      ``/private/tmp`` / ``/private/var`` 时放行、目标漂移即拒绝；SDK 入口须在可信
      runtime_roots 内且不在 workspace 内；runtime_roots 不得为 ``/``、不得含用户
      HOME、不得与控制目录或 workspace 重叠。``py_compile`` 语法门禁也在此类内离线运行。
- [5][6] 复用 ``isolation.prepare_launch``（同一函数对象）；非法输入零新建 HOME、
      零进程；只放行 locale/timezone/terminal env；返回规格含固定
      HOME/DSH_HOME/TMPDIR，控制账本在所有允许根外。
- [7] 正负例与 fixture 清理策略：fixture 与 ``py_compile`` 临时产物只在**本轮回执
      目录** ``receipts/task-demo-8b8adacd92/att-demo-c6d56c0d13/`` 下新建独立目录，结束即
      删除本次 fixture / 临时产物与本次私有 HOME，绝不触碰既有目录（含历史 attempt
      收据目录）。
- [8] 测试输出打印新增源码 SHA 与本次精确计数（见 ``load_tests`` / ``_ReportingSuite``）。
"""
import contextlib
import hashlib
import inspect
import json
import os
from pathlib import Path
import py_compile
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest import mock

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

from orchestrator import isolation  # noqa: E402
from orchestrator import dsh_launch  # noqa: E402

#: R1 起 fixture / 临时产物只落在**本轮回执目录**下，不再写入历史 attempt 收据目录。
RECEIPT_ROOT = os.path.join(WORKSPACE_ROOT, "receipts", "task-demo-8b8adacd92",
                            "att-demo-c6d56c0d13")
FIXTURE_PREFIX = "fixture-dsh-launch-"
HOME_PREFIX = "orchestrator-worker-"
COMPILE_PREFIX = "py-compile-"

#: acceptance[2] 冻结输入（相对 workspace 的路径 -> 期望 SHA256）。
FROZEN_INPUTS = (
    ("orchestrator/isolation.py",
     "202474720ec2823084b821eec94bb00dbd6d995d7045cd5a14ac9373c14e8fe6"),
    ("orchestrator/dsh.py",
     "58d603cc531298a2f6f69be768f9d79dceb64876a5d67cc34621ba55f8eb4c62"),
    ("adapters/dsh_sdk.py",
     "a4ac283a652550713ed0533460bef678d12a4dab2840bfbc17ec44a003322b48"),
    ("scripts/d4ctl.py",
     "8603d4482a74ba9843e8e175a571183f0b9601aa2e78af760735b61fc9ac5640"),
)

#: 本次测试进程真实创建过的私有 HOME（只清理这些）。
_TRACKED_HOMES = []


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tracking_mkdtemp(real):
    def run(*args, **kwargs):
        path = real(*args, **kwargs)
        _TRACKED_HOMES.append(path)
        return path

    return run


def _cleanup_tracked_homes():
    """只删除本次测试创建的私有 HOME；名字/位置不符即拒绝删除并失败。"""
    while _TRACKED_HOMES:
        path = _TRACKED_HOMES.pop()
        resolved = os.path.realpath(path)
        if not os.path.basename(resolved).startswith(HOME_PREFIX) \
                or os.path.dirname(resolved) not in ("/private/tmp", "/tmp"):
            raise AssertionError("refusing to remove unexpected private HOME: " + resolved)
        if os.path.islink(path):
            os.unlink(path)
        elif os.path.isdir(resolved):
            shutil.rmtree(resolved)


def _build_fixture(label):
    """在 receipts 下新建**独立新目录**作为 fixture；已存在即失败，不覆盖。"""
    base = Path(RECEIPT_ROOT)
    base.mkdir(parents=True, exist_ok=True)
    root = base / (FIXTURE_PREFIX + "%d-%s-%s" % (os.getpid(), uuid.uuid4().hex[:10], label))
    os.mkdir(str(root), 0o700)
    runtime = root / "runtime"
    sdk_dir = runtime / "sdk"
    workspace = root / "workspace"
    control = root / "control"
    sdk_dir.mkdir(parents=True)
    workspace.mkdir(mode=0o700)
    control.mkdir(mode=0o700)
    node = runtime / "node"
    node.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(str(node), 0o755)
    node_noexec = runtime / "node-noexec"
    node_noexec.write_text("not executable\n", encoding="utf-8")
    os.chmod(str(node_noexec), 0o644)
    entry = sdk_dir / "main.js"
    entry.write_text("// offline fixture entry; never executed\n", encoding="utf-8")
    os.chmod(str(entry), 0o644)
    return {"root": root, "runtime": runtime, "sdk_dir": sdk_dir, "workspace": workspace,
            "control": control, "node": node, "node_noexec": node_noexec, "entry": entry}


def _remove_fixture(fixture):
    resolved = os.path.realpath(str(fixture["root"]))
    if not resolved.startswith(os.path.realpath(RECEIPT_ROOT) + os.sep) \
            or not os.path.basename(resolved).startswith(FIXTURE_PREFIX):
        raise AssertionError("refusing to remove fixture outside this attempt root: " + resolved)
    if os.path.isdir(resolved):
        shutil.rmtree(resolved)


class _FakePlatform:
    """只替换 ``orchestrator.isolation.platform`` 的按需替身（不动全局 platform 模块）。"""

    def __init__(self, system):
        self._system = system

    def system(self):
        return self._system


class _NoSandboxOS:
    """让 ``/usr/bin/sandbox-exec`` 探测失败，其余 os 调用透传真实实现。"""

    def __init__(self, real):
        self._real = real

    def access(self, path, mode, **kwargs):
        if "sandbox-exec" in str(path):
            return False
        return self._real.access(path, mode, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _OfflineCase(unittest.TestCase):
    """所有用例共用不变量：本次私有 HOME 必须被清理；任何路径都不得启动进程。"""

    def setUp(self):
        patcher = mock.patch("subprocess.Popen")
        self.popen = patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._enforce_invariants)

    def _enforce_invariants(self):
        _cleanup_tracked_homes()
        self.popen.assert_not_called()

    @contextlib.contextmanager
    def home_tracker(self):
        """追踪本次真实创建的私有 HOME（``isolation.tempfile.mkdtemp``）。"""
        with mock.patch("orchestrator.isolation.tempfile.mkdtemp",
                        side_effect=_tracking_mkdtemp(tempfile.mkdtemp)) as tracker:
            yield tracker


class _FixtureCase(_OfflineCase):
    LABEL = "fixture"

    @classmethod
    def setUpClass(cls):
        cls.fixture = _build_fixture(cls.LABEL)

    @classmethod
    def tearDownClass(cls):
        _remove_fixture(cls.fixture)

    def args(self, **overrides):
        values = {"node": str(self.fixture["node"]),
                  "sdk_entry": str(self.fixture["entry"]),
                  "workspace": str(self.fixture["workspace"]),
                  "runtime_roots": [str(self.fixture["runtime"])],
                  "state_dir": str(self.fixture["control"])}
        values.update(overrides)
        return values

    def launch(self, **overrides):
        return dsh_launch.prepare_dsh_launch(**self.args(**overrides))

    def accept(self, **overrides):
        """正向调用：真实走 isolation.prepare_launch，并追踪本次私有 HOME。"""
        with self.home_tracker() as tracker:
            spec = self.launch(**overrides)
        self.assertEqual(tracker.call_count, 1, "每个规格只应新建一个私有 HOME")
        return spec

    def reject(self, error=isolation.IsolationError, **overrides):
        """负向调用：必须拒绝，且不得新建私有 HOME。"""
        with self.home_tracker() as tracker:
            with self.assertRaises(error) as context:
                self.launch(**overrides)
        self.assertEqual(tracker.call_count, 0, "非法输入不得新建私有 HOME")
        return context.exception


class FrozenInputCase(_OfflineCase):
    """acceptance[2]：冻结输入 SHA 核验；漂移停止。"""

    def test_each_frozen_input_is_a_regular_file(self):
        for relative, _ in FROZEN_INPUTS:
            path = os.path.join(WORKSPACE_ROOT, relative)
            self.assertTrue(os.path.isfile(path), "冻结输入缺失：" + relative)

    def test_frozen_input_sha256_matches_frozen_digest(self):
        for relative, expected in FROZEN_INPUTS:
            path = os.path.join(WORKSPACE_ROOT, relative)
            actual = _file_sha256(path)
            self.assertEqual(actual, expected,
                             "冻结输入 SHA 漂移，停止：%s（期望 %s，实际 %s）"
                             % (relative, expected, actual))


class InterfaceCase(_FixtureCase):
    """acceptance[3][5][8]：接口形状、复用 relation、无进程/联网 token。"""

    LABEL = "interface"

    def test_signature_is_keyword_only_and_exact(self):
        signature = inspect.signature(dsh_launch.prepare_dsh_launch)
        parameters = list(signature.parameters.values())
        self.assertEqual([item.name for item in parameters],
                         ["node", "sdk_entry", "workspace", "runtime_roots", "state_dir", "env"])
        for item in parameters:
            self.assertEqual(item.kind, inspect.Parameter.KEYWORD_ONLY, item.name)
        self.assertIsNone(signature.parameters["env"].default)

    def test_no_positional_or_override_entrypoints(self):
        values = self.args()
        with self.home_tracker() as tracker:
            with self.assertRaises(TypeError):
                dsh_launch.prepare_dsh_launch(values["node"], values["sdk_entry"],
                                              values["workspace"], values["runtime_roots"],
                                              values["state_dir"])
            with self.assertRaises(TypeError):
                self.launch(profile="sdk-full")
            with self.assertRaises(TypeError):
                self.launch(command=["/bin/sh"])
        self.assertEqual(tracker.call_count, 0)

    def test_profile_constant_is_fixed_sdk_minimal(self):
        self.assertEqual(dsh_launch.DSH_SDK_PROFILE, "sdk-minimal")

    def test_reuses_isolation_prepare_launch_and_error_types(self):
        self.assertIs(dsh_launch.prepare_launch, isolation.prepare_launch)
        self.assertIs(dsh_launch.IsolationError, isolation.IsolationError)
        self.assertIs(dsh_launch.IsolationUnavailable, isolation.IsolationUnavailable)
        self.assertIs(dsh_launch.LaunchSpec, isolation.LaunchSpec)

    def test_source_has_no_process_or_network_tokens(self):
        source = Path(os.path.join(WORKSPACE_ROOT, "orchestrator", "dsh_launch.py")).read_text(
            encoding="utf-8")
        for token in ("subprocess", "Popen", "popen", "os.system", "spawn",
                      "socket", "urllib", "requests", "http"):
            self.assertNotIn(token, source, "新增规格模块不得包含进程/联网入口：" + token)


class PositiveLaunchCase(_FixtureCase):
    """acceptance[3][4][5][6]：正向规格内容与私有 HOME 布局。"""

    LABEL = "positive"

    def test_fixture_paths_have_no_symlink_segments(self):
        for key in ("node", "entry", "workspace", "runtime", "control", "root"):
            literal = str(self.fixture[key])
            self.assertEqual(os.path.realpath(literal), literal, key)

    def test_positive_returns_isolation_launchspec(self):
        self.assertIsInstance(self.accept(), isolation.LaunchSpec)

    def test_positive_exact_argv_is_sandboxed_and_fixed(self):
        spec = self.accept()
        self.assertEqual(list(spec.command[:3]), ["/usr/bin/sandbox-exec", "-p", spec.profile])
        self.assertEqual(list(spec.command[3:]),
                         [str(self.fixture["node"]), str(self.fixture["entry"]),
                          "--profile", "sdk-minimal"])
        self.assertEqual(spec.profile, spec.command[2])

    def test_positive_cwd_is_the_requested_workspace(self):
        spec = self.accept()
        self.assertEqual(spec.cwd, str(self.fixture["workspace"]))

    def test_positive_env_has_fixed_home_dsh_home_and_tmpdir(self):
        spec = self.accept()
        self.assertTrue(spec.home.startswith("/private/tmp/" + HOME_PREFIX), spec.home)
        self.assertEqual(spec.env["HOME"], spec.home)
        self.assertEqual(spec.env["DSH_HOME"], os.path.join(spec.home, "dsh"))
        for name in ("TMPDIR", "TMP", "TEMP"):
            self.assertEqual(spec.env[name], os.path.join(spec.home, "tmp"))
        self.assertEqual(spec.env["PATH"], "/usr/bin:/bin")
        self.assertEqual(spec.env["LANG"], "en_US.UTF-8")

    def test_positive_env_cannot_inject_credentials_or_node_options(self):
        spec = self.accept(env={"TZ": "Asia/Shanghai", "TERM": "dumb"})
        self.assertEqual(spec.env["TZ"], "Asia/Shanghai")
        self.assertEqual(spec.env["TERM"], "dumb")
        for forbidden in ("DEEPSEEK_API_KEY", "NODE_OPTIONS", "DSH_PROFILE", "AWS_SECRET"):
            self.assertNotIn(forbidden, spec.env)

    def test_positive_profile_keeps_seatbelt_rules_from_isolator(self):
        spec = self.accept()
        self.assertIn("(deny default)", spec.profile)
        self.assertIn("(deny network*)", spec.profile)
        self.assertIn("(subpath %s)" % json.dumps(str(self.fixture["runtime"])), spec.profile)
        self.assertIn("(subpath %s)" % json.dumps(str(self.fixture["workspace"])), spec.profile)
        self.assertIn("(subpath %s)" % json.dumps(spec.home), spec.profile)
        self.assertIn("(deny file-read* file-write* (subpath %s))"
                      % json.dumps(str(self.fixture["control"])), spec.profile)

    def test_positive_control_ledger_is_outside_all_allowed_roots(self):
        spec = self.accept()
        control = str(self.fixture["control"])
        for other in (str(self.fixture["workspace"]), str(self.fixture["runtime"]), spec.home):
            self.assertFalse(control == other or control.startswith(other + os.sep))
            self.assertFalse(other == control or other.startswith(control + os.sep))

    def test_positive_private_home_is_outside_allowed_roots(self):
        spec = self.accept()
        for other in (str(self.fixture["workspace"]), str(self.fixture["runtime"]),
                      str(self.fixture["control"])):
            self.assertFalse(spec.home.startswith(other + os.sep))

    def test_positive_exactly_one_home_and_zero_process(self):
        with self.home_tracker() as tracker:
            spec = self.launch()
        self.assertEqual(tracker.call_count, 1)
        self.popen.assert_not_called()
        self.assertTrue(os.path.isdir(spec.home))

    def test_positive_runtime_root_inside_user_home_is_allowed(self):
        home = Path(os.path.realpath(os.path.expanduser("~")))
        runtime = Path(os.path.realpath(str(self.fixture["runtime"])))
        if runtime == home or home in runtime.parents:
            spec = self.accept()
            self.assertTrue(spec.command[3].startswith(str(home) + os.sep))
        else:
            # fixture 在 receipts 下，若它不在本进程 HOME 内，则不能在不越权写入的
            # 前提下构造「runtime root 位于 HOME 内」的正向用例；如实跳过并写明原因。
            self.skipTest("fixture runtime %s 不在用户 HOME %s 内，本环境无法构造该正向用例"
                          % (runtime, home))


class PathShapeCase(_FixtureCase):
    """acceptance[4]：Node 与 SDK 入口的路径形状。"""

    LABEL = "pathshape"

    def test_negative_node_missing(self):
        error = self.reject(node=str(self.fixture["runtime"] / "missing-node"))
        self.assertIn("does not exist", str(error))

    def test_negative_node_is_directory(self):
        error = self.reject(node=str(self.fixture["runtime"]))
        self.assertIn("regular file", str(error))

    def test_negative_node_not_executable(self):
        error = self.reject(node=str(self.fixture["node_noexec"]))
        self.assertIn("executable", str(error))

    def test_negative_sdk_entry_missing(self):
        error = self.reject(sdk_entry=str(self.fixture["sdk_dir"] / "missing.js"))
        self.assertIn("does not exist", str(error))

    def test_negative_sdk_entry_is_directory(self):
        error = self.reject(sdk_entry=str(self.fixture["sdk_dir"]))
        self.assertIn("regular file", str(error))

    def test_negative_relative_node(self):
        error = self.reject(node="runtime/node")
        self.assertIn("absolute", str(error))

    def test_negative_relative_sdk_entry(self):
        error = self.reject(sdk_entry="main.js")
        self.assertIn("absolute", str(error))

    def test_negative_dotdot_in_node(self):
        error = self.reject(node=str(self.fixture["runtime"]) + "/../runtime/node")
        self.assertIn("..", str(error))

    def test_negative_dotdot_in_sdk_entry(self):
        error = self.reject(sdk_entry=str(self.fixture["root"]) + "/runtime/../runtime/sdk/main.js")
        self.assertIn("..", str(error))

    def test_negative_dotdot_in_workspace(self):
        error = self.reject(workspace=str(self.fixture["workspace"]) + "/..")
        self.assertIn("..", str(error))

    def test_negative_dotdot_in_state_dir(self):
        error = self.reject(state_dir=str(self.fixture["control"]) + "/../control")
        self.assertIn("..", str(error))

    def test_negative_nul_in_node(self):
        error = self.reject(node=str(self.fixture["node"]) + "\x00")
        self.assertIn("NUL", str(error))

    def test_negative_nul_in_sdk_entry(self):
        error = self.reject(sdk_entry=str(self.fixture["entry"]) + "\x00")
        self.assertIn("NUL", str(error))

    def test_negative_nul_in_state_dir(self):
        error = self.reject(state_dir=str(self.fixture["control"]) + "\x00")
        self.assertIn("NUL", str(error))

    def test_negative_nul_in_runtime_roots(self):
        error = self.reject(runtime_roots=[str(self.fixture["runtime"]) + "\x00"])
        self.assertIn("NUL", str(error))

    def test_negative_empty_path(self):
        error = self.reject(node="")
        self.assertIn("non-empty string", str(error))

    def test_negative_non_string_path(self):
        error = self.reject(sdk_entry=123)
        self.assertIn("non-empty string", str(error))

    def test_negative_runtime_roots_non_string_item(self):
        error = self.reject(runtime_roots=[123])
        self.assertIn("runtime_roots[0]", str(error))

    def test_negative_node_is_user_symlink(self):
        link = self.fixture["runtime"] / "node-link"
        os.symlink(str(self.fixture["node"]), str(link))
        error = self.reject(node=str(link))
        self.assertIn("symlink segment", str(error))

    def test_negative_sdk_entry_is_user_symlink(self):
        link = self.fixture["sdk_dir"] / "entry-link.js"
        os.symlink(str(self.fixture["entry"]), str(link))
        error = self.reject(sdk_entry=str(link))
        self.assertIn("symlink segment", str(error))

    def test_negative_sdk_entry_parent_segment_is_symlink(self):
        link = self.fixture["root"] / "linked-sdk"
        os.symlink(str(self.fixture["sdk_dir"]), str(link))
        error = self.reject(sdk_entry=str(link / "main.js"))
        self.assertIn("symlink segment", str(error))

    def test_negative_tmp_alias_is_not_a_user_symlink(self):
        missing = "/tmp/d4-missing-node-" + uuid.uuid4().hex
        error = self.reject(node=missing)
        self.assertIn("does not exist", str(error))
        self.assertNotIn("symlink", str(error))

    def test_negative_var_alias_is_not_a_user_symlink(self):
        missing = "/var/d4-missing-node-" + uuid.uuid4().hex
        error = self.reject(node=missing)
        self.assertIn("does not exist", str(error))
        self.assertNotIn("symlink", str(error))


class DirectorySymlinkCase(_FixtureCase):
    """acceptance[2][4]：workspace / state_dir / runtime_roots 也必须在任何 resolve
    之前逐段拒绝用户 symlink（父目录段与最终段都算）。"""

    LABEL = "dirsymlink"

    def test_negative_workspace_is_user_symlink(self):
        link = self.fixture["root"] / "linked-workspace"
        os.symlink(str(self.fixture["workspace"]), str(link))
        error = self.reject(workspace=str(link))
        self.assertIn("workspace must not contain a symlink segment", str(error))

    def test_negative_workspace_parent_segment_is_user_symlink(self):
        link = self.fixture["root"] / "linked-workspace-parent"
        os.symlink(str(self.fixture["workspace"]), str(link))
        error = self.reject(workspace=str(link / "nested"))
        self.assertIn("workspace must not contain a symlink segment", str(error))

    def test_negative_state_dir_is_user_symlink(self):
        link = self.fixture["root"] / "linked-control"
        os.symlink(str(self.fixture["control"]), str(link))
        error = self.reject(state_dir=str(link))
        self.assertIn("state_dir must not contain a symlink segment", str(error))

    def test_negative_state_dir_parent_segment_is_user_symlink(self):
        link = self.fixture["root"] / "linked-control-parent"
        os.symlink(str(self.fixture["control"]), str(link))
        error = self.reject(state_dir=str(link / "nested"))
        self.assertIn("state_dir must not contain a symlink segment", str(error))

    def test_negative_runtime_root_is_user_symlink(self):
        link = self.fixture["root"] / "linked-runtime"
        os.symlink(str(self.fixture["runtime"]), str(link))
        error = self.reject(runtime_roots=[str(link)])
        self.assertIn("runtime_roots[0] must not contain a symlink segment", str(error))

    def test_negative_runtime_root_parent_segment_is_user_symlink(self):
        link = self.fixture["root"] / "linked-runtime-parent"
        os.symlink(str(self.fixture["runtime"]), str(link))
        error = self.reject(runtime_roots=[str(link / "nested")])
        self.assertIn("runtime_roots[0] must not contain a symlink segment", str(error))

    def test_negative_runtime_roots_second_item_symlink_has_index_label(self):
        link = self.fixture["root"] / "linked-runtime-second"
        os.symlink(str(self.fixture["runtime"]), str(link))
        error = self.reject(runtime_roots=[str(self.fixture["runtime"]), str(link)])
        self.assertIn("runtime_roots[1] must not contain a symlink segment", str(error))

    def test_negative_broken_symlink_segment_is_rejected_before_resolve(self):
        """断裂软链接也必须在 resolve 之前拒绝（resolve 会把它当普通缺失路径放行）。"""
        link = self.fixture["root"] / "broken-control-link"
        os.symlink(str(self.fixture["root"] / "does-not-exist"), str(link))
        error = self.reject(state_dir=str(link))
        self.assertIn("state_dir must not contain a symlink segment", str(error))

    def test_negative_many_directory_symlinks_zero_home_and_zero_process(self):
        cases = (
            {"workspace": str(self.fixture["root"] / "linked-workspace")},
            {"state_dir": str(self.fixture["root"] / "linked-control")},
            {"runtime_roots": [str(self.fixture["root"] / "linked-runtime")]},
        )
        with self.home_tracker() as tracker:
            for overrides in cases:
                with self.assertRaises(isolation.IsolationError):
                    self.launch(**overrides)
        self.assertEqual(tracker.call_count, 0)
        self.popen.assert_not_called()


class SystemAliasCase(_FixtureCase):
    """acceptance[2][4]：``/tmp`` / ``/var`` 只在段名与 ``realpath`` 双双精确命中时放行。"""

    LABEL = "alias"

    def test_real_host_alias_decision_follows_pinned_realpath(self):
        """宿主事实核对：别名段是否放行，必须与其 realpath 是否精确等于固定目标一致。"""
        for alias, target in sorted(dsh_launch._SYSTEM_ALIAS_TARGETS.items()):
            cursor = Path(alias)
            if not os.path.islink(alias):
                continue
            drifted = os.path.realpath(alias) != target
            self.assertEqual(dsh_launch._is_allowed_system_alias(cursor), not drifted,
                             "%s 的放行结论与宿主 realpath 漂移状态不一致" % alias)

    def test_positive_alias_exact_target_is_allowed(self):
        """机制双向验证：段名命中且 realpath 精确匹配 -> 按系统别名放行。"""
        link = self.fixture["root"] / "alias-exact-match"
        target = self.fixture["workspace"]
        os.symlink(str(target), str(link))
        with mock.patch.dict(dsh_launch._SYSTEM_ALIAS_TARGETS,
                             {str(link): os.path.realpath(str(link))}, clear=True):
            spec = self.accept(workspace=str(link))
        self.assertEqual(spec.cwd, os.path.realpath(str(target)))

    def test_negative_alias_target_drift_is_rejected(self):
        """段名命中但 realpath 与固定目标不精确相等 -> 按用户 symlink 拒绝。"""
        link = self.fixture["root"] / "alias-drifted"
        os.symlink(str(self.fixture["workspace"]), str(link))
        with mock.patch.dict(dsh_launch._SYSTEM_ALIAS_TARGETS,
                             {str(link): "/private/not-the-alias-target"}, clear=True):
            error = self.reject(workspace=str(link))
        self.assertIn("workspace must not contain a symlink segment", str(error))

    def test_negative_alias_name_outside_pinned_table_is_rejected(self):
        """即使 realpath 指向 fixture，段名不在固定别名表里也不能例外放行。"""
        link = self.fixture["root"] / "alias-outside-table"
        os.symlink(str(self.fixture["workspace"]), str(link))
        with mock.patch.dict(dsh_launch._SYSTEM_ALIAS_TARGETS, {}, clear=True):
            error = self.reject(workspace=str(link))
        self.assertIn("workspace must not contain a symlink segment", str(error))

    def test_negative_real_tmp_alias_with_drifted_target_is_rejected(self):
        """真实 /tmp 是系统软链接时，把期望目标写错必须落在拒绝分支（realpath 真的参与判定）。"""
        if not os.path.islink("/tmp"):
            self.skipTest("宿主 /tmp 不是 symlink，本环境无法构造系统别名漂移用例")
        missing = "/tmp/d4-drift-probe-" + uuid.uuid4().hex
        with mock.patch.dict(dsh_launch._SYSTEM_ALIAS_TARGETS,
                             {"/tmp": "/private/tmp-drifted"}, clear=True):
            error = self.reject(node=missing)
        self.assertIn("node must not contain a symlink segment", str(error))

    def test_negative_real_var_alias_without_pinned_target_is_rejected(self):
        if not os.path.islink("/var"):
            self.skipTest("宿主 /var 不是 symlink，本环境无法构造系统别名漂移用例")
        missing = "/var/d4-drift-probe-" + uuid.uuid4().hex
        with mock.patch.dict(dsh_launch._SYSTEM_ALIAS_TARGETS, {}, clear=True):
            error = self.reject(state_dir=missing)
        self.assertIn("state_dir must not contain a symlink segment", str(error))


class CompileCase(_OfflineCase):
    """acceptance[4]：两个候选文件必须通过 ``py_compile`` 语法门禁。

    字节码只写本轮回执目录下的临时子目录，写完即删，不在产品目录留产物。
    """

    def test_candidate_sources_pass_py_compile(self):
        Path(RECEIPT_ROOT).mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=COMPILE_PREFIX, dir=RECEIPT_ROOT))
        try:
            for relative in ("orchestrator/dsh_launch.py", "tests/test_d4_dsh_launch.py"):
                source = os.path.join(WORKSPACE_ROOT, relative)
                self.assertTrue(os.path.isfile(source), "候选文件缺失：" + relative)
                py_compile.compile(source, cfile=str(scratch / (relative.replace("/", "_") + "c")),
                                   doraise=True)
        finally:
            resolved = os.path.realpath(str(scratch))
            if not resolved.startswith(os.path.realpath(RECEIPT_ROOT) + os.sep) \
                    or not os.path.basename(resolved).startswith(COMPILE_PREFIX):
                raise AssertionError("refusing to remove unexpected compile scratch: " + resolved)
            if os.path.isdir(resolved):
                shutil.rmtree(resolved)


class RootsCase(_FixtureCase):
    """acceptance[4][6]：runtime_roots 形状、归属与重叠。"""

    LABEL = "roots"

    def test_negative_sdk_entry_outside_runtime_roots(self):
        outside = self.fixture["root"] / "outside-entry.js"
        outside.write_text("// outside all roots\n", encoding="utf-8")
        error = self.reject(sdk_entry=str(outside))
        self.assertIn("trusted runtime root", str(error))

    def test_negative_sdk_entry_inside_workspace(self):
        inside = self.fixture["workspace"] / "inside-entry.js"
        inside.write_text("// inside writable workspace\n", encoding="utf-8")
        error = self.reject(sdk_entry=str(inside))
        self.assertIn("workspace", str(error))

    def test_negative_runtime_roots_empty(self):
        error = self.reject(runtime_roots=[])
        self.assertIn("must not be empty", str(error))

    def test_negative_runtime_roots_is_string(self):
        error = self.reject(runtime_roots=str(self.fixture["runtime"]))
        self.assertIn("sequence", str(error))

    def test_negative_runtime_roots_relative(self):
        error = self.reject(runtime_roots=["runtime"])
        self.assertIn("absolute", str(error))

    def test_negative_runtime_roots_is_filesystem_root(self):
        error = self.reject(runtime_roots=["/"])
        self.assertIn("filesystem root", str(error))

    def test_negative_runtime_roots_is_user_home(self):
        error = self.reject(runtime_roots=[os.path.expanduser("~")])
        self.assertIn("user HOME", str(error))

    def test_negative_runtime_roots_is_user_home_ancestor(self):
        error = self.reject(runtime_roots=[os.path.dirname(os.path.expanduser("~"))])
        self.assertIn("user HOME", str(error))

    def test_negative_runtime_roots_overlaps_workspace(self):
        error = self.reject(runtime_roots=[str(self.fixture["workspace"])])
        self.assertIn("overlap the workspace", str(error))

    def test_negative_runtime_roots_contains_workspace(self):
        error = self.reject(runtime_roots=[str(self.fixture["root"])])
        self.assertIn("overlap the workspace", str(error))

    def test_negative_runtime_roots_overlaps_state_dir(self):
        error = self.reject(runtime_roots=[str(self.fixture["control"])])
        self.assertIn("overlap the control state_dir", str(error))

    def test_negative_runtime_roots_duplicate(self):
        error = self.reject(runtime_roots=[str(self.fixture["runtime"]),
                                           str(self.fixture["runtime"])])
        self.assertIn("duplicates", str(error))

    def test_negative_workspace_overlaps_state_dir(self):
        error = self.reject(state_dir=str(self.fixture["workspace"]))
        self.assertIn("disjoint from the workspace", str(error))


class EnvCase(_FixtureCase):
    """acceptance[5]：只允许 locale/timezone/terminal env。"""

    LABEL = "env"

    def test_negative_env_api_token_rejected(self):
        error = self.reject(env={"DEEPSEEK_API_KEY": "not-a-real-token"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_node_options_rejected(self):
        error = self.reject(env={"NODE_OPTIONS": "--max-old-space-size=64"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_path_override_rejected(self):
        error = self.reject(env={"PATH": "/tmp/attacker"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_home_override_rejected(self):
        error = self.reject(env={"HOME": "/tmp/attacker-home"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_dsh_home_override_rejected(self):
        error = self.reject(env={"DSH_HOME": "/tmp/attacker-dsh"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_tmpdir_override_rejected(self):
        error = self.reject(env={"TMPDIR": "/tmp/attacker-tmp"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_dsh_profile_override_rejected(self):
        error = self.reject(env={"DSH_PROFILE": "full"})
        self.assertIn("whitelist", str(error))

    def test_negative_env_value_nul_rejected(self):
        error = self.reject(env={"TZ": "UTC\x00"})
        self.assertIn("invalid environment value", str(error))

    def test_negative_env_value_not_string(self):
        error = self.reject(env={"TZ": 8})
        self.assertIn("invalid environment value", str(error))

    def test_negative_env_not_a_mapping(self):
        error = self.reject(env=[("TZ", "UTC")])
        self.assertIn("mapping", str(error))


class NoFallbackCase(_FixtureCase):
    """acceptance[3][6][7]：不 fallback、异常原样透传、零新建 HOME / 零进程。"""

    LABEL = "nofallback"

    def test_prepare_launch_called_once_with_fixed_argv(self):
        with self.home_tracker() as tracker:
            with mock.patch.object(dsh_launch, "prepare_launch",
                                   side_effect=isolation.IsolationError("stop")) as fake:
                with self.assertRaises(isolation.IsolationError):
                    self.launch(env={"TZ": "Asia/Shanghai"})
        self.assertEqual(fake.call_count, 1)
        args, kwargs = fake.call_args
        self.assertEqual(kwargs, {})
        self.assertEqual(list(args[0]), [str(self.fixture["node"]), str(self.fixture["entry"]),
                                         "--profile", "sdk-minimal"])
        self.assertEqual(args[1], str(self.fixture["workspace"]))
        self.assertEqual(list(args[2]), [str(self.fixture["runtime"])])
        self.assertEqual(args[3], str(self.fixture["control"]))
        self.assertEqual(dict(args[4]), {"TZ": "Asia/Shanghai"})
        self.assertEqual(tracker.call_count, 0)

    def test_prepare_unavailable_is_propagated_unmodified(self):
        error = isolation.IsolationUnavailable("simulated: sandbox-exec unavailable")
        with self.home_tracker() as tracker:
            with mock.patch.object(dsh_launch, "prepare_launch", side_effect=error):
                with self.assertRaises(isolation.IsolationUnavailable) as context:
                    self.launch()
        self.assertIs(context.exception, error)
        self.assertEqual(tracker.call_count, 0)

    def test_prepare_isolation_error_is_propagated_unmodified(self):
        error = isolation.IsolationError("simulated: path rejected")
        with self.home_tracker() as tracker:
            with mock.patch.object(dsh_launch, "prepare_launch", side_effect=error):
                with self.assertRaises(isolation.IsolationError) as context:
                    self.launch()
        self.assertIs(context.exception, error)
        self.assertEqual(tracker.call_count, 0)

    def test_non_sandbox_spec_from_prepare_is_refused(self):
        bogus = isolation.LaunchSpec(
            command=[str(self.fixture["node"]), str(self.fixture["entry"]),
                     "--profile", "sdk-minimal"],
            cwd=str(self.fixture["workspace"]),
            env={"HOME": "/tmp/attacker"}, home="/tmp/attacker", profile="")
        with self.home_tracker() as tracker:
            with mock.patch.object(dsh_launch, "prepare_launch", return_value=bogus):
                with self.assertRaises(isolation.IsolationError) as context:
                    self.launch()
        self.assertIn("sandbox", str(context.exception))
        self.assertEqual(tracker.call_count, 0)

    def test_negative_non_macos_host_raises_unavailable(self):
        with mock.patch("orchestrator.isolation.platform", _FakePlatform("Linux")):
            error = self.reject(error=isolation.IsolationUnavailable)
        self.assertIs(type(error), isolation.IsolationUnavailable)

    def test_negative_missing_sandbox_exec_raises_unavailable(self):
        with mock.patch("orchestrator.isolation.os", _NoSandboxOS(os)):
            error = self.reject(error=isolation.IsolationUnavailable)
        self.assertIs(type(error), isolation.IsolationUnavailable)

    def test_negative_many_invalid_inputs_zero_home_and_zero_process(self):
        cases = (
            {"node": "node"},
            {"sdk_entry": "main.js"},
            {"runtime_roots": []},
            {"env": {"NODE_OPTIONS": "--max-old-space-size=64"}},
            {"node": str(self.fixture["runtime"] / "missing")},
            {"state_dir": str(self.fixture["workspace"])},
            {"sdk_entry": str(self.fixture["root"] / "nowhere.js")},
        )
        with self.home_tracker() as tracker:
            for overrides in cases:
                with self.assertRaises(isolation.IsolationError):
                    self.launch(**overrides)
        self.assertEqual(tracker.call_count, 0)
        self.popen.assert_not_called()


def _iter_cases(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            for inner in _iter_cases(test):
                yield inner
        else:
            yield test


class _ReportingSuite(unittest.TestSuite):
    """acceptance[8]：打印新增源码 SHA 与本次精确计数。"""

    def run(self, result, debug=False):
        # 计数必须在运行前取：TestSuite.run 会把已运行的子项置空（清理引用）。
        cases = len(list(_iter_cases(self)))
        before = (result.testsRun, len(result.failures), len(result.errors),
                  len(getattr(result, "skipped", ())))
        outcome = super(_ReportingSuite, self).run(result, debug)
        after = (result.testsRun, len(result.failures), len(result.errors),
                 len(getattr(result, "skipped", ())))
        ran, failures, errors = after[0] - before[0], after[1] - before[1], after[2] - before[2]
        skipped = after[3] - before[3]
        lines = []
        for relative in ("orchestrator/dsh_launch.py", "tests/test_d4_dsh_launch.py"):
            lines.append("[D4-DSH-LAUNCH] source sha256 %s = %s"
                         % (relative, _file_sha256(os.path.join(WORKSPACE_ROOT, relative))))
        lines.append("[D4-DSH-LAUNCH] cases=%d testsRun=%d passed=%d failures=%d errors=%d skipped=%d"
                     % (cases, ran, ran - failures - errors - skipped, failures, errors, skipped))
        lines.append("[D4-DSH-LAUNCH] env home=%s tempdir=%s cwd=%s"
                     % (os.path.expanduser("~"), tempfile.gettempdir(), os.getcwd()))
        lines.append("[D4-DSH-LAUNCH] system aliases islink(/tmp)=%s islink(/var)=%s"
                     % (os.path.islink("/tmp"), os.path.islink("/var")))
        print("\n".join(lines))
        sys.stdout.flush()
        return outcome


def load_tests(loader, tests, pattern):
    return _ReportingSuite(tests)
