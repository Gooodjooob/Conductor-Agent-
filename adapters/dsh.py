"""DSH（DeepSeek Harness）D4 适配器 —— 任务卡 D4-DSH-ADAPTER-001 §5.1。

## Research Gate 结论如何约束本模块（证据见
`.conductor-local/d4-real-chain-validation/dsh-adapter-001/evidence/research-gate.md`）

对 `@deepseek-ai/dsh@0.1.1-rc.2`（`/Users/demo/Documents/dsh-2`）实测：

1. **没有 stdio JSON-RPC 入口**。旧 `orchestrator/dsh.py` 依赖的
   `initialize` → `serverInfo.name == "deepseek-harness-sdk-runtime"` 握手在本安装中
   没有匹配（全量 `grep` 为空）。
2. 唯一非交互、可脚本化的入口是 `dsh --profile headless "<task>"`；它**只把最终助手
   文本写到 stdout 然后退出**，**不回显 sessionId / runId / 任何宿主接收凭证**，
   也不提供 started / progress / blocked 分项回执；退出码 1 同时覆盖
   "usage error" 与 "turn 未 completed" 两种完全不同的情况。
3. headless 使用模型 provider，**必需联网**；而 D4 验证切片冻结的
   `permissions` 强制 `allowNetwork=false`。

因此本模块的诚实能力边界是：

- `submitted` **可以**真实产生（请求确实发出了）；
- `accepted_by_host` **不能**在本版本产生 —— 没有接收凭证就绝不写
  `accepted_by_host_at`；
- `delivered` **不由适配器产生**：D4 `EventService` 要求
  `submitted_at` + `accepted_by_host_at` 同时存在才允许交付，本版本该路径被关闭。

## 硬边界（对齐卡片 §5.1 / §7）

- 只以 **argv 数组**启动 DSH，`shell=False`；绝不拼接 shell 字符串。
- 复用 D4 投递消息合同（`build_task_message`）固定注入 taskId / attemptId /
  workspace / 权限 / 验收要求；身份只来自受控常量，不从 task/payload 自报读取。
- 版本门禁：`dsh --version` 必须精确等于支持基线，否则 `DSH_VERSION_UNSUPPORTED`
  fail closed，不放行任何 dispatch。
- 超时 / 断线 / 非零退出 / 无效输出 / 响应未知一律映射为 `submit_unknown`，
  **不自动重试**；同一 attempt 重复提交只回放第一次结果。
- 原始帧脱敏落盘（长度 + sha256 + 首行截断），模型文本**不**写入 D4 可信状态字段。
- 交付路径必须落在该 attempt 的 workspace 内；拒绝绝对路径逃逸、`..` 逃逸、
  符号链接逃逸与非 regular file。

## 未接线说明

本模块**未**接入 `scripts/d4ctl.py`（该文件不在本卡 §6 允许修改范围内）。新增 DSH
来源需要 `ALLOWED_SOURCES` 与受控路由改动，属 cox 裁定项，见实现报告。
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
from pathlib import Path

from adapters.base import AdapterCapabilities, AdapterResult, HostAdapter
from adapters.qclaw_gateway import build_task_message
from contracts.paths import assert_within_scope, resolve_scopes, resolve_within
from contracts.schemas import ContractError

# -- 受控常量（不可由调用者覆盖）---------------------------------------------
#: 首版唯一支持基线（卡片 §2.1）。不是这个版本一律 fail closed。
SUPPORTED_DSH_VERSIONS = ("0.1.1-rc.2",)
#: 受控来源 id。加入 CLI 白名单需另卡，本模块只声明常量。
DSH_SOURCE_ID = "dsh-headless"
#: D4 登记的宿主执行成员键（受控常量映射）。构造函数不提供覆盖入口。
D4_DSH_AGENT_ID = "dsh-headless-exec-001"
#: 唯一允许的 profile（0.1.1-rc.2 的非交互入口）。
DSH_PROFILE = "headless"

DEFAULT_TIMEOUT = 900.0
DEFAULT_VERSION_TIMEOUT = 20.0
MAX_OUTPUT_BYTES = 32 * 1024 * 1024
MAX_ARTIFACT_BYTES = 16 * 1024 * 1024
_FIRST_LINE_LIMIT = 120

# -- transport 层错误码 ------------------------------------------------------
# 说明：这些码目前**只作为 adapter 局部 `dshCode` 放进异常 details**，未写入冻结的
# `contracts/schemas.py::ERROR_CODES`（未经 cox review 不改冻结合同）。
DSH_VERSION_UNSUPPORTED = "DSH_VERSION_UNSUPPORTED"
DSH_LAUNCH_FAILED = "DSH_LAUNCH_FAILED"
DSH_TIMEOUT = "DSH_TIMEOUT"
DSH_EXIT_NONZERO = "DSH_EXIT_NONZERO"
DSH_OUTPUT_TOO_LARGE = "DSH_OUTPUT_TOO_LARGE"
DSH_EMPTY_OUTPUT = "DSH_EMPTY_OUTPUT"
DSH_INVALID_RECEIPT = "DSH_INVALID_RECEIPT"
DSH_RECEIPT_MISMATCH = "DSH_RECEIPT_MISMATCH"
DSH_HOST_IDENTITY_MISMATCH = "DSH_HOST_IDENTITY_MISMATCH"
DSH_NETWORK_REQUIRED_BUT_FORBIDDEN = "DSH_NETWORK_REQUIRED_BUT_FORBIDDEN"
DSH_ATTEMPT_CONFLICT = "DSH_ATTEMPT_CONFLICT"
DSH_STORE_NOT_PRIVATE = "DSH_STORE_NOT_PRIVATE"
DSH_ARTIFACT_INVALID = "DSH_ARTIFACT_INVALID"


class DshTransportError(RuntimeError):
    """transport 层错误（版本不符/启动失败/超时/非零退出/输出非法）。

    调用方 fail closed：不写乐观状态、不自动重试。
    """

    def __init__(self, code, message=None, details=None):
        self.code = code
        self.details = dict(details or {})
        super().__init__("%s%s" % (code, (": %s" % message) if message else ""))


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_version(text):
    """把 `dsh --version` 输出规范化为版本号；读不到则返回 None。

    只接受单行、纯版本号（可选 `v` 前缀 / 三段落 + 可选预发布后缀）。
    多余文本不做模糊匹配——版本门禁不能靠猜。
    """
    if not isinstance(text, str):
        return None
    for raw in text.splitlines():
        line = raw.strip().lstrip("v")
        if not line:
            continue
        head = line.split()[0]
        parts = head.split(".")
        if len(parts) < 2 or not all(part for part in parts):
            continue
        stem = parts[0].split("-")[0] + parts[1].split("-")[0]
        if not stem.isdigit():
            continue
        return head
    return None


def _truncate(text, limit=_FIRST_LINE_LIMIT):
    """脱敏：折叠空白、截断首行。用于摘要，不用于可信字段。"""
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    return collapsed[:limit]


def frame_summary(stdout, stderr, exit_code, timed_out):
    """原始帧的**脱敏摘要**（不进 argv、不进可信状态字段）。

    保留结构判定所需的长度与摘要；模型文本只留 sha256 与前 120 字符。
    """
    out = stdout if isinstance(stdout, str) else ""
    err = stderr if isinstance(stderr, str) else ""
    return {
        "stdoutBytes": len(out.encode("utf-8")),
        "stdoutSha256": _sha256_text(out),
        "stdoutFirstLine": _truncate(out),
        "stderrBytes": len(err.encode("utf-8")),
        "stderrSha256": _sha256_text(err),
        "stderrFirstLine": _truncate(err),
        "exitCode": exit_code,
        "timedOut": bool(timed_out),
    }


def _private_dir(path, code):
    """校验/创建当前用户私有目录（0700），否则 fail closed。"""
    target = Path(path).absolute()
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = target.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise DshTransportError(
            code,
            message="目录必须是当前用户私有目录（0700）",
            details={"path": str(target)},
        )
    return target


def _atomic_private_write(directory, name, text):
    """在私有目录内写 0600 文件（先写临时名再 rename，避免半截文件）。"""
    directory = Path(directory)
    final = directory / name
    tmp = directory / ("%s.tmp-%d" % (name, os.getpid()))
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(final))
    return final


class DshHeadlessTransport:
    """`dsh --profile headless` 的受控 argv transport。

    - `command` 必须是 argv 数组，`command[0]` 必须是绝对路径（通常 `node`）；
      本类不提供 shell 字符串入口。
    - 每次调用都从 `argv` 数组直接 `exec`，`shell=False`；任务文本作为**位置参数**
      传入，不经 shell 解释。
    - `requires_network`：0.1.1-rc.2 headless 经模型 provider 执行，**必需联网**。
      适配器据此在 D4 声明 `allowNetwork=false` 时拒绝 dispatch。
    - `receipt_mode = "none"`：本版本不回显任何运行编号，**不能**产生
      `accepted_by_host`。离线夹具可声明 `receipt_mode="runid"` 以覆盖未来的合规宿主。
    - `evidence_level`：真实宿主本轮**未做 canary**，如实自报 `UNVERIFIED`。
    """

    evidence_level = "UNVERIFIED"
    requires_network = True
    receipt_mode = "none"

    def __init__(self, command, workspace, store_dir, *, dsh_home=None,
                 timeout=DEFAULT_TIMEOUT, version_timeout=DEFAULT_VERSION_TIMEOUT,
                 env=None, max_output=MAX_OUTPUT_BYTES,
                 expected_versions=SUPPORTED_DSH_VERSIONS):
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("DSH_COMMAND_REQUIRED")
        if not all(isinstance(part, str) and part for part in command):
            raise ValueError("DSH_COMMAND_PARTS_MUST_BE_NONBLANK_STRINGS")
        if not Path(command[0]).is_absolute():
            raise ValueError("DSH_COMMAND_MUST_BE_ABSOLUTE")
        self.command = list(command)
        try:
            self.workspace = Path(workspace).resolve(strict=True)
        except OSError as exc:
            raise ValueError("DSH_WORKSPACE_NOT_RESOLVABLE") from exc
        self.store = _private_dir(store_dir, DSH_STORE_NOT_PRIVATE)
        self.timeout = float(timeout)
        self.version_timeout = float(version_timeout)
        self.max_output = int(max_output)
        self.expected_versions = tuple(str(v) for v in expected_versions)
        if not self.expected_versions:
            raise ValueError("DSH_EXPECTED_VERSIONS_REQUIRED")
        self.env = dict(env or {})
        # 显式 HOME：不继承调用者环境，避免把用户凭据/配置泄漏进子进程。
        if not isinstance(self.env.get("HOME"), str) or not self.env["HOME"]:
            raise ValueError("DSH_EXPLICIT_ENV_HOME_REQUIRED")
        if dsh_home is not None:
            if not isinstance(dsh_home, str) or not Path(dsh_home).is_absolute():
                raise ValueError("DSH_HOME_MUST_BE_ABSOLUTE")
            self.env["DSH_HOME"] = dsh_home
        self.dsh_home = dsh_home
        self.frames_dir = _private_dir(self.store / "frames", DSH_STORE_NOT_PRIVATE)

    # -- argv（数组，永不拼接字符串）-----------------------------------------
    def version_argv(self):
        return list(self.command) + ["--version"]

    def argv(self, task_text):
        if not isinstance(task_text, str) or not task_text.strip():
            raise ValueError("DSH_TASK_TEXT_REQUIRED")
        return list(self.command) + ["--profile", DSH_PROFILE, task_text]

    # -- 进程执行 -----------------------------------------------------------
    def _run_argv(self, argv, timeout):
        try:
            child = subprocess.Popen(
                argv, cwd=str(self.workspace), env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, close_fds=True, start_new_session=True,
            )
        except OSError as exc:
            raise DshTransportError(
                DSH_LAUNCH_FAILED, message=type(exc).__name__,
                details={"argv0": argv[0], "error": type(exc).__name__},
            ) from exc
        timed_out = False
        try:
            out, err = child.communicate(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            # 宿主静默不输出时不能等：由看门狗路径强制回收整个进程组。
            timed_out = True
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, err = child.communicate()
        if len(out) + len(err) > self.max_output:
            raise DshTransportError(
                DSH_OUTPUT_TOO_LARGE,
                details={"bytes": len(out) + len(err), "limit": self.max_output},
            )
        return {
            "argv": list(argv),
            "stdout": out.decode("utf-8", errors="replace"),
            "stderr": err.decode("utf-8", errors="replace"),
            "exitCode": child.returncode,
            "timedOut": timed_out,
        }

    def probe_version(self):
        """核对版本；不是支持基线一律 `DSH_VERSION_UNSUPPORTED` fail closed。"""
        run = self._run_argv(self.version_argv(), self.version_timeout)
        if run["timedOut"]:
            raise DshTransportError(
                DSH_TIMEOUT, message="版本探测超时",
                details={"timeoutSeconds": self.version_timeout},
            )
        if run["exitCode"] != 0:
            raise DshTransportError(
                DSH_LAUNCH_FAILED,
                message="版本探测退出码非 0",
                details={"exitCode": run["exitCode"],
                         "stderrFirstLine": _truncate(run["stderr"])},
            )
        version = normalize_version(run["stdout"])
        if version is None:
            raise DshTransportError(
                DSH_VERSION_UNSUPPORTED, message="版本输出无法解析",
                details={"stdoutFirstLine": _truncate(run["stdout"]),
                         "supported": list(self.expected_versions)},
            )
        if version not in self.expected_versions:
            raise DshTransportError(
                DSH_VERSION_UNSUPPORTED,
                message="DSH 版本不受支持，fail closed",
                details={"actual": version, "supported": list(self.expected_versions),
                         "hint": "不得把别的版本当作兼容版本继续执行"},
            )
        return version

    def run(self, task_text, attempt_id):
        """一次性投递并采集原始帧。

        返回 dict：`argv` / `stdout` / `stderr` / `exitCode` / `timedOut` /
        `bytes` / `version` / `receipt` / `frameDigest` / `framePath` / `summaryPath`。

        `receipt` 恒为 None（0.1.1-rc.2 不回显运行编号）。带 `receipt_mode="runid"`
        的合规宿主实现才可能返回 receipt；本类不伪造。
        """
        version = self.probe_version()
        run = self._run_argv(self.argv(task_text), self.timeout)
        summary = frame_summary(run["stdout"], run["stderr"], run["exitCode"], run["timedOut"])
        safe = dict(summary, version=version, profile=DSH_PROFILE,
                    command0=str(self.command[0]), receiptMode=self.receipt_mode)
        raw_text = _json({
            "argv": run["argv"],
            "stdout": run["stdout"],
            "stderr": run["stderr"],
            "exitCode": run["exitCode"],
            "timedOut": run["timedOut"],
        })
        frame_path = _atomic_private_write(self.frames_dir, "%s.frames.json" % attempt_id, raw_text)
        summary_path = _atomic_private_write(self.frames_dir, "%s.summary.json" % attempt_id, _json(safe))
        return {
            "argv": run["argv"],
            "stdout": run["stdout"],
            "stderr": run["stderr"],
            "exitCode": run["exitCode"],
            "timedOut": run["timedOut"],
            "bytes": summary["stdoutBytes"] + summary["stderrBytes"],
            "version": version,
            "receipt": None,
            "frameDigest": summary["stdoutSha256"],
            "framePath": str(frame_path),
            "summaryPath": str(summary_path),
            "summary": safe,
        }


def validate_dsh_receipt(receipt, expected_run_id, expected_host_agent):
    """校验合规宿主的回执；不满足合同即 `host_unconfirmed`，绝不乐观采信。

    采信条件（缺一即拒绝）：
    - receipt 是对象；
    - `runId` 非空且与本次 attempt 幂等键一致（防串号）；
    - `status` 属于 accepted / in_flight；
    - 若回执带 `hostAgentId`，必须等于受控常量映射的宿主成员键。
    终态快照（completed 等）不是接收证据。
    """
    if not isinstance(receipt, dict):
        raise ContractError(
            "host_unconfirmed",
            message="DSH 回执必须是对象，不予采信",
            details={"dshCode": DSH_INVALID_RECEIPT, "got": type(receipt).__name__},
        )
    run_id = receipt.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ContractError(
            "host_unconfirmed",
            message="DSH 回执缺少 runId，不予采信",
            details={"dshCode": DSH_INVALID_RECEIPT, "reason": "run_id_missing"},
        )
    if run_id != expected_run_id:
        raise ContractError(
            "host_unconfirmed",
            message="DSH 回执 runId 与本次 attempt 不一致，不予采信",
            details={"dshCode": DSH_RECEIPT_MISMATCH,
                     "expected": expected_run_id, "got": run_id},
        )
    host_agent = receipt.get("hostAgentId")
    if host_agent is not None and host_agent != expected_host_agent:
        raise ContractError(
            "host_unconfirmed",
            message="DSH 回执宿主成员键与受控映射不一致，不予采信",
            details={"dshCode": DSH_HOST_IDENTITY_MISMATCH,
                     "expected": expected_host_agent, "got": host_agent},
        )
    status = receipt.get("status")
    if status not in ("accepted", "in_flight"):
        raise ContractError(
            "host_unconfirmed",
            message="DSH 回执 status %r 不能记为宿主已接收" % (status,),
            details={"dshCode": DSH_INVALID_RECEIPT, "status": status},
        )
    return run_id, status


class DshAdapter(HostAdapter):
    """把 D4 任务投递给 `dsh --profile headless`，并如实回传真实事实。

    本切片只实现 `submit`：
    - 版本门禁 → 权限/网络门禁 → 幂等 → dispatch → 回执判定；
    - **恒不写** `accepted_by_host_at`（0.1.1-rc.2 无接收凭证）；
    - `wait_for_terminal` / `list_host_tasks` / `fetch_history` 仅接口预留，fail closed。
    """

    name = "dsh-headless"

    def __init__(self, transport, store_dir, *, source_id=DSH_SOURCE_ID,
                 security_sink=None, timeout_seconds=None):
        if transport is None or not callable(getattr(transport, "run", None)):
            raise ValueError("DSH_TRANSPORT_REQUIRED")
        if not callable(getattr(transport, "probe_version", None)):
            raise ValueError("DSH_TRANSPORT_MUST_SUPPORT_VERSION_PROBE")
        if source_id != DSH_SOURCE_ID:
            raise ValueError("DSH_SOURCE_ID_FIXED")
        if security_sink is not None and not callable(security_sink):
            raise ValueError("DSH_SECURITY_SINK_MUST_BE_CALLABLE")
        self.transport = transport
        self.source_id = DSH_SOURCE_ID
        # 受控常量映射：构造函数无 agentId 覆盖入口；传 agent_id 会直接 TypeError。
        self.agent_id = D4_DSH_AGENT_ID
        self.store = _private_dir(store_dir, DSH_STORE_NOT_PRIVATE)
        self.submissions_dir = _private_dir(self.store / "submissions", DSH_STORE_NOT_PRIVATE)
        self.security_sink = security_sink
        self.timeout_seconds = timeout_seconds
        self._last_version = None
        self._replay = {}

    # -- 能力自报（如实，不夸大）--------------------------------------------
    def capabilities(self):
        receipt_mode = str(getattr(self.transport, "receipt_mode", "none"))
        requires_network = bool(getattr(self.transport, "requires_network", True))
        return AdapterCapabilities(
            submit=True,
            # 0.1.1-rc.2 无接收凭证：只有 receipt_mode 被显式声明为 runid 才可能接通。
            host_acceptance=receipt_mode == "runid",
            stable_ids=False,
            restart_recovery=False,
            evidence_level="UNVERIFIED",
            notes=(
                "DSH 0.1.1-rc.2 无 stdio JSON-RPC；唯一入口 `dsh --profile headless <task>` "
                "只输出最终助手文本，不回显 sessionId/runId，也无 started/progress/blocked 回执。"
                "receipt_mode=%s；requires_network=%s（D4 验证切片冻结 allowNetwork=false，"
                "因此当前版本无法 dispatch）。真实宿主本机未做 canary。"
                "wait_for_terminal/list_host_tasks/fetch_history 仅接口预留。"
                % (receipt_mode, requires_network)
            ),
        )

    # -- 提交 ---------------------------------------------------------------
    def submit(self, task, attempt):
        self._validate_attempt_binding(task, attempt)
        attempt_id = attempt["attempt_id"]

        # 1) 版本门禁（dispatch 前）：不是支持基线一律不放行。
        try:
            version = self.transport.probe_version()
        except DshTransportError as exc:
            self._security("dsh.version_rejected", {"code": exc.code, **exc.details})
            raise ContractError(
                "invalid_transition",
                message="DSH 版本门禁未通过，未 dispatch：%s" % exc,
                details={"dshCode": exc.code, **exc.details},
            ) from exc
        self._last_version = version

        # 2) 防御性权限复核（workspace/writeScope 声明层 + 网络门禁）。
        self._preflight_permissions(task)

        # 2) 幂等：同一 attempt 重复提交只回放第一次结果，不再次启动 DSH。
        message = self._build_message(task, attempt)
        digest = _sha256_text(message)
        prior = self._load_submission(attempt_id)
        if prior is not None:
            if prior.get("requestDigest") != digest:
                raise ContractError(
                    "invalid_transition",
                    message="同一 attempt 的提交内容与首次不一致，拒绝重新投递",
                    details={"dshCode": DSH_ATTEMPT_CONFLICT, "attemptId": attempt_id},
                )
            self._security("dsh.submission_replayed", {"attemptId": attempt_id})
            return AdapterResult(
                status=prior["status"], source_id=self.source_id,
                host_receipt=prior.get("runId"),
                note=prior.get("note", ""), evidence_level=prior.get("evidenceLevel", "UNVERIFIED"),
            )

        # 3) dispatch（事务外、一次性、不重试）。
        try:
            run = self.transport.run(message, attempt_id)
        except DshTransportError as exc:
            self._security("dsh.submit_unknown", {"code": exc.code, **exc.details})
            raise ContractError(
                "submit_unknown",
                message="DSH 提交结果未知，不自动重试：%s" % exc,
                details={"dshCode": exc.code, "attemptId": attempt_id, **exc.details},
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 外部调用异常一律记为未知，不自动重试
            self._security("dsh.submit_unknown",
                           {"code": type(exc).__name__, "attemptId": attempt_id})
            raise ContractError(
                "submit_unknown",
                message="DSH 提交过程中异常，结果未知，不自动重试",
                details={"dshCode": DSH_LAUNCH_FAILED, "attemptId": attempt_id,
                         "error": type(exc).__name__},
            ) from exc

        summary = run.get("summary") or {}
        base_detail = {
            "attemptId": attempt_id,
            "version": run.get("version") or version,
            "profile": DSH_PROFILE,
            "exitCode": run.get("exitCode"),
            "timedOut": bool(run.get("timedOut")),
            "frameDigest": run.get("frameDigest"),
            "summaryPath": run.get("summaryPath"),
            "hostSessionId": None,
            "sourceRunId": None,
            "stdoutBytes": summary.get("stdoutBytes"),
            "stderrBytes": summary.get("stderrBytes"),
        }

        # 4) 失败面统一 fail closed：超时 / 非零退出 / 空输出都不能当成功。
        if base_detail["timedOut"]:
            self._security("dsh.submit_unknown", {"code": DSH_TIMEOUT, **base_detail})
            raise ContractError(
                "submit_unknown",
                message="DSH 执行超时，结果未知，不自动重试",
                details={"dshCode": DSH_TIMEOUT, **base_detail},
            )
        if base_detail["exitCode"] != 0:
            self._security("dsh.submit_unknown", {"code": DSH_EXIT_NONZERO, **base_detail})
            raise ContractError(
                "submit_unknown",
                message="DSH 退出码非 0，无法区分“未执行”与“已执行但未完成”，不自动重试",
                details={"dshCode": DSH_EXIT_NONZERO, **base_detail},
            )

        # 5) 回执判定：有 receipt 才可能 accepted_by_host；无 receipt 如实停在 submitted。
        receipt = run.get("receipt")
        if receipt is None:
            detail = dict(base_detail, acceptedByHost=False,
                          reason="receipt_mode=none：0.1.1-rc.2 不回显运行编号")
            self._persist_submission(attempt_id, digest, "submitted", None, detail, "UNVERIFIED")
            self._security("dsh.submitted_without_host_receipt", detail)
            return AdapterResult(
                status="submitted", source_id=self.source_id, host_receipt=None,
                note=_json(detail), evidence_level="UNVERIFIED",
            )
        run_id, status = validate_dsh_receipt(receipt, attempt_id, self.agent_id)
        level = str(getattr(self.transport, "evidence_level", "UNVERIFIED"))
        if level not in ("OFFICIAL", "LOCAL"):
            raise ContractError(
                "host_unconfirmed",
                message="DSH 接收凭证证据等级不足，不能记为已接收",
                details={"dshCode": DSH_INVALID_RECEIPT, "evidenceLevel": level},
            )
        detail = dict(base_detail, hostSessionId=receipt.get("sessionId"),
                      sourceRunId=run_id, acceptedByHost=True, hostStatus=status)
        self._persist_submission(attempt_id, digest, "accepted_by_host", run_id, detail, level)
        self._security("dsh.accepted_by_host", detail)
        return AdapterResult(
            status="accepted_by_host", source_id=self.source_id, host_receipt=run_id,
            note=_json(detail), evidence_level=level,
        )

    # -- 产物收集与交付 payload ---------------------------------------------
    def collect_artifacts(self, task, attempt, entries, *, quiesced=False):
        """按声明层合同校验并摘要一组产物，返回 `[{path, sha256, size}]`。

        拒绝：workspace 外、`..` 逃逸、绝对路径逃逸、符号链接、非 regular file、
        硬链接（nlink != 1）、单文件超限、路径重复、读取期间内容变化。

        P1 修复（D4-DSH-02-SDK-001 §5.4）：首次交付不再依赖 D4 `terminal_at`。
        本方法的职责是“宿主本轮已停稳后核验产物”，不是“D4 交付已入账”——
        `terminal_at` 由 EventService 在 `task.delivered` 成功后写入，
        首次交付前它必然为 None。改为接受宿主停稳事实（quiesced=True 表示
        调用方已证明宿主进程结束/事件流关闭），不再要求 D4 终态。
        旧签名（不传 quiesced）保持兼容：默认视为未证明停稳，拒绝收集。
        """
        self._validate_attempt_binding(task, attempt)
        if not quiesced:
            raise ContractError(
                "invalid_transition",
                message="宿主运行未证明停稳，不能收集产物",
                details={"attemptId": attempt.get("attempt_id"), "quiesced": False},
            )
        if attempt.get("terminal_at") is not None and attempt.get("terminal_kind") != "delivered":
            # 已结束且不是 delivered（如返工后旧轮次）：只读拒绝，不改状态。
            raise ContractError(
                "invalid_transition",
                message="执行轮次已以其他方式结束，不能收集产物",
                details={"attemptId": attempt.get("attempt_id"),
                         "terminalKind": attempt.get("terminal_kind")},
            )
        workspace = task["workspace"]
        permissions = self._load_permissions(task)
        scopes = resolve_scopes(workspace, permissions.get("writeScope") or [])
        manifest, seen = [], set()
        for entry in entries or []:
            target = assert_within_scope(workspace, permissions.get("writeScope") or [], entry)
            if target in seen:
                raise ContractError(
                    "path_escape", message="产物路径重复",
                    details={"dshCode": DSH_ARTIFACT_INVALID, "entry": entry},
                )
            seen.add(target)
            manifest.append(self._digest_artifact(workspace, target))
        return sorted(manifest, key=lambda item: item["path"])
    def _digest_artifact(self, workspace, target):
        """`O_NOFOLLOW` 逐段打开并摘要；拒绝 symlink / 非 regular / 变化中文件。"""
        # 与 contracts/paths.py 口径一致：两侧都按 realpath 比较，避免 macOS
        # `/var` → `/private/var` 这类别名导致误判。
        root = Path(os.path.realpath(str(workspace)))
        try:
            relative = Path(os.path.realpath(str(target))).relative_to(root).parts
        except ValueError:
            raise ContractError(
                "path_escape", message="产物不在 workspace 内",
                details={"dshCode": DSH_ARTIFACT_INVALID, "target": target},
            )
        if not relative or any(part in (".", "..") for part in relative):
            raise ContractError(
                "path_escape", message="产物路径无效",
                details={"dshCode": DSH_ARTIFACT_INVALID, "target": target},
            )
        handles = []
        try:
            fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            handles.append(fd)
            for part in relative[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                handles.append(fd)
            file_fd = os.open(relative[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            handles.append(file_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ContractError(
                    "path_escape", message="产物必须是独立普通文件",
                    details={"dshCode": DSH_ARTIFACT_INVALID, "target": target},
                )
            if before.st_size > MAX_ARTIFACT_BYTES:
                raise ContractError(
                    "path_escape", message="产物超出单文件上限",
                    details={"dshCode": DSH_ARTIFACT_INVALID, "target": target,
                             "limit": MAX_ARTIFACT_BYTES},
                )
            digest = hashlib.sha256()
            while True:
                chunk = os.read(file_fd, 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
            after = os.fstat(file_fd)
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise ContractError(
                    "path_escape", message="产物在收集期间变化，拒绝采信",
                    details={"dshCode": DSH_ARTIFACT_INVALID, "target": target},
                )
            return {"path": str(Path(os.path.realpath(str(target))).relative_to(root)),
                    "sha256": digest.hexdigest(), "size": after.st_size}
        except OSError as exc:
            raise ContractError(
                "path_escape", message="产物不可安全读取：%s" % exc,
                details={"dshCode": DSH_ARTIFACT_INVALID, "target": target},
            ) from exc
        finally:
            for handle in reversed(handles):
                os.close(handle)

    def build_delivery_payload(self, task, attempt, *, summary, report_entry,
                               changed_entries, verification, risks=None, unfinished=None,
                               source_delivery_id=None, quiesced=False):
        """构造 `task.delivered` payload：结果文件、报告、相对路径、大小、SHA-256、
        生成时间、验证命令、风险和未完成项。

        P1 修复（D4-DSH-02-SDK-001 §5.4）：`quiesced` 必须显式传 True 才构造首次
        交付——调用方需先证明宿主本轮正常结束、进程停稳（对 SDK 路径即
        `DshSdkAdapter.wait_and_close()` 的 runtimeClosed=True；对 headless 路径即
        子进程已退出且输出完整）。宿主 failed/unknown/仍运行时调用方不得传
        quiesced=True，本方法无法独立验证该事实（诚实边界：停稳证明在受控入口）。
        `terminal_at=None` 是首次交付的正常前置状态，不再阻断。
        """
        self._validate_attempt_binding(task, attempt)
        workspace = task["workspace"]
        permissions = self._load_permissions(task)
        scopes = permissions.get("writeScope") or []
        resolve_within(workspace, report_entry)
        for entry in changed_entries or []:
            assert_within_scope(workspace, scopes, entry)
        manifest = self.collect_artifacts(task, attempt, list(changed_entries or []),
                                          quiesced=quiesced)
        return {
            "summary": summary,
            "changedFiles": [entry["path"] for entry in manifest],
            "artifacts": manifest,
            "reportPath": report_entry,
            "verification": list(verification or []),
            "risks": list(risks or []),
            "unfinished": list(unfinished or []),
            "sourceDeliveryId": source_delivery_id,
        }

    # -- 预留接口：一律 fail closed ------------------------------------------
    def wait_for_terminal(self, task, attempt, timeout_ms):
        raise ContractError(
            "invalid_transition",
            message="DSH 无 wait/query 能力（0.1.1-rc.2 headless 一次性同步执行），接口预留",
            details={"reserved": True, "dshCode": "DSH_QUERY_UNSUPPORTED"},
        )

    def list_host_tasks(self, agent_id=None):
        raise ContractError(
            "invalid_transition",
            message="DSH 无 tasks.list 能力，接口预留",
            details={"reserved": True, "dshCode": "DSH_QUERY_UNSUPPORTED"},
        )

    def fetch_history(self, task, attempt):
        raise ContractError(
            "invalid_transition",
            message="DSH 无 chat.history 能力，接口预留",
            details={"reserved": True, "dshCode": "DSH_QUERY_UNSUPPORTED"},
        )

    # -- 内部 ---------------------------------------------------------------
    def _build_message(self, task, attempt):
        """复用 D4 投递消息合同，追加本宿主的真实能力说明（不改可信字段）。"""
        note = (
            "[DSH 宿主事实] version=%s profile=%s receipt_mode=%s。"
            "本版本不提供接收凭证与分项回执；未经宿主接收确认前不得声称已开始或已交付。"
            % (self._last_version or "unverified", DSH_PROFILE,
               getattr(self.transport, "receipt_mode", "none"))
        )
        return build_task_message(task, attempt) + "\n" + note

    @staticmethod
    def _load_permissions(task):
        try:
            permissions = json.loads(task.get("permissions_json") or "{}")
        except ValueError:
            permissions = {}
        if not isinstance(permissions, dict):
            raise ContractError(
                "invalid_task", message="任务 permissions 不是对象",
                details={"taskId": task.get("task_id")},
            )
        return permissions

    def _preflight_permissions(self, task):
        """dispatch 前的防御性复核：workspace/writeScope + 目录绑定 + 网络门禁。"""
        workspace = task.get("workspace")
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise ContractError(
                "invalid_task", message="任务 workspace 必须是绝对路径",
                details={"workspace": workspace},
            )
        # P1-2 修复（D4-DSH-02-SDK-001 §5.4）：transport 实际执行目录必须与任务
        # 声明 workspace 的 realpath 一致，否则零任务启动。两侧都按 realpath 归一
        # （macOS /var → /private/var 系统别名视为同一目录，可通过对齐用例）。
        # 离线夹具（FakeDshTransport）没有 workspace 属性时视为一致：它没有
        # 真实执行目录，目录绑定由真实 transport（有 workspace）承担。
        transport_workspace = getattr(self.transport, "workspace", None)
        if transport_workspace is not None:
            declared = os.path.realpath(workspace)
            actual = os.path.realpath(str(transport_workspace))
            if declared != actual:
                raise ContractError(
                    "invalid_task",
                    message="任务声明 workspace 与 transport 实际执行目录不一致，零任务启动",
                    details={"dshCode": "DSH_WORKSPACE_MISMATCH",
                             "declaredWorkspace": declared, "actualWorkspace": actual},
                )
        permissions = self._load_permissions(task)
        scopes = permissions.get("writeScope")
        if not isinstance(scopes, list) or not scopes:
            raise ContractError(
                "invalid_task", message="任务 writeScope 必须是非空数组",
                details={"taskId": task.get("task_id")},
            )
        resolve_scopes(workspace, scopes)  # 声明层：拒绝 `..`/绝对路径/symlink 逃逸
        if bool(getattr(self.transport, "requires_network", True)) and permissions.get("allowNetwork") is not True:
            raise ContractError(
                "permission_denied",
                message="该 DSH 入口经模型 provider 执行、必需联网，与任务声明的 allowNetwork=false 冲突，拒绝 dispatch",
                details={"dshCode": DSH_NETWORK_REQUIRED_BUT_FORBIDDEN,
                         "allowNetwork": permissions.get("allowNetwork"),
                         "hint": "需 cox 裁定是否放宽网络权限或改用离线宿主实现"},
            )
        return permissions

    @staticmethod
    def _validate_attempt_binding(task, attempt):
        if not isinstance(task, dict) or not isinstance(attempt, dict):
            raise ContractError("invalid_transition", message="任务与执行轮次数据无效")
        if attempt.get("task_id") != task.get("task_id"):
            raise ContractError(
                "invalid_transition", message="执行轮次不属于当前任务",
                details={"taskId": task.get("task_id"),
                         "attemptTaskId": attempt.get("task_id")},
            )

    def _submission_path(self, attempt_id):
        return self.submissions_dir / ("%s.json" % attempt_id)

    def _load_submission(self, attempt_id):
        if attempt_id in self._replay:
            return self._replay[attempt_id]
        path = self._submission_path(attempt_id)
        if not path.is_file():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(record, dict):
            self._replay[attempt_id] = record
            return record
        return None

    def _persist_submission(self, attempt_id, digest, status, run_id, detail, level):
        record = {"attemptId": attempt_id, "requestDigest": digest, "status": status,
                  "runId": run_id, "note": _json(detail), "evidenceLevel": level}
        _atomic_private_write(self.submissions_dir, "%s.json" % attempt_id, _json(record))
        self._replay[attempt_id] = record

    def _security(self, kind, detail):
        """把安全事件交给受控入口注入的 sink（默认无 sink 则静默，不落可信状态）。"""
        if self.security_sink is None:
            return
        try:
            self.security_sink(kind, detail)
        except Exception:  # noqa: BLE001 - 记账失败不得改变提交事实
            pass
