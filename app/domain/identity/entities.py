"""Identity domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.domain.identity.permissions import Permission


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """The caller behind the current request.

    Built once per request from the access token (and, for sensitive routes,
    re-checked against the database), then passed down into application
    services so they can authorise and audit without touching HTTP.
    """

    id: UUID
    email: str
    full_name: str | None
    is_active: bool
    is_superuser: bool
    permissions: frozenset[Permission] = field(default_factory=frozenset)

    def has_permission(self, permission: Permission) -> bool:
        return self.is_superuser or permission in self.permissions

    def has_all(self, *permissions: Permission) -> bool:
        return all(self.has_permission(p) for p in permissions)

    def has_any(self, *permissions: Permission) -> bool:
        return any(self.has_permission(p) for p in permissions)


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    id: UUID
    name: str
    description: str | None
    permissions: frozenset[Permission]
