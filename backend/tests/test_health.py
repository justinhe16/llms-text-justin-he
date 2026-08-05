"""Tests for the health endpoint."""

import asyncio
import time

import pytest
from httpx import AsyncClient

from app.api.deps import get_db_pool, get_optional_arq_pool
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


class _FakeOkRedis:
    """Stands in for an `ArqRedis` that answers `PING`."""

    async def ping(self) -> bool:
        return True


class _FakeFailingRedis:
    """Stands in for a Redis that refuses the connection or errors on the command."""

    async def ping(self) -> bool:
        raise ConnectionError("simulated redis failure")


class _FakeHangingRedis:
    """Stands in for an Upstash instance that has stopped answering rather than refusing."""

    async def ping(self) -> bool:
        await asyncio.sleep(30)
        return True


def _override(db: object, redis: object | None) -> None:
    """Point both of `/health`'s dependencies at fakes."""
    app.dependency_overrides[get_db_pool] = lambda: db
    app.dependency_overrides[get_optional_arq_pool] = lambda: redis


def _clear_overrides() -> None:
    app.dependency_overrides.pop(get_db_pool, None)
    app.dependency_overrides.pop(get_optional_arq_pool, None)


async def test_health_reports_ok_when_both_dependencies_are_reachable(
    client: AsyncClient,
) -> None:
    """`GET /health` answers 200 with every field `ok` when both probes succeed.

    **Asserts the raw response text, and that is the point of this particular case.**
    `response.json() == {...}` compares dictionaries, and dict equality in Python is
    order-independent — so reordering the fields on `HealthResponse` leaves every other
    assertion in this file green while breaking the `build-check` job in ci-backend.yml,
    which compares the body to a literal string. Byte equality here is what makes that a
    local `pytest` failure instead of something CI finds after a push.

    The literal below is the same one `build-check` asserts, character for character
    (Starlette's `JSONResponse` uses no whitespace separators). If it changes, that job
    changes in the same commit.
    """
    _override(_FakeOkPool(), _FakeOkRedis())
    try:
        response = await client.get("/health")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok", "redis": "ok"}
    assert response.text == '{"status":"ok","db":"ok","redis":"ok"}'


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
    _override(_FakeFailingPool(), _FakeOkRedis())
    try:
        response = await client.get("/health")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "error", "redis": "ok"}


async def test_health_stays_200_when_redis_fails_but_the_database_is_fine(
    client: AsyncClient,
) -> None:
    """A queue outage is reported, never fatal. This is the whole point of the field.

    The API only ever *enqueues* — the worker is the consumer — so an unreachable Redis
    costs it the ability to start a crawl and nothing else. Every read endpoint still
    works, so a non-200 here would take a serving API down over a dependency it does not
    need in order to serve reads, which is precisely the flapping-fleet failure the
    database case above guards against.

    `status` still says `"degraded"`, because it is. What "non-fatal" buys is the 200, not
    a flattering summary.
    """
    _override(_FakeOkPool(), _FakeFailingRedis())
    try:
        response = await client.get("/health")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "ok", "redis": "error"}


async def test_health_reports_redis_error_when_the_pool_was_never_opened(
    client: AsyncClient,
) -> None:
    """`app.main.lifespan` boots the API even when Redis is unreachable at startup.

    In that state `get_optional_arq_pool` yields `None` rather than raising, and `/health`
    has to report it like any other unreachable Redis. If resolving that dependency ever
    became an exception, the endpoint would return 500 in exactly the situation it exists
    to describe.
    """
    _override(_FakeOkPool(), None)
    try:
        response = await client.get("/health")
    finally:
        _clear_overrides()

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "ok", "redis": "error"}


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
    monkeypatch.setattr(health_router, "_CHECK_TIMEOUT_SECONDS", budget)
    _override(_FakeHangingPool(), _FakeOkRedis())

    started = time.monotonic()
    try:
        response = await client.get("/health")
    finally:
        _clear_overrides()
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "timeout", "redis": "ok"}
    # Generous relative to a 0.05s budget, but orders of magnitude below the 30s the fake
    # query would take if the endpoint actually waited for it.
    assert elapsed < 5, f"/health took {elapsed:.2f}s, so it waited on the hung query"


async def test_health_returns_within_one_budget_when_both_dependencies_hang(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two probes run concurrently, so a second sick dependency costs no extra time.

    Guards the regression that adding the Redis check most obviously invites: awaiting the
    two probes one after the other. That version passes every other test in this file and
    shows up only as `/health` taking two budgets instead of one — which, against a Fly
    `timeout` set above the endpoint's budget but not above twice it, is the difference
    between a reported degradation and a restarted machine.
    """
    budget = 0.05
    monkeypatch.setattr(health_router, "_CHECK_TIMEOUT_SECONDS", budget)
    _override(_FakeHangingPool(), _FakeHangingRedis())

    started = time.monotonic()
    try:
        response = await client.get("/health")
    finally:
        _clear_overrides()
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "db": "timeout", "redis": "timeout"}
    # Two probes at a 0.05s budget each: serial would be ~0.10s, concurrent ~0.05s. The
    # threshold sits far above both and far below the 30s either fake would take alone, so
    # this asserts "did not wait on the hung calls" without being timing-flaky.
    assert elapsed < 5, f"/health took {elapsed:.2f}s, so it waited on the hung probes"
