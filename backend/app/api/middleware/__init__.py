"""ASGI middleware for the FastAPI application.

One module per concern, wired in `app.main.create_app()`. Middleware is deliberately kept
out of `app/api/routers/` — a router is thin by contract (ARCHITECTURE.md §3.2) and a
cross-cutting wrapper around every request is the opposite of thin.
"""
