"""Evidence sink protocol and durability wrapper.

P3 — evidence is emitted at the point of action, not reconstructed from traces.
*Test: if the observability vendor were removed tomorrow, would we still have
the audit trail?* Every sink in this package answers yes; a tracing exporter
never satisfies the protocol on its own.

The risk register rates "evidence gap discovered late" as Severe. `SpillingSink`
is the mitigation: a primary sink that fails does not lose the row, and does not
take the agent down with it.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from agent_platform.evidence.record import EvidenceRecord

logger = logging.getLogger(__name__)


@runtime_checkable
class EvidenceSink(Protocol):
    """Append-only destination for evidence records.

    Implementations must be safe to call from multiple threads and must never
    mutate or delete a previously written record.
    """

    def emit(self, record: EvidenceRecord) -> None:
        """Durably append one record."""
        ...

    def close(self) -> None:
        """Flush and release resources. Must be idempotent."""
        ...


class EvidenceSinkError(RuntimeError):
    """Raised when a sink cannot durably persist a record."""


class SpillingSink:
    """Wrap a primary sink so a transient outage cannot create an evidence gap.

    On primary failure the record is appended to a local spill file and the
    error is logged. The agent keeps running; the row survives; a later job can
    replay the spill into the primary store.

    Set `fail_closed=True` to instead propagate the error and halt the agent.
    That is the correct posture once the evidence log is control evidence
    rather than good practice (watch item W5) — but it is not the Phase 1
    default, because a pilot that dies on a BigQuery hiccup produces no
    evidence at all.
    """

    def __init__(
        self,
        primary: EvidenceSink,
        spill_path: str | Path,
        *,
        fail_closed: bool = False,
    ) -> None:
        self._primary = primary
        self._spill_path = Path(spill_path)
        self._fail_closed = fail_closed
        self._lock = threading.Lock()
        self._spilled = 0
        self._closed = False

    @property
    def spilled_count(self) -> int:
        """Number of records that failed the primary and landed in the spill."""
        return self._spilled

    def emit(self, record: EvidenceRecord) -> None:
        try:
            self._primary.emit(record)
        except Exception as exc:  # noqa: BLE001 - the whole point is to not propagate
            self._spill(_unwritten(exc, record), exc)
            if self._fail_closed:
                msg = f"evidence sink failed and fail_closed is set: {exc}"
                raise EvidenceSinkError(msg) from exc

    def _spill(self, records: tuple[EvidenceRecord, ...], exc: Exception) -> None:
        if not records:
            return
        with self._lock:
            try:
                self._spill_path.parent.mkdir(parents=True, exist_ok=True)
                with self._spill_path.open("a", encoding="utf-8") as fh:
                    for record in records:
                        fh.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                self._spilled += len(records)
                logger.error(
                    "evidence primary sink failed; %d record(s) spilled to %s (%s)",
                    len(records),
                    self._spill_path,
                    exc,
                )
            except Exception:
                # Last resort: the records must at least reach the log stream.
                logger.critical(
                    "EVIDENCE LOSS: primary sink and spill both failed for %r",
                    [r.to_dict() for r in records],
                    exc_info=True,
                )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._primary.close()
        except Exception as exc:  # noqa: BLE001
            # A batching sink flushes on close. If that flush fails, the buffered
            # rows are gone unless we catch them here — the one moment where
            # "evidence gap discovered late" is created in a single call.
            self._spill(_unwritten(exc, None), exc)
            if self._fail_closed:
                msg = f"evidence sink failed to flush on close and fail_closed is set: {exc}"
                raise EvidenceSinkError(msg) from exc


def _unwritten(exc: Exception, fallback: EvidenceRecord | None) -> tuple[EvidenceRecord, ...]:
    """The records a failed sink reports as *not* durably written.

    A batching sink (BigQuery) fails a whole batch at once, so the record handed
    to `emit` is only the last of several that just died. Sinks signal this by
    attaching the full batch to the exception as `.records`; without honouring
    it, `batch_size - 1` rows are lost silently on every failed flush — an
    evidence gap of exactly the kind Section 6 says cannot be backfilled.
    """
    batch = getattr(exc, "records", None)
    if batch:
        return tuple(r for r in batch if isinstance(r, EvidenceRecord))
    return (fallback,) if fallback is not None else ()
