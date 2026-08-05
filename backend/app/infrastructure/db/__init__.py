"""Postgres access: pool factory, base repository classes, and the transaction helper.

`pool.py` creates and owns the process-wide asyncpg pool. `base_repository.py` provides
`Reader`/`Writer` base classes that every feature's `internals/{feature}_reader.py` and
`internals/{feature}_writer.py` build on. `transaction.py` provides the `transaction()`
context manager that services use to open a unit of work (ARCHITECTURE.md §5).
"""
