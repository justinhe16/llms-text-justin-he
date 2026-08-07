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
* `sitemap.py` — `discover_sitemap_urls`, which fills that frontier before the loop runs:
  `/sitemap.xml`, then `/sitemap_index.xml`, then whatever `robots.txt` declares. As of
  PER-191 it also fetches `robots.txt` itself unconditionally, once per run, before either
  well-known probe, and returns the parsed `RobotsRules` alongside the discovered URLs on
  `DiscoveryResult.robots` — the same response feeds both consumers, never fetched twice.
  Every fetch this module makes goes through `fetcher.py` (and therefore `ssrf.py`) exactly
  like the crawl loop's own.

Beside those sit this feature's **pure** modules, which do no I/O at all and are listed
separately for exactly that reason: `payload.py` (the bytes a run's pages are stored as),
`run_stats.py` (the `dict` `runs.stats` persists), `extract.py` (`extract_content`, one
page's HTML parsed into a title, a description, and a markdown body), `llms_txt.py`
(`generate_llms_txt` and `generate_llms_full_txt`, the two artifacts a run publishes, plus
the two counts `runs.stats` records about them — ARCHITECTURE.md §3.4), `url_ranking.py`
(`select_urls`, a list of discovered URLs ranked and cut down to the ones worth spending a
run's page budget on), and `robots.py` (`parse_robots`, one `robots.txt` body turned into the
`Disallow`/`Allow`/`Crawl-delay` rules `select_urls` and `crawl_site` enforce, and
`effective_crawl_delay_ms`, PER-191). `extract.py` is called by `fetcher.py`, once per
fetched page that looks like HTML (PER-177). `url_ranking.py` is called by `service.py`, over
whatever `sitemap.py` discovers, to build `crawl_site`'s `extra_urls` (PER-176). `robots.py`'s
`parse_robots` is called only by `sitemap.py`, over the one `robots.txt` fetch a run makes;
`url_ranking.py` imports `RobotsRules` only for its `select_urls(..., robots=...)` parameter's
type, and calls the `RobotsRules.is_allowed` method on whatever `sitemap.py` already parsed —
neither module, nor `fetcher.py` or `crawler.py`, ever parses a `robots.txt` body itself.

Like any other feature's `internals/`, this package is private: only
`app.features.crawl.service` and this feature's own modules import from it. No other
feature reaches in here, the same rule that applies to every `{feature}/internals/` in this
codebase.
"""
