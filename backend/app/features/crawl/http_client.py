"""The single `httpx.AsyncClient` every crawl fetch is issued through.

Built once per worker process (`app/worker/settings.py`'s `open_worker_resources`, a later
phase of this ticket) and threaded through every `fetch_page` call, the same way the process
shares one asyncpg pool rather than opening a connection per job.

Lives at the top of the feature, not in `internals/`, because `app/worker/settings.py`
constructs it and `internals/` is private to this feature (ARCHITECTURE.md §3.1) — a module
another feature's setup code has to import cannot also be one only this feature may import.
"""

import httpx

from app.core.settings import Settings


CRAWL_USER_AGENT = "llms-text-bot/0.1 (+https://github.com/justinhe16/llms-text-justin-he)"
"""Identifies every crawl request as this project's, with a URL a site operator can visit
to find out what is fetching their pages and how to block it. A descriptive UA is a
courtesy this crawler can afford to extend and cannot afford to skip: it is one grep away
from being the entire explanation a confused site owner gets."""


def build_crawl_client(settings: Settings) -> httpx.AsyncClient:
    """Build the crawler's shared `httpx.AsyncClient`.

    `follow_redirects=False` is LOAD-BEARING, not a style preference. `internals/ssrf.py`'s
    check-then-connect defense only holds if every redirect hop is re-validated by hand
    before the next request is made (`internals/fetcher.py`); if httpx followed redirects
    itself, a validated first hop could still land on an unvalidated final one, silently
    routed through this client with no chance for `validate_url()` to see it. Do not flip
    this to `True` to "simplify" a call site — it removes the one property this whole
    ticket exists to guarantee.

    Constructing this client opens no socket and makes no network call, which is why
    `open_worker_resources` (app/worker/settings.py, a later phase) can call this
    unconditionally in `on_startup` without a way for it to fail.
    """
    timeout = httpx.Timeout(settings.crawl_request_timeout_s)
    limits = httpx.Limits(
        max_connections=settings.crawl_concurrency,
        max_keepalive_connections=settings.crawl_concurrency,
    )
    return httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        limits=limits,
        headers={"User-Agent": CRAWL_USER_AGENT},
    )
