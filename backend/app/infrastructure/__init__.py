"""Infrastructure clients: the Postgres pool and the Redis/ARQ connection.

`db/` holds the asyncpg pool factory, the base repository classes, and the transaction
helper (ARCHITECTURE.md §3.5). The Redis/ARQ client lands with the worker ticket.
"""
