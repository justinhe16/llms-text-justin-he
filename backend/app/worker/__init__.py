"""The ARQ worker: the second process run from the backend image.

`settings.py` holds `WorkerSettings`, which is what `arq app.worker.settings.WorkerSettings`
(the `worker` process in backend/fly.toml, and `make dev`) loads. `jobs.py` holds the job
functions themselves.

Job functions stay thin for the same reason routers do (ARCHITECTURE.md §3.3): a background
job is a service call with a different trigger, not a parallel implementation. Real crawling
lives behind the seam in §3.4 and is not in this package yet.

Nothing here is imported by `app.api` or `app.features`. The dependency runs the other way —
the worker imports services — so that the API never drags the queue into a request path.
"""
