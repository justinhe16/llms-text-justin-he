"""Private to the schedules feature: its reader, its writer, and its `next_run` helper.

`internals/` means what it says (ARCHITECTURE.md §3.1). Only `app.features.schedules.service`
imports from this package. No router imports it, and no other feature imports it — a feature
that needs schedule data calls `ScheduleService`, which is the rule that keeps the dependency
graph acyclic.
"""
