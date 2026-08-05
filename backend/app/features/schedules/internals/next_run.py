"""`compute_next_run_at` — the one pure function that decides what a PUT does to a schedule.

This module lives in `schedules/internals/` for the same reason `url_normalize.py` lives in
`websites/internals/` (ARCHITECTURE.md §3.1): it is feature-owned logic with no I/O and no
state, tested exhaustively on its own, and imported by exactly one service.

**Why this has to be pure.** `PUT /websites/{id}/schedule` is the only write this feature has,
and it is not a plain upsert: whether `next_run_at` should move depends on what the row
already looked like, not just on what the client just sent. A function of `(current,
new_state, now)` — with no database access — is the only way to make every branch of that
decision exhaustively testable without a real transaction, and it is the shape the ticket
asks for directly.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta


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
