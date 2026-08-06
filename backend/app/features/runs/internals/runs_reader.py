"""Every `SELECT` for the runs feature. `internals/runs_writer.py`, beside this file, holds
every write: the manual-trigger insert behind `POST /websites/{id}/runs`, plus the atomic
`pending -> processing` claim and the terminal-failure record the crawl feature's worker job
needs.

Nothing below this docstring changed to make room for any of them — every query here is
still a plain `SELECT`, and this reader knows nothing about `runs_writer.py`. The three
queries `_ACTIVE_FOR_WEBSITE`, `_COUNT_ACTIVE_MANUAL_FOR_USER`, and
`_COUNT_AND_OLDEST_SINCE_FOR_USER` belong to the manual-trigger path even though all three are
`SELECT`s: the duplicate-run guard and both abuse caps are read-then-decide checks the service
runs before it opens a transaction, not writes in their own right. A fourth,
`_WEBSITE_IDS_WITH_ACTIVE_RUNS`, belongs to a different write path entirely — the cron tick's
`ScheduleService.run_due_schedules`, reached only through `RunService.website_ids_with_active_
runs` (ARCHITECTURE.md §3.1: one feature calls another feature's service, never its reader) —
and is documented beside `_ACTIVE_FOR_WEBSITE` below because it answers the same underlying
question in batch rather than one website at a time.

**READS IN THIS FILE ARE INTENTIONALLY NOT SCOPED TO THE CALLER — WITH ONE NAMED EXCEPTION.**
Same rule as `websites_reader.py` (ARCHITECTURE.md §4.1): `list_by_website` returns a
website's runs to ANY signed-in user, not merely its owner, and `get_by_id` returns any run,
including its `llms_txt`, to any signed-in user. Do not add `WHERE user_id = $1` to either —
there is no `user_id` column on `runs` in the first place, but the same principle applies to
`website_id`-scoping-as-a-security-measure: the `website_id` filter below exists because
"this website's runs" is the endpoint's documented contract, not because it is standing in
for an ownership check.

The exception is the two new cap queries, `count_active_manual_for_user` and
`count_and_oldest_since_for_user`, and it is worth spelling out why they are not a
contradiction of the paragraph above. Both take `user_id` and both filter on it — but "how
many runs is THIS caller responsible for right now" is `POST /websites/{id}/runs`'s
documented, product-level contract ("your caps"), the exact carve-out §4.1's last paragraph
describes for "a genuinely user-filtered feature": a reader may accept `user_id` as a query
argument when the endpoint's contract says it filters. It is not standing in for
`require_owner` — `RunService.trigger_run` already calls that, on the *website*, before
either of these queries runs — and it is not an ownership check on the run being created,
which does not exist yet when these run.

## The deliberate asymmetry: duplicate guard vs. concurrency cap vs. daily cap

Three queries below look like they should agree on which runs they count, and they do not,
on purpose:

- **`_ACTIVE_FOR_WEBSITE`** (the duplicate-run guard) has **no `trigger` filter**: ANY
  `pending`/`processing` run for that website is a 409, scheduled ones included. Two
  concurrent crawls of the same site are wasteful and produce interleaved writes to the same
  `llms_txt` regardless of what started either one — the website does not care who asked.
- **`_COUNT_ACTIVE_MANUAL_FOR_USER`** (the concurrency cap) counts `trigger = 'manual'`
  **only**. A site on an hourly schedule would otherwise occupy one of a user's two
  concurrency slots for a large fraction of every hour — a scheduled run the user did not
  even ask for right now, blocking a manual one they did.
- **`_COUNT_AND_OLDEST_SINCE_FOR_USER`** (the daily cap) counts **every** trigger. It is a
  total-work budget, and a scheduled crawl costs the same worker time a manual one does — the
  cap exists to bound load, not to bound button-clicks.

## Why the two cap queries are separate queries, not one with a `FILTER` clause

A single query answering both caps at once would need `WHERE websites.user_id = $1` with no
bound on `started_at`, scanning every run the user has EVER had and using neither index well.
Two queries, each shaped to use its own index — `runs_status_active_idx` (the partial index
on `pending`/`processing`) for the active count, `runs_website_id_started_at_idx` per site for
the windowed count — is cheaper, and it is the shape the ticket itself describes.

## The keyset query — read this before touching it

`_LIST_BY_WEBSITE_AFTER_CURSOR` (and its `_WITH_STATUS` sibling) compares the sort key as a
ROW:

    WHERE website_id = $1 AND (started_at, id) < ($2, $3)
    ORDER BY started_at DESC, id DESC

**Do not "simplify" that into the logically identical, manually OR-expanded form:**

    WHERE website_id = $1
      AND (started_at < $2 OR (started_at = $2 AND id < $3))

Both return the same rows. They are not equally fast. One benchmark, so that the two numbers
below are directly comparable: **87,600 runs for one website** (ten years of hourly
crawling), `started_at` tied 4-deep, asking for a page **5,000 rows in**, against
`runs_website_id_started_at_idx (website_id, started_at DESC)` on PostgreSQL 14.18 after
`ANALYZE`:

  * **Row-value form (the one below).** Postgres decomposes the row constructor and pushes
    `website_id = $1 AND started_at <= $2` straight into the Index Cond, so the scan STARTS
    at the cursor. The tuple comparison survives only as a Filter discarding the couple of
    rows that bound could not exclude on its own — **2 rows removed, 5 buffers, 0.019 ms**.
  * **OR-expanded form.** The planner does NOT push the `started_at` bound into the Index
    Cond. The scan starts at the newest run and walks — then discards — every row the client
    has already paged past — **5,001 rows removed, 336 buffers, 0.427 ms**. Same index, same
    result set, 67x the buffers.

That gap is a function of PAGE DEPTH, not of table size: it is ~0 on page 1 and grows
linearly as the client pages, which is precisely why it survives the manual spot-check a
reviewer is most likely to run. Independently reproduced on PostgreSQL 16 against a ~270k-row
table (4,001 rows removed / 90 buffers, versus 1 row removed / 5 buffers), so it is a
property of how the planner treats the two predicate shapes rather than an artifact of one
version or one dataset.

`tests/test_runs_api.py`'s EXPLAIN tests import the exact query constants below and pin their
plan shape, so a regression here fails a test instead of only a production dashboard. Those
tests deliberately use a far smaller fixture than the benchmark above — 5,000 rows, a cursor
1,000 deep — which is enough for the planner to choose an index scan and for the shape
assertions to mean something, while still running in milliseconds on every `pytest`
invocation. The figures in this docstring are documentation of a one-off benchmark, not
values CI asserts.

## Why the SELECT list and ORDER BY are both plain columns

`ORDER BY started_at DESC, id DESC` touches only the two raw columns
`runs_website_id_started_at_idx` is built on, and every query below selects plain columns —
nothing computed, nothing aliased to an expression the ORDER BY then references. A computed
sort key cannot use an index at all: Postgres would have to materialize every matching row,
evaluate the expression, and sort the result set from scratch, which is exactly the
"OR-expanded form" cost above but for every row rather than only the ones near the cursor.
`duration_ms` is therefore computed in Python, in `service.py`, and never appears in a
`SELECT` or `ORDER BY` in this file. A reviewer can verify the rule holds by reading the
four query constants below — none of them contains an expression.

## Four fixed query shapes, not one assembled with string concatenation

`list_by_website` picks among `_LIST_BY_WEBSITE`, `_LIST_BY_WEBSITE_WITH_STATUS`,
`_LIST_BY_WEBSITE_AFTER_CURSOR`, and `_LIST_BY_WEBSITE_AFTER_CURSOR_WITH_STATUS` by which of
`cursor` / `status` are present, rather than building the WHERE clause with an f-string at
call time. That is what lets the EXPLAIN test import "the exact SQL string the reader uses"
as a plain module constant and assert against it directly — a query built at call time has
no single string a test could import without re-deriving it, which is exactly the kind of
drift this module docstring is trying to prevent two paragraphs up.

## `_WEBSITE_STATS` is the one exception to "no computed expressions in a SELECT"

The "plain columns only" rule two sections up exists to keep the four keyset-pagination
queries above on `runs_website_id_started_at_idx` — a computed sort key cannot use that
index. `_WEBSITE_STATS` (`website_stats`, below) is a bucketed aggregate with no keyset
`ORDER BY` to protect: its `WHERE` is still a plain indexed range on `(website_id,
started_at)`, but its `SELECT` computes averages, `date_trunc` buckets, and a zero-filled
`generate_series` join on purpose, because there is no index ordering downstream of an
aggregate for a computed column to break.
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Final
from uuid import UUID

from app.features.runs.internals.run_cursor import RunCursor
from app.features.runs.internals.stats_window import StatsWindow
from app.features.runs.schemas import RunStatusName
from app.infrastructure.db.base_repository import Reader


# The columns every history-list query returns, in `RunListItemResponse` field order.
# Deliberately excludes `llms_txt` and `storage_path` — a list view never renders either,
# and `llms_txt` in particular can be large; fetching it for every row of every page would
# cost real bytes for a value the client throws away. `_DETAIL_COLUMNS` below adds both
# back for `get_by_id`, the one query that needs them.
_LIST_COLUMNS: Final = "id, website_id, trigger, status, started_at, completed_at, stats, error"

_DETAIL_COLUMNS: Final = f"{_LIST_COLUMNS}, llms_txt, storage_path"

_GET_BY_ID: Final = f"""
    SELECT {_DETAIL_COLUMNS}
    FROM runs
    WHERE id = $1
"""

# First page, no status filter. `$2` is the caller's page size PLUS ONE — the extra row is
# how `RunService.list_runs` detects a next page without a second, and stale-the-instant-
# it-runs, `COUNT(*)` query (see `core/pagination.py`).
_LIST_BY_WEBSITE: Final = f"""
    SELECT {_LIST_COLUMNS}
    FROM runs
    WHERE website_id = $1
    ORDER BY started_at DESC, id DESC
    LIMIT $2
"""

# `AND status = $2` is a second Filter over the SAME Index Cond as `_LIST_BY_WEBSITE`
# above — `status` is not a column of `runs_website_id_started_at_idx`, so adding this
# filter never changes which index answers the query... usually. See the EXPLAIN test's
# `status='pending'` case in `tests/test_runs_api.py` for the one value where the planner
# may legitimately prefer the partial `runs_status_active_idx` instead — a data-dependent
# optimization, not a regression, and the reason that one regime is not pinned to a name.
_LIST_BY_WEBSITE_WITH_STATUS: Final = f"""
    SELECT {_LIST_COLUMNS}
    FROM runs
    WHERE website_id = $1 AND status = $2::run_status
    ORDER BY started_at DESC, id DESC
    LIMIT $3
"""

# THE keyset query. See the module docstring's long comment before changing the WHERE
# clause below — the row-value comparison is not a style preference.
_LIST_BY_WEBSITE_AFTER_CURSOR: Final = f"""
    SELECT {_LIST_COLUMNS}
    FROM runs
    WHERE website_id = $1 AND (started_at, id) < ($2, $3)
    ORDER BY started_at DESC, id DESC
    LIMIT $4
"""

_LIST_BY_WEBSITE_AFTER_CURSOR_WITH_STATUS: Final = f"""
    SELECT {_LIST_COLUMNS}
    FROM runs
    WHERE website_id = $1 AND (started_at, id) < ($2, $3) AND status = $4::run_status
    ORDER BY started_at DESC, id DESC
    LIMIT $5
"""

# The duplicate-run guard for `POST /websites/{id}/runs`. `IN ('pending', 'processing')`
# below is written to match `runs_status_active_idx`'s partial-index predicate
# (db/migrations/20260805092204_init/migration.sql) CHARACTER FOR CHARACTER, which is what
# lets the planner recognize this WHERE clause as exactly what that index answers — a
# rewrite that is logically equivalent but textually different (`status != 'completed' AND
# status != 'failed'`, say) is not guaranteed to be recognized as implying the index's
# predicate. No `trigger` filter: see the module docstring's asymmetry section for why this
# query alone counts every trigger. `ORDER BY ... LIMIT 1` rather than `EXISTS`, because the
# service's 409 needs the row's `id` and `status`, not merely a boolean.
_ACTIVE_FOR_WEBSITE: Final = """
    SELECT id, status
    FROM runs
    WHERE website_id = $1 AND status IN ('pending', 'processing')
    ORDER BY started_at DESC, id DESC
    LIMIT 1
"""

# The cron tick's batch version of the same question `_ACTIVE_FOR_WEBSITE` answers for one
# website at a time: "of these websites, which already have a run in flight?"
# `status IN ('pending', 'processing')` is written CHARACTER FOR CHARACTER identical to
# `_ACTIVE_FOR_WEBSITE`'s own — not merely equivalent to it — because that textual match is
# what lets the planner recognize this WHERE clause as exactly what `runs_status_active_idx`'s
# partial predicate answers (see that query's own comment, and this module's docstring, for
# why a logically-equivalent rewrite is not good enough). One `ANY($1::uuid[])` probe for the
# whole locked batch, rather than up to `DUE_BATCH_LIMIT` (`schedules/service.py`) separate
# single-website lookups: the tick holds a transaction open across this call, so 50 round
# trips instead of one would mean 50x the time a lock (and a pooled connection) stays held for
# no benefit. `SELECT DISTINCT website_id`, not `id`: the caller only ever asks "is this
# website blocked", never which run is blocking it.
_WEBSITE_IDS_WITH_ACTIVE_RUNS: Final = """
    SELECT DISTINCT website_id
    FROM runs
    WHERE website_id = ANY($1::uuid[]) AND status IN ('pending', 'processing')
"""

# The per-user concurrency cap. `trigger = 'manual'` is the one filter that makes this query
# different from the duplicate guard above — see the module docstring's asymmetry section.
# The enum literals ('pending', 'processing', 'manual') are written bare, not cast, because
# they are being compared against columns Postgres already knows the type of; casting them
# would buy nothing and would make this WHERE clause look different from
# `_ACTIVE_FOR_WEBSITE`'s at a glance, when keeping the `status IN (...)` list textually
# identical between the two is what makes the shared index obvious to a reader of both.
_COUNT_ACTIVE_MANUAL_FOR_USER: Final = """
    SELECT count(*)
    FROM runs
    JOIN websites ON websites.id = runs.website_id
    WHERE websites.user_id = $1
      AND runs.status IN ('pending', 'processing')
      AND runs.trigger = 'manual'
"""

# The rolling-24h daily cap. Counts EVERY trigger — see the module docstring's asymmetry
# section. `oldest_started_at` is not incidental: it is the one piece of data
# `daily_limit_message` (`internals/run_limits.py`) needs to compute `resets_at`, so this
# query is shaped to answer "how many, AND since when" in one round trip rather than a count
# followed by a second query for the minimum.
_COUNT_AND_OLDEST_SINCE_FOR_USER: Final = """
    SELECT count(*) AS total, min(runs.started_at) AS oldest_started_at
    FROM runs
    JOIN websites ON websites.id = runs.website_id
    WHERE websites.user_id = $1 AND runs.started_at > $2
"""

# `GET /websites/{id}/stats` — one aggregate query, no per-bucket queries. See
# `internals/stats_window.py` for how the five parameters below are derived from `?window=`.
#
# Params: $1 website_id (uuid), $2 window start (timestamptz, inclusive), $3 window end
# (timestamptz, exclusive), $4 step (interval, one bucket wide), $5 bucket field (text,
# 'hour' | 'day').
#
# Why this exact shape, since none of it is obvious from the SQL alone:
#
# * `per_bucket` aggregates real rows and is THEN left-joined onto `buckets`. The obvious
#   alternative — `FROM buckets LEFT JOIN scoped ... GROUP BY` — makes `count(*)` return 1
#   for an empty bucket, which is the single easiest way to ship a wrong zero-fill.
#   Aggregating first and `COALESCE`-ing after cannot have that bug.
# * `totals` is a separate single-row CTE `CROSS JOIN`ed onto every series row. That returns
#   both response shapes from one query, and a one-row aggregate always produces a row even
#   over zero input, so the series is still fully zero-filled for a website with no runs.
#   Cost is a handful of repeated columns over at most 168 rows.
# * Deliberately NOT `json_agg`. `to_jsonb(timestamptz)` renders using the session
#   `TimeZone`, so bucket timestamps would come back offset-rendered rather than UTC, and
#   asyncpg hands json/jsonb back as `str`, requiring a `json.loads`. Plain columns keep
#   every value a native asyncpg type (`datetime` with `tzinfo=utc`, `int`, `float`).
# * `AS MATERIALIZED` documents the intent: `runs` is scanned once and the result feeds both
#   aggregates. (Postgres already materializes a CTE referenced twice; saying so keeps it
#   true if that ever changes.)
# * `avg(duration_ms)` relies on `AVG` skipping NULLs, so the denominator is completed runs
#   only, while `count(*)` still counts failed and in-flight runs in `runs`/`total_runs`. The
#   outer `COALESCE(..., 0)` fires only when a bucket has zero completed runs. This is the
#   subtlest thing in the query: `avg(pages_crawled)` below counts every in-window run,
#   missing stats as `0`, while `avg(duration_ms)` counts only completed ones. Both are
#   correct for what each one means, but they are not the same population.
# * `jsonb_typeof(...) = 'number'` rather than a bare cast: `stats` is `NULL` for runs that
#   predate the crawler and for runs that failed during fetch, and a non-numeric value would
#   make a bare `::numeric` cast raise. The guard makes the query total over any real data.
# * `date_trunc($5, ..., 'UTC')` — the three-argument form pins bucketing to UTC instead of
#   inheriting the session `TimeZone`.
_WEBSITE_STATS: Final = """
    WITH buckets AS (
        SELECT generate_series($2::timestamptz, $3::timestamptz - $4::interval, $4::interval)
               AS bucket_start
    ),
    scoped AS MATERIALIZED (
        SELECT
            date_trunc($5::text, r.started_at, 'UTC') AS bucket_start,
            r.status,
            r.started_at,
            CASE WHEN jsonb_typeof(r.stats -> 'pages_crawled') = 'number'
                 THEN (r.stats ->> 'pages_crawled')::numeric
                 ELSE 0
            END AS pages_crawled,
            CASE WHEN r.status = 'completed' AND r.completed_at IS NOT NULL
                 THEN EXTRACT(EPOCH FROM (r.completed_at - r.started_at)) * 1000
                 ELSE NULL
            END AS duration_ms
        FROM runs r
        WHERE r.website_id = $1::uuid
          AND r.started_at >= $2::timestamptz
          AND r.started_at <  $3::timestamptz
    ),
    per_bucket AS (
        SELECT
            bucket_start,
            count(*) AS runs,
            count(*) FILTER (WHERE status = 'completed') AS completed,
            count(*) FILTER (WHERE status = 'failed') AS failed,
            avg(pages_crawled) AS avg_pages,
            avg(duration_ms) AS avg_duration_ms
        FROM scoped
        GROUP BY bucket_start
    ),
    totals AS (
        SELECT
            count(*) AS total_runs,
            count(*) FILTER (WHERE status = 'completed') AS total_completed,
            count(*) FILTER (WHERE status = 'failed') AS total_failed,
            avg(pages_crawled) AS total_avg_pages,
            avg(duration_ms) AS total_avg_duration_ms,
            max(started_at) AS last_run_at
        FROM scoped
    )
    SELECT
        b.bucket_start,
        COALESCE(p.runs, 0)::bigint AS runs,
        COALESCE(p.completed, 0)::bigint AS completed,
        COALESCE(p.failed, 0)::bigint AS failed,
        COALESCE(p.avg_pages, 0)::double precision AS avg_pages,
        COALESCE(p.avg_duration_ms, 0)::double precision AS avg_duration_ms,
        t.total_runs,
        t.total_completed,
        t.total_failed,
        COALESCE(t.total_avg_pages, 0)::double precision AS total_avg_pages,
        COALESCE(t.total_avg_duration_ms, 0)::double precision AS total_avg_duration_ms,
        t.last_run_at
    FROM buckets b
    LEFT JOIN per_bucket p ON p.bucket_start = b.bucket_start
    CROSS JOIN totals t
    ORDER BY b.bucket_start
"""


class RunsReader(Reader):
    """Reads for the runs feature. Private to it — only `RunService` calls this."""

    async def get_by_id(self, run_id: UUID) -> dict[str, Any] | None:
        """Return one run by id, in full, or `None` if there is no such row.

        Unscoped, deliberately: any signed-in user may read any run's `llms_txt`
        (ARCHITECTURE.md §4.1).
        """
        return await self.fetch_one(_GET_BY_ID, run_id)

    async def list_by_website(
        self,
        website_id: UUID,
        *,
        cursor: RunCursor | None,
        status: RunStatusName | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return up to `limit` of `website_id`'s runs, newest first.

        `limit` is the caller's page size PLUS ONE — see the module docstring on
        `_LIST_BY_WEBSITE`. The `website_id` filter is this endpoint's actual, documented
        contract ("this website's runs"), not a stand-in `user_id` check: any signed-in
        caller gets the same rows for the same `website_id` (ARCHITECTURE.md §4.1).

        `cursor` is `None` for a first page; `status` is `None` for an unfiltered one.
        Which of the four module-level query constants runs is a direct function of that
        pair, so the exact SQL text executed for a given call is always one of those four
        constants — never something assembled here.
        """
        if cursor is None and status is None:
            return await self.fetch_all(_LIST_BY_WEBSITE, website_id, limit)
        if cursor is None:
            return await self.fetch_all(_LIST_BY_WEBSITE_WITH_STATUS, website_id, status, limit)
        if status is None:
            return await self.fetch_all(
                _LIST_BY_WEBSITE_AFTER_CURSOR,
                website_id,
                cursor.started_at,
                cursor.run_id,
                limit,
            )
        return await self.fetch_all(
            _LIST_BY_WEBSITE_AFTER_CURSOR_WITH_STATUS,
            website_id,
            cursor.started_at,
            cursor.run_id,
            status,
            limit,
        )

    async def get_active_for_website(self, website_id: UUID) -> dict[str, Any] | None:
        """Return the most recent `pending`/`processing` run for `website_id`, or `None`.

        The duplicate-run guard `RunService.trigger_run` checks before enforcing either
        per-user cap — cheaper to ask "is this exact website already busy?" than "is this
        user busy?", and the module docstring's asymmetry section explains why the two
        questions are deliberately not the same query. `ORDER BY ... LIMIT 1` rather than a
        bare existence check because the `409` this feeds needs the row's `id` and `status`.
        """
        return await self.fetch_one(_ACTIVE_FOR_WEBSITE, website_id)

    async def website_ids_with_active_runs(self, website_ids: Sequence[UUID]) -> set[UUID]:
        """Return the subset of `website_ids` that already have a `pending`/`processing` run.

        The cron tick's batch probe — see `_WEBSITE_IDS_WITH_ACTIVE_RUNS` above for why this
        is one query rather than `len(website_ids)` calls to `get_active_for_website`.

        Args:
            website_ids: The websites behind one tick's locked batch of due schedules. Empty
                is a legitimate call (a tick that locked nothing): answered without a query,
                the same early-return `SchedulesWriter`'s batched advances use for the same
                reason.

        Returns:
            A `set`, not a `list`: every call site only ever asks "is this website's id a
            member of the blocked set", an O(1) question a `set` answers and a `list` does not.
        """
        if not website_ids:
            return set()
        rows = await self.fetch_all(_WEBSITE_IDS_WITH_ACTIVE_RUNS, list(website_ids))
        return {row["website_id"] for row in rows}

    async def count_active_manual_for_user(self, user_id: UUID) -> int:
        """Count `user_id`'s in-flight MANUAL runs, across every website they own.

        Feeds `RunService._enforce_run_caps`'s concurrency check. `fetch_val` plus an
        explicit `int(...)` because `count(*)` decodes as Python `int` already — the cast is
        documentation that this is a scalar, not defensive coercion of something that could
        actually be another type.
        """
        return int(await self.fetch_val(_COUNT_ACTIVE_MANUAL_FOR_USER, user_id))

    async def count_and_oldest_since_for_user(
        self, user_id: UUID, since: datetime
    ) -> dict[str, Any]:
        """Count every run `user_id` has started after `since`, and the oldest of them.

        Feeds `RunService._enforce_run_caps`'s daily-cap check: `since` is `now - 24h`, and
        `oldest_started_at` (`None` when `total` is `0`) is what `resets_at` is computed
        from. Returns a dict with both keys rather than a tuple so the two values stay
        self-describing at the call site — `row["total"]`, `row["oldest_started_at"]` —
        matching the shape every other reader method in this codebase returns.

        Raises:
            RuntimeError: never, in practice — `count(*)`/`min(...)` with no `GROUP BY`
                always produces exactly one row, even when nothing matches (`total` is `0`,
                `oldest_started_at` is `NULL`). Guarded anyway, the same way
                `WebsitesWriter.insert` guards an `INSERT ... RETURNING` that cannot
                actually return nothing, so a `dict[str, Any] | None` from the base class
                never silently becomes `None` flowing into arithmetic on `oldest_started_at`.
        """
        row = await self.fetch_one(_COUNT_AND_OLDEST_SINCE_FOR_USER, user_id, since)
        if row is None:
            raise RuntimeError("count(*)/min(...) aggregate query produced no row")
        return row

    async def website_stats(self, website_id: UUID, *, window: StatsWindow) -> list[dict[str, Any]]:
        """Return one row per bucket in `window`, zero-filled, with the website's totals
        cross-joined onto every row. See `_WEBSITE_STATS` above for the query, and
        `RunService.get_website_stats` / `_to_stats` for how a row becomes a response.

        Never returns an empty list: `generate_series` in `_WEBSITE_STATS` always yields
        exactly `window.bucket_count` rows, even for a website with zero runs in range.
        """
        return await self.fetch_all(
            _WEBSITE_STATS, website_id, window.start, window.end, window.step, window.bucket
        )
