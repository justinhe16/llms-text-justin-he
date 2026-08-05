"""The websites feature: registering a site, browsing every site, and deleting your own.

The first feature module in this codebase, and therefore the reference implementation for
every one after it (ARCHITECTURE.md §3.1). Read `service.py` before writing the second one.

Layout, per §3.1 — `schemas.py` (DTOs), `service.py` (business logic, ownership checks,
transaction boundaries), and `internals/` (this feature's reader and writer, plus the URL
normalizer). `internals/` is private: no other feature imports from it. If another feature
needs website data, it calls `WebsiteService`.
"""
