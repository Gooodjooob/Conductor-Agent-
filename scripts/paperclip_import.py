#!/usr/bin/env python3
"""从 Paperclip 只读导入一个 issue，并冻结为本地 Scheduler plan。"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.paperclip import Paperclip
from orchestrator.paperclip_import import PaperclipIssueImporter, build_plan_document, private_json, write_private_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paperclip-url", required=True, help="Paperclip URL；远端必须 HTTPS，本机可用 loopback HTTP")
    parser.add_argument("--issue-id", required=True)
    parser.add_argument("--plan-input", required=True, help="中枢生成的拆分计划 JSON，必须是私有文件")
    parser.add_argument("--output", required=True, help="新的私有冻结 plan JSON 绝对路径")
    parser.add_argument("--token-env", default="PAPERCLIP_TOKEN", help="读取 Paperclip token 的环境变量名")
    args = parser.parse_args()
    token = os.environ.get(args.token_env)
    if not token:
        print(json.dumps({"status": "blocked", "error": "PAPERCLIP_TOKEN_ENV_MISSING"}, ensure_ascii=False))
        return 2
    try:
        client = Paperclip(args.paperclip_url, token)
        client.assert_boundary()
        issue = PaperclipIssueImporter(client).read_issue(args.issue_id)
        source = private_json(args.plan_input)
        document = build_plan_document(issue, source)
        write_private_json(args.output, document)
        print(json.dumps({
            "status": "ready",
            "issueId": issue["issueId"],
            "planId": document["plan"]["planId"],
            "output": str(Path(args.output).resolve()),
            "writeBack": False,
        }, ensure_ascii=False))
        return 0
    except Exception as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(json.dumps({"status": "blocked", "error": code}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
