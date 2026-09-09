"""Short-lived SQLite transactions: commit or roll back, then always close the handle."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager


@contextmanager
def database(path: str, initialize: Callable[[sqlite3.Connection], None]) -> Iterator[sqlite3.Connection]:
    """Own the connection even when schema initialization or the transaction fails."""
    with closing(sqlite3.connect(path, timeout=10)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        with connection:
            initialize(connection)
            yield connection
