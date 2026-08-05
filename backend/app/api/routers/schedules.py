"""HTTP handlers for `/websites/{id}/schedule`. Thin by contract (ARCHITECTURE.md §3.2).

Every handler below parses its input, calls exactly one service method, and returns the
result. There is no `if`, no `for`, no SQL, and no second service call anywhere in this file,
mirroring `api/routers/websites.py` exactly.

**Authentication vs. authorization, visible in the signatures.** Every handler takes
`CurrentUserId`, so every endpoint is `401` without a valid token. Only `upsert_schedule`
passes that id on to the service, because only the write cares who is asking (ARCHITECTURE.md
§4). `get_schedule` takes the dependency purely to require a token and then deliberately
ignores it — reads are authenticated and unscoped, and threading a `user_id` into a read
service method is how that rule gets broken.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from app.api.deps import CurrentUserId, DbPool
from app.features.schedules.schemas import ScheduleResponse, UpsertScheduleRequest
from app.features.schedules.service import ScheduleService


router = APIRouter(tags=["schedules"])


def get_schedule_service(pool: DbPool) -> ScheduleService:
    """Build the service for one request from the process-wide pool.

    Feature-specific wiring lives beside the feature's routes, exactly like
    `get_website_service` in `api/routers/websites.py` — see that function's docstring for
    why this does not live in `app.api.deps` instead.
    """
    return ScheduleService(pool)


ScheduleServiceDep = Annotated[ScheduleService, Depends(get_schedule_service)]


@router.get("/websites/{id}/schedule", response_model=ScheduleResponse | None)
async def get_schedule(
    id: UUID,
    user_id: CurrentUserId,
    service: ScheduleServiceDep,
) -> ScheduleResponse | None:
    """Return one website's schedule. Any signed-in user may read any website's schedule.

    `200` with a `null` body when the website exists but has no schedule yet — that is a
    normal state, not an error. `404` is reserved for an unknown *website*; a missing
    schedule never produces one.

    The path parameter is `id`, not `website_id`, because the noun is already in the path
    (ARCHITECTURE.md §10.3), matching `get_website` in `api/routers/websites.py`.
    """
    return await service.get_schedule(id)


@router.put("/websites/{id}/schedule", response_model=ScheduleResponse)
async def upsert_schedule(
    id: UUID,
    body: UpsertScheduleRequest,
    user_id: CurrentUserId,
    service: ScheduleServiceDep,
) -> ScheduleResponse:
    """Create or replace the schedule for a website the caller owns.

    Always `200`, never a conditional `201` for the create case. Distinguishing "this was a
    create" from "this was an update" would require a branch in this handler — checking
    whether a schedule already existed — and CLAUDE.md #6 forbids exactly that: a router
    parses input, calls one service method, and returns. The ticket's own wording ("200/201")
    permits either; `200` is the one that keeps this file free of logic.

    `403`, not `404`, when the caller is not the website's owner — `GET /websites/{id}`
    already returns that website to every signed-in user, so hiding its existence here would
    be inconsistent with the endpoint next to it (ARCHITECTURE.md §4).
    """
    return await service.upsert_schedule(id, body, user_id)
