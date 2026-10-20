"""Dev/QA 任务级工具授权；不宣称 OS sandbox，测试程序副作用需另行审核。"""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from contracts.paths import resolve_scopes, resolve_workspace

MEMBERS = {
    "dev-wkb1": ("workbuddy-codebuddy", "skill-worker", 60),
    "qa-wkb1": ("workbuddy-codebuddy", "qa-reporter", 50),
    "dev-ckb1": ("codebuddy-cli", "skill-worker", 60),
    "qa-ckb1": ("codebuddy-cli", "qa-reporter", 50),
}


def within(path, root):
    return path == root or root in path.parents


def literal_path(entry):
    if not isinstance(entry, str) or not entry or any(c in entry for c in '\n\r\t*?()[]{}\\'):
        raise ValueError("TASK_PERMISSION_PATH_INVALID")
    return entry


def validate_command(command):
    # 仅测试入口；不接受 shell 组合、内联代码、环境前缀、安装/执行任意包。
    if not isinstance(command, str) or not command or any(c in command for c in '\n\r;|&<>`$\\*?()'):
        raise ValueError("TASK_BASH_COMMAND_INVALID")
    args = shlex.split(command)
    if not args:
        raise ValueError("TASK_BASH_COMMAND_INVALID")
    executable = Path(args[0]).name
    valid = ((executable in ("python", "python3") and len(args) > 2 and args[1:3] == ["-m", "unittest"])
             or (executable == "node" and len(args) > 1 and args[1] == "--test")
             or (executable == "pytest" and len(args) > 1)
             or (executable == "npm" and args[1:] == ["test"]))
    if not valid or any(a in ("-c", "-e", "--eval", "--require", "--import") for a in args):
        raise ValueError("TASK_BASH_TEST_ENTRY_REQUIRED")
    return command


def build_policy(agent_id, workspace, permissions, *, allowed_roots=None, task_id=None, attempt_id=None):
    if agent_id not in MEMBERS:
        raise ValueError("TASK_MEMBER_NOT_BOUND")
    if not isinstance(permissions, dict) or any(permissions.get(k) is not False for k in
            ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish")):
        raise ValueError("TASK_HIGH_RISK_PERMISSION_FORBIDDEN")
    workspace = Path(workspace).resolve(strict=True)
    scope = permissions.get("writeScope")
    if not isinstance(scope, list) or not scope:
        raise ValueError("TASK_WRITE_SCOPE_REQUIRED")
    for entry in scope:
        literal_path(entry)
    writes = resolve_scopes(str(workspace), scope)
    # QA workspace 必须是独立输出根；不可把源码本身作为输出根或授权整个 workspace。
    if MEMBERS[agent_id][1] == "qa-reporter" and str(workspace) in writes:
        raise ValueError("QA_DEDICATED_OUTPUT_SCOPE_REQUIRED")
    reads = permissions.get("readScope", [])
    commands = permissions.get("bashCommands", [])
    if not isinstance(reads, list) or not isinstance(commands, list):
        raise ValueError("TASK_PERMISSION_LIST_REQUIRED")
    normalized_reads = [str(workspace)]
    for entry in reads:
        literal_path(entry)
        if not isinstance(entry, str) or not Path(entry).is_absolute() or Path(entry).resolve() != Path(entry):
            raise ValueError("TASK_READ_SCOPE_INVALID")
        if allowed_roots is not None:
            resolve_workspace(entry, allowed_roots)
        normalized_reads.append(entry)
    if agent_id.startswith("qa-"):
        for entry in reads:
            if any(within(Path(entry), Path(w)) or within(Path(w), Path(entry)) for w in writes):
                raise ValueError("QA_INPUT_OUTPUT_SCOPE_OVERLAP")
    tools = ["Read", "Write", "Glob", "Grep"] + (["Bash"] if commands else []) + \
            ([] if agent_id.startswith("qa-") else ["Edit"])
    policy = {"agentId": agent_id, "workspace": str(workspace),
            "readScope": sorted(set(normalized_reads)), "writeScope": writes,
            "bashCommands": [validate_command(c) for c in commands],
            "tools": tools}
    if task_id is not None or attempt_id is not None:
        for value in (task_id, attempt_id):
            if not isinstance(value, str) or not value or "/" in value or value in (".", ".."):
                raise ValueError("TASK_RECEIPT_ID_INVALID")
        policy["receiptRoot"] = str(workspace / "receipts")
        policy["currentReceiptRoot"] = str(workspace / "receipts" / task_id / attempt_id)
    return policy


def check_tool(policy, event):
    if not isinstance(event, dict) or not isinstance(event.get("tool_input", {}), dict):
        return "TASK_TOOL_EVENT_INVALID"
    tool = event.get("tool_name")
    data = event.get("tool_input") or {}
    if tool not in policy["tools"]:
        return "TASK_TOOL_NOT_ALLOWED"
    if tool == "Bash":
        if event.get("cwd") and Path(event["cwd"]).resolve() != Path(policy["workspace"]):
            return "TASK_BASH_CWD_MISMATCH"
        return None if data.get("command") in policy["bashCommands"] else "TASK_BASH_NOT_ALLOWLISTED"
    entry = data.get("file_path") if tool in ("Read", "Write", "Edit") else data.get("path", policy["workspace"])
    if not isinstance(entry, str):
        return "TASK_TOOL_PATH_INVALID"
    path = Path(entry)
    path = (path if path.is_absolute() else Path(policy["workspace"]) / path).resolve()
    if tool in ("Write", "Edit") and policy.get("receiptRoot"):
        if within(path, Path(policy["receiptRoot"])) and not within(path, Path(policy["currentReceiptRoot"])):
            return "TASK_OLD_RECEIPT_WRITE_DENIED"
    scopes = policy["writeScope"] if tool in ("Write", "Edit") else policy["readScope"]
    return None if any(within(path, Path(s)) for s in scopes) else "TASK_PATH_NOT_ALLOWLISTED"


def cli_arguments(policy, policy_path):
    for scope in policy["writeScope"]:
        if any(within(p.resolve(), Path(scope)) for p in (policy_path, Path(__file__))):
            raise ValueError("TASK_PERMISSION_GUARD_WRITABLE")
    policy_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with policy_path.open("x", encoding="utf-8") as handle:
        json.dump(policy, handle, ensure_ascii=False)
    policy_path.chmod(0o600)
    allow = []
    for tool in ("Read", "Glob", "Grep"):
        for scope in policy["readScope"]:
            allow.extend([f"{tool}({scope})", f"{tool}({scope}/**)"])
    for tool in ("Write", "Edit"):
        if tool in policy["tools"]:
            for scope in policy["writeScope"]:
                allow.extend([f"{tool}({scope})", f"{tool}({scope}/**)"])
    allow.extend(f"Bash({c})" for c in policy["bashCommands"])
    hook = shlex.join([sys.executable, str(Path(__file__).resolve()), str(policy_path)])
    settings = {"permissions": {"allow": allow}, "hooks": {"PreToolUse": [
        {"matcher": ".*", "hooks": [{"type": "command", "command": hook}]}]}}
    return ["--tools", ",".join(policy["tools"]), "--allowedTools", *allow,
            "--settings", json.dumps(settings, ensure_ascii=False),
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']


if __name__ == "__main__":
    try:
        policy = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        reason = check_tool(policy, json.load(sys.stdin))
        if reason:
            print(reason, file=sys.stderr)
            raise SystemExit(2)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as exc:
        print("TASK_PERMISSION_HOOK_INVALID:" + str(exc), file=sys.stderr)
        raise SystemExit(2)
