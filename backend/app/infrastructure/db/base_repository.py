"""Base repository classes: the shared shape every feature's reader and writer builds on.

Every feature's `internals/{feature}_reader.py` subclasses `Reader`, and every
`internals/{feature}_writer.py` subclasses `Writer` (ARCHITECTURE.md §3.1). Both accept
either a pool or a single connection — see `DbHandle` below — which is what lets a service
run several repository calls inside ONE transaction on ONE connection: pass the
`Connection` yielded by `transaction()` (`transaction.py`) instead of the pool, and every
reader/writer constructed with it participates in that same unit of work.

**Writers never commit, never roll back, and never open a transaction.** `Writer.execute`
and `Writer.fetch_one` run exactly one statement against whatever they were constructed
with and return. The transaction — commit on success, rollback on any exception — belongs
to `transaction()` in the service layer (ARCHITECTURE.md §5), not to a writer. That
separation is what makes writer calls composable: two writer calls can share one unit of
work because neither one assumes it owns the transaction it is running inside.

**Deliberately no tenant scoping.** This project is not multi-tenant (ARCHITECTURE.md
§4): there is no `tenant_id` column, no `TenantScopedReader`, and no implicit
`WHERE tenant_id = $1` anywhere below or in anything that subclasses these. If you are
porting a pattern from a multi-tenant codebase, leave that machinery behind — reads in
this codebase are intentionally unscoped, and any filter a reader applies is a named,
product-level query parameter (e.g. `user_id` for "list my websites"), never a security
boundary bolted on here.

**Every query is parameterized with asyncpg's positional placeholders (`$1`, `$2`, ...),
never `%s` and never an f-string.** That includes `ORDER BY` and `LIMIT`: a value used
there must still travel as a bind parameter, or — if the thing that varies is a column or
table name, which cannot be a bind parameter — be validated against an explicit allowlist
before it touches the query string. Never interpolate a value into SQL.
"""

from typing import Any

from asyncpg import Connection, Pool


# What a repository is constructed with: either the process-wide pool (each call borrows
# its own connection and returns it immediately) or a single already-open `Connection`
# (every call runs on that one connection, e.g. inside a service's `transaction()`
# block). Naming this once here means "accepts a DbHandle" reads the same way in every
# feature's reader and writer.
DbHandle = Pool | Connection


class Reader:
    """Base class for a feature's `internals/{feature}_reader.py`. Owns every `SELECT`.

    See the module docstring for the pool-vs-connection, no-tenant-scoping, and
    parameterized-query rules that apply to every method below and to every subclass.
    """

    def __init__(self, db: DbHandle) -> None:
        self._db = db

    async def fetch_one(self, query: str, *args: object) -> dict[str, Any] | None:
        """Run a query expected to return at most one row.

        Converts the `asyncpg.Record` to a plain `dict` before returning it. An
        `asyncpg.Record` never escapes a repository — that conversion happens here, in
        the base class, so no subclass can leak one by forgetting to convert it.
        Repositories return dicts (or, in a service, a Pydantic model built from one),
        never asyncpg's row type.
        """
        record = await self._db.fetchrow(query, *args)
        return dict(record) if record is not None else None

    async def fetch_all(self, query: str, *args: object) -> list[dict[str, Any]]:
        """Run a query and return every row as a plain `dict`. See `fetch_one`."""
        records = await self._db.fetch(query, *args)
        return [dict(record) for record in records]

    async def fetch_val(self, query: str, *args: object) -> Any:
        """Run a query and return a single scalar — the first column of the first row.

        Typed `Any` because a Postgres column's decoded Python type is genuinely dynamic
        (`UUID`, `datetime`, `int`, `str`, ...) — the same reason `fetch_one`/`fetch_all`
        return `dict[str, Any]`. This mirrors asyncpg's own `fetchval` signature.
        """
        return await self._db.fetchval(query, *args)


class Writer:
    """Base class for a feature's `internals/{feature}_writer.py`. Owns every write.

    **Never commits, never rolls back, never opens a transaction** — see the module
    docstring. See also the no-tenant-scoping and parameterized-query rules there, which
    apply here too.
    """

    def __init__(self, db: DbHandle) -> None:
        self._db = db

    async def execute(self, query: str, *args: object) -> str:
        """Run an `INSERT`/`UPDATE`/`DELETE` that does not need to return a row.

        Returns asyncpg's status tag (e.g. `"UPDATE 1"`) — useful for asserting a write
        touched the expected number of rows; not meant to be parsed for data.
        """
        return await self._db.execute(query, *args)

    async def fetch_one(self, query: str, *args: object) -> dict[str, Any] | None:
        """Run a write with a `RETURNING` clause and return the row it produced.

        Named and shaped like `Reader.fetch_one` on purpose: a writer that needs the row
        it just wrote (`INSERT ... RETURNING *`) uses this instead of a second round trip
        through a reader. See `Reader.fetch_one` for the `Record` -> `dict` conversion
        rule this follows.
        """
        record = await self._db.fetchrow(query, *args)
        return dict(record) if record is not None else None
