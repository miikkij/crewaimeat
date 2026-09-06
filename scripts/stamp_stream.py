"""Run a command and put a date + time on every line it prints.

The shared serve daemon writes to its own console window, and the connector prints ONE timestamp at
startup and none after it. So the window that shows the tunnel reconnecting, the token refreshing
and every agent attaching gave no way to tell whether a line was from five seconds ago or five
hours ago — which is the first thing anyone needs when they open that window to find out what just
happened.

Transparent otherwise: same argv, same exit code, stdout and stderr each stamped and each kept on
its own stream.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import datetime


def _pump(src, dst) -> None:
    for raw in iter(src.readline, ""):
        line = raw.rstrip("\r\n")
        dst.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n" if line else "\n")
        dst.flush()


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: stamp_stream.py <command> [args...]", file=sys.stderr)
        return 2
    proc = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    pumps = [
        threading.Thread(target=_pump, args=(proc.stdout, sys.stdout), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, sys.stderr), daemon=True),
    ]
    for t in pumps:
        t.start()
    code = proc.wait()
    for t in pumps:
        t.join(timeout=2)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
