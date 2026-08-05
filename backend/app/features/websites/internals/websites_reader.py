"""Every `SELECT` for the websites feature. No writes, ever (ARCHITECTURE.md §3.1).

**READS IN THIS FILE ARE INTENTIONALLY NOT SCOPED TO THE CALLER. DO NOT ADD
`WHERE user_id = $1` TO `list_all`, `list_all_with_latest_run`, OR `get_by_id`.**

This is the single easiest rule in this project to break by being helpful (ARCHITECTURE.md
§4.1). This application is **not multi-tenant**: there is one flat pool of websites, and
browsing every user's sites — including the `llms.txt` they generated — is the product, not
a leak. An owner filter added here for "security hygiene" would pass every test that only
ever signs in as one user, look like an improvement in review, and silently delete the main
feature.

The one method that does take a `user_id` is `get_by_user_and_origin`, and that is the
case §4.1 explicitly blesses: "A reader may still accept a `user_id` as a query argument for
a genuinely user-filtered feature... What is prohibited is applying that filter to the
general read path as an implicit security measure." It exists because
`UNIQUE (user_id, origin)` is a *dedupe* key — one user may not add the same site twice,
two users may each add it — so the duplicate lookup is per-user by definition, not for
protection.

**Why the list query is one statement.** `GET /websites?include=latest_run` backs the main
table. Folding in each website's most recent run with a query per row is the classic N+1,
and it is felt immediately here because the page renders every website the installation has.
`_LIST_WITH_LATEST_RUN` uses a `LEFT JOIN LATERAL ... LIMIT 1`, so the query count is one,
independently of how many websites exist. `tests/test_websites_api.py` measures that rather
than assuming it.

`LATERAL` over `DISTINCT ON` (the ticket offers either): both are one statement and both ride
the `runs(website_id, started_at DESC)` index from db/schema.prisma, but `DISTINCT ON`
requires the outer `ORDER BY` to lead with `website_id`, which collides with this endpoint's
"most recently active first" ordering and would need a wrapping subquery to fix.
"""

from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Reader


# The columns of `websites`, in the order `WebsiteResponse` declares them. Named once so
# the four queries below cannot drift apart, and so adding a column to the DTO produces one
# edit rather than four.
_WEBSITE_COLUMNS: Final = "w.id, w.user_id, w.url, w.origin, w.title, w.created_at"

# "Most recently active first": the newest run's start time, falling back to when the
# website was added for one that has never run. Used as the sort key by BOTH list queries,
# so `?include=latest_run` changes what a row *contains* and never what order rows arrive
# in — a list whose ordering depended on which fields you asked for would be a genuinely
# confusing API.
#
# `w.id` breaks ties. Without it, two websites created in the same transaction (their
# `created_at` comes from `CURRENT_TIMESTAMP`, which is transaction start, so ties are
# routine rather than exotic) could come back in either order on successive calls.
_ACTIVITY_ORDER: Final = "ORDER BY COALESCE(latest.started_at, w.created_at) DESC, w.id"

# The correlated lookup of one website's newest run. `LIMIT 1` inside the lateral is what
# makes this an index seek per website rather than a sort of that website's whole run
# history, and `started_at DESC, id DESC` makes "newest" deterministic when two runs share
# a start time.
_LATEST_RUN_LATERAL: Final = """
    LEFT JOIN LATERAL (
        SELECT r.id, r.status, r.started_at, r.completed_at, r.stats
        FROM runs r
        WHERE r.website_id = w.id
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT 1
    ) latest ON TRUE
"""

# `pages_crawled` lives in `runs.stats`, a jsonb column whose shape belongs to the crawler
# milestone and is therefore not designed yet (ARCHITECTURE.md §3.4) — nothing in the
# database constrains it. So it is read in a way that cannot fail:
#
#   * `jsonb_typeof(...) = 'number'` skips a string, an object, a JSON null, or an absent
#     key, all of which would raise on a cast.
#   * `::numeric`, not `::bigint`, because `jsonb_typeof` says `12.5` is a number too and
#     `'12.5'::bigint` raises `invalid input syntax`. `trunc()` then gives an integer.
#
# A malformed `stats` therefore yields `NULL` — the same as "no stats yet" — instead of
# turning one bad row into a 500 for the whole list.
_PAGES_CRAWLED: Final = """
    CASE
        WHEN jsonb_typeof(latest.stats -> 'pages_crawled') = 'number'
        THEN trunc((latest.stats ->> 'pages_crawled')::numeric)::bigint
    END AS pages_crawled
"""

_GET_BY_ID: Final = f"""
    SELECT {_WEBSITE_COLUMNS}
    FROM websites w
    WHERE w.id = $1
"""

_GET_BY_USER_AND_ORIGIN: Final = f"""
    SELECT {_WEBSITE_COLUMNS}
    FROM websites w
    WHERE w.user_id = $1 AND w.origin = $2
"""

# No WHERE clause. That is the point — see the module docstring.
_LIST_ALL: Final = f"""
    SELECT {_WEBSITE_COLUMNS}
    FROM websites w
    LEFT JOIN LATERAL (
        SELECT r.started_at
        FROM runs r
        WHERE r.website_id = w.id
        ORDER BY r.started_at DESC, r.id DESC
        LIMIT 1
    ) latest ON TRUE
    {_ACTIVITY_ORDER}
"""

# Also no WHERE clause, and one statement for the whole page.
_LIST_WITH_LATEST_RUN: Final = f"""
    SELECT
        {_WEBSITE_COLUMNS},
        latest.id AS latest_run_id,
        latest.status AS latest_run_status,
        latest.started_at AS latest_run_started_at,
        latest.completed_at AS latest_run_completed_at,
        {_PAGES_CRAWLED},
        s.active AS schedule_active,
        s.interval_minutes AS schedule_interval_minutes,
        s.next_run_at AS schedule_next_run_at
    FROM websites w
    {_LATEST_RUN_LATERAL}
    LEFT JOIN schedules s ON s.website_id = w.id
    {_ACTIVITY_ORDER}
"""


class WebsitesReader(Reader):
    """Reads for the websites feature. Private to it — only `WebsiteService` calls this."""

    async def get_by_id(self, website_id: UUID) -> dict[str, Any] | None:
        """Return one website by id, or `None` if there is no such row.

        Unscoped, and called from both the read and the write paths. On a write path the
        service checks ownership on the result with `require_owner`; this method does not,
        because the same fetch also serves `GET /websites/{id}`, which any signed-in user
        may call for any website.
        """
        return await self.fetch_one(_GET_BY_ID, website_id)

    async def get_by_user_and_origin(self, user_id: UUID, origin: str) -> dict[str, Any] | None:
        """Return this user's website with `origin`, if they already added it.

        The duplicate check behind the `409` from `POST /websites`, and the one query in
        this file that filters by `user_id` — because `UNIQUE (user_id, origin)` is a
        per-user dedupe key, not a tenancy boundary. See the module docstring.
        """
        return await self.fetch_one(_GET_BY_USER_AND_ORIGIN, user_id, origin)

    async def list_all(self) -> list[dict[str, Any]]:
        """Return every website from every user, most recently active first.

        Not filtered by caller, deliberately and permanently (ARCHITECTURE.md §4.1).
        """
        return await self.fetch_all(_LIST_ALL)

    async def list_all_with_latest_run(self) -> list[dict[str, Any]]:
        """Return every website with its newest run and schedule folded in — in one query.

        Same unscoped result set and same ordering as `list_all`, with the latest-run and
        schedule columns added. One statement regardless of row count; see the module
        docstring on why that is load-bearing rather than an optimization.
        """
        return await self.fetch_all(_LIST_WITH_LATEST_RUN)
