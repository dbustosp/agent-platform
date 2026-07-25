"""Canonical hashing for evidence payloads.

Section 6: "Hash rather than store payloads in Phase 1 to avoid a
data-classification conversation before there is anything to govern."

A hash is only useful if it is stable. Two structurally identical tool calls
must produce the same digest across processes, Python versions, and dict
insertion orders — otherwise `args_hash` cannot answer "did we do this
before?", which is the only question it exists to answer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

ALGORITHM = "sha256"
#: Digest of the absent value. Distinct from the digest of `None`, which is a
#: value the caller actually passed.
EMPTY = f"{ALGORITHM}:__empty__"


def _canonicalise(value: Any) -> Any:
    """Reduce a value to something `json.dumps` can order deterministically.

    Unserialisable objects degrade to a type-tagged repr rather than raising:
    an imperfect digest of a tool result is worth more than a crashed agent
    and a missing evidence row.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        # Keys may be non-string (e.g. int); coerce so sorting is total.
        items = sorted(value.items(), key=lambda kv: str(kv[0]))
        return {str(k): _canonicalise(v) for k, v in items}
    if isinstance(value, (list, tuple)):
        return [_canonicalise(v) for v in value]
    if isinstance(value, (set, frozenset)):
        # Sets have no order; sort their canonical forms for stability.
        canonical = (_canonicalise(v) for v in value)
        return sorted(canonical, key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, bytes):
        return {"__bytes__": hashlib.sha256(value).hexdigest()}
    for attr in ("model_dump", "to_dict", "_asdict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                return _canonicalise(method())
            except Exception:  # noqa: BLE001 - fall through to repr
                break
    return {"__repr__": f"{type(value).__name__}:{value!r}"}


def hash_payload(value: Any) -> str:
    """Return a stable `sha256:<hex>` digest for an arbitrary payload."""
    canonical = json.dumps(
        _canonicalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{ALGORITHM}:{digest}"


def hash_args(args: Any) -> str:
    """Digest of tool-call arguments. Missing/empty args get a stable sentinel."""
    if args is None or args == {}:
        return EMPTY
    return hash_payload(args)


def hash_result(result: Any) -> str:
    """Digest of a tool result."""
    return hash_payload(result)
