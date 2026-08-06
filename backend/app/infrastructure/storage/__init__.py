"""Supabase Storage access: one factory pair, no process-wide singleton.

The third backing-service package alongside `app.infrastructure.db` (ARCHITECTURE.md §3.5)
and `app.infrastructure.queue` (§3.6). `supabase_storage.py` builds a dedicated
`httpx.AsyncClient` and the `SupabaseStorage` client that uploads a crawl run's compressed
payload to the private `crawl-payloads` bucket.

**Only the worker uses this package, and it holds no singleton.** `db/pool.py` and
`queue/pool.py` both need an `open_/get_/close_` triple because FastAPI's request path
reaches for the pool through a dependency, constructed once at API startup and shared across
requests it did not create. Nothing under `app/api/` ever uploads to Storage — only
`app.features.crawl.service.CrawlService`, built fresh per job — so there is no second
caller for a module-level singleton to serve, and no dependency-injection path that would
need one. The worker's own `ctx` dict (arq's per-process context, populated by
`app/worker/settings.py`'s `open_worker_resources`) already does the "build once per
process" job a singleton here would duplicate.
"""
