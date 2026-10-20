"""Tencent WorkBuddy/CodeBuddy headless adapter.

The desktop host exposes the bundled ``codebuddy`` CLI. This adapter keeps the
host-specific invocation separate from Codex while sharing the same receipt
contract: a process result is only executable evidence, never QA or done.
"""
from .codex import CodexAdapter


class WorkBuddyAdapter(CodexAdapter):
    """CodeBuddy CLI JSON adapter with a frozen host label."""
    def probe(self):
        result = super().probe()
        result.update(transport="workbuddy-codebuddy-stream-json", host="workbuddy",
                      executableName="codebuddy")
        return result

    def _command_args(self, prompt):
        # WorkBuddy's ``json`` mode emits a pretty-printed JSON array, which
        # is not a line protocol. ``stream-json`` emits one event per line and
        # preserves the terminal ``result/subtype=success`` evidence consumed
        # by the shared Codex JSONL receipt parser.
        args = list(self.command) + ["--print", "--output-format", "stream-json",
                "--permission-mode", "dontAsk", "--tools", "",
                "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--session-id", self._receipt()["sessionId"],
                "--no-session-persistence"]
        if self.model: args += ["--model", self.model]
        args += [prompt]
        return args
