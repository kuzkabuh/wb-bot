from pydantic import AnyHttpUrl
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # =========================
    # Database
    # =========================
    POSTGRES_USER: str = "wb"
    POSTGRES_PASSWORD: str = "wbpass"
    POSTGRES_DB: str = "wb"
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432

    # =========================
    # Redis
    # =========================
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # =========================
    # Telegram Bot
    # =========================
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_WEBHOOK_SECRET: str
    PUBLIC_BASE_URL: AnyHttpUrl
    WEBHOOK_PATH: str = "/webhook"

    # =========================
    # Security
    # =========================
    MASTER_ENCRYPTION_KEY: str  # формат: base64:...

    # =========================
    # Logging / System
    # =========================
    TIMEZONE: str = "Europe/Moscow"
    LOG_LEVEL: str = "INFO"
    SYNC_INTERVAL_MINUTES: int = 30

    # =========================
    # Admin
    # =========================
    ADMIN_TOKEN: str = "admin"

    # =========================
    # Wildberries API
    # =========================
    WB_API_BASE_URL: AnyHttpUrl = "https://suppliers-api.wildberries.ru"
    WB_BALANCE_PATH: str = "/api/v2/finances/balance"
    BALANCE_CACHE_TTL: int = 60  # сек, кэш баланса


    model_config = SettingsConfigDict(
        env_file=".env",
        case_sensitive=True,
        extra="ignore",  # игнор лишних переменных
    )


settings = Settings()
