"""Business logic for the runs feature.

`internals/runs_writer.py` holds the feature's two writes: `claim_for_processing` below
opens one `transaction(self._pool)` around `RunsWriter.claim_pending` — the atomic
`pending -> processing` guard `app.features.crawl.service.CrawlService.execute_run` uses
before it ever fetches a byte — and `record_failure` opens a second, independent
`transaction()` around `RunsWriter.mark_failed` for the case where a crawl never produces an
artifact. Both are the only writes this feature has; `list_runs` and `get_run` remain reads
with no transaction of their own.

**Reads are unscoped, the same rule `websites/service.py` follows.** `list_runs`, `get_run`,
and `get_website_stats` never look at who is asking — `CurrentUserId` is threaded through
`app.api.routers.runs` purely to require a token (ARCHITECTURE.md §4.1). There is no
`require_owner` call anywhere in this file, and there should never be one — not even beside
the two write methods. `require_owner` (ARCHITECTURE.md §4.2) checks an HTTP caller against
a resource's owner; `claim_for_processing` and `record_failure` are called from
`app.worker.jobs.crawl_task`, a background job acting on its own behalf, with no caller and
therefore no owner to compare against. Ownership on this feature's writes is a question that
does not arise, not one this file forgot to ask.

**`RunService` depends on `WebsiteService`, never on `WebsitesReader`.** `list_runs` has to
404 on an unknown website before "that website's runs" means anything, and
ARCHITECTURE.md §3.1 is explicit about how one feature gets data another feature owns: "it
calls B's service, never B's reader." Reimplementing "does this website exist?" against
`WebsitesReader` here would be a second copy of a check `WebsiteService.get_website` already
makes, free to drift from it the next time either one changes.

**`duration_ms` is computed here, in Python, never in SQL.** `_shared_fields` below is the
one function both DTO builders call to turn a `started_at`/`completed_at` pair into a
milliseconds figure. Keeping the computation out of `internals/runs_reader.py` is what
guarantees it can never end up referenced by an `ORDER BY` there — a computed sort key
cannot use the `(website_id, started_at DESC)` index the keyset query depends on, at any
cost from "slower" to "a sequential scan", and that module's docstring spells out exactly
what the working alternative costs instead. There is exactly one function that computes
`duration_ms` in this whole codebase, which is what makes "compute it in Python" and "never
sort on it" impossible to accidentally pull apart.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

from asyncpg import Pool
from fastapi import HTTPException, status

from app.core.pagination import Page
from app.features.runs.internals.run_cursor import RunCursor, encode_cursor
from app.features.runs.internals.runs_reader import RunsReader
from app.features.runs.internals.runs_writer import RunsWriter
from app.features.runs.internals.stats_window import StatsWindow, resolve_window
from app.features.runs.schemas import (
    RunDetailResponse,
    RunListItemResponse,
    RunStatsPoint,
    RunStatsTotals,
    RunStatusName,
    StatsWindowName,
    WebsiteStatsResponse,
)
from app.features.websites.service import WebsiteService
from app.infrastructure.db.transaction import transaction


_NOT_FOUND_DETAIL = "No run with that id"

# The largest page `list_runs` will ever return, no matter what a caller asks for.
# `GET /websites/{id}/runs?limit=` is `Query(ge=1)` at the router — deliberately no `le=` —
# so a request above this is CLAMPED here rather than rejected with a 422: the contract is
# "you get at most this many", not "you asked for too many and that is an error."
MAX_LIMIT: Final = 100


def _duration_ms(started_at: datetime, completed_at: datetime | None) -> int | None:
    """`None` while a run is still in flight; otherwise its elapsed time in whole ms.

    See `schemas.RunListItemResponse.duration_ms` and the module docstring for why this
    computation lives here and nowhere else.
    """
    if completed_at is None:
        return None
    # Integer floor division by a 1ms timedelta, not `int(total_seconds() * 1000)`. The two
    # agree for every duration a crawl will plausibly have, but `total_seconds()` returns a
    # float, and float multiplication is exact right up until the day it is not. Dividing
    # two timedeltas stays in integer microseconds the whole way.
    return (completed_at - started_at) // timedelta(milliseconds=1)


def _parse_stats(raw: Any) -> dict[str, Any] | None:
    """Decode `runs.stats`, defensively.

    asyncpg decodes a `jsonb` column as a plain `str` rather than a `dict` — there is no
    codec registered that would do otherwise — so every non-`None` value handed to this
    function starts life as JSON text that needs a `json.loads`. `stats` also has no shape
    Postgres enforces (ARCHITECTURE.md §3.4: the crawler milestone is not designed yet), so
    a stored value can be malformed JSON, or valid JSON that decodes to something other
    than an object (an array, a bare number, `null`). Either case yields `None` here —
    "no usable stats" — rather than letting one bad row 500 an otherwise-healthy run's
    history.
    """
    if raw is None:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _shared_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Every field `RunListItemResponse` has, built from one reader row.

    The single row -> DTO builder both `_to_list_item` and `_to_detail` call, so
    `duration_ms` and `stats` are computed in exactly one place regardless of which
    endpoint is asking.
    """
    return {
        "id": row["id"],
        "website_id": row["website_id"],
        "status": row["status"],
        "trigger": row["trigger"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "duration_ms": _duration_ms(row["started_at"], row["completed_at"]),
        "stats": _parse_stats(row["stats"]),
        "error": row["error"],
    }


def _to_list_item(row: dict[str, Any]) -> RunListItemResponse:
    """Build one `GET /websites/{id}/runs` row. Excludes `llms_txt` / `storage_path` because
    `_LIST_COLUMNS` (`internals/runs_reader.py`) never selected them in the first place."""
    return RunListItemResponse(**_shared_fields(row))


def _to_detail(row: dict[str, Any]) -> RunDetailResponse:
    """Build the full `GET /runs/{id}` body: every list-item field, plus the two `_DETAIL_
    COLUMNS` (`internals/runs_reader.py`) adds on top of `_LIST_COLUMNS`."""
    return RunDetailResponse(
        **_shared_fields(row), llms_txt=row["llms_txt"], storage_path=row["storage_path"]
    )


def _to_stats(window: StatsWindow, rows: list[dict[str, Any]]) -> WebsiteStatsResponse:
    """Build the full `GET /websites/{id}/stats` body from `_WEBSITE_STATS`'s rows.

    `rows` is never empty — `RunsReader.website_stats`'s `generate_series` always yields
    `window.bucket_count` of them — so `totals` is read off `rows[0]` rather than recomputed
    in Python from the series. The `assert` below documents that invariant instead of silently
    indexing into a list that could, in principle, be empty.

    All PRESENTATION rounding happens here, in exactly one place, never in SQL: `avg(...)`
    already zero-filled every empty bucket and the whole-window totals, so this only rounds
    values SQL already computed correctly.
    """
    assert rows, "generate_series always yields window.bucket_count rows"

    series = [
        RunStatsPoint(
            t=row["bucket_start"],
            runs=row["runs"],
            completed=row["completed"],
            failed=row["failed"],
            avg_pages=round(row["avg_pages"], 2),
            avg_duration_ms=round(row["avg_duration_ms"]),
        )
        for row in rows
    ]

    total_runs = rows[0]["total_runs"]
    total_completed = rows[0]["total_completed"]
    totals = RunStatsTotals(
        total_runs=total_runs,
        completed=total_completed,
        failed=rows[0]["total_failed"],
        # `None`, never `0.0`, when there are no runs to compute a rate over — see
        # `RunStatsTotals.success_rate`.
        success_rate=round(total_completed / total_runs, 4) if total_runs > 0 else None,
        avg_duration_ms=round(rows[0]["total_avg_duration_ms"]),
        avg_pages=round(rows[0]["total_avg_pages"], 2),
        last_run_at=rows[0]["last_run_at"],
    )

    return WebsiteStatsResponse(
        window=window.name, bucket=window.bucket, series=series, totals=totals
    )


class RunService:
    """Business logic for runs. Constructed per request from the shared pool."""

    def __init__(self, pool: Pool, website_service: WebsiteService) -> None:
        self._pool = pool
        # Bound to the pool, like `WebsitesReader` in `websites/service.py` — each read
        # borrows a connection and returns it immediately. There is no writer to build
        # inside a `transaction()` block, because this feature has none.
        self._reader = RunsReader(pool)
        # Injected rather than constructed here so the router's provider function
        # (`get_run_service`, `app.api.routers.runs`) controls both services' lifetimes
        # the same way, and so a test can hand this a `WebsiteService` built against a
        # different pool or with dependencies overridden.
        self._website_service = website_service

    async def list_runs(
        self,
        website_id: UUID,
        *,
        cursor: RunCursor | None,
        status: RunStatusName | None,
        limit: int,
    ) -> Page[RunListItemResponse]:
        """Return one page of `website_id`'s run history, newest first.

        `cursor` arrives already decoded — the router's `parse_cursor` dependency
        (`app.api.routers.runs`) turns a bad `?cursor=` into a `422` before this method
        ever runs. That is what keeps this method directly unit-testable without
        constructing base64 in a test: a `RunCursor` is built by hand instead.

        Raises:
            HTTPException: `404` if there is no website with that id — delegated to
                `WebsiteService.get_website`, whose return value is unused here. This
                method needs only the fact that the row exists, not any of its fields
                (ARCHITECTURE.md §3.1: a feature calls another feature's service, never
                its reader).
        """
        await self._website_service.get_website(website_id)

        capped_limit = min(limit, MAX_LIMIT)
        # Ask for one row more than a page holds. Getting exactly `capped_limit + 1` rows
        # back is how the presence of a next page is detected without a second query — a
        # `COUNT(*)` would cost as much as the page query itself and go stale the instant
        # a new run is inserted (`core/pagination.py`'s module docstring).
        rows = await self._reader.list_by_website(
            website_id, cursor=cursor, status=status, limit=capped_limit + 1
        )

        has_next_page = len(rows) > capped_limit
        page_rows = rows[:capped_limit]

        next_cursor = None
        if has_next_page:
            # Built from the last row THIS PAGE KEEPS, never from the probe row dropped
            # above. The client never sees that row, so a cursor built from it would point
            # one position too far forward and silently skip it (and any tie-mates sharing
            # its `started_at`) on every later page.
            last = page_rows[-1]
            next_cursor = encode_cursor(last["started_at"], last["id"])

        return Page[RunListItemResponse](
            items=[_to_list_item(row) for row in page_rows], next_cursor=next_cursor
        )

    async def get_run(self, run_id: UUID) -> RunDetailResponse:
        """Return one run in full, for any signed-in caller.

        Raises:
            HTTPException: `404` if there is no run with that id.
        """
        row = await self._reader.get_by_id(run_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
        return _to_detail(row)

    async def get_website_stats(
        self, website_id: UUID, *, window: StatsWindowName
    ) -> WebsiteStatsResponse:
        """Return `website_id`'s run statistics over `window`, pre-aggregated into fixed,
        zero-filled buckets plus a whole-window summary — one query, no per-bucket queries.

        Reuses `WebsiteService.get_website` purely for its `404`, exactly like `list_runs`
        above — its return value is unused here (ARCHITECTURE.md §3.1: a feature calls
        another feature's service, never its reader). Never scoped by caller: reads are
        unscoped in this codebase (ARCHITECTURE.md §4.1), and there is no `require_owner`
        call anywhere in this path.

        Raises:
            HTTPException: `404` if there is no website with that id.
        """
        await self._website_service.get_website(website_id)

        resolved = resolve_window(window, datetime.now(UTC))
        rows = await self._reader.website_stats(website_id, window=resolved)
        return _to_stats(resolved, rows)

    async def claim_for_processing(self, run_id: UUID) -> bool:
        """Atomically flip `run_id` from `pending` to `processing`, or refuse to.

        The correctness guard `app.features.crawl.service.CrawlService.execute_run` relies
        on before it fetches a single byte: `arq`'s `MAX_JOBS = 2`
        (`app/worker/settings.py`) makes concurrent delivery of the same job a real
        scenario, and only a single, atomic conditional `UPDATE` — never a `SELECT` followed
        by an `UPDATE` — can guarantee that at most one caller ever sees `True` for a given
        `run_id`.

        Returns:
            `True` if this call won the race and the row is now `processing`; `False` if it
            was not `pending` — already claimed, already terminal, or nonexistent. The
            caller treats every `False` the same way: do not crawl.
        """
        async with transaction(self._pool) as tx:
            return await RunsWriter(tx).claim_pending(run_id)

    async def record_failure(self, run_id: UUID, error: str) -> None:
        """Record a run's terminal failure.

        `error` must already be sanitized by the caller
        (`app.features.crawl.service.CrawlService`'s "Sanitizing" note) — this method does
        not inspect, truncate, or otherwise second-guess it, because `runs.error` is
        readable by every signed-in user (ARCHITECTURE.md §4.1) and this feature has no way
        to tell a safe message from an unsafe one after the fact.
        """
        async with transaction(self._pool) as tx:
            await RunsWriter(tx).mark_failed(run_id, error)
