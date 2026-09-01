"""Application configuration.

Reads settings from environment / `.env` via pydantic-settings. The values here
are consumed across the app (DB URL, webhook secrets, reservation TTL, and the
embedding/search thresholds used by Phase 2+).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """Runtime settings for the ferretería MVP."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Database. The password is deliberately not defaulted to a real value:
    # it comes from .env / POSTGRES_PASSWORD so no credential is committed.
    postgres_user: str = "ferreteria"
    postgres_password: str = ""
    postgres_db: str = "ferreteria"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    database_url: str | None = None

    # Webhook security
    webhook_secret: str = "change-me"

    # Reservation soft-lock TTL (minutes)
    reservation_ttl_minutes: int = 30

    # WhatsApp Cloud API
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_verify_token: str = ""

    # Telegram demo channel
    telegram_bot_token: str = ""
    # Secret token Telegram echoes in the X-Telegram-Bot-Api-Secret-Token header
    # (set at setWebhook time). Empty = demo mode (accept any webhook).
    telegram_secret_token: str = ""

    # OpenAI
    openai_api_key: str = ""
    openai_embedding_model: str = "text-embedding-3-small"
    openai_embedding_dims: int = 1536

    # Google Sheets (append-only order registration)
    google_sheets_credentials_file: str = ""
    google_sheets_spreadsheet_key: str = ""

    # Owner sender allowlist. The owner is the ONLY chat actor: every inbound
    # message is gated against these keys before routing (Telegram senders are
    # chat ids, WhatsApp senders are phone numbers). When both are empty the
    # gate is open and the legacy customer intake keeps working (rollback path).
    owner_telegram_chat_id: str = ""
    owner_whatsapp_phone: str = ""

    # DEPRECATED: legacy owner notification target (quotes/cancellations pushed
    # over Telegram). Kept parseable so old .env files still load; the owner
    # push was removed — replies now travel in the owner's chat.
    owner_phone: str = ""

    # Hybrid catalog search thresholds (confidence in [0, 1]).
    # A single candidate at/above the auto-map threshold maps without prompting;
    # candidates at/above the ambiguity floor populate the disambiguation menu.
    search_auto_map_threshold: float = 0.85
    search_ambiguity_floor: float = 0.65

    # Feature flags (per-Fase stop points)
    fase1_enabled: bool = True
    fase2_enabled: bool = True
    fase3_enabled: bool = True
    fase4_enabled: bool = True

    @property
    def sqlalchemy_database_url(self) -> str:
        """Effective SQLAlchemy database URL (explicit override wins)."""
        if self.database_url:
            return self.database_url
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sqlalchemy_test_database_url(self) -> str:
        """Effective SQLAlchemy URL for the disposable pytest database.

        Derived from the main database URL by appending ``_test`` to the
        database name, so explicit ``DATABASE_URL`` overrides keep working
        (e.g. CI sets ``.../ferreteria`` and tests derive ``.../ferreteria_test``).
        """
        url = make_url(self.sqlalchemy_database_url)
        # render_as_string(hide_password=False) is required: SQLAlchemy 2.0
        # masks passwords as "***" in str(URL), which would break every
        # connection derived from this URL.
        return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (avoids re-parsing env on each call)."""
    return Settings()
