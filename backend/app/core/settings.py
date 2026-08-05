"""Application settings.

Configuration is read from the environment, with `.env` support for local development,
and validated **at module import**. That timing is deliberate: importing this module is
the first thing both processes that run from this image do, so a misconfigured container
refuses to boot instead of serving a 500 on the first request that happens to need a
missing value. Validating in the API's app factory instead would leave the ARQ worker
unchecked.

Nothing in this module may print or log a configuration *value* — see
[ARCHITECTURE.md §9.4](../../../ARCHITECTURE.md#94-never-echo-or-log-a-secret). The
validation error below names missing variables and nothing else.
"""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings for every process that runs from the backend image.

    Both the FastAPI API and the ARQ worker read this one class. Later tickets append
    fields here rather than introducing a second settings object, so that a single file
    describes everything the service needs to boot.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Constrained to the values the code actually branches on, so that a typo
    # (ENVIRONMENT=prod) fails at construction rather than silently missing every
    # `environment ==` comparison downstream.
    environment: Literal["development", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Required in every environment, but declared with an empty default so that
    # construction always succeeds and validate_required_secrets() can report *all*
    # missing variables at once. Declaring them as required Pydantic fields would abort
    # on the first one, costing an operator one restart per missing variable.
    database_url: str = ""
    redis_url: str = ""
    supabase_url: str = ""
    supabase_secret_key: str = ""

    # Optional: error reporting is disabled when this is unset.
    sentry_dsn: str | None = None

    def validate_required_secrets(self) -> None:
        """Fail loudly if any required variable is unset, naming every one of them.

        Raises:
            RuntimeError: names each missing environment variable. Values are never
                included in the message — the repository, its CI logs, and its issue
                tracker are all public.
        """
        # Ordered to match backend/.env.example, so the message reads as a checklist
        # against the file an operator is about to edit.
        required: tuple[tuple[str, str], ...] = (
            ("DATABASE_URL", self.database_url),
            ("REDIS_URL", self.redis_url),
            ("SUPABASE_URL", self.supabase_url),
            ("SUPABASE_SECRET_KEY", self.supabase_secret_key),
        )
        missing = [name for name, value in required if not value.strip()]
        if missing:
            raise RuntimeError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in backend/.env for local development, or with `fly secrets "
                "set` for a deployed environment. See backend/.env.example."
            )


settings = Settings()
settings.validate_required_secrets()
