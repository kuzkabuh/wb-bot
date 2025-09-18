from celery import Celery
from app.core.config import settings
celery = Celery("wb_bot", broker=settings.CELERY_BROKER_URL, backend=settings.CELERY_RESULT_BACKEND)
celery.conf.timezone = settings.TIMEZONE
@celery.task
def ping():
    return "pong"
