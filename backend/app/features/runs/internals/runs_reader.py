"""Every `SELECT` for the runs feature. `internals/runs_writer.py`, beside this file, holds
the feature's two writes — the atomic `pending -> processing` claim and the terminal-failure
record the crawl feature's worker job needs — added once something in this codebase finally
mutated `runs`. Nothing below this docstring changed to make room for it: every query here
remains a plain `SELECT`, and the reader still knows nothing about `runs_writer.py`.

**READS IN THIS FILE ARE INTENTIONALLY NOT SCOPED TO THE CALLER.** Same rule as
`websites_reader.py` (ARCHITECTURE.md §4.1): `list_by_website` returns a website's runs to
ANY signed-in user, not merely its owner, and `get_by_id` returns any run, including its
`llms_txt`, to any signed-in user. Do not add `WHERE user_id = $1` to either — there is no
`user_id` column on `runs` in the first place, but the same principle applies to
`website_id`-scoping-as-a-security-measure: the `website_id` filter below exists because
"this website's runs" is the endpoint's documented contract, not because it is standing in
for an ownership check.

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
