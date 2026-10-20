"""d4ctl — D4 受控 CLI 入口（任务卡 D4-VAL-002-BRIDGE §4 G1）。

身份由子命令固定注入，不开放 ``--as`` 任意身份参数：
- ``task create`` / ``review submit`` / ``review relay`` → owner / reviewer 身份
- ``attempt submit``                     → worker 身份（--agent 声明，须与任务绑定 Agent 一致）
- ``event ingest``                       → source 身份（--source 声明，默认 qclaw-gateway）
- ``query``                              → 只读投影，不注入写身份
- ``agent list`` / ``agent inspect``     → 只读成员投影（复用既有只读服务与
  ``storage.registry_migrations.inspect_registry_schema``；只打开已存在数据库的
  read-only 连接，``--registry`` 缺省时只读取得库内 candidate registryId，
  不迁移、不登记固定身份、不更新 lastSeen、不 probe 宿主）
- ``report import``                      → reviewer 身份（D4-NEXT-01C：只把正式报告
                                            绑定到已有交付，不改任务状态）

输出统一为脱敏、可审计 JSON（stdout），不输出完整聊天、token 或隐藏推理。
宿主交互只发生在 ``attempt submit`` 与 ``review relay``：
- 测试通过 ``run(argv, transport=...)`` 注入 mock transport（强制 synthetic 标记）
- 真实链路必须显式提供 ``--gateway-bin`` + ``--gateway-state-dir``（均为绝对路径）
- relay 只读已落库的 changes_requested，复用提交 CAS；结果未知保留 unknown，
  不自动或人工重发；已接收仅回放本地结果，不再次投递
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.qclaw_gateway import (  # noqa: E402
    CliGatewayTransport,
    GatewayTransportError,
    QClawGatewayAdapter,
)
from adapters.dsh_sdk import (  # noqa: E402
    DSH_SDK_PROFILE,
    DSH_SDK_SOURCE_ID,
    SUPPORTED_SDK_VERSIONS,
    DshSdkAdapter,
    normalize_version as _dsh_normalize_version,
)
from adapters.workbuddy import (  # noqa: E402
    CodeBuddyTransport,
    DEFAULT_WORKBUDDY_MODEL,
    ALLOWED_WORKBUDDY_MODELS,
    WORKBUDDY_AGENT_ID,
    WORKBUDDY_AGENT_IDS,
    WORKBUDDY_SOURCE_ID,
    WorkBuddyAdapter,
    workbuddy_binding,
)
from contracts.document_io import (  # noqa: E402
    DOC_KINDS,
    DocumentContractError,
    build_task_card_source_reference,
    inspect_document,
    task_card_to_create_payload,
)
from contracts.paths import assert_within_scope  # noqa: E402
from contracts.project_intake import (  # noqa: E402
    PROJECT_INTAKE_V1,
    PROJECT_UPDATE_V1,
    ProjectIntakeContractError,
)
from contracts.schemas import (  # noqa: E402
    ContractError,
    is_strict_card_revision,
    validate_source_card_reference,
)
from projections.read_models import ReadModels  # noqa: E402
from service.context import (  # noqa: E402
    owner_context,
    reviewer_context,
    source_context,
    worker_context,
)
from service.events import EventService  # noqa: E402
from service.file_receipts import import_receipt  # noqa: E402
from service.project_intake import preflight_document  # noqa: E402
from service.report_import import import_report  # noqa: E402
from service.registry_detail import inspect_registry_agent  # noqa: E402
from service.registry_read import (  # noqa: E402
    REASON_REGISTRY_UNMIGRATED,
    list_registry_agents,
)
from service.reviews import ReviewService  # noqa: E402
from service.rework import ReworkSubmissionAdapter, relay_receipt  # noqa: E402
from service.tasks import DEFAULT_LIMITS, TaskService, now_iso  # noqa: E402
from storage.registry_migrations import inspect_registry_schema  # noqa: E402
from storage.sqlite_store import SqliteStore  # noqa: E402

# G3：第一版事件入口只支持 worker 主动上报这四种事件类型。
CLI_EVENT_TYPES = ("task.started", "task.progress", "task.delivered", "task.blocked")

# D4 bridge 的入口身份是配置事实，不是调用者可以自行选择的参数。
D4_SOURCE_ID = "qclaw-gateway"
# D4-WB-BRIDGE-001 §4.3：WorkBuddy 来源同为受控常量。调用者只能在此白名单内选择，
# 既不能自造来源，也不能用参数把 workbuddy 轮次伪造成 qclaw 轮次（或反之）。
# D4-DSH-02-SDK-001 §5.3：dsh-sdk 为 DSH 0.2 SDK 受控来源（生产路由默认禁用，
# 见 _dsh_sdk_authorized）。
ALLOWED_SOURCES = (D4_SOURCE_ID, WORKBUDDY_SOURCE_ID, DSH_SDK_SOURCE_ID)
D4_LOGICAL_AGENT_ID = "dev-1"
D4_REVIEWER_ID = "cox"
# D4-AGENT-ISOLATION-GATE G1：发往 Gateway 的宿主 Agent ID 固定为
# agent-d4-exec-002，只存在于 adapters.qclaw_gateway 的受控常量
# D4_GATEWAY_AGENT_ID。本 CLI 不提供任何 Gateway Agent 覆盖入口；
# --gateway-agent 参数已移除，传入任意值会被 argparse 直接拒绝（exit 2）。


def _ok(cmd, data):
    return 0, {"ok": True, "cmd": cmd, "data": data}


def _err(cmd, code, message, details=None):
    return 1, {
        "ok": False,
        "cmd": cmd,
        "error": {"code": code, "message": message, "details": details or {}},
    }


class DshLifecycleAbort(ContractError):
    """提交已被宿主确认，但后续生命周期阶段失败（R2-2 / R2-3）。

    cox review F3：回执越界写入时 CLI 仍返回 ``exit 0``，会让调用方以为整条
    链路成功。本异常保证这类失败**退出码非 0**，同时把已收集的分段诊断
    （submission / execution / collection / teardown）挂在异常上，由 ``run()``
    放回 ``details.lifecycle``，失败原因保持可追溯。

    错误码沿用最初失败的 ``code``（提交未知仍是 ``submit_unknown``），不做改写。
    """

    def __init__(self, code, message, summary, details=None):
        super().__init__(code, message=message, details=details)
        self.lifecycle_summary = summary


#: R2-3：标准回执写入上限（与 service.file_receipts.MAX_BYTES 同口径）。
DSH_SDK_RECEIPT_MAX_BYTES = 1024 * 1024


def build_parser():
    parser = argparse.ArgumentParser(
        prog="d4ctl",
        description="D4 受控入口：任务创建/提交/事件/审核/回传/查询（离线可验证）",
    )
    parser.add_argument("--db", required=True, help="SQLite 数据库绝对路径")
    parser.add_argument("--allowed-root", required=True,
                         help="workspace 允许根目录（绝对路径）")
    parser.add_argument("--source", default=D4_SOURCE_ID,
                         help="受控来源 id；当前只允许 qclaw-gateway")
    parser.add_argument("--agent", default=D4_LOGICAL_AGENT_ID,
                         help="受控逻辑 worker id；当前只允许 dev-1")
    parser.add_argument("--reviewer", default=D4_REVIEWER_ID,
                         help="受控审核人 id；当前只允许 cox")
    # G1：不暴露 --gateway-agent。宿主 Agent 映射由 adapter 受控常量固定。

    sub = parser.add_subparsers(dest="cmd", required=True)

    task_p = sub.add_parser("task", help="任务管理")
    task_sub = task_p.add_subparsers(dest="sub", required=True)
    task_create = task_sub.add_parser("create", help="创建任务（owner 身份）")
    task_create.add_argument("--file", required=True, help="任务 payload JSON 文件")

    attempt_p = sub.add_parser("attempt", help="执行轮次")
    attempt_sub = attempt_p.add_subparsers(dest="sub", required=True)
    attempt_submit = attempt_sub.add_parser("submit", help="提交执行轮次（worker 身份）")
    attempt_submit.add_argument("--task", required=True)
    attempt_submit.add_argument("--attempt", default=None,
                                help="指定 attemptId；缺省则先创建新执行轮次")
    attempt_submit.add_argument("--gateway-bin", default=None,
                                help="openclaw CLI 可执行文件绝对路径")
    attempt_submit.add_argument("--gateway-state-dir", default=None,
                                help="OpenClaw state 目录绝对路径（走环境变量，不进 argv）")
    attempt_submit.add_argument("--gateway-config", default=None,
                                help="可选 OpenClaw 配置文件绝对路径")
    # D4-WB-BRIDGE-001 §4.3：WorkBuddy 路由参数。
    # 刻意**不提供** --workbuddy-session：宿主运行编号由 attemptId 派生，
    # 开放该参数等于给 accepted_by_host 留伪造口。
    attempt_submit.add_argument("--workbuddy-bin", default=None,
                                help="codebuddy 可执行文件绝对路径")
    attempt_submit.add_argument("--workbuddy-home", default=None,
                                help="隔离 HOME 绝对路径（适配器 store 派生其下）")
    attempt_submit.add_argument("--workbuddy-timeout-ms", type=int, default=600000,
                                help="WorkBuddy 单轮执行超时毫秒（默认 600000）")
    attempt_submit.add_argument("--workbuddy-model", default=DEFAULT_WORKBUDDY_MODEL,
                                choices=ALLOWED_WORKBUDDY_MODELS,
                                help="显式指定受控模型；需宿主 init 回显一致，不允许自动换模型")
    # D4-DSH-02-SDK-001 §5.3：DSH 0.2 SDK opt-in 路由参数。
    # 刻意**不提供** --dsh-sdk-agent / --dsh-sdk-profile / argv 注入：
    # source 固定 dsh-sdk、agent 固定 dsh-sdk-exec-001、profile 固定
    # sdk-minimal，均由 adapters.dsh_sdk 受控常量承载（卡片 §5.1）。
    attempt_submit.add_argument("--dsh-sdk-bin", default=None,
                                help="DSH SDK CLI 可执行文件绝对路径（0.2.0-rc.2）")
    attempt_submit.add_argument("--dsh-sdk-home", default=None,
                                help="隔离 HOME 绝对路径（SDK store 派生其下）")
    attempt_submit.add_argument("--dsh-sdk-execution-timeout-ms", type=int,
                                default=300000,
                                help="DSH 单轮执行超时毫秒（默认 300000）")
    attempt_submit.add_argument("--timeout-ms", type=int, default=15000)

    event_p = sub.add_parser("event", help="事件入口")
    event_sub = event_p.add_subparsers(dest="sub", required=True)
    event_ingest = event_sub.add_parser("ingest", help="上报事件（source 身份）")
    event_ingest.add_argument("--file", required=True, help="事件 payload JSON 文件")
    event_import = event_sub.add_parser("import", help="导入本轮目录的 Agent 文件回执")
    event_import.add_argument("--task", required=True)
    event_import.add_argument("--attempt", required=True)
    event_import.add_argument("--file", required=True)

    review_p = sub.add_parser("review", help="审核决定")
    review_sub = review_p.add_subparsers(dest="sub", required=True)
    review_submit = review_sub.add_parser("submit", help="提交审核决定（reviewer 身份）")
    review_submit.add_argument("--task", required=True)
    review_submit.add_argument("--file", required=True, help="审核 payload JSON 文件")
    review_relay = review_sub.add_parser("relay",
                                         help="把 changes_requested 回传给同一 Agent session")
    review_relay.add_argument("--task", required=True)
    review_relay.add_argument("--gateway-bin", default=None)
    review_relay.add_argument("--gateway-state-dir", default=None)
    review_relay.add_argument("--gateway-config", default=None)
    review_relay.add_argument("--timeout-ms", type=int, default=15000)

    # D4-NEXT-01C：正式报告导入。与 `event import` 的分工见 _cmd_report_import 注释：
    # event import 只收 events.json 事件包；report import 只把正式报告绑定到已有交付，
    # 两者共用同一套失败 fail-closed 语义，不新增第二套事件接收状态机。
    report_p = sub.add_parser("report", help="正式报告导入与摘要记账")
    report_sub = report_p.add_subparsers(dest="sub", required=True)
    report_import = report_sub.add_parser(
        "import", help="把 Agent 正式 Markdown 报告绑定到指定当前交付（reviewer 身份）")
    report_import.add_argument("--task", required=True, help="目标任务 id")
    report_import.add_argument("--attempt", required=True, help="当前执行轮次 id")
    report_import.add_argument("--delivery", required=True, help="当前交付 id（必须是任务当前交付）")
    report_import.add_argument("--file", required=True, help="报告 Markdown 文件绝对路径")
    report_import.add_argument(
        "--source-card-id", default=None,
        help="可选：额外的来源任务卡 cardId 一致性断言；与 --source-card-revision 必须成对。"
             "无论是否传入，都会以库内冻结来源为准核对（不传也核，传错即拒绝，不能建立缺失来源）")
    report_import.add_argument(
        "--source-card-revision", type=int, default=None,
        help="可选：额外的来源任务卡 cardRevision 一致性断言；与 --source-card-id 必须成对")

    query_p = sub.add_parser("query", help="只读查询（不注入写身份）")
    query_p.add_argument("--task", default=None)
    query_p.add_argument("--activity", action="store_true",
                         help="配合 --task 输出任务活动流水")
    query_p.add_argument("--project", default=None)
    query_p.add_argument("--review-queue", action="store_true")

    # D4-NEXT-01B：只读文档预检。不要求正式数据库，不创建数据库/任务/执行轮次，不调宿主。
    document_p = sub.add_parser("document", help="文档解析与只读预检")
    document_sub = document_p.add_subparsers(dest="sub", required=True)
    document_inspect = document_sub.add_parser(
        "inspect", help="只读预检任务卡/回执报告（不写库、不派发、不调宿主）")
    document_inspect.add_argument("--file", required=True, help="Markdown 文件绝对路径")
    document_inspect.add_argument(
        "--kind", default=None, choices=list(DOC_KINDS),
        help="文档类型；缺省时只在文件明确声明且无歧义时自动判断，否则拒绝")

    # D4-PROJECT-01：项目接入资料只读预检。与 document inspect 同一形状——
    # 在打开/创建数据库之前返回，不写盘、不生成快照、不派发、不调宿主。
    # project import / project update 为后续卡的待实现工具，本卡不提供。
    project_p = sub.add_parser("project", help="项目接入资料（只读预检）")
    project_sub = project_p.add_subparsers(dest="sub", required=True)
    project_inspect = project_sub.add_parser(
        "inspect", help="只读预检 project-intake.v1 / project-update.v1（不写库、不派发）")
    project_inspect.add_argument("--file", required=True, help="资料 JSON 文件绝对路径")
    project_inspect.add_argument(
        "--kind", default=None, choices=("intake", "update"),
        help="资料类型；缺省时按 schemaVersion 判断，无法判断则拒绝（%s / %s）"
             % (PROJECT_INTAKE_V1, PROJECT_UPDATE_V1))

    # 成员列表/详情只读入口：只打开已存在数据库的 read-only 连接，复用
    # service.registry_read / service.registry_detail / storage.registry_migrations
    # 的既有投影与稳定错误语义；host/role/enabled/agentId/registryId 校验全部转交
    # 服务与迁移器，CLI 不自建第二套校验、不拼 Registry SQL。--registry 缺省时
    # 用 inspect_registry_schema 只读取得库内 candidate registryId（只读发现，
    # 不建库、不迁移、不猜测）。
    agent_p = sub.add_parser("agent", help="成员列表/详情（只读，不写库、不派发、不探测）")
    agent_sub = agent_p.add_subparsers(dest="sub", required=True)
    agent_list = agent_sub.add_parser("list", help="候选成员列表（只读投影）")
    agent_list.add_argument(
        "--registry", default=None,
        help="registryId（受控 ID，完整匹配，转交服务校验）；缺省时只读从库内 "
             "candidate 元数据取得，不猜、不建库、不迁移")
    agent_list.add_argument("--host", default=None,
                            help="按 environment.hostType 精确过滤（转交服务校验）")
    agent_list.add_argument("--role", default=None,
                            help="按成员 roles 精确过滤（转交服务校验）")
    agent_list.add_argument("--enabled", default=None, choices=("true", "false"),
                            help="按 enabled 过滤；只接受小写 true/false")
    agent_list.add_argument("--format", default="json", choices=("table", "json"),
                            help="输出形态；json 为服务完整投影，table 为可读视图")
    agent_inspect = agent_sub.add_parser("inspect", help="候选成员详情（只读投影）")
    agent_inspect.add_argument("agent_id",
                               help="成员 agentId（转交服务校验）")
    agent_inspect.add_argument(
        "--registry", default=None,
        help="registryId（受控 ID，完整匹配，转交服务校验）；缺省时只读从库内 "
             "candidate 元数据取得，不猜、不建库、不迁移")

    return parser


def _require_absolute(value, name):
    if not value or not Path(value).is_absolute():
        raise ValueError("%s 必须是绝对路径" % (name,))
    return value


def _workbuddy_requested(args):
    """是否请求 WorkBuddy 路由（只看两个受控参数，不看身份）。"""
    return bool(getattr(args, "workbuddy_bin", None) or getattr(args, "workbuddy_home", None))


def _dsh_sdk_requested(args):
    """是否请求 DSH SDK 路由（只看受控参数，不看身份）。"""
    return bool(getattr(args, "dsh_sdk_bin", None) or getattr(args, "dsh_sdk_home", None))


def _dsh_sdk_material_diagnostics():
    """只读诊断：报告 env 与三份策略 JSON 是否存在/形状是否可解析（R2-1）。

    **本函数不参与放行判定**。R2-1 之后真实 DSH 路由无条件 fail closed，
    保留这层解析只为排障，以及给后续「受信配置 + 运行时隔离核验 + 双网络授权」
    实施卡复用形状；无论解析结果多完整，都不能让真实路由变成"可投递"。
    """
    diagnostics = {
        "envEnabled": os.environ.get("D4_DSH_SDK_PROD_ROUTING") == "1",
        "untrusted": True,
        "materials": {},
    }
    spec = (
        ("member", "D4_DSH_SDK_MEMBER_MANIFEST", _validate_member_manifest),
        ("isolation", "D4_DSH_SDK_ISOLATION_MANIFEST", _validate_isolation_manifest),
        ("network", "D4_DSH_SDK_NETWORK_MANIFEST", _validate_network_manifest),
    )
    for kind, env_name, validator in spec:
        path = os.environ.get(env_name)
        if not path:
            diagnostics["materials"][kind] = {"env": env_name, "status": "unset"}
            continue
        try:
            validator(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            diagnostics["materials"][kind] = {"env": env_name, "status": "shape-invalid",
                                             "error": type(exc).__name__}
        else:
            diagnostics["materials"][kind] = {"env": env_name, "status": "shape-ok"}
    return diagnostics


def _dsh_sdk_authorized():
    """真实 DSH 路由授权判定 —— R2-1：**无条件 fail closed**。

    返回值恒定 ``(False, reason)``：

    - ``D4_DSH_SDK_PROD_ROUTING=1`` 加三份人工填写的 JSON **不是**可信授权
      （R1 cox 裁定 4）。真实成员绑定、运行隔离和模型/工具网络出口必须来自
      受信配置与运行时核验；任意字段组合都不能让真实路由返回"可投递"。
    - 环境变量与材料解析保留为**非授权诊断**，只进 details，不改变判定。
    - 放行需要另一张实施卡接入受信配置来源、运行时隔离核验与双网络授权；
      本卡不自造 token、签名或"已验隔离"证明。

    调用方拿到 False 后必须：零创建 attempt、零启动 SDK、零 prompt。
    """
    diagnostics = _dsh_sdk_material_diagnostics()
    return False, {
        "gate": "dsh-sdk-prod-routing-fail-closed",
        "reason": (
            "DSH SDK 真实路由未授权：环境变量与人工填写的策略材料属**非授权材料**，"
            "不构成本轮的可信授权（fail closed）"),
        "authorizationSource": "none-in-this-card",
        "diagnostics": diagnostics,
        # 这些是「后续实施卡」的前提，不是让操作员就地补 JSON 的清单。
        "futureCardRequirements": [
            "另卡：受信配置来源（非人工填写 JSON）",
            "另卡：运行时隔离核验（非声明字段）",
            "另卡：模型/工具双网络授权（非声明字段）",
        ],
    }


def _load_manifest_json(path):
    """读策略材料 JSON：必须是对象；非对象按材料缺失处理。"""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest 必须是 JSON 对象")
    return data


def _validate_member_manifest(path):
    data = _load_manifest_json(path)
    agents = data.get("agents")
    if not isinstance(agents, list) or "dsh-sdk-exec-001" not in agents:
        raise ValueError("成员登记缺少 dsh-sdk-exec-001")
    return data


def _validate_isolation_manifest(path):
    data = _load_manifest_json(path)
    if data.get("isolationLevel") != "sandboxed":
        raise ValueError("运行隔离等级未达 sandboxed")
    return data


def _validate_network_manifest(path):
    data = _load_manifest_json(path)
    if data.get("allowModelNetwork") is not True or data.get("allowToolNetwork") is not True:
        raise ValueError("模型/工具网络授权不完整")
    return data


def _resolve_dsh_sdk_base(args, task):
    """解析 DSH SDK 基础配置：bin/home/demo/env（与 attempt 无关，R1-6 预检用）。

    刻意不接受调用者指定 sessionId/messageId（宿主编号由 SDK 分配），
    也不接受 agent/profile/argv 覆盖（受控常量固定）。

    R1-1：真实启动 argv 必须显式携带 ``--profile sdk-minimal``。本机
    0.2.0-rc.2 CLI 参数解析只认 argv，不读 DSH_PROFILE 环境变量（cox
    review F1 实测 exit=1 / error: --profile <name> is required）。
    profile 是代码内受控常量，不由参数/环境提供。
    """
    binary = getattr(args, "dsh_sdk_bin", None)
    home = getattr(args, "dsh_sdk_home", None)
    if not (binary and home):
        raise ValueError("dsh-sdk 配置不完整：--dsh-sdk-bin 与 --dsh-sdk-home 必须同时提供")
    _require_absolute(binary, "--dsh-sdk-bin")
    _require_absolute(home, "--dsh-sdk-home")
    home_path = Path(home)
    if not home_path.is_dir():
        raise ValueError("--dsh-sdk-home 必须是已存在的目录")
    if not Path(binary).is_file():
        raise ValueError("--dsh-sdk-bin 必须是已存在的可执行文件")
    node = os.environ.get("D4_DSH_SDK_NODE", "/Users/demo/.hermes/node/bin/node")
    # R1-1：任务启动 argv 固定 --profile sdk-minimal（受控常量，非环境/参数）。
    command = [node, binary, "--profile", DSH_SDK_PROFILE]
    env = {"HOME": str(home_path),
           "PATH": "/usr/bin:/bin",
           "DSH_PROFILE": DSH_SDK_PROFILE}
    return command, str(task["workspace"]), env


def _resolve_dsh_sdk_store(args, attempt_id):
    """R1-4：每个 attempt 独立传输账本 ``<home>/d4-dsh-sdk-store/<taskId>/<attemptId>/``。

    attemptId 只来自 TaskService（建轮次或复述已存在轮次）。
    返回 (command, sdk_store)；command 与 base 一致（含固定 profile）。
    """
    home_path = Path(args.dsh_sdk_home)
    attempt_id = attempt_id or ""
    if not (isinstance(attempt_id, str) and attempt_id.strip()):
        raise ValueError("dsh-sdk 路由需要已创建的执行轮次（attemptId 为空）")
    if "/" in attempt_id or attempt_id in (".", ".."):
        raise ValueError("attemptId 非法，不能用于派生传输目录")
    task_id = args.task
    sdk_store = str(home_path / "d4-dsh-sdk-store" / task_id / attempt_id)
    node = os.environ.get("D4_DSH_SDK_NODE", "/Users/demo/.hermes/node/bin/node")
    command = [node, args.dsh_sdk_bin, "--profile", DSH_SDK_PROFILE]
    return command, sdk_store


def _dsh_sdk_version_preflight(command, workspace, env, timeout_seconds=15.0):
    """R1-6：版本预检在创建提交占位（TaskService.begin_submission）之前执行。

    只读 ``--version`` 子进程（shell=False、受限 env）；版本不匹配 →
    ValueError（CLI 映射 usage_error），此时数据库里还没有轮次/占位，
    不产生 submit_state=unknown。与 Adapter 内的防御性复查同一判据
    （精确等于 SUPPORTED_SDK_VERSIONS[0]）。
    """
    import subprocess

    argv = list(command) + ["--version"]
    try:
        run = subprocess.run(
            argv, cwd=str(workspace), env=env, capture_output=True, text=True,
            timeout=max(1.0, float(timeout_seconds)), shell=False)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("DSH SDK 版本预检超时，未创建提交占位：%s" % (exc,)) from exc
    except OSError as exc:
        raise ValueError("DSH SDK 版本预检启动失败，未创建提交占位：%s" % (exc,)) from exc
    if run.returncode != 0:
        raise ValueError(
            "DSH SDK 版本预检退出码非 0（%d），未创建提交占位" % run.returncode)
    version = _dsh_normalize_version(run.stdout)
    if version is None:
        raise ValueError("DSH SDK 版本输出无法解析，未创建提交占位")
    if version not in SUPPORTED_SDK_VERSIONS:
        raise ValueError(
            "DSH SDK 版本 %s 不受支持（支持 %s），未创建提交占位"
            % (version, ", ".join(SUPPORTED_SDK_VERSIONS)))
    return version


def _resolve_workbuddy(args):
    """解析 WorkBuddy 路由：两个参数必须同时给出且都是绝对路径。

    刻意不接受调用者指定 sessionId：宿主运行编号由适配器从 attemptId 派生，
    开放该入口等于给 accepted_by_host 留伪造口（§4.3）。
    """
    binary = getattr(args, "workbuddy_bin", None)
    home = getattr(args, "workbuddy_home", None)
    if not (binary and home):
        raise ValueError("workbuddy 配置不完整：--workbuddy-bin 与 --workbuddy-home 必须同时提供")
    _require_absolute(binary, "--workbuddy-bin")
    _require_absolute(home, "--workbuddy-home")
    home_path = Path(home)
    if not home_path.is_dir():
        raise ValueError("--workbuddy-home 必须是已存在的目录")
    return binary, str(home_path)


def _resolve_transport(args, cmd, injected, synthetic):
    """解析宿主 transport：测试注入优先（强制 synthetic）；真实链路需显式 CLI 配置。"""
    if injected is not None:
        # 注入即视为离线链路：synthetic 标记强制为 True，防离线回执冒充真实宿主证据。
        return injected, True
    if getattr(args, "gateway_bin", None) or getattr(args, "gateway_state_dir", None):
        if not (args.gateway_bin and args.gateway_state_dir):
            raise ValueError("gateway 配置不完整：--gateway-bin 与 --gateway-state-dir 必须同时提供")
        _require_absolute(args.gateway_bin, "--gateway-bin")
        _require_absolute(args.gateway_state_dir, "--gateway-state-dir")
        if args.gateway_config:
            _require_absolute(args.gateway_config, "--gateway-config")
        return (
            CliGatewayTransport(
                command=[args.gateway_bin],
                state_dir=args.gateway_state_dir,
                config_path=args.gateway_config,
            ),
            synthetic,
        )
    raise ValueError(
        "本子命令(%s)需要宿主通道：测试用 run(transport=...) 注入 mock，"
        "真实链路必须提供 --gateway-bin + --gateway-state-dir" % (cmd,)
    )


def _load_json_file(path):
    _require_absolute(path, "输入文件路径")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _relay_already_sent(store, task_id, review_id):
    for row in store.list_activity(task_id):
        if row.get("kind") != "review.relay_sent":
            continue
        try:
            detail = json.loads(row.get("detail_json") or "{}")
        except ValueError:
            continue
        if detail.get("reviewId") == review_id:
            return detail
    return None


def _validate_fixed_identities(args):
    """CLI 身份只来自固定映射；参数不能变成身份伪造入口。"""
    if args.source not in ALLOWED_SOURCES:
        raise ContractError(
            "permission_denied",
            message="受控入口不允许自选 source 身份",
            details={"identity": "source", "allowed": list(ALLOWED_SOURCES),
                     "got": args.source},
        )
    expected = {
        "reviewer": D4_REVIEWER_ID,
    }
    allowed_agents = ((D4_LOGICAL_AGENT_ID,) + WORKBUDDY_AGENT_IDS
                      if args.source == WORKBUDDY_SOURCE_ID else (D4_LOGICAL_AGENT_ID,))
    if args.agent not in allowed_agents:
        raise ContractError("permission_denied", message="成员不在受控宿主映射中",
                            details={"agent": args.agent, "allowed": list(allowed_agents)})
    for name, value in expected.items():
        if getattr(args, name) != value:
            raise ContractError(
                "permission_denied",
                message="受控入口不允许自选 %s 身份" % name,
                details={"identity": name, "expected": value, "got": getattr(args, name)},
            )
    # G1：--gateway-agent 参数已移除；任何残留注入痕迹都按身份伪造拒绝（防御性）。
    if getattr(args, "gateway_agent", None) is not None:
        raise ContractError(
            "permission_denied",
            message="Gateway Agent 映射已是受控常量，不接受任何 --gateway-agent 值",
            details={"got": getattr(args, "gateway_agent")},
        )


def _is_markdown_file(path):
    """Markdown 入口判定：只看扩展名，不猜内容、不做宽松兜底。"""
    return str(path).lower().endswith((".md", ".markdown"))


def _load_task_card_payload(args):
    """Markdown 任务卡：先规范化 + 整体校验，再映射为建卡入参。

    缺当前服务必填字段时拒绝创建，不自动补默认身份、目录、权限或成员。
    """
    _require_absolute(args.file, "输入文件路径")
    document = inspect_document(
        args.file,
        kind="task-card",
        allowed_roots=[args.allowed_root],
        cli_agent=args.agent,
        cli_source=args.source,
    )
    payload, missing, errors = task_card_to_create_payload(
        document["canonical"],
        cli_agent=args.agent,
        cli_source=args.source,
        allowed_roots=[args.allowed_root],
    )
    if missing or errors:
        raise ContractError(
            "invalid_task",
            message="Markdown 任务卡缺少建卡必填字段，未创建任务",
            details={"missing": missing, "errors": errors},
        )
    return payload, document


# D4-NEXT-01C-R1/R2：JSON 建卡可选的 sourceCard 引用一致性检查字段。
# R2-B：比较的是两侧「实际入库的完整有效语义」，不是 payload 里出现的字段。
_SOURCE_CARD_COMPARE_FIELDS = (
    "projectId", "agentId", "sourceId", "workspace", "goal",
    "acceptance", "permissions", "limits",
)


def _effective_task_semantics(payload):
    """把建卡入参规范化为「实际入库的完整有效语义」。

    - acceptance 省略 / null / 非数组 -> 实入库为空数组；
    - limits 按 TaskService.DEFAULT_LIMITS 规范化（省略 / null 用默认补齐，
      只接受非空值覆盖默认），保证两端在相同默认下可比较；
    - permissions 省略 / 非对象 -> 空对象（服务层另有必填校验）；
    其余标量直接取声明值。这样「省略或 null」不能悄悄放宽原卡要求。
    """
    permissions = payload.get("permissions")
    permissions = permissions if isinstance(permissions, dict) else {}
    acceptance = payload.get("acceptance")
    acceptance = list(acceptance) if isinstance(acceptance, list) else []
    limits = dict(DEFAULT_LIMITS)
    provided = payload.get("limits")
    if isinstance(provided, dict):
        limits.update({k: v for k, v in provided.items() if v is not None})
    return {
        "projectId": payload.get("projectId"),
        "agentId": payload.get("agentId"),
        "sourceId": payload.get("sourceId"),
        "workspace": payload.get("workspace"),
        "goal": payload.get("goal"),
        "acceptance": acceptance,
        "permissions": permissions,
        "limits": limits,
    }


def _assert_json_source_consistent(mapped, payload):
    """JSON 建卡与来源卡需一致：不是给一份任意 JSON 登记个名字。

    比较两侧**实际入库的完整有效语义**（projectId/agentId/sourceId/workspace/goal/
    acceptance/permissions/limits）：省略或 null 先按默认规范化再完整比较，
    因此「省略 acceptance/limits」不能削弱原卡的非空验收或更严格限制。
    合法同默认值（两侧都省略）仍然接受；严格原卡不能被放宽。
    """
    json_sem = _effective_task_semantics(payload)
    card_sem = _effective_task_semantics(mapped)
    for key in _SOURCE_CARD_COMPARE_FIELDS:
        if json_sem[key] != card_sem[key]:
            raise ContractError(
                "source_card_mismatch",
                message="JSON 建卡与来源任务卡的实际有效 %s 语义不一致，拒绝建卡" % key,
                details={"field": key, "jsonValue": json_sem[key], "cardValue": card_sem[key]},
            )


def _load_json_source_card(args, payload):
    """JSON 建卡的可选 sourceCard 引用：整体校验 + 受控读取 + 一致核对。

    R2-A：存在 sourceCard 时 path/cardId/cardRevision 三字段必填且类型严格
    （省略 / null / 空 / 非字符串 / 布尔或非整数版本 / 0 / 负数一律拒绝）；
    必须整体校验被引用的任务卡（复用 01B 校验）并核对实际建卡内容；
    不是给一份任意 JSON 登记个名字。

    R3：严格区分「sourceCard key 缺失」与「key 存在但值为显式 null」：
    - key 缺失 -> 返回 None（旧 JSON 建卡保持兼容，但这张任务没有登记来源，
      不能新导入正式报告）；
    - key 存在且值为 null -> 视为错误引用，抛 source_card_mismatch（受控拒绝、
      零 task/source/activity 写入），绝不退化成 legacy。
    """
    if "sourceCard" not in payload:
        return None
    ref = payload["sourceCard"]
    if ref is None:
        raise ContractError(
            "source_card_mismatch",
            message="sourceCard 显式声明为 null 不允许，拒绝建卡（仅缺 key 才走旧 JSON 兼容）",
            details={"field": "sourceCard", "value": None},
        )
    if not isinstance(ref, dict):
        raise ContractError("source_card_mismatch",
                            message="sourceCard 必须是对象", details={"type": type(ref).__name__})
    ref_errors = validate_source_card_reference(ref)
    if ref_errors:
        raise ContractError(
            "source_card_mismatch",
            message="sourceCard 的 path/cardId/cardRevision 三字段必填且类型严格，拒绝建卡",
            details={"errors": ref_errors},
        )
    card_path = ref["path"]
    _require_absolute(card_path, "sourceCard.path")
    document = inspect_document(
        card_path, kind="task-card", allowed_roots=[args.allowed_root],
        cli_agent=args.agent, cli_source=args.source,
    )
    canonical = document["canonical"]
    actual_revision = canonical.get("cardRevision")
    # 被引用卡自身的版本也必须是受控版本：拒绝字符串/布尔/0/负数冒充。
    if not is_strict_card_revision(actual_revision):
        raise ContractError(
            "source_card_mismatch",
            message="被引用任务卡 cardRevision 必须是 >=1 的整数，拒绝建卡",
            details={"actual": actual_revision, "type": type(actual_revision).__name__},
        )
    if ref["cardId"] != canonical.get("cardId"):
        raise ContractError(
            "source_card_mismatch",
            message="sourceCard.cardId 与被引用任务卡不一致，拒绝建卡",
            details={"declared": ref["cardId"], "actual": canonical.get("cardId")},
        )
    if ref["cardRevision"] != actual_revision:
        raise ContractError(
            "source_card_mismatch",
            message="sourceCard.cardRevision 与被引用任务卡不一致，拒绝建卡",
            details={"declared": ref["cardRevision"], "actual": actual_revision},
        )
    mapped, missing, errors = task_card_to_create_payload(
        canonical, cli_agent=args.agent, cli_source=args.source,
        allowed_roots=[args.allowed_root],
    )
    if missing or errors:
        raise ContractError(
            "source_card_mismatch",
            message="被引用的来源任务卡缺少建卡必填字段，拒绝建卡",
            details={"missing": missing, "errors": errors},
        )
    _assert_json_source_consistent(mapped, payload)
    return build_task_card_source_reference(document, path=card_path)


def _cmd_document_inspect(args):
    """只读预检入口：不打开数据库、不创建任务、不创建 attempt、不调宿主。"""
    _require_absolute(args.file, "输入文件路径")
    return inspect_document(
        args.file,
        kind=args.kind,
        allowed_roots=[args.allowed_root],
        cli_agent=args.agent,
        cli_source=args.source,
    )


def _cmd_project_inspect(args):
    """项目资料只读预检：不打开/创建数据库、不写盘、不生成快照、不派发、不调宿主。

    允许根（--allowed-root）与两份大小上限都由受控入口注入；资料 JSON 自身
    不能指定读取上限，也不能借此扩大读取范围。
    """
    _require_absolute(args.file, "输入文件路径")
    return preflight_document(
        args.file,
        doc_type=args.kind,
        allowed_roots=[args.allowed_root],
    )


def _cmd_task_create(args, services, synthetic):
    document = None
    source_card = None
    if _is_markdown_file(args.file):
        payload, document = _load_task_card_payload(args)
        # 受控来源：摘要由实际读取计算，cardId/cardRevision 从已校验 canonical 提取。
        source_card = build_task_card_source_reference(
            document, path=document["sourceFile"]["path"])
    else:
        payload = _load_json_file(args.file)
        source_card = _load_json_source_card(args, payload)
    # CLI 层前置校验：payload 自报身份与受控入口配置不一致时提前拒绝（服务层仍有权威校验）；
    # 缺字段则交给服务层报 invalid_task，语义更准确。
    if payload.get("agentId") is not None and payload["agentId"] != args.agent:
        raise ContractError(
            "permission_denied",
            message="任务 payload 的 agentId 与受控入口 --agent 不一致",
            details={"payloadAgentId": payload.get("agentId"), "cliAgentId": args.agent},
        )
    if payload.get("sourceId") is not None and payload["sourceId"] != args.source:
        raise ContractError(
            "permission_denied",
            message="任务 payload 的 sourceId 与受控入口 --source 不一致",
            details={"payloadSourceId": payload.get("sourceId"), "cliSourceId": args.source},
        )
    context = owner_context(actor_id=args.reviewer, entry="d4ctl task create",
                             synthetic=synthetic)
    result = services["tasks"].create_task(payload, context, source_card=source_card)
    if document is not None:
        result = dict(result)
        result["document"] = {
            "documentKind": document["documentKind"],
            "schemaVersion": document["schemaVersion"],
            "canonicalDigest": document["canonicalDigest"],
        }
    return result


def _dsh_sdk_close_facts(adapter, attempt_id):
    """读取适配器对某轮次「关闭动作」的权威记录（无记录返回 ``None``）。

    R4（cox review P1）：关闭动作是否已执行由适配器实例状态承载，
    **与 ``wait_and_close`` 是否正常返回无关**——``wait_and_close`` 内部
    已经 close、随后因 runtime 未停稳抛异常时，这个事实仍然可查。
    本函数**只读**，绝不触发任何关闭。
    """
    getter = getattr(adapter, "close_state", None)
    if not callable(getter) or not attempt_id:
        return None
    try:
        facts = getter(attempt_id)
    except Exception:  # noqa: BLE001 - 读不到就不当作"已关闭"证据
        return None
    return facts if isinstance(facts, dict) else None


def _dsh_sdk_teardown_from_close(facts):
    """把关闭事实转成 teardown 段（R4：只记录，不再关闭）。

    ``closed`` 表示**关闭动作是否已执行**（不表示 runtime 是否已停稳）；
    停稳证据单独放在 ``runtimeClosed``。
    """
    teardown = {"closed": True,
                "runtimeClosed": facts.get("runtimeClosed"),
                "processExited": facts.get("processExited")}
    if facts.get("closeCalled") is not None:
        teardown["closeCalled"] = bool(facts["closeCalled"])
    if facts.get("closeError") is not None:
        teardown["closeError"] = facts["closeError"]
    return teardown


def _dsh_sdk_execution_from_close(facts):
    """``wait_and_close`` 抛异常时，用关闭事实补齐 execution 段（R4）。

    执行段异常退出时不能只剩默认值：关闭动作与停稳证据都要可查，
    但 ``runtimeClosed`` 仍如实为"未证明停稳"。
    """
    execution = {"finalState": facts.get("finalState"),
                 "endReason": facts.get("endReason"),
                 "runtimeClosed": facts.get("runtimeClosed"),
                 "processExited": facts.get("processExited"),
                 "timedOut": bool(facts.get("timedOut"))}
    if facts.get("closeCalled") is not None:
        execution["closeCalled"] = bool(facts["closeCalled"])
    if facts.get("closeError") is not None:
        execution["closeError"] = facts["closeError"]
    return execution


def _dsh_sdk_failure_teardown(adapter, attempt_id):
    """失败路径收尾（R2-2）：不等待终态，只做受控关闭并如实记账。

    - 只负责关闭，不重新提交、**不**把 ``submit_unknown`` 改写成"确定未执行"；
    - 关闭失败/复核失败写入 ``closeError``，如实上报，不掩盖最初的提交错误；
    - 幂等（R4）：由 ``Adapter.close_runtime`` 以「关闭动作是否已执行」判重，
      关闭动作已发生（哪怕 runtime 未停稳）时不会再次调用底层 close；
      重复调用也不会再次提交任务。
    """
    if not attempt_id:
        return {"closed": False, "reason": "no_attempt_bound"}
    try:
        closed = adapter.close_runtime(attempt_id)
    except Exception as exc:  # noqa: BLE001 - 收尾本身失败也要留下可查询证据
        return {"closed": False, "closeError": type(exc).__name__,
                "message": str(exc)[:200]}
    return _dsh_sdk_teardown_from_close(closed)


def _dsh_sdk_generic_failure(stage, submit_confirmed, attempt_id, exc):
    """普通异常 → 受控 D4 失败描述（R3，R2 cox review P1）。

    - **不新造冻结错误码**：沿用既有 ``submit_unknown``。理由是只要宿主 transport
      已构造，普通异常就不能让调用方以为"确定未执行"；即便提交已被确认，attempt
      也处在"已接收但采集 / 入账未完成"的不可确定状态。协议内 ``ContractError``
      仍保留自己的码（``path_escape`` / ``host_unconfirmed`` 等）。
    - 阶段、原始异常类型与（截断的）消息放进 details，供人工对账；
      不写入凭据、完整堆栈或任何业务数据。
    """
    return {
        "code": "submit_unknown",
        "message": "DSH SDK 生命周期在 %s 阶段抛出未受控异常，结果未知，不自动重试：%s"
                   % (stage, type(exc).__name__),
        "details": {
            "attemptId": attempt_id,
            "phase": stage,
            "error": type(exc).__name__,
            "errorMessage": str(exc)[:200],
            "submitConfirmed": bool(submit_confirmed),
            "genericException": True,
        },
    }


def _cmd_attempt_submit(args, services, synthetic, dsh_route=None):
    """提交执行轮次（worker 身份）。

    DSH SDK 路由（R1-2 / R2-2 / R2-3）在提交事实落库后，同一个 CLI 进程继续：
    等待终态 → 核对归属 → 转换 legacy 事件 → 安全写标准回执 → 入账 → 收尾。
    最终 JSON 分四段：submission（接收）/ execution（执行）/ collection（采集）/
    teardown（收尾）。

    R2-2 收尾责任：只要本子命令为 dsh-sdk 建立了宿主 transport，**所有出口**
    （成功 / 确定拒绝 / 未知 / 超时 / 异常）都必须有明确关闭责任人——``finally``
    对未关闭的 runtime 做受控关闭；重复调用不会再次提交任务。
    R2-3 失败语义：提交已确认但后续阶段失败时，CLI 不得返回"整链成功"，
    退出码非 0，同时把分段诊断保留在 ``details.lifecycle``。
    R4 关闭语义（cox review P1）：收尾判据是"关闭动作是否已执行"而不是
    "``wait_and_close`` 是否正常返回"；关闭动作已发生（包括它在 close 之后
    抛普通异常）时，``finally`` 只记录既有事实，不再触发第二次 ``close()``。
    """
    task = services["store"].get_task(args.task)
    if task is None:
        raise ContractError("invalid_task", message="任务不存在",
                            details={"taskId": args.task})
    host_transport = services["adapter"].transport if services.get("adapter") else None
    if isinstance(host_transport, CodeBuddyTransport) and host_transport.binding:
        if workbuddy_binding(args.agent) != host_transport.binding:
            raise ContractError("permission_denied", message="成员或专家绑定在提交前已变化，未提交")
    if isinstance(host_transport, CliGatewayTransport):
        try:
            host_transport.preflight(task)
        except GatewayTransportError as exc:
            raise ContractError(
                "permission_denied",
                message="宿主隔离配置预检未通过，任务未提交：%s" % exc,
            ) from exc
    context = worker_context(actor_id=args.agent, entry="d4ctl attempt submit",
                             synthetic=synthetic)
    tasks_service = services["tasks"]
    attempt_id = args.attempt

    if dsh_route is None:
        if not attempt_id:
            attempt = tasks_service.create_attempt(args.task, context=context)
            attempt_id = attempt["attemptId"]
        result = tasks_service.submit_attempt(args.task, attempt_id, context=context)
        result.setdefault("attemptId", attempt_id)
        if isinstance(host_transport, CodeBuddyTransport):
            receipt = services["adapter"].get_status(attempt_id)
            facts = {key: receipt.get(key) for key in
                     ("requestedModel", "observedModel", "binding", "workspace", "observedWorkspace", "hostState")}
            result["executionFacts"] = facts
            store = services["store"]
            with store.transaction():
                if not any(row["kind"] == "workbuddy.execution_facts"
                           and row["attempt_id"] == attempt_id
                           for row in store.list_activity(args.task)):
                    store.log_activity(args.task, attempt_id, "workbuddy.execution_facts", facts, now_iso())
        return result

    # -- DSH SDK 受控生命周期（R1-2 / R2-2）：提交与收尾在同一个 try/finally ----
    adapter = services["adapter"]
    summary = {}
    execution = {"finalState": None, "endReason": None, "runtimeClosed": False,
                 "processExited": None, "timedOut": False}
    collection = {"status": "not_attempted"}
    teardown = {"closed": False}
    submit_confirmed = False   # 宿主已确认接收才允许生成交付回执
    failure = None
    failure_stage = None
    failure_is_generic = False   # R3：区分协议内 ContractError 与协议外普通异常
    stage = "submission"
    try:
        if not attempt_id:
            attempt = tasks_service.create_attempt(args.task, context=context)
            attempt_id = attempt["attemptId"]
            args.attempt = attempt_id
        result = tasks_service.submit_attempt(args.task, attempt_id, context=context)
        result.setdefault("attemptId", attempt_id)
        submit_confirmed = True
        summary["submission"] = result
        # 1) 有界等待宿主终态并受控收尾。关闭动作一旦发生就先记账（teardown.closed），
        #    避免后续阶段异常时 finally 重复关闭（R2-2 "close 恰好一次"）。
        #    R4：teardown.closed 表示"关闭动作是否已执行"，不表示"runtime 已停稳"；
        #    停稳证据单独由 runtimeClosed 承载，closeCalled 与之一并如实上报。
        stage = "execution"
        execution = adapter.wait_and_close(
            attempt_id,
            timeout_seconds=max(1.0, args.dsh_sdk_execution_timeout_ms / 1000.0))
        teardown = _dsh_sdk_teardown_from_close(execution)
        # 2) 归属核对 + legacy 事件读取
        stage = "collection"
        binding = adapter.host_binding(attempt_id)
        legacy_events = adapter.collect_events(attempt_id)
        session_events = [e for e in legacy_events
                          if e.get("sessionId") == binding["sessionId"]]
        # 3) 事件转换 + 安全回执写入 + EventService 入账（R2-3）
        stage = "receipt"
        collection = _dsh_sdk_collect_events(
            services, task, attempt_id, binding, session_events, synthetic)
    except ContractError as exc:
        failure = exc
        failure_stage = stage
        if not submit_confirmed:
            # 提交前/提交中失败：保留首次错误语义（submit_unknown 不得被改写成
            # "确定未执行"）；attemptId 仍如实回填。
            summary["submission"] = {"attemptId": attempt_id, "error": exc.to_dict()}
        collection = {"status": "failed", "error": exc.code, "message": str(exc)}
        summary["lifecycleError"] = exc.to_dict()
        summary["failureStage"] = stage
    except Exception as exc:  # noqa: BLE001 - R3：普通异常也必须受控转换，不裸抛
        # R3（R2 cox review P1）：协议外的异常同样不能让调用方拿到裸异常。
        # 只接 Exception，不接 BaseException —— KeyboardInterrupt / SystemExit
        # 必须继续穿透，不能被生命周期逻辑吞掉。
        failure = exc
        failure_stage = stage
        failure_is_generic = True
        detail = _dsh_sdk_generic_failure(stage, submit_confirmed, attempt_id, exc)
        if not submit_confirmed:
            summary["submission"] = {"attemptId": attempt_id, "error": detail}
        collection = {"status": "failed", "error": detail["code"],
                      "message": detail["message"]}
        summary["lifecycleError"] = detail
        summary["failureStage"] = stage
    finally:
        # R2-2：只要建立了宿主 transport，任何出口都必须有收尾责任人。
        # R4（cox review P1）：收尾判据是**「关闭动作是否已执行」**，不是
        # "wait_and_close 是否正常返回"。关闭动作已发生（含 wait_and_close
        # 在 close 之后抛普通异常、或异常对象携带了关闭状态）时，只把既有
        # 事实记进 execution / teardown，**绝不再次关闭**；只有确认关闭
        # 尚未发生时，才进入失败路径收尾（那里同样只会 close 一次）。
        if not teardown.get("closed"):
            facts = _dsh_sdk_close_facts(adapter, attempt_id)
            if facts is None:
                attached = getattr(failure, "close_state", None)
                facts = attached if isinstance(attached, dict) else None
            if facts is None:
                teardown = _dsh_sdk_failure_teardown(adapter, attempt_id)
            else:
                execution = {**execution, **_dsh_sdk_execution_from_close(facts)}
                teardown = _dsh_sdk_teardown_from_close(facts)
        summary["execution"] = execution
        summary["collection"] = collection
        summary["teardown"] = teardown

    if failure is not None:
        # 退出码规则（不冒充质量结论）：
        # - submission / receipt 阶段的 ContractError（R2-2 / R2-3）：原样非 0，
        #   错误码保持 submit_unknown / path_escape 等既有语义；
        # - **任何阶段的普通异常**（R3）：协议外出口，必须非 0 —— 否则调用方
        #   无法从 exit code 察觉自己收到的是异常终止而不是受控结果；
        # - execution / collection 阶段的 ContractError（有界等待超时、未停稳、
        #   归属读不到）是协议内受控结果，沿用 R1 的分段返回，避免把"等待超时"
        #   误报成链级失败。
        if failure_is_generic:
            generic = summary["lifecycleError"]
            raise DshLifecycleAbort(generic["code"], message=generic["message"],
                                    summary=summary, details=dict(generic["details"]))
        if failure_stage in ("submission", "receipt"):
            raise DshLifecycleAbort(failure.code, message=str(failure), summary=summary,
                                    details=dict(failure.details or {}))
        return summary
    return summary


def _dsh_sdk_collect_events(services, task, attempt_id, binding, session_events,
                            synthetic):
    """legacy 事件 → D4 标准回执 → EventService 入账（R1-2 步骤 3-5）。

    只有提交已被宿主接收（submission.status == accepted_by_host 且有
    messageId）才写回执；否则记 not_accepted，不造成功交付。
    写入位置遵循 file_receipts 合同：workspace/receipts/<taskId>/<attemptId>/
    events.json，bundle.hostRunId 必须等于宿主 messageId。
    """
    store = services["store"]
    events_service = services["events"]
    attempt = store.get_attempt(attempt_id)
    if attempt is None:
        raise ContractError("invalid_task", message="执行轮次不存在",
                            details={"attemptId": attempt_id})
    submission_json = json.loads(attempt["submission_json"] or "{}")
    host_receipt = submission_json.get("host_receipt")
    if not (isinstance(host_receipt, str) and host_receipt.strip()):
        return {"status": "not_accepted",
                "reason": "宿主未确认接收（无 messageId），不生成交付回执"}
    # 事件映射：turn/start → task.started；turn/end(completed) → 不直接交付
    # （交付需要 worker 真实报告；SDK 的 turn 结束只证明宿主轮次结束）；
    # error/异常 → task.blocked（保留原始原因）。
    mapped = []
    for index, event in enumerate(session_events):
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "turn/start":
            mapped.append({
                "eventId": "dshsdk-%s-%d" % (attempt_id, event.get("seq", index)),
                "projectId": task["project_id"], "taskId": task["task_id"],
                "attemptId": attempt_id, "sourceId": DSH_SDK_SOURCE_ID,
                "agentId": task["agent_id"], "sequence": index + 1,
                "type": "task.started", "occurredAt": now_iso(),
                "payload": {"note": "SDK turn/start 已核验（seq=%s）" % event.get("seq")},
            })
        elif kind == "turn/end":
            reason = data.get("reason") or {}
            if reason.get("kind") == "error":
                mapped.append({
                    "eventId": "dshsdk-%s-%d" % (attempt_id, event.get("seq", index)),
                    "projectId": task["project_id"], "taskId": task["task_id"],
                    "attemptId": attempt_id, "sourceId": DSH_SDK_SOURCE_ID,
                    "agentId": task["agent_id"], "sequence": index + 1,
                    "type": "task.blocked", "occurredAt": now_iso(),
                    "payload": {"reason": "SDK turn/end error: %s"
                                % json.dumps(reason, ensure_ascii=False)[:200],
                                "owner": "human", "resumeWhen": "人工核对宿主失败原因"},
                })
            # completed 不映射为交付：SDK 轮次完成 ≠ 任务交付（P1-1 语义保留）
    if not mapped:
        return {"status": "no_state_changing_events",
                "reason": "本轮无状态推进事件（turn/end completed 不自动交付）",
                "legacyEventCount": len(session_events)}
    bundle = {"hostRunId": host_receipt, "events": mapped}
    from service.context import source_context as _sc
    context = _sc(DSH_SDK_SOURCE_ID, entry="d4ctl attempt submit (dsh-sdk lifecycle)",
                  synthetic=synthetic)
    canonical_path = (Path(task["workspace"]) / "receipts" / task["task_id"]
                      / attempt_id / "events.json")
    # R3 补正（R2 遗留回归）：同一 attempt 重跑时，TaskService 回放既有提交事实，
    # 生命周期仍会走一遍；而事件转换里的 now_iso() 是秒级，两次生成的回执字节
    # 必然漂移（跨秒即不同），会被 R2-3 的"不同内容拒绝覆盖"拦下 → 重放反而失败
    # （R1 的直接 write_text 覆盖写不会）。回执是该 attempt 已固化的事实：已存在
    # 就直接复用做幂等导入，不重新生成；写入层的防覆盖约束保持不动。
    if canonical_path.is_file():
        imported = import_receipt(
            store, events_service, task["task_id"], attempt_id, str(canonical_path),
            context)
        return {"status": "replayed" if imported.get("replayed") else "imported",
                "eventCount": len(imported.get("events", [])),
                "receiptPath": str(canonical_path),
                "receiptWrite": "reused-existing"}
    # R2-3：写入前先做目录约束（writeScope 声明层 + 逐级 O_NOFOLLOW 目录 fd +
    # 同目录原子发布）。不再直接 mkdir/write_text：越界时外部不落任何字节，
    # 异常向上传播为 CLI 非 0（不再出现"先越界写、后拒绝导入但仍 exit 0"）。
    receipt_path, write_state = _dsh_sdk_write_receipt(
        task, task["task_id"], attempt_id, bundle)
    # 入账：复用 file_receipts 的 import_receipt（含 hostRunId 核对、路径校验、
    # synthetic 标记、幂等重放）。context.source_id == DSH_SDK_SOURCE_ID。
    imported = import_receipt(
        store, events_service, task["task_id"], attempt_id, str(receipt_path),
        context)
    return {"status": "imported" if not imported.get("replayed") else "replayed",
            "eventCount": len(imported.get("events", [])),
            "receiptPath": str(receipt_path),
            "receiptWrite": write_state}


def _unlink_child(directory_fd, name):
    """尽力删除受控目录下的临时文件；失败不改变原始错误。"""
    try:
        os.unlink(name, dir_fd=directory_fd)
    except OSError:
        pass


def _open_receipt_dir(parent_fd, name):
    """在受控父目录 fd 下打开/创建子目录；拒绝 symlink、普通文件与特殊文件。

    每一级都走 ``O_DIRECTORY|O_NOFOLLOW``：只要路径中某一级被换成 symlink，
    打开动作本身就失败（ELOOP/ENOTDIR），不会跟随到目录外。
    """
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise ContractError("path_escape",
                            message="回执目录不可创建（%s）：%s" % (name, exc))
    try:
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                     dir_fd=parent_fd)
    except OSError as exc:
        raise ContractError(
            "path_escape",
            message="回执目录不可安全打开（拒绝 symlink/非目录）：%s（%s）" % (name, exc))
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise ContractError("path_escape", message="回执路径不是普通目录：%s" % name)
    return fd


def _read_existing_receipt(directory_fd):
    """读取已存在的 events.json（O_NOFOLLOW + 独立普通文件 + 大小上限）。

    不存在返回 ``None``；目标是 symlink/特殊文件/硬链接或超限则拒绝。
    """
    try:
        fd = os.open("events.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ContractError(
            "path_escape",
            message="已存在的回执不可安全读取（拒绝 symlink/特殊文件）：%s" % exc)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_size > DSH_SDK_RECEIPT_MAX_BYTES):
            raise ContractError("path_escape",
                                message="已存在的回执不是大小受限的独立普通文件")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read(DSH_SDK_RECEIPT_MAX_BYTES + 1)
    finally:
        os.close(fd)


def _dsh_sdk_write_receipt(task, task_id, attempt_id, bundle):
    """R2-3：在 ``workspace/receipts/<taskId>/<attemptId>/events.json`` 内安全落盘。

    写入前两层约束：

    1. **声明层**：回执相对路径必须落在任务 writeScope 内（复用
       ``contracts.paths.assert_within_scope``，与交付文件同一判据）；
    2. **实际层**：从 workspace 起逐级用受控目录 fd（``O_DIRECTORY|O_NOFOLLOW``）
       打开 ``receipts`` / ``taskId`` / ``attemptId``，拒绝 symlink、普通文件与
       特殊文件；再用同一目录 fd 建大小受限临时普通文件
       （``O_CREAT|O_EXCL|O_NOFOLLOW``），fsync 后 ``os.replace`` **同目录内原子发布**。

    因为发布走的是已打开的目录 fd，即使路径上的目录在写入过程中被并发替换成
    symlink，数据仍落在原目录 inode 内，不会写到外部目标——单纯 realpath 检查
    提供不了这个保证，故不能替代本层。

    已存在同内容回执 → 幂等复用（``reused``，不改字节）；内容不同 → 拒绝覆盖。

    返回 ``(receipt_path, state)``：state ∈ {"written", "reused"}。
    """
    for label, value in (("taskId", task_id), ("attemptId", attempt_id)):
        if (not isinstance(value, str) or not value.strip()
                or "/" in value or value in (".", "..")):
            raise ContractError("path_escape", message="回执目录名非法：%s" % label)
    workspace = task.get("workspace")
    if not isinstance(workspace, str) or not workspace.startswith("/"):
        raise ContractError("path_escape", message="回执写入需要绝对 workspace")
    relative = "receipts/%s/%s/events.json" % (task_id, attempt_id)
    permissions = json.loads(task.get("permissions_json") or "{}")
    assert_within_scope(workspace, permissions.get("writeScope") or [], relative)

    payload = json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8")
    if len(payload) > DSH_SDK_RECEIPT_MAX_BYTES:
        raise ContractError("invalid_event", message="回执超过大小上限，拒绝写入")

    receipt_path = Path(workspace) / relative
    directory_fd = None
    try:
        directory_fd = os.open(workspace,
                               os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for name in ("receipts", task_id, attempt_id):
            child_fd = _open_receipt_dir(directory_fd, name)
            os.close(directory_fd)
            directory_fd = child_fd
        existing = _read_existing_receipt(directory_fd)
        if existing is not None:
            if existing != payload:
                raise ContractError(
                    "path_escape",
                    message="已存在内容不同的回执，拒绝覆盖（仅同内容允许幂等复用）")
            return receipt_path, "reused"
        tmp_name = ".events-%s.tmp" % os.urandom(6).hex()
        file_fd = os.open(tmp_name,
                          os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                          0o600, dir_fd=directory_fd)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(file_fd, payload[offset:])
            os.fsync(file_fd)
        except BaseException:
            os.close(file_fd)
            _unlink_child(directory_fd, tmp_name)
            raise
        os.close(file_fd)
        try:
            os.replace(tmp_name, "events.json",
                       src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        except OSError as exc:
            _unlink_child(directory_fd, tmp_name)
            raise ContractError("path_escape", message="回执原子发布失败：%s" % exc)
        return receipt_path, "written"
    except OSError as exc:
        raise ContractError("path_escape", message="回执写入被拒绝：%s" % exc)
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _cmd_event_ingest(args, services, synthetic):
    payload = _load_json_file(args.file)
    event_type = payload.get("type")
    if event_type not in CLI_EVENT_TYPES:
        # G3：第一版只支持 worker 主动上报这四种；task.accepted 由宿主回执产生，
        # source.heartbeat 不走本入口。chat.history 自动翻译明确不做。
        raise ContractError(
            "invalid_event",
            message="第一版事件入口只支持 worker 上报 %s" % (", ".join(CLI_EVENT_TYPES),),
            details={"type": event_type},
        )
    # 只拦截“提供且不一致”；缺字段留给服务层报 invalid_event，语义更准确。
    if payload.get("sourceId") is not None and payload["sourceId"] != args.source:
        raise ContractError(
            "permission_denied",
            message="事件 sourceId 与受控入口 --source 不一致",
            details={"payloadSourceId": payload.get("sourceId"), "cliSourceId": args.source},
        )
    context = source_context(source_id=args.source, entry="d4ctl event ingest",
                             synthetic=synthetic)
    return services["events"].ingest(payload, context)


def _cmd_review_submit(args, services, synthetic):
    payload = _load_json_file(args.file)
    context = reviewer_context(actor_id=args.reviewer, entry="d4ctl review submit",
                               synthetic=synthetic)
    return services["reviews"].submit_review(args.task, payload, context)


def _cmd_review_relay(args, services, synthetic):
    """把最新已落库的 changes_requested 回传给同一 agentId + sessionKey。

    只读审核事实、不生成审核事实；失败保留待发状态，不自动重试。
    """
    store = services["store"]
    task = store.get_task(args.task)
    if task is None:
        raise ContractError("invalid_task", message="任务不存在",
                            details={"taskId": args.task})
    reviews = store.list_reviews(args.task)
    # 只看最新一条审核决定：旧的 changes_requested 已被后续决定覆盖，不回传。
    target = None
    if reviews:
        # review_round 是服务层在同一事务内分配的单调顺序，不能用时间戳/随机 reviewId 推断先后。
        latest = max(reviews, key=lambda row: (row.get("review_round", 0), row.get("review_id", "")))
        if latest["decision"] == "changes_requested" and latest["resulting_attempt_id"]:
            target = latest
    if target is None:
        raise ContractError(
            "invalid_transition",
            message="没有待回传的返工决定（需要 changes_requested 且已生成新执行轮次）",
            details={"taskId": args.task},
        )
    sent = _relay_already_sent(store, args.task, target["review_id"])
    if sent is not None:
        raise ContractError(
            "invalid_transition",
            message="该返工决定已回传过，不重复发送",
            details={"reviewId": target["review_id"], "runId": sent.get("runId")},
        )
    new_attempt_id = target["resulting_attempt_id"]
    if task["status"] != "active" or task["current_attempt_id"] != new_attempt_id:
        raise ContractError(
            "invalid_transition",
            message="任务状态已漂移（不是执行中或当前轮次已变化），拒绝回传",
            details={
                "taskStatus": task["status"],
                "currentAttemptId": task["current_attempt_id"],
                "reviewAttemptId": new_attempt_id,
            },
        )
    attempt = store.get_attempt(new_attempt_id)
    if attempt is None:
        raise ContractError(
            "invalid_task",
            message="返工决定引用的新执行轮次不存在",
            details={"attemptId": new_attempt_id},
        )
    context = reviewer_context(actor_id=args.reviewer, entry="d4ctl review relay",
                               synthetic=synthetic)
    if attempt["submit_state"] == "submitted":
        # Recovery after accepted submission but before activity logging must not send again.
        relay_receipt(json.loads(attempt["submission_json"]), target["review_id"])
    if attempt["submit_state"] == "created" and isinstance(services["adapter"].transport, CliGatewayTransport):
        try:
            services["adapter"].transport.preflight(task)
        except GatewayTransportError as exc:
            raise ContractError("permission_denied", message="隔离预检拒绝，返工未发送：%s" % exc)
    relay_tasks = TaskService(
        store, adapter=ReworkSubmissionAdapter(services["adapter"], target),
        allowed_roots=services["tasks"].allowed_roots,
    )
    submitted = relay_tasks.submit_attempt(
        args.task, new_attempt_id,
        worker_context(actor_id=task["agent_id"], entry="d4ctl review relay submission",
                       synthetic=synthetic),
    )
    receipt = relay_receipt(submitted["submission"], target["review_id"])
    with store.transaction():
        if _relay_already_sent(store, args.task, target["review_id"]) is not None:
            return {"taskId": args.task, "reviewId": target["review_id"],
                    "attemptId": new_attempt_id, **receipt, "replayed": True}
        store.log_activity(
            args.task, new_attempt_id, "review.relay_sent",
            {
                "reviewId": target["review_id"],
                "attemptId": new_attempt_id,
                "runId": receipt["runId"],
                "sessionKey": receipt["sessionKey"],
                "idempotencyKey": receipt["idempotencyKey"],
                "synthetic": bool(context.synthetic),
                "relayedBy": context.to_dict(),
            },
            now_iso(),
        )
    return {
        "taskId": args.task,
        "reviewId": target["review_id"],
        "attemptId": new_attempt_id,
        "runId": receipt["runId"],
        "status": receipt["status"],
        "sessionKey": receipt["sessionKey"],
        "idempotencyKey": receipt["idempotencyKey"],
    }


def _cmd_report_import(args, services, synthetic):
    """正式报告导入：绑定到指定当前交付，失败不改任务状态。

    与 `event import` 的关系（两者互斥、互不替代）：
    - `event import`：读取本轮目录里的 events.json 事件包，交给既有事件状态机；
    - `report import`：只读取正式 Markdown 报告并绑定到已存在的当前交付；
      它不接收事件、不创建任务/轮次/交付/审核，也不改变任务状态。
    两者都复用同一套 fail-closed 与「首次结果重放」语义，CLI 不实现第二套状态机。
    """
    _require_absolute(args.file, "输入文件路径")
    if (args.source_card_id is None) != (args.source_card_revision is None):
        raise ValueError("--source-card-id 与 --source-card-revision 必须同时提供或都不提供")
    context = reviewer_context(actor_id=args.reviewer, entry="d4ctl report import",
                               synthetic=synthetic)
    return import_report(
        services["store"], args.task, args.attempt, args.delivery, args.file, context,
        allowed_roots=[args.allowed_root], cli_agent=args.agent, cli_source=args.source,
        source_card_id=args.source_card_id, source_card_revision=args.source_card_revision,
    )


def _cmd_query(args, services, _synthetic):
    read_models = services["read_models"]
    if args.task:
        if args.activity:
            return read_models.task_history(args.task)
        return read_models.task_detail(args.task)
    if args.project:
        return read_models.project_summary(args.project)
    if args.review_queue:
        return read_models.review_queue()
    raise ValueError("query 需要至少一个参数：--task / --project / --review-queue")


# ---------------------------------------------------------------------------
# agent list / agent inspect：只读成员投影入口。
#
# CLI 只做四件事：打开已存在数据库的 read-only 连接（mode=ro，无任何 PRAGMA
# 赋值）、解析受控参数、缺省 --registry 时用
# storage.registry_migrations.inspect_registry_schema 只读取回库内 candidate
# registryId、把连接与该 ID 原样交给既有只读服务。成员/schema 校验、事务和稳定
# 错误语义全部由 service.registry_read / service.registry_detail /
# storage.registry_migrations 承担，本文件不复制服务投影逻辑、不拼 Registry SQL。
# ---------------------------------------------------------------------------

def _resolve_registry_id(conn, declared):
    """解析本轮 registryId：显式 ``--registry`` 优先，缺省时只读发现。

    缺省才发现复用既有只读 ``inspect_registry_schema(conn)``（同一 read-only
    连接、非迁移路径）：不建表、不迁移、不改元数据、不猜测成员。库未迁移时按
    与只读服务一致的稳定错误语义失败关闭（code=invalid_task、
    field=registry、reason=registry_unmigrated），绝不退化成"全库/空列表"。
    """
    if declared is not None:
        return declared
    inspection = inspect_registry_schema(conn)
    if inspection.get("status") != "ready":
        raise ContractError(
            "invalid_task",
            message="成员只读入口拒绝：%s（field=registry）" % (REASON_REGISTRY_UNMIGRATED,),
            details={"field": "registry", "reason": REASON_REGISTRY_UNMIGRATED},
        )
    return inspection["registryId"]


def _cmd_agent(args, conn):
    """agent 只读入口分发：list / inspect。"""
    if args.sub == "list":
        return _cmd_agent_list(args, conn)
    return _cmd_agent_inspect(args, conn)


def _cmd_agent_list(args, conn):
    """成员列表：host/role/enabled 全部转交既有服务校验（enabled 已由
    argparse 限制为小写 true/false，这里只转成严格 bool）；registryId 显式传入
    或由既有只读 schema 检查取得，校验同样在服务内完成。
    """
    enabled = None
    if args.enabled is not None:
        enabled = args.enabled == "true"
    result = list_registry_agents(
        conn, registry_id=_resolve_registry_id(conn, args.registry),
        host=args.host, role=args.role, enabled=enabled,
    )
    if args.format == "json":
        # JSON：保留服务返回的完整投影，不加列、不裁剪。
        return result
    return {
        "format": "table",
        "table": _render_agent_list_table(result),
        "result": result,
    }


def _cmd_agent_inspect(args, conn):
    """成员详情：输出服务返回的完整详情投影（registry-agent-detail.v1）。"""
    return inspect_registry_agent(
        conn, registry_id=_resolve_registry_id(conn, args.registry),
        agent_id=args.agent_id)


def _render_agent_list_table(result):
    """把列表投影渲染成可读文本：只显示原始登记字段与检查记录。

    不推导 online / ready / 可接任务等状态列；服务完整投影原样附带在
    result 字段，检查 failureReason 等错误信息不会因 table 渲染丢失。
    """
    registry = result["registry"]
    filters = result["filters"]
    lines = [
        "registry %s (schemaVersion=%s authority=%s revision=%s)"
        % (registry["registryId"], registry["schemaVersion"],
           registry["authority"], registry["revision"]),
        "sourceDigest: %s" % registry["sourceDigest"],
        "filters: host=%s role=%s enabled=%s"
        % (filters["host"], filters["role"], filters["enabled"]),
        "members: %d" % len(result["items"]),
        "wouldWrite=%s wouldDispatch=%s authorityUnchanged=%s"
        % (result["wouldWrite"], result["wouldDispatch"],
           result["authorityUnchanged"]),
    ]
    for item in result["items"]:
        environment = item["environment"]
        binding = item["binding"]
        model_profile = item["modelProfile"]
        lines.extend([
            "",
            "agentId: %s" % item["agentId"],
            "  alias: %s (normalized: %s)"
            % (item["agentAlias"], item["normalizedAlias"]),
            "  displayName: %s" % item["displayName"],
            "  roles: %s" % json.dumps(item["roles"], ensure_ascii=False),
            "  capabilities: %s"
            % json.dumps(item["capabilities"], ensure_ascii=False),
            "  enabled: %s registrationComplete: %s revision: %s"
            % (item["enabled"], item["registrationComplete"], item["revision"]),
            "  allowedRoots: %s"
            % json.dumps(item["allowedRoots"], ensure_ascii=False),
            "  environment: %s rev=%s hostType=%s instanceId=%s adapter=%s enabled=%s"
            % (environment["environmentId"], environment["revision"],
               environment["hostType"], environment["instanceId"],
               environment["adapterType"], environment["enabled"]),
            "  binding: rev=%s hostAgentId=%s executionProfileRef=%s"
            % (binding["bindingRevision"], binding["hostAgentId"],
               binding["executionProfileRef"]),
            "  modelProfile: %s rev=%s strategy=%s provider=%s modelId=%s"
            % (model_profile["modelProfileId"], model_profile["profileRevision"],
               model_profile["strategy"], model_profile["provider"],
               model_profile["modelId"]),
            "  createdAt: %s updatedAt: %s" % (item["createdAt"], item["updatedAt"]),
        ])
        for project in item["projects"]:
            lines.append(
                "  project: %s roles=%s enabled=%s revision=%s"
                % (project["projectId"],
                   json.dumps(project["roles"], ensure_ascii=False),
                   project["enabled"], project["revision"]))
        for check in item["checks"]:
            lines.append(
                "  check: %s type=%s status=%s evidence=%s observedAt=%s expiresAt=%s"
                % (check["checkId"], check["checkType"], check["status"],
                   check["evidenceLevel"], check["observedAt"], check["expiresAt"]))
            if check["failureReason"]:
                lines.append("    failureReason: %s" % check["failureReason"])
    return "\n".join(lines)


def _create_attempt_only(store, args, context):
    """R1-6 辅助：版本预检通过后，先建执行轮次（不提交）。

    dsh-sdk 路由需要在构造 transport 前确定 attemptId（store 按轮次分目录），
    而 TaskService.submit_attempt 才是权威占位入口；这里只走 create_attempt，
    占位/提交语义不变。
    """
    tasks_service = TaskService(store, adapter=None, allowed_roots=[args.allowed_root])
    attempt = tasks_service.create_attempt(args.task, context=context)
    return attempt["attemptId"]


def run(argv, transport=None, synthetic=False, dsh_transport_factory=None):
    """执行一次 CLI 调用。

    Returns:
        (exit_code, result_dict)：result_dict 为脱敏、可审计的 JSON 结构。

    transport:
        测试注入的 GatewayTransport。注入即视为离线链路（synthetic 强制 True），
        防止离线 mock 回执被写成真实宿主证据。

    dsh_transport_factory:
        测试注入的 DSH SDK legacy transport 工厂，签名为
        ``(task, attempt_id) -> (legacy_transport, sdk_store)``。
        注入即 SYNTHETIC（同上），用于 R1-2/R1-4/R1-5 生命周期离线验收；
        不经生产策略门（门禁独立测试：env=1 + 缺材料仍拒绝）。
    """
    parser = build_parser()
    args = parser.parse_args(list(argv))
    cmd = " ".join([args.cmd] + ([args.sub] if getattr(args, "sub", None) else []))

    try:
        db_path = _require_absolute(args.db, "--db")
        allowed_root = _require_absolute(args.allowed_root, "--allowed-root")
    except ValueError as exc:
        return _err(cmd, "usage_error", str(exc))

    store = None
    try:
        _validate_fixed_identities(args)
        if args.cmd == "document":
            # 只读预检：不打开/创建数据库，不注入写身份，不调用 Adapter/宿主。
            return _ok(cmd, _cmd_document_inspect(args))
        if args.cmd == "project":
            # D4-PROJECT-01 只读预检：同样在打开/创建数据库之前返回。
            return _ok(cmd, _cmd_project_inspect(args))
        if args.cmd == "query" and not Path(db_path).is_file():
            raise ValueError("查询数据库不存在；只读入口不会创建数据库")
        if args.cmd == "agent" and not Path(db_path).is_file():
            raise ValueError("数据库不存在；agent 只读入口不会创建数据库")
        if args.cmd == "agent":
            # agent list / agent inspect：只读成员投影。裸 read-only 连接
            # （mode=ro，无任何 PRAGMA 赋值、不建库、不迁移、不登记固定身份），
            # 直接交给既有只读服务；registryId 缺省时由既有只读
            # inspect_registry_schema 取得。数据库未迁移 / registry 损坏 /
            # 成员不存在按服务的稳定错误语义失败关闭（invalid_task + field/reason）。
            conn = None
            try:
                conn = sqlite3.connect(
                    Path(db_path).resolve().as_uri() + "?mode=ro",
                    uri=True, isolation_level=None)
                conn.row_factory = sqlite3.Row
                data = _cmd_agent(args, conn)
            finally:
                if conn is not None:
                    conn.close()
            return _ok(cmd, data)
        store = SqliteStore(db_path, read_only=args.cmd == "query")
        # query 必须真正只读；固定 registry 只在写入口进行一次性 bootstrap。
        if args.cmd != "query":
            store.register_source(args.source, label="d4ctl 受控来源")
            store.register_agent(D4_LOGICAL_AGENT_ID, label="d4ctl worker")
            if args.source == WORKBUDDY_SOURCE_ID and args.agent in WORKBUDDY_AGENT_IDS:
                for member in WORKBUDDY_AGENT_IDS:
                    store.register_agent(member, label="WorkBuddy " + member)
            store.register_agent(D4_REVIEWER_ID, label="d4ctl reviewer")

        needs_host = (
            (args.cmd == "attempt" and args.sub == "submit")
            or (args.cmd == "review" and args.sub == "relay")
        )
        workbuddy = _workbuddy_requested(args)
        dsh_sdk = _dsh_sdk_requested(args)
        # 路由互斥（§5.3）：一条命令只能选择一种宿主通道。
        if workbuddy and dsh_sdk:
            raise ContractError(
                "permission_denied",
                message="宿主路由互斥：workbuddy 与 dsh-sdk 参数不能同时提供",
                details={"routes": ["workbuddy", "dsh-sdk"]},
            )
        if dsh_sdk and (getattr(args, "gateway_bin", None)
                        or getattr(args, "gateway_state_dir", None)):
            raise ContractError(
                "permission_denied",
                message="宿主路由互斥：dsh-sdk 与 gateway 参数不能同时提供",
                details={"routes": ["dsh-sdk", "gateway"]},
            )
        # 路由与来源必须一一对应：不允许用 WorkBuddy 通道冒充 qclaw 来源，或反之。
        # 只在真正要提交宿主的子命令上校验：--workbuddy-*/--dsh-sdk-* 只挂在 attempt submit 上，
        # 若对 task create 也强制"来源必须有路由"，WorkBuddy/DSH 任务将永远建不出来。
        if needs_host:
            if workbuddy and args.source != WORKBUDDY_SOURCE_ID:
                raise ContractError(
                    "permission_denied",
                    message="WorkBuddy 路由必须搭配 --source %s" % (WORKBUDDY_SOURCE_ID,),
                    details={"source": args.source, "required": WORKBUDDY_SOURCE_ID},
                )
            if not workbuddy and args.source == WORKBUDDY_SOURCE_ID:
                raise ContractError(
                    "permission_denied",
                    message="WorkBuddy 来源必须走 --workbuddy-bin + --workbuddy-home 路由",
                    details={"source": args.source},
                )
            if dsh_sdk and args.source != DSH_SDK_SOURCE_ID:
                raise ContractError(
                    "permission_denied",
                    message="DSH SDK 路由必须搭配 --source %s" % (DSH_SDK_SOURCE_ID,),
                    details={"source": args.source, "required": DSH_SDK_SOURCE_ID},
                )
            if not dsh_sdk and args.source == DSH_SDK_SOURCE_ID:
                raise ContractError(
                    "permission_denied",
                    message="DSH SDK 来源必须走 --dsh-sdk-bin + --dsh-sdk-home 路由",
                    details={"source": args.source},
                )
        adapter = None
        eff_synthetic = synthetic
        dsh_route = None
        dsh_injected = bool(dsh_transport_factory)
        if needs_host and dsh_sdk and dsh_injected:
            # 离线测试注入（R1-2/R1-4/R1-5 生命周期验收）：工厂构造 legacy
            # transport + store。注入即 SYNTHETIC（eff_synthetic 强制 True），
            # 不允许离线 mock 回执写成 LOCAL 或绕过 synthetic 校验；不触发
            # 生产策略门（门禁语义由独立测试覆盖：env=1+缺材料仍拒绝）。
            task = store.get_task(args.task)
            if task is None:
                raise ContractError("invalid_task", message="任务不存在",
                                    details={"taskId": args.task})
            context_attempt = args.attempt or None
            if context_attempt is None:
                from service.context import worker_context as _wc
                boot = _wc(actor_id=args.agent, entry="d4ctl attempt submit (route)",
                           synthetic=True)
                context_attempt = _create_attempt_only(store, args, boot)
            legacy_transport, sdk_store = dsh_transport_factory(
                task, context_attempt)
            adapter = DshSdkAdapter(legacy_transport, sdk_store)
            eff_synthetic = True
            dsh_route = {"store": sdk_store, "task": args.task}
            args.attempt = context_attempt
        elif needs_host and dsh_sdk:
            # DSH SDK 路由（R2-1）：真实路由**无条件** fail closed。环境变量与三份
            # 人工 JSON 只是非授权诊断材料，任意字段组合都不能让路由变成"可投递"。
            # 拒绝发生在创建轮次之前 → 零 attempt、零 SDK 启动、零 prompt。
            # 授权门之后的启动链（前置版本预检 → 建轮次 → legacy transport）完整
            # 保留，供后续「受信配置 + 运行时隔离核验 + 双网络授权」实施卡启用；
            # 它的性质由单元级回归覆盖（见 tests/test_d4ctl.py）。
            authorized, gate_detail = _dsh_sdk_authorized()
            if not authorized:
                raise ContractError(
                    "permission_denied",
                    message=gate_detail.get("reason",
                                            "DSH SDK 真实路由未授权（fail closed）"),
                    details={"source": args.source, "gate": gate_detail},
                )
            task = store.get_task(args.task)
            if task is None:
                raise ContractError("invalid_task", message="任务不存在",
                                    details={"taskId": args.task})
            # R1-6：版本预检在创建提交占位**和**创建轮次之前（数据库零污染）。
            # 预检只需要 bin/home/demo（不含 attempt 目录）。
            base_command, workspace, env = _resolve_dsh_sdk_base(args, task)
            _dsh_sdk_version_preflight(base_command, workspace, env)
            # R1-4：确定 attempt（缺省先建轮次），store 按轮次分目录。
            context_attempt = args.attempt or None
            if context_attempt is None:
                from service.context import worker_context as _wc
                boot = _wc(actor_id=args.agent, entry="d4ctl attempt submit (route)",
                           synthetic=synthetic)
                context_attempt = _create_attempt_only(store, args, boot)
            command, sdk_store = _resolve_dsh_sdk_store(args, context_attempt)
            from orchestrator.dsh import DSHAdapter as LegacyDshTransport
            transport = LegacyDshTransport(
                sdk_store, command, workspace, env,
                timeout=max(1.0, args.timeout_ms / 1000.0),
                execution_timeout=max(1.0, args.dsh_sdk_execution_timeout_ms / 1000.0),
            )
            adapter = DshSdkAdapter(transport, sdk_store)
            eff_synthetic = False
            dsh_route = {"store": sdk_store, "task": args.task}
            # _cmd_attempt_submit 复用 args.attempt 为空时的建轮次逻辑：
            # 这里已经建好（context_attempt），把 args.attempt 指向它，
            # 服务层 submit_attempt 走占位+CAS，不再重复建轮次。
            args.attempt = context_attempt
        elif needs_host and workbuddy:
            task = store.get_task(args.task)
            if task is None:
                raise ContractError("invalid_task", message="任务不存在",
                                    details={"taskId": args.task})
            binary, home = _resolve_workbuddy(args)
            binding = workbuddy_binding(args.agent) if args.agent in WORKBUDDY_AGENT_IDS else None
            # store 派生在隔离 HOME 之下，且必须与 workspace 分离（适配器会再校验权限）。
            cb_store = str(Path(home) / "d4-workbuddy-store")
            resolved = CodeBuddyTransport(
                command=[binary],
                workspace=task["workspace"],
                store_dir=cb_store,
                env={"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                     "CODEBUDDY_DISABLE_AUTO_MEMORY": "1",
                     "CODEBUDDY_MEMORY_ENABLED": "false",
                     "CODEBUDDY_TEAM_MEMORY_ENABLED": "false"},
                timeout=max(1.0, args.workbuddy_timeout_ms / 1000.0),
                model=args.workbuddy_model,
                binding=binding,
            )
            adapter = WorkBuddyAdapter(resolved, cb_store)
        elif needs_host:
            resolved, eff_synthetic = _resolve_transport(args, cmd, transport, synthetic)
            adapter = QClawGatewayAdapter(
                resolved,
                submit_timeout_ms=args.timeout_ms, relay_timeout_ms=args.timeout_ms,
            )

        services = {
            "store": store,
            "tasks": TaskService(store, adapter=adapter, allowed_roots=[allowed_root]),
            "events": EventService(store),
            "reviews": None,
            "read_models": ReadModels(store),
            "adapter": adapter,
        }
        services["reviews"] = ReviewService(store, services["tasks"])

        if args.cmd == "task":
            data = _cmd_task_create(args, services, eff_synthetic)
        elif args.cmd == "attempt":
            data = _cmd_attempt_submit(args, services, eff_synthetic, dsh_route=dsh_route)
        elif args.cmd == "event":
            if args.sub == "import":
                _require_absolute(args.file, "输入文件路径")
                data = import_receipt(store, services["events"], args.task, args.attempt,
                                      args.file, source_context(args.source, entry="d4ctl event import",
                                                                synthetic=eff_synthetic))
            else:
                data = _cmd_event_ingest(args, services, eff_synthetic)
        elif args.cmd == "report":
            data = _cmd_report_import(args, services, eff_synthetic)
        elif args.cmd == "review":
            data = (_cmd_review_submit(args, services, eff_synthetic)
                    if args.sub == "submit"
                    else _cmd_review_relay(args, services, eff_synthetic))
        elif args.cmd == "query":
            data = _cmd_query(args, services, eff_synthetic)
        else:  # pragma: no cover - argparse 已保证
            raise ValueError("未知子命令: %r" % (args.cmd,))
        return _ok(cmd, data)
    except ContractError as exc:
        details = dict(exc.details or {})
        lifecycle = getattr(exc, "lifecycle_summary", None)
        if lifecycle is not None:
            # R2-2 / R2-3：失败也保留分段诊断（submission/execution/collection/
            # teardown），供独立复核追溯；同时退出码非 0，不报"整链成功"。
            details = {**details, "lifecycle": lifecycle}
        return _err(cmd, exc.code, str(exc), details)
    except DocumentContractError as exc:
        return _err(cmd, exc.code, str(exc), exc.details)
    except ProjectIntakeContractError as exc:
        return _err(cmd, exc.code, str(exc), exc.details)
    except ValueError as exc:
        # CLI 参数/JSON 结构错误（ContractError 是 ValueError 子类，已在上面接住）
        return _err(cmd, "usage_error", str(exc))
    except sqlite3.DatabaseError as exc:
        return _err(cmd, "usage_error", "数据库读取/写入失败: %s" % exc)
    except FileNotFoundError as exc:
        return _err(cmd, "usage_error", "输入文件不存在: %s" % (exc,))
    except (OSError, json.JSONDecodeError) as exc:
        return _err(cmd, "usage_error", "%s: %s" % (type(exc).__name__, exc))
    finally:
        if store is not None:
            store.close()


def main(argv=None):
    code, result = run(sys.argv[1:] if argv is None else argv)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
