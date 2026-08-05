"""Shared FastAPI dependencies.

`Annotated` aliases live here so that route handlers read as `settings: SettingsDep`
instead of repeating `Depends(...)` wiring at every call site. The database session and
the authenticated-user dependency join this file in their own tickets.
"""

from typing import Annotated

from fastapi import Depends

from app.core.settings import Settings, settings


def get_settings() -> Settings:
    """Return the process-wide settings.

    Reads are routed through a dependency rather than importing the module-level
    singleton directly so that a test can substitute a different configuration with
    `app.dependency_overrides[get_settings]`.
    """
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
