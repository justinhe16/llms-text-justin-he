"""`compute_next_run_at` and `advance_next_run_at` — the two pure functions that decide what
`next_run_at` becomes, from the two different directions something can change it.

This module lives in `schedules/internals/` for the same reason `url_normalize.py` lives in
`websites/internals/` (ARCHITECTURE.md §3.1): it is feature-owned logic with no I/O and no
state, tested exhaustively on its own, and imported by exactly one service.

**Why this has to be pure.** `PUT /websites/{id}/schedule` is the only write this feature has,
and it is not a plain upsert: whether `next_run_at` should move depends on what the row
already looked like, not just on what the client just sent. A function of `(current,
new_state, now)` — with no database access — is the only way to make every branch of that
decision exhaustively testable without a real transaction, and it is the shape the ticket
asks for directly.

**`compute_next_run_at` owns what a PUT does; `advance_next_run_at` owns what the cron tick
does.** The two functions never call each other and share no code beyond both taking `now` as
a required keyword argument — the same discipline for the same reason. `compute_next_run_at`
decides whether a schedule needs a `next_run_at` at all, and if so, whether to keep the one it
already had (row 5's starvation guard) or recompute one. `advance_next_run_at` is only ever
called on a schedule the tick just locked because it WAS due, so there is no "keep the old
value" branch to speak of — every call unconditionally schedules the next occurrence forward.
Both always schedule from `now`, never from the stale `next_run_at` that made the row due in
the first place, which is what stops a schedule that was missed during a worker outage from
trying to "catch up" with a burst of back-to-back runs the moment the tick resumes — making
that behaviour configurable is explicitly out of scope for this ticket.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random
from typing import Final


@dataclass(frozen=True)
class CurrentSchedule:
    """The persisted state a PUT is transitioning FROM.

    `None` stands for "no schedule row exists yet" — the first `PUT` for a website — which is
    why `compute_next_run_at` takes `CurrentSchedule | None` rather than requiring this to be
    constructed for a case that has no prior state to describe.
    """

    active: bool
    interval_minutes: int
    next_run_at: datetime | None


@dataclass(frozen=True)
class RequestedSchedule:
    """The state a PUT is transitioning TO.

    Deliberately has no `next_run_at` field — that is the value this module derives, not one
    a caller supplies. `UpsertScheduleRequest` (`schemas.py`) does not accept it either, for
    the same reason: a client-supplied `next_run_at` would let a schedule be pushed arbitrarily
    far into the future or into the past, bypassing the interval it claims to run on.
    """

    active: bool
    interval_minutes: int


def compute_next_run_at(
    current: CurrentSchedule | None,
    new_state: RequestedSchedule,
    *,
    now: datetime,
) -> datetime | None:
    """Derive the `next_run_at` a `PUT` should persist, given the row it is replacing.

    `now` is a **required, keyword-only argument with no default.** That is what makes this
    function genuinely pure — every output is determined entirely by its three inputs, with
    no hidden call to `datetime.now()` inside — and therefore exhaustively testable with a
    single fixed clock (`tests/test_next_run.py`). The ticket's own signature,
    `compute_next_run_at(current, new_state)`, is preserved exactly in the positional
    parameters; `now` only adds the clock as an explicit input rather than an ambient one.

    The full decision table, in the order it is implemented:

    | # | current             | new_state.active | extra condition                  | result                      |
    |---|---------------------|-------------------|-----------------------------------|------------------------------|
    | 1 | anything (or `None`)| `False`           | —                                  | `None`                       |
    | 2 | `None`              | `True`            | —                                  | `now + new interval`         |
    | 3 | `not current.active`| `True`            | —                                  | `now + new interval`         |
    | 4 | `current.active`    | `True`            | interval CHANGED                  | `now + new interval`         |
    | 5 | `current.active`    | `True`            | interval same, `next_run_at` set  | `current.next_run_at` (kept) |
    | 6 | `current.active`    | `True`            | interval same, `next_run_at` None | `now + new interval`         |

    Rows 1-4 are the transitions the ticket describes directly: deactivating always clears
    `next_run_at`, and activating — for the first time, after being inactive, or with a new
    interval — schedules the next run one interval from now.

    Rows 5 and 6 are not in the ticket, and they are the two decisions that matter most here:

    * **Row 5 — idempotence.** A `PUT` that changes nothing must NOT push `next_run_at`
      further out. If this function recomputed `now + interval` unconditionally whenever
      `active` is `True`, a client that re-`PUT`s the same body on every page load (a
      perfectly reasonable thing for a settings page to do) would keep resetting the clock
      and the schedule would never become due — it would starve forever behind its own
      "confirmation" requests. "Recompute from `now()`" is the rule for a genuine
      *transition* (rows 2-4), not for a no-op, so a no-op keeps whatever `next_run_at`
      already had.
    * **Row 6 — repair.** `active = true` with `next_run_at IS NULL` is a row the cron tick's
      hot-path query (`WHERE active AND next_run_at <= now()`, ARCHITECTURE.md §6.4) can never
      select — it is stuck active forever with nothing to make it run. That state should not
      be possible by construction, but if a `PUT` ever encounters it (a hand-seeded row, a
      row from before this invariant existed, manual repair gone wrong), the right move is to
      heal it by scheduling a real next run rather than reproducing the inconsistency by
      "preserving" a `None`.

    Args:
        current: The schedule row this `PUT` is replacing, or `None` if the website has no
            schedule yet.
        new_state: The `active` flag and `interval_minutes` the request asked for.
        now: The instant to schedule from. Always compared and returned as the same
            timezone-aware instant it was given — this function performs no timezone
            conversion and assumes its caller already has.

    Returns:
        The `next_run_at` to persist: `None` if `new_state.active` is `False`, otherwise a
        `datetime` that is either `now + timedelta(minutes=new_state.interval_minutes)` or,
        for a genuine no-op (row 5), the unchanged `current.next_run_at`.
    """
    if not new_state.active:
        return None

    if current is None or not current.active:
        return now + timedelta(minutes=new_state.interval_minutes)

    interval_changed = current.interval_minutes != new_state.interval_minutes
    if interval_changed:
        return now + timedelta(minutes=new_state.interval_minutes)

    if current.next_run_at is not None:
        return current.next_run_at

    return now + timedelta(minutes=new_state.interval_minutes)


# How far a jittered `next_run_at` may drift from a plain `now + interval`, as a fraction of
# the interval. 5% of a daily (1440-minute) schedule is about ±72 minutes; 5% of an hourly
# (60-minute) one is about ±3 minutes — enough to break up a cohort of schedules created in
# the same second (a common onboarding flow: several websites added back-to-back, all
# defaulted to the same interval) without meaningfully changing when any one of them runs.
MAX_JITTER_FRACTION: Final = 0.05


def advance_next_run_at(*, now: datetime, interval_minutes: int, rng: Random) -> datetime:
    """Derive the `next_run_at` the cron tick should persist for a schedule that just fired.

    Returns `now + interval + offset`, where `offset` is drawn uniformly from
    `[-MAX_JITTER_FRACTION * interval, +MAX_JITTER_FRACTION * interval]`.

    **Why jitter exists at all.** Without it, every schedule created with the same interval at
    the same moment — the onboarding flow described above, or a batch of websites added in one
    sitting — fires in the same second forever: `advance_next_run_at` would otherwise recompute
    `now + interval` from the SAME `now` (the tick's clock) for every one of them, on every
    tick, and the cohort would never spread out. A one-time ±5% nudge, applied on every advance,
    is enough to break that lockstep permanently without meaningfully changing how often any
    individual schedule runs.

    **Why the offset is computed in Python, and never `random()` in SQL.** The bound
    (`MAX_JITTER_FRACTION`) is visible to a reviewer of this function rather than buried in a
    query string, and the drawn value travels to `SchedulesWriter` as an ordinary bind
    parameter — a plain `timestamptz` the writer never has to derive — which is what makes a
    single tick's outcome deterministic and testable end to end: seed `rng` and every value
    `advance_next_run_at` will produce for that tick is reproducible, which a SQL-side
    `random()` could never be.

    **How this relates to `compute_next_run_at`.** That function owns what a `PUT` does to
    `next_run_at` — including, in its row 5, choosing NOT to move it at all. This function owns
    what the cron tick does after a schedule fires, and it has no such branch: it is only ever
    called on a schedule the tick just selected because it WAS due, so there is nothing to
    preserve. Both always schedule forward from `now`, never from the stale `next_run_at` that
    made the row due in the first place — which is what stops a schedule that was missed during
    downtime from trying to catch up with a burst of runs the moment the tick resumes. Making
    "catch up" configurable is explicitly out of scope for this ticket.

    **Why no clamp is needed.** `ScheduleIntervalMinutes` (`schedules/schemas.py`) rejects
    anything below 60 minutes at the API boundary, so the smallest interval this function ever
    sees already tolerates a jitter window many times wider than the tick's own 60-second
    cadence — the jittered result is always comfortably in the future, and there is no need to
    clamp it away from `now`.

    Args:
        now: The one clock the whole tick runs off, captured once in the service — see
            `ScheduleService.run_due_schedules` for why every write in a tick shares it.
        interval_minutes: The schedule's own interval. `int`, matching the `schedules` column
            and `CurrentSchedule.interval_minutes`, not `ScheduleIntervalMinutes` — a tick reads
            whatever interval is already persisted, and does not re-validate it against the
            API's current allowlist.
        rng: A **required keyword argument with no default**, for the same reason `now` on
            `compute_next_run_at` has none: it keeps this function pure, with no hidden call to
            a module-level random source, so a test can drive it with `Random(seed)` and assert
            an exact value instead of only a range.

    Returns:
        `now + timedelta(minutes=interval_minutes)`, offset by a uniformly-drawn jitter in
        `[-5%, +5%]` of that interval, as a timezone-aware `datetime`.
    """
    interval = timedelta(minutes=interval_minutes)
    max_offset_seconds = interval.total_seconds() * MAX_JITTER_FRACTION
    offset = timedelta(seconds=rng.uniform(-max_offset_seconds, max_offset_seconds))
    return now + interval + offset
