"""Shared pytest fixtures.

Four families of fixtures live here: an in-process HTTP client, which is all a shallow
`/health` needs; for tests that exercise real Postgres, like `tests/test_transaction.py`, a
session-scoped pool and a function-scoped, always-rolled-back connection; for
`tests/test_jwks.py` and `tests/test_auth_dependencies.py`, a kit for generating throwaway
signing keys and standing up a fake JWKS endpoint that counts genuine HTTP requests; and —
at the bottom of this file — signed-in API clients for two different users, which is what
lets a feature suite assert both halves of the authorization contract (ARCHITECTURE.md §4)
against the real application.
"""

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, NoReturn
from uuid import UUID

import asyncpg
import httpx
import jwt
import pytest
from arq import create_pool
from arq.connections import ArqRedis
from asyncpg import Connection, Pool
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from redis.exceptions import RedisError


# `app.core.settings` validates its configuration at import time (by design — see that
# module), so the required variables must exist before `app.main` is imported below.
# These are obvious non-values: never put a real credential in this file.
#
# Assigned unconditionally rather than with setdefault(), so that a developer's exported
# variables and their backend/.env cannot reach the suite. Nothing that reads these
# specific settings-backed values opens a real connection — the database fixtures below
# deliberately use a *different* variable (TEST_DATABASE_URL) for that. The suite decides
# what app.core.settings sees; the surrounding environment does not.
#
# Captured before the override, because CI hands the suite a real, throwaway Postgres in
# DATABASE_URL — see TEST_DATABASE_URL below.
_ambient_database_url = os.environ.get("DATABASE_URL", "")

os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = "postgresql://localhost:5432/llms_text_test"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["SUPABASE_URL"] = "https://test-project.supabase.co"
os.environ["SUPABASE_SECRET_KEY"] = "not-a-real-key"

from app.api.deps import get_db_pool  # noqa: E402  — must follow the assignments above
from app.core.auth.jwks import JwksCache, get_jwks_cache  # noqa: E402  — as above
from app.infrastructure.queue.pool import redis_settings_from_url  # noqa: E402  — as above
from app.main import app  # noqa: E402  — must follow the assignments above


# Which real Postgres the database-backed fixtures below connect to. Kept distinct from
# DATABASE_URL above so that isolation stays absolute — app.core.settings never sees a
# real database — while these fixtures still get something real to run against.
#
# Resolution order:
#   1. TEST_DATABASE_URL, set explicitly. `make test` fills it in from the local Supabase
#      stack (see the Makefile and scripts/local-env.sh); export it yourself to run these
#      tests with a bare `pytest`.
#   2. In CI only, the ambient DATABASE_URL. .github/workflows/ci-backend.yml stands up a
#      `postgres:16` service container and points DATABASE_URL at it, and without this the
#      database-backed tests would silently *skip* in CI — a green required check over
#      commit/rollback guarantees that were never actually exercised.
#   3. Nothing, in which case those tests skip with a message saying how to enable them.
#
# Step 2 is deliberately gated on CI rather than always falling back. Locally, an exported
# DATABASE_URL is plausibly a real database someone cares about, and these fixtures CREATE
# and DROP a table; in CI it is a container that is destroyed with the job. The gate is
# what keeps "run the tests" from ever meaning "write to the database I happen to have
# exported".
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL") or (
    _ambient_database_url if os.environ.get("CI") else ""
)

# An obviously test-only name for the scratch table tests/test_transaction.py reads and
# writes. Created and dropped by the db_pool fixture below.
_SCRATCH_TABLE = "per_145_transaction_scratch_test"

# Which real Redis the queue-backed fixtures below connect to, and which logical database
# on it. **Database 15, not 0, and the fixture FLUSHES it** — the same "create a scratch
# space and destroy it afterwards" contract `db_pool` has with `_SCRATCH_TABLE`, except
# Redis has no tables to scope the damage to. Index 0 is where docker-compose.yml's local
# container keeps a developer's actual `make dev` queue, and this must never be pointed at
# it. Override only with another throwaway.
#
# Unlike TEST_DATABASE_URL there is no CI special case, because there is nothing to
# discover: docker-compose.yml and the `redis:7-alpine` service in ci-backend.yml both
# publish 6379 on localhost, so one default is correct in both places.
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")

# The queue the enqueue tests read and write. Named, rather than left as arq's default
# `arq:queue`, so that a misconfigured TEST_REDIS_URL pointing at a real instance is
# visible in `redis-cli KEYS *` instead of quietly interleaving with production job ids.
TEST_QUEUE_NAME = "per_157_test_queue"


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app in-process.

    ASGITransport calls the application directly, so the suite needs no running server
    and opens no socket. It also never triggers `app.main`'s lifespan (startup/shutdown),
    so this fixture never opens the real database pool — tests that need `GET /health` to
    see a working or failing database override `app.api.deps.get_db_pool` instead.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


def _refuse_real_network_access(self: object, request: httpx.Request) -> NoReturn:
    """Raise instead of letting a request reach a real socket.

    Installed onto `httpx.AsyncHTTPTransport.handle_async_request` by the autouse fixture
    below, in place of the real method. Signature is `(self, request)` — not just
    `(request)` — because `monkeypatch.setattr` assigns this as an ordinary function
    attribute on the class, and Python's descriptor protocol binds `self` to it exactly as
    it would for the method it replaced.
    """
    raise RuntimeError(
        "A test attempted a real HTTP request through httpx's default network transport. "
        "Every httpx.AsyncClient in this suite must be built with an explicit "
        "transport=httpx.MockTransport(...) (see the JWKS test kit below) instead of "
        "falling back to a real connection."
    )


@pytest.fixture(autouse=True)
def _forbid_real_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn "tests pass without network access" from a hope into a guarantee.

    Patches httpx's REAL transport class, not any code this repository owns, so a test
    that accidentally builds an `httpx.AsyncClient` without a `MockTransport` fails loudly
    with a `RuntimeError` instead of silently succeeding against a live connection — and,
    worse, silently succeeding against a real Supabase project if one happened to be
    reachable from wherever the suite runs.

    Disturbs nothing that already exists: the `client` fixture above builds its
    `AsyncClient` with `ASGITransport`, an unrelated class that calls the FastAPI app
    in-process rather than opening a socket, and asyncpg does not use httpx at all — this
    guard has no surface to touch on either of those paths.
    """
    monkeypatch.setattr(
        httpx.AsyncHTTPTransport, "handle_async_request", _refuse_real_network_access
    )


@pytest.fixture(scope="session")
async def db_pool() -> AsyncIterator[Pool]:
    """A real asyncpg pool against TEST_DATABASE_URL, shared for the whole test session.

    Skips loudly rather than failing when there is nothing to connect to, so this suite
    still runs offline (CLAUDE.md "Commands": `make test` must work without Supabase
    running). The scratch table is created here, outside of any per-test transaction —
    see `db_conn` below for why that matters — and as a real table, not a `TEMP` one:
    `transaction()` (app/infrastructure/db/transaction.py) acquires an arbitrary
    connection from the pool, and a `TEMP` table would only be visible on the connection
    that created it.
    """
    if not TEST_DATABASE_URL:
        pytest.skip(
            "TEST_DATABASE_URL is not set - database-backed tests are skipped. Start the "
            "local Supabase stack with `make dev`, then run `make test` (which exports "
            "TEST_DATABASE_URL for you), or export it yourself before running pytest "
            "directly."
        )

    try:
        pool = await asyncpg.create_pool(dsn=TEST_DATABASE_URL, min_size=1, max_size=5)
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.skip(
            f"TEST_DATABASE_URL is set but the database is unreachable ({type(exc).__name__}). "
            "Start the local Supabase stack with `make dev` and re-run `make test`."
        )
        return  # pytest.skip() always raises; this line only satisfies static analysis.

    await pool.execute(
        f"CREATE TABLE IF NOT EXISTS {_SCRATCH_TABLE} (key text PRIMARY KEY, value text NOT NULL)"
    )

    yield pool

    await pool.execute(f"DROP TABLE IF EXISTS {_SCRATCH_TABLE}")
    await pool.close()


@pytest.fixture(scope="session")
async def queue_pool() -> AsyncIterator[ArqRedis]:
    """A real ARQ Redis pool against TEST_REDIS_URL, shared for the whole test session.

    Skips loudly rather than failing when there is nothing to connect to, exactly like
    `db_pool` above, so `make test` still works offline (CLAUDE.md "Commands"). A real
    Redis and not `fakeredis` for the same reason ci-backend.yml stands up a real
    `postgres:16`: what these tests are verifying is that arq's own serialization,
    sorted-set queue, and job keys behave as expected, and a reimplementation of Redis
    cannot answer that question about arq.

    `conn_retries = 0` overrides arq's default of five one-second retries. Those are right
    in production, where Redis may be a moment behind a deploy, and wrong here, where they
    would add five seconds of dead time to every offline test run before the skip.

    **The database is flushed on the way in and on the way out.** See TEST_REDIS_URL for
    why that is safe and what it must never be pointed at. Flushing on entry as well as
    exit means a previous run killed with Ctrl-C cannot leave keys that make this one pass
    or fail for the wrong reason.
    """
    redis_settings = redis_settings_from_url(TEST_REDIS_URL)
    redis_settings.conn_retries = 0

    try:
        pool = await create_pool(redis_settings, default_queue_name=TEST_QUEUE_NAME)
    except (OSError, RedisError) as exc:
        pytest.skip(
            f"TEST_REDIS_URL is set but Redis is unreachable ({type(exc).__name__}). "
            "Start it with `docker compose up -d redis` (or `make dev`) and re-run "
            "`make test`."
        )
        return  # pytest.skip() always raises; this line only satisfies static analysis.

    await pool.flushdb()

    yield pool

    await pool.flushdb()
    await pool.aclose(close_connection_pool=True)


@pytest.fixture
async def db_conn(db_pool: Pool) -> AsyncIterator[Connection]:
    """A connection inside its own transaction, always rolled back after the test.

    The per-test isolation mechanism for future feature tests: whatever a test writes
    through this connection disappears when the test ends, pass or fail.

    Independent of `transaction()` (app/infrastructure/db/transaction.py): that helper
    acquires its own connection from `db_pool`, so calling it from inside a test that also
    uses `db_conn` does not nest inside this fixture's rollback — they are two separate
    connections. That is why tests/test_transaction.py clean up their own rows rather than
    relying on this fixture.
    """
    async with db_pool.acquire() as conn:
        tx = conn.transaction()
        await tx.start()
        try:
            yield conn
        finally:
            await tx.rollback()


# -----------------------------------------------------------------------------------------
# JWKS test kit, used by tests/test_jwks.py and tests/test_auth_dependencies.py.
#
# NO KEY MATERIAL IS EVER COMMITTED (ARCHITECTURE.md §9.1) — every keypair below is
# generated fresh, in this process, on every test run. Nothing here is written to disk.
# -----------------------------------------------------------------------------------------

# An obviously-fake UUID, not a real Supabase user id, used as the default `sub` claim.
_TEST_SUB = "5b3d1c2e-6a4f-4b8e-9c2a-1f7e4d6a9b3c"


@dataclass
class _SigningKey:
    """A throwaway keypair plus the JWKS entry describing its public half."""

    kid: str
    algorithm: str
    private_key: ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey
    jwk: dict[str, Any]

    def sign(self, claims: dict[str, Any], *, headers: dict[str, Any] | None = None) -> str:
        """Sign `claims` with this key. `kid` is always set; `headers` can override it.

        Signs with the private key OBJECT directly (PyJWT accepts it — no PEM round trip
        needed), which is why `private_key` above is the cryptography key type rather than
        PEM bytes.
        """
        merged_headers = {"kid": self.kid, **(headers or {})}
        return jwt.encode(
            claims, self.private_key, algorithm=self.algorithm, headers=merged_headers
        )


def _generate_es256_key(kid: str) -> _SigningKey:
    """Generate a fresh EC P-256 keypair.

    Shaped like what the local Supabase stack actually publishes: a real sign-in against
    it (measured) returns a token signed ES256 over a single EC P-256 JWKS key.
    """
    private_key = ec.generate_private_key(ec.SECP256R1())
    jwk = ECAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return _SigningKey(kid=kid, algorithm="ES256", private_key=private_key, jwk=jwk)


def _generate_rs256_key(kid: str) -> _SigningKey:
    """Generate a fresh RSA keypair — Supabase's other supported asymmetric algorithm."""
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return _SigningKey(kid=kid, algorithm="RS256", private_key=private_key, jwk=jwk)


def user_claims(
    sub: str = _TEST_SUB,
    *,
    aud: str = "authenticated",
    expires_in: float = 3600.0,
    **overrides: Any,
) -> dict[str, Any]:
    """Build claims shaped like a real Supabase access token (measured against the local
    stack): `sub`, `aud`, `iss`, `role`, `iat`, `exp`.

    `expires_in` is seconds from now added to `exp`; a negative value builds an
    already-expired token. `**overrides` replace or add claims after the defaults are
    built — e.g. `user_claims(sub="")` for the "no usable sub" case, or `user_claims(aud=
    "service_role")` for the service-role-shaped-token case.
    """
    now = time.time()
    claims: dict[str, Any] = {
        "sub": sub,
        "aud": aud,
        "iss": "http://127.0.0.1:54321/auth/v1",
        "role": "authenticated",
        "iat": int(now),
        "exp": int(now + expires_in),
    }
    claims.update(overrides)
    return claims


@pytest.fixture(scope="session")
def es256_key() -> _SigningKey:
    return _generate_es256_key("kid-es256-primary")


@pytest.fixture(scope="session")
def rs256_key() -> _SigningKey:
    return _generate_rs256_key("kid-rs256-secondary")


@pytest.fixture(scope="session")
def rotated_key() -> _SigningKey:
    """A key absent from the initial JWKS document — simulates a Supabase key rotation."""
    return _generate_es256_key("kid-rotated")


@pytest.fixture(scope="session")
def foreign_key() -> _SigningKey:
    """Same `kid` as `es256_key`, a DIFFERENT private key, and never published.

    A token signed with this key and presented under `es256_key`'s `kid` is "signed by a
    different key," done properly: the `kid` resolves to a real, cached key (so no refetch
    should happen), but the signature must still fail verification because the key
    material does not match.
    """
    return _generate_es256_key("kid-es256-primary")


class _JwksServer:
    """A fake JWKS endpoint behind a REAL `httpx.AsyncClient` — the measurement instrument.

    `request_count` increments on the first line of `handle()`, the callable
    `httpx.MockTransport` invokes. That happens INSIDE the real `httpx.AsyncClient`
    request pipeline — after the client has built the `httpx.Request` from a URL, and
    before it parses the `httpx.Response` — so it counts genuine HTTP requests while URL
    construction, status handling, and response parsing all still execute for real. That is
    what lets this ticket's two "must be measured, not inferred" acceptance criteria (no
    I/O on a cache hit; at most one refetch per rate-limit window) be measured at the HTTP
    layer, rather than at a seam this codebase owns which could be faked out.

    `handle()` is async and suspends with `await asyncio.sleep(0)` on purpose. Without a
    genuine suspension point inside the fetch, an `asyncio.gather` burst of concurrent
    lookups would run to completion one coroutine at a time on a single-threaded event
    loop, and the concurrency tests — which exist to prove the cache's lock actually
    serializes concurrent refetches — would pass for a reason unrelated to the lock.
    """

    # A distinct sentinel for "the test hasn't overridden the body" — rather than `None` —
    # because a malformed-document test needs to send a literal JSON `null` as the body,
    # and `None` doubling as both "unset" and "the JSON value null" would make that case
    # impossible to express.
    _UNSET_BODY: Any = object()

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self.keys = keys
        self.status_code = 200
        self.body: Any = self._UNSET_BODY  # _UNSET_BODY => serve {"keys": self.keys}
        self.failure: Exception | None = None
        self.request_count = 0
        self.requested_urls: list[str] = []
        self.transport = httpx.MockTransport(self.handle)
        self.client = httpx.AsyncClient(transport=self.transport, timeout=httpx.Timeout(5.0))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.request_count += 1
        self.requested_urls.append(str(request.url))
        await asyncio.sleep(0)  # see the class docstring — required for gather() to matter
        if self.failure is not None:
            raise self.failure
        body = {"keys": self.keys} if self.body is self._UNSET_BODY else self.body
        return httpx.Response(self.status_code, json=body)


class _FakeClock:
    """A stand-in for `time.monotonic()` that only moves when a test tells it to.

    Injected into `JwksCache(clock=...)` so the rate-limit tests can cross the 60-second
    window instantly with `advance()` instead of sleeping for real.
    """

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
async def jwks_server() -> AsyncIterator[_JwksServer]:
    """A fresh `_JwksServer` with an empty key document; tests populate `.keys` themselves."""
    server = _JwksServer(keys=[])
    yield server
    await server.client.aclose()


@pytest.fixture
def fake_clock() -> _FakeClock:
    return _FakeClock()


@pytest.fixture
async def jwks_cache(
    jwks_server: _JwksServer,
    fake_clock: _FakeClock,
    es256_key: _SigningKey,
    rs256_key: _SigningKey,
) -> JwksCache:
    """A `JwksCache` wired to the fake JWKS endpoint, primed exactly like startup primes it.

    The document behind `jwks_server` starts with BOTH `es256_key` and `rs256_key`
    published, so `tests/test_auth_dependencies.py` can verify a token signed with either
    algorithm without triggering a refetch. Priming with `await cache.refresh()` — rather
    than going through `open_jwks_cache()` — reproduces the startup semantics exactly:
    `request_count` is `1` afterward, and the rate-limit window is still CLOSED, because
    `refresh()` deliberately never touches `_last_refetch_at` (see `jwks.py`). A test that
    forgets that and immediately looks up an unknown kid correctly observes a real,
    un-rate-limited refetch rather than a suppressed one — matching what a freshly-booted
    process would do.
    """
    jwks_server.keys = [es256_key.jwk, rs256_key.jwk]
    cache = JwksCache(
        jwks_server.client,
        "https://test-project.supabase.co/auth/v1/.well-known/jwks.json",
        clock=fake_clock,
    )
    await cache.refresh()
    return cache


# -----------------------------------------------------------------------------------------
# Signed-in API clients, for feature suites that drive the real application.
#
# Two users, because this project's authorization contract is only half-testable with one
# (ARCHITECTURE.md §4): "any signed-in user can read everything" and "only the owner may
# write" are both statements about a SECOND user, and a suite that only ever signs in as the
# owner passes identically whether reads are unscoped or scoped to the caller.
#
# These fixtures deliberately drive `app.main.app` — the shipped application, with its real
# routers, its real dependency graph, and its real exception handling — rather than a
# throwaway FastAPI instance. `tests/test_auth_dependencies.py` builds a throwaway app
# instead, and correctly so: it tests two dependencies in isolation. A feature suite is
# testing the endpoints as deployed, so it uses the real thing.
# -----------------------------------------------------------------------------------------

# Obviously-fake, deterministic user ids — not real Supabase subjects. Deterministic rather
# than random so that `_delete_test_user_rows` below can clean up after a run that crashed
# before its teardown, and so a failure message names the same id every time.
TEST_USER_A_ID: Final = UUID("aaaaaaaa-0000-4000-8000-000000000001")
TEST_USER_B_ID: Final = UUID("bbbbbbbb-0000-4000-8000-000000000002")

_TEST_USER_IDS: Final = (TEST_USER_A_ID, TEST_USER_B_ID)


def _bearer_headers(key: _SigningKey, user_id: UUID) -> dict[str, str]:
    """Sign a token for `user_id` with a key the `jwks_cache` fixture publishes.

    A real, signed, verifiable JWT rather than a stubbed-out `get_current_user_id`. That
    costs nothing here — the keypair already exists for the auth suite — and it means the
    endpoints under test are reached through the actual verification path, so a route that
    forgot its authentication dependency cannot pass by being handed a user id anyway.
    """
    return {"Authorization": f"Bearer {key.sign(user_claims(sub=str(user_id)))}"}


@pytest.fixture
async def websites_db(db_pool: Pool) -> AsyncIterator[Pool]:
    """The test database, with this suite's rows removed before and after each test.

    **Deletes only rows owned by `TEST_USER_A_ID` / `TEST_USER_B_ID`, never `TRUNCATE`.**
    `TEST_DATABASE_URL` may point at a developer's local Supabase database (that is what
    `make test` wires up), which can hold websites they added by hand while working on the
    frontend. A suite that truncated the table would delete them, be entirely green about
    it, and only ever do it on a laptop — never in CI, where someone would notice.

    `schedules` and `runs` need no separate cleanup: both cascade from `websites`
    (db/migrations/20260805092204_init/migration.sql).

    Skips rather than errors when the migrations have not been applied, matching how
    `db_pool` handles an absent database — "the schema is not there" is a setup problem with
    a known fix, not a test failure.
    """
    if await db_pool.fetchval("SELECT to_regclass('public.websites')") is None:
        pytest.skip(
            "The `websites` table does not exist in TEST_DATABASE_URL. Apply the migrations "
            "with `make migrate-apply` (or `make reset`) and re-run."
        )

    await _delete_test_user_rows(db_pool)
    yield db_pool
    await _delete_test_user_rows(db_pool)


async def _delete_test_user_rows(pool: Pool) -> None:
    """Remove every website owned by this suite's two fake users, and their cascades."""
    await pool.execute("DELETE FROM websites WHERE user_id = ANY($1::uuid[])", list(_TEST_USER_IDS))


@pytest.fixture
def api_app(websites_db: Pool, jwks_cache: JwksCache) -> Iterator[FastAPI]:
    """`app.main.app`, wired to the test database and the fake JWKS document.

    Two dependencies are overridden, and both are required even for a request that sends no
    credentials at all:

    * `get_db_pool`, because the `client` fixture's `ASGITransport` never runs the lifespan,
      so the process-wide pool was never opened.
    * `get_jwks_cache`, because FastAPI resolves *every* sub-dependency before calling the
      dependency that needs them. `get_current_user_id`'s "no credentials -> 401" check runs
      only after its `cache` parameter has been solved, so an un-overridden
      `get_jwks_cache()` raises `RuntimeError` first and the unauthenticated tests would
      error out instead of observing a 401.

    Teardown restores exactly the two keys this fixture set, rather than calling
    `dependency_overrides.clear()`, which would silently discard overrides belonging to an
    enclosing fixture or another test module.
    """
    overrides: dict[Callable[..., Any], Callable[..., Any]] = {
        get_db_pool: lambda: websites_db,
        get_jwks_cache: lambda: jwks_cache,
    }
    sentinel = object()
    previous = {key: app.dependency_overrides.get(key, sentinel) for key in overrides}
    app.dependency_overrides.update(overrides)
    try:
        yield app
    finally:
        for key, value in previous.items():
            if value is sentinel:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value


def _api_client(api_app: FastAPI, headers: dict[str, str]) -> AsyncClient:
    """An httpx client bound to the app in-process, carrying `headers` on every request."""
    return AsyncClient(
        transport=ASGITransport(app=api_app), base_url="http://test", headers=headers
    )


@pytest.fixture
async def unauthenticated_client(api_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """A client that sends no `Authorization` header. Every endpoint must answer 401."""
    async with _api_client(api_app, {}) as ac:
        yield ac


@pytest.fixture
async def user_client(api_app: FastAPI, es256_key: _SigningKey) -> AsyncIterator[AsyncClient]:
    """Signed in as `TEST_USER_A_ID` — the owner in most tests."""
    async with _api_client(api_app, _bearer_headers(es256_key, TEST_USER_A_ID)) as ac:
        yield ac


@pytest.fixture
async def second_user_client(
    api_app: FastAPI, es256_key: _SigningKey
) -> AsyncIterator[AsyncClient]:
    """Signed in as `TEST_USER_B_ID` — a different, equally valid user.

    The fixture that makes the authorization contract testable, and the reason it lives in
    `conftest.py` rather than in one feature's test module: every endpoint added after this
    one has a "someone else tries to write it" case, and they should all express it the same
    way. Use it for the `403` tests, and — just as importantly — for the "a non-owner CAN
    read this" tests, which are the ones that catch a well-meaning `WHERE user_id = $1`.
    """
    async with _api_client(api_app, _bearer_headers(es256_key, TEST_USER_B_ID)) as ac:
        yield ac


# -----------------------------------------------------------------------------------------
# Seed helpers, shared by every feature suite that writes fixture rows directly against the
# test database rather than through the API (see `tests/test_websites_api.py`'s module
# docstring for why: a test that builds its own fixtures by calling `POST /websites` cannot
# tell "the list query is wrong" apart from "the create endpoint is wrong").
#
# Promoted out of `tests/test_websites_api.py` (PER-155) once a second suite,
# `tests/test_runs_api.py`, needed the same fixtures. Deliberately NOT promoted alongside
# them: each suite's own `_NOW` time base, which every seeded timestamp below is relative
# to — sharing one `_NOW` across suites would make a run seeded by one suite sort relative
# to a website seeded by the other in a way neither test file controls.
# -----------------------------------------------------------------------------------------


async def seed_website(pool: Pool, user_id: UUID, origin: str, url: str | None = None) -> UUID:
    """Insert a website directly, bypassing the API. See the section docstring above."""
    website_id: UUID = await pool.fetchval(
        "INSERT INTO websites (user_id, url, origin) VALUES ($1, $2, $3) RETURNING id",
        user_id,
        url or f"{origin}/",
        origin,
    )
    return website_id


async def seed_run(
    pool: Pool,
    website_id: UUID,
    *,
    started_at: datetime,
    status: str = "completed",
    trigger: str = "manual",
    completed_at: datetime | None = None,
    stats: dict[str, Any] | str | None = None,
    llms_txt: str | None = None,
    storage_path: str | None = None,
    error: str | None = None,
) -> UUID:
    """Insert a run directly.

    `stats` is passed as a JSON *string* cast to jsonb: asyncpg encodes and decodes
    `json`/`jsonb` as `str` and will not accept a Python dict for a jsonb parameter without a
    custom type codec. It also accepts a raw string, so a test can seed deliberately
    malformed stats.

    `"trigger"` is quoted because it is a SQL keyword, matching the generated migration.
    `llms_txt`, `storage_path`, and `error` default to `None`, matching a run that has not
    (yet, or ever) recorded them; `tests/test_runs_api.py` passes them explicitly to
    exercise `RunDetailResponse`.
    """
    encoded = json.dumps(stats) if isinstance(stats, dict) else stats
    run_id: UUID = await pool.fetchval(
        """
        INSERT INTO runs
            (website_id, "trigger", status, started_at, completed_at, stats,
             llms_txt, storage_path, error)
        VALUES ($1, $2::run_trigger, $3::run_status, $4, $5, $6::jsonb, $7, $8, $9)
        RETURNING id
        """,
        website_id,
        trigger,
        status,
        started_at,
        completed_at,
        encoded,
        llms_txt,
        storage_path,
        error,
    )
    return run_id


async def seed_schedule(
    pool: Pool,
    website_id: UUID,
    *,
    active: bool = True,
    interval_minutes: int = 360,
    next_run_at: datetime | None = None,
) -> UUID:
    schedule_id: UUID = await pool.fetchval(
        """
        INSERT INTO schedules (website_id, active, interval_minutes, next_run_at)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        website_id,
        active,
        interval_minutes,
        next_run_at,
    )
    return schedule_id


def parse(timestamp: str) -> datetime:
    """Parse a timestamp out of a JSON response, for comparison against what was seeded.

    Every column involved is `timestamptz` with microsecond precision, so this round trip is
    exact — an assertion may compare for equality rather than for approximate closeness.
    """
    return datetime.fromisoformat(timestamp)
