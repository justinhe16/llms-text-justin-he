"""Diffing one run's `llms.txt` index against the previous completed run's — the "what changed
in the latest run" block the Trends tab reads through `runs.stats["index_diff"]`
(`internals/run_stats.py`, `RUN_STATS_VERSION` 8).

Pure, feature-owned, no I/O — the same category `internals/run_stats.py` and
`internals/llms_txt.py` already occupy, and for the same reason CLAUDE.md #9 states for
those two: everything downstream of a fetched page stays behind a model-free seam, and this
module sits one layer further downstream than either, describing the DIFFERENCE between two
already-generated artifacts rather than generating one.

**Why this is a diff of two STRINGS, not a diff of two page lists — and, as of PER-194, not
ONLY that.** `runs.stats` cannot afford to carry a stored `indexed_pages: [{url, title}]`
list of its own — `_LIST_COLUMNS` (`runs/internals/runs_reader.py`) deliberately excludes
`llms_txt` from every row of `GET /websites/{id}/runs`, a list the Runs tab polls every three
seconds while a run is active, and re-adding an equivalent list under another name inside
`stats` would undo that exclusion at up to `crawl_max_pages` entries per row. So this module
reads back the ONE column that already exists cheaply enough to fetch per run —
`runs.llms_txt` itself, "kilobytes for any site a crawler actually meets" (`llms_txt.py`'s
`MAX_FULL_TEXT_BYTES` docstring) — and recovers the page list by parsing it.
`build_content_hashes` below is the one exception to "pure module, no I/O awareness of
`CrawledPage`": it imports `CrawledPage` from `app.features.crawl.schemas` — a frozen
dataclass with no I/O of its own — to hash a run's page BODIES as a side-channel alongside
the parsed-string diff, because a mode flip (enrichment turning on or off) rewrites title and
description without touching a page's body at all, and that side-channel is what lets this
module tell "the metadata changed because a different author wrote it" apart from "the page
itself changed" (see `build_index_diff`'s own docstring).

**Parsing BOTH sides, current and previous, is the part that actually matters.** The tempting
alternative — build the current run's entry list directly from `artifact_pages`, the same
`CrawledPage`s `generate_llms_txt` was just handed, and diff that against a PARSED previous
list — would make any imperfection in the escape round-trip (`_escape_label`/`_escape_target`
in `llms_txt.py`, `_clean`'s 500-character ellipsis) look like a page swap on every single run:
today's list built one way, yesterday's rebuilt the other way, compared as if they were the
same kind of thing. Running both sides through the identical parser makes those imperfections
cancel exactly — a title truncated the same way on both sides diffs as unchanged, because both
sides now describe what the ARTIFACT says rather than what the crawler originally saw. It also
matches what the Trends tab actually claims to show: "what changed in the site's `llms.txt`
between runs," not "what changed in the site." `test_parse_index_round_trips_generate_llms_txt`
in `tests/test_index_diff.py` is the drift gate this trades for: `parse_index` and `_bullet`
(`llms_txt.py`) are two descriptions of one format, and that test is the only thing tying them
together — see the cross-reference comment on `_bullet` itself.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.features.crawl.internals.url_ranking import normalize_url
from app.features.crawl.schemas import CrawledPage


SAMPLE_LIMIT: Final = 10
"""How many entries `added_sample`/`removed_sample`/`changed_sample` each carry, at most.

Sized against `MAX_SAMPLE_TITLE_CHARS` below for the byte budget `runs.stats["index_diff"]`
adds to a row: three samples times ten entries times (~200 bytes of URL plus up to 120 bytes
of title) is on the order of 10 KB worst case, 1-2 KB typical — a number worth stating in the
constant it depends on so a future "let's show 50" has to argue against it explicitly, the
same way `internals/llms_txt.py`'s `MAX_TEXT_CHARS` states what it protects. The true count
(`pages_added`/`pages_removed`/`pages_changed`) is always recorded beside the sample, so the
UI never has to say "and more" without a number to put after it."""

MAX_SAMPLE_TITLE_CHARS: Final = 120
"""How long a sample's `title` may be before it is cut. Deliberately smaller than
`internals/llms_txt.py`'s `MAX_TEXT_CHARS` (500): that cap bounds ONE title stored once, in
`runs.llms_txt`; this one bounds up to 30 titles (three sample lists of up to ten) riding
`RunListItemResponse.stats` on every row of every page of `GET /websites/{id}/runs` — a much
tighter budget for a much more repeated cost."""

_ELLIPSIS: Final = "…"


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One bullet recovered from a parsed `llms.txt`, on either side of a diff."""

    section: str
    """The `## ` heading this entry appeared under, exactly as written — never escaped in
    `llms_txt.py`'s `_bullet`/`_index_entries`, so nothing here needs to unescape it either."""

    url: str
    """The link target, unescaped back to the URL `generate_llms_txt` was actually given —
    see `_unescape_target`. This is what `generate_llms_txt` listed, not necessarily what
    `select_urls` selected upstream of it (Q1's "selected pages" note, below)."""

    key: str
    """`normalize_url(url)`, falling back to `url` itself when `url` does not parse — the
    same normalized form `select_urls` (`internals/url_ranking.py`) already compares
    candidates by. Comparing entries on THIS field, not on `url`, is what makes a
    trailing-slash-only or tracking-parameter-only change read as the same page rather than
    a swap — see `test_a_trailing_slash_change_is_not_a_page_swap`."""

    title: str | None
    """The bullet's link label, unescaped back to the original title (or the fallback label
    `_label_for` chose) — see `_unescape_label`. In practice never `None` for a bullet
    `generate_llms_txt` actually wrote (`_label_for` always returns something), but typed
    optional to match `IndexPageRef.title` on the API side rather than asserting a guarantee
    this module does not itself enforce."""

    description: str | None
    """The text after `): ` on the bullet's line, verbatim — `_bullet` never escapes a
    description, so nothing here unescapes it either. `None` when the bullet carried no
    `: description` suffix at all."""


@dataclass(frozen=True, slots=True)
class PreviousIndex:
    """What `CrawlService._build_index_diff` (`crawl/service.py`) has in hand about the
    previous completed run, already reduced to the JSON-safe values this module needs — built
    from `RunService.get_previous_completed_index`'s `PreviousCompletedIndex`, one layer
    up."""

    run_id: str
    """Already `str(UUID)`. This module writes only JSON-safe values into the dict it
    returns — no `UUID`, no `datetime` — because that dict is spread straight into
    `runs.stats` and re-encoded as jsonb by `RunsWriter`, never round-tripped through a
    Pydantic model on the write side."""

    completed_at: str | None
    """Already ISO-8601 (`.isoformat()`), or `None` — see `run_id` above for why this
    module never accepts a raw `datetime`."""

    llms_txt: str
    """The previous completed run's stored `llms_txt` column, unparsed — this module parses
    it exactly once, the same way it parses `current_llms_txt`."""

    urls_discovered: int | None
    """The previous run's `runs.stats["urls_discovered"]`, read defensively by the caller —
    `None` when that row predates `RUN_STATS_VERSION` 4 (`internals/run_stats.py`) or the
    value there is not an `int`. `None` here is what makes `urls_discovered_delta` below
    `None` rather than a number computed against a value that was never really there."""

    enrich_applied: bool | None
    """The previous run's `runs.stats["enrich_applied"]`, read defensively by the caller —
    `None` when that row predates `RUN_STATS_VERSION` 8 or the value there is not a `bool`
    (PER-194). **`None` is treated as COMPARABLE, not as "unknown mode"** — see
    `build_index_diff`'s own docstring for why a pre-version-8 row is assumed to be in the
    same mode as the current run rather than triggering a not-comparable result on every
    website's first post-deploy run."""

    content_hashes: dict[str, str] | None
    """The previous run's `runs.stats["content_hashes"]`, read defensively by the caller —
    `None` when that row predates `RUN_STATS_VERSION` 8, the value there is not a `dict`, or
    any key or value in it is not a `str` (PER-194). `None` here is what makes
    `content_changed` below `None` rather than a count computed against hashes that were
    never really there — the identical null rule `urls_discovered` above already holds for
    `urls_discovered_delta`."""


CONTENT_HASH_CHARS: Final = 16
"""How many hex characters of a page's sha256 `build_content_hashes` keeps — 64 bits. This is
change DETECTION, not a security boundary, so a birthday collision only costs a false
"unchanged" on two genuinely different bodies that happen to hash the same: at 500 entries
(comfortably above `Settings.crawl_max_pages`'s default of 100) the collision probability is
on the order of 7e-15, which is not a number worth spending the other 48 hex characters on."""


def build_content_hashes(pages: list[CrawledPage]) -> dict[str, str]:
    """A `{normalized_url: content_hash}` map of `pages`' bodies — the side-channel
    `build_index_diff` joins against the previous run's own map (via `PreviousIndex.
    content_hashes`) to compute `content_changed` independently of whatever a mode flip did
    to title and description (PER-194).

    **The key is character-identical to `IndexEntry.key`'s own construction** —
    `normalize_url(page.url) or page.url` — which is what makes the join in `build_index_diff`
    exact rather than approximate: every key this function emits either matches an
    `IndexEntry.key` derived from the same URL or matches nothing, and there is no third
    case where the two normalization rules quietly disagree, because there is only one rule.

    **The value is a truncated sha256**, `hashlib.sha256(page.markdown.encode("utf-8")
    ).hexdigest()[:CONTENT_HASH_CHARS]` — see `CONTENT_HASH_CHARS` for the collision
    arithmetic.

    **Populated from every page whose `markdown.strip()` is non-empty — deliberately NOT a
    branch on `CrawledPage.is_empty`.** ARCHITECTURE.md §3.4 is explicit that
    `generate_llms_txt` is "the ONE place in the codebase that branches on that flag";
    `internals/enrich.py`'s own module docstring already refuses the same shortcut for the
    identical reason, and this function holds the line a third time. The two signals can
    disagree in both directions — a page can be `is_empty` with non-blank `markdown` (a
    JavaScript shell whose extracted body is short but not zero) or have blank `markdown`
    while `is_empty` is `False` in principle — and reading `is_empty` here would make this
    function's population rule silently depend on `internals/extract.py`'s `MIN_BODY_CHARS`
    threshold instead of on its own stated one.

    **The key set is therefore a SUPERSET of the index** `parse_index` recovers from this same
    run's `llms.txt` — `generate_llms_txt` additionally omits an empty page from the artifact
    entirely, so a page with (say) ten characters of markdown can appear here and never appear
    as an `IndexEntry`. That is safe by construction: `build_index_diff` intersects this map's
    keys with `current_keys & previous_keys` before counting anything, so an extra key here
    simply never joins to anything and is silently ignored, and no `IndexEntry` can ever lack
    a corresponding hash (the index's population rule is strictly narrower than this one).

    **Sized like `SAMPLE_LIMIT` above bounds its own budget.** One entry per page with
    non-empty markdown, each entry roughly a normalized URL (~100-200 bytes) plus 16 hex
    characters, bounded by `Settings.crawl_max_pages` (default 100) — on the order of 10 KB
    typical, ~22 KB worst case for a run at the cap. That is real enough to matter on a
    row the Runs tab polls every three seconds, which is why `runs/service.py`'s
    `_public_stats` strips this key from every API response before it ever reaches a client;
    this function itself has no opinion about who reads its output.

    Args:
        pages: `result.pages` — the run's ORIGINALLY EXTRACTED pages, never `artifact_pages`.
            Enrichment (`internals/enrich.py`'s `apply_summaries`) rewrites `title` and
            `description` and nothing else, so the two lists produce identical hashes here —
            but reading `result.pages` makes "the body fingerprint is mode-independent" a
            property of the code this function is called with, rather than a coincidence a
            future refactor could quietly break. See the call site in `crawl/service.py` for
            the same sentence, repeated at the point where the choice is actually made.

    Returns:
        A JSON-safe `dict[str, str]` — every key and value is a plain `str` — ready to sit
        under `"content_hashes"` in the dict `build_run_stats` returns. `{}` for an empty
        `pages` list, never `None`.
    """
    hashes: dict[str, str] = {}
    for page in pages:
        stripped = page.markdown.strip()
        if not stripped:
            continue
        key = normalize_url(page.url) or page.url
        digest = hashlib.sha256(page.markdown.encode("utf-8")).hexdigest()
        hashes[key] = digest[:CONTENT_HASH_CHARS]
    return hashes


# Mirrors `llms_txt.py`'s `_LABEL_ESCAPES`, in REVERSE order — `_escape_label` replaces
# backslash first, then `[`, then `]`, so undoing it correctly requires undoing `]` first and
# backslash LAST; doing it in forward order here would un-escape a literal `\]` that was never
# an escaped bracket to begin with. See `_bullet`'s own cross-reference comment and
# `tests/test_index_diff.py::test_parse_index_round_trips_generate_llms_txt`, the one test
# tying this table to that one.
_LABEL_UNESCAPES: Final = (("\\]", "]"), ("\\[", "["), ("\\\\", "\\"))

# Mirrors `llms_txt.py`'s `_TARGET_ESCAPES`. Order does not affect correctness here — none of
# the five percent-encoded forms is a substring of another, unlike the label escapes above —
# but it is written in reverse for the same symmetry. Deliberately NOT `urllib.parse.unquote`:
# that would also decode a `%2F` or any other percent-encoded sequence the ORIGINAL URL
# genuinely contained, which these five specific pairs never touch.
_TARGET_UNESCAPES: Final = (
    ("%3E", ">"),
    ("%3C", "<"),
    ("%29", ")"),
    ("%28", "("),
    ("%20", " "),
)

# One bullet line: `- [label](target)` with an optional `: description` suffix. The label
# group is escape-aware (`\\.` consumes an escaped pair as one unit before `[^\]]` can see
# either half of it), which is what lets an escaped `\]` inside a label not end the match
# early. The target group is a plain `[^)]*` because `_escape_target` guarantees the encoded
# URL contains no literal `)` — every one was already turned into `%29` before this module
# ever sees it.
_BULLET_PATTERN: Final = re.compile(r"^- \[((?:\\.|[^\]])*)\]\(([^)]*)\)(?:: (.*))?$")


def _unescape_label(label: str) -> str:
    for escaped, character in _LABEL_UNESCAPES:
        label = label.replace(escaped, character)
    return label


def _unescape_target(url: str) -> str:
    for escaped, character in _TARGET_UNESCAPES:
        url = url.replace(escaped, character)
    return url


def parse_index(llms_txt: str) -> list[IndexEntry]:
    """Recover the entries `generate_llms_txt` listed, from the text it produced.

    Walks `llms_txt` line by line: a `## ` line sets the current section; a `- [...](...)`
    line — matched by `_BULLET_PATTERN` — yields one `IndexEntry` under it; every other line
    (the `# ` heading, the `> ` summary, blank lines, and any line this module fails to
    recognize) is ignored. **Never raises** — a line that does not match the bullet pattern
    is silently skipped rather than treated as an error, the same defensive posture
    `RunService._parse_stats` takes toward `runs.stats` itself: this function's input is a
    column this codebase wrote, but treating a parse failure as fatal would turn "an artifact
    format changed" into "the Trends tab 500s," which is a worse failure than under-reporting
    one bullet.

    Args:
        llms_txt: A run's stored `llms.txt` column — either side of a diff. `_EMPTY_DOCUMENT`
            (`llms_txt.py`) parses to `[]`, the same as any string with no bullet lines.

    Returns:
        Entries in the order they appeared in the document — section order, then the URL
        order `_index_entries` already sorted them into. `build_index_diff` re-sorts by
        `key` for its own samples, so this order is not load-bearing beyond being
        deterministic for a deterministic input, which `generate_llms_txt` already
        guarantees.
    """
    entries: list[IndexEntry] = []
    section = ""
    for line in llms_txt.splitlines():
        if line.startswith("## "):
            section = line[3:]
            continue
        if not line.startswith("- ["):
            continue
        match = _BULLET_PATTERN.match(line)
        if match is None:
            continue
        raw_label, raw_target, raw_description = match.groups()
        url = _unescape_target(raw_target)
        entries.append(
            IndexEntry(
                section=section,
                url=url,
                key=normalize_url(url) or url,
                title=_unescape_label(raw_label),
                description=raw_description,
            )
        )
    return entries


def _truncate_title(title: str | None) -> str | None:
    if title is None or len(title) <= MAX_SAMPLE_TITLE_CHARS:
        return title
    return title[:MAX_SAMPLE_TITLE_CHARS].rstrip() + _ELLIPSIS


def _sample(entries: list[IndexEntry]) -> list[dict[str, Any]]:
    """The first `SAMPLE_LIMIT` of `entries`, as `IndexPageRef`-shaped dicts.

    `entries` arrives already sorted by `key` — see `build_index_diff`'s three call sites —
    so this is deterministic under a shuffled INPUT to `build_index_diff`'s two `llms_txt`
    strings precisely because the strings themselves are already deterministic
    (`generate_llms_txt`'s own contract) and `key`-sorting removes the one remaining source of
    order that could vary, the order `parse_index` happened to encounter entries in.
    """
    return [
        {"url": entry.url, "title": _truncate_title(entry.title)}
        for entry in entries[:SAMPLE_LIMIT]
    ]


def _sections_delta(
    current_entries: list[IndexEntry], previous_entries: list[IndexEntry]
) -> dict[str, int]:
    """How many entries each section gained or lost, key-sorted, **containing only sections
    whose count actually changed** — the same "only rules that actually fired" discipline
    `SelectionResult.dropped` (`internals/url_ranking.py`) already holds its own dict to, and
    for the same reason: a section absent from this dict is a section with nothing to report,
    not a section this function forgot to visit.
    """
    current_counts = Counter(entry.section for entry in current_entries)
    previous_counts = Counter(entry.section for entry in previous_entries)
    deltas = {
        section: current_counts.get(section, 0) - previous_counts.get(section, 0)
        for section in current_counts.keys() | previous_counts.keys()
    }
    return {section: delta for section, delta in sorted(deltas.items()) if delta != 0}


MetadataNotComparableReason = Literal["enrichment_enabled", "enrichment_disabled"]


def build_index_diff(
    *,
    current_llms_txt: str,
    current_urls_discovered: int,
    current_enrich_applied: bool,
    current_content_hashes: dict[str, str],
    previous: PreviousIndex | None,
    previous_run_completed: bool | None,
) -> dict[str, Any]:
    """Build the `index_diff` block `RUN_STATS_VERSION` 8 stores in `runs.stats`.

    `previous is None` covers both `previous_run_completed` states that carry no comparison —
    no earlier run at all (`previous_run_completed is None`) and an earlier run that exists
    but never completed with an index of its own (`previous_run_completed is False`, or a
    completed row whose `llms_txt` was `NULL` — `RunService.get_previous_completed_index`
    returns `None` for that case too, deliberately, since a completed run with no index
    cannot be compared against). Either way the result is the `"first_run"` shape, carrying
    no metrics at all — there is nothing to measure a delta against, and a `0` in every field
    would claim a comparison that never happened.

    **Semantics, pinned here because nowhere else states them:**

    * **`metadata_changed`** — an entry whose `key` (see `IndexEntry.key`) is present on both
      sides, with a different `title` **or** a different `description`. A same-key entry
      with both fields byte-identical is neither added, removed, nor changed; it simply
      is not counted at all, the same way an untouched row does not appear in a diff. Named
      `metadata_changed`, not `pages_changed` (PER-194's rename): title and description are
      the ONLY thing enrichment ever rewrites, so this is the one signal a mode flip
      contaminates, and the honest name says exactly that.
    * **Why `metadata_changed` is the only signal a mode flip contaminates, and every other
      signal is reported normally regardless.** Enrichment rewrites title and description and
      NOTHING else — the URL set, the extracted page bodies, and sitemap discovery are
      untouched by it (`internals/enrich.py`'s `apply_summaries`). So when
      `current_enrich_applied` disagrees with the previous run's own `enrich_applied`
      (`mode_changed` below), a `metadata_changed` count would be comparing text written by
      two different authors and calling the difference a "change" — every page in the index
      could show up as changed for no reason a reader could act on. Nothing else has that
      problem: `pages_added`/`pages_removed`/`urls_discovered_delta`/`sections_delta`/
      `selection_churn`/`selection_churn_ratio` are all about which URLs the index lists, not
      about what their bullets say, and a genuine content change is now detectable
      independently via `content_changed` (below) because `current_content_hashes` fingerprints
      page BODIES, which enrichment never touches. **Do not suppress the whole diff** — only
      `metadata_changed` degrades to `None`; everything else in this list is computed and
      returned exactly as it always was, mode flip or not, which is what
      `test_a_mode_change_still_reports_a_genuine_content_change` pins.
    * **`content_changed`** — an entry whose `key` is present in both this run's index AND
      the previous run's, where `current_content_hashes` and `previous.content_hashes` both
      have that key and disagree. `None` — never a count — when `previous.content_hashes` is
      `None` (the previous run predates `RUN_STATS_VERSION` 8, or its hashes were otherwise
      unreadable): the same "the previous row didn't record it" rule
      `urls_discovered_delta` already applies to its own predecessor field.
      Mode-independent by construction, since the hashes it joins are keyed off page bodies
      `apply_summaries` never rewrites.
    * **`metadata_not_comparable_reason`** — `None` when `metadata_changed` is a real count;
      otherwise `"enrichment_enabled"` when this run enriched and the previous one did not
      (the direction the ticket's own copy describes), or `"enrichment_disabled"` for the
      mirror case. **An unknown previous mode (`previous.enrich_applied is None`) is treated
      as COMPARABLE, not as "cannot tell"** — see the "unknown mode" paragraph below.
    * **`selection_churn`** = `pages_added + pages_removed`. **`selection_churn_ratio`** is
      that count divided by `len(current_keys | previous_keys)`, rounded to four decimal
      places, and `None` — never `0.0` — when that union is empty (both indexes have no
      entries at all; a ratio over zero pages describes nothing). "Selected pages" here means
      what the index actually LISTS, not the frontier `select_urls` chose upstream of the
      crawl: that intermediate list is never persisted anywhere `execute_run` could read it
      back from, and the index is the honest, available answer to "what does the crawler now
      consider important."
    * **`sections_delta`** — see `_sections_delta`.
    * **`llms_txt_bytes_delta`** — left reported and UNLABELLED even across a mode change.
      Enrichment rewrites every enriched page's title and description, which shifts the
      artifact's byte count regardless of whether any page was "added", "removed", or
      genuinely changed in the sense this module can still measure — this field was already
      a coarse, whole-artifact number before PER-194 and stays exactly that coarse after it;
      adding a reason to it would imply a precision this field has never actually had.
    * **Samples** (`added_sample`/`removed_sample`/`metadata_changed_sample`/
      `content_changed_sample`) — every entry in the corresponding set, sorted by `key`,
      capped at `SAMPLE_LIMIT`, each title truncated at `MAX_SAMPLE_TITLE_CHARS`. The true
      count is always recorded alongside the sample (`pages_added` etc.), so a caller
      displaying "and N more" always has an N. `metadata_changed_sample` is `[]` — never
      `null` — when `metadata_changed` is `None`, and `content_changed_sample` is `[]` when
      `content_changed` is `None`, so a client never has to guard a sample field against a
      `null` its count field does not also carry.

    **Why an unknown previous mode (`previous.enrich_applied is None`) is treated as
    comparable rather than not-comparable.** A pre-version-8 row was written back when
    enrichment was a single deployment-wide flag that defaults OFF — so "assume the same mode
    as this run" is the assumption that keeps `metadata_changed` reporting normally for every
    existing website's first run after this deploy, rather than blanking the signal for every
    site at once on a ticket that, for most of them, changed nothing about how their runs
    enrich. The one-time cost is real but bounded: a deployment that already had the global
    flag ON, for a website that opts in for the first time under this ticket, sees exactly one
    run report every page's metadata as "changed" (because it genuinely was, for that website,
    on that run) rather than as not-comparable — an artifact of the flag having always applied
    deployment-wide before this ticket existed, not a defect in this rule.

    Args:
        current_llms_txt: This run's freshly generated `llms.txt` — the same string just
            written to `runs.llms_txt`, parsed exactly once, here.
        current_urls_discovered: This run's `runs.stats["urls_discovered"]` value, for
            `urls_discovered_delta` below.
        current_enrich_applied: This run's own `enrich_applied` (PER-194) — whether at least
            one page in THIS run's index was enriched. Compared against `previous.
            enrich_applied` to decide `metadata_not_comparable_reason`.
        current_content_hashes: This run's own `build_content_hashes(result.pages)` — see
            that function's own docstring. Joined against `previous.content_hashes` to compute
            `content_changed`.
        previous: The previous completed run's index and metadata, or `None` — see above.
        previous_run_completed: Threaded straight into the returned dict either way (both the
            `"first_run"` and `"compared"` shapes carry it) — `None` when there is no earlier
            run at all, `False` when the immediately preceding run exists but did not
            complete, `True` when it did. Not derived from `previous is None`: a `"compared"`
            result is always built from a genuinely completed previous run
            (`previous_run_completed` is `True` whenever `previous is not None`), but the
            reverse does not hold — `previous is None` can mean either of the first two
            states, which is exactly why this is a separate parameter rather than inferred.

    Returns:
        A JSON-safe `dict` — every value is already a `str`, `int`, `float`, `bool`, `None`,
        a `list` of such dicts, or a `dict[str, int]` — ready to sit under `"index_diff"` in
        the dict `build_run_stats` returns.
    """
    if previous is None:
        return {"state": "first_run", "previous_run_completed": previous_run_completed}

    current_entries = parse_index(current_llms_txt)
    previous_entries = parse_index(previous.llms_txt)

    # Keyed by `key`, last-entry-wins on a collision — `generate_llms_txt` cannot itself
    # produce two entries sharing a normalized key (`select_urls` already dedupes on it
    # upstream), so a collision here would mean two literally different URLs that happen to
    # normalize the same way; last-wins is a defensive, deterministic tiebreak rather than a
    # claim that case is expected.
    current_by_key = {entry.key: entry for entry in current_entries}
    previous_by_key = {entry.key: entry for entry in previous_entries}

    current_keys = current_by_key.keys()
    previous_keys = previous_by_key.keys()
    common_keys = current_keys & previous_keys

    added_keys = sorted(current_keys - previous_keys)
    removed_keys = sorted(previous_keys - current_keys)

    # THE TWO EXPRESSION SITES `mode_changed` IS READ AT, both inside this dict literal —
    # kept that way deliberately (see the module's own risk notes / PR description): a
    # reviewer greps for `mode_changed` and finds every place a mode flip actually changes
    # behaviour, in one function, with nothing hidden behind an extra layer of indirection.
    mode_changed = (
        previous.enrich_applied is not None and previous.enrich_applied != current_enrich_applied
    )
    metadata_not_comparable_reason: MetadataNotComparableReason | None = None
    if mode_changed:
        metadata_not_comparable_reason = (
            "enrichment_enabled" if current_enrich_applied else "enrichment_disabled"
        )

    metadata_changed_keys = sorted(
        key
        for key in common_keys
        if (current_by_key[key].title, current_by_key[key].description)
        != (previous_by_key[key].title, previous_by_key[key].description)
    )

    content_changed_keys = (
        []
        if previous.content_hashes is None
        else sorted(
            key
            for key in common_keys
            if key in previous.content_hashes
            and key in current_content_hashes
            and previous.content_hashes[key] != current_content_hashes[key]
        )
    )

    urls_discovered_delta = (
        None
        if previous.urls_discovered is None
        else current_urls_discovered - previous.urls_discovered
    )

    selection_churn = len(added_keys) + len(removed_keys)
    union_size = len(current_by_key.keys() | previous_by_key.keys())
    selection_churn_ratio = round(selection_churn / union_size, 4) if union_size > 0 else None

    return {
        "state": "compared",
        "previous_run_completed": previous_run_completed,
        "compared_to_run_id": previous.run_id,
        "compared_to_completed_at": previous.completed_at,
        "pages_added": len(added_keys),
        "pages_removed": len(removed_keys),
        "metadata_changed": None if mode_changed else len(metadata_changed_keys),
        "added_sample": _sample([current_by_key[key] for key in added_keys]),
        "removed_sample": _sample([previous_by_key[key] for key in removed_keys]),
        # The CURRENT entry, not the previous one — a changed sample shows what the title
        # reads NOW, which is the more useful half of "this page changed" for a reader who
        # cannot see the previous run's text at all.
        "metadata_changed_sample": (
            [] if mode_changed else _sample([current_by_key[key] for key in metadata_changed_keys])
        ),
        "metadata_not_comparable_reason": metadata_not_comparable_reason,
        "content_changed": (None if previous.content_hashes is None else len(content_changed_keys)),
        "content_changed_sample": _sample([current_by_key[key] for key in content_changed_keys]),
        "urls_discovered_delta": urls_discovered_delta,
        "sections_delta": _sections_delta(current_entries, previous_entries),
        "selection_churn": selection_churn,
        "selection_churn_ratio": selection_churn_ratio,
        "llms_txt_bytes_delta": len(current_llms_txt.encode()) - len(previous.llms_txt.encode()),
    }
