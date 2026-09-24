"""Application configuration loaded from environment."""
from __future__ import annotations

import os
from functools import lru_cache


class Settings:
    """Central settings.

    Reads from environment with sensible local-dev defaults so the app runs
    out of the box with SQLite and mock providers.  Production overrides come
    from Cloud Run environment variables / Secret Manager.
    """

    # Database
    database_url: str
    # Security
    secret_key: str
    # Twilio click-to-call
    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str
    app_base_url: str
    # Google OAuth
    google_client_id: str
    google_client_secret: str
    # Skip tracing
    skiptrace_provider: str
    skiptrace_api_key: str
    # Comps
    comps_provider: str
    # LLM (scraper parsing/analysis — OpenAI-compatible)
    llm_api_key: str
    llm_base_url: str
    # RentCast (property data API — scraper)
    rentcast_api_key: str

    def __init__(self) -> None:
        self.database_url = os.getenv("DATABASE_URL", "sqlite:///./dre_capital.db")
        self.secret_key = os.getenv("SECRET_KEY", "dev-secret-change-me-0123456789abcdef")
        self.twilio_account_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
        self.twilio_auth_token = os.getenv("TWILIO_AUTH_TOKEN", "")
        self.twilio_from_number = os.getenv("TWILIO_FROM_NUMBER", "")
        self.app_base_url = os.getenv("APP_BASE_URL", "http://localhost:8000")
        self.google_client_id = os.getenv("GOOGLE_CLIENT_ID", "")
        self.google_client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
        self.skiptrace_provider = os.getenv("SKIPTRACE_PROVIDER", "mock")
        self.skiptrace_api_key = os.getenv("SKIPTRACE_API_KEY", "")
        self.comps_provider = os.getenv("COMPS_PROVIDER", "mock")
        self.llm_api_key = os.getenv("LLM_API_KEY", "")
        self.llm_base_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")
        self.rentcast_api_key = os.getenv("RENTCAST_API_KEY", "")

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def twilio_configured(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @property
    def google_auth_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()
