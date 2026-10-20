"""Own the CLI process group and stop it on owner-pipe EOF or deadline."""
import argparse
import os
import select
import signal
import subprocess
import threading
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner-fd", type=int, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.timeout <= 0 or os.getpgrp() != os.getpid():
        parser.error("guard requires a command, a positive timeout and its own process group")
    # Start the deadline/owner watcher before launching, including startup failures.
    done = threading.Event()

    def watch_owner():
        deadline = time.monotonic() + args.timeout
        while not done.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            readable, _, _ = select.select([args.owner_fd], [], [], min(0.2, remaining))
            if readable and not os.read(args.owner_fd, 1):
                os.killpg(os.getpgrp(), signal.SIGKILL)

    watcher = threading.Thread(target=watch_owner, daemon=True)
    watcher.start()
    try:
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL, close_fds=True)
        return child.wait()
    finally:
        done.set()
        # The watcher may still be inside select/read. Join before closing its
        # descriptor or finalizing Python; a daemon traceback during shutdown
        # can itself abort the guard with SIGABRT after a successful child exit.
        watcher.join()
        os.close(args.owner_fd)


if __name__ == "__main__":
    raise SystemExit(main())
