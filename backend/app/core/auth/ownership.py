"""The ownership check: one helper, called from every write path, and nowhere else.

ARCHITECTURE.md §4 splits authorization in two, and this module is the whole of the second
half:

- **Reads are authenticated and unscoped.** Every signed-in user can read every website and
  every run. Nothing in this module is called on a read path, and a reader that filters by
  `user_id` for safety is a bug, not a precaution (§4.1).
- **Writes are authenticated and owned.** `POST`, `PATCH`, `PUT`, and `DELETE` require a
  valid JWT *and* ownership of the resource being written.

**How to call it.** From a service method, as the first thing that happens after the
resource is fetched — before any mutation, before `transaction()` is opened, before any
external call:

```python
async def delete_website(self, website_id: UUID, user_id: UUID) -> None:
    website = await self._reader.get_by_id(website_id)   # 404 if missing
    require_owner(website, user_id)                      # 403 if not the owner
    async with transaction(self._pool) as tx:
        await WebsitesWriter(tx).delete(website_id)
```

The review test is mechanical, and it is why the call belongs immediately after the fetch:
find the `require_owner` line, and check that nothing above it does anything but fetch the
resource. That question stops being answerable the moment a check is inlined into a router
or spelled out as a bare `if` in a service.

**Why `403` and not `404`.** Hiding a resource's existence behind a `404` is a real and
often correct pattern — but not here. `GET /websites/{id}` already returns any website to
any signed-in user (§4), so the caller can trivially confirm the row exists. Answering
`404` on the write would be a lie the very next request disproves, and it would send a
frontend down the "this was deleted, navigate away" path when the truth is "this is not
yours". There is nothing to conceal, so this says what actually happened.

**Why the check is here and not in Postgres.** The backend connects as a single application
role and does not use row-level security (§4.3). One enforcement point in application code
beats two that can disagree.
"""

from typing import Protocol
from uuid import UUID

from fastapi import HTTPException, status


# One string for every ownership failure in the codebase. It names no resource, no owner,
# and no id: a caller who is not the owner learns that they are not the owner, and nothing
# else. The service that raised it knows the specifics; the client does not need them.
_FORBIDDEN_DETAIL = "Only the owner of this resource may modify it"


class Owned(Protocol):
    """Anything with an owner: a resource DTO carrying the `user_id` that owns it.

    A `Protocol` rather than a base class or a `UUID` argument, for two reasons.

    Structural typing means `WebsiteResponse`, and every later feature's response DTO,
    satisfies this by simply having the `user_id` field the database already gives them —
    no inheritance to remember, no registration step, and mypy rejects a call that passes
    something without an owner.

    And taking the *resource* rather than `resource.user_id` is what keeps call sites
    honest. `require_owner(website, user_id)` cannot be got wrong; a helper taking two bare
    UUIDs (`require_owner(website.user_id, user_id)`) is one transposition away from
    comparing a value to itself and authorizing everything — a bug that no test which only
    checks the happy path would ever catch.

    Declared as a read-only property so that a frozen dataclass, a pydantic model, and a
    plain attribute all satisfy it.
    """

    @property
    def user_id(self) -> UUID: ...


def require_owner(resource: Owned, user_id: UUID) -> None:
    """Raise `HTTPException(403)` unless `user_id` owns `resource`. Return `None` if it does.

    Args:
        resource: The already-fetched resource, carrying the `user_id` of its owner.
        user_id: The authenticated caller, from `CurrentUserId` (`app.api.deps`).

    Raises:
        HTTPException: `403`, with a detail that identifies neither the resource nor its
            owner. Never `404` — see the module docstring.
    """
    if resource.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_FORBIDDEN_DETAIL)
