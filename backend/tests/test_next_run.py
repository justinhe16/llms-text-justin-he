"""Tests for app.features.schedules.internals.next_run.compute_next_run_at.

Pure-function tests, no database and no HTTP — in the spirit of
`tests/test_url_normalize.py`: this module is the one place "what should `next_run_at` become"
is decided, and it deserves the same exhaustive, no-tolerance treatment url_normalize.py gets.

Every assertion below compares against `_NOW` with `==`, never `pytest.approx` or a delta
tolerance — a fixed clock passed as `now=` makes every output exact, which is the entire point
of `compute_next_run_at` taking `now` as a required keyword argument rather than calling
`datetime.now()` itself.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.features.schedules.internals.next_run import (
    CurrentSchedule,
    RequestedSchedule,
    compute_next_run_at,
)


_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


# -----------------------------------------------------------------------------------------
# The six-row truth table, one test per row.
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "current",
    [
        pytest.param(None, id="no-existing-schedule"),
        pytest.param(
            CurrentSchedule(active=True, interval_minutes=60, next_run_at=_NOW),
            id="was-active",
        ),
        pytest.param(
            CurrentSchedule(active=False, interval_minutes=60, next_run_at=None),
            id="was-inactive",
        ),
    ],
)
def test_row_1_deactivating_always_clears_next_run_at(current: CurrentSchedule | None) -> None:
    """Row 1: `new_state.active is False` -> `None`, regardless of the prior state."""
    result = compute_next_run_at(
        current, RequestedSchedule(active=False, interval_minutes=360), now=_NOW
    )
    assert result is None


def test_row_2_activating_with_no_prior_schedule_schedules_from_now() -> None:
    """Row 2: `current is None`, activating -> `now + new interval`."""
    result = compute_next_run_at(
        None, RequestedSchedule(active=True, interval_minutes=60), now=_NOW
    )
    assert result == _NOW + timedelta(minutes=60)


def test_row_3_reactivating_a_previously_inactive_schedule_schedules_from_now() -> None:
    """Row 3: `current.active is False`, activating -> `now + new interval`.

    The stale `next_run_at` on the inactive row (`_NOW - timedelta(days=30)`, deliberately in
    the past) must be ignored, not reused — reactivating is a transition, not a no-op.
    """
    current = CurrentSchedule(
        active=False, interval_minutes=360, next_run_at=_NOW - timedelta(days=30)
    )
    result = compute_next_run_at(
        current, RequestedSchedule(active=True, interval_minutes=360), now=_NOW
    )
    assert result == _NOW + timedelta(minutes=360)


def test_row_4_changing_the_interval_while_active_reschedules_from_now() -> None:
    """Row 4: active -> active, but the interval changed -> `now + new interval`.

    This is the "does not cause an immediate fire" acceptance criterion in miniature: even
    though the schedule was already active and due far in the future, a changed interval
    recomputes from `now`, not from the stale `next_run_at`.
    """
    current = CurrentSchedule(
        active=True, interval_minutes=10080, next_run_at=_NOW + timedelta(days=6)
    )
    result = compute_next_run_at(
        current, RequestedSchedule(active=True, interval_minutes=60), now=_NOW
    )
    assert result == _NOW + timedelta(minutes=60)


def test_row_5_a_true_no_op_keeps_the_existing_next_run_at() -> None:
    """Row 5 — the starvation guard. Same `active`, same interval, `next_run_at` already set:

    the existing value is kept byte-for-byte, not recomputed. A client that re-`PUT`s the same
    body on every page load must not be able to push the schedule further out with each call.
    """
    existing_next_run_at = _NOW + timedelta(hours=3, minutes=17, seconds=42)
    current = CurrentSchedule(active=True, interval_minutes=360, next_run_at=existing_next_run_at)

    result = compute_next_run_at(
        current, RequestedSchedule(active=True, interval_minutes=360), now=_NOW
    )

    assert result == existing_next_run_at
    # Not merely equal in value — the exact object that was already there, proving nothing
    # recomputed it.
    assert result is existing_next_run_at


def test_row_6_an_active_schedule_with_a_null_next_run_at_is_repaired() -> None:
    """Row 6 — the repair case. `active=True` with `next_run_at=None` is a row the cron
    tick's `WHERE active AND next_run_at <= now()` can never select; encountering it heals it
    with a real value instead of preserving the inconsistency.
    """
    current = CurrentSchedule(active=True, interval_minutes=1440, next_run_at=None)

    result = compute_next_run_at(
        current, RequestedSchedule(active=True, interval_minutes=1440), now=_NOW
    )

    assert result == _NOW + timedelta(minutes=1440)


# -----------------------------------------------------------------------------------------
# Additional coverage beyond the six rows.
# -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "interval_minutes",
    [
        pytest.param(60, id="hourly"),
        pytest.param(360, id="6-hourly"),
        pytest.param(1440, id="daily"),
        pytest.param(10080, id="weekly"),
    ],
)
def test_every_preset_produces_exactly_the_right_delta(interval_minutes: int) -> None:
    """Each of the four supported presets yields exactly `now + that many minutes` — no
    off-by-one on the delta, and no rounding.
    """
    result = compute_next_run_at(
        None, RequestedSchedule(active=True, interval_minutes=interval_minutes), now=_NOW
    )
    assert result == _NOW + timedelta(minutes=interval_minutes)


def test_deactivating_an_already_inactive_schedule_is_none() -> None:
    current = CurrentSchedule(active=False, interval_minutes=60, next_run_at=None)
    result = compute_next_run_at(
        current, RequestedSchedule(active=False, interval_minutes=1440), now=_NOW
    )
    assert result is None


def test_inactive_to_inactive_with_a_changed_interval_is_still_none() -> None:
    """Changing the interval is irrelevant if the schedule is not, and will not be, active."""
    current = CurrentSchedule(active=False, interval_minutes=60, next_run_at=None)
    result = compute_next_run_at(
        current, RequestedSchedule(active=False, interval_minutes=10080), now=_NOW
    )
    assert result is None


def test_weekly_to_hourly_does_not_cause_an_immediate_fire() -> None:
    """The acceptance criterion, spelled out literally: switching a weekly schedule to hourly
    must schedule the NEXT run strictly in the future, never `now` itself or earlier — a
    schedule change must not make a run fire immediately.
    """
    current = CurrentSchedule(active=True, interval_minutes=10080, next_run_at=_NOW)
    result = compute_next_run_at(
        current, RequestedSchedule(active=True, interval_minutes=60), now=_NOW
    )

    assert result is not None
    assert result > _NOW
    assert result == _NOW + timedelta(minutes=60)


def test_the_function_does_not_mutate_its_inputs() -> None:
    current = CurrentSchedule(active=True, interval_minutes=360, next_run_at=_NOW)
    new_state = RequestedSchedule(active=True, interval_minutes=60)

    compute_next_run_at(current, new_state, now=_NOW)

    # Frozen dataclasses already make mutation impossible, but the identity/value checks
    # below are what actually prove nothing was replaced or copied-and-changed in place.
    assert current == CurrentSchedule(active=True, interval_minutes=360, next_run_at=_NOW)
    assert new_state == RequestedSchedule(active=True, interval_minutes=60)


def test_the_result_is_timezone_aware_utc() -> None:
    result = compute_next_run_at(
        None, RequestedSchedule(active=True, interval_minutes=60), now=_NOW
    )
    assert result is not None
    assert result.tzinfo is not None
    assert result.utcoffset() == timedelta(0)
