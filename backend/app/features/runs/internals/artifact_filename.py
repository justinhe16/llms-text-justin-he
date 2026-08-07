"""Pure helpers for naming a run's downloadable artifacts.

Lives in `runs/internals/` for the reason `run_limits.py` and `websites/internals/
url_normalize.py` do (ARCHITECTURE.md §3.1): feature-owned logic with no I/O and no state,
imported by one service and by one reader, and cheap to pin exhaustively in a test that needs
no database.

## This is a SECOND implementation of a function that already exists in TypeScript

`llmsTxtFilename` in `frontend/lib/crawls/run-display.ts` has named the Output tab's
client-side download since PER-16x, and `artifact_filename` below must agree with it
character for character: the same website downloaded from the Download button and from
`GET /runs/{id}/llms.txt` has to land in the user's Downloads folder under the same name, or
they get two files they cannot tell apart.

Two implementations exist because there is no seam between them — the TypeScript one names a
`Blob` the browser already holds and never makes a request, so neither can call the other.
What protects them from drifting is `tests/test_artifact_filename.py`'s fixture table, which
is a transcription of inputs and expected outputs run against BOTH implementations by hand
when this landed, and which every future change to either function must be re-checked
against. `run-display.ts` carries the reciprocal pointer. There is no vitest/jest in this
repo (see `frontend/lib/api/schema.type-test.ts`), so a shared executable fixture is not
available; a shared, documented table plus two pointers is.

## Why the regexes are ASCII-explicit where the TypeScript ones use a flag

The TypeScript writes `/[^a-z0-9]+/gi`. The obvious Python transcription — `re.compile(
r"[^a-z0-9]+", re.IGNORECASE)` — is NOT equivalent: Python's `re.IGNORECASE` applies full
Unicode case folding to `str` patterns, so `[a-z]` matches U+212A KELVIN SIGN and U+017F LATIN
SMALL LETTER LONG S, while JavaScript's non-`u` canonicalization explicitly refuses to fold a
non-ASCII character onto an ASCII one. The classes below are therefore written out as
`[A-Za-z0-9]` with NO ignore-case flag, which is exactly what the JavaScript means.
"""

import re
from typing import Final, Literal


# Which of a run's two artifacts is being named. Internal vocabulary, not a wire value — it
# never appears in a request, a response, or `openapi.json` — which is why it lives here
# rather than in `schemas.py` beside `RunStatusName` and `StatsWindowName`. The two names
# match CLAUDE.md #9's own wording for the pair: the llms.txt INDEX, and the llms-full.txt
# EXPANSION.
ArtifactKind = Literal["index", "full"]

# The ONE table that knows which kind is which artifact. Both the download filename and the
# "this run has no such artifact" 404 detail are derived from it, so there is no second place
# for `llms-full` to be spelled and get out of step.
_FILENAME_STEMS: Final[dict[ArtifactKind, str]] = {"index": "llms", "full": "llms-full"}

# `origin.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "")`. Anchored and unbounded-repeat-free, so it
# strips at most one leading scheme. `+`, `.` and `-` are in the class because `svn+ssh://`
# is a real scheme shape; `-` is escaped so it is a literal rather than a range.
_SCHEME_PREFIX: Final = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

# `.replace(/[^a-z0-9]+/gi, "-")`. THE `+` IS LOAD-BEARING: it collapses a RUN of unusable
# characters to a single hyphen, so `xn--r8jz45g.com` becomes `xn-r8jz45g-com` and not
# `xn---r8jz45g-com`. Dropping it is the single most likely way to reimplement this wrong.
_NON_ALPHANUMERIC: Final = re.compile(r"[^A-Za-z0-9]+")

# `.replace(/^-+|-+$/g, "")` — trim the hyphens the substitution above put at either end.
_EDGE_HYPHENS: Final = re.compile(r"^-+|-+$")

# What an origin that sanitizes away to nothing is called. `https://` alone, or an origin
# made entirely of non-ASCII, would otherwise produce `llms-.txt`.
_FALLBACK_SLUG: Final = "site"


def artifact_name(kind: ArtifactKind) -> str:
    """`"llms.txt"` or `"llms-full.txt"` — the artifact's own name, with no origin in it.

    What `RunService`'s "this run has no such artifact" `404` detail is built from, so that
    the message and the filename cannot name two different artifacts.
    """
    return f"{_FILENAME_STEMS[kind]}.txt"


def artifact_filename(origin: str, kind: ArtifactKind) -> str:
    """The filename `Content-Disposition` offers for one run's artifact.

    `("https://example.com:8443", "index")` -> `"llms-example-com-8443.txt"`.

    Byte-for-byte identical to `llmsTxtFilename` (frontend/lib/crawls/run-display.ts) for the
    `index` kind, and the same transformation with a `llms-full` stem for `full`. See the
    module docstring before changing anything here.

    Args:
        origin: `websites.origin` — a normalized scheme + host, e.g. `https://example.com`.
            NOT NULL in the schema, so this is never handed `None`. Any string is accepted
            though: an origin that sanitizes away to nothing becomes `_FALLBACK_SLUG`.
        kind: Which artifact is being named.

    Returns:
        A filename containing only `[a-z0-9-]` and one `.txt`. That guarantee is what lets
        the caller interpolate it into a quoted `Content-Disposition` without escaping.
    """
    without_scheme = _SCHEME_PREFIX.sub("", origin, count=1)
    slug = _EDGE_HYPHENS.sub("", _NON_ALPHANUMERIC.sub("-", without_scheme)).lower()
    return f"{_FILENAME_STEMS[kind]}-{slug or _FALLBACK_SLUG}.txt"
