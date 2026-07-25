"""SQLite evidence sink — locally queryable evidence with no service to run.

Section 5 defers the dashboard ("a dashboard before there is anything worth
displaying") but somebody still has to answer "what has this thing been doing?"
in week two. That question is a `SELECT`, and this sink is the smallest thing
that makes it one. It stays inside the runtime boundary and in the local
filesystem, so it does not violate the "no hosted service in the runtime
critical path" ban the way a shared analytics database would.

Columns are `FIELD_ORDER`, in `FIELD_ORDER`, so a human running `.schema` sees
Section 6 as written in the plan.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

from agent_platform.evidence.record import FIELD_ORDER, EvidenceRecord
from agent_platform.evidence.sink import EvidenceSinkError

DEFAULT_TABLE = "evidence"

#: Columns that the record type guarantees are never `None`. Kept in step with
#: `EvidenceRecord.__post_init__` and the BigQuery schema so the same row is
#: rejected by the same rules in every store.
NOT_NULL_COLUMNS: frozenset[str] = frozenset(
    {
        "run_id",
        "agent_principal",
        "delegated_by",
        "intent",
        "tool",
        "args_hash",
        "policy_decision",
        "timestamp",
        "schema_version",
    }
)

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check_identifier(name: str) -> str:
    """Reject anything that cannot be a bare SQL identifier.

    Table names are interpolated into DDL because SQLite cannot parameterise
    them. This is the check that keeps that from being an injection point.
    """
    if not _IDENTIFIER.match(name):
        msg = f"{name!r} is not a valid SQL identifier"
        raise ValueError(msg)
    return name


class SqliteSink:
    """Append-only evidence table in a local SQLite file.

    Append-only is enforced by the database, not by the absence of an `update()`
    method on this class: `BEFORE UPDATE` and `BEFORE DELETE` triggers abort any
    mutation, including one issued from the `sqlite3` shell by a person who
    never imported this module. That is the difference between a property and a
    convention, and evidence has to be the former.

    One connection is shared across threads (`check_same_thread=False`) behind a
    lock. Each `emit` commits, with `synchronous=FULL`, so the row is durable at
    the point of action (P3) rather than at the end of the run.
    """

    def __init__(self, path: str | Path, *, table: str = DEFAULT_TABLE) -> None:
        self.path = Path(path)
        self.table = _check_identifier(table)
        self._lock = threading.Lock()
        self._closed = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._create_schema()

    def _create_schema(self) -> None:
        columns = ",\n  ".join(
            f"{name} TEXT{' NOT NULL' if name in NOT_NULL_COLUMNS else ''}" for name in FIELD_ORDER
        )
        with self._conn:
            self._conn.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (\n  {columns}\n)")
            # No surrogate key column: the implicit rowid preserves insertion
            # order without adding a column that is not in Section 6, and no
            # uniqueness constraint can reject a genuine duplicate action.
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.table}_run_id ON {self.table}(run_id)"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self.table}_timestamp ON {self.table}(timestamp)"
            )
            self._conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {self.table}_no_update "
                f"BEFORE UPDATE ON {self.table} BEGIN "
                f"SELECT RAISE(ABORT, 'evidence is append-only: UPDATE is not permitted'); END"
            )
            self._conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS {self.table}_no_delete "
                f"BEFORE DELETE ON {self.table} BEGIN "
                f"SELECT RAISE(ABORT, 'evidence is append-only: DELETE is not permitted'); END"
            )

    def emit(self, record: EvidenceRecord) -> None:
        payload = record.to_dict()
        values = [payload.get(name) for name in FIELD_ORDER]
        placeholders = ", ".join("?" for _ in FIELD_ORDER)
        columns = ", ".join(FIELD_ORDER)
        with self._lock:
            if self._closed:
                msg = f"SqliteSink({self.path}) is closed; refusing to drop an evidence record"
                raise EvidenceSinkError(msg)
            try:
                with self._conn:
                    self._conn.execute(
                        f"INSERT INTO {self.table} ({columns}) VALUES ({placeholders})",
                        values,
                    )
            except sqlite3.Error as exc:
                msg = f"sqlite evidence insert failed: {exc}"
                raise EvidenceSinkError(msg) from exc

    def read(self, *, limit: int | None = None, run_id: str | None = None) -> list[EvidenceRecord]:
        """Rows back as records, oldest first — insertion order via rowid.

        `limit` returns the *most recent* N, still oldest-first, because that is
        what a tail wants.
        """
        columns = ", ".join(FIELD_ORDER)
        params: list[Any] = []
        where = ""
        if run_id is not None:
            where = " WHERE run_id = ?"
            params.append(run_id)
        if limit is None:
            sql = f"SELECT {columns} FROM {self.table}{where} ORDER BY rowid"
        else:
            sql = (
                f"SELECT {columns} FROM ("
                f"SELECT {columns}, rowid AS _rid FROM {self.table}{where} "
                f"ORDER BY rowid DESC LIMIT ?"
                f") ORDER BY _rid"
            )
            params.append(limit)
        with self._lock:
            if self._closed:
                # Without this the sqlite3 driver's own ProgrammingError leaks
                # through the sink abstraction; callers handle EvidenceSinkError.
                msg = f"SqliteSink({self.path}) is closed; reopen it to read"
                raise EvidenceSinkError(msg)
            rows = self._conn.execute(sql, params).fetchall()
        return [EvidenceRecord.from_dict(dict(zip(FIELD_ORDER, row, strict=True))) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._conn.commit()
            self._conn.close()
