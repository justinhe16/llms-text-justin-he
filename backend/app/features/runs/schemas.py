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

# The three windows `GET /websites/{id}/stats` accepts, and the `date_trunc` field each one
# buckets on. Both `Literal`, not `enum.Enum`, for the same reason as the two above: it
# renders as an OpenAPI enum and an unrecognized value is a 422 that names the valid options.
# See `app.features.runs.internals.stats_window` for the window -> bucket -> step mapping.
StatsWindowName = Literal["1d", "7d", "14d"]
StatsBucketName = Literal["hour", "day"]


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


class TriggerRunResponse(BaseModel):
    """The body of `POST /websites/{id}/runs` on success.

    Deliberately not `RunListItemResponse` or a slice of it: a just-triggered run has no
    `completed_at`, `duration_ms`, `stats`, or `error` worth returning, and reusing that
    model would mean either faking those fields or making them optional on a response that
    is supposed to be the simplest one this feature has.

    Status `202 Accepted`, not `201 Created` (see the router): the row this describes has
    been written, but the crawl it names has only been queued, not completed — `201` would
    claim more than is true the instant this response leaves the server.
    """

    id: UUID
    status: RunStatusName
    """Always `"pending"` on this path — `RunsWriter.insert_manual` inserts with that status
    literally, and nothing between the insert and this response could have changed it. It is
    still a field, and still typed as the full `RunStatusName` rather than
    `Literal["pending"]`, for two reasons: `RunStatusName` is the one vocabulary this feature
    owns for the column (the module docstring above is explicit that there must never be a
    second copy of it), and the client renders this row into the same list `GET
    /websites/{id}/runs` returns, then lets polling take over from there — a component that
    already knows how to render every `RunStatusName` needs no special case for "the one
    value this particular response can have."""

    started_at: datetime
    """The database's `CURRENT_TIMESTAMP`, read back through `RETURNING` — see
    `internals/runs_writer.py` for why this is the only place that value exists."""


class RunAlreadyInFlightDetail(BaseModel):
    """The `detail` of the `409` from `POST /websites/{id}/runs` when that website already
    has a run in progress.

    Carries the existing run's id and status, the same shape `WebsiteAlreadyExistsDetail`
    (`app.features.websites.schemas`) carries the existing website's id: a 409 that hands
    back nothing actionable makes the client build a second request just to find out what it
    collided with.
    """

    code: Literal["run_already_in_flight"] = "run_already_in_flight"
    """A stable machine-readable discriminator. The frontend branches on this, never on
    `message`, which is free to be reworded."""

    message: str
    run_id: UUID
    status: RunStatusName
    """`"pending"` or `"processing"` — never anything else, since this is only ever built
    from `RunsReader.get_active_for_website`'s result, which is scoped to those two values.
    Lets the UI say which: a run still `pending` has not started crawling yet, and a caller
    deciding whether to keep waiting cares about the difference."""


class RunAlreadyInFlightResponse(BaseModel):
    """The **whole** `409` body, envelope included — see `WebsiteAlreadyExistsResponse` for
    why the envelope is a separate model from its `detail` (FastAPI wraps `detail` in a JSON
    object, so documenting the inner model directly in `responses=` would describe a shape no
    actual response has)."""

    detail: RunAlreadyInFlightDetail


class RunLimitExceededDetail(BaseModel):
    """The `detail` of the `429` from `POST /websites/{id}/runs` — either abuse cap.

    `scope` is the discriminator a client branches on; `message` is prose for a human and is
    free to be reworded without breaking a client that reads `scope` and `limit` instead.
    """

    code: Literal["run_limit_exceeded"] = "run_limit_exceeded"
    scope: Literal["concurrent", "daily"]
    """Which of the two caps in `app.core.settings.Settings` was hit —
    `max_concurrent_runs_per_user` or `max_runs_per_day_per_user`. Never inferred from
    `message`'s wording, which `run_limits.py`'s docstring reserves the right to reword."""

    message: str
    limit: int
    resets_at: datetime | None
    """The instant the cap is expected to free a slot, or `None` for the concurrency cap.

    The two caps are not symmetric here, and that asymmetry is the point rather than an
    oversight. The daily cap has a real, computable instant: the oldest run inside the
    rolling 24h window ages out of it at a specific time, and `RunService` derives
    `resets_at` from exactly that row. The concurrency cap has no such instant — it frees
    the moment any of the caller's in-flight manual runs completes, and nothing in this
    codebase can predict when a crawl in progress will finish. Inventing a `resets_at` for
    that case would be a more misleading answer than admitting there is none, so it is
    `None` there and nowhere else. See `run_limits.concurrency_limit_message`.
    """


class RunLimitExceededResponse(BaseModel):
    """The **whole** `429` body, envelope included — same reasoning as
    `RunAlreadyInFlightResponse` above."""

    detail: RunLimitExceededDetail


class RunStatsPoint(BaseModel):
    """One bucket of `GET /websites/{id}/stats`'s `series`.

    Every bucket in the requested window appears exactly once, in order — including buckets
    with zero runs, which report zeroes rather than being omitted (`RunsReader.
    website_stats`'s `_WEBSITE_STATS` zero-fills them with `generate_series`). `avg_pages`
    and `avg_duration_ms` are therefore never `null`: see `service.py`'s `_to_stats` for the
    rounding that turns SQL's already-zero-filled averages into these two fields.
    """

    t: datetime
    """This bucket's start, UTC. The field is named `t`, not `bucket_start`, because it is
    the ticket's own wire name."""

    runs: int
    """Every run that started in this bucket, regardless of status — pending, processing,
    completed, or failed all count here."""

    completed: int
    failed: int

    avg_pages: float
    """Mean `pages_crawled` (from `runs.stats`) over every run in this bucket, completed or
    not — a run with no usable stats contributes `0`, not a gap in the denominator. See
    `internals/runs_reader.py`'s `_WEBSITE_STATS` for why this average and `avg_duration_ms`
    below use different populations."""

    avg_duration_ms: int
    """Mean duration over only this bucket's COMPLETED runs — failed and in-flight runs have
    no meaningful duration and are excluded from the denominator, not counted as zero. An
    `int`: sub-millisecond precision on an averaged crawl duration is noise."""


class RunStatsTotals(BaseModel):
    """The whole-window summary alongside `series` in `GET /websites/{id}/stats`.

    Field names are `completed`/`failed`, matching `RunStatsPoint` above — not
    `total_completed`/`total_failed` — because they are still counts of completed/failed
    runs, merely summed over the whole window instead of one bucket.
    """

    total_runs: int
    completed: int
    failed: int

    success_rate: float | None
    """`completed / total_runs`, rounded, or `None` — never `0.0` — when `total_runs == 0`.
    A website with no runs yet has no success rate to report; `0.0` would misreport it as
    100% failure."""

    avg_duration_ms: int
    """Same population rule as `RunStatsPoint.avg_duration_ms`, over the whole window."""

    avg_pages: float

    last_run_at: datetime | None
    """The most recent `started_at` in the window, or `None` — never a fabricated
    value — when `total_runs == 0`."""


class WebsiteStatsResponse(BaseModel):
    """The full body of `GET /websites/{id}/stats`.

    `window` and `bucket` echo the request back (`bucket` is derived from `window`, never
    supplied by the caller — see `internals/stats_window.resolve_window`), so a client that
    only stores the response still knows what it is looking at.
    """

    window: StatsWindowName
    bucket: StatsBucketName
    series: list[RunStatsPoint]
    totals: RunStatsTotals
