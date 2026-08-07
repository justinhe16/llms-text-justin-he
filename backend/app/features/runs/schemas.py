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

    runs_compared: int
    """How many of this bucket's COMPLETED runs carry an `index_diff` block — i.e. were
    actually compared against a previous index (PER-193). `0` is a real count: either this
    bucket had no completed runs, or none of them recorded a comparison (a version-5 row, or
    a run whose read of the previous index failed). Never `null` — this is a count, exactly
    like `runs`/`completed`/`failed` above, not a measurement that can be unknown."""

    pages_added: int
    """Summed `index_diff.pages_added` over this bucket's compared runs. `0` is real —
    either `runs_compared == 0` or every compared run happened to add nothing."""

    pages_removed: int
    """Summed `index_diff.pages_removed` over this bucket's compared runs. Same zero rule as
    `pages_added`."""

    index_pages: int | None
    """The generated index's page count (`runs.stats["links_emitted"]`) of the LAST
    completed run in this bucket that recorded one — never a sum or an average across the
    bucket, since an index size is a snapshot, not a quantity buckets accumulate. **`null`,
    never `0`, when no completed run in this bucket recorded one** — a bucket where nothing
    finished has an UNKNOWN index size, not a zero one. Contrast `pages_added`/
    `pages_removed` above, whose `0` is a real measurement: those are things that happened
    (or did not) in the bucket, this is a state the bucket may simply have no evidence
    about."""

    index_bytes: int | None
    """`runs.stats["llms_txt_bytes"]` of that same last completed run — same null-vs-zero
    rule as `index_pages`, and always from the SAME run as `index_pages` (never two
    different runs' numbers paired together)."""


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

    runs_compared: int
    """`RunStatsPoint.runs_compared`, summed over the whole window. `0` is real — see that
    field's own docstring."""

    pages_added: int
    """`RunStatsPoint.pages_added`, summed over the whole window."""

    pages_removed: int
    """`RunStatsPoint.pages_removed`, summed over the whole window."""


class IndexPageRef(BaseModel):
    """One page in a sample list (`RunIndexDiff.added_sample` and its two siblings) — just
    enough to link to it and label it, never the full `IndexEntry` `internals/index_diff.py`
    builds for itself."""

    url: str
    title: str | None
    """`None` only in principle — `internals/index_diff.py`'s `IndexEntry.title` docstring
    explains why a bullet `generate_llms_txt` actually wrote in practice always has one."""


class RunIndexDiff(BaseModel):
    """What changed in the latest run's index, compared with the previous completed run's —
    the API shape of `internals/index_diff.py`'s `build_index_diff`'s `"compared"` dict.
    Present on `LatestRunSnapshot.diff` if and only if `diff_state == "compared"`; see that
    field's own docstring for why every other state carries `diff: None` instead of this
    model with its numeric fields zeroed out.
    """

    compared_to_run_id: UUID
    compared_to_completed_at: datetime | None
    """When the compared-to run completed. Typed optional to match the column it is read
    from; a `completed` row has one in practice (see `PreviousCompletedIndex.completed_at`,
    `runs/service.py`)."""

    pages_added: int
    pages_removed: int
    pages_changed: int
    """Same URL (by `internals/index_diff.py`'s normalized `key`) on both sides, with a
    different title or a different description — see `build_index_diff`'s own docstring for
    the exact rule."""

    added_sample: list[IndexPageRef]
    removed_sample: list[IndexPageRef]
    changed_sample: list[IndexPageRef]
    """Each capped at `internals/index_diff.py`'s `SAMPLE_LIMIT` (10) — the corresponding
    `pages_*` count above is the TRUE total, always present beside a sample that may be
    shorter than it, so a client never has to say "and more" without a number."""

    urls_discovered_delta: int | None
    """`None` — never a number computed against a value that was not really there — when the
    previous run's `urls_discovered` predates `RUN_STATS_VERSION` 4 or was otherwise
    unreadable. See `internals/index_diff.py`'s `build_index_diff`."""

    sections_delta: dict[str, int]
    """Only the sections whose entry count actually changed between the two runs, key-sorted
    — a section absent from this dict had the same count on both sides, not a section this
    endpoint forgot to report on. See `internals/index_diff.py`'s `_sections_delta`."""

    selection_churn: int
    """`pages_added + pages_removed`."""

    selection_churn_ratio: float | None
    """`selection_churn` divided by the union of both runs' page counts, rounded to four
    decimal places. `None` — never `0.0` — when that union is empty (both indexes have no
    pages at all; a ratio over zero pages describes nothing)."""

    llms_txt_bytes_delta: int
    """This run's `llms_txt_bytes` minus the previous run's, in UTF-8 bytes. Can be
    negative."""


class LatestRunSnapshot(BaseModel):
    """The newest completed run inside the requested `window`, and what changed in its index
    — the Output tab's "what changed in the latest run" panel reads this, not `series`, which
    is why it is not itself a `RunStatsPoint`.

    **Window-scoped, like `RunStatsTotals.last_run_at`.** A completed run older than the
    window does not surface here even if it is the website's most recent run overall — the
    same rule `last_run_at` already follows, stated explicitly here because a client reading
    only this field (and not also checking the window) could otherwise mistake "no completed
    run in the last 24 hours" for "this website has never completed a run."
    """

    run_id: UUID
    completed_at: datetime | None
    """Typed optional to match the column; a `completed` row has one in practice."""

    index_pages: int | None
    """This run's own `runs.stats["links_emitted"]` — `None` only if that key is somehow
    unreadable on an otherwise-completed row, never a fabricated `0`."""

    index_bytes: int | None
    """This run's own `runs.stats["llms_txt_bytes"]`, same null rule as `index_pages`."""

    diff_state: Literal["compared", "first_run", "not_recorded"]
    """The discriminator a client branches on — never on whether `diff` is `null`, and never
    on whether any individual count is `0`, which is why every state below gets a name rather
    than being inferred from `diff`/`previous_run_completed` alone:

    * `"compared"` — `diff` is present and describes a real comparison.
    * `"first_run"` — this run had nothing to compare against: either it is the website's
      very first run (`previous_run_completed is None`), or every earlier run failed
      (`previous_run_completed is False`).
    * `"not_recorded"` — this row predates `RUN_STATS_VERSION` 7, or its `index_diff` could
      not be parsed. Distinct from `"first_run"` specifically so a pre-feature row never
      claims "first run of this site" about a site that has been running for years.
    """

    previous_run_completed: bool | None
    """`None` when there is no earlier run of any kind; `False` when the run immediately
    before this one did not complete; `True` when it did. Independent of `diff_state` —
    see that field's own docstring for how the two combine into the five UI states."""

    diff: RunIndexDiff | None
    """Non-`null` **if and only if** `diff_state == "compared"` — so no metric on
    `RunIndexDiff` is ever nullable-because-unknown; every field there is a definite number
    once `diff` exists at all. Do not flatten this model's fields onto `LatestRunSnapshot`
    directly — the wrapping is what keeps "no comparison" representable as one `None` rather
    than eleven individually-nullable fields that could disagree with each other."""


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

    latest: LatestRunSnapshot | None
    """The newest completed run inside `window`, or `None` when the window contains no
    completed run at all — see `LatestRunSnapshot`'s own docstring for why this is
    window-scoped rather than "this website's most recent run, full stop.\""""
