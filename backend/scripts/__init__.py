"""Developer-facing scripts for the backend.

Not part of the runtime image — `backend/.dockerignore` never copies this directory into
the container, and `backend/Dockerfile` copies only `requirements.txt` and `app/` — and
not type-checked by `mypy app` (`backend/pyproject.toml` scopes mypy to the `app` package
alone). `ruff check backend` still lints it, which is why `export_openapi.py` keeps its
imports at module scope: `PLC0415` applies here exactly as it does everywhere else in
`backend/`.
"""
