"""JSONL evidence sink — the local default for Phase 1.

Section 4.4 names BigQuery as the Phase 1 evidence target. This sink is what
runs when BigQuery is not configured, and it is not a lesser option: it needs
no credentials, no network, and no SDK, which means there is no configuration
state in which the agent runs and the evidence does not get written. Given that
Section 6 calls the evidence log the one thing that cannot be backfilled, a
sink that cannot be switched off by a missing environment variable is worth
more than a sink with better query ergonomics.

The on-disk format is byte-identical to what `SpillingSink` writes to its spill
file, so replaying a spill into a JSONL log is a concatenation rather than a
migration.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from agent_platform.evidence.record import EvidenceRecord
from agent_platform.evidence.sink import EvidenceSinkError


class JsonlSink:
    """Append one JSON object per line to a file.

    Append-only is enforced by the file mode, not by discipline: the handle is
    opened `"a"`, so the OS puts every write at end-of-file and there is no code
    path here that seeks, truncates, or rewrites. Reopening the same path
    continues the log rather than starting a new one.

    Every record is flushed out of the Python buffer as it is written, so a
    process that dies without calling `close()` still leaves its last row on
    disk. `fsync=True` additionally forces the OS page cache down to the device,
    which is what survives a machine losing power. It is the default because at
    pilot volume — a handful of tool calls a minute — the cost is unmeasurable
    and the failure it prevents is the one the risk register rates Severe.
    """

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Opened eagerly: a path we cannot write to should fail at wiring time,
        # not on the first tool call of a real run.
        self._fh = self.path.open("a", encoding="utf-8")

    def emit(self, record: EvidenceRecord) -> None:
        line = json.dumps(record.to_dict(), ensure_ascii=False)
        with self._lock:
            if self._closed:
                msg = f"JsonlSink({self.path}) is closed; refusing to drop an evidence record"
                raise EvidenceSinkError(msg)
            self._fh.write(line + "\n")
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._fh.flush()
            if self._fsync:
                os.fsync(self._fh.fileno())
            self._fh.close()

    # -- reading back -------------------------------------------------------
    #
    # Reading is a classmethod on the path rather than an instance method: the
    # CLI's evidence-tail command inspects a log it did not open, and should not
    # have to construct a writer to do it.

    @classmethod
    def read(cls, path: str | Path, *, strict: bool = False) -> list[EvidenceRecord]:
        """Load every record from a JSONL log, oldest first.

        Missing file reads as empty — a run that emitted nothing is a legitimate
        state, not an error. With `strict=False` (the default) an unparseable
        line is skipped rather than fatal, because the plausible cause is a
        torn final write from a crash, and losing one row should not make the
        other ten thousand unreadable. Pass `strict=True` when the caller is an
        audit process that must not silently narrow its own input.
        """
        source = Path(path)
        if not source.exists():
            return []
        records: list[EvidenceRecord] = []
        with source.open("r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    records.append(EvidenceRecord.from_dict(json.loads(text)))
                except Exception as exc:
                    if strict:
                        msg = f"{source}:{lineno} is not a valid evidence record: {exc}"
                        raise EvidenceSinkError(msg) from exc
                    continue
        return records

    @classmethod
    def tail(cls, path: str | Path, limit: int = 20) -> list[EvidenceRecord]:
        """The last `limit` records, oldest first. Backs `evidence tail`."""
        records = cls.read(path)
        if limit <= 0:
            return []
        return records[-limit:]
