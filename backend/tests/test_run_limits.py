"""Tests for `app.features.runs.internals.run_limits`.

Pure-function tests, no database and no HTTP — in the spirit of `tests/test_url_normalize.py`
and `tests/test_next_run.py`: `format_reset_delay` and the two message builders are the one
place the wording of a `429` is decided, and the ticket's own acceptance criterion is that a
`429` NAMES the limit and, where one exists, the reset time. Pinning the exact strings here is
what keeps that a fact a test enforces, rather than a convention a reviewer has to eyeball
three call frames deep in `RunService`.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.features.runs.internals.run_limits import (
    concurrency_limit_message,
    daily_limit_message,
    format_reset_delay,
)


_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


# -----------------------------------------------------------------------------------------
# format_reset_delay — rendering, and the round-DOWN rule
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        pytest.param(timedelta(hours=3, minutes=12), "3h 12m", id="hours-and-minutes"),
        pytest.param(timedelta(minutes=12), "12m", id="minutes-only"),
        pytest.param(timedelta(hours=4), "4h", id="exact-hour-drops-the-m-component"),
        pytest.param(timedelta(0), "less than a minute", id="zero"),
        pytest.param(timedelta(seconds=-5), "less than a minute", id="negative"),
        pytest.param(timedelta(seconds=45), "less than a minute", id="sub-minute"),
        pytest.param(timedelta(hours=23, minutes=59), "23h 59m", id="23h59m-just-under-the-window"),
    ],
)
def test_format_reset_delay_renders_the_expected_string(delta: timedelta, expected: str) -> None:
    assert format_reset_delay(delta) == expected


def test_format_reset_delay_rounds_down_never_up() -> None:
    """3h 12m 59s must render as `"3h 12m"`, not `"3h 13m"`.

    The stated reason (`run_limits.py`'s module docstring) is that a client which waits
    exactly as long as it was told and retries must never land back on a second `429` — an
    early promise is worse than a late one, so seconds are always dropped, never rounded to
    the nearest minute.
    """
    assert format_reset_delay(timedelta(hours=3, minutes=12, seconds=59)) == "3h 12m"


def test_format_reset_delay_rounds_down_to_zero_minutes_not_up_to_one() -> None:
    """59 seconds is still `"less than a minute"`, not `"1m"`."""
    assert format_reset_delay(timedelta(seconds=59)) == "less than a minute"


def test_format_reset_delay_a_large_negative_delta_is_still_less_than_a_minute() -> None:
    """A delta far in the past (the window already reset by the time this renders, under one
    of `RunService`'s documented check-then-act races) is not a crash and is not a bizarre
    negative string — it is the same honest floor as any other already-elapsed delta."""
    assert format_reset_delay(timedelta(hours=-2)) == "less than a minute"


# -----------------------------------------------------------------------------------------
# concurrency_limit_message — no reset time, ever
# -----------------------------------------------------------------------------------------


def test_concurrency_limit_message_names_the_limit_and_has_no_reset_time() -> None:
    assert concurrency_limit_message(2) == (
        "Too many runs in progress (limit: 2). Wait for one to finish, then trigger another."
    )


def test_concurrency_limit_message_reflects_a_different_configured_limit() -> None:
    """Not hardcoded to `2` internally — the number in the message tracks whatever
    `Settings.max_concurrent_runs_per_user` actually is."""
    assert "limit: 5" in concurrency_limit_message(5)


# -----------------------------------------------------------------------------------------
# daily_limit_message — names the limit AND the reset delay
# -----------------------------------------------------------------------------------------


def test_daily_limit_message_names_the_limit_and_the_reset_delay() -> None:
    resets_at = _NOW + timedelta(hours=3, minutes=12)
    assert daily_limit_message(50, resets_at, _NOW) == (
        "Daily limit reached (50 runs in 24 hours). Resets in 3h 12m."
    )


def test_daily_limit_message_delegates_its_delay_rendering_to_format_reset_delay() -> None:
    """A sub-minute reset delay renders the same way here as it does on its own — this
    message is `format_reset_delay` plus a template, not a second implementation of it."""
    resets_at = _NOW + timedelta(seconds=30)
    assert daily_limit_message(50, resets_at, _NOW) == (
        "Daily limit reached (50 runs in 24 hours). Resets in less than a minute."
    )
