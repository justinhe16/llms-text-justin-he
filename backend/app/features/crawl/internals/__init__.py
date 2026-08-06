"""Private to the crawl feature: SSRF validation and the bounded, redirect-following fetch.

Ordinarily this directory holds a feature's reader and writer (ARCHITECTURE.md §3.1). This
feature owns no table — see the package docstring in `app.features.crawl` — so there is no
`crawl_reader.py` or `crawl_writer.py` here. What lives here instead is the feature's own
private, table-free I/O:

* `ssrf.py` — the security boundary every fetch target passes through before a socket is
  ever opened. `validate_url()` is the one function every other module in this feature
  calls before making a connection; nothing downstream of it re-checks a hostname.
* `fetcher.py` — the one bounded GET that dials exactly what `ssrf.py` validated, and the
  `ByteBudget` that caps how many response bytes one run may accept in total.
* `crawler.py` — `crawl_site`, the bounded loop that turns one `fetch_page` call into a run:
  the seed fetch, the frontier, and the six caps from `Settings`.
* `llms_txt.py` — `generate_llms_txt`, the stub seam (ARCHITECTURE.md §3.4, CLAUDE.md #9).

Like any other feature's `internals/`, this package is private: only
`app.features.crawl.service` and this feature's own modules import from it. No other
feature reaches in here, the same rule that applies to every `{feature}/internals/` in this
codebase.
"""
