"""Request and response DTOs for the schedules feature.

These are shapes, not logic (ARCHITECTURE.md §3.1). The one rule worth calling out before the
models themselves: `auto_publish` never appears here, in either direction. See
`ScheduleResponse` for why.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


# The four UI presets this product ships, in minutes: hourly, 6-hourly, daily, weekly.
# ARCHITECTURE.md §6.4 explains why the column is a plain integer rather than a cron
# expression — an interval is "last_run_at + interval", one indexed range scan, and every
# interval a user could want, including one cron cannot express without enumerating (e.g.
# "every 90 minutes"), is just a number.
#
# A `Literal` rather than a bare `int` (mirroring `RunStatusName` / `WebsiteInclude` in
# `websites/schemas.py`) for two reasons: it renders as `"enum": [60, 360, 1440, 10080]` in
# the OpenAPI schema, so the client PER-158 generates from this document gets the allowed
# values for free instead of a bare `integer`; and pydantic's own 422 for a rejected value
# already lists every member, so there is nothing to hand-write for "which intervals are
# valid" — the type itself is the validation and the documentation.
ScheduleIntervalMinutes = Literal[60, 360, 1440, 10080]


class UpsertScheduleRequest(BaseModel):
    """Body of `PUT /websites/{id}/schedule`.

    Both fields are required — there is no partial update here. A schedule has exactly two
    knobs a caller controls (`active`, `interval_minutes`); `next_run_at` and `last_run_at`
    are derived or owned elsewhere (see `ScheduleResponse`), so there is nothing left for a
    `PATCH`-style optional field to mean.

    Deliberately **not** `model_config = ConfigDict(extra="forbid")` — the rest of this
    codebase's request models do not set it either. An `auto_publish` key in the body is
    silently ignored rather than rejected; `tests/test_schedules_api.py` proves that ignoring
    it does not mean writing it — the column this API never touches stays whatever it already
    was.
    """

    active: bool = Field(..., description="Whether this schedule should run automatically.")

    interval_minutes: ScheduleIntervalMinutes = Field(
        ...,
        description=(
            "Minutes between runs. One of the four supported presets: 60 (hourly), "
            "360 (6-hourly), 1440 (daily), or 10080 (weekly)."
        ),
    )


class ScheduleResponse(BaseModel):
    """One website's schedule, as both schedules endpoints return it.

    **`auto_publish` MUST NOT appear here, in either direction.** The column is reserved and
    unused in this milestone (its comment in `db/schema.prisma` says so directly): no code
    reads or writes it yet, and it exists only so that turning on publish-on-success later is
    a behaviour change rather than a migration on a table the cron tick is actively scanning.
    Exposing it now — even read-only — would let a client start depending on a value nothing
    in this system ever sets to anything but its default, and would give this API two things
    to keep in sync the day `auto_publish` does become real.

    `interval_minutes` here is a plain `int`, **not** `ScheduleIntervalMinutes`. The allowlist
    on the request is an input guardrail — "these are the presets this product offers right
    now" — and the ticket that defined it notes arbitrary intervals may be allowed later
    without a migration. A response model that re-validated the same allowlist on the way out
    would turn any row outside it (a legacy value, a hand-seeded row, a future relaxation that
    ships before this model is updated) into a `500` on a plain `GET` — the read path failing
    for a reason that has nothing to do with reading.

    `last_run_at` **is** exposed, read-only. It is not one of the two knobs
    `UpsertScheduleRequest` accepts, and this API never writes it — only the cron tick and the
    run pipeline (neither built yet) ever will. Surfacing it here is what lets a settings page
    show "last ran 3 hours ago" without a second endpoint.
    """

    id: UUID
    website_id: UUID
    active: bool
    interval_minutes: int
    next_run_at: datetime | None
    last_run_at: datetime | None
