"""Shared pytest fixtures.

Two families of fixtures live here: an in-process HTTP client, which is all a shallow
`/health` needs, and — for tests that exercise real Postgres, like
`tests/test_transaction.py` — a session-scoped pool and a function-scoped, always-rolled-
-back connection.
"""

import os
from collections.abc import AsyncIterator

import asyncpg
import pytest
from asyncpg import Connection, Pool
from httpx import ASGITransport, AsyncClient


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
