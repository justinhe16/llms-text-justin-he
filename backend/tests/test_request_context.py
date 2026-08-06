"""Tests for `app.api.middleware.request_context` — the API's correlation id, the
`X-Request-ID` header, and the one-line request summary.

Every test drives a real HTTP request through a throwaway FastAPI app carrying the real
middleware, the real exception handler, and — for the authenticated cases — the real
`CurrentUser` dependency over a really-signed token from the `jwks_cache` fixture. The
production app (`app.main.app`) is only touched by the last test in this file, which exists
to prove the middleware is actually installed on the thing that ships; everything else uses
a throwaway app so that no route, override, or exception handler of this suite's can leak
into another module or into the committed OpenAPI document.

**`test_the_summary_line_names_the_authenticated_user` is the load-bearing one.** The
middleware logs its summary *after* the handler has returned, and `user_id` is bound by an
authentication dependency deep inside it. That only works because the middleware is a pure
ASGI callable rather than a `BaseHTTPMiddleware` subclass: `BaseHTTPMiddleware` runs the
downstream app in a separate anyio task, which gets a COPY of the context, so a contextvar
set inside the request would be invisible again by the time the summary was logged. If a
future Starlette introduces a task boundary on this path, that test is what fails.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import pytest
from conftest import _JsonLogCapture, _SigningKey, user_claims
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api.deps import CurrentUser
from app.api.middleware.request_context import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    request_id_exception_handler,
)
from app.core.auth.jwks import JwksCache, get_jwks_cache


HANDLER_LOGGER = "app.tests.request_context.handler"
SUMMARY_LOGGER = "app.api.middleware.request_context"


@pytest.fixture
def context_app(jwks_cache: JwksCache) -> FastAPI:
    """A throwaway app wired exactly the way `app.main.create_app()` wires the real one."""
    api = FastAPI()
    api.add_middleware(RequestContextMiddleware)
    api.add_exception_handler(Exception, request_id_exception_handler)

    @api.get("/echo")
    async def echo() -> dict[str, str]:
        # Stands in for a service, a reader, or an internals module: something that logs
        # without ever having been handed a request id.
        logging.getLogger(HANDLER_LOGGER).info("deep inside the handler")
        return {"ok": "yes"}

    @api.get("/protected")
    async def protected(user_id: CurrentUser) -> dict[str, str]:
        logging.getLogger(HANDLER_LOGGER).info("authenticated work")
        return {"user_id": user_id}

    @api.get("/conflict")
    async def conflict() -> dict[str, str]:
        raise HTTPException(status_code=409, detail="a run is already in flight")

    @api.get("/boom")
    async def boom() -> dict[str, str]:
        raise RuntimeError("kaboom")

    @api.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    api.dependency_overrides[get_jwks_cache] = lambda: jwks_cache
    return api


@pytest.fixture
async def context_client(context_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """`raise_app_exceptions=False` so the 500 an unhandled exception produces can be
    inspected as a response. Without it httpx re-raises the exception into the test and the
    header this middleware exists to stamp is never observed."""
    transport = ASGITransport(app=context_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _summary(json_logs: _JsonLogCapture) -> dict[str, Any]:
    """The one request-summary line, failing loudly if there was not exactly one."""
    (line,) = json_logs.at(SUMMARY_LOGGER)
    return line


# -----------------------------------------------------------------------------------------
# The header.
# -----------------------------------------------------------------------------------------


async def test_every_response_carries_a_generated_request_id(context_client: AsyncClient) -> None:
    response = await context_client.get("/echo")

    assert response.status_code == 200
    # A UUID, not a counter: two API machines minting ids must not collide, and the id ends
    # up in a support conversation ("what does the header say?").
    UUID(response.headers[REQUEST_ID_HEADER])


async def test_an_incoming_request_id_is_honoured_so_one_id_spans_both_hops(
    context_client: AsyncClient,
) -> None:
    """The Next.js BFF proxies every browser request server-side (ARCHITECTURE.md §8.1), so
    accepting an inbound id is what lets one identifier cover the Vercel hop and the Fly hop
    instead of two unrelated ones that nobody can join up."""
    response = await context_client.get("/echo", headers={"X-Request-ID": "abc-123_45:67"})

    assert response.headers[REQUEST_ID_HEADER] == "abc-123_45:67"


@pytest.mark.parametrize(
    ("case", "value"),
    [
        ("whitespace", "has spaces"),
        ("a newline", "line-one\nline-two"),
        ("json punctuation", '{"injected":"field"}'),
        ("too long", "x" * 129),
        ("empty", ""),
    ],
)
async def test_a_malformed_incoming_request_id_is_replaced_rather_than_trusted(
    context_client: AsyncClient, case: str, value: str
) -> None:
    """An inbound header is attacker-controlled and ends up in a field an operator greps
    by. Anything that is not id-shaped is discarded and a fresh id generated — not answered
    with a 400, because a malformed correlation header is no reason to fail a request that
    is otherwise perfectly good."""
    response = await context_client.get("/echo", headers={"X-Request-ID": value})

    echoed = response.headers[REQUEST_ID_HEADER]
    assert echoed != value, case
    UUID(echoed)


async def test_the_500_from_an_unhandled_exception_still_carries_the_header(
    context_client: AsyncClient,
) -> None:
    """The response a caller most needs a correlation id for. It is written by Starlette's
    `ServerErrorMiddleware`, which sits ABOVE this middleware and uses the original `send`
    — so it takes a second, deliberate stamp from `request_id_exception_handler` to get
    there."""
    response = await context_client.get("/boom")

    assert response.status_code == 500
    UUID(response.headers[REQUEST_ID_HEADER])


async def test_the_real_application_installs_the_middleware(client: AsyncClient) -> None:
    """Against `app.main.app` itself, on a route that exists nowhere: a 404 needs no
    database, no Redis, and no JWKS, so this asserts the wiring in `create_app()` and
    nothing else. Everything above it would still pass with the middleware added only to
    this suite's throwaway app."""
    response = await client.get("/definitely-not-a-route")

    assert response.status_code == 404
    UUID(response.headers[REQUEST_ID_HEADER])


# -----------------------------------------------------------------------------------------
# The correlation id on the logs.
# -----------------------------------------------------------------------------------------


async def test_a_request_id_bound_in_middleware_reaches_a_log_line_inside_the_handler(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    """The whole point of the contextvar: no handler, service, or reader is passed an id,
    and every line carries one anyway."""
    response = await context_client.get("/echo", headers={"X-Request-ID": "spanning-id"})

    assert response.headers[REQUEST_ID_HEADER] == "spanning-id"
    handler_lines = json_logs.at(HANDLER_LOGGER)
    assert [line["request_id"] for line in handler_lines] == ["spanning-id"]
    assert _summary(json_logs)["request_id"] == "spanning-id"


async def test_concurrent_requests_never_share_a_request_id(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    """The API's version of the worker's isolation property (`tests/test_logging.py`), over
    real, simultaneous, in-flight requests rather than over bare tasks."""
    ids = [f"concurrent-{index}" for index in range(8)]
    await asyncio.gather(
        *(context_client.get("/echo", headers={"X-Request-ID": value}) for value in ids)
    )

    observed = {line["request_id"] for line in json_logs.at(HANDLER_LOGGER)}
    assert observed == set(ids)
    # Every summary is paired with its own handler line, so no request logged under
    # another's id.
    assert {line["request_id"] for line in json_logs.at(SUMMARY_LOGGER)} == set(ids)


async def test_no_request_id_leaks_out_of_the_request_that_bound_it(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    """The `contextvar` is reset on the way out, so a line logged by the lifespan, a
    background task, or the next request cannot inherit a stale id."""
    await context_client.get("/echo")
    logging.getLogger(HANDLER_LOGGER).info("after the request is over")

    assert "request_id" not in json_logs.at(HANDLER_LOGGER)[-1]


# -----------------------------------------------------------------------------------------
# The one-line request summary.
# -----------------------------------------------------------------------------------------


async def test_the_summary_line_carries_method_path_status_and_duration(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    """The line that answers most "what happened?" questions with no other instrumentation."""
    await context_client.get("/echo")

    summary = _summary(json_logs)
    assert summary["method"] == "GET"
    assert summary["path"] == "/echo"
    assert summary["status"] == 200
    assert isinstance(summary["duration_ms"], float)
    assert summary["duration_ms"] >= 0
    assert summary["level"] == "INFO"


async def test_the_summary_line_names_the_authenticated_user(
    context_client: AsyncClient, json_logs: _JsonLogCapture, es256_key: _SigningKey
) -> None:
    """`user_id` is bound by `app.core.auth.dependencies` deep inside the request and read
    by the middleware after the handler returns — see this module's docstring for why that
    is a real property of the implementation and not a coincidence."""
    claims = user_claims()
    token = es256_key.sign(claims)

    response = await context_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert _summary(json_logs)["user_id"] == claims["sub"]


async def test_an_unauthenticated_request_has_no_user_id_rather_than_a_placeholder(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    await context_client.get("/protected")

    summary = _summary(json_logs)
    assert summary["status"] == 401
    assert "user_id" not in summary


async def test_the_authorization_header_never_reaches_the_log_stream(
    context_client: AsyncClient, json_logs: _JsonLogCapture, es256_key: _SigningKey
) -> None:
    """The middleware never reads the header at all, and the redaction filter is the second
    line of defence. This asserts the outcome both of them exist for."""
    token = es256_key.sign(user_claims())

    await context_client.get("/protected", headers={"Authorization": f"Bearer {token}"})

    assert token not in json_logs.raw
    assert "Bearer" not in json_logs.raw


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_level"),
    [
        ("/echo", 200, "INFO"),
        # 409 and 429 are the two 4xx that mean something went wrong rather than "a client
        # asked for something silly", so they are the two that earn a WARNING.
        ("/conflict", 409, "WARNING"),
        ("/boom", 500, "ERROR"),
        # Fly probes /health every 10 seconds forever; at INFO that is ~8,600 lines a day
        # saying nothing happened.
        ("/health", 200, "DEBUG"),
    ],
)
async def test_the_summary_level_reflects_how_bad_the_outcome_was(
    context_client: AsyncClient,
    json_logs: _JsonLogCapture,
    path: str,
    expected_status: int,
    expected_level: str,
) -> None:
    response = await context_client.get(path)

    assert response.status_code == expected_status
    summary = _summary(json_logs)
    assert summary["status"] == expected_status
    assert summary["level"] == expected_level


async def test_an_unhandled_exception_logs_one_error_line_with_the_full_traceback(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    """With no error-tracking service anywhere in this system, this line IS the incident
    record (ARCHITECTURE.md §3.8), so it carries the whole traceback and the request id
    needed to find everything else that request did."""
    await context_client.get("/boom", headers={"X-Request-ID": "the-broken-one"})

    summary = _summary(json_logs)
    assert summary["level"] == "ERROR"
    assert summary["request_id"] == "the-broken-one"
    assert "RuntimeError: kaboom" in summary["exception"]
    assert "Traceback (most recent call last)" in summary["exception"]


async def test_the_summary_path_excludes_the_query_string(
    context_client: AsyncClient, json_logs: _JsonLogCapture
) -> None:
    """A query string is caller-supplied and is exactly where a token would end up if one
    were ever passed in a URL, so it is never logged — not even redacted."""
    await context_client.get("/echo", params={"access_token": "eyJhbGciOiJub25lIn0.e30."})

    summary = _summary(json_logs)
    assert summary["path"] == "/echo"
    assert "eyJhbGciOiJub25lIn0" not in json_logs.raw
