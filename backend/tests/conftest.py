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
# These are obvious non-values: never put a real credential in this file.
#
# Assigned unconditionally rather than with setdefault(), so that a developer's exported
# variables and their backend/.env cannot reach the suite. Nothing under test opens a
# connection today, but the moment real database and Redis fixtures land, an ambient
# DATABASE_URL leaking in would point the tests at real infrastructure. The suite decides
# what it connects to; the surrounding environment does not.
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = "postgresql://localhost:5432/llms_text_test"
os.environ["REDIS_URL"] = "redis://localhost:6379/0"
os.environ["SUPABASE_URL"] = "https://test-project.supabase.co"
os.environ["SUPABASE_SECRET_KEY"] = "not-a-real-key"

from app.main import app  # noqa: E402  — must follow the assignments above


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the app in-process.

    ASGITransport calls the application directly, so the suite needs no running server
    and opens no socket.
    """
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
