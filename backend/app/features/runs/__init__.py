"""The runs feature: a website's run history, one run's full detail, manually triggering a
new one (PER-160), and — as of PER-181 — downloading a run's two generated artifacts.

Five endpoints now (ARCHITECTURE.md §10.3): `GET /websites/{id}/runs` (keyset-paginated
history), `GET /runs/{id}` (one run in full, including `llms_txt`),
`POST /websites/{id}/runs` (start a manual crawl, owner-only), and
`GET /runs/{id}/llms.txt` / `GET /runs/{id}/llms-full.txt` (PER-181, the two generated
artifacts as plain-text downloads). Four of the five are unscoped — any signed-in user may
read any website's runs, any run's full detail, and any run's artifacts (§4.1) — and remain
so; `POST /websites/{id}/runs` is this feature's only write, and the first thing in this
feature `require_owner` is called on. There is an `internals/runs_writer.py` beside the
reader, which the module docstring on this feature used to say did not exist — it does now,
and only `RunService.trigger_run` calls it.

Layout, per §3.1 — `schemas.py` (DTOs, and the sole owner of `RunStatusName` /
`RunTriggerName`), `service.py` (business logic, including the keyset pagination mechanics
and, new in PER-160, the transaction and abuse-cap enforcement `trigger_run` needs), and
`internals/` (this feature's reader, its writer, its pure cursor codec, its pure
rate-limit-message helpers, and — as of PER-181 — its pure artifact-naming helpers).
`internals/` is private: no other feature imports from it. `app.features.websites.schemas`
needing `RunStatusName` is handled by importing THIS module's `schemas.py`, never
`internals/`.
"""
