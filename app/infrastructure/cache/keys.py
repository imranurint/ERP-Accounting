"""Redis key namespace.

Every key is built here so the namespace stays greppable and a prefix change
is a one-line edit.
"""

from __future__ import annotations

import hashlib
from uuid import UUID

PREFIX = "erpfin"


def _digest(value: str) -> str:
    """Hash free-form input so keys stay bounded and leak nothing."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def rate_limit_key(identity: str, window: int) -> str:
    return f"{PREFIX}:ratelimit:{_digest(identity)}:{window}"


def idempotency_key(company_id: UUID, key: str) -> str:
    """Cache of a resolved idempotency key. Never authoritative — the unique
    index on `journal_entries` is."""
    return f"{PREFIX}:idem:{company_id}:{_digest(key)}"


def user_permissions_key(user_id: UUID) -> str:
    return f"{PREFIX}:perms:{user_id}"


def company_key(company_id: UUID) -> str:
    return f"{PREFIX}:company:{company_id}"


def currency_key(currency_code: str) -> str:
    return f"{PREFIX}:currency:{currency_code.upper()}"


IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
PERMISSIONS_TTL_SECONDS = 300
CONFIG_TTL_SECONDS = 600
