"""Tests for settings validation.

These construct `Settings` with `_env_file=None` so the developer's own `backend/.env`
cannot influence the result.
"""

from typing import Any

import pytest

from app.core.settings import Settings


REQUIRED_VARIABLES = ("DATABASE_URL", "REDIS_URL", "SUPABASE_URL", "SUPABASE_SECRET_KEY")


def _settings(**overrides: Any) -> Settings:
    """Build Settings from explicit values only, ignoring the ambient environment.

    Widened from `**overrides: str` to `**overrides: Any` for PER-180's
    `crawl_enrich_with_llm=True/False` overrides — every other field this helper accepts is
    still a `str`, but a `bool` has to flow through the same `**overrides` dict.
    """
    values: dict[str, Any] = {
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


# -----------------------------------------------------------------------------------------
# PER-180: `ANTHROPIC_API_KEY` is required only when `crawl_enrich_with_llm` is on — the
# FIRST conditional entry in `validate_required_secrets`. Every test above this point already
# proves the four unconditional variables validate identically whether the flag is set or
# not, since `_settings()`'s defaults never touch it; these four are about the conditional
# itself.
# -----------------------------------------------------------------------------------------


def test_the_key_is_not_required_while_the_flag_is_off() -> None:
    """The default: `crawl_enrich_with_llm` is `False` and no `anthropic_api_key` was given.
    A correctly configured deployment — or this test suite, or CI — must still validate."""
    _settings(crawl_enrich_with_llm=False).validate_required_secrets()


def test_the_key_is_required_once_the_flag_is_on() -> None:
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY") as exc_info:
        _settings(crawl_enrich_with_llm=True, anthropic_api_key="").validate_required_secrets()

    assert "ANTHROPIC_API_KEY" in str(exc_info.value)


def test_only_the_key_is_named_when_only_it_is_missing() -> None:
    """With the flag on and every other variable configured, the message names
    ANTHROPIC_API_KEY and nothing else — the same "only the missing variable is named"
    property `test_only_the_missing_variable_is_named` pins for the four unconditional ones."""
    with pytest.raises(RuntimeError) as exc_info:
        _settings(crawl_enrich_with_llm=True, anthropic_api_key="").validate_required_secrets()

    message = str(exc_info.value)
    assert "ANTHROPIC_API_KEY" in message
    for other in REQUIRED_VARIABLES:
        assert other not in message


def test_the_key_is_configured_and_the_flag_on_together_validate() -> None:
    _settings(
        crawl_enrich_with_llm=True, anthropic_api_key="a-placeholder-key"
    ).validate_required_secrets()


def test_the_key_error_never_leaks_the_configured_value() -> None:
    """The same guarantee `test_error_never_leaks_a_configured_value` makes for the four
    unconditional variables, made for the conditional one: a sentinel placed in
    `anthropic_api_key` while some OTHER required variable is blank must never appear in the
    message, even though the key itself is present and configured."""
    sentinel = "super-secret-anthropic-sentinel-value"
    partly_configured = _settings(
        crawl_enrich_with_llm=True, anthropic_api_key=sentinel, database_url=""
    )

    with pytest.raises(RuntimeError) as exc_info:
        partly_configured.validate_required_secrets()

    assert sentinel not in str(exc_info.value)
