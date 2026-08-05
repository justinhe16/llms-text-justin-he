"""Authentication primitives: the JWKS cache and the request dependencies built on it.

Cross-cutting, like `app.core.settings`, and subject to the same rule: nothing here
imports from `app.api`, `app.features`, or `app.infrastructure` (ARCHITECTURE.md §3.1).
`app.api.deps` imports *from* this package, not the other way round.
"""
