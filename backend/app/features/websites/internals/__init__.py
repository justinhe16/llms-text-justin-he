"""Private to the websites feature: its reader, its writer, and its URL normalizer.

`internals/` means what it says (ARCHITECTURE.md §3.1). Only `app.features.websites.service`
imports from this package. No router imports it, and no other feature imports it — a feature
that needs website data calls `WebsiteService`, which is the rule that keeps the dependency
graph acyclic.
"""
