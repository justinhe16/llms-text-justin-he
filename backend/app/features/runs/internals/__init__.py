"""Private to the runs feature: its reader and its pagination-cursor codec.

`internals/` means what it says (ARCHITECTURE.md §3.1): **no other feature imports anything
in this package.** A feature that needs run data calls `RunService`, never `RunsReader` —
that is the rule that keeps the dependency graph acyclic, and it is the one this boundary
exists to enforce.

Within the runs feature itself, two modules import from here, and the distinction between
them matters:

* `service.py` imports `runs_reader` — every `SELECT` this feature runs is in there, and the
  service is the only thing allowed to call it.
* `api/routers/runs.py` imports `run_cursor` (`decode_cursor`, `RunCursor`, `CursorError`) so
  that a malformed `?cursor=` becomes a `422` in a `Depends()` before any service method
  runs. A router reaching into `internals/` looks like a layering violation and is not one:
  `run_cursor` is a **pure, no-I/O helper**, the category §3.1 explicitly permits here
  ("`internals/` may also hold a feature's own **pure** helpers"), and the reference
  implementation already set this precedent — `websites/schemas.py` imports
  `websites/internals/url_normalize.py` for exactly the same reason, to validate at the HTTP
  boundary with the same function the service uses.

  What the router does NOT import, and must never import, is `runs_reader`. The rule being
  protected is "no SQL outside the reader, and no reader call outside the service", not
  "nothing above `features/` may name a module in this directory".
"""
