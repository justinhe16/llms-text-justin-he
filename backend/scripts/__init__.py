"""Developer-facing scripts for the backend.

Not part of the runtime image: `backend/Dockerfile` copies only `requirements.txt` and
`app/`, and it is that selective `COPY` — not `backend/.dockerignore`, which does not
mention this directory at all — that keeps everything here out of the container. Also not
type-checked, because `backend/pyproject.toml` scopes mypy to the `app` package and CI
runs it as `mypy app`.

`ruff check backend` *does* lint this directory, which is why `export_openapi.py` keeps
its imports at module scope: `PLC0415` and `E402` apply here exactly as they do
everywhere else under `backend/`.
"""
