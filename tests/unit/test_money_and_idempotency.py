"""Unit tests for money handling, identifiers and idempotency hashing."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from app.core.idempotency import canonical_payload, payload_hash
from app.core.ids import uuid7, uuid7_timestamp_ms
from app.core.money import (
    MoneyError,
    exceeds_currency_precision,
    format_money,
    quantize_currency,
    quantize_storage,
    to_decimal,
    within_storage_range,
)
from app.domain.identity.entities import AuthenticatedUser
from app.domain.identity.permissions import DEFAULT_ROLE_PERMISSIONS, Permission, Role

pytestmark = pytest.mark.unit


class TestMoneyParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("115.00", Decimal("115.00")),
            ("0", Decimal("0")),
            (" 42.5 ", Decimal("42.5")),
            (100, Decimal("100")),
            (Decimal("1.2345"), Decimal("1.2345")),
        ],
    )
    def test_accepted_inputs(self, raw: object, expected: Decimal) -> None:
        assert to_decimal(raw) == expected  # type: ignore[arg-type]

    @pytest.mark.parametrize("raw", ["", "abc", "1.2.3", "NaN", "Infinity"])
    def test_rejected_inputs(self, raw: str) -> None:
        with pytest.raises(MoneyError):
            to_decimal(raw)

    def test_float_is_refused_outright(self) -> None:
        with pytest.raises(MoneyError):
            to_decimal(115.00)  # type: ignore[arg-type]


class TestRounding:
    def test_storage_scale_is_four_decimals(self) -> None:
        assert quantize_storage(Decimal("1.5")) == Decimal("1.5000")

    def test_round_half_up_is_the_default(self) -> None:
        # Banker's rounding would give 0.12 here; accounting expects 0.13.
        assert quantize_currency(Decimal("0.125"), 2) == Decimal("0.1300")

    def test_currency_scale_widens_back_to_storage_scale(self) -> None:
        assert quantize_currency(Decimal("10.999"), 2) == Decimal("11.0000")

    @pytest.mark.parametrize(
        ("value", "places", "expected"),
        [
            ("115.00", 2, False),
            ("115.005", 2, True),
            ("115", 0, False),
            ("115.5", 0, True),
            ("1.2345", 4, False),
        ],
    )
    def test_precision_detection(self, value: str, places: int, expected: bool) -> None:
        assert exceeds_currency_precision(Decimal(value), places) is expected

    def test_storage_range_guard(self) -> None:
        assert within_storage_range(Decimal("999999999999999.9999"))
        assert not within_storage_range(Decimal("1000000000000000.0000"))

    def test_format_money_always_returns_a_string(self) -> None:
        assert format_money(Decimal("115.0000"), 2) == "115.00"
        assert isinstance(format_money(Decimal("1")), str)


class TestUuid7:
    def test_version_and_variant(self) -> None:
        value = uuid7()
        assert value.version == 7
        assert (value.int >> 62) & 0b11 == 0b10

    def test_values_are_monotonically_increasing(self) -> None:
        values = [uuid7() for _ in range(1000)]
        assert values == sorted(values)
        assert len(set(values)) == 1000

    def test_timestamp_is_recoverable(self) -> None:
        import time

        before = time.time_ns() // 1_000_000
        value = uuid7()
        after = time.time_ns() // 1_000_000
        assert before <= uuid7_timestamp_ms(value) <= after + 1

    def test_non_v7_uuid_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a UUIDv7"):
            uuid7_timestamp_ms(UUID("00000000-0000-4000-8000-000000000000"))


class TestIdempotencyHashing:
    BASE = {
        "company_id": UUID("018f0000-0000-7000-8000-000000000001"),
        "entry_date": "2026-03-31",
        "lines": [
            {"line_number": 1, "debit": Decimal("115.00"), "credit": Decimal("0")},
            {"line_number": 2, "debit": Decimal("0"), "credit": Decimal("115.00")},
        ],
    }

    def test_key_order_does_not_change_the_hash(self) -> None:
        reordered = {
            "lines": self.BASE["lines"],
            "entry_date": self.BASE["entry_date"],
            "company_id": self.BASE["company_id"],
        }
        assert payload_hash(self.BASE) == payload_hash(reordered)

    def test_equivalent_decimal_spellings_hash_the_same(self) -> None:
        variant = {
            **self.BASE,
            "lines": [
                {"line_number": 1, "debit": Decimal("115.0000"), "credit": Decimal("0.00")},
                {"line_number": 2, "debit": Decimal("0.0"), "credit": Decimal("115")},
            ],
        }
        assert payload_hash(self.BASE) == payload_hash(variant)

    def test_a_different_amount_changes_the_hash(self) -> None:
        variant = {
            **self.BASE,
            "lines": [
                {"line_number": 1, "debit": Decimal("115.01"), "credit": Decimal("0")},
                {"line_number": 2, "debit": Decimal("0"), "credit": Decimal("115.01")},
            ],
        }
        assert payload_hash(self.BASE) != payload_hash(variant)

    def test_hash_is_a_sha256_hex_digest(self) -> None:
        digest = payload_hash(self.BASE)
        assert len(digest) == 64
        assert all(char in "0123456789abcdef" for char in digest)

    def test_canonical_form_is_compact_and_sorted(self) -> None:
        rendered = canonical_payload({"b": 1, "a": 2})
        assert rendered == '{"a":2,"b":1}'


class TestPermissions:
    def test_auditor_cannot_write_accounting(self) -> None:
        auditor = AuthenticatedUser(
            id=uuid7(),
            email="auditor@example.com",
            full_name="Auditor",
            is_active=True,
            is_superuser=False,
            permissions=DEFAULT_ROLE_PERMISSIONS[Role.AUDITOR],
        )
        assert auditor.has_permission(Permission.ACCOUNTING_READ)
        assert auditor.has_permission(Permission.REPORTS_READ)
        assert not auditor.has_permission(Permission.ACCOUNTING_POST)
        assert not auditor.has_permission(Permission.ACCOUNTING_WRITE)
        assert not auditor.has_permission(Permission.PERIOD_CLOSE)

    def test_accountant_can_post_but_not_manage_users(self) -> None:
        accountant = AuthenticatedUser(
            id=uuid7(),
            email="accountant@example.com",
            full_name="Accountant",
            is_active=True,
            is_superuser=False,
            permissions=DEFAULT_ROLE_PERMISSIONS[Role.ACCOUNTANT],
        )
        assert accountant.has_all(
            Permission.ACCOUNTING_POST,
            Permission.ACCOUNTING_REVERSE,
            Permission.PERIOD_CLOSE,
        )
        assert not accountant.has_permission(Permission.USERS_MANAGE)
        assert not accountant.has_permission(Permission.PERIOD_REOPEN)

    def test_superuser_bypasses_every_check(self) -> None:
        root = AuthenticatedUser(
            id=uuid7(),
            email="root@example.com",
            full_name="Root",
            is_active=True,
            is_superuser=True,
        )
        assert all(root.has_permission(permission) for permission in Permission)

    def test_admin_role_carries_every_permission(self) -> None:
        assert DEFAULT_ROLE_PERMISSIONS[Role.ADMIN] == frozenset(Permission)
