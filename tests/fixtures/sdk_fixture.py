"""Synthetic protocol peer; never calls a model or reads real credentials."""
import json
import os
import subprocess
import sys
import time

mode = sys.argv[1] if len(sys.argv) > 1 else "completed"


def emit(value):
    print(json.dumps(dict(jsonrpc="2.0", **value)), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    if req["method"] == "initialize":
        emit({"id": req["id"], "result": {"serverInfo": {
            "name": "deepseek-harness-sdk-runtime", "version": "synthetic"}}})
    elif req["method"] == "shutdown":
        emit({"id": req["id"], "result": {}})
        break
    elif req["method"] == "session/prompt":
        if os.environ.get("FIXTURE_COUNT"):
            with open(os.environ["FIXTURE_COUNT"], "a") as out:
                out.write("accepted\n")
        sid = req["params"]["sessionId"]
        if mode == "death":
            sys.exit(3)
        if mode == "timeout":
            time.sleep(1)
            continue
        if mode == "wrong_response":
            emit({"id": 9000, "result": {"messageId": "m1"}})
            continue
        if mode == "malformed":
            print("not-json", flush=True)
            continue
        if mode == "cross_session":
            sid = "other-session"
        def event(seq, kind, data):
            emit({"method": "session.event", "params": {"sessionId": sid,
                 "event": {"seq": seq, "type": kind, "data": data}}})
        event(4, "turn/start", {"turn": 1})
        emit({"id": req["id"], "result": {"messageId": "m1"}})
        if mode == "duplicate_response":
            emit({"id": req["id"], "result": {"messageId": "m1"}})
        if mode == "descendant":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open("child-pid", "w") as out:
                out.write(str(child.pid))
        if mode == "running":
            continue
        reason = {"kind": "error", "error": {"code": "MISSING_CREDENTIAL"}} if mode == "error" else {"kind": mode if mode in {"interrupted", "blocked", "max-tokens", "novel"} else "completed"}
        event(3 if mode == "out_of_order" else 16, "turn/end", {"turn": 2 if mode == "wrong_turn" else 1, "reason": reason})
        if mode == "duplicate":
            event(16, "turn/end", {"turn": 1, "reason": reason})
        if mode == "conflict":
            event(16, "turn/end", {"turn": 1, "reason": {"kind": "error"}})
        emit({"method": "session.status", "params": {"sessionId": sid, "status": "idle"}})
