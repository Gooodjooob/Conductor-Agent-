"""Poll known, host-confirmed D4 attempts for worker-authored file receipts."""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapters.workbuddy import WORKBUDDY_SOURCE_ID  # noqa: E402
from contracts.schemas import ContractError  # noqa: E402
from service.context import source_context  # noqa: E402
from service.events import EventService  # noqa: E402
from service.file_receipts import import_receipt, receipt_directory  # noqa: E402
from service.tasks import now_iso  # noqa: E402
from storage.sqlite_store import SqliteStore  # noqa: E402

# D4-DSH-02-SDK-001 §5.3：dsh-sdk 为受控来源（只增不删）。
# 采集器对 dsh-sdk 来源同样执行 host-confirmation + 非 synthetic 双重校验；
# 合成的 SDK 测试证据不能经采集器冒充真实链路（卡片 §5.4）。
from adapters.dsh_sdk import DSH_SDK_SOURCE_ID  # noqa: E402

SOURCES = ("qclaw-gateway", WORKBUDDY_SOURCE_ID, DSH_SDK_SOURCE_ID)


def collect_once(db_path):
    store = SqliteStore(db_path)
    results = []
    try:
        events = EventService(store)
        for task in store.list_tasks():
            for attempt in store.list_attempts(task["task_id"]):
                result = {"at": now_iso(), "taskId": task["task_id"],
                          "attemptId": attempt["attempt_id"]}
                if task["source_id"] not in SOURCES or not store.get_source(task["source_id"]):
                    result.update(status="skipped", reason="unknown_source")
                elif not attempt["submitted_at"] or not attempt["accepted_by_host_at"]:
                    result.update(status="skipped", reason="host_unconfirmed")
                else:
                    activity = store.list_activity(task["task_id"])
                    submitted = [row for row in activity if row["attempt_id"] == attempt["attempt_id"]
                                 and row["kind"] == "attempt.submitted"]
                    if not submitted or any(json.loads(row["detail_json"] or "{}").get("synthetic")
                                            for row in submitted):
                        result.update(status="skipped", reason="submission_not_real")
                    else:
                        path = (Path(task["workspace"]) /
                                receipt_directory(task["task_id"], attempt["attempt_id"]) /
                                "events.json")
                        if not path.exists() and not path.is_symlink():
                            result.update(status="waiting", reason="receipt_missing")
                        else:
                            try:
                                imported = import_receipt(
                                    store, events, task["task_id"], attempt["attempt_id"], str(path),
                                    source_context(task["source_id"], entry="d4_collect"))
                                result.update(status="unchanged" if imported.get("replayed") else "imported",
                                              receiptSha256=imported["receiptSha256"],
                                              eventCount=len(imported["events"]))
                            except (ContractError, OSError, ValueError) as exc:
                                result.update(status="error", reason=getattr(exc, "code", type(exc).__name__),
                                              message=str(exc))
                results.append(result)
    finally:
        store.close()
    return results


def append_audit(path, records):
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_mode & 0o077:
            raise ValueError("audit 文件必须是仅当前用户可读写的普通文件")
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            fd = -1
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    finally:
        if fd >= 0:
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description="采集已确认轮次的 D4 文件回执")
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path, help="私有 JSONL 审计文件")
    parser.add_argument("--interval", type=float, default=0, help="轮询秒数；0 表示只运行一次")
    args = parser.parse_args(argv)
    if not args.db.is_file() or args.interval < 0:
        parser.error("数据库必须存在，轮询间隔不能为负数")
    append_audit(args.audit, [])
    while True:
        records = collect_once(args.db)
        append_audit(args.audit, records)
        print(json.dumps({"at": now_iso(), "results": records}, ensure_ascii=False), flush=True)
        if args.interval == 0:
            return 1 if any(row["status"] == "error" for row in records) else 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
