"""Private to the runs feature: its reader and its pagination-cursor codec.

`internals/` means what it says (ARCHITECTURE.md §3.1). Only `app.features.runs.service`
imports from this package. No router imports it, and no other feature imports it — a feature
that needs run data calls `RunService`, which is the rule that keeps the dependency graph
acyclic.
"""
