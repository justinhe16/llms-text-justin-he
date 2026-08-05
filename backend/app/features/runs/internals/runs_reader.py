"""Every `SELECT` for the runs feature. No writes — this ticket has none at all: there is no
`runs_writer.py` beside this file, because nothing in PER-155 mutates `runs`.

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

Both return the same rows. They are not equally fast, measured against
`runs_website_id_started_at_idx (website_id, started_at DESC)` on both Postgres 14 and 16:

  * **Row-value form (the one below).** Postgres decomposes the row constructor and pushes
    `website_id = $1 AND started_at <= $2` straight into the Index Cond. The tuple
    comparison survives only as a Filter that discards a couple of rows the bound could not
    exclude on its own — measured at 2 rows removed / 8 buffers on PG14.
  * **OR-expanded form.** The planner does NOT push the `started_at` bound into the Index
    Cond. It walks every row newer than the cursor and discards them one at a time —
    measured at 101 rows removed / 23 buffers on PG14, and, on a deeper page of a ~270k-row
    PG16 table, 4001 rows removed / 90 buffers against the row-value form's 1 row removed /
    5 buffers. That cost grows LINEARLY with how far into the history the client has paged;
    the row-value form's does not.

`tests/test_runs_api.py`'s EXPLAIN test imports the exact query constants below and pins
their plan shape, so a regression here fails a test instead of only a production dashboard.

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
"""

from typing import Any, Final
from uuid import UUID

from app.features.runs.internals.run_cursor import RunCursor
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
