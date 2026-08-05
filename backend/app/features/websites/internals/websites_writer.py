"""Every `INSERT`/`UPDATE`/`DELETE` for the websites feature (ARCHITECTURE.md §3.1).

**This writer never commits, never rolls back, and never opens a transaction.** It executes
one statement against whatever it was constructed with and returns. The unit of work belongs
to `transaction()` in the service (ARCHITECTURE.md §5), which is what lets two writer calls
share one transaction: neither assumes it owns the connection it is running on.

Consequently a `WebsitesWriter` is built *inside* a service's `transaction()` block, from
the `Connection` that block yields:

```python
async with transaction(self._pool) as tx:
    await WebsitesWriter(tx).delete(website_id)
```

That is the mechanism `app.infrastructure.db.base_repository` provides ("pass the
`Connection` yielded by `transaction()` instead of the pool, and every reader/writer
constructed with it participates in that same unit of work"). ARCHITECTURE.md §4.2/§5 sketch
it as `self._writer.delete(tx, ...)`; those snippets predate the base classes and are
illustrative about *where the boundary is*, which is identical either way — the same
divergence `app/infrastructure/db/transaction.py`'s docstring already records for
`self.transaction()`.

**No ownership check happens here.** A writer cannot know whether the caller is allowed to
write; by the time one of these methods runs, the service has already fetched the resource
and called `require_owner`. Putting a `user_id` predicate in the `DELETE` below would be a
second, silently-diverging enforcement point — and one that answers "0 rows deleted" where
the contract says `403`.
"""

from typing import Any, Final
from uuid import UUID

from app.infrastructure.db.base_repository import Writer


# `RETURNING` the same column list `WebsiteResponse` needs, so creating a website costs one
# round trip instead of an INSERT followed by a SELECT. `id` and `created_at` are database
# defaults (`gen_random_uuid()`, `CURRENT_TIMESTAMP`), so the returned row is the only place
# their values exist — reconstructing the response from the request would be a guess.
_INSERT: Final = """
    INSERT INTO websites (user_id, url, origin)
    VALUES ($1, $2, $3)
    RETURNING id, user_id, url, origin, title, created_at
"""

# No `user_id` predicate — see the module docstring. `schedules` and `runs` disappear with
# the row through `ON DELETE CASCADE` on their foreign keys (db/migrations/…/migration.sql),
# so the cascade is the database's job and needs no second statement here.
_DELETE: Final = "DELETE FROM websites WHERE id = $1"


class WebsitesWriter(Writer):
    """Writes for the websites feature. Private to it — only `WebsiteService` calls this."""

    async def insert(self, user_id: UUID, url: str, origin: str) -> dict[str, Any]:
        """Insert one website owned by `user_id` and return the row that was created.

        Args:
            user_id: The authenticated caller, who becomes the owner.
            url: The URL as submitted, trimmed.
            origin: The normalized origin, half of the `UNIQUE (user_id, origin)` key.

        Returns:
            The inserted row, including the database-generated `id` and `created_at`.

        Raises:
            asyncpg.UniqueViolationError: If this user already has a website with this
                origin. Deliberately not caught here: the service turns it into the `409`
                that carries the existing website's id, and that requires a second query
                this writer has no business issuing.
            RuntimeError: If `RETURNING` somehow produced no row, which would mean an
                `INSERT` that reported success without inserting.
        """
        row = await self.fetch_one(_INSERT, user_id, url, origin)
        if row is None:
            # Unreachable: an INSERT ... RETURNING either raises or returns its row. Asserted
            # rather than `assert`ed so it survives `python -O`, and so the impossible case
            # cannot silently become `None` flowing into a response model.
            raise RuntimeError("INSERT INTO websites ... RETURNING produced no row")
        return row

    async def delete(self, website_id: UUID) -> str:
        """Delete one website by id, cascading to its schedule and runs.

        Returns:
            asyncpg's status tag, e.g. `"DELETE 1"`. The service does not branch on it: it
            has already fetched the row and checked ownership, and a concurrent delete that
            makes this a no-op leaves the caller in exactly the state they asked for.
        """
        return await self.execute(_DELETE, website_id)
