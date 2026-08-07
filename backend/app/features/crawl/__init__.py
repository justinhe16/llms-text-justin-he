"""The crawl feature: fetching a website's pages under hard caps and SSRF protection.

**This feature owns no table.** `runs` already has every column a crawl writes to
(`status`, `completed_at`, `error`, `stats` — ARCHITECTURE.md §6.4), so this feature reads
and writes them through the *runs* feature's own service, never through a reader or writer
of its own (ARCHITECTURE.md §3.1: "if feature A needs data owned by feature B, it calls B's
service, never B's reader"). That is why `internals/` here holds this feature's private I/O
helpers — SSRF validation, the bounded fetch — instead of the usual
`{feature}_reader.py` / `{feature}_writer.py` pair every other feature has.

Layout:

* `schemas.py` — `CrawledPage` and `CrawlOutcome`. Not Pydantic DTOs: nothing here crosses
  the HTTP boundary, because this feature has no router.
* `http_client.py` — `build_crawl_client(settings)`, the one `httpx.AsyncClient` every
  fetch in a run is issued through. Lives at the top of the feature, not in `internals/`,
  because `app/worker/settings.py` constructs it once per process and `internals/` is
  private to this feature.
* `service.py` — `CrawlService.execute_run`, the one method `app.worker.jobs.crawl_task`
  calls, and `build_crawl_service`. The only place in this feature that calls another
  feature's service (`RunService`, `WebsiteService`) instead of a reader or writer.
* `internals/` — see that package's own docstring.

Everything downstream of a fetched page lives behind one pair of functions in
`internals/llms_txt.py` — `generate_llms_txt(pages: list[CrawledPage]) -> str` and its
`generate_llms_full_txt` sibling — and both are pure, deterministic and model-free
(ARCHITECTURE.md §3.4, CLAUDE.md #9). Calling a model remains out of scope for this
milestone, and not merely unimplemented: that pass is a layer ABOVE these functions, which
stay the fallback it degrades to when its flag is off or its API call fails.
"""
