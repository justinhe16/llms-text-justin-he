"""Tests for `app.features.crawl.schemas` — specifically the one property that matters more
than any single field's value: `CrawledPage`'s FIELD ORDER.

`CrawledPage` is a plain dataclass with no keyword-only marker on its fields, so a caller
using positional construction silently reorders arguments into the wrong parameters the
moment a field is inserted anywhere but the end — no `TypeError`, just a `CrawledPage` with
its values quietly transposed. `content_bytes`'s own docstring documents that hazard, and
`description`, `markdown`, and `is_empty` (PER-177) repeat the same promise about their own
position. This file is the guard that turns a violation of that promise into a test failure
rather than a silent misassignment the next time someone edits this dataclass.
"""

from dataclasses import fields

from app.features.crawl.schemas import CrawledPage


def test_the_three_extraction_fields_are_appended_after_content_bytes() -> None:
    """`description`, `markdown`, and `is_empty` — in that order — are the LAST three fields
    on `CrawledPage`, appended after `content_bytes` exactly as PER-177's own docstrings
    promise. A field inserted anywhere else would still pass every test that constructs a
    `CrawledPage` with keyword arguments, which is why this checks the dataclass's own field
    order directly instead of relying on any one call site to notice."""
    names = [field.name for field in fields(CrawledPage)]

    assert names[-3:] == ["description", "markdown", "is_empty"]
    assert names.index("content_bytes") == len(names) - 4, (
        "content_bytes must be immediately followed by the three extraction fields, with "
        "nothing inserted between them"
    )
