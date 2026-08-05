"""HTTP handlers for `/websites/{id}/runs` and `/runs/{id}`. Thin by contract
(ARCHITECTURE.md §3.2).

Every handler below parses its input, calls exactly one service method, and returns the
result. There is no `if`, no `for`, no SQL, and no second service call anywhere in this
file. The one thing that looks like logic — `parse_cursor` — is a router-level input
*parser*, the same role `id: UUID` already plays for a path parameter: it turns a value
FastAPI cannot validate declaratively (an opaque, feature-owned cursor format) into either
a typed `RunCursor` or a `422`, before any service method runs. It contains no business
rule about runs — it is `decode_cursor` plus one `try`/`except` translating its one
exception type into HTTP.

**Authentication vs. authorization, visible in the signatures — same pattern as
`websites.py`.** Every handler takes `CurrentUserId`, so every endpoint is `401` without a
valid token. Neither handler passes that id to its service, because both endpoints are
reads and reads in this codebase are unscoped by caller (ARCHITECTURE.md §4.1): any
signed-in user may read any website's run history, and any run's full detail including its
`llms_txt`. `user_id` is present purely to require a token and is deliberately never
threaded any further — that is the whole of what makes this feature's tests for "a
non-owner can read this" meaningful rather than vacuous.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.deps import CurrentUserId, DbPool
from app.core.pagination import Page
from app.features.runs.internals.run_cursor import CursorError, RunCursor, decode_cursor
from app.features.runs.schemas import RunDetailResponse, RunListItemResponse, RunStatusName
from app.features.runs.service import RunService
from app.features.websites.service import WebsiteService


router = APIRouter(tags=["runs"])


def get_run_service(pool: DbPool) -> RunService:
    """Build the service for one request from the process-wide pool, mirroring
    `get_website_service` (`app.api.routers.websites`).

    Builds a `WebsiteService` here too, rather than importing the websites router's
    provider function: `RunService.list_runs` calls `WebsiteService.get_website` to 404 on
    an unknown website (ARCHITECTURE.md §3.1 — a feature calls another feature's service,
    never its reader), and the service it calls is wired up right where the rest of this
    request's dependencies are, the same way `get_website_service` wires up
    `WebsitesReader` for its own feature.
    """
    return RunService(pool, WebsiteService(pool))


RunServiceDep = Annotated[RunService, Depends(get_run_service)]

# Spelled as `Annotated` aliases with defaults supplied at the parameter, matching
# `websites.py`'s `IncludeQuery` — it avoids a function call in an argument default
# (ruff's B008), which is a real hazard for mutable defaults and a lint error here either
# way.
CursorQuery = Annotated[
    str | None,
    Query(
        description=(
            "Opaque pagination cursor from a previous page's `next_cursor`. Omit it to "
            "fetch the first page. Treat it as a blob — see `Page.next_cursor`."
        )
    ),
]

StatusQuery = Annotated[
    RunStatusName | None,
    Query(description="Return only runs with this status. An unrecognized value is a 422."),
]

LimitQuery = Annotated[
    int,
    Query(
        ge=1,
        description=(
            "Page size. Values above the maximum (100) are silently clamped to it rather "
            "than rejected; `<= 0` is a 422."
        ),
    ),
]


def parse_cursor(cursor: CursorQuery = None) -> RunCursor | None:
    """Decode `?cursor=` before `RunService` ever sees it, turning a bad one into a `422`.

    A router-level `Depends()`, not a pydantic validator (there is no request body here to
    validate against — `cursor` is a query parameter) and not a service-layer
    `try`/`except` — `RunService.list_runs` takes an already-decoded `RunCursor | None`
    precisely so it stays directly unit-testable without constructing base64 in every test
    (see its docstring).

    The `detail` says only that the cursor is invalid, never what was wrong with it or what
    was submitted. `decode_cursor`'s `CursorError` already withholds the raw value (see its
    module docstring); this is the one place downstream of it that could still leak that
    value into a response, and it deliberately does not.
    """
    if cursor is None:
        return None
    try:
        return decode_cursor(cursor)
    except CursorError as error:
        raise HTTPException(status_code=422, detail="Invalid pagination cursor") from error


CursorDep = Annotated[RunCursor | None, Depends(parse_cursor)]


@router.get("/websites/{id}/runs", response_model=Page[RunListItemResponse])
async def list_runs(
    id: UUID,
    user_id: CurrentUserId,
    service: RunServiceDep,
    cursor: CursorDep,
    status: StatusQuery = None,
    limit: LimitQuery = 25,
) -> Page[RunListItemResponse]:
    """List one website's run history, newest first. Any signed-in user, any website.

    Not filtered by the caller, on purpose (ARCHITECTURE.md §4.1) — `user_id` is present to
    require authentication and is intentionally not passed to the service. `404` if `id`
    names no website; see `RunService.list_runs`.

    The path parameter is `id`, not `website_id`, because the noun is already in the path
    (ARCHITECTURE.md §10.3) — it shadows the `id` builtin inside this function body, which
    is the accepted cost of the route contract, matching `websites.get_website`.
    """
    return await service.list_runs(id, cursor=cursor, status=status, limit=limit)


@router.get("/runs/{id}", response_model=RunDetailResponse)
async def get_run(
    id: UUID,
    user_id: CurrentUserId,
    service: RunServiceDep,
) -> RunDetailResponse:
    """Return one run in full, including `llms_txt` and `storage_path`.

    Any signed-in user may read any run (ARCHITECTURE.md §4.1); `user_id` is present only
    to require a token and is deliberately never passed to the service. `404` if `id` names
    no run.
    """
    return await service.get_run(id)
