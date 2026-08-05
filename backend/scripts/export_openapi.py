"""Export the FastAPI application's OpenAPI document as a deterministic JSON snapshot.

`app.openapi()` builds the document from the routes, `response_model`s, and `Depends()`
wiring that `app.api.routers` and `app.features.*` already declare at import time — that
is a pure computation over Python objects already in memory, so producing it needs no
running server and no database connection. Booting uvicorn just to introspect its own
route table would be strictly more expensive and no more correct.

Importing `app.main` still runs `app.core.settings` at module scope (see that module's
own docstring for why), which raises unless `DATABASE_URL`, `REDIS_URL`, `SUPABASE_URL`,
and `SUPABASE_SECRET_KEY` are all non-empty. This script therefore needs those four
variables set to *something* before it runs — `scripts/export-openapi.sh` (repo root)
supplies obvious non-values for exactly this reason — even though the document this
script prints does not depend on any of their actual values.

Run as a module from `backend/`, so `app.main`'s absolute imports resolve the same way
they do for the application itself:

    python -m scripts.export_openapi > lib/api/openapi.json

Writes the document to stdout only, with a trailing newline, and touches nothing on disk
itself — `scripts/export-openapi.sh` is what decides whether that stdout becomes the
committed snapshot or a throwaway file to `diff` against it.

`sort_keys=True` is deliberate, not cosmetic. FastAPI assembles `components.schemas` by
walking registered routes in whatever order `app.main.create_app()` included their
routers, and Python dicts preserve insertion order — so an unsorted dump would depend on
incidental import order rather than on any change a person actually made. Sorting keys
removes that entire axis of false-positive diffs from the drift check in
`scripts/export-openapi.sh --check`, which is the one thing that check cannot afford to
flap on.
"""

import json

from app.main import app


def main() -> None:
    """Print the application's OpenAPI document as sorted, indented JSON."""
    document = json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False)
    print(document)


if __name__ == "__main__":
    main()
