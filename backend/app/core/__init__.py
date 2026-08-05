"""Cross-cutting concerns: settings, shared helpers, and infrastructure clients.

Nothing here may import from `app.api` or `app.features`. The import direction is
one-way: `api` -> `features` -> `core` (ARCHITECTURE.md §3.1).
"""
