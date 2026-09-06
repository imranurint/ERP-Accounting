"""Prometheus metrics.

HTTP metrics plus a small set of accounting counters. Journal postings are
counted where they happen — in the posting service — so the number reflects
committed accounting activity rather than HTTP traffic.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

REGISTRY = CollectorRegistry(auto_describe=True)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests.",
    ["method", "path", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
    registry=REGISTRY,
)

http_errors_total = Counter(
    "http_errors_total",
    "HTTP responses with a 4xx or 5xx status.",
    ["method", "path", "status"],
    registry=REGISTRY,
)

journal_entries_posted_total = Counter(
    "journal_entries_posted_total",
    "Journal entries successfully committed to the general ledger.",
    ["company_id", "source_type"],
    registry=REGISTRY,
)

journal_posting_failures_total = Counter(
    "journal_posting_failures_total",
    "Journal postings rejected, labelled by business error code.",
    ["error_code"],
    registry=REGISTRY,
)

journal_posting_duration_seconds = Histogram(
    "journal_posting_duration_seconds",
    "End-to-end duration of the journal posting transaction.",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

journal_entries_reversed_total = Counter(
    "journal_entries_reversed_total",
    "Journal entries reversed.",
    registry=REGISTRY,
)

database_operation_duration_seconds = Histogram(
    "database_operation_duration_seconds",
    "Duration of named database operations.",
    ["operation"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
    registry=REGISTRY,
)

outbox_pending_events = Gauge(
    "outbox_pending_events",
    "Domain events waiting to be relayed.",
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST


def _route_template(request: Request) -> str:
    """Use the route pattern, not the raw path.

    `/api/v1/journal-entries/{entry_id}` keeps cardinality bounded; the raw
    path would create one time series per journal entry.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else "unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)

        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration = time.perf_counter() - started
            path = _route_template(request)
            http_request_duration_seconds.labels(request.method, path).observe(duration)
            http_requests_total.labels(request.method, path, str(status)).inc()
            if status >= 400:
                http_errors_total.labels(request.method, path, str(status)).inc()
