"""Tests for the health endpoint."""

import asyncio
import time

import pytest
from httpx import AsyncClient

from app.api.deps import get_db_pool
from app.api.routers import health as health_router
from app.main import app


class _FakeOkPool:
    """Stands in for a `Pool` whose `SELECT 1` check succeeds."""

    async def fetchval(self, query: str, *args: object, timeout: float | None = None) -> int:
        return 1


class _FakeFailingPool:
    """Stands in for a `Pool` whose query fails — the PER-142 concern, encoded as a test."""

    async def fetchval(self, query: str, *args: object, timeout: float | None = None) -> int:
        raise ConnectionError("simulated database failure")


class _FakeHangingPool:
    """Stands in for a Postgres that has stopped answering rather than refusing.

    Deliberately ignores the `timeout` argument, which is precisely how a real hung server
    behaves from the caller's point of view: asyncpg's own `timeout=` fires but then blocks
    trying to send a cancellation request over a *second* connection to the same
    unresponsive server. Measured against a frozen Postgres, a 1s budget took >25s to
    return. A fake that merely raised instantly (as the two above do) cannot catch that
    regression — which is exactly how it was missed the first time.
    """

    async def fetchval(self, query: str, *args: object, timeout: float | None = None) -> int:
        await asyncio.sleep(30)
        return 1


async def test_health_reports_ok_when_the_database_is_reachable(client: AsyncClient) -> None:
    """`GET /health` answers 200 with `status: ok` and `db: ok` when `SELECT 1` succeeds."""
    app.dependency_overrides[get_db_pool] = lambda: _FakeOkPool()
    try:
        response = await client.get("/health")
    finally:
        del app.dependency_overrides[get_db_pool]

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}


async def test_health_stays_200_and_reports_degraded_when_the_database_fails(
    client: AsyncClient,
) -> None:
    """A failing database must not turn a healthy process into a Fly restart.

    Fly polls `/health` every 10 seconds; restarting a healthy web machine does not fix a
    Postgres outage, it just adds a flapping fleet on top of one. The status code stays
    200 and the outage is reported in the body instead (ARCHITECTURE.md is silent on this
    endpoint, but PER-142's original concern — a check that turns one slow dependency into
    a restart loop — still has to hold).
    """
    app.dependency_overrides[get_db_pool] = lambda: _FakeFailingPool()
    try:
        response = await client.get("/health")
    finally:
        del app.dependency_overrides[get_db_pool]

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "error"}


async def test_health_returns_within_its_budget_when_the_database_hangs(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung database must not hang `GET /health` itself.

    The regression this guards: the check originally delegated its budget to asyncpg's
    per-call `timeout=`, which does not bound anything when the server has stopped
    answering, so `/health` blocked for tens of seconds — the exact behavior that makes
    Fly's poll time out and restart a machine whose only problem is a sick database.

    The budget is shortened here so the suite stays fast; what is asserted is that the
    endpoint returns on the order of its budget rather than waiting for the query.
    """
    budget = 0.05
    monkeypatch.setattr(health_router, "_DB_CHECK_TIMEOUT_SECONDS", budget)
    app.dependency_overrides[get_db_pool] = lambda: _FakeHangingPool()

    started = time.monotonic()
    try:
        response = await client.get("/health")
    finally:
        del app.dependency_overrides[get_db_pool]
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "timeout"}
    # Generous relative to a 0.05s budget, but orders of magnitude below the 30s the fake
    # query would take if the endpoint actually waited for it.
    assert elapsed < 5, f"/health took {elapsed:.2f}s, so it waited on the hung query"
