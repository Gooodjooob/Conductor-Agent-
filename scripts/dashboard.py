#!/usr/bin/env python3
"""Generate a static card board or serve a read-only live projection.

The provider is intentionally an embedding point: callers must supply a
trusted function that returns Scheduler.snapshot(); this script never reads
Gateway credentials or workspace files.
"""
import argparse
import json
from pathlib import Path
import secrets
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from orchestrator.dashboard import make_dashboard, render_html


def private_json(path):
    p = Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_mode & 0o077:
        raise SystemExit('SNAPSHOT_MUST_BE_PRIVATE')
    return json.loads(p.read_text())


def main():
    ap = argparse.ArgumentParser(description='Agent orchestration read-only card board')
    ap.add_argument('--snapshot', required=True, help='trusted Scheduler.snapshot JSON')
    ap.add_argument('--output', help='write static HTML (must not already exist)')
    ap.add_argument('--serve', action='store_true', help='serve live read-only JSON from the supplied snapshot')
    ap.add_argument('--token', help='read-only token for --serve; omitted prints a generated token')
    ap.add_argument('--port', type=int, default=0)
    args = ap.parse_args()
    snapshot = private_json(args.snapshot)
    if args.output:
        target = Path(args.output)
        if target.exists() or target.is_symlink(): raise SystemExit('DASHBOARD_OUTPUT_EXISTS')
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = __import__('os').open(str(target), __import__('os').O_WRONLY|__import__('os').O_CREAT|__import__('os').O_EXCL, 0o600)
        with __import__('os').fdopen(fd, 'w') as out: out.write(render_html(dict(snapshot, live=False)))
        print(json.dumps({'output':str(target),'cards':len(snapshot.get('cards',[]))}, ensure_ascii=False))
    if args.serve:
        token=args.token or secrets.token_urlsafe(32)
        server=make_dashboard(lambda: snapshot, token, args.port)
        print(json.dumps({'listen':server.server_address,'readOnlyToken':token}, ensure_ascii=False), flush=True)
        try: server.serve_forever()
        except KeyboardInterrupt: pass
        finally: server.shutdown(); server.server_close()
    if not args.output and not args.serve:
        print(json.dumps({'cards':len(snapshot.get('cards',[])),'plans':len(snapshot.get('plans',[]))}, ensure_ascii=False))


if __name__ == '__main__': main()
