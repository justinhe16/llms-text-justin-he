"""Health check endpoint."""

from fastapi import APIRouter
from pydantic import BaseModel


router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Response body for `GET /health`.

    This DTO lives beside its route rather than in `app/features/`: health is an
    operational endpoint with no data access, not a product feature with a service, a
    reader, and a writer.
    """

    status: str


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness probe.

    Deliberately shallow — it must not touch Postgres, Redis, or any other network
    dependency. Fly polls this every 10 seconds, so a check that fans out to every
    dependency turns one slow dependency into a restart loop across healthy machines.
    The database layer ticket extends this with a `SELECT 1` only on a separate
    `/health/deep` route that nothing automated polls.
    """
    return HealthResponse(status="ok")
