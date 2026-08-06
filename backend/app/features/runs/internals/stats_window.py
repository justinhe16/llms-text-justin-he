"""`resolve_window` — the one pure function that turns a requested window name into the fixed
set of buckets `RunService.get_website_stats` aggregates over.

This module lives in `runs/internals/` for the same reason `next_run.py` lives in
`schedules/internals/` (ARCHITECTURE.md §3.1): feature-owned logic with no I/O and no state,
tested exhaustively on its own, and imported by exactly one service.

**Why this has to be pure.** `GET /websites/{id}/stats?window=` derives a bucket size, a
bucket count, and a half-open `[start, end)` range entirely from `window` and the current
instant — no database access is involved in deciding *what to ask for*, only in answering it.
A function of `(name, now)` — with no hidden call to `datetime.now()` inside — is what makes
every boundary exhaustively testable with a fixed clock (`tests/test_stats_window.py`), the
same reason `compute_next_run_at` takes `now` as a required keyword argument instead of an
ambient one.

**The mapping is a module-level `Final` dict keyed by the window name** — never built from
the raw request string. `StatsWindowName` is a `Literal`, so an unrecognized value is already
a `422` at the FastAPI boundary before this module ever runs; `_WINDOWS` only has to answer
for the three names that literal admits.

| window | bucket | step   | bucket_count |
| ------ | ------ | ------ | ------------ |
| `7d`   | `hour` | 1 hour | 168          |
| `30d`  | `day`  | 1 day  | 30           |
| `90d`  | `day`  | 1 day  | 90           |

**Boundary arithmetic**, all in UTC (`now` is converted with `.astimezone(UTC)` first, so a
caller in any offset gets the same buckets a UTC clock would):

    last_bucket = truncate(now, bucket)   # hour -> zero min/sec/us; day -> also zero hour
    start       = last_bucket - (bucket_count - 1) * step
    end         = last_bucket + step      # exclusive; covers the whole current bucket

This yields exactly `bucket_count` buckets for every window. The range is half-open
`[start, end)`, and `date_trunc` (in `runs_reader.py`'s `_WEBSITE_STATS`) partitions it into
non-overlapping buckets on that same boundary — which is what makes "a run at the edge of a
bucket is counted exactly once, never twice and never zero times" true.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from app.features.runs.schemas import StatsBucketName, StatsWindowName


@dataclass(frozen=True)
class StatsWindow:
    """The fully-resolved shape of one `?window=` request — everything `RunsReader.
    website_stats` needs to bind into `_WEBSITE_STATS`, plus the two fields
    `WebsiteStatsResponse` echoes back (`window`/`bucket`)."""

    name: StatsWindowName
    bucket: StatsBucketName
    """The `date_trunc` field this window buckets on. Also the response's `bucket` field."""

    step: timedelta
    """The width of one bucket — `date_trunc($5, r.started_at, 'UTC')`'s implicit grouping
    width, and the `generate_series` step in `_WEBSITE_STATS`."""

    bucket_count: int
    """How many buckets `series` will have — always this many, zero-filled or not."""

    start: datetime
    """Inclusive lower bound of the aggregated range, UTC, truncated to a bucket boundary."""

    end: datetime
    """Exclusive upper bound — `start + bucket_count * step`."""


@dataclass(frozen=True)
class _WindowSpec:
    """The three numbers that vary per window name; `start`/`end` are computed from these
    plus `now`, so they do not belong in this dict."""

    bucket: StatsBucketName
    step: timedelta
    bucket_count: int


_WINDOWS: Final[dict[StatsWindowName, _WindowSpec]] = {
    "7d": _WindowSpec(bucket="hour", step=timedelta(hours=1), bucket_count=168),
    "30d": _WindowSpec(bucket="day", step=timedelta(days=1), bucket_count=30),
    "90d": _WindowSpec(bucket="day", step=timedelta(days=1), bucket_count=90),
}


def _truncate(instant: datetime, bucket: StatsBucketName) -> datetime:
    """Zero out everything finer than `bucket`. `instant` is assumed already UTC."""
    truncated = instant.replace(minute=0, second=0, microsecond=0)
    if bucket == "day":
        truncated = truncated.replace(hour=0)
    return truncated


def resolve_window(name: StatsWindowName, now: datetime) -> StatsWindow:
    """Resolve `name` into the concrete bucket layout to aggregate over, as of `now`.

    Args:
        name: One of the three `StatsWindowName` values. FastAPI's `Literal` validation
            guarantees this by the time a request reaches `RunService.get_website_stats` —
            this function does not itself validate it.
        now: The instant to resolve the window relative to. Required, with no default, so
            this function is exhaustively unit-testable with a fixed clock — the caller
            (`RunService.get_website_stats`) passes `datetime.now(UTC)`.

    Returns:
        A `StatsWindow` covering exactly `bucket_count` buckets, ending with the bucket `now`
        falls in.
    """
    spec = _WINDOWS[name]
    utc_now = now.astimezone(UTC)
    last_bucket = _truncate(utc_now, spec.bucket)
    start = last_bucket - (spec.bucket_count - 1) * spec.step
    end = last_bucket + spec.step
    return StatsWindow(
        name=name,
        bucket=spec.bucket,
        step=spec.step,
        bucket_count=spec.bucket_count,
        start=start,
        end=end,
    )
