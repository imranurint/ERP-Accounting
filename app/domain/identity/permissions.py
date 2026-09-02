"""Permissions and default roles.

Business logic checks *permissions*, never role names. Roles are just bundles
of permissions and are expected to be edited per deployment; the permission
constants below are the stable contract.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    ACCOUNTING_READ = "accounting:read"
    ACCOUNTING_WRITE = "accounting:write"
    ACCOUNTING_POST = "accounting:post"
    ACCOUNTING_REVERSE = "accounting:reverse"
    ACCOUNTING_VOID = "accounting:void"
    REPORTS_READ = "reports:read"
    PERIOD_CLOSE = "period:close"
    PERIOD_REOPEN = "period:reopen"
    CONFIG_MANAGE = "config:manage"
    USERS_MANAGE = "users:manage"


PERMISSION_DESCRIPTIONS: dict[Permission, str] = {
    Permission.ACCOUNTING_READ: "Read journal entries and accounting configuration",
    Permission.ACCOUNTING_WRITE: "Create and edit draft journal entries",
    Permission.ACCOUNTING_POST: "Post journal entries to the general ledger",
    Permission.ACCOUNTING_REVERSE: "Reverse posted journal entries",
    Permission.ACCOUNTING_VOID: "Void draft journal entries",
    Permission.REPORTS_READ: "Run financial reports",
    Permission.PERIOD_CLOSE: "Close and lock fiscal periods",
    Permission.PERIOD_REOPEN: "Reopen closed fiscal periods",
    Permission.CONFIG_MANAGE: "Manage companies, currencies, accounts and tax codes",
    Permission.USERS_MANAGE: "Manage users and role assignments",
}


class Role(StrEnum):
    ADMIN = "Admin"
    ACCOUNTANT = "Accountant"
    AUDITOR = "Auditor"
    VIEWER = "Viewer"


#: Default role → permission mapping installed by `python -m app.scripts.seed`.
DEFAULT_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(Permission),
    Role.ACCOUNTANT: frozenset(
        {
            Permission.ACCOUNTING_READ,
            Permission.ACCOUNTING_WRITE,
            Permission.ACCOUNTING_POST,
            Permission.ACCOUNTING_REVERSE,
            Permission.ACCOUNTING_VOID,
            Permission.REPORTS_READ,
            Permission.PERIOD_CLOSE,
        }
    ),
    # An auditor observes; it must never be able to change the ledger.
    Role.AUDITOR: frozenset({Permission.ACCOUNTING_READ, Permission.REPORTS_READ}),
    Role.VIEWER: frozenset({Permission.ACCOUNTING_READ, Permission.REPORTS_READ}),
}

ROLE_DESCRIPTIONS: dict[Role, str] = {
    Role.ADMIN: "Full administrative control including configuration and users",
    Role.ACCOUNTANT: "Day-to-day accounting: draft, post, reverse and close periods",
    Role.AUDITOR: "Read-only access to the ledger and all reports",
    Role.VIEWER: "Read-only access for general business users",
}
