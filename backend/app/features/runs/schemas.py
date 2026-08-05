"""Request and response DTOs for the runs feature.

Owns `RunStatusName` and `RunTriggerName` — the two enum-shaped vocabularies read out of
Postgres's `run_status` and `run_trigger` types (db/schema.prisma). `RunStatusName` used to
live in `app.features.websites.schemas`, under a comment marked "THIS IS A TEMPORARY HOME":
`GET /websites?include=latest_run` needed it before this feature existed, and no feature may
import another feature's `internals/`. Now that this module exists, `websites/schemas.py`
imports it from here — a `schemas.py` -> `schemas.py` import is the one cross-feature import
ARCHITECTURE.md §3.1 allows (it prohibits reaching into another feature's `internals/`, not
its public DTOs, and there is no import back the other way — this module imports nothing
from `app.features.websites`). There must never be two copies of either Literal.

These are shapes, not logic (ARCHITECTURE.md §3.1). In particular, `duration_ms` below is
computed in Python rather than derived by a validator on this model — see `service.py`'s
`_shared_fields` for where that actually happens and why it lives there instead of here.
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel


# The four values of the `run_status` Postgres enum. See the module docstring for why this
# is defined here, and only here.
RunStatusName = Literal["pending", "processing", "completed", "failed"]

# The two values of the `run_trigger` Postgres enum — what started a run. A `Literal`, like
# `RunStatusName`, so an unrecognized value fails at the OpenAPI boundary with a 422 that
# names the valid options, rather than leaking a string the frontend has never seen into
# the UI.
RunTriggerName = Literal["manual", "scheduled"]


class RunListItemResponse(BaseModel):
    """One row of `GET /websites/{id}/runs`.

    Deliberately excludes `llms_txt` and `storage_path` — an artifact can be large and no
    list view renders it; `RunDetailResponse` below is the endpoint that returns them.
    """

    id: UUID
    website_id: UUID
    status: RunStatusName
    trigger: RunTriggerName
    started_at: datetime
    completed_at: datetime | None

    duration_ms: int | None
    """`completed_at - started_at` in whole milliseconds; `None` while the run is still in
    flight (`completed_at is None`). Computed in Python, in `service.py`'s row -> DTO
    builder — never in SQL, and never as a value this model derives on its own. Keeping it
    out of SQL is what guarantees it can never end up referenced by an `ORDER BY`; see
    `internals/runs_reader.py`'s module docstring for what a computed sort key would cost
    against the index this feature's pagination depends on."""

    stats: dict[str, Any] | None
    """Raw `runs.stats` jsonb, decoded from the `str` asyncpg hands back for a jsonb
    column. Typed loosely (`dict[str, Any]`) because its shape belongs to the crawler
    milestone, which is not designed yet (ARCHITECTURE.md §3.4) — deliberately not
    modeled, the same choice `websites.schemas.LatestRunSummary` makes for this column."""

    error: str | None


class RunDetailResponse(RunListItemResponse):
    """The full body of `GET /runs/{id}` — every list-item field, plus the two that are too
    large or too rarely needed for a list view.

    A subclass of `RunListItemResponse`, the same relationship `WebsiteListItemResponse`
    has to `WebsiteResponse`: the detail view is the list item plus more, not an
    independently maintained sibling that could drift from it field by field.
    """

    llms_txt: str | None
    storage_path: str | None
