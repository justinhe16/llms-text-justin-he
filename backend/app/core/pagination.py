"""The one pagination envelope every list endpoint in this codebase returns.

**This is a convention, not a utility.** `GET /websites/{id}/runs` is the first paginated
endpoint here, so the shape it returns is the shape every later one inherits — the stats
endpoint, and every list the frontend grows after it. Defining it once, in `core/`, is what
stops the second paginated endpoint from inventing `{results, cursor}` and the third from
inventing `{data, page_token}`, leaving the frontend with three ways to read a list.

    {"items": [...], "next_cursor": "eyJzdGFy..." | null}

**Why `core/`.** The import direction is `api` -> `features` -> `infrastructure` -> `core`
(ARCHITECTURE.md §3.1), so `core/` is the only layer every feature may import from. This
module is a pure DTO with no logic, no I/O, and no dependency beyond pydantic, which is what
makes it safe to live at the bottom of that graph. §3.1's rule for a helper a second feature
needs — "promote it to a shared location rather than importing across the boundary or
copying it" — is the instruction being followed here, in advance of the second feature,
because the ticket that introduced this envelope explicitly asked for it to be decided once.

**Why cursor pagination and not `?page=2`.** Offset pagination is wrong for every list in
this product, not merely slower. Runs are inserted at the head of their own history, so an
offset taken before an insert points one row further back after it: page 2 re-serves a row
the client already displayed on page 1, and — worse — a deletion makes it *skip* one
silently. A cursor names a position in the ordering rather than a distance from its start,
so concurrent writes cannot shift it. See `app.features.runs.internals.run_cursor` for the
keyset mechanics.

**`next_cursor` is the only pagination state.** There is deliberately no `total`, no
`page_count`, and no `has_more` flag. A total requires a `COUNT(*)` over the whole filtered
set — a second query, and one whose cost grows with the table while the page query's does
not — and it would be stale the moment it was computed. `next_cursor is None` already
answers "is there more?", so a separate boolean would be a second source of truth for the
same fact.
"""

from pydantic import BaseModel, Field


# `ItemT` is declared inline with PEP 695 syntax below rather than as a module-level
# `TypeVar`. Both work; this is the form ruff's UP046 asks for on `target-version = "py312"`,
# and taking it means this file needs no lint suppression. It is deliberately unbounded — a
# page of scalars is a legitimate thing to return, and nothing here touches the item type.
class Page[ItemT](BaseModel):
    """One page of a keyset-paginated list, plus the cursor that fetches the next one.

    Used as `response_model=Page[RunListItemResponse]`. Pydantic renders a parameterized
    generic into the OpenAPI document under a mangled component name —
    `Page_RunListItemResponse_` — which is standard for pydantic v2 generics and is the
    accepted cost of having exactly one envelope type. It is not a bug and does not need
    "fixing" with a hand-written per-feature copy; a generated client reads it fine.
    """

    items: list[ItemT]
    """This page's rows, in the endpoint's documented order. Empty on a page past the end,
    or when nothing matches the filter — never `null`."""

    next_cursor: str | None = Field(default=None)
    """Opaque. Pass it back as `?cursor=` to fetch the next page; `None` means this was the
    last page.

    **Clients must treat this as a blob** — do not parse it, decode it, construct one, or
    persist one and expect it to keep working. The encoding is an implementation detail that
    is expected to change (see `run_cursor.encode_cursor`), and the only contract is that a
    value this API produced can be handed back to the same endpoint.

    An empty `items` with a non-`None` `next_cursor` is possible in principle for a filtered
    list and clients should keep following the cursor rather than stopping at the first empty
    page — though the `LIMIT n + 1` probe in the runs reader means it does not arise there.
    """
