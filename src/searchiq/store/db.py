"""Connection handling for the analytics store.

SQLite is the right engine here: the store is read-mostly, single-node, a few
hundred megabytes at most, and rebuilt wholesale by the ETL. Shipping it as a
file means the dashboard, the CLI, and the tests all open the same artefact with
no server to run.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from searchiq.config import settings

_SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# How long a write waits for another writer before giving up.
_BUSY_TIMEOUT_MS = 5_000


@contextmanager
def connect(
    db_path: Path | str | None = None, *, read_only: bool = False
) -> Iterator[sqlite3.Connection]:
    """Open the analytics store, yielding a connection with row access by name.

    Commits on clean exit and rolls back if the block raises, so a failed ETL
    never leaves the store half-written.
    """
    path = Path(db_path) if db_path is not None else settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)

    if read_only and path.exists():
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        connection = sqlite3.connect(path)

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    # SQLite allows one writer at a time and fails immediately when another
    # holds the lock. Under a web server, two reviewers clicking approve at the
    # same moment would otherwise surface as an error to one of them; waiting a
    # few seconds turns that into an imperceptible pause instead.
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    try:
        yield connection
        if not read_only:
            connection.commit()
    except Exception:
        if not read_only:
            connection.rollback()
        raise
    finally:
        connection.close()


def apply_schema(connection: sqlite3.Connection) -> None:
    """Create every table and index that does not already exist."""
    connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))


def write_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
    """Record a provenance fact (row counts, source path, extract timestamp)."""
    connection.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def read_meta(connection: sqlite3.Connection) -> dict[str, str]:
    """Return every provenance fact recorded by the last ETL run."""
    return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM meta")}


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    """Convert `sqlite3.Row` results into plain dicts for JSON serialisation."""
    return [dict(row) for row in rows]
