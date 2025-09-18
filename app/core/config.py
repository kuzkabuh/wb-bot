from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl

class Settings(BaseSettings):
    POSTGRES_USER: str = "wb"
    POSTGRES_PASSWORD: str = "wbpass"
    POSTGRES_DB: str = "wb"
    POSTGRES_HOST: str = "db"
    POSTGRES_PORT: int = 5432

    REDIS_URL: str = "redis://redis:6379/0"

    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_WEBHOOK_SECRET: str
    PUBLIC_BASE_URL: AnyHttpUrl
    WEBHOOK_PATH: str

    MASTER_ENCRYPTION_KEY: str = "base64:"

    TIMEZONE: str = "Europe/Moscow"
    LOG_LEVEL: str = "INFO"

    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"
    SYNC_INTERVAL_MINUTES: int = 30

    ADMIN_TOKEN: str = "admin"

    class Config:
        env_file = ".env"
        case_sensitive = True

settings = Settings()
