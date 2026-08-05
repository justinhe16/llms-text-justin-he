"""Request and response DTOs for the websites feature.

These are shapes, not logic (ARCHITECTURE.md §3.1). The one thing that looks like an
exception is `CreateWebsiteRequest`'s validator, and it is the opposite: it delegates every
rule to `internals.url_normalize`, so that the same function decides what a valid URL is for
the HTTP boundary, for the service, and — later — for the crawler.

**Why `CreateWebsiteRequest` validates but does not carry the normalized result.**
`normalize_url` gets called twice for one `POST`: once here, for its exception (pydantic
turns a `ValueError` into FastAPI's native `422`, so the router needs no `try`), and once in
the service, for the `origin` it needs to dedupe against. The obvious "optimization" is to
stash the `NormalizedUrl` on this model in a `model_validator` and let the service read it
back — and that is exactly what §3.1's "schemas must never contain logic" rules out: it
turns a DTO into a carrier of derived state, and makes the service's behaviour depend on
whether its input came through pydantic or was constructed directly in a test.
`normalize_url` is pure, allocation-light, and called once per `POST /websites`; the second
call costs microseconds and buys a layer boundary that stays honest.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.features.runs.schemas import RunStatusName
from app.features.websites.internals.url_normalize import MAX_URL_LENGTH, normalize_url


# `RunStatusName` is owned by the runs feature (`app.features.runs.schemas`) and imported
# from there, not redefined here. `GET /websites?include=latest_run` needs the same
# vocabulary to type `LatestRunSummary.status` below, and a `schemas.py` -> `schemas.py`
# import between two features is the one cross-feature import ARCHITECTURE.md §3.1 allows
# — it prohibits reaching into another feature's `internals/`, not its public DTOs. Do not
# reintroduce a second copy of this Literal here.

# The accepted values of `GET /websites?include=`. A `Literal` rather than a free `str` so
# that `?include=latest_runs` is a `422` naming the valid values, instead of being silently
# ignored and returning a list with every fold field null — a typo that would look like a
# backend bug from the frontend's side.
WebsiteInclude = Literal["latest_run"]


class CreateWebsiteRequest(BaseModel):
    """Body of `POST /websites`."""

    url: str = Field(
        ...,
        # max_length duplicates a check inside normalize_url on purpose: it lets pydantic
        # reject a megabyte of text before this module's validator runs, and it puts the
        # limit in the OpenAPI schema where a client can see it.
        max_length=MAX_URL_LENGTH,
        description=(
            "An absolute http:// or https:// URL. Stored as submitted; its normalized "
            "origin (lowercased scheme and host, default port and path removed) is what "
            "deduplicates it against your existing websites."
        ),
        examples=["https://example.com/docs"],
    )

    @field_validator("url")
    @classmethod
    def _url_must_be_an_absolute_http_url(cls, value: str) -> str:
        """Reject anything `normalize_url` will not accept, and return the trimmed input.

        Raising `normalize_url`'s `UrlValidationError` — a `ValueError` subclass — is what
        produces FastAPI's native `422` with the specific reason attached. See the module
        docstring for why the normalized result is deliberately discarded here.
        """
        return normalize_url(value).url


class WebsiteResponse(BaseModel):
    """One website, as every endpoint in this feature returns it.

    Carries `user_id` because the UI renders the owner and gates the delete control on it —
    and because `require_owner` (`app.core.auth.ownership`) takes the resource, not a bare
    id, so this field is what makes an instance of this model checkable. Exposing it to
    every signed-in caller is intended: this application is not multi-tenant, and who added
    a site is public information within it (ARCHITECTURE.md §4).
    """

    id: UUID
    user_id: UUID
    url: str
    origin: str
    title: str | None
    created_at: datetime


class LatestRunSummary(BaseModel):
    """The compact view of a website's most recent run, folded into the list response.

    A fragment of `WebsiteListItemResponse`, never an endpoint's response on its own — the
    runs feature owns the full run representation. Only the fields the websites table in the
    UI actually renders are here; anything more would make this a second, competing run DTO.
    """

    id: UUID
    status: RunStatusName
    started_at: datetime
    completed_at: datetime | None
    pages_crawled: int | None
    """Read out of `runs.stats->>'pages_crawled'`. `None` when the run has not recorded
    stats yet, or recorded them without that key — `stats` is jsonb whose shape belongs to
    the undesigned crawler milestone (ARCHITECTURE.md §3.4), so it is read defensively."""


class ScheduleSummary(BaseModel):
    """The compact view of a website's schedule, folded into the list response.

    Like `LatestRunSummary`, a fragment: enough to render "every 6 hours, next at 14:00" and
    nothing else. The schedules feature owns the full representation and the endpoints that
    change it.
    """

    active: bool
    interval_minutes: int
    next_run_at: datetime | None


class WebsiteListItemResponse(WebsiteResponse):
    """One row of `GET /websites`.

    `latest_run` and `schedule` are **always `null` unless `?include=latest_run` was
    requested**. With it, each is `null` only when the website genuinely has no run / no
    schedule. The two cases are deliberately not distinguished in the payload: a caller that
    did not ask for the fold has no reason to tell "not requested" from "none exists", and a
    caller that did ask knows it asked.
    """

    latest_run: LatestRunSummary | None = None
    schedule: ScheduleSummary | None = None


class WebsiteAlreadyExistsDetail(BaseModel):
    """The `detail` of the `409` from `POST /websites`, carrying the id of the existing row.

    The id is the point. Pasting a URL you already added is a normal thing to do from the
    landing page, and the useful response is "you already have this, here it is" — the
    frontend navigates to `website_id` instead of rendering an error the user cannot act on.
    A bare `409` would turn a one-click flow into a dead end.

    A model rather than an ad-hoc dict so the shape appears in the OpenAPI document (see the
    router's `responses=`) and the frontend can generate a type for it.
    """

    code: Literal["website_already_exists"] = "website_already_exists"
    """A stable machine-readable discriminator. The frontend branches on this, never on the
    prose in `message`, which is free to be reworded."""

    message: str
    website_id: UUID
    origin: str


class WebsiteAlreadyExistsResponse(BaseModel):
    """The **whole** `409` body, envelope included.

    FastAPI serializes an `HTTPException`'s `detail` as `{"detail": <detail>}`, so this
    wrapper — not `WebsiteAlreadyExistsDetail` on its own — is what a client actually
    receives. Documenting the inner model directly in the route's `responses=` would put a
    schema in the OpenAPI document that no response ever matches, and any client generated
    from it would look for `website_id` one level too high.
    """

    detail: WebsiteAlreadyExistsDetail
