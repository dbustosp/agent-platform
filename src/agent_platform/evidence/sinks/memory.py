"""In-memory evidence sink.

Not a production sink, and deliberately so: P3's test is "if the observability
vendor were removed tomorrow, would we still have the audit trail?" — a list in
a process that has exited answers no.

It exists because the evidence path is the one thing Section 6 calls
non-negotiable, and a non-negotiable path has to be cheap to assert on. Gateway
and harness tests wire this in, act, and read `.records`; nothing touches a
filesystem, so there is no excuse for a test that skips the evidence check.
"""

from __future__ import annotations

import threading

from agent_platform.evidence.record import EvidenceRecord
from agent_platform.evidence.sink import EvidenceSinkError


class InMemorySink:
    """Collect records in a list. Thread-safe, append-only, closeable.

    `records` hands back a tuple rather than the live list: append-only is a
    property we enforce, not a convention we document, and a caller holding the
    internal list could rewrite history that a real sink would not let them
    touch.
    """

    def __init__(self) -> None:
        self._records: list[EvidenceRecord] = []
        self._lock = threading.Lock()
        self._closed = False

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        """Snapshot of everything emitted so far, in emission order."""
        with self._lock:
            return tuple(self._records)

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    def emit(self, record: EvidenceRecord) -> None:
        with self._lock:
            if self._closed:
                msg = "InMemorySink is closed; refusing to drop an evidence record"
                raise EvidenceSinkError(msg)
            self._records.append(record)

    def close(self) -> None:
        with self._lock:
            self._closed = True
