"""Diffing one run's `llms.txt` index against the previous completed run's — the "what changed
in the latest run" block the Trends tab reads through `runs.stats["index_diff"]`
(`internals/run_stats.py`, `RUN_STATS_VERSION` 7).

Pure, feature-owned, no I/O — the same category `internals/run_stats.py` and
`internals/llms_txt.py` already occupy, and for the same reason CLAUDE.md #9 states for
those two: everything downstream of a fetched page stays behind a model-free seam, and this
module sits one layer further downstream than either, describing the DIFFERENCE between two
already-generated artifacts rather than generating one.

**Why this is a diff of two STRINGS, not a diff of two page lists.** `runs.stats` cannot
afford to carry a stored `indexed_pages: [{url, title}]` list of its own — `_LIST_COLUMNS`
(`runs/internals/runs_reader.py`) deliberately excludes `llms_txt` from every row of
`GET /websites/{id}/runs`, a list the Runs tab polls every three seconds while a run is
active, and re-adding an equivalent list under another name inside `stats` would undo that
exclusion at up to `crawl_max_pages` entries per row. So this module reads back the ONE
column that already exists cheaply enough to fetch per run — `runs.llms_txt` itself, "kilobytes
for any site a crawler actually meets" (`llms_txt.py`'s `MAX_FULL_TEXT_BYTES` docstring) — and
recovers the page list by parsing it.

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

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Final

from app.features.crawl.internals.url_ranking import normalize_url


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


def build_index_diff(
    *,
    current_llms_txt: str,
    current_urls_discovered: int,
    previous: PreviousIndex | None,
    previous_run_completed: bool | None,
) -> dict[str, Any]:
    """Build the `index_diff` block `RUN_STATS_VERSION` 7 stores in `runs.stats`.

    `previous is None` covers both `previous_run_completed` states that carry no comparison —
    no earlier run at all (`previous_run_completed is None`) and an earlier run that exists
    but never completed with an index of its own (`previous_run_completed is False`, or a
    completed row whose `llms_txt` was `NULL` — `RunService.get_previous_completed_index`
    returns `None` for that case too, deliberately, since a completed run with no index
    cannot be compared against). Either way the result is the `"first_run"` shape, carrying
    no metrics at all — there is nothing to measure a delta against, and a `0` in every field
    would claim a comparison that never happened.

    **Semantics, pinned here because nowhere else states them:**

    * **`pages_changed`** — an entry whose `key` (see `IndexEntry.key`) is present on both
      sides, with a different `title` **or** a different `description`. A same-key entry
      with both fields byte-identical is neither added, removed, nor changed; it simply
      is not counted at all, the same way an untouched row does not appear in a diff.
    * **`selection_churn`** = `pages_added + pages_removed`. **`selection_churn_ratio`** is
      that count divided by `len(current_keys | previous_keys)`, rounded to four decimal
      places, and `None` — never `0.0` — when that union is empty (both indexes have no
      entries at all; a ratio over zero pages describes nothing). "Selected pages" here means
      what the index actually LISTS, not the frontier `select_urls` chose upstream of the
      crawl: that intermediate list is never persisted anywhere `execute_run` could read it
      back from, and the index is the honest, available answer to "what does the crawler now
      consider important."
    * **`sections_delta`** — see `_sections_delta`.
    * **Samples** (`added_sample`/`removed_sample`/`changed_sample`) — every entry in the
      corresponding set, sorted by `key`, capped at `SAMPLE_LIMIT`, each title truncated at
      `MAX_SAMPLE_TITLE_CHARS`. The true count is always recorded alongside the sample
      (`pages_added` etc.), so a caller displaying "and N more" always has an N.

    Args:
        current_llms_txt: This run's freshly generated `llms.txt` — the same string just
            written to `runs.llms_txt`, parsed exactly once, here.
        current_urls_discovered: This run's `runs.stats["urls_discovered"]` value, for
            `urls_discovered_delta` below.
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

    added_keys = sorted(current_keys - previous_keys)
    removed_keys = sorted(previous_keys - current_keys)
    changed_keys = sorted(
        key
        for key in current_keys & previous_keys
        if (current_by_key[key].title, current_by_key[key].description)
        != (previous_by_key[key].title, previous_by_key[key].description)
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
        "pages_changed": len(changed_keys),
        "added_sample": _sample([current_by_key[key] for key in added_keys]),
        "removed_sample": _sample([previous_by_key[key] for key in removed_keys]),
        # The CURRENT entry, not the previous one — a changed sample shows what the title
        # reads NOW, which is the more useful half of "this page changed" for a reader who
        # cannot see the previous run's text at all.
        "changed_sample": _sample([current_by_key[key] for key in changed_keys]),
        "urls_discovered_delta": urls_discovered_delta,
        "sections_delta": _sections_delta(current_entries, previous_entries),
        "selection_churn": selection_churn,
        "selection_churn_ratio": selection_churn_ratio,
        "llms_txt_bytes_delta": len(current_llms_txt.encode()) - len(previous.llms_txt.encode()),
    }
