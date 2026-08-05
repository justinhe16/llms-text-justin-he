"""HTTP handlers for `/websites`. Thin by contract (ARCHITECTURE.md §3.2).

Every handler below parses its input, calls exactly one service method, and returns the
result. There is no `if`, no `for`, no SQL, and no second service call anywhere in this
file, and there should never be — the moment a handler needs one of those, the logic belongs
in `WebsiteService`.

**Authentication vs. authorization, visible in the signatures.** Every handler takes
`CurrentUserId`, so every endpoint is `401` without a valid token. Only `delete_website`
passes that id on to the service, because only writes care who is asking (ARCHITECTURE.md
§4). `list_websites` and `get_website` take the dependency purely to require a token and
then deliberately ignore it — reads are authenticated and unscoped, and a `user_id` argument
threaded into a read service method is how that rule gets broken.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUserId, DbPool
from app.features.websites.schemas import (
    CreateWebsiteRequest,
    WebsiteAlreadyExistsResponse,
    WebsiteInclude,
    WebsiteListItemResponse,
    WebsiteResponse,
)
from app.features.websites.service import WebsiteService


router = APIRouter(tags=["websites"])


def get_website_service(pool: DbPool) -> WebsiteService:
    """Build the service for one request from the process-wide pool.

    Feature-specific wiring lives beside the feature's routes rather than in
    `app.api.deps`, which stays generic (settings, the pool, the current user) — otherwise
    that module grows an import of every feature in the codebase. `api` importing from
    `features` is the allowed direction either way (ARCHITECTURE.md §3.1).

    The service is cheap to construct — it stores a pool reference and a reader — so this
    builds a new one per request instead of caching a singleton that would outlive a
    `dependency_overrides[get_db_pool]` swap in a test.
    """
    return WebsiteService(pool)


WebsiteServiceDep = Annotated[WebsiteService, Depends(get_website_service)]

# Spelled as an `Annotated` alias with the default supplied at the parameter, rather than
# `include: WebsiteInclude | None = Query(default=None, ...)`. Both work in FastAPI; only
# this one avoids a function call in an argument default (ruff's B008), which is a real
# hazard for mutable defaults and a lint error here regardless.
IncludeQuery = Annotated[
    WebsiteInclude | None,
    Query(
        description=(
            "Set to `latest_run` to fold each website's most recent run and its schedule "
            "summary into the response. Omitted, both fields are null."
        )
    ),
]


@router.post(
    "/websites",
    response_model=WebsiteResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        status.HTTP_409_CONFLICT: {
            "model": WebsiteAlreadyExistsResponse,
            "description": (
                "You have already added a website with this origin. The response carries "
                "the existing website's id so the client can navigate to it."
            ),
        }
    },
)
async def create_website(
    body: CreateWebsiteRequest,
    user_id: CurrentUserId,
    service: WebsiteServiceDep,
) -> WebsiteResponse:
    """Register a website, owned by the caller.

    The URL is validated by `CreateWebsiteRequest` before this runs, so a malformed one is a
    `422` that never reaches the service.
    """
    return await service.create_website(body, user_id)


@router.get("/websites", response_model=list[WebsiteListItemResponse])
async def list_websites(
    user_id: CurrentUserId,
    service: WebsiteServiceDep,
    include: IncludeQuery = None,
) -> list[WebsiteListItemResponse]:
    """List **every** website, from every user, most recently active first.

    Not filtered by the caller, on purpose: this application is not multi-tenant and
    browsing everyone's sites is the product (ARCHITECTURE.md §4.1). `user_id` is present to
    require authentication and is intentionally not passed to the service.
    """
    return await service.list_websites(include)


@router.get("/websites/{id}", response_model=WebsiteResponse)
async def get_website(
    id: UUID,
    user_id: CurrentUserId,
    service: WebsiteServiceDep,
) -> WebsiteResponse:
    """Return one website. Any signed-in user may read any website; `404` if unknown.

    The path parameter is `id`, not `website_id`, because the noun is already in the path
    (ARCHITECTURE.md §10.3). It shadows the `id` builtin inside this function body, which is
    the accepted cost of the route contract — nothing here calls `id()`.
    """
    return await service.get_website(id)


@router.delete("/websites/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_website(
    id: UUID,
    user_id: CurrentUserId,
    service: WebsiteServiceDep,
) -> None:
    """Delete a website the caller owns, along with its schedule and runs.

    `403`, not `404`, when the caller is not the owner: `GET /websites/{id}` already returns
    this row to every signed-in user, so hiding its existence here would be inconsistent
    with the endpoint next to it (ARCHITECTURE.md §4, `app.core.auth.ownership`).

    Returns `204` with no body — hence no `response_model` and a `None` return.
    """
    await service.delete_website(id, user_id)
