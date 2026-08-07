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

Beside those sit this feature's **pure** modules, which do no I/O at all and are listed
separately for exactly that reason: `payload.py` (the bytes a run's pages are stored as),
`run_stats.py` (the `dict` `runs.stats` persists), `extract.py` (`extract_content`, one
page's HTML parsed into a title, a description, and a markdown body), and `url_ranking.py`
(`select_urls`, a list of discovered URLs ranked and cut down to the ones worth spending a
run's page budget on). Neither of the last two is called by anything today: wiring extraction
into the crawl loop is PER-177, and wiring selection into it — so that `crawl_site`'s
`extra_urls` stops being an empty tuple — is PER-176 (ARCHITECTURE.md §3.4).

Like any other feature's `internals/`, this package is private: only
`app.features.crawl.service` and this feature's own modules import from it. No other
feature reaches in here, the same rule that applies to every `{feature}/internals/` in this
codebase.
"""
