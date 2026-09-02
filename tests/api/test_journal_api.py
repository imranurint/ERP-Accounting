"""HTTP-level tests: authentication, authorisation, validation, idempotency."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.domain.identity.permissions import Role
from tests.factories import TEST_PASSWORD, DemoData, invoice_payload

pytestmark = [pytest.mark.api, pytest.mark.asyncio]

API = "/api/v1"


async def token_for(client: AsyncClient, role: Role) -> str:
    response = await client.post(
        f"{API}/auth/login",
        json={"email": f"{str(role).lower()}@example.com", "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


async def auth(client: AsyncClient, role: Role = Role.ACCOUNTANT) -> dict[str, str]:
    return {"Authorization": f"Bearer {await token_for(client, role)}"}


# ---------------------------------------------------------------------------
class TestAuthentication:
    async def test_login_returns_a_token_pair(self, client: AsyncClient, demo: DemoData) -> None:
        response = await client.post(
            f"{API}/auth/login",
            json={"email": "accountant@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["access_token"]
        assert body["refresh_token"]

    async def test_wrong_password_is_rejected(self, client: AsyncClient, demo: DemoData) -> None:
        response = await client.post(
            f"{API}/auth/login",
            json={"email": "accountant@example.com", "password": "wrong-password"},
        )
        assert response.status_code == 401
        assert response.json()["error"] == "INVALID_CREDENTIALS"

    async def test_unknown_email_gives_the_same_error(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        response = await client.post(
            f"{API}/auth/login",
            json={"email": "nobody@example.com", "password": TEST_PASSWORD},
        )
        assert response.status_code == 401
        assert response.json()["error"] == "INVALID_CREDENTIALS"

    async def test_me_reports_roles_and_permissions(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        response = await client.get(f"{API}/auth/me", headers=await auth(client))
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == "accountant@example.com"
        assert body["roles"] == ["Accountant"]
        assert "accounting:post" in body["permissions"]
        assert "users:manage" not in body["permissions"]
        assert "hashed_password" not in body

    async def test_refresh_rotates_the_token(self, client: AsyncClient, demo: DemoData) -> None:
        login = await client.post(
            f"{API}/auth/login",
            json={"email": "accountant@example.com", "password": TEST_PASSWORD},
        )
        first_refresh = login.json()["refresh_token"]

        rotated = await client.post(f"{API}/auth/refresh", json={"refresh_token": first_refresh})
        assert rotated.status_code == 200
        assert rotated.json()["refresh_token"] != first_refresh

        # The old token is dead.
        replay = await client.post(f"{API}/auth/refresh", json={"refresh_token": first_refresh})
        assert replay.status_code == 401

    async def test_accounting_endpoints_require_a_token(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        response = await client.get(
            f"{API}/journal-entries", params={"company_id": str(demo.company_id)}
        )
        assert response.status_code == 401

    async def test_a_garbage_token_is_rejected(self, client: AsyncClient, demo: DemoData) -> None:
        response = await client.get(
            f"{API}/journal-entries",
            params={"company_id": str(demo.company_id)},
            headers={"Authorization": "Bearer not-a-jwt"},
        )
        assert response.status_code == 401
        assert response.json()["error"] == "INVALID_TOKEN"


# ---------------------------------------------------------------------------
class TestAuthorization:
    async def test_viewer_cannot_post(self, client: AsyncClient, demo: DemoData) -> None:
        response = await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=await auth(client, Role.VIEWER),
        )
        assert response.status_code == 403
        body = response.json()
        assert body["error"] == "PERMISSION_DENIED"
        assert body["details"]["required_permission"] == "accounting:write"

    async def test_auditor_can_read_but_not_write(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        headers = await auth(client, Role.AUDITOR)

        read = await client.get(
            f"{API}/journal-entries",
            params={"company_id": str(demo.company_id)},
            headers=headers,
        )
        assert read.status_code == 200

        write = await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=headers,
        )
        assert write.status_code == 403

    async def test_accountant_cannot_reopen_a_period(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        headers = await auth(client, Role.ACCOUNTANT)
        period_id = demo.period("March").id

        closed = await client.post(
            f"{API}/fiscal-periods/{period_id}/close",
            params={"company_id": str(demo.company_id)},
            headers=headers,
        )
        assert closed.status_code == 200

        reopened = await client.post(
            f"{API}/fiscal-periods/{period_id}/reopen",
            params={"company_id": str(demo.company_id)},
            headers=headers,
        )
        assert reopened.status_code == 403

    async def test_admin_can_reopen_a_period(self, client: AsyncClient, demo: DemoData) -> None:
        period_id = demo.period("March").id
        admin = await auth(client, Role.ADMIN)

        await client.post(
            f"{API}/fiscal-periods/{period_id}/close",
            params={"company_id": str(demo.company_id)},
            headers=admin,
        )
        response = await client.post(
            f"{API}/fiscal-periods/{period_id}/reopen",
            params={"company_id": str(demo.company_id)},
            headers=admin,
        )
        assert response.status_code == 200
        assert response.json()["status"] == "OPEN"


# ---------------------------------------------------------------------------
class TestInvoiceEndToEnd:
    async def test_posting_the_invoice_example(self, client: AsyncClient, demo: DemoData) -> None:
        response = await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=await auth(client),
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["status"] == "POSTED"
        assert body["entry_number"] == "JE-2026-000001"
        assert body["total_debits"] == "115.0000"
        assert body["total_credits"] == "115.0000"
        assert len(body["items"]) == 3

        # Money is serialised as strings, never JSON numbers.
        for item in body["items"]:
            assert isinstance(item["debit"], str)
            assert isinstance(item["credit"], str)

        by_number = {item["account_number"]: item for item in body["items"]}
        assert by_number["101"]["debit"] == "115.0000"
        assert by_number["402"]["credit"] == "100.0000"
        assert by_number["205"]["credit"] == "15.0000"

    async def test_trial_balance_after_posting(self, client: AsyncClient, demo: DemoData) -> None:
        headers = await auth(client)
        await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=headers,
        )

        response = await client.get(
            f"{API}/reports/trial-balance",
            params={
                "company_id": str(demo.company_id),
                "period_id": str(demo.period("March").id),
            },
            headers=headers,
        )
        assert response.status_code == 200
        report = response.json()
        assert report["is_balanced"] is True
        assert report["total_debits"] == "115.0000"
        assert report["total_credits"] == "115.0000"

    async def test_reversing_the_invoice(self, client: AsyncClient, demo: DemoData) -> None:
        headers = await auth(client)
        created = await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=headers,
        )
        entry_id = created.json()["id"]

        response = await client.post(
            f"{API}/journal-entries/{entry_id}/reverse",
            params={"company_id": str(demo.company_id)},
            json={"reference": "Credit note CN-1"},
            headers=headers,
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["original"]["status"] == "REVERSED"
        assert body["reversal"]["status"] == "POSTED"
        assert body["reversal"]["entry_number"] == "JE-2026-000002"
        assert body["original"]["reversed_by_entry_id"] == body["reversal"]["id"]

        reversal_lines = {item["account_number"]: item for item in body["reversal"]["items"]}
        assert reversal_lines["101"]["credit"] == "115.0000"
        assert reversal_lines["402"]["debit"] == "100.0000"

    async def test_csv_export(self, client: AsyncClient, demo: DemoData) -> None:
        headers = await auth(client)
        await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=headers,
        )

        response = await client.get(
            f"{API}/reports/trial-balance",
            params={
                "company_id": str(demo.company_id),
                "period_id": str(demo.period("March").id),
                "format": "csv",
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")

        lines = response.text.strip().splitlines()
        assert lines[0].startswith("account_number,account_name")
        assert any(line.startswith("101,") for line in lines)
        assert lines[-1].startswith("TOTAL")


# ---------------------------------------------------------------------------
class TestIdempotencyOverHttp:
    async def test_header_key_makes_a_retry_safe(self, client: AsyncClient, demo: DemoData) -> None:
        headers = await auth(client)
        payload = invoice_payload(demo.company_id, demo.period("March").id, idempotency_key=None)
        headers["Idempotency-Key"] = "sales_invoice:INV-9923"

        first = await client.post(f"{API}/journal-entries", json=payload, headers=headers)
        second = await client.post(f"{API}/journal-entries", json=payload, headers=headers)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.json()["id"] == second.json()["id"]
        assert first.json()["entry_number"] == second.json()["entry_number"]

    async def test_same_key_different_payload_is_a_conflict(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        headers = await auth(client)
        payload = invoice_payload(demo.company_id, demo.period("March").id)
        await client.post(f"{API}/journal-entries", json=payload, headers=headers)

        altered: dict[str, Any] = {
            **payload,
            "lines": [
                {"line_number": 1, "account_number": "101", "debit": "230.00", "credit": "0.00"},
                {"line_number": 2, "account_number": "402", "debit": "0.00", "credit": "230.00"},
            ],
        }
        response = await client.post(f"{API}/journal-entries", json=altered, headers=headers)
        assert response.status_code == 409
        assert response.json()["error"] == "DUPLICATE_IDEMPOTENCY_KEY"


# ---------------------------------------------------------------------------
class TestValidationOverHttp:
    async def test_unbalanced_entry_returns_a_structured_error(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        payload = invoice_payload(demo.company_id, demo.period("March").id, idempotency_key=None)
        payload["lines"][2]["credit"] = "14.00"

        response = await client.post(
            f"{API}/journal-entries", json=payload, headers=await auth(client)
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "UNBALANCED_JOURNAL_ENTRY"
        assert body["details"]["total_debits"] == "115.0000"
        assert body["details"]["total_credits"] == "114.0000"
        assert body["details"]["difference"] == "1.0000"
        assert body["request_id"]

    async def test_line_with_both_sides_is_rejected(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        payload = invoice_payload(demo.company_id, demo.period("March").id, idempotency_key=None)
        payload["lines"][0]["credit"] = "115.00"

        response = await client.post(
            f"{API}/journal-entries", json=payload, headers=await auth(client)
        )
        assert response.status_code == 422
        assert response.json()["error"] == "INVALID_JOURNAL_LINE"

    async def test_float_amounts_are_refused(self, client: AsyncClient, demo: DemoData) -> None:
        payload = invoice_payload(demo.company_id, demo.period("March").id, idempotency_key=None)
        payload["lines"][0]["debit"] = 115.00  # a JSON number, not a string

        response = await client.post(
            f"{API}/journal-entries", json=payload, headers=await auth(client)
        )
        assert response.status_code == 422
        assert response.json()["error"] == "VALIDATION_ERROR"

    async def test_posting_into_a_closed_period_is_rejected(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        headers = await auth(client)
        period_id = demo.period("March").id

        closed = await client.post(
            f"{API}/fiscal-periods/{period_id}/close",
            params={"company_id": str(demo.company_id)},
            headers=headers,
        )
        assert closed.status_code == 200
        assert closed.json()["status"] == "CLOSED"

        response = await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, period_id),
            headers=headers,
        )
        assert response.status_code == 409
        assert response.json()["error"] == "FISCAL_PERIOD_CLOSED"

    async def test_unknown_account_number_is_reported_with_its_line(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        payload = invoice_payload(demo.company_id, demo.period("March").id, idempotency_key=None)
        payload["lines"][1]["account_number"] = "999"

        response = await client.post(
            f"{API}/journal-entries", json=payload, headers=await auth(client)
        )
        assert response.status_code == 404
        body = response.json()
        assert body["error"] == "ACCOUNT_NOT_FOUND"
        assert body["details"]["line_number"] == 2


# ---------------------------------------------------------------------------
class TestImmutabilityOverHttp:
    async def _post_invoice(self, client: AsyncClient, demo: DemoData) -> str:
        response = await client.post(
            f"{API}/journal-entries",
            json=invoice_payload(demo.company_id, demo.period("March").id),
            headers=await auth(client),
        )
        assert response.status_code == 201
        return str(response.json()["id"])

    async def test_posted_entry_cannot_be_patched(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        entry_id = await self._post_invoice(client, demo)
        response = await client.patch(
            f"{API}/journal-entries/{entry_id}",
            params={"company_id": str(demo.company_id)},
            json={"reference": "tampered"},
            headers=await auth(client),
        )
        assert response.status_code == 409
        assert response.json()["error"] == "POSTED_ENTRY_CANNOT_BE_MODIFIED"

    async def test_posted_entry_cannot_be_deleted(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        entry_id = await self._post_invoice(client, demo)
        response = await client.delete(
            f"{API}/journal-entries/{entry_id}",
            params={"company_id": str(demo.company_id)},
            headers=await auth(client),
        )
        assert response.status_code == 409
        assert response.json()["error"] == "POSTED_ENTRY_CANNOT_BE_DELETED"

    async def test_posted_entry_cannot_be_voided(self, client: AsyncClient, demo: DemoData) -> None:
        entry_id = await self._post_invoice(client, demo)
        response = await client.post(
            f"{API}/journal-entries/{entry_id}/void",
            params={"company_id": str(demo.company_id)},
            json={"reason": "mistake"},
            headers=await auth(client),
        )
        assert response.status_code == 409
        assert response.json()["error"] == "INVALID_JOURNAL_STATE"

    async def test_draft_can_be_patched_and_deleted(
        self, client: AsyncClient, demo: DemoData
    ) -> None:
        headers = await auth(client)
        payload = invoice_payload(demo.company_id, demo.period("March").id, idempotency_key=None)
        payload["post_immediately"] = False

        created = await client.post(f"{API}/journal-entries", json=payload, headers=headers)
        assert created.status_code == 201
        assert created.json()["status"] == "DRAFT"
        entry_id = created.json()["id"]

        patched = await client.patch(
            f"{API}/journal-entries/{entry_id}",
            params={"company_id": str(demo.company_id)},
            json={"reference": "Revised"},
            headers=headers,
        )
        assert patched.status_code == 200
        assert patched.json()["reference"] == "Revised"

        deleted = await client.delete(
            f"{API}/journal-entries/{entry_id}",
            params={"company_id": str(demo.company_id)},
            headers=headers,
        )
        assert deleted.status_code == 204


# ---------------------------------------------------------------------------
class TestPagination:
    async def test_list_is_paginated(self, client: AsyncClient, demo: DemoData) -> None:
        headers = await auth(client)
        for index in range(5):
            payload = invoice_payload(
                demo.company_id,
                demo.period("March").id,
                idempotency_key=f"page-{index}",
            )
            payload["source_document"] = f"INV-{index}"
            response = await client.post(f"{API}/journal-entries", json=payload, headers=headers)
            assert response.status_code == 201

        page = await client.get(
            f"{API}/journal-entries",
            params={"company_id": str(demo.company_id), "page_size": 2},
            headers=headers,
        )
        assert page.status_code == 200
        body = page.json()
        assert len(body["items"]) == 2
        assert body["total"] == 5
        assert body["has_more"] is True
        assert body["next_cursor"]
        assert body["items"][0]["total_debits"] == "115.0000"

    async def test_page_size_is_capped(self, client: AsyncClient, demo: DemoData) -> None:
        response = await client.get(
            f"{API}/journal-entries",
            params={"company_id": str(demo.company_id), "page_size": 10_000},
            headers=await auth(client),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
class TestHealthAndDocs:
    async def test_liveness(self, client: AsyncClient) -> None:
        response = await client.get("/livez")
        assert response.status_code == 200
        assert response.json()["status"] == "alive"

    async def test_readiness_checks_the_database(self, client: AsyncClient) -> None:
        response = await client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"]["status"] == "ok"

    async def test_openapi_document_is_served(self, client: AsyncClient) -> None:
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        paths = response.json()["paths"]
        assert "/api/v1/journal-entries" in paths
        assert "/api/v1/reports/trial-balance" in paths

    async def test_every_response_carries_a_request_id(self, client: AsyncClient) -> None:
        response = await client.get("/livez")
        assert response.headers["X-Request-ID"]
        assert response.headers["X-Correlation-ID"]
