"""Business logic and transaction boundaries for the websites feature.

This is the first feature module in the codebase, so it is the reference implementation
every later one copies (ARCHITECTURE.md §3.1: "The first feature module to land becomes the
reference implementation for every one after it"). The four things worth copying:

1. **Reads are unscoped; writes are ownership-checked.** `list_websites` and `get_website`
   never look at who is asking. `delete_website` fetches, calls `require_owner`, and only
   then writes. That asymmetry is the authorization contract (§4), not an oversight.
2. **`require_owner` is the first thing after the fetch.** Nothing sits between them. The
   review test for any write path in this project is to find that call and check what is
   above it (§4.2).
3. **The transaction is opened here, and nowhere else.** Readers and writers are handed a
   pool or a connection; only this layer decides where a unit of work starts and ends (§5).
   Transactions stay short and contain no network calls (§5.1) — the normalization, the
   duplicate lookup, and the 409 all happen before one is opened.
4. **Repositories return dicts; this layer returns DTOs.** `asyncpg.Record` never escapes a
   repository (`app.infrastructure.db.base_repository`), and a `dict` never escapes this
   service. `require_owner` needs a `.user_id` attribute, so the model is built from the
   fetched row *as part of the fetch*, before the ownership check.

**Why a `features/` module imports FastAPI.** Pre-empting the review question, in the same
spirit as `app.core.auth.dependencies`'s note: §3.1 constrains the direction of imports
between *this project's* layers (`api` → `features` → `infrastructure` → `core`), and says
nothing about third-party libraries. Raising `HTTPException` from a service is what §4.2
prescribes — `require_owner` itself raises one — and it is what keeps the router thin: a
service that returned a sentinel for "not found" would push a branch into every handler.
"""

import logging
from typing import Any
from uuid import UUID

from asyncpg import Pool, UniqueViolationError
from fastapi import HTTPException, status

from app.core.auth.ownership import require_owner
from app.features.websites.internals.url_normalize import normalize_url
from app.features.websites.internals.websites_reader import WebsitesReader
from app.features.websites.internals.websites_writer import WebsitesWriter
from app.features.websites.schemas import (
    CreateWebsiteRequest,
    LatestRunSummary,
    ScheduleSummary,
    UpdateWebsiteRequest,
    WebsiteAlreadyExistsDetail,
    WebsiteInclude,
    WebsiteListItemResponse,
    WebsiteResponse,
)
from app.infrastructure.db.transaction import transaction


logger = logging.getLogger(__name__)

# The `UNIQUE (user_id, origin)` index from db/migrations/20260805092204_init/migration.sql.
# The duplicate handler below matches on this name so that some *other* unique violation —
# one a later migration adds — cannot be misreported as "you already added this website",
# sending the frontend to a website id that has nothing to do with the failure.
_ORIGIN_UNIQUE_CONSTRAINT = "websites_user_id_origin_key"

_NOT_FOUND_DETAIL = "No website with that id"


def _to_website(row: dict[str, Any]) -> WebsiteResponse:
    """Build the response DTO from a reader row."""
    return WebsiteResponse.model_validate(row)


def _to_list_item(row: dict[str, Any]) -> WebsiteListItemResponse:
    """Build one `GET /websites?include=latest_run` row from the folded query's flat result.

    The reader returns one flat row per website — website columns, `latest_run_*` columns,
    and `schedule_*` columns side by side — because that is what a single query can produce.
    Reassembling it into nested objects is presentation shaping, so it happens here rather
    than being faked in SQL with `json_build_object`, which would move DTO structure into
    the reader and make the response shape invisible to the type checker.

    Both folds are absent-or-present as a unit: a `LEFT JOIN` that matched nothing leaves
    every column of that side `NULL`, so one non-null discriminator per side is enough.
    `latest.id` and `schedules.active` are both `NOT NULL` in their own tables, which is what
    makes them trustworthy discriminators rather than merely usually-set ones.
    """
    latest_run = None
    if row["latest_run_id"] is not None:
        latest_run = LatestRunSummary(
            id=row["latest_run_id"],
            status=row["latest_run_status"],
            started_at=row["latest_run_started_at"],
            completed_at=row["latest_run_completed_at"],
            pages_crawled=row["pages_crawled"],
        )

    schedule = None
    if row["schedule_active"] is not None:
        schedule = ScheduleSummary(
            active=row["schedule_active"],
            interval_minutes=row["schedule_interval_minutes"],
            next_run_at=row["schedule_next_run_at"],
        )

    return WebsiteListItemResponse(
        id=row["id"],
        user_id=row["user_id"],
        url=row["url"],
        origin=row["origin"],
        title=row["title"],
        enrich_with_llm=row["enrich_with_llm"],
        created_at=row["created_at"],
        latest_run=latest_run,
        schedule=schedule,
    )


def _already_exists(existing: WebsiteResponse) -> HTTPException:
    """Build the `409` for a duplicate origin, carrying the existing website's id.

    Returned rather than raised so the `raise` stays visible at the call site — the same
    reason `url_normalize._reject` is written that way.
    """
    detail = WebsiteAlreadyExistsDetail(
        message="You have already added this website",
        website_id=existing.id,
        origin=existing.origin,
    )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        # `mode="json"` so the UUID is serialized to a string here. FastAPI passes an
        # exception's `detail` to the JSON encoder as-is, and a bare `UUID` in a dict is not
        # JSON-serializable — it would turn this 409 into a 500 at render time.
        detail=detail.model_dump(mode="json"),
    )


class WebsiteService:
    """Business logic for websites. Constructed per request from the shared pool."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool
        # Bound to the pool: each read borrows a connection and returns it immediately.
        # Writers are NOT built here — they are constructed inside a `transaction()` block
        # from the connection it yields, so that their statements join that unit of work.
        self._reader = WebsitesReader(pool)

    async def create_website(self, request: CreateWebsiteRequest, user_id: UUID) -> WebsiteResponse:
        """Register a website owned by `user_id`.

        The URL has already been validated by `CreateWebsiteRequest` (a bad one never
        reaches this method — it is a `422` at the boundary); normalizing again here is what
        produces the `origin` to dedupe on. See `schemas.py` for why that is two calls.

        Raises:
            HTTPException: `409`, with `WebsiteAlreadyExistsDetail` as its detail, if this
                user already has a website with this origin.
        """
        normalized = normalize_url(request.url)

        # The common case, and the only one that costs a query: the user is re-adding a site
        # they already have. Checked before opening a transaction, so the overwhelmingly
        # likely 409 never starts a unit of work it would only roll back.
        existing = await self._reader.get_by_user_and_origin(user_id, normalized.origin)
        if existing is not None:
            raise _already_exists(_to_website(existing))

        try:
            async with transaction(self._pool) as tx:
                row = await WebsitesWriter(tx).insert(
                    user_id=user_id,
                    url=normalized.url,
                    origin=normalized.origin,
                    enrich_with_llm=request.enrich_with_llm,
                )
        except UniqueViolationError as error:
            # The check above is not atomic with the insert, so two concurrent POSTs of the
            # same URL can both pass it. The database's UNIQUE index is what actually
            # guarantees uniqueness; this converts the loser of that race into the same 409
            # the sequential case produces, so the client cannot tell the two apart.
            #
            # Caught OUTSIDE the `async with`: the failed INSERT has already aborted the
            # transaction, and `transaction()` has rolled it back and released the
            # connection by the time this runs — the re-read below therefore needs, and
            # gets, a fresh connection from the pool.
            if error.constraint_name != _ORIGIN_UNIQUE_CONSTRAINT:
                raise
            duplicate = await self._reader.get_by_user_and_origin(user_id, normalized.origin)
            if duplicate is None:
                # The conflicting row was deleted between the INSERT failing and this read.
                # Vanishingly unlikely, and there is nothing honest to point the client at,
                # so let the original error surface rather than inventing a 409 with no id.
                raise
            raise _already_exists(_to_website(duplicate)) from None

        return _to_website(row)

    async def list_websites(
        self, include: WebsiteInclude | None = None
    ) -> list[WebsiteListItemResponse]:
        """Return every website from every user, most recently active first.

        **Unscoped, deliberately** — no `user_id` parameter, nothing to filter by, nothing
        to accidentally filter by later (ARCHITECTURE.md §4.1). Every signed-in user browses
        the whole pool; that is the product.

        Args:
            include: `"latest_run"` folds each website's newest run and its schedule
                summary into the response, in the same single query. `None` leaves both
                `null`.
        """
        if include == "latest_run":
            rows = await self._reader.list_all_with_latest_run()
            return [_to_list_item(row) for row in rows]

        rows = await self._reader.list_all()
        return [WebsiteListItemResponse.model_validate(row) for row in rows]

    async def get_website(self, website_id: UUID) -> WebsiteResponse:
        """Return one website by id, for any signed-in caller.

        Takes no `user_id`, because ownership is irrelevant to reading (§4.1). A non-owner
        gets the same `200` and the same body the owner does.

        Raises:
            HTTPException: `404` if there is no website with that id.
        """
        row = await self._reader.get_by_id(website_id)
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)
        return _to_website(row)

    async def update_website(
        self, website_id: UUID, request: UpdateWebsiteRequest, user_id: UUID
    ) -> WebsiteResponse:
        """Change a website the caller owns — today, only its `enrich_with_llm` intent.

        The canonical write path for this codebase (ARCHITECTURE.md §4.2): fetch, then
        `require_owner` with nothing in between, then — and only then — a short transaction.
        Mirrors `delete_website` below exactly, one line longer for the `UPDATE`'s return
        value.

        A change here affects the NEXT run only, never a run already in flight or already
        recorded — this method writes one column on `websites` and nothing on `runs`, so
        that guarantee falls out of what the write touches rather than needing an explicit
        check.

        Raises:
            HTTPException: `404` if there is no website with that id; `403` (from
                `require_owner`) if the caller is not its owner, for the identical reason
                `delete_website` gives — `GET /websites/{id}` already returns this row to
                everyone, so a `404` for a non-owner would be a lie the next request
                disproves. A second, race-window `404` — character-identical to the first —
                if the website is deleted between the fetch above and the `UPDATE` below.
        """
        website = await self.get_website(website_id)  # 404
        require_owner(website, user_id)  # 403 — nothing between these two lines

        async with transaction(self._pool) as tx:
            row = await WebsitesWriter(tx).update(
                website_id, enrich_with_llm=request.enrich_with_llm
            )
        if row is None:
            # Deleted in the race window between the fetch above and this UPDATE.
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND_DETAIL)

        # `enrich_with_llm`'s value is not a secret (ARCHITECTURE.md §9.4 is about keys, not
        # this boolean), so it is safe to log — unlike `Settings.anthropic_api_key`, which
        # nothing in this line touches.
        logger.info("Set enrich_with_llm=%s on website %s", request.enrich_with_llm, website_id)
        return _to_website(row)

    async def delete_website(self, website_id: UUID, user_id: UUID) -> None:
        """Delete a website the caller owns, cascading to its schedule and runs.

        The canonical write path for this codebase: fetch, `require_owner`, then — and only
        then — a short transaction. Nothing but the fetch precedes the ownership check.

        Raises:
            HTTPException: `404` if no such website exists; `403` (from `require_owner`) if
                the caller is not its owner. `403` rather than `404` for a non-owner is
                deliberate — `GET /websites/{id}` already returns this row to everyone, so
                claiming it does not exist would be a lie the next request disproves.
        """
        website = await self.get_website(website_id)
        require_owner(website, user_id)

        async with transaction(self._pool) as tx:
            await WebsitesWriter(tx).delete(website_id)

        # The id is not a secret and not a credential — it appears in the request path — and
        # a deletion that cascades to a website's whole run history is worth a log line.
        logger.info("Deleted website %s", website_id)
