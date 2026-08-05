"""Shared pytest fixtures.

Real database and Redis fixtures land with the infrastructure tickets. Today the only
fixture is an in-process HTTP client, which is all a shallow `/health` needs.
"""

import os
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient


# `app.core.settings` validates its configuration at import time (by design — see that
# module), so the required variables must exist before `app.main` is imported below.
# These are obvious non-values: never put a real credential in this file, and note that
# none of them is ever dialled, because nothing under test opens a connection.
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/llms_text_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("SUPABASE_URL", "https://test-project.supabase.co")
os.environ.setdefault("SUPABASE_SECRET_KEY", "not-a-real-key")

from app.main import app  # noqa: E402  — must follow the defaults set above


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app in-process.

    ASGITransport calls the application directly, so the suite needs no running server
    and opens no socket.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
