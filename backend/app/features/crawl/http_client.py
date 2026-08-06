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
    `open_worker_resources` (app/worker/settings.py) can call this unconditionally in
    `on_startup` without a way for it to fail.

    **Known, accepted, and worth writing down: httpcore pools connections by
    `(scheme, host, port)` of the URL actually dialed — which, for this client, is the
    validated IP.** Two different hostnames that resolve to the same IP and port (a shared
    CDN edge, say) can therefore share one keep-alive connection, so the second request's
    `Host` header travels over a TLS session negotiated with the first hostname's SNI. This
    is NOT an SSRF hole: both hostnames must already have passed `validate_url()`
    independently, and no address the guard refused becomes reachable this way. It is a
    response-attribution wrinkle on shared-hosting IPs, in the same family as this client's
    cookie jar being keyed on the dialed IP rather than the origin host. Both are follow-ups
    for the ticket that makes the frontier span more than one host; today a run fetches a
    single site, so neither can arise.
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
