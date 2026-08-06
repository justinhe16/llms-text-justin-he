"""The transaction boundary: the one context manager every service opens a unit of work with.

This module encodes three rules from ARCHITECTURE.md §5 in code, not just prose:

(a) **Writers never commit.** A writer (`base_repository.Writer`) executes its statement
    against whatever connection it is handed and returns; `transaction()` below is the
    only thing that commits or rolls back. That is what makes writer calls composable —
    two writer calls can share one unit of work because neither assumes it owns the
    transaction.
(b) **Transactions are opened in the service layer — never in a router, never in a
    repository.** A router calls one service method and returns its result; a repository
    takes whatever connection or pool its constructor is given. Only a service decides
    where a unit of work begins and ends, by calling `transaction(self._pool)`.
(c) **No network calls inside a transaction.** Storage uploads and HTTP fetches happen
    *before* `transaction()` opens; the transaction only records the result afterwards.
    Holding a transaction open across an unpredictable network call ties up a pooled
    connection for however long that call takes — that is how a slow upstream becomes
    connection-pool exhaustion (ARCHITECTURE.md §5.1). This module has no way to enforce
    that rule mechanically; it is enforced by review and by keeping transaction blocks
    short.

Services call `transaction(self._pool)` directly — a module-level function taking the pool,
not a method — and construct a writer around the handle it yields (`WebsitesWriter(tx)`),
rather than passing `tx` to each write call. ARCHITECTURE.md §4.2 and §5 show that same
shape; if you change one, change the other in the same PR.

A `self.transaction()` convenience method on a shared service base class would be a
one-line wrapper around this function, and is deliberately not built: no second shape has
emerged across the feature modules to prove what that base class should hold, and one
indirection that only ever wraps one call is not worth the layer.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from asyncpg import Connection, Pool


@asynccontextmanager
async def transaction(pool: Pool) -> AsyncIterator[Connection]:
    """Open one unit of work: acquire a connection, start a transaction, yield it.

    Commits when the `async with` block exits cleanly. Rolls back if ANY exception
    propagates out of the block — including a FastAPI `HTTPException`. This function does
    not catch, wrap, or replace any exception it sees; letting everything propagate
    unchanged (courtesy of asyncpg's own `Connection.transaction()`, which this wraps) is
    exactly what lets a service raise `HTTPException(status_code=409)` mid-transaction and
    still get both the rollback AND the original status code reaching the client, with no
    special-casing here.

    Takes a `Pool`, never the `DbHandle` union that `base_repository.py`'s `Reader` and
    `Writer` accept. That is deliberate: ARCHITECTURE.md §5 says never nest transactions,
    and typing this parameter as `Pool` only turns "a `Connection` from an outer
    transaction got passed into a second `transaction()` call" into a mypy error at the
    call site. The `isinstance` check below backs that up at runtime, for call sites a
    type checker does not cover (e.g. a `Pool | Connection` value narrowed dynamically).

    If you already hold a `Connection` from an outer `transaction()` block, pass it
    directly to your reader/writer — do not open a second transaction to get one.
    """
    if isinstance(pool, Connection):
        raise TypeError(
            "transaction() takes a Pool, not a Connection — nesting a transaction inside "
            "another one is not supported (ARCHITECTURE.md §5: 'never nest "
            "transactions'). Pass the Connection you already have to your reader/writer "
            "directly instead of opening a second transaction with it."
        )

    async with pool.acquire() as raw_connection:
        # asyncpg-stubs types `pool.acquire()` as yielding `PoolConnectionProxy`, not
        # `Connection` — a stub limitation, not a real difference: PoolConnectionProxy
        # proxies every Connection method (fetch, fetchrow, execute, transaction, ...)
        # with an identical signature, so this cast describes actual runtime behavior
        # rather than working around a real type mismatch.
        connection = cast(Connection, raw_connection)
        async with connection.transaction():
            yield connection
