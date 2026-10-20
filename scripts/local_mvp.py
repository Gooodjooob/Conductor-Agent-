#!/usr/bin/env python3
"""启动本机 Agent 调度 MVP（loopback、只读看板、可选合成 demo）。"""

import argparse
import json
import os
import signal
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.local_mvp import LocalMvp
from orchestrator.paperclip_import import private_json
from orchestrator.execution import PlanExecution, error_code, validate_execution, verify_upstream
from orchestrator.gateway import require
from orchestrator.paperclip import Paperclip


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="私有状态目录；执行模式必填，恢复时复用原目录")
    parser.add_argument("--gateway-port", type=int, default=3180)
    parser.add_argument("--dashboard-port", type=int, default=3210)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--demo-plan", action="store_true", help="加载合成任务；不调用真实宿主")
    modes.add_argument("--plan", help="加载冻结计划；默认仅预览，加 --execute 才派发")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--execute", action="store_true", help="显式执行计划，调用受信 Runner")
    action.add_argument("--check", action="store_true", help="只检查执行配置及 Paperclip 状态，不调用宿主")
    parser.add_argument("--jobs", help="私有 orchestrator-jobs/v1 配置")
    parser.add_argument("--paperclip-url", help="执行时复核任务的 Paperclip 地址")
    parser.add_argument("--token-env", default="PAPERCLIP_TOKEN", help="读取 Paperclip token 的环境变量名称")
    parser.add_argument("--resume", action="store_true", help="同一执行目录恢复观测及尚未派发任务；不重派已有 attempt")
    args = parser.parse_args()
    executing = args.execute or args.check
    if executing and not all((args.plan, args.jobs, args.state_dir, args.paperclip_url)):
        parser.error('--execute/--check requires --plan, --jobs, --state-dir and --paperclip-url')
    if not executing and (args.jobs or args.paperclip_url or args.resume):
        parser.error('--jobs/--paperclip-url/--resume require --execute or --check')
    done = threading.Event()

    def stop(_signum, _frame):
        done.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    runtime = None
    try:
        state_dir = Path(args.state_dir).expanduser().resolve() if args.state_dir else Path(tempfile.mkdtemp(prefix="agent-orchestrator-mvp-", dir="/tmp")).resolve()
        plan_document = private_json(Path(args.plan).expanduser()) if args.plan else None
        if executing:
            jobs = private_json(Path(args.jobs).expanduser())
            # Do not leave the upstream credential available to child CLIs.
            token = os.environ.pop(args.token_env, None)
            require(token is not None, 'PAPERCLIP_TOKEN_ENV_MISSING', 400)
            client = Paperclip(args.paperclip_url, token)
            validate_execution(plan_document, jobs, state_dir)
            if args.check:
                verify_upstream(client, plan_document)
                print(json.dumps({'status': 'ready', 'mode': 'execution-check', 'dispatched': False,
                                  'tasks': len(jobs['jobs']), 'quota': 'disabled', 'writeBack': False}), flush=True)
                return 0
            runtime = PlanExecution(state_dir, plan_document, jobs, client,
                                    args.gateway_port, args.dashboard_port, resume=args.resume)
        else:
            runtime = LocalMvp(state_dir, args.gateway_port, args.dashboard_port, args.demo_plan, plan_document)
        runtime.start()
        print(json.dumps({
            "stateDir": str(state_dir),
            "gateway": runtime.addresses["gateway"],
            "dashboard": runtime.addresses["dashboard"],
            "dashboardReadToken": runtime.dashboard_token,
            "mode": "plan-execute" if executing else "demo/fixture" if args.demo_plan else "paperclip/plan-preview" if args.plan else "empty",
            "quota": "disabled", "writeBack": False,
            "message": "打开 dashboard 地址后输入只读令牌；Ctrl-C 停止新派发，等待在途任务回执/超时后关闭。",
        }, ensure_ascii=False), flush=True)
        done.wait()
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error': error_code(exc)}, ensure_ascii=False), flush=True)
        return 2
    finally:
        if runtime:
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
