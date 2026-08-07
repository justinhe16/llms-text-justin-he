"""HTTP handlers for `/websites/{id}/runs`, `/runs/{id}`, `/runs/{id}/llms.txt`,
`/runs/{id}/llms-full.txt`, and `/websites/{id}/stats`. Thin by contract (ARCHITECTURE.md
§3.2).

Every handler below parses its input, calls exactly one service method, and returns the
result. There is no `if`, no `for`, no SQL, and no second service call anywhere in this
file. The one thing that looks like logic — `parse_cursor` — is a router-level input
*parser*, the same role `id: UUID` already plays for a path parameter: it turns a value
FastAPI cannot validate declaratively (an opaque, feature-owned cursor format) into either
a typed `RunCursor` or a `422`, before any service method runs. It contains no business
rule about runs — it is `decode_cursor` plus one `try`/`except` translating its one
exception type into HTTP. `WindowQuery`, `get_website_stats`'s query parameter, needs no
such parser: `StatsWindowName` is a `Literal`, so FastAPI itself turns an unrecognized
`?window=` into a `422` declaratively. `_artifact_response`, below the two artifact
handlers, is a second router-level *presentation* helper of the same kind — it decides no
business rule about runs either, only how a `RunArtifact` becomes an HTTP response.

**Authentication vs. authorization, visible in the signatures — same pattern as
`websites.py`.** Every handler takes `CurrentUserId`, so every endpoint is `401` without a
valid token. `trigger_run` is the one handler that passes that id on to its service, because
it is the one WRITE in this router and writes are ownership-checked — `RunService.trigger_run`
hands it straight to `require_owner` (ARCHITECTURE.md §4.2). The five reads — `list_runs`,
`get_run`, `get_run_llms_txt`, `get_run_llms_full_txt`, and `get_website_stats` — all still
take `CurrentUserId` purely to require a token and deliberately never thread it any further,
because reads in this codebase are unscoped by caller (ARCHITECTURE.md §4.1): any signed-in
user may read any website's run history, any run's full detail including its `llms_txt`, any
run's two generated artifacts (PER-181), and any website's run statistics. That asymmetry is
the authorization contract in miniature, and it is what makes this feature's tests for "a
non-owner can read this, but cannot trigger this" meaningful rather than vacuous.

`GET /websites/{id}/stats` lives here rather than in a new `stats` feature module because
the aggregate it runs reads exactly one table, `runs`, which this feature already owns —
see `app.features.runs.service.RunService.get_website_stats` for the full reasoning.

**`get_run_llms_txt` and `get_run_llms_full_txt` return a `Response`, not a Pydantic model,
and that is intentional rather than a crack in "routers are thin."** Each handler is still
exactly one call to one service method — `return _artifact_response(await
service.get_llms_txt(id))` — with the response-shaping factored into the one helper both
share, the same way `parse_cursor` factors out the one piece of input-shaping this router
does. See `_artifact_response`'s own docstring for the `PlainTextResponse` and
`Content-Disposition` details, and Decision C of PER-181's implementation notes for why
`response_class=PlainTextResponse` is what makes `openapi.json` declare `text/plain` here
instead of an empty `application/json` schema.

**What these two routes do NOT do, on purpose: `Content-Disposition` reaches a browser only
when nothing sits between it and FastAPI.** `frontend/app/api/[...path]/route.ts`'s BFF proxy
(ARCHITECTURE.md §8.1) forwards only `content-type` from the upstream response today, so the
header these two handlers set is currently dropped before it reaches the browser. That gap is
deliberate, documented, and owned by the frontend download ticket that wires a UI up to these
routes — it is not fixed here.
"""

from typing import Annotated, Any, Final
from uuid import UUID

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import PlainTextResponse

from app.api.deps import CurrentUserId, DbPool, SettingsDep, get_arq_pool
from app.core.pagination import Page
from app.features.runs.internals.run_cursor import CursorError, RunCursor, decode_cursor
from app.features.runs.schemas import (
    RunAlreadyInFlightResponse,
    RunDetailResponse,
    RunLimitExceededResponse,
    RunListItemResponse,
    RunStatusName,
    StatsWindowName,
    TriggerRunResponse,
    WebsiteStatsResponse,
)
from app.features.runs.service import RunArtifact, RunService
from app.features.websites.service import WebsiteService


router = APIRouter(tags=["runs"])

# `list_runs` below has its own parameter named `status` (the `?status=` query filter),
# which shadows this import — but only inside that function's body. The two `@router...`
# decorators, and every `status.HTTP_*` reference in this module's `responses=` and
# `status_code=` keyword arguments, are evaluated at MODULE scope, where the name still
# resolves to `fastapi.status`. This looks like a collision on a quick read and is not one.


def get_run_service(pool: DbPool, settings: SettingsDep) -> RunService:
    """Build the service for one request from the process-wide pool, mirroring
    `get_website_service` (`app.api.routers.websites`).

    Builds a `WebsiteService` here too, rather than importing the websites router's
    provider function: `RunService.list_runs` and `.trigger_run` both call
    `WebsiteService.get_website` to 404 on an unknown website (ARCHITECTURE.md §3.1 — a
    feature calls another feature's service, never its reader), and the service it calls is
    wired up right where the rest of this request's dependencies are, the same way
    `get_website_service` wires up `WebsitesReader` for its own feature.

    `settings` arrives through `SettingsDep` rather than being read from the module
    singleton inside `RunService`, so that `dependency_overrides[get_settings]` can lower
    the two abuse caps in a test — see `RunService.__init__`. This is the first feature
    service in the codebase to need configuration at all; every one that follows should
    take it the same way.
    """
    return RunService(pool, WebsiteService(pool), settings)


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

WindowQuery = Annotated[
    StatsWindowName,
    Query(description="Time span to aggregate over. One of 7d, 30d, 90d; anything else is a 422."),
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

# The `detail` of the `503` this router answers with when there is no queue to enqueue onto
# at all — as opposed to `RunService`'s own `503`, raised after a queue pool was obtained but
# `enqueue_job` still failed. Deliberately the same shape of message (generic, no mention of
# Redis — ARCHITECTURE.md §9.4): a client cannot act differently on "there was never a pool"
# versus "there was a pool and the call failed", so there is no reason to make it try.
_QUEUE_UNAVAILABLE_DETAIL = "Could not start this run right now. Please try again shortly."


def require_queue_pool() -> ArqRedis:
    """Turn `app.api.deps.get_arq_pool`'s `RuntimeError` into this route's `503`.

    `get_arq_pool`'s own docstring names exactly this obligation: "a route that enqueues has
    to decide what to do about it — the first one to exist should catch this and answer
    503." `POST /websites/{id}/runs` is that first route, which is also why this function
    lives here and not in `app.api.deps`, which stays generic (ARCHITECTURE.md §3.1's
    reasoning for `get_website_service` living beside its own routes applies identically
    here). When a second feature enqueues, this moves to `app.api.deps` and both routes
    depend on it there.

    **Must call `get_arq_pool()` inside this function's own `try`, never take it as a
    declared parameter (`pool: ArqPool`).** FastAPI resolves every sub-dependency before it
    calls the dependant that needs them, so a declared `ArqPool` parameter would let
    `get_arq_pool`'s `RuntimeError` propagate BEFORE this function's body — where the
    `except` lives — ever ran, surfacing as an unhandled `500` instead of the `503` this
    exists to produce. Calling the plain function directly, from inside this function, is
    the entire reason this dependency is shaped as a zero-argument function rather than a
    thin wrapper around `Depends(get_arq_pool)`.

    **Why the two read endpoints below do not take this dependency at all.** The API boots
    without Redis on purpose (`app.main.lifespan`), and `GET /runs/{id}` /
    `GET /websites/{id}/runs` must keep answering while Redis is down — a queue outage is
    not a database outage. That is also why the queue is a per-ROUTE dependency here rather
    than a constructor argument on `RunService`: `get_run_service` above is shared by both
    reads and this one write, and threading a queue pool through its constructor would make
    every read's service construction depend on Redis being reachable — exactly the
    coupling `get_optional_arq_pool` and `/health`'s two-tier reporting exist to avoid
    everywhere else in this codebase.
    """
    try:
        return get_arq_pool()
    except RuntimeError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_QUEUE_UNAVAILABLE_DETAIL
        ) from error


QueuePoolDep = Annotated[ArqRedis, Depends(require_queue_pool)]


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


# Documented by hand rather than with a `"model"` key, unlike `trigger_run`'s 409/429 below.
# On a route whose `response_class` is not JSON, FastAPI files an additional response's model
# under THAT route's media type (`route_response_media_type or "application/json"`,
# fastapi/openapi/utils.py) — so `{"model": ...}` here would document these 404s as
# `text/plain`, which is a lie: FastAPI's own `HTTPException` handler answers them as
# `application/json`. Verified against the pinned FastAPI by dumping `app.openapi()`.
_ARTIFACT_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    status.HTTP_404_NOT_FOUND: {
        "description": (
            "Either there is no run with that id, or that run has not produced this "
            "artifact — it is still pending or processing, or it failed. The two cases "
            "answer with different `detail` strings; the status is the same because from a "
            "client's side both mean there is nothing at this URL."
        ),
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": {"detail": {"type": "string"}}}
            }
        },
    }
}


def _artifact_response(artifact: RunArtifact) -> Response:
    """Turn one `RunArtifact` into the downloadable body both handlers below return.

    A router-level presentation helper, like `parse_cursor` above — no business rule about
    runs is decided here, and it exists so the exact `Content-Disposition` spelling lives in
    ONE place rather than being retyped in two handlers that must not drift.

    `PlainTextResponse` supplies `content-type: text/plain; charset=utf-8` on its own:
    Starlette appends `; charset=utf-8` to any `text/*` media type that does not already name
    a charset. Spelling the charset out here would change nothing at runtime and would NOT
    change what `openapi.json` says, which is read off the response CLASS, not the instance.

    `filename` needs no quote-escaping and no RFC 5987 `filename*` companion:
    `artifact_filename` has already reduced it to `[a-z0-9-]` plus one `.txt`, so it cannot
    contain a quote, a semicolon, a CR, or an LF. That is the security argument for
    sanitizing an attacker-influenceable value (a website's origin is whatever a user typed)
    server-side rather than echoing it into a header.
    """
    return PlainTextResponse(
        artifact.content,
        headers={"Content-Disposition": f'attachment; filename="{artifact.filename}"'},
    )


@router.get(
    "/runs/{id}/llms.txt",
    response_class=PlainTextResponse,
    responses=_ARTIFACT_RESPONSES,
)
async def get_run_llms_txt(
    id: UUID,
    user_id: CurrentUserId,
    service: RunServiceDep,
) -> Response:
    """Download one run's `llms.txt` index as `text/plain`. Any signed-in user, any run.

    Not filtered by the caller, on purpose (ARCHITECTURE.md §4.1) — `user_id` is present to
    require authentication and is intentionally not passed to the service. `404` if `id`
    names no run, or if that run has not produced this artifact — see
    `RunService.get_llms_txt` for the two distinguishable `detail`s.

    `-> Response`, not `-> str`: this handler's declared contract is that it returns an HTTP
    response it built itself, not a value FastAPI should serialize — see `response_class`
    above and this router's module docstring for why that is not a "routers are thin"
    violation.
    """
    return _artifact_response(await service.get_llms_txt(id))


@router.get(
    "/runs/{id}/llms-full.txt",
    response_class=PlainTextResponse,
    responses=_ARTIFACT_RESPONSES,
)
async def get_run_llms_full_txt(
    id: UUID,
    user_id: CurrentUserId,
    service: RunServiceDep,
) -> Response:
    """Download one run's `llms-full.txt` expansion as `text/plain`. Identical contract to
    `get_run_llms_txt` above, including both `404`s — see `RunService.get_llms_full_txt`."""
    return _artifact_response(await service.get_llms_full_txt(id))


@router.get("/websites/{id}/stats", response_model=WebsiteStatsResponse)
async def get_website_stats(
    id: UUID,
    user_id: CurrentUserId,
    service: RunServiceDep,
    window: WindowQuery = "30d",
) -> WebsiteStatsResponse:
    """Return one website's run statistics over `window`, bucketed and zero-filled, plus a
    whole-window summary. Any signed-in user, any website.

    Not filtered by the caller, on purpose (ARCHITECTURE.md §4.1) — `user_id` is present to
    require authentication and is intentionally not passed to the service. `404` if `id`
    names no website; an unrecognized `window` is a native `422` (`StatsWindowName` is a
    `Literal`, so FastAPI rejects it before this handler runs).

    The path parameter is `id`, not `website_id`, matching `list_runs` above
    (ARCHITECTURE.md §10.3).
    """
    return await service.get_website_stats(id, window=window)


@router.post(
    "/websites/{id}/runs",
    response_model=TriggerRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": RunAlreadyInFlightResponse,
            "description": (
                "This website already has a pending or processing run — of any trigger, "
                "manual or scheduled. The response carries that run's id and status."
            ),
        },
        status.HTTP_429_TOO_MANY_REQUESTS: {
            "model": RunLimitExceededResponse,
            "description": (
                "The caller is over their concurrent- or daily-run cap. `detail.scope` "
                "says which; `detail.resets_at` is set only for the daily cap."
            ),
        },
    },
)
async def trigger_run(
    id: UUID,
    user_id: CurrentUserId,
    service: RunServiceDep,
    queue: QueuePoolDep,
) -> TriggerRunResponse:
    """Start a manual crawl of one website. Owner-only — unlike this module's reads.

    `202 Accepted`, not `201 Created`: the body describes a `pending` run that has been
    queued, not one that has finished (see `TriggerRunResponse`). Unlike `list_runs` and
    `get_run`, `user_id` IS passed to the service here, because this is the one write in
    this router and writes are ownership-checked (ARCHITECTURE.md §4.2).

    Four things can stop this short of a `202`: `404` if `id` names no website; `403` if the
    caller does not own it; `409` if that website already has a run in flight; `429` if the
    caller is over their concurrent- or daily-run cap. A `503` is also possible, if the run
    was created but could not be queued — see `RunService.trigger_run` for exactly which
    check produces which, and `require_queue_pool` above for the one `503` this router
    itself can raise before the service even runs.
    """
    return await service.trigger_run(id, user_id, queue)
