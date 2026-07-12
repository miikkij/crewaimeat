"""Prefix every fleet log line with a timestamp — in ONE place.

The fleet host runs N agents as threads sharing one stdout, and many lines come from the
aimeat-crewai package's daemon (`[daemon:*] …`), not crewfive. Touching hundreds of `print()`
calls (ours + the package's) is a non-starter, so instead we wrap `sys.stdout`/`sys.stderr` once
at fleet-host startup and prepend a timestamp at each line start.

Thread-safe (a lock around each write) and constant-width prefix, so Rich's box output stays
aligned (every line shifts by the same amount). Best-effort: never breaks startup. Opt out with
`AIMEAT_LOG_TIMESTAMPS=0`; change the format with `AIMEAT_LOG_TS_FORMAT` (a strftime string,
default `%H:%M:%S` — set e.g. `%Y-%m-%d %H:%M:%S` to include the date on a fleet that runs for days).
"""

from __future__ import annotations

import os
import sys
import threading
from datetime import datetime


class _TimestampedWriter:
    """A stdout/stderr proxy that prefixes a timestamp at the start of every line. Buffers each
    thread's partial line separately and only emits COMPLETE lines (prefix + line + newline) in one
    locked write — so 40 agent threads printing at once never interleave mid-line. Delegates
    everything else (flush, isatty, encoding, …) to the wrapped stream."""

    def __init__(self, stream, fmt: str) -> None:
        self._stream = stream
        self._fmt = fmt
        self._lock = threading.Lock()
        self._partial: dict[int, str] = {}  # thread id -> buffered text with no trailing newline yet

    def write(self, text: str) -> int:
        if not text:
            return 0
        tid = threading.get_ident()
        with self._lock:
            buf = self._partial.get(tid, "") + text
            lines = buf.split("\n")
            self._partial[tid] = lines.pop()  # last chunk has no newline yet — keep buffering it
            if lines:
                stamp = datetime.now().strftime(self._fmt) + " "
                self._stream.write("".join(f"{stamp}{ln}\n" for ln in lines))
        return len(text)

    def flush(self) -> None:
        self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


_installed = False


def install() -> None:
    """Wrap sys.stdout/sys.stderr so every line is timestamped. Idempotent + best-effort; a no-op
    when AIMEAT_LOG_TIMESTAMPS is falsey. Call once at fleet-host startup, before any agent runs."""
    global _installed
    if _installed:
        return
    if (os.getenv("AIMEAT_LOG_TIMESTAMPS", "1") or "").strip().lower() in ("0", "false", "no", "off"):
        return
    fmt = os.getenv("AIMEAT_LOG_TS_FORMAT") or "%H:%M:%S"
    try:
        sys.stdout = _TimestampedWriter(sys.stdout, fmt)
        sys.stderr = _TimestampedWriter(sys.stderr, fmt)
        _installed = True
    except Exception:  # noqa: BLE001 — never break startup over a logging cosmetic
        pass
