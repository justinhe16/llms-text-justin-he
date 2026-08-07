"""The artifacts a run publishes: `llms.txt` (a curated index) and `llms-full.txt` (the
corpus behind it). Pure functions — no network, no clock, no I/O, nothing that reads a
setting.

**This module was THE STUB SEAM, and is no longer one.** ARCHITECTURE.md §3.4 and CLAUDE.md
#9 held everything downstream of a fetched page behind `generate_llms_txt(pages) -> str`
while that pipeline was undesigned, and until PER-179 this function honoured that by
returning an H1, a blockquote disclaiming itself as provisional, and one bullet per fetched
URL. The pipeline is designed now and this module implements it. What the old docstring
forbade has NOT become permitted wholesale, though — the boundary moved rather than vanished:

* **Deciding which fetched pages belong in the artifact, how they are grouped, and how they
  are labelled is now this module's job.** That is the design that landed, and it lives here,
  in one file, rather than scattered through `app.features.crawl.service` or
  `internals/crawler.py`.
* **Calling a model is still out of scope, and not merely "not yet implemented".** Everything
  below produces a complete, useful artifact with no model involved, on purpose: when
  summarization does land it is a layer *above* this one, and this module stays the fallback
  that runs when that feature's flag is off or its API call fails. A function that only works
  when an external service answers is not a fallback. Nothing here may grow a network call,
  and nothing here may become conditional on one having happened.

**Deterministic regardless of fetch order.** `internals/crawler.py`'s frontier fetches race
each other, so the order pages arrive in `CrawlResult.pages` is not reproducible between two
runs of the same crawl. Every value derived below — the project name, the origin, the section
each page lands in, the order of bullets within a section, and the order page bodies appear
in `llms-full.txt` — is computed from the pages sorted by URL first, and every ordering
decision falls back to a total tie-break. Both artifacts are therefore pure functions of
*which* pages were fetched, never of the order they finished in. Sorting happens HERE, once,
rather than at the call site, so every caller gets that guarantee for free and no future
caller can forget to. `internals/url_ranking.py`'s `select_urls` makes the same promise for
the upstream half (ARCHITECTURE.md §3.4).

## Four public functions where the ticket named two

`generate_llms_txt` and `generate_llms_full_txt` are the artifacts, and both return `str`
exactly as ARCHITECTURE.md §3.4 pins the seam's shape. But `runs.stats` has to record two
numbers only this module can know: how many links the index actually lists
(`links_emitted`, which stopped meaning "one per fetched page" the moment `is_empty` pages
began being skipped) and how many page bodies the caps below cut (`full_txt_truncated`).
Widening either generator's return type to carry them would change the seam's signature;
re-deriving them in `app.features.crawl.service` would put this module's skip rule and cap
arithmetic in a second place, free to drift from the artifact it claims to describe — the
exact failure `internals/run_stats.py` warns about when it says `links_emitted` is "exactly
as accurate as the artifact it describes".

So the counts are their own functions, `count_indexed_pages` and
`count_full_txt_truncations`, and each shares its whole implementation with the generator it
describes (`_index_entries` and `_plan_full_txt` respectively) rather than reimplementing it.
The cost is that a caller wanting both an artifact and its count computes the selection
twice. That is deliberate and bounded: the selection is a sort and a filter, and
`_plan_full_txt`'s worst case is capped at `MAX_FULL_TEXT_BYTES` by construction. Paying it
buys the property that a count and the artifact it counts cannot disagree, because there is
only one implementation of each for them to disagree about. Do not "optimize" this into a
cache or shared mutable state; a pure function that is called twice is the cheap problem
here.
"""

from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

from app.features.crawl.schemas import CrawledPage


MAX_PAGE_TEXT_BYTES: Final = 50 * 1024
"""How much of ONE page's markdown `llms-full.txt` will carry, in UTF-8 bytes (50 KiB —
`50 * 1024`, not 50 000; the ticket's "50 KB" is read the way every other size in this
codebase is).

Bytes, not characters, because the number that matters is how much of a Postgres `text`
column and how much of a worker's memory this consumes, and a character is not a fixed amount
of either — a page of CJK prose averages three bytes per character, so a character budget
would permit roughly three times the column it advertises. Bounded independently of the
crawl's own `crawl_max_bytes` setting on purpose — and NOT read from settings here, which is
what keeps this module pure: that cap bounds what comes off the WIRE, and a 2 MB HTML
document can extract to markdown larger than its own compressed transfer or smaller than a
tenth of it. Neither direction is predictable enough for one cap to stand in for the other."""

MAX_FULL_TEXT_BYTES: Final = 5 * 1024 * 1024
"""How large the whole `llms-full.txt` artifact may get, in UTF-8 bytes (5 MiB).

The per-page cap alone does not bound this — a run may fetch `crawl_max_pages` pages, and
50 KiB times that is not a number this codebase wants to inline into a single column. This is
the cap that actually protects `runs.llms_full_txt`, and it is enforced at a PAGE BOUNDARY
(see `_plan_full_txt`), never mid-page.

A backstop rather than the common path: at today's defaults this binds only if nearly every
page in a run is at its own cap.

**`llms.txt` deliberately gets no equivalent cap**, and the asymmetry is reasoned rather than
an omission. A bullet is a bounded title (`MAX_TEXT_CHARS`), a bounded description (likewise)
and a URL, repeated at most `crawl_max_pages` times — so the index's size is bounded by the
page count times the longest URL in the run, which is kilobytes for any site a crawler
actually meets. The URL is the one part that is NOT bounded here, and deliberately so: a
truncated URL is a broken link, which is strictly worse than a long one, and dropping bullets
to hit a byte target would defeat the point of an index. If a pathological run ever makes
this matter, the fix is a cap on URL length at the frontier — upstream, where an over-long
URL can be declined before it is fetched — not a truncation at the point of writing it out."""

_RUN_NOTICE_RESERVE_BYTES: Final = 256
"""Bytes held back from `MAX_FULL_TEXT_BYTES` so `_run_truncation_notice` always fits.

Reserved unconditionally, including on runs that never need it, because the alternative is
circular: how many bytes the notice needs depends on how many pages were omitted, which
depends on how much budget was left after the notice. Giving up 256 bytes of 5 MiB buys a
budget that is decided before the first page is appended and a document that is never one
byte over the cap it claims."""

_PAGE_TRUNCATION_NOTICE: Final = (
    f"\n\n_[Truncated: this page exceeded the {MAX_PAGE_TEXT_BYTES // 1024} KB per-page limit.]_"
)
"""What marks a page whose body hit `MAX_PAGE_TEXT_BYTES`. The artifact says so in its own
text rather than only in `runs.stats`, because a reader — human or model — consuming
`llms-full.txt` on its own has no access to the stats row, and a body that simply stops
mid-sentence is indistinguishable from a page the crawler genuinely found nothing more of."""

MAX_TEXT_CHARS: Final = 500
"""How long a title or a description may be before `_clean` cuts it.

**This is a cap, not a formatting preference, and it is what makes the other two caps hold.**
Every other bound in this module governs a page's markdown, and none of them covers a title
or a description — but both are extracted from a crawled page, which is to say both are
chosen by the site being crawled rather than by this codebase. Without this, a single page
with an eight-megabyte `<title>` produces an eight-megabyte `llms-full.txt` header before the
first page section is even considered, and `MAX_FULL_TEXT_BYTES` never gets a say; the index,
which has no size cap of its own precisely because bullets were assumed to be small, has the
same hole through a long description. `internals/extract.py` bounds neither, and should not
have to — it reports what the document said, and what is safe to inline into a column is this
module's question.

500 characters is far past any real value and still trivially bounded: a `<title>` is
conventionally under 60, an `og:description` under 200. Cutting at a limit no honest page
reaches means the truncation is invisible in practice and total in the adversarial case."""

_TEXT_ELLIPSIS: Final = "…"
"""Marks a title or description cut at `MAX_TEXT_CHARS`, so a truncated value is not passed
off as the whole of what the page said."""

_OTHER_SECTION: Final = "Other"
"""The catch-all heading, and the only section never derived from a readable path segment.

Two kinds of page land here: those with no leading path segment at all (the site root,
`https://example.test/`), and those whose leading segment yields no readable name — a
filename like `/sitemap.xml`, or punctuation that humanizes to nothing. Everything else gets
a section named after its own segment, so this stays genuinely a catch-all rather than the
default."""

_SECTION_NAMES: Final = {
    "api": "API",
    "apis": "API",
    "doc": "Docs",
    "docs": "Docs",
    "documentation": "Docs",
    "guide": "Guide",
    "guides": "Guide",
    "reference": "Reference",
    "references": "Reference",
}
"""Path segments whose readable name is not what humanizing them produces.

Two jobs, and neither is "enumerate every segment a site might use". The first is acronyms:
`"api".title()` is `"Api"`, wrong in a way a reader notices immediately. The second is synonym
merging — `/doc/`, `/docs/` and `/documentation/` are one section of a site, not three, and a
site using two of them must not get two headings. Every other segment is humanized by
`_section_for` and needs no entry here; adding one for a segment that humanizes correctly and
has no synonym would be maintenance for nothing."""

_SECTION_ORDER: Final = ("Docs", "Guide", "API", "Reference")
"""The curated sections, in the order a reader most likely wants them, ahead of every section
derived by humanizing a segment.

Sections outside this tuple sort alphabetically after it, and `_OTHER_SECTION` sorts after
those — see `_section_rank`. Fixed and total, which is what makes it reproducible; not
claimed to be optimal for every site."""

_ACRONYMS: Final = frozenset(
    {"ai", "api", "cli", "faq", "http", "json", "llm", "sdk", "ui", "ux", "yaml"}
)
"""Path-segment words that are read as acronyms rather than words, so `api-reference` becomes
"API Reference" and not "Api Reference". Consulted per word by `_humanize`, which is why
`_SECTION_NAMES` above still needs its own `api` entry: that table merges synonyms, this set
only fixes casing."""

_LABEL_ESCAPES: Final = (("\\", "\\\\"), ("[", "\\["), ("]", "\\]"))
"""Characters that would end a markdown link's `[label]` early, and their escapes. The
backslash is replaced FIRST — order matters, since escaping a bracket introduces a backslash
a later pass would otherwise escape again."""

_TARGET_ESCAPES: Final = ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E"))
"""Characters that would end a markdown link's `(target)` early, percent-encoded rather than
backslash-escaped. Percent-encoding is identity-preserving for a URL — a client following
`%28` arrives exactly where `(` would have taken it — whereas a backslash inside a URL is a
character the URL did not contain. These should be rare: `internals/url_normalize.py` has
already been over every URL that reaches here. Rare is not never, and one unescaped
parenthesis silently corrupts the rest of the line."""

_EMPTY_DOCUMENT: Final = "# llms.txt\n\n> No pages were fetched during this run.\n"
"""What both generators return for `pages == []`: still exactly one H1 and one blockquote, so
an empty run's artifact is the same SHAPE as a full one rather than a special case every
consumer has to detect. Never `""` — an empty string in a `text` column is indistinguishable
from a bug that wrote nothing.

A crawl whose seed failed never reaches this module (`CrawlService.execute_run`
short-circuits on `CrawlResult.seed_error` first), but a frontier that legitimately yielded
nothing is not this module's problem to raise about."""


@dataclass(frozen=True, slots=True)
class _Entry:
    """One page that survived selection, with everything both artifacts need already derived.

    Built once by `_index_entries` so the index and the full text cannot disagree about which
    pages are in, what each is called, or what order they come in. `label` and `description`
    are already whitespace-collapsed; escaping happens at render time, not here, because the
    same `label` is a markdown link text in one artifact and a heading in the other, and only
    the first of those needs brackets escaped.
    """

    section: str
    url: str
    label: str
    description: str | None
    markdown: str


def generate_llms_txt(pages: list[CrawledPage]) -> str:
    """Build the `llms.txt` index for `pages`, in the llmstxt.org shape.

    Exactly one H1 (the project name), exactly one blockquote (a factual summary), then an H2
    per section, each holding one annotated link per page:

    ```
    # Acme Docs

    > An index of 12 pages from https://acme.test. Excludes 3 pages with no extractable
    > content.

    ## Docs

    - [Configuration](https://acme.test/docs/config): How to configure Acme.
    ```

    Pages whose extraction came back empty (`CrawledPage.is_empty`) are omitted, and this is
    the one place in the codebase that branches on that flag. It is a narrowing of
    ARCHITECTURE.md §3.4's rule rather than a breach of it: §3.4 forbids anything *upstream*
    of this seam from deciding what to fetch, or what to keep, because a page is empty — and
    none does. The page was fetched, it is in the run's payload, and it is still counted in
    `runs.stats["pages_empty_content"]` exactly as before. What it is not is a line in a
    curated index, because a bullet naming a URL with no title and no description tells a
    reader nothing the sitemap would not have.

    Args:
        pages: Every page `internals/crawler.py`'s `crawl_site` collected for one run, in
            whatever order they finished fetching. Never sorted by the caller — sorting
            happens here, once, so every caller gets determinism for free.

    Returns:
        The artifact, ending in a newline. `_EMPTY_DOCUMENT` for `pages == []`.
    """
    if not pages:
        return _EMPTY_DOCUMENT

    origin = _origin(pages)
    entries = _index_entries(pages)
    lines = [
        f"# {_project_name(pages, origin)}",
        "",
        _index_summary(len(entries), len(pages) - len(entries), origin),
    ]

    section: str | None = None
    for entry in entries:
        if entry.section != section:
            section = entry.section
            lines.extend(("", f"## {section}", ""))
        lines.append(_bullet(entry))

    lines.append("")
    return "\n".join(lines)


def generate_llms_full_txt(pages: list[CrawledPage]) -> str:
    """Build the `llms-full.txt` corpus for `pages`: the same H1 and a blockquote of its own,
    then `## {title}` and that page's markdown, once per page, **in the same order the index
    lists them** — section order first, then URL order within a section, not plain URL order
    across the whole run.

    The two artifacts are read together: a consumer that finds a link in `llms.txt` and wants
    the page behind it should find the bodies in the order the links were in. Sorting this one
    by URL instead would be a second, silently different ordering for no gain.

    The document header is this module's own addition rather than the ticket's, and it earns
    its two lines: it makes the stored column self-describing, it makes the empty case fall
    out as the same stable document `generate_llms_txt` returns instead of a bare `""`, and it
    lets one `_project_name` derivation serve both artifacts. The per-page shape below it is
    exactly what was specified.

    **No `<|firecrawl-page-N-lllmstxt|>` separators**, deliberately. Firecrawl's
    implementation emits them between pages and then strips them out again with a regex
    (`removePageSeparators`) before anything consumes the result; they are vestigial, and
    copying a marker whose own author removes it would be copying the bug rather than the
    feature. An H2 already delimits a page, in a way a markdown reader understands.

    Both caps live here — see `_plan_full_txt` for how they are enforced, and
    `count_full_txt_truncations` for the number a run records about it.

    Args:
        pages: As `generate_llms_txt`.

    Returns:
        The artifact. `_EMPTY_DOCUMENT` for `pages == []`. Never larger than
        `MAX_FULL_TEXT_BYTES` when encoded as UTF-8.
    """
    return "".join(_plan_full_txt(pages)[0])


def count_indexed_pages(pages: list[CrawledPage]) -> int:
    """How many links `generate_llms_txt(pages)` lists — `runs.stats["links_emitted"]`.

    Shares `_index_entries` with the generator rather than recounting, so this can disagree
    with the artifact only if the artifact disagrees with itself. Equal to `len(pages)` minus
    the pages skipped for `is_empty`, which is why `links_emitted` and `pages_crawled` diverge
    from `RUN_STATS_VERSION` 3 onwards (`internals/run_stats.py`).

    Deliberately not computed by the caller as `sum(1 for p in pages if not p.is_empty)`: that
    is correct only while "empty" is the ONLY reason a page can be left out, which is a
    property of today's selection rule rather than of the data. Ask the artifact what it
    listed.
    """
    return len(_index_entries(pages))


def count_full_txt_truncations(pages: list[CrawledPage]) -> int:
    """How many pages `generate_llms_full_txt(pages)` could not carry in full —
    `runs.stats["full_txt_truncated"]`.

    **Counts both ways a page can lose content**, and the distinction is worth stating,
    because "truncated" could be read as only the first:

    * a page whose markdown exceeded `MAX_PAGE_TEXT_BYTES` and was trimmed, and
    * a page omitted entirely once `MAX_FULL_TEXT_BYTES` stopped the append.

    An omitted page lost strictly more of itself than a trimmed one, so a counter reporting
    only trims would understate the loss precisely in the runs where it was worst — a run that
    hit the whole-artifact cap on its second page would report `1` while silently dropping a
    hundred. One number covering both answers the question this stat is actually asked ("how
    much of this run's text is missing from the artifact?"); splitting it in two is a change
    to `runs.stats`' shape and would need its own version bump.

    A page that is both trimmed and then omitted counts once: this is a count of pages missing
    content, not of truncation events. It is `0` on every run whose full text is complete.
    """
    return _plan_full_txt(pages)[1]


def _plan_full_txt(pages: list[CrawledPage]) -> tuple[list[str], int]:
    """Decide the whole of `llms-full.txt`: its chunks in order, and how many pages were cut.

    One implementation, two public callers (`generate_llms_full_txt` joins the chunks,
    `count_full_txt_truncations` takes the number), because the caps are arithmetic that must
    not be performed twice from two places.

    **`MAX_FULL_TEXT_BYTES` stops at a page boundary, never mid-page.** Once the next page's
    section would not fit, this stops appending entirely and charges every remaining page to
    the truncation count. Cutting a final page mid-sentence to fill the budget exactly would
    buy a few kilobytes of a document nobody needs to be exactly 5 MiB, at the cost of leaving
    a reader unable to tell a trimmed page from a complete one — the per-page marker exists
    precisely so that is never guesswork, and a mid-page cut could land inside a fenced code
    block and leave it unterminated.

    **First-fit-stop, not best-fit**: this does not skip an oversized page to squeeze in a
    smaller one after it. The output stays a strict PREFIX of the index order, which is what
    makes "the same order as the index" checkable and makes the truncation count easy to
    reason about — a suffix of the index is missing, never holes in the middle.

    The budget covers the whole rendered document, headings and blank lines included, because
    what is being bounded is the size of a Postgres value rather than the size of the prose.
    The `_PAGE_TRUNCATION_NOTICE` appended to a trimmed page does not count against that
    page's own `MAX_PAGE_TEXT_BYTES` — that cap bounds a page's CONTENT, and charging our own
    annotation to it would mean the more a page was truncated the less of it survived.
    """
    if not pages:
        return [_EMPTY_DOCUMENT], 0

    origin = _origin(pages)
    entries = _index_entries(pages)
    header = f"# {_project_name(pages, origin)}\n\n{_full_summary(len(entries), origin)}\n"

    chunks = [header]
    budget = MAX_FULL_TEXT_BYTES - _RUN_NOTICE_RESERVE_BYTES
    used = len(header.encode())
    truncated = 0

    for position, entry in enumerate(entries):
        body, was_trimmed = _truncate(entry.markdown)
        section = f"\n## {entry.label}\n\n{body}\n"
        size = len(section.encode())
        if used + size > budget:
            omitted = len(entries) - position
            chunks.append(_run_truncation_notice(omitted))
            return chunks, truncated + omitted
        chunks.append(section)
        used += size
        truncated += int(was_trimmed)

    return chunks, truncated


def _run_truncation_notice(omitted: int) -> str:
    """The line that closes a `llms-full.txt` stopped by `MAX_FULL_TEXT_BYTES`, naming how
    many pages are missing. Symmetric with `_PAGE_TRUNCATION_NOTICE`, and for the same
    reason: a document that simply ends is indistinguishable from a complete one. Sized to
    fit `_RUN_NOTICE_RESERVE_BYTES` at any page count this crawler can reach."""
    limit = MAX_FULL_TEXT_BYTES // (1024 * 1024)
    return f"\n_[Truncated: {_page_count(omitted)} omitted at the {limit} MB per-run limit.]_\n"


def _truncate(markdown: str) -> tuple[str, bool]:
    """Trim `markdown` to `MAX_PAGE_TEXT_BYTES` if it is over, and say whether it was.

    Sliced on the ENCODED bytes and decoded with `errors="ignore"`, so a cut landing inside a
    multi-byte character drops that character rather than emitting a lone continuation byte or
    a replacement character. `errors="ignore"` can only ever discard an incomplete trailing
    sequence here, because everything before the cut is by construction valid UTF-8 that came
    from a `str`. Slicing the `str` by characters instead would satisfy no byte budget at all
    on any text that is not pure ASCII, which is the whole point of the cap.
    """
    encoded = markdown.encode()
    if len(encoded) <= MAX_PAGE_TEXT_BYTES:
        return markdown, False
    kept = encoded[:MAX_PAGE_TEXT_BYTES].decode("utf-8", errors="ignore")
    return f"{kept}{_PAGE_TRUNCATION_NOTICE}", True


def _index_entries(pages: list[CrawledPage]) -> list[_Entry]:
    """Select, label and order the pages both artifacts are built from.

    Sorted by URL before anything is derived (the module docstring's determinism contract),
    then re-sorted onto `(section rank, section name, url)`. Python's sort is stable, so the
    second pass keeps URL order within a section for free.

    The first sort's key carries three fields beyond the URL. They only ever break a tie
    between two pages that share a URL — which `crawl_site` cannot produce, since its frontier
    visits each URL once — but a hand-built list can, and without them a shuffle of such a
    list would change the output and quietly break the one property this module promises.
    """
    ordered = sorted(pages, key=_ordering_key)
    entries = [
        _Entry(
            section=_section_for(page.url),
            url=page.url,
            label=_label_for(page),
            description=_clean(page.description),
            markdown=page.markdown,
        )
        for page in ordered
        if not page.is_empty
    ]
    entries.sort(key=lambda entry: (_section_rank(entry.section), entry.section))
    return entries


def _ordering_key(page: CrawledPage) -> tuple[str, str, str, str]:
    """The total order every pass over `pages` in this module uses.

    URL first — the determinism contract the module docstring states. The three fields after
    it only ever break a tie between two pages that share a URL, which `crawl_site` cannot
    produce (its frontier visits each URL once) but a hand-built list can. Without them the
    sort is merely STABLE rather than total, which means it preserves input order for such a
    pair — and input order is exactly what this module promises never to depend on.

    Shared by `_index_entries` and `_project_name` rather than written out at each call site,
    because the two disagreeing is the specific bug this function exists to prevent: an
    earlier revision keyed `_project_name` on the URL alone, and two root pages with different
    titles produced a different H1 depending on which one the caller happened to list first.
    """
    return (page.url, page.title or "", page.description or "", page.markdown)


def _section_for(url: str) -> str:
    """Which H2 `url` belongs under: its leading path segment, made readable.

    A curated name if `_SECTION_NAMES` has one, otherwise the segment humanized
    (`/getting-started/x` -> "Getting Started"). No leading segment at all — the site root —
    means `_OTHER_SECTION`, and so does a segment that is really a filename (`/sitemap.xml`,
    which would otherwise become the section "Sitemap.Xml") or that humanizes to nothing.

    The segment is taken whether or not the page is that segment's own index: `/docs` and
    `/docs/intro` both land under "Docs", which is what a reader expects of a section and its
    landing page.
    """
    segments = _path_segments(url)
    if not segments:
        return _OTHER_SECTION
    segment = segments[0].lower()
    if segment in _SECTION_NAMES:
        return _SECTION_NAMES[segment]
    if "." in segment:
        return _OTHER_SECTION
    return _humanize(segment) or _OTHER_SECTION


def _section_rank(section: str) -> int:
    """Sort position for a section name: curated sections in `_SECTION_ORDER`'s order first,
    then everything else (which the caller's next sort key orders alphabetically), then
    `_OTHER_SECTION` last."""
    if section == _OTHER_SECTION:
        return len(_SECTION_ORDER) + 1
    if section in _SECTION_ORDER:
        return _SECTION_ORDER.index(section)
    return len(_SECTION_ORDER)


def _humanize(segment: str) -> str:
    """A URL slug as a reader would write it: `api-reference` -> "API Reference".

    Split on `-` and `_`, uppercase the words `_ACRONYMS` names, capitalize the rest. Returns
    `""` for a segment with no word characters at all, which `_section_for` and `_label_for`
    both treat as "no usable name" rather than emitting an empty heading.
    """
    words = [word for word in segment.replace("_", "-").split("-") if word]
    return " ".join(word.upper() if word in _ACRONYMS else word.capitalize() for word in words)


def _path_segments(url: str) -> list[str]:
    return [segment for segment in urlsplit(url).path.split("/") if segment]


def _label_for(page: CrawledPage) -> str:
    """What to call `page` in a link and in an H2 heading: its extracted title, or its last
    path segment humanized, or the host.

    Never the raw URL: the URL is already the link's target, and `- [https://…](https://…)` is
    noise rather than information. A page with no title and no path segment is the site root,
    where the host is the only thing left that names it. A trailing filename has its extension
    dropped first, so `/docs/intro.html` reads "Intro" rather than "Intro.html".
    """
    title = _clean(page.title)
    if title:
        return title
    segments = _path_segments(page.url)
    if segments:
        # Through `_clean` as well, not just the title above: a path segment is as much the
        # crawled site's choice as a `<title>` is, so the fallback needs the same bound.
        label = _clean(_humanize(segments[-1].rsplit(".", 1)[0]))
        if label:
            return label
    return _clean(urlsplit(page.url).netloc) or urlsplit(page.url).netloc


def _origin(pages: list[CrawledPage]) -> str:
    """The scheme and host every page in this run shares, taken from the alphabetically first
    URL. Sorted rather than "the first page in the list" for the module docstring's reason:
    the list's order is whatever the frontier's races produced."""
    first = urlsplit(min(page.url for page in pages))
    return f"{first.scheme}://{first.netloc}"


def _project_name(pages: list[CrawledPage], origin: str) -> str:
    """The H1: the site's own title if this run fetched a page carrying one, else `origin`.

    "The site's title" is specifically the title of the page AT the origin's root — the
    alphabetically first such page with a usable title. A deep page's title describes that
    page ("Configuration | Acme Docs"), not the site, so falling back to "the first page that
    has any title" would put whichever subpage sorts first on the whole artifact. Falling back
    to the origin instead is honest: a bare host is obviously an identifier rather than a
    name, where a mislabelled H1 is not obviously anything.

    **Consults the root page even when it is `is_empty`**, which looks inconsistent with the
    index skipping those pages and is not. `internals/extract.py` deliberately keeps the title
    of a JavaScript shell — "a shell is real HTML with a real `<title>`" — and a documentation
    SPA whose homepage is a mount div is exactly the case this fallback exists for. The skip
    rule governs what the index LISTS; it is not a rule about which pages may contribute
    metadata.

    The origin comparison is not redundant with the crawler being same-origin: a page's `url`
    is the FINAL url after redirects (`CrawledPage.url`), so a redirect off-origin can leave a
    page here whose root is not this run's root.
    """
    for page in sorted(pages, key=_ordering_key):
        parts = urlsplit(page.url)
        if f"{parts.scheme}://{parts.netloc}" != origin or parts.path.strip("/") or parts.query:
            continue
        title = _clean(page.title)
        if title:
            return title
    return origin


def _index_summary(indexed: int, skipped: int, origin: str) -> str:
    """`llms.txt`'s blockquote: what the artifact contains, and what it leaves out.

    Factual and countable, with no adjectives and no claim about quality — and, since PER-179,
    no disclaimer about being provisional, which is what this artifact used to lead with. The
    second sentence appears only when something was actually excluded, so a run that indexed
    everything it fetched does not carry a clause about nothing.
    """
    summary = f"> An index of {_page_count(indexed)} from {origin}."
    if skipped:
        summary += f" Excludes {_page_count(skipped)} with no extractable content."
    return summary


def _full_summary(indexed: int, origin: str) -> str:
    """`llms-full.txt`'s blockquote. Says what this document is rather than repeating the
    index's exclusion clause: a reader holding the expansion wants to know it is the full text
    of the indexed pages, and the index beside it already accounts for what was left out."""
    return f"> The full text of {_page_count(indexed)} from {origin}."


def _page_count(count: int) -> str:
    return "1 page" if count == 1 else f"{count} pages"


def _bullet(entry: _Entry) -> str:
    """One `- [label](url): description` line, with the description omitted entirely rather
    than left as a dangling `: ` when the page has none. A description is optional in the
    llmstxt.org shape, and an empty one is not a different thing from an absent one."""
    link = f"- [{_escape_label(entry.label)}]({_escape_target(entry.url)})"
    return f"{link}: {entry.description}" if entry.description else link


def _escape_label(label: str) -> str:
    for character, escaped in _LABEL_ESCAPES:
        label = label.replace(character, escaped)
    return label


def _escape_target(url: str) -> str:
    for character, escaped in _TARGET_ESCAPES:
        url = url.replace(character, escaped)
    return url


def _clean(text: str | None) -> str | None:
    """Collapse every whitespace run in `text` to one space, or return `None`.

    A title or description is emitted inside a single markdown line, and extraction reads both
    out of markup where a newline is insignificant — a `<title>` split across three indented
    source lines is one title. Left alone, that newline would end the bullet and turn the rest
    of the title into a stray paragraph.

    Returns `None`, never `""`, for both an absent value and one that was entirely whitespace,
    so callers can keep writing `_clean(x) or fallback` — the same reason
    `ExtractedContent.title` is `None` rather than empty.

    Also the one place a title or description is bounded, at `MAX_TEXT_CHARS` — see that
    constant for why an unbounded one is a real problem rather than an untidy one. Every value
    this module puts in an H1, an H2 or a bullet passes through here, which is what makes one
    check sufficient.
    """
    if text is None:
        return None
    collapsed = " ".join(text.split())
    if len(collapsed) > MAX_TEXT_CHARS:
        return collapsed[:MAX_TEXT_CHARS].rstrip() + _TEXT_ELLIPSIS
    return collapsed or None
