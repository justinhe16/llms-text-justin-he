"""Private to the runs feature: its reader, its writer, its pagination-cursor codec, and its
pure rate-limit-message helpers.

`internals/` means what it says (ARCHITECTURE.md §3.1): **no other feature imports anything
in this package.** A feature that needs run data calls `RunService`, never `RunsReader` or
`RunsWriter` — that is the rule that keeps the dependency graph acyclic, and it is the one
this boundary exists to enforce.

Within the runs feature itself, three modules import from here, and the distinction between
them matters:

* `service.py` imports `runs_reader`, `runs_writer`, and `run_limits` — every `SELECT` and
  every `INSERT`/`UPDATE` this feature runs is in the first two, and `RunService` is the
  only thing allowed to call either. `run_limits` is imported here too, but it is pure (see
  below), so this is not the same kind of dependency as the other two.
* `api/routers/runs.py` imports `run_cursor` (`decode_cursor`, `RunCursor`, `CursorError`) so
  that a malformed `?cursor=` becomes a `422` in a `Depends()` before any service method
  runs. A router reaching into `internals/` looks like a layering violation and is not one:
  `run_cursor` is a **pure, no-I/O helper**, the category §3.1 explicitly permits here
  ("`internals/` may also hold a feature's own **pure** helpers"), and the reference
  implementation already set this precedent — `websites/schemas.py` imports
  `websites/internals/url_normalize.py` for exactly the same reason, to validate at the HTTP
  boundary with the same function the service uses.
* `tests/test_run_limits.py` imports `run_limits` directly, with no database and no HTTP
  client, for exactly the reason `run_cursor` is importable from a router: it is pure. It is
  NOT imported by `api/routers/runs.py`, unlike `run_cursor` — the 429 message strings are
  service-layer business logic (which cap, what it should say), not input parsing at the
  HTTP boundary, so the router has no reason to reach for it.

  What the router does NOT import, and must never import, is `runs_reader` or `runs_writer`.
  The rule being protected is "no SQL outside the reader/writer, and no reader/writer call
  outside the service", not "nothing above `features/` may name a module in this directory".
"""
