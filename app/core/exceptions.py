"""Structured application errors.

Every error the API returns is a `Problem`-style JSON document:

    {
      "error": "UNBALANCED_JOURNAL_ENTRY",
      "message": "Total debits must equal total credits.",
      "details": {...},
      "request_id": "..."
    }

Business errors are raised deep in the domain/application layers and mapped to
HTTP by a single exception handler in `app.main`. Routes never build error
responses by hand.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error this application raises deliberately."""

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.message
        self.details: dict[str, Any] = details or {}
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        super().__init__(self.message)

    def to_dict(self, request_id: str | None = None) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
            "request_id": request_id,
        }


# --------------------------------------------------------------------------
# Generic HTTP-shaped errors
# --------------------------------------------------------------------------
class ValidationError(AppError):
    code = "VALIDATION_ERROR"
    status_code = 422
    message = "The request payload is invalid."


class NotFoundError(AppError):
    code = "NOT_FOUND"
    status_code = 404
    message = "The requested resource does not exist."


class ConflictError(AppError):
    code = "CONFLICT"
    status_code = 409
    message = "The request conflicts with the current state of the resource."


class UnauthorizedError(AppError):
    code = "UNAUTHORIZED"
    status_code = 401
    message = "Authentication is required."


class ForbiddenError(AppError):
    code = "FORBIDDEN"
    status_code = 403
    message = "You do not have permission to perform this action."


class RateLimitedError(AppError):
    code = "RATE_LIMITED"
    status_code = 429
    message = "Too many requests."


class PayloadTooLargeError(AppError):
    code = "PAYLOAD_TOO_LARGE"
    status_code = 413
    message = "Request body exceeds the configured limit."


class ConcurrencyError(ConflictError):
    code = "CONCURRENT_MODIFICATION"
    message = "The resource was modified by another request. Reload and retry."


class ServiceUnavailableError(AppError):
    code = "SERVICE_UNAVAILABLE"
    status_code = 503
    message = "A required dependency is unavailable."
