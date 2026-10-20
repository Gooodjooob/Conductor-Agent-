"""Independent CodeBuddy account binding; shares only the stream protocol with WorkBuddy."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from adapters.base import AdapterCapabilities
from adapters.workbuddy import CodeBuddyTransport, WorkBuddyAdapter
from contracts.schemas import ContractError
from adapters.task_permissions import MEMBERS, build_policy

SOURCE_ID = "codebuddy-cli"
AGENT_ID = "dev-ckb1"
AGENT_IDS = ("dev-ckb1", "qa-ckb1")
ROLE_ROOT = Path(__file__).resolve().parents[3] / "sales-dev/references/host-members"
MODEL_ID = "deepseek-v4.1-flash"
DEPLOYMENT_ROOT = Path(__file__).resolve().parents[3] / ".conductor-local/d4-codebuddy-independent-binding-20261009"
BINARY = DEPLOYMENT_ROOT / "cli-install/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy"
AUTH_HOME = DEPLOYMENT_ROOT / "auth-home"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_binding(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or path.resolve() != path or not path.is_file():
        raise ValueError("CODEBUDDY_BINDING_PATH_INVALID")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CODEBUDDY_BINDING_INVALID")
    schema = value.get("schemaVersion")
    agent_id = value.get("agentId")
    if schema not in ("d4-codebuddy-connector.v1", "d4-codebuddy-connector.v2") or agent_id not in AGENT_IDS:
        raise ValueError("CODEBUDDY_MEMBER_NOT_BOUND")
    if schema.endswith(".v1") and agent_id != AGENT_ID:
        raise ValueError("CODEBUDDY_LEGACY_MEMBER_MISMATCH")
    role_path = DEPLOYMENT_ROOT / "dev-ckb1.md" if schema.endswith(".v1") else ROLE_ROOT / (agent_id + ".md")
    expected = {"schemaVersion": schema, "sourceId": SOURCE_ID,
                "agentId": agent_id, "environmentId": "codebuddy-cli-independent-local",
                "model": MODEL_ID, "transparentFallback": False,
                "binary": str(BINARY), "home": str(AUTH_HOME),
                "configDir": str(AUTH_HOME / ".codebuddy"), "version": "2.163.0",
                "accountEvidence": "USER_CONFIRMED_INDEPENDENT_ACCOUNT",
                "rolePath": str(role_path)}
    if any(value.get(key) != entry for key, entry in expected.items()):
        raise ValueError("CODEBUDDY_BINDING_MISMATCH")
    if value.get("transparentFallback") is not False:
        raise ValueError("CODEBUDDY_FALLBACK_FORBIDDEN")
    for entry in (BINARY, Path(value["rolePath"])):
        if entry.is_symlink() or not entry.is_file() or entry.resolve() != entry:
            raise ValueError("CODEBUDDY_BINDING_FILE_INVALID")
    if not os.access(BINARY, os.X_OK) or not AUTH_HOME.is_dir():
        raise ValueError("CODEBUDDY_INSTALLATION_MISSING")
    config_dir = Path(value["configDir"])
    for entry in (AUTH_HOME, config_dir):
        if entry.is_symlink() or entry.resolve() != entry or entry.stat().st_mode & 0o077:
            raise ValueError("CODEBUDDY_AUTH_HOME_NOT_PRIVATE")
    checks = {"binarySha256": BINARY, "roleSha256": Path(value["rolePath"]),
              "bundleSha256": BINARY.parent.parent / "dist/codebuddy-headless.js"}
    if any(value.get(key) != digest(entry) for key, entry in checks.items()):
        raise ValueError("CODEBUDDY_BINDING_DRIFT")
    if json.loads((BINARY.parent.parent / "package.json").read_text())["version"] != value["version"]:
        raise ValueError("CODEBUDDY_VERSION_DRIFT")
    return value


class ConnectorBinding(dict):
    """运行事实用的 connector 绑定（evidence-only，显式 falsy）。

    ``CodeBuddyTransport`` 的 ``binding`` 语义是 WorkBuddy **专家插件**
    （``hostAgent`` / ``pluginDir`` / 冻结 prompt）：一旦为真值就会改写 argv，
    并接管 agent 校验与 ``configure_task`` 分支。独立 CodeBuddy connector 没有
    这些字段，因此这里显式保持 falsy：它只作为 ``runtime.json`` / 回执的固定
    运行事实（agentId / sourceId / environmentId），不改变任何 argv、校验、
    权限或回退行为。
    """

    __slots__ = ()

    def __bool__(self):
        return False


class IndependentCodeBuddyTransport(CodeBuddyTransport):
    def __init__(self, connector_path, workspace, store_dir, *, timeout=300):
        self.connector_path = str(connector_path)
        self.connector = read_binding(connector_path)
        self.expected_host_agent = self.connector["agentId"]
        self.task_scoped = self.connector["schemaVersion"].endswith(".v2")
        workspace = Path(workspace).resolve(strict=True)
        if AUTH_HOME == workspace or AUTH_HOME in workspace.parents or workspace in AUTH_HOME.parents:
            raise ValueError("CODEBUDDY_AUTH_WORKSPACE_OVERLAP")
        env = {"HOME": str(AUTH_HOME), "PATH": "/Users/demo/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
               "CODEBUDDY_CONFIG_DIR": self.connector["configDir"],
               "WORKBUDDY_CONFIG_DIR": str(AUTH_HOME / ".workbuddy"),
               "DISABLE_AUTOUPDATER": "1", "CODEBUDDY_DISABLE_IDE": "1",
               "CODEBUDDY_DISABLE_AUTO_MEMORY": "1", "CODEBUDDY_MEMORY_ENABLED": "false",
               "CODEBUDDY_TEAM_MEMORY_ENABLED": "false"}
        super().__init__([str(BINARY)], str(workspace), store_dir, model=MODEL_ID,
                         env=env, timeout=timeout, binding=None)
        # 初始化时不得丢 connector binding：把已通过 read_binding 的完整 connector 固定为
        # 运行事实，使 runtime.json、回执与状态读取始终保存 dev-ckb1 / codebuddy-cli /
        # codebuddy-cli-independent-local。run() 仍逐次比对 connector，漂移即 fail closed。
        self.binding = ConnectorBinding(self.connector)

    def argv(self, session_id, prompt):
        role = Path(self.connector["rolePath"]).read_text(encoding="utf-8")
        if self.task_scoped:
            if self.task_policy is None:
                raise ValueError("CODEBUDDY_TASK_PERMISSIONS_REQUIRED")
            agents = {self.expected_host_agent: {"description": "D4 task-scoped dev/QA member",
                      "prompt": role, "tools": self.task_policy["tools"], "model": MODEL_ID}}
            return super().argv(session_id, prompt)[:-2] + [
                "--agents", json.dumps(agents, ensure_ascii=False), "--agent", self.expected_host_agent,
                "--max-turns", str(MEMBERS[self.expected_host_agent][2]), "--", prompt]
        agents = {AGENT_ID: {"description": "D4 controlled independent CodeBuddy worker",
                             "prompt": role, "tools": ["Read", "Write"], "model": MODEL_ID}}
        settings = {"permissions": {"allow": ["Read(%s/**)" % self.workspace,
                    "Write(%s/receipts/**)" % self.workspace]}}
        return super().argv(session_id, prompt)[:-2] + [
            "--agents", json.dumps(agents, ensure_ascii=False), "--agent", AGENT_ID,
            "--tools", "Read,Write", "--allowedTools",
            "Read(%s/**)" % self.workspace, "Write(%s/receipts/**)" % self.workspace,
            "--settings", json.dumps(settings), "--setting-sources", "",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--max-turns", "12", "--", prompt]

    def configure_task(self, task, attempt=None):
        if task["agent_id"] != self.expected_host_agent or Path(task["workspace"]).resolve() != self.workspace:
            raise ValueError("CODEBUDDY_TASK_MEMBER_MISMATCH")
        self.task_policy = build_policy(task["agent_id"], task["workspace"], json.loads(task["permissions_json"]),
            task_id=task["task_id"] if attempt else None, attempt_id=attempt["attempt_id"] if attempt else None)

    def run(self, session_id, prompt):
        if read_binding(self.connector_path) != self.connector:
            raise ValueError("CODEBUDDY_CONNECTOR_DRIFT")
        return super().run(session_id, prompt)


class IndependentCodeBuddyAdapter(WorkBuddyAdapter):
    name = SOURCE_ID
    source_id = SOURCE_ID

    def __init__(self, transport, store_dir):
        super().__init__(transport, store_dir, source_id=SOURCE_ID)

    @classmethod
    def _validate_binding(cls, task, attempt):
        super()._validate_binding(task, attempt)
        if task.get("agent_id") not in AGENT_IDS:
            raise ContractError("permission_denied", message="独立 CodeBuddy 成员不匹配")

    def capabilities(self):
        return AdapterCapabilities(submit=True, host_acceptance=True, stable_ids=True,
            restart_recovery=False, evidence_level="LOCAL",
            notes="Independent CLI/account configuration; account confirmed by user, not provider attestation. "
                  "Task-scoped dev/QA tools; no filesystem sandbox claim; completion requires independent review.")

    def submit(self, task, attempt):
        expected = getattr(self.transport, "expected_host_agent", None)
        if expected is not None and task.get("agent_id") != expected:
            raise ContractError("permission_denied", message="任务与 CodeBuddy connector 成员不匹配")
        return super().submit(task, attempt)

    def _result_from_receipt(self, receipt, replayed):
        result = super()._result_from_receipt(receipt, replayed)
        result.note = result.note.replace("workbuddy hostState=", "codebuddy-cli hostState=")
        return result

    def _judge(self, run, attempt_id, session_id, started_at):
        receipt = super()._judge(run, attempt_id, session_id, started_at)
        initial = next((event for event in run["events"]
                        if event.get("type") == "system" and event.get("subtype") == "init"), {})
        receipt["observedAgent"] = initial.get("agent")
        expected_agent = getattr(self.transport, "expected_host_agent", AGENT_ID)
        receipt["runtimeIdentity"] = "LOCAL_MATCH" if initial.get("agent") == expected_agent else "UNVERIFIED"
        if initial.get("agent") is not None and initial["agent"] != expected_agent:
            receipt.update(hostState="unknown", reason="CODEBUDDY_AGENT_MISMATCH")
        if initial and initial.get("model") != MODEL_ID:
            receipt.update(hostState="unknown", reason="CODEBUDDY_MODEL_MISMATCH")
        return receipt
