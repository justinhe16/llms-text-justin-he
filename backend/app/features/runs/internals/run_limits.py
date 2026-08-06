"""Pure helpers for the two `429` messages `POST /websites/{id}/runs` can return.

Lives in `runs/internals/` for the same reason `websites/internals/url_normalize.py` and
`schedules/internals/next_run.py` do (ARCHITECTURE.md §3.1): feature-owned logic with no I/O
and no state, imported by exactly one service, and cheap to pin exhaustively in a unit test
that needs no database and no clock mocking beyond passing a `datetime` in.

**Why the wording lives here instead of inline in `service.py`.** The ticket requires a `429`
to name both the limit that was hit and, where one exists, when it clears — "you are over
the limit" alone tells a client nothing about whether to retry in ten seconds or ten hours.
Keeping the exact strings in one pure module, imported by `tests/test_run_limits.py`, is what
keeps that requirement a fact a test enforces rather than a convention a reviewer has to
eyeball in a service method three call frames deep.

**Rounding is DOWN, always, to whole minutes.** A client that reads "resets in 3h 12m",
waits exactly that long, and retries at 3h12m00s should never land back on a `429` because
the real reset was at 3h12m59s. Rounding up would promise an earlier time than the cap
actually clears; rounding down never does. The cost is a client that could have retried a
few seconds sooner — trivial next to the cost of a client that retried on schedule and was
turned away again.
"""

from datetime import datetime, timedelta


def format_reset_delay(delta: timedelta) -> str:
    """Render `delta` as `"3h 12m"`, `"12m"`, `"4h"`, or `"less than a minute"`.

    Args:
        delta: A duration, expected to be non-negative — the gap between `now` and a future
            reset instant. Zero and negative values are both folded into `"less than a
            minute"` rather than raising: a negative delta means the window has already
            reset by the time this renders (a race between the count check and the message
            build), and telling the client "less than a minute" is still an honest answer,
            unlike a nonsensical negative duration or a `ValueError` in a 429 body.

    Returns:
        Whole minutes only — `delta`'s seconds and smaller are dropped by floor division,
        never rounded to the nearest minute. `"4h"`, not `"4h 0m"`, when the minute
        component is zero: an `m` of `0` carries no information a reader needs. No days
        unit: every caller in this codebase bounds `delta` to at most 24h (the daily cap's
        window), so hours alone are always enough.
    """
    total_minutes = delta // timedelta(minutes=1)
    if total_minutes <= 0:
        return "less than a minute"

    hours, minutes = divmod(total_minutes, 60)
    if hours == 0:
        return f"{minutes}m"
    if minutes == 0:
        return f"{hours}h"
    return f"{hours}h {minutes}m"


def concurrency_limit_message(limit: int) -> str:
    """The `429` message for the per-user concurrency cap — no reset time.

    Unlike `daily_limit_message` below, this message names no instant, because there is no
    predictable one: the cap frees the moment any of the caller's in-flight manual runs
    finishes, and nothing in this codebase can say when that will be — a crawl's duration
    depends on the site being crawled. Inventing a time here would be a more misleading
    answer than admitting there is none; a client that wants to know "is a slot free yet?"
    already has the tool for that, `GET /websites/{id}/runs`.
    """
    return (
        f"Too many runs in progress (limit: {limit}). Wait for one to finish, then trigger another."
    )


def daily_limit_message(limit: int, resets_at: datetime, now: datetime) -> str:
    """The `429` message for the rolling-24h daily cap, including a rounded-down reset delay.

    `resets_at` is the instant the OLDEST run inside the current 24h window ages out of it —
    the earliest moment a slot can free, not a promise that one will be free exactly then.
    If the caller is over the cap by more than one (possible under the races
    `RunService.trigger_run`'s docstring documents), the next slot may take longer than this
    message states; the value is a floor, matching `RunService`'s own framing of it.
    """
    delay = format_reset_delay(resets_at - now)
    return f"Daily limit reached ({limit} runs in 24 hours). Resets in {delay}."
