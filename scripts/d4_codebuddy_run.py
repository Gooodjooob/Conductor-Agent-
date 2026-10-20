"""Controlled independent CodeBuddy single-task entry; no fallback or resubmission."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.codebuddy_cli import (AGENT_ID, SOURCE_ID, MODEL_ID,
    IndependentCodeBuddyAdapter, IndependentCodeBuddyTransport, read_binding)
from contracts.document_io import (inspect_document, task_card_to_create_payload,
    build_task_card_source_reference)
from service.context import owner_context, worker_context, source_context
from service.tasks import TaskService
from service.events import EventService
from service.file_receipts import import_receipt, receipt_directory
from storage.sqlite_store import SqliteStore
from adapters.task_permissions import MEMBERS, build_policy


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "x", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def preflight(card, connector, allowed_root):
    binding = read_binding(connector)
    agent_id = binding["agentId"]
    document = inspect_document(str(card), kind="task-card",
        cli_agent=agent_id, cli_source=SOURCE_ID, allowed_roots=[str(allowed_root)])
    payload, missing, errors = task_card_to_create_payload(document["canonical"],
        cli_agent=agent_id, cli_source=SOURCE_ID, allowed_roots=[str(allowed_root)])
    if missing or errors:
        raise ValueError("CODEBUDDY_CARD_INVALID:%s" % json.dumps({"missing": missing, "errors": errors}))
    executor = document["canonical"]["executor"]
    if executor.get("role") != MEMBERS[agent_id][1]:
        raise ValueError("CODEBUDDY_CARD_ROLE_MISMATCH")
    if executor["environmentId"] != binding["environmentId"] or executor["host"] != SOURCE_ID:
        raise ValueError("CODEBUDDY_CARD_ENVIRONMENT_MISMATCH")
    permissions = payload["permissions"]
    if any(permissions[key] for key in ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish")):
        raise ValueError("CODEBUDDY_SMOKE_PERMISSION_FORBIDDEN")
    workspace = Path(payload["workspace"]).resolve(strict=True)
    if binding["schemaVersion"].endswith(".v1"):
        if payload["limits"]["maxAttempts"] != 1 or payload["limits"]["timeoutMinutes"] > 5:
            raise ValueError("CODEBUDDY_SMOKE_LIMITS_INVALID")
        if permissions["writeScope"] != [str(workspace / "receipts")]:
            raise ValueError("CODEBUDDY_SMOKE_SCOPE_INVALID")
    else:
        build_policy(agent_id, str(workspace), permissions, allowed_roots=[str(allowed_root)])
    return binding, document, payload


def start(args):
    for path in (args.card, args.connector, args.db, args.runtime_dir, args.allowed_root):
        if not path.is_absolute() or path.resolve() != path or path.is_symlink():
            raise ValueError("CODEBUDDY_PATH_INVALID")
    binding, document, payload = preflight(args.card, args.connector, args.allowed_root)
    agent_id = binding["agentId"]
    if args.db.exists():
        raise ValueError("CODEBUDDY_VALIDATION_DB_ALREADY_EXISTS")
    # Reserve a new runtime before creating any task; never reuse an unknown attempt.
    args.runtime_dir.mkdir(mode=0o700)
    transport = IndependentCodeBuddyTransport(args.connector, payload["workspace"],
        str(args.runtime_dir / "host"), timeout=payload["limits"]["timeoutMinutes"] * 60)
    adapter = IndependentCodeBuddyAdapter(transport, str(args.runtime_dir / "host"))
    store = SqliteStore(args.db)
    state = {"status": "preparing", "sourceId": SOURCE_ID, "agentId": agent_id,
        "environmentId": binding["environmentId"], "requestedModel": MODEL_ID,
        "connectorSha256": hashlib.sha256(args.connector.read_bytes()).hexdigest(),
        "cardSha256": document["sourceFile"]["sha256"],
        "accountEvidence": binding["accountEvidence"], "quotaVerification": "UNVERIFIED"}
    save(args.runtime_dir / "state.json", state)
    try:
        store.register_source(SOURCE_ID, label="Independent CodeBuddy CLI", capability=adapter.capabilities().to_dict())
        store.register_agent(agent_id, label="Independent CodeBuddy " + agent_id)
        tasks = TaskService(store, adapter=adapter, allowed_roots=[str(args.allowed_root)])
        created = tasks.create_task(payload, context=owner_context(entry="d4_codebuddy_run"),
            source_card=build_task_card_source_reference(document, str(args.card)))
        attempt = tasks.create_attempt(created["taskId"], context=worker_context(agent_id))
        state.update(status="submitting", taskId=created["taskId"], attemptId=attempt["attemptId"])
        save(args.runtime_dir / "state.json", state)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        submitted = tasks.submit_attempt(state["taskId"], state["attemptId"], context=worker_context(agent_id))
        save(args.runtime_dir / "submission.json", submitted)
        receipt = Path(payload["workspace"]) / receipt_directory(state["taskId"], state["attemptId"]) / "events.json"
        collection = import_receipt(store, EventService(store), state["taskId"], state["attemptId"],
            str(receipt), source_context(SOURCE_ID, entry="d4_codebuddy_run collect"))
        save(args.runtime_dir / "collection.json", collection)
        state.update(status="awaiting_review", observedModel=adapter.get_status(state["attemptId"])["observedModel"],
            taskStatus=store.get_task(state["taskId"])["status"])
        save(args.runtime_dir / "state.json", state)
        return state
    except Exception as exc:
        state.update(status="attention_required", errorType=type(exc).__name__, error=str(exc),
            retryAllowed=False)
        save(args.runtime_dir / "state.json", state)
        raise
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    launch = sub.add_parser("start")
    for name in ("card", "connector", "db", "runtime-dir", "allowed-root"):
        launch.add_argument("--" + name, type=Path, required=True)
    sub.add_parser("status").add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        data = start(args) if args.command == "start" else json.loads((args.runtime_dir / "state.json").read_text())
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": {"type": type(exc).__name__, "message": str(exc)}}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
