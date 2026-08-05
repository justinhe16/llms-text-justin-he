"""Tests for settings validation.

These construct `Settings` with `_env_file=None` so the developer's own `backend/.env`
cannot influence the result.
"""

import pytest

from app.core.settings import Settings


REQUIRED_VARIABLES = ("DATABASE_URL", "REDIS_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY")


def _settings(**overrides: str) -> Settings:
    """Build Settings from explicit values only, ignoring the ambient environment."""
    values: dict[str, str] = {
        "database_url": "postgresql://localhost:5432/llms_text",
        "redis_url": "redis://localhost:6379/0",
        "supabase_url": "https://example.supabase.co",
        "supabase_secret_key": "placeholder",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_fully_configured_settings_validate() -> None:
    _settings().validate_required_secrets()


def test_every_missing_variable_is_named_in_one_error() -> None:
    """One boot, one error, every missing name — not one restart per missing variable."""
    blank = _settings(database_url="", redis_url="", supabase_url="", supabase_secret_key="")

    with pytest.raises(RuntimeError) as exc_info:
        blank.validate_required_secrets()

    message = str(exc_info.value)
    for name in REQUIRED_VARIABLES:
        assert name in message


@pytest.mark.parametrize(
    ("field", "variable"),
    [
        ("database_url", "DATABASE_URL"),
        ("redis_url", "REDIS_URL"),
        ("supabase_url", "SUPABASE_URL"),
        ("supabase_secret_key", "SUPABASE_SECRET_KEY"),
    ],
)
def test_only_the_missing_variable_is_named(field: str, variable: str) -> None:
    with pytest.raises(RuntimeError) as exc_info:
        _settings(**{field: ""}).validate_required_secrets()

    message = str(exc_info.value)
    assert variable in message
    for other in REQUIRED_VARIABLES:
        if other != variable:
            assert other not in message


def test_whitespace_only_value_counts_as_missing() -> None:
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        _settings(database_url="   ").validate_required_secrets()


def test_error_never_leaks_a_configured_value() -> None:
    """The message names variables, never values (ARCHITECTURE.md §9.4)."""
    sentinel = "super-secret-sentinel-value"
    partly_configured = _settings(supabase_secret_key=sentinel, database_url="")

    with pytest.raises(RuntimeError) as exc_info:
        partly_configured.validate_required_secrets()

    assert sentinel not in str(exc_info.value)


def test_unknown_environment_is_rejected() -> None:
    """A typo like ENVIRONMENT=prod fails at construction, not at the first branch."""
    with pytest.raises(ValueError):
        _settings(environment="prod")
