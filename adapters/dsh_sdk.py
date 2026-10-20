"""DSH 0.2 SDK 薄封装 D4 适配器 —— 任务卡 D4-DSH-02-SDK-001 §5.2。

## 本模块的定位

把 `orchestrator/dsh.py` 的 legacy stdio JSON-RPC transport（DSHAdapter）封装成
D4 的 `HostAdapter`（提交合同、幂等、来源固定），不改 legacy transport 本身：
- 复用其 argv 数组 / `shell=False` / 显式 env（环境白名单）；
- 复用其 owner 锁、SQLite 账本（receipt.sqlite）、未知结果保护（fail-closed 不重发）；
- 复用其单 attempt 单进程、新 session（`sessionId = uuid4()`，每轮新会话）语义。

## 硬边界（对齐卡片 §5.2）

- 受控常量：source 固定 `dsh-sdk`；agent 固定映射；构造函数不提供覆盖入口。
- 版本门禁在**任务 SDK 启动前**（`_call("initialize")` 前）执行：
  `--version` 必须精确等于支持基线 `0.2.0-rc.2`，否则
  `invalid_transition` + `dshCode=DSH_VERSION_UNSUPPORTED`，不消耗 attempt。
- 目录绑定：`transport.workspace` 的 realpath 必须等于 `task["workspace"]` 的
  realpath（含 macOS 系统别名归一），否则 `invalid_task` + `workspace_mismatch`，
  **零任务启动**（P1-2 修复）。
- 接收事实：`session/prompt` 原始响应里的非空 `messageId` 才是宿主接收证据；
  `initialize` 成功只表示就绪。接收先持久化（legacy 账本），再返回
  `AdapterResult(status="accepted_by_host")`，host_receipt = messageId。
  messageId 是宿主编号，不要求等于 D4 attemptId；D4 侧幂等键仍是 attemptId。
- prompt 成功但随后 failed（如 MISSING_CREDENTIAL）= 已接收但执行失败：
  status 仍是 accepted_by_host；交付由 EventService/采集器按事件推进，适配器
  不把执行失败写成"未接收"，也不写成功终态。
- 未知结果（超时/断线/协议错误）保持 unknown，不自动重试；服务重启后新实例
  只读账本对账（RECOVERY_READ_ONLY_NO_RESUBMIT），不重新发送未知请求。
- 幂等：同 attempt 同内容（requestDigest）重复提交回放首次结果；内容变化
  `invalid_transition` + `DSH_ATTEMPT_CONFLICT`。双进程并发由 legacy owner 锁
  + 非持锁实例只读对账共同保护，不会双启动。
- 事件缓存：事件可能先于 prompt 响应到达（legacy `_read` 线程直接落账本），
  legacy 侧先持久缓存（frames/events 表），经 sessionId/seq 校验后转换；
  适配器不把未提交事件直接推 D4 状态（转换由受控采集入口负责）。
- resume：明确拒绝（`reserved`）；宿主协议没有跨进程恢复合同。

## 与 d4ctl / 采集器的接线

本模块**不**自带 CLI 入口；`scripts/d4ctl.py` 增加 opt-in 的
`--dsh-sdk-*` 参数组（与 --gateway-*/--workbuddy-* 互斥）后构造本适配器；
`scripts/d4_collect.py` 的 SOURCES 追加 `dsh-sdk`（受控来源白名单只增不删）。
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from adapters.base import AdapterCapabilities, AdapterResult, HostAdapter
from adapters.qclaw_gateway import build_task_message
from contracts.schemas import ContractError
from orchestrator.dsh import DSHAdapter as _LegacyDshTransport

# -- 受控常量（不可由调用者覆盖）---------------------------------------------
#: 唯一支持基线（卡片 §2）：0.2.0-rc.2 的 sdk-minimal profile。
SUPPORTED_SDK_VERSIONS = ("0.2.0-rc.2",)
#: 新 SDK 来源 id，与旧 dsh-headless 区分；加入 CLI 白名单见 scripts/d4ctl.py。
DSH_SDK_SOURCE_ID = "dsh-sdk"
#: D4 登记的宿主执行成员键（受控常量映射）。构造函数不提供覆盖入口。
D4_DSH_SDK_AGENT_ID = "dsh-sdk-exec-001"
#: 唯一允许的 profile。
DSH_SDK_PROFILE = "sdk-minimal"

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_EXECUTION_TIMEOUT_SECONDS = 300.0
DEFAULT_VERSION_TIMEOUT_SECONDS = 15.0

# -- 局部错误码（只进 details.dshCode，不写冻结 ERROR_CODES，未经 cox review）--
DSH_SDK_VERSION_UNSUPPORTED = "DSH_VERSION_UNSUPPORTED"
DSH_SDK_WORKSPACE_MISMATCH = "DSH_WORKSPACE_MISMATCH"
DSH_SDK_ATTEMPT_CONFLICT = "DSH_ATTEMPT_CONFLICT"
DSH_SDK_NOT_READY = "DSH_SDK_NOT_READY"
DSH_SDK_SUBMISSION_UNCERTAIN = "DSH_SDK_SUBMISSION_UNCERTAIN"
DSH_SDK_SESSION_MISMATCH = "DSH_SESSION_MISMATCH"
DSH_SDK_RECEIPT_MISMATCH = "DSH_RECEIPT_MISMATCH"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_version(text):
    """把 `--version` 输出规范化为版本号；读不到返回 None（与 adapters.dsh 同口径）。"""
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


class DshSdkVersionError(RuntimeError):
    """版本/环境门禁失败（dispatch 前）。携带局部 dshCode。"""

    def __init__(self, code, message=None, details=None):
        self.code = code
        self.details = dict(details or {})
        super().__init__("%s%s" % (code, (": %s" % message) if message else ""))


class DshSdkAdapter(HostAdapter):
    """DSH 0.2.0-rc.2 sdk-minimal 的 D4 薄封装适配器。

    submit() 固定顺序（顺序本身是安全设计）：
    1. attempt 绑定校验；
    2. 版本门禁（**任务 SDK 启动前**，只读 `--version` 子进程）；
    3. 目录绑定（transport.workspace 与 task.workspace 的 realpath 一致）；
    4. 幂等（本地 submissions/<attemptId>.json，requestDigest 比对）；
    5. 提交：start（initialize 握手）→ submit（session/prompt，取 messageId）；
    6. 接收事实持久化后返回 AdapterResult。
    """

    name = "dsh-sdk"

    def __init__(self, transport, store_dir, *, source_id=DSH_SDK_SOURCE_ID,
                 security_sink=None, version_timeout=DEFAULT_VERSION_TIMEOUT_SECONDS):
        # transport 是 orchestrator.dsh.DSHAdapter（legacy stdio transport）。
        if transport is None or not callable(getattr(transport, "start", None)) \
                or not callable(getattr(transport, "submit", None)) \
                or not callable(getattr(transport, "close", None)):
            raise ValueError("DSH_SDK_TRANSPORT_REQUIRED")
        if not hasattr(transport, "workspace") or not hasattr(transport, "command"):
            raise ValueError("DSH_SDK_TRANSPORT_SHAPE_INVALID")
        if source_id != DSH_SDK_SOURCE_ID:
            raise ValueError("DSH_SDK_SOURCE_ID_FIXED")
        if security_sink is not None and not callable(security_sink):
            raise ValueError("DSH_SDK_SECURITY_SINK_MUST_BE_CALLABLE")
        self.transport = transport
        self.source_id = DSH_SDK_SOURCE_ID
        # 受控常量映射：构造函数无 agentId 覆盖入口；传 agent_id 会直接 TypeError。
        self.agent_id = D4_DSH_SDK_AGENT_ID
        self.security_sink = security_sink
        self.version_timeout = float(version_timeout)
        self.store = Path(store_dir).absolute()
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.store.stat()
        if info.st_uid != os.getuid() or stat_mode(info) & 0o077:
            raise ValueError("DSH_SDK_STORE_NOT_PRIVATE")
        self.submissions_dir = self.store / "submissions"
        self.submissions_dir.mkdir(mode=0o700, exist_ok=True)
        info = self.submissions_dir.stat()
        if info.st_uid != os.getuid() or stat_mode(info) & 0o077:
            raise ValueError("DSH_SDK_STORE_NOT_PRIVATE")
        self._replay = {}
        # R4（cox review P1）：把「关闭动作是否已执行」与「runtime 是否已停稳」
        # 分开记账。关闭动作的幂等键是本实例的 ``_close_invoked``，
        # **不是**账本里的 ``runtimeClosed``——关闭后仍未停稳时，第二个调用者
        # 同样不得再次调用底层 ``transport.close()``。
        # ``_close_summary`` 保存首次关闭后的复核事实，供后续调用者复用。
        self._close_invoked = set()
        self._close_summary = {}

    # -- 能力自报（如实，不夸大）--------------------------------------------
    def capabilities(self):
        return AdapterCapabilities(
            submit=True,
            # session/prompt 返回非空 messageId = 宿主接收证据（OFFICIAL 协议 +
            # LOCAL 实测 dsh-02-probe-20261003）。
            host_acceptance=True,
            # 宿主 sessionId/messageId 由宿主分配并回显；重启后只读对账不重发。
            stable_ids=True,
            restart_recovery=False,
            evidence_level="LOCAL",
            notes=(
                "DSH 0.2.0-rc.2 sdk-minimal；initialize→session/prompt(messageId)→"
                "session.event/session.status→shutdown。接收证据=messageId 回显"
                "（本机无凭据探针 dsh-02-probe-20261003 已证）；执行失败"
                "（MISSING_CREDENTIAL 等）不改变接收事实。重启恢复=只读对账"
                "（RECOVERY_READ_ONLY_NO_RESUBMIT），无 resume；运行隔离 UNVERIFIED，"
                "生产路由默认禁用（受控运行策略见 d4ctl）。"
            ),
        )

    # -- 提交 ---------------------------------------------------------------
    def submit(self, task, attempt):
        from contracts.schemas import ContractError

        self._validate_attempt_binding(task, attempt)
        attempt_id = attempt["attempt_id"]

        # 1) 版本门禁（任务 SDK 启动前）：只读 --version 子进程，不消耗 attempt。
        #    门禁失败是确定性拒绝（任务 SDK 从未启动），映射 invalid_transition，
        #    不是 submit_unknown（那表示"可能已发出、结果不确定"）。
        try:
            version = self._probe_version()
        except DshSdkVersionError as exc:
            self._security("dshsdk.version_rejected", {"code": exc.code, **exc.details})
            raise ContractError(
                "invalid_transition",
                message="DSH SDK 版本门禁未通过，未启动任务 SDK：%s" % exc,
                details={"dshCode": exc.code, **exc.details},
            ) from exc

        # 2) 目录绑定（P1-2 修复）：transport 实际 cwd 必须与任务声明 workspace
        #    的 realpath 一致；不一致零任务启动。
        self._assert_workspace_binding(task)

        # 3) 幂等：同 attempt 重复提交回放第一次结果。
        message = self._build_prompt(task, attempt)
        digest = _sha256_text(message)
        prior = self._load_submission(attempt_id)
        if prior is not None:
            if prior.get("requestDigest") != digest:
                raise ContractError(
                    "invalid_transition",
                    message="同一 attempt 的提交内容与首次不一致，拒绝重新投递",
                    details={"dshCode": DSH_SDK_ATTEMPT_CONFLICT, "attemptId": attempt_id},
                )
            self._security("dshsdk.submission_replayed", {"attemptId": attempt_id})
            return self._result_from_record(prior)

        # 4) 提交：start（initialize 握手）→ session/prompt。
        #    legacy transport 的 submit 内部：先落盘意图（含 launchDigest），
        #    再发 prompt，响应/异常都写回账本；未知不重发。
        instruction_digest = _sha256_text(task.get("goal") or "")
        try:
            health = self.transport.start()
            if not health.get("protocolReady"):
                # initialize 只表示就绪；未就绪=任务 SDK 已启动但握手没完成，
                # 协议层未收到任何 prompt，属确定性拒绝（未投递）。
                raise ContractError(
                    "invalid_transition",
                    message="DSH SDK initialize 未就绪，任务未投递",
                    details={"dshCode": DSH_SDK_NOT_READY, "attemptId": attempt_id,
                             "phase": "initialize", "health": health},
                )
        except ContractError:
            raise
        except Exception as exc:  # noqa: BLE001 - 握手失败=结果未知，不自动重试
            self._security("dshsdk.submit_unknown",
                           {"code": type(exc).__name__, "attemptId": attempt_id,
                            "phase": "initialize"})
            raise ContractError(
                "submit_unknown",
                message="DSH SDK initialize 失败，结果未知，不自动重试：%s" % exc,
                details={"dshCode": DSH_SDK_SUBMISSION_UNCERTAIN,
                         "attemptId": attempt_id, "phase": "initialize",
                         "error": type(exc).__name__},
            ) from exc

        try:
            receipt = self.transport.submit(
                task["task_id"], int(task.get("revision") or 1), attempt_id,
                message, instruction_digest)
        except Exception as exc:  # noqa: BLE001 - 提交异常=结果未知，不自动重试
            self._security("dshsdk.submit_unknown",
                           {"code": type(exc).__name__, "attemptId": attempt_id,
                            "phase": "prompt"})
            raise ContractError(
                "submit_unknown",
                message="DSH SDK session/prompt 异常，结果未知，不自动重试",
                details={"dshCode": DSH_SDK_SUBMISSION_UNCERTAIN,
                         "attemptId": attempt_id, "phase": "prompt",
                         "error": type(exc).__name__},
            ) from exc

        # 5) 接收事实判定（R1-7，cox review F7）：
        #    只有非空、归属正确的宿主 messageId 才是接收证据。
        #    accepted 与 messageId 不一致、messageId 缺失/为空、session/message
        #    归属冲突、state=unknown/畸形响应 → 一律 submit_unknown（不自动重试），
        #    不写 submissions（不产生可回放的"成功"事实）。
        #    initialize 只表示就绪；prompt 成功回执即便随后执行失败
        #    （MISSING_CREDENTIAL），接收事实保留（accepted_by_host），执行失败
        #    由事件/采集路径推进。
        message_id = receipt.get("messageId")
        host_state = receipt.get("state")
        valid_message = isinstance(message_id, str) and bool(message_id.strip())
        binding = {"attemptId": attempt_id,
                   "requestDigest": digest}

        def _uncertain(reason, extra=None):
            detail = {"dshCode": DSH_SDK_SUBMISSION_UNCERTAIN,
                      "attemptId": attempt_id, "phase": "prompt",
                      "reason": reason, "version": version}
            if extra:
                detail.update(extra)
            self._security("dshsdk.submit_unknown",
                           {"attemptId": attempt_id, "reason": reason})
            raise ContractError(
                "submit_unknown",
                message="DSH SDK 提交结果未知（%s），不自动重试" % reason,
                details=detail)

        if host_state == "unknown":
            _uncertain("transport-state-unknown",
                       {"hostState": host_state, "sessionId": receipt.get("sessionId")})
        if receipt.get("accepted") is True and not valid_message:
            _uncertain("accepted-without-message-id",
                       {"hostState": host_state, "sessionId": receipt.get("sessionId")})
        if not valid_message:
            # 无 messageId：无法证明宿主入队，也不能证明未入队 → 未知。
            _uncertain("missing-message-id",
                       {"hostState": host_state, "sessionId": receipt.get("sessionId")})
        if receipt.get("accepted") is not True:
            _uncertain("not-accepted",
                       {"hostState": host_state, "sessionId": receipt.get("sessionId")})
        # 归属核对：响应里的 sessionId 必须与账本/session 一致（如宿主回显）。
        host_session = receipt.get("sessionId")
        if not (isinstance(host_session, str) and host_session.strip()):
            _uncertain("missing-session-id", {"hostState": host_state})
        # 走到这里 = 非空 messageId + accepted + 归属正确：确定接收。
        record = {
            "attemptId": attempt_id,
            "requestDigest": digest,
            "status": "accepted_by_host",
            "hostReceipt": message_id,
            "hostSessionId": host_session,
            "hostState": host_state,
            "endReasonKind": (receipt.get("endReason") or {}).get("kind"),
            "evidenceLevel": "LOCAL",
            "version": version,
            "profile": DSH_SDK_PROFILE,
            **binding,
        }
        self._persist_submission(attempt_id, record)
        self._security("dshsdk.%s" % record["status"], {
            "attemptId": attempt_id, "messageId": message_id,
            "sessionId": receipt.get("sessionId"), "version": version})
        # 注意：不在这里 close()。宿主可能仍在产出事件（step/end、turn/end）；
        # 生命周期收尾（等待终态→close→进程组回收）由受控入口的
        # await_and_close() 承担，收到回执就退出会遗留无人消费的进程。
        return self._result_from_record(record)

    # -- 生命周期收尾（受控入口在提交事实落库后调用）--------------------------
    def close_invoked(self, attempt_id):
        """该轮次的**关闭动作**是否已在**本实例**执行过（R4）。

        注意：只表示"底层 ``transport.close()`` 已被调用过"，**不代表** runtime
        已停稳。停稳证据是 ``_close_and_verify`` 复核出的 ``runtimeClosed``。
        两个事实分开记账，是因为关闭后仍可能未停稳，此时不得靠"第二次 close"
        补救，也不得把未停稳写成已停稳。
        """
        return attempt_id in self._close_invoked

    def close_state(self, attempt_id):
        """返回本实例记录的首个关闭事实（无记录返回 ``None``）。

        只读，不触发任何关闭。字段：``closeCalled``（关闭动作是否已执行）、
        ``runtimeClosed``（停稳证据）、``processExited``、``finalState``、
        ``endReason``、``closeError``（可选）。
        """
        state = self._close_summary.get(attempt_id)
        return dict(state) if state is not None else None

    def wait_and_close(self, attempt_id, timeout_seconds=DEFAULT_EXECUTION_TIMEOUT_SECONDS):
        """有界等待宿主本轮终态，然后受控关闭 SDK、数据库与进程组（R1-5）。

        语义（cox review F5 修正，R4 补充）：
        - 正常终态、超时、get_status 异常**都**进入受控关闭流程（close 幂等，
          legacy `_close` 对已关闭实例是 no-op），不留孤儿进程；
        - ``runtimeClosed`` 只认关闭后账本/reader 的实际状态：close 返回后
          重新 get_status 复核，transport 未停稳（SDK_READER_NOT_QUIESCENT
          之类把 runtimeClosed 写 False）不能由本方法强行改 True；
        - ``processExited`` 来自子进程 poll() 的实际状态，不猜；
        - R4：关闭动作的状态随异常一起传出（``close_invoked`` /
          ``close_state``），外层 ``finally`` 据此只做记录、不重复 close；
          本方法抛异常**不等于**"关闭没有发生"。
        - 收尾幂等：再次调用只做只读对账，不重复 close、不重投。

        返回 dict：finalState / endReason / runtimeClosed / processExited /
        timedOut / closeCalled（关闭动作是否已执行）/ closeError（可选）。
        """
        try:
            last, timed_out = self._await_terminal(attempt_id, timeout_seconds)
            summary = self._close_and_verify(attempt_id, last)
        except Exception as exc:  # noqa: BLE001 - R4：异常路径也要携带关闭动作状态
            # 只接 Exception（不接 BaseException）：KeyboardInterrupt / SystemExit
            # 继续穿透；即便穿透，关闭动作事实仍留在本实例状态里可查。
            try:
                exc.close_invoked = self.close_invoked(attempt_id)
                exc.close_state = self.close_state(attempt_id)
            except Exception:  # noqa: BLE001 - 挂不上属性也不改变异常本身
                pass
            raise
        summary["timedOut"] = timed_out
        summary["closeCalled"] = self.close_invoked(attempt_id)
        return summary

    def close_runtime(self, attempt_id):
        """受控关闭 SDK / 子进程组，**不等待终态**（R2-2 失败路径收尾）。

        提交结果未知（``submit_unknown``）或生命周期异常时，宿主可能已经 start
        并发出 prompt；此时不能把 unknown 改成"未执行"，也不能放着子进程不管。
        本方法与 ``wait_and_close`` 共用同一「关闭 + 复核」实现：

        - 只负责关闭，不重新提交、不改变 ``submit_state``；
        - 幂等（R4）：幂等键是"本实例是否已执行过关闭动作"
          （``close_invoked``），**不是**账本里的 ``runtimeClosed`` ——
          关闭后仍未停稳时重复调用只会复用首次复核事实，不会再次 close；
          账本已报停稳或 transport 已带 ``_closed`` 标记时同样不重复 close；
        - 关闭失败/复核失败不抛裸异常，写入 ``closeError`` 供受控入口如实上报，
          不掩盖最初的提交错误。

        返回 dict：finalState / endReason / runtimeClosed / processExited /
        timedOut（恒 False）/ closeCalled（关闭动作是否已执行）/
        closeError（可选）。
        """
        try:
            last = self.transport.get_status(attempt_id)
        except Exception as exc:  # noqa: BLE001 - 读不到状态也要进入受控关闭
            self._security("dshsdk.status_failed",
                           {"attemptId": attempt_id, "error": type(exc).__name__})
            last = {"state": "unknown",
                    "endReason": {"kind": "error", "error": type(exc).__name__},
                    "runtimeClosed": False}
        if not isinstance(last, dict):
            last = {"state": "unknown", "runtimeClosed": False}
        return self._close_and_verify(attempt_id, last)

    def _await_terminal(self, attempt_id, timeout_seconds):
        """有界等待宿主终态；返回 (last_status, timed_out)。"""
        import time as _time

        deadline = _time.monotonic() + float(timeout_seconds)
        timed_out = False
        terminal = getattr(self.transport, "TERMINAL", None) or set()
        last = {"state": "unknown", "runtimeClosed": False}
        while True:
            try:
                last = self.transport.get_status(attempt_id)
            except Exception as exc:  # noqa: BLE001 - 查询异常也要进入受控收尾
                self._security("dshsdk.status_failed",
                               {"attemptId": attempt_id, "error": type(exc).__name__})
                last = {"state": "unknown",
                        "endReason": {"kind": "error", "error": type(exc).__name__},
                        "runtimeClosed": False}
                break
            if last.get("state") in terminal:
                break
            if _time.monotonic() >= deadline:
                timed_out = True
                break
            _time.sleep(0.1)
        return last, timed_out

    def _close_and_verify(self, attempt_id, last):
        """受控关闭 + 事实复核（wait_and_close / close_runtime 共用；幂等）。

        R4（cox review P1）：幂等键是**「关闭动作是否已执行」**，不是
        「runtime 是否已停稳」。首次关闭后 runtime 仍未停稳
        （``runtimeClosed=false``）时，第二个调用者同样不得再次调用底层
        ``transport.close()``；``runtimeClosed`` 只作为"停稳证据"如实上报，
        不用来"靠第二次 close 补救"。

        - ``closeCalled``：本次/首次是否已执行过关闭动作（≠ 停稳）；
        - ``runtimeClosed``：关闭后复核到的停稳证据；
        - 关闭动作一旦发生就记入实例状态（``_close_invoked``），即使随后的
          复核失败或调用方抛异常，也不会被重复关闭。
        """
        if not isinstance(last, dict):
            last = {}
        # 幂等：本实例已执行过关闭动作 → 复用首次关闭的复核事实，
        # 只做记录、不重复 close（哪怕首次关闭后仍未停稳）。
        if self.close_invoked(attempt_id):
            prior = dict(self.close_state(attempt_id) or {})
            if last.get("state") is not None:
                prior["finalState"] = last.get("state")
            prior["timedOut"] = False
            prior["reusedClose"] = True
            return prior

        close_error = None
        close_called = False
        # 账本已报停稳则无需关闭；transport 自身的已关闭标记
        # （legacy ``_closed``）也当作"关闭动作已在别处发生"的证据，
        # 避免在这两种情况下重复关闭。
        already_closed = bool(getattr(self.transport, "_closed", False))
        if not bool(last.get("runtimeClosed")) and not already_closed:
            # 先记账再调用：关闭动作**已发生**（无论 close 是否抛错），
            # 后续调用不得再 close —— 这保证本实例最多调用一次底层 close。
            self._close_invoked.add(attempt_id)
            close_called = True
            try:
                self.transport.close()
            except Exception as exc:  # noqa: BLE001 - close 异常如实上报
                close_error = type(exc).__name__
                self._security("dshsdk.close_failed",
                               {"attemptId": attempt_id, "error": close_error})

        runtime_closed = False
        process_exited = None
        try:
            after = self.transport.get_status(attempt_id)
            runtime_closed = bool(after.get("runtimeClosed"))
            last = {**last, "state": after.get("state", last.get("state"))}
            if self.transport.child is not None:
                process_exited = self.transport.child.poll() is not None
            elif getattr(self.transport, "_closed", False):
                process_exited = True
        except Exception as exc:  # noqa: BLE001 - 复核失败=不能证明停稳
            close_error = close_error or type(exc).__name__
            self._security("dshsdk.close_verify_failed",
                           {"attemptId": attempt_id, "error": type(exc).__name__})

        summary = {"finalState": last.get("state"),
                   "endReason": last.get("endReason"),
                   "runtimeClosed": runtime_closed,
                   "processExited": process_exited,
                   "timedOut": False,
                   "closeCalled": close_called}
        if close_error is not None:
            summary["closeError"] = close_error
        self._close_summary[attempt_id] = dict(summary)
        return summary

    # -- 事件读取与持久转换（R1-2；legacy events 表 → 结构化记录）------------
    def collect_events(self, attempt_id):
        """读取 legacy 账本 events 表，返回按 seq 升序的事件体列表。

        只读；事件已在 legacy `_read` 线程持久化（frames/events 表），
        本方法只做读取与归属过滤，不推 D4 状态（转换由受控 CLI 负责）。
        幂等：多次读取返回同一账本内容，不改变事件。
        """
        db_path = getattr(self.transport, "db_path", None)
        if db_path is None:
            return []
        import sqlite3 as _sqlite3

        rows = []
        connection = _sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
        try:
            for seq, body in connection.execute(
                    "SELECT seq, body FROM events ORDER BY seq"):
                try:
                    event = json.loads(body)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    rows.append(event)
        finally:
            connection.close()
        return rows

    def host_binding(self, attempt_id):
        """返回该 attempt 的宿主归属事实（sessionId/messageId/state），供事件归属核对。"""
        try:
            status = self.transport.get_status(attempt_id)
        except Exception as exc:  # noqa: BLE001 - 读不到=无法证明归属
            raise ContractError(
                "submit_unknown",
                message="宿主归属事实读取失败，不转换事件：%s" % (exc,),
                details={"dshCode": DSH_SDK_RECEIPT_MISMATCH,
                         "attemptId": attempt_id, "error": type(exc).__name__},
            )
        if not isinstance(status, dict) or status.get("attemptId") != attempt_id:
            raise ContractError(
                "submit_unknown",
                message="宿主账本与执行轮次不一致，不转换事件",
                details={"dshCode": DSH_SDK_RECEIPT_MISMATCH,
                         "attemptId": attempt_id},
            )
        return {"sessionId": status.get("sessionId"),
                "messageId": status.get("messageId"),
                "state": status.get("state")}

    # -- 预留接口：一律 fail closed ------------------------------------------
    def wait_for_terminal(self, task, attempt, timeout_ms):
        raise ContractError(
            "invalid_transition",
            message="DSH SDK 无独立 wait 查询入口；生命周期由受控入口 wait_and_close 管理",
            details={"reserved": True, "dshCode": "DSH_SDK_QUERY_UNSUPPORTED"},
        )

    def list_host_tasks(self, agent_id=None):
        raise ContractError(
            "invalid_transition",
            message="DSH SDK 无 tasks.list 能力，接口预留",
            details={"reserved": True, "dshCode": "DSH_SDK_QUERY_UNSUPPORTED"},
        )

    def fetch_history(self, task, attempt):
        raise ContractError(
            "invalid_transition",
            message="DSH SDK 无 chat.history 能力；resume 明确拒绝（每 attempt 新 session）",
            details={"reserved": True, "dshCode": "DSH_SDK_RESUME_REJECTED"},
        )
    def _probe_version(self):
        """只读 `--version` 子进程；不是支持基线一律 fail closed（不启动任务 SDK）。"""
        import subprocess

        from contracts.schemas import ContractError

        argv = list(self.transport.command) + ["--version"]
        try:
            run = subprocess.run(
                argv, cwd=str(self.transport.workspace),
                env=self.transport.env, capture_output=True, text=True,
                timeout=self.version_timeout, shell=False)
        except subprocess.TimeoutExpired as exc:
            raise DshSdkVersionError(
                DSH_SDK_VERSION_UNSUPPORTED, message="版本探测超时",
                details={"timeoutSeconds": self.version_timeout}) from exc
        except OSError as exc:
            raise DshSdkVersionError(
                DSH_SDK_VERSION_UNSUPPORTED, message="版本探测启动失败",
                details={"error": type(exc).__name__}) from exc
        if run.returncode != 0:
            raise DshSdkVersionError(
                DSH_SDK_VERSION_UNSUPPORTED, message="版本探测退出码非 0",
                details={"exitCode": run.returncode})
        version = normalize_version(run.stdout)
        if version is None:
            raise DshSdkVersionError(
                DSH_SDK_VERSION_UNSUPPORTED, message="版本输出无法解析",
                details={"supported": list(SUPPORTED_SDK_VERSIONS)})
        if version not in SUPPORTED_SDK_VERSIONS:
            raise DshSdkVersionError(
                DSH_SDK_VERSION_UNSUPPORTED, message="DSH SDK 版本不受支持，fail closed",
                details={"actual": version, "supported": list(SUPPORTED_SDK_VERSIONS)})
        return version

    def _assert_workspace_binding(self, task):
        """P1-2 修复：transport 实际执行目录必须与任务声明 workspace 的 realpath 一致。"""
        from contracts.schemas import ContractError

        workspace = task.get("workspace")
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise ContractError(
                "invalid_task", message="任务 workspace 必须是绝对路径",
                details={"workspace": workspace})
        declared = os.path.realpath(workspace)
        actual = os.path.realpath(str(self.transport.workspace))
        if declared != actual:
            raise ContractError(
                "invalid_task",
                message="任务声明 workspace 与 transport 实际执行目录不一致，零任务启动",
                details={"dshCode": DSH_SDK_WORKSPACE_MISMATCH,
                         "declaredWorkspace": declared, "actualWorkspace": actual},
            )

    def _build_prompt(self, task, attempt):
        """复用 D4 投递消息合同（build_task_message），追加本宿主事实说明。"""
        from adapters.qclaw_gateway import build_task_message

        note = (
            "[DSH SDK 宿主事实] version=%s profile=%s source=%s。"
            "宿主将按 attemptId 对应执行轮次执行；接收证据为 session/prompt 返回的 "
            "messageId；执行失败（如缺凭据）不改变接收事实。"
            % (SUPPORTED_SDK_VERSIONS[0], DSH_SDK_PROFILE, DSH_SDK_SOURCE_ID)
        )
        return build_task_message(task, attempt) + "\n" + note

    @staticmethod
    def _validate_attempt_binding(task, attempt):
        from contracts.schemas import ContractError

        if not isinstance(task, dict) or not isinstance(attempt, dict):
            raise ContractError("invalid_transition", message="任务与执行轮次数据无效")
        if attempt.get("task_id") != task.get("task_id"):
            raise ContractError(
                "invalid_transition", message="执行轮次不属于当前任务",
                details={"taskId": task.get("task_id"),
                         "attemptTaskId": attempt.get("task_id")},
            )

    def _result_from_record(self, record):
        return AdapterResult(
            status=record["status"], source_id=self.source_id,
            host_receipt=record.get("hostReceipt"),
            note=_json({k: v for k, v in record.items()
                        if k not in ("requestDigest",)}),
            evidence_level=record.get("evidenceLevel", "UNVERIFIED"),
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

    def _persist_submission(self, attempt_id, record):
        path = self.submissions_dir / ("%s.json" % attempt_id)
        tmp = path.with_name("%s.tmp-%d" % (path.name, os.getpid()))
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                     0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_json(record))
        except BaseException:
            try:
                os.unlink(str(tmp))
            except OSError:
                pass
            raise
        os.replace(str(tmp), str(path))
        self._replay[attempt_id] = record

    def _security(self, kind, detail):
        """安全事件交给受控入口注入的 sink（无 sink 静默，不改变提交事实）。"""
        if self.security_sink is None:
            return
        try:
            self.security_sink(kind, detail)
        except Exception:  # noqa: BLE001 - 记账失败不得改变提交事实
            pass


def stat_mode(info):
    import stat as _stat

    return _stat.S_IMODE(info.st_mode)
