"""The schedules feature: reading and upserting a website's recurring-run configuration.

Layout, per ARCHITECTURE.md §3.1 — `schemas.py` (DTOs), `service.py` (business logic,
ownership checks, transaction boundaries), and `internals/` (this feature's reader, writer,
and the pure `compute_next_run_at` helper). `internals/` is private: no other feature imports
from it. If another feature needs schedule data, it calls `ScheduleService`.

**This feature has no `user_id`.** `schedules` is owned through its parent `websites` row
(`website_id`, `UNIQUE`), not through a column of its own — see `service.py` for what that
means for the authorization contract.
"""
