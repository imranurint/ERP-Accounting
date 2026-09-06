"""Idempotency support for accounting commands.

The database is the source of truth: `journal_entries.idempotency_key` carries
a partial unique index scoped to the company. Redis may cache a resolved key
to short-circuit repeat traffic, but it is never authoritative — a cache miss
simply falls through to the database.

Canonicalisation must be *stable*: the same logical command has to hash to the
same digest regardless of key ordering, whitespace, or how a client spelled a
decimal ("100", "100.0", "100.00" are the same amount).
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any
from uuid import UUID


def _canonical(value: Any) -> Any:
    """Normalise a value into a stable, comparable representation."""
    if isinstance(value, Decimal):
        # normalize() collapses trailing zeros so "100.00" == "100".
        normalized = value.normalize()
        if normalized == normalized.to_integral_value():
            normalized = normalized.quantize(Decimal(1))
        return f"{normalized:f}"
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if value is None or isinstance(value, int | str):
        return value
    return str(value)


def canonical_payload(payload: dict[str, Any]) -> str:
    """Deterministic JSON representation of a command payload."""
    return json.dumps(
        _canonical(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of the canonical payload, hex encoded."""
    return hashlib.sha256(canonical_payload(payload).encode("utf-8")).hexdigest()
