"""The runs feature: a website's run history and one run's full detail.

Two read-only endpoints (ARCHITECTURE.md §10.3): `GET /websites/{id}/runs` (keyset-paginated
history) and `GET /runs/{id}` (one run in full, including `llms_txt`). Both are unscoped —
any signed-in user may read any website's runs (§4.1) — and neither has a write path in this
ticket, which is why there is no `internals/runs_writer.py` next to the reader.

Layout, per §3.1 — `schemas.py` (DTOs, and the sole owner of `RunStatusName` /
`RunTriggerName`), `service.py` (business logic, including the keyset pagination mechanics),
and `internals/` (this feature's reader and its pure cursor codec). `internals/` is private:
no other feature imports from it. `app.features.websites.schemas` needing `RunStatusName` is
handled by importing THIS module's `schemas.py`, never `internals/`.
"""
