"""Inherited guard for explicitly declared Python subprocess tests, loaded by Python's site module."""

import os
import sys


class OfflineNetworkAttempt(BaseException):
    pass


def guard(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto", "subprocess.Popen"}:
        raise OfflineNetworkAttempt(f"Offline child blocked {event}")


if os.environ.get("CREWAIMEAT_OFFLINE_TEST") == "1":
    sys.addaudithook(guard)
