"""The runs feature: a website's run history, one run's full detail, and — as of PER-160 —
manually triggering a new one.

Three endpoints now (ARCHITECTURE.md §10.3): `GET /websites/{id}/runs` (keyset-paginated
history), `GET /runs/{id}` (one run in full, including `llms_txt`), and
`POST /websites/{id}/runs` (start a manual crawl, owner-only). The first two are unscoped —
any signed-in user may read any website's runs (§4.1) — and remain so; the third is this
feature's first write, and the first thing in this feature `require_owner` is called on.
There is now an `internals/runs_writer.py` beside the reader, which the module docstring on
this feature used to say did not exist — it does now, and only `RunService.trigger_run`
calls it.

Layout, per §3.1 — `schemas.py` (DTOs, and the sole owner of `RunStatusName` /
`RunTriggerName`), `service.py` (business logic, including the keyset pagination mechanics
and, new in PER-160, the transaction and abuse-cap enforcement `trigger_run` needs), and
`internals/` (this feature's reader, its writer, its pure cursor codec, and its pure
rate-limit-message helpers). `internals/` is private: no other feature imports from it.
`app.features.websites.schemas` needing `RunStatusName` is handled by importing THIS
module's `schemas.py`, never `internals/`.
"""
