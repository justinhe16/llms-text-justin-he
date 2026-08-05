"""Tests for the asyncpg pool factory and its transaction-pooler (port 6543) guard.

None of these need a real database: `asyncpg.create_pool` is monkeypatched so the guard's
behavior — which kwargs it passes, whether it logs — can be asserted directly, without a
live server.
"""

import logging
from typing import Any

import asyncpg
import pytest

from app.core.settings import Settings
from app.infrastructure.db import pool as pool_module


def _settings(database_url: str) -> Settings:
    """Build Settings with an explicit DATABASE_URL, ignoring the ambient environment."""
    return Settings(
        _env_file=None,
        database_url=database_url,
        redis_url="redis://localhost:6379/0",
        supabase_url="https://example.supabase.co",
        supabase_secret_key="placeholder",
    )


class _FakePool:
    """Stands in for the object `asyncpg.create_pool` would return — never a live pool."""


def _patch_create_pool(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Monkeypatch asyncpg.create_pool and return the dict its kwargs land in."""
    captured: dict[str, Any] = {}

    async def fake_create_pool(**kwargs: Any) -> _FakePool:
        captured.update(kwargs)
        return _FakePool()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    return captured


async def test_transaction_pooler_url_disables_statement_cache_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A :6543 URL gets `statement_cache_size=0` and a warning, with no leaked credentials."""
    captured = _patch_create_pool(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await pool_module.create_pool(
            _settings("postgresql://app_user:s3cret-pw@db.example.supabase.co:6543/postgres")
        )

    assert captured["statement_cache_size"] == 0

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "6543" in message
    # The message is static and must never carry the URL it was derived from.
    assert "s3cret-pw" not in message
    assert "db.example.supabase.co" not in message
    assert "postgresql://" not in message


async def test_session_pooler_url_leaves_statement_cache_untouched(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A :5432 URL is left alone: no forced statement_cache_size, no warning."""
    captured = _patch_create_pool(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await pool_module.create_pool(
            _settings("postgresql://app_user:s3cret-pw@db.example.supabase.co:5432/postgres")
        )

    assert "statement_cache_size" not in captured
    assert caplog.records == []


@pytest.mark.parametrize(
    "database_url",
    [
        "not-a-valid-url-at-all: : :",
        "postgresql://app_user:pw@host:not-a-port/postgres",
    ],
)
async def test_malformed_url_falls_back_to_the_safe_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    database_url: str,
) -> None:
    """A URL urlsplit cannot parse a port from is treated as session-mode, not guessed at."""
    captured = _patch_create_pool(monkeypatch)

    with caplog.at_level(logging.WARNING):
        await pool_module.create_pool(_settings(database_url))

    assert "statement_cache_size" not in captured
    assert caplog.records == []


def test_get_pool_before_open_pool_raises_a_clear_error() -> None:
    """get_pool() must fail loudly, not with an AttributeError on None deep in a caller."""
    pool_module._pool = None  # in case an earlier test in this session opened one

    with pytest.raises(RuntimeError, match="open_pool"):
        pool_module.get_pool()


async def test_open_pool_twice_raises_instead_of_leaking_the_first_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second open_pool() must fail loudly rather than silently orphan the first pool.

    Overwriting the singleton would leave the first pool's connections checked out against
    Postgres for the life of the process, with nothing referencing them to close. The
    realistic way that happens is a worker entrypoint copy-pasted from app.main.
    """
    pool_module._pool = None

    async def fake_create_pool(**_: object) -> object:
        return object()

    monkeypatch.setattr("asyncpg.create_pool", fake_create_pool)
    settings = _settings("postgresql://app:pw@host:5432/postgres")

    try:
        first = await pool_module.open_pool(settings)

        with pytest.raises(RuntimeError, match="already open"):
            await pool_module.open_pool(settings)

        # The original pool is still the one on offer — it was not swapped out.
        assert pool_module.get_pool() is first
    finally:
        pool_module._pool = None
