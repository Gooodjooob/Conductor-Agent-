import tempfile
import unittest
from pathlib import Path

from orchestrator.gateway import Rejected
from orchestrator.local_mvp import LocalMvp
from orchestrator.paperclip_import import PaperclipIssueImporter, build_plan_document, private_json, write_private_json


class FakePaperclip:
    def __init__(self, issue):
        self.issue = issue
        self.calls = []

    def request(self, method, path):
        self.calls.append((method, path))
        return 200, dict(self.issue)


def source_plan(root, issue_id="paperclip-issue"):
    workspace = (Path(root) / "task-workspace").resolve()
    workspace.mkdir(mode=0o700)
    spec = {
        "taskId": "task-paperclip-1",
        "revision": 1,
        "issueId": "paperclip-child-1",
        "workerId": "codex-dev",
        "runnerId": "runner-1",
        "qaId": "qa-1",
        "reviewerId": "reviewer-1",
        "controllerId": "controller-1",
        "workspace": str(workspace),
        "instructionDigest": "1" * 64,
        "promptDigest": "2" * 64,
        "artifactPaths": ["result.txt"],
        "children": [],
        "upstreamPolicy": {"reviewPolicy": "human_only", "executionPolicy": None, "parentId": issue_id},
    }
    return {
        "plan": {
            "planId": "paperclip-plan-1",
            "paperclipIssueId": issue_id,
            "title": "Paperclip 导入演示",
            "revision": 1,
            "maxConcurrency": 1,
            "members": [{"memberId": "codex-dev", "host": "codex", "roles": ["developer"], "maxConcurrency": 1}],
            "nodes": [{"taskId": spec["taskId"], "title": "只读实现", "memberId": "codex-dev", "role": "developer", "dependsOn": []}],
        },
        "tasks": [spec],
    }


class PaperclipImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.issue = {"id": "paperclip-issue", "title": "调度系统开发", "description": "只读导入测试", "status": "in_progress"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_import_reads_one_issue_and_builds_frozen_document(self):
        client = FakePaperclip(self.issue)
        issue = PaperclipIssueImporter(client).read_issue("paperclip-issue")
        document = build_plan_document(issue, source_plan(self.tmp.name))
        self.assertEqual(client.calls, [("GET", "/issues/paperclip-issue")])
        self.assertEqual(document["schema"], "paperclip-orchestrator-plan/v1")
        self.assertEqual(document["paperclip"]["issueId"], "paperclip-issue")
        self.assertEqual(document["writeBack"], "controller-finalize-only")
        self.assertNotIn("token", document)

    def test_completed_issue_and_binding_drift_are_fail_closed(self):
        client = FakePaperclip(dict(self.issue, status="done"))
        with self.assertRaises(Rejected) as error:
            PaperclipIssueImporter(client).read_issue("paperclip-issue")
        self.assertEqual(error.exception.code, "PAPERCLIP_ISSUE_NOT_DISPATCHABLE")
        client = FakePaperclip(self.issue)
        issue = PaperclipIssueImporter(client).read_issue("paperclip-issue")
        bad = source_plan(self.tmp.name, issue_id="other-issue")
        with self.assertRaises(Rejected) as error:
            build_plan_document(issue, bad)
        self.assertEqual(error.exception.code, "PAPERCLIP_ISSUE_BINDING_MISMATCH")

    def test_private_plan_loads_into_scheduler_preview_without_dispatch(self):
        issue = PaperclipIssueImporter(FakePaperclip(self.issue)).read_issue("paperclip-issue")
        document = build_plan_document(issue, source_plan(self.tmp.name))
        output = Path(self.tmp.name) / "plan.json"
        write_private_json(output, document)
        loaded = private_json(output)
        runtime = LocalMvp(Path(self.tmp.name) / "runtime", 0, 0, plan_document=loaded).start()
        try:
            snapshot = runtime.snapshot()
            self.assertEqual(snapshot["evidenceType"], "paperclip/plan-preview")
            self.assertEqual(snapshot["paperclip"]["issueId"], "paperclip-issue")
            self.assertEqual(snapshot["cards"][0]["state"], "queued")
            self.assertFalse(runtime.scheduler.futures)
        finally:
            runtime.close()


if __name__ == "__main__":
    unittest.main()
