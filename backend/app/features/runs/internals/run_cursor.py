"""Encoding and decoding of the opaque run-history cursor. Pure, no I/O.

A feature-owned pure helper, the same kind of module as the websites feature's
`url_normalize.py` (ARCHITECTURE.md §3.1: "`internals/` may also hold a feature's own
**pure** helpers... logic that is neither a DTO nor a query, with no I/O and no state"). It
is private to the runs feature. When a second feature paginates — PER-156's stats list is the
likely first — §3.1's instruction is to *promote this to a shared location*, not to import it
across the feature boundary and not to copy it. It is not in `core/` already because the
payload below encodes **this** feature's sort key, and a shared codec whose only caller is
one feature is a guess about the second caller rather than a design for it.

**What the cursor identifies.** A position in the `(started_at DESC, id DESC)` ordering — not
an offset from the start of the list. `id` is in the payload and is not optional: runs
inserted by one cron tick share a `started_at` down to the microsecond, and a cursor carrying
only a timestamp cannot distinguish "the rows I already sent" from "the rows I have not sent
yet" within that tie group. It would therefore have to choose between re-sending the whole
group on the next page (duplicates) or skipping it entirely (silent data loss). Both are
invisible in a test whose fixtures happen to have distinct timestamps, which is why
`tests/test_runs_api.py` seeds a deliberate tie.

**Opaque by contract.** The wire format is base64url of a small JSON object:

    {"started_at": "2026-08-04T13:38:01.438329-07:00",
     "id": "d6933910-2d66-4465-9f11-f78bb17ae292"}

Base64 is not encryption and is not pretending to be — the point is not to hide the values
(they are a timestamp and an id the client can already see in the rows it was just sent), it
is to make the format *visibly* not part of the contract, so that changing it later is a
backend detail rather than a breaking API change. JSON rather than a delimited string
because it is the format that can grow: adding a third key later is backward compatible with
this decoder in a way that adding a third delimited field is not.

**Every decode failure is one exception type.** `decode_cursor` wraps the whole parse rather
than enumerating `binascii.Error`, `UnicodeDecodeError`, `JSONDecodeError`, and whatever the
next Python version adds to that list. A cursor arrives from the query string, so *every*
byte of it is attacker-controlled; an unenumerated exception type escaping this function is a
500 on a malformed input, which the ticket calls out specifically. The broad catch is the
safe direction here, and it re-raises as `CursorError` so the one thing callers must handle
is one thing.

**Failures never quote the input.** Every rejection below raises the same fixed message. The
caller cannot act differently on "you corrupted it" than on "we changed the format", and
echoing a decoded fragment of an attacker-supplied value back into an error body is how a
reflection bug gets built by accident.
"""

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final
from uuid import UUID


# Refuse to base64-decode more than this many characters. A legitimate cursor is ~120 bytes
# encoded; this is generous by several multiples and exists only so that a multi-megabyte
# `?cursor=` cannot make the process do real work before being rejected. Checked before
# decoding, not after.
MAX_CURSOR_LENGTH: Final = 512

# The single message every failure below reports. See the module docstring.
_INVALID: Final = "Not a valid pagination cursor"


class CursorError(ValueError):
    """A `?cursor=` value this API did not produce, or can no longer read.

    Raised by `decode_cursor` and turned into a `422` by the router's cursor dependency
    (`app.api.routers.runs`). A `ValueError` subclass because that is what it is — a value
    that failed validation — not because anything relies on catching it as one.
    """


@dataclass(frozen=True)
class RunCursor:
    """A decoded cursor: the exact `(started_at, id)` of the last row the client was sent.

    Frozen because a decoded cursor is a value, and nothing should be able to shift a
    client's pagination position by mutating it in flight. The service takes one of these
    rather than the raw string, so it can be constructed directly in a test without going
    through base64.
    """

    started_at: datetime
    run_id: UUID


def encode_cursor(started_at: datetime, run_id: UUID) -> str:
    """Encode one row's sort key into the opaque cursor that fetches the page after it.

    `started_at.isoformat()` is what makes the round trip exact: it preserves microsecond
    precision and the UTC offset, both of which the keyset comparison depends on. Formatting
    it any other way — `str()`, a `strftime` with second resolution — would silently truncate
    the timestamp and make the cursor point at a slightly different place in the ordering
    than the row it came from, which manifests as duplicated or skipped rows at page
    boundaries rather than as an error.
    """
    payload = json.dumps(
        {"started_at": started_at.isoformat(), "id": str(run_id)},
        # Compact: no reason to spend cursor length on whitespace.
        separators=(",", ":"),
    )
    # `urlsafe_b64encode` so the result needs no escaping in a query string. The `=` padding
    # is stripped for the same reason (it is `%3D`-escaped by some clients and not others,
    # which makes otherwise-identical cursors compare unequal in logs); `decode_cursor` puts
    # it back.
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(raw: str) -> RunCursor:
    """Decode a cursor produced by `encode_cursor`.

    Raises:
        CursorError: if `raw` is not a cursor this module produced — bad base64, bad UTF-8,
            malformed JSON, a non-object payload, a missing or non-string field, an
            unparseable timestamp or UUID, or a timestamp with no UTC offset.
    """
    if not raw or len(raw) > MAX_CURSOR_LENGTH:
        raise CursorError(_INVALID)

    try:
        # Restore the padding stripped by `encode_cursor`. base64 encodes 3 bytes to 4
        # characters, so a valid payload's length is always a multiple of 4 once padded.
        padded = raw + "=" * (-len(raw) % 4)
        # `validate=True` so that stray characters outside the base64url alphabet are an
        # error rather than being silently discarded — without it, a corrupted cursor can
        # decode to a *different* valid cursor instead of failing.
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        payload: Any = json.loads(decoded)
        if not isinstance(payload, dict):
            raise ValueError("cursor payload is not an object")
        started_at_text = payload["started_at"]
        run_id_text = payload["id"]
        if not isinstance(started_at_text, str) or not isinstance(run_id_text, str):
            raise ValueError("cursor fields are not strings")
        started_at = datetime.fromisoformat(started_at_text)
        run_id = UUID(run_id_text)
    except (ValueError, KeyError, TypeError, binascii.Error, UnicodeDecodeError) as error:
        raise CursorError(_INVALID) from error
    except Exception as error:  # noqa: BLE001 — see the module docstring
        # Deliberately broader than the tuple above. Every byte here is attacker-controlled,
        # and an exception type not listed above escaping this function would be a 500 on a
        # malformed query parameter. This is the backstop that makes "422, never 500" true by
        # construction rather than by having enumerated correctly.
        raise CursorError(_INVALID) from error

    if started_at.tzinfo is None:
        # `encode_cursor` only ever sees `timestamptz` values from asyncpg, which are always
        # timezone-aware, so a naive timestamp did not come from us. Rejecting it here also
        # keeps a naive datetime from reaching asyncpg, which would interpret it against the
        # session time zone and quietly shift the client's position in the ordering.
        raise CursorError(_INVALID)

    return RunCursor(started_at=started_at, run_id=run_id)
