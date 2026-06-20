from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "content_factory",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.scheduler.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    # One task at a time per worker — Agent 2 tasks involve long Claude calls
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    # Check all active channels and trigger discovery for those overdue
    "dispatch-discovery": {
        "task": "app.scheduler.tasks.dispatch_discovery",
        "schedule": crontab(minute="0", hour="*/6"),   # every 6 hours
    },
    # Auto-approve or flag NEEDS_REVIEW for expired Telegram validations
    "check-validation-timeouts": {
        "task": "app.scheduler.tasks.check_validation_timeouts",
        "schedule": crontab(minute="*/15"),            # every 15 minutes
    },
    # Trigger multilingual generation for newly APPROVED content
    "pickup-approved-content": {
        "task": "app.scheduler.tasks.pickup_approved_content",
        "schedule": crontab(minute="*/15"),            # every 15 minutes
    },
    # D-1: trigger discovery for channels whose next publish is ~24h away
    "schedule-content-creation": {
        "task": "app.scheduler.tasks.schedule_content_creation",
        "schedule": crontab(minute="0"),               # every hour
    },
    # D-day: log (and eventually publish) content due in the next 30 minutes
    "dispatch-publishing": {
        "task": "app.scheduler.tasks.dispatch_publishing",
        "schedule": crontab(minute="0,30"),            # every 30 minutes
    },
    # Agent 3: generate audio for all validated scripts
    "pickup-scripts-validated": {
        "task": "app.scheduler.tasks.pickup_scripts_validated",
        "schedule": crontab(minute="*/15"),            # every 15 minutes
    },
    # Agent 5: render video for all audio-done content
    "pickup-audio-done": {
        "task": "app.scheduler.tasks.pickup_audio_done",
        "schedule": crontab(minute="*/15"),            # every 15 minutes
    },
}
