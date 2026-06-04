import logging
import logging.config
from contextlib import asynccontextmanager

# Configure application loggers to appear in the same uvicorn terminal
logging.config.dictConfig({
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "app": {"format": "%(levelname)s:     %(name)s — %(message)s"}
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "app",
            "stream": "ext://sys.stdout",
        }
    },
    "root": {"level": "INFO", "handlers": ["console"]},
    # Silence noisy third-party loggers
    "loggers": {
        "httpx": {"level": "WARNING"},
        "telegram": {"level": "WARNING"},
        "apscheduler": {"level": "WARNING"},
        "celery": {"level": "WARNING"},
    },
})

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.agents.agent1_setup.routers import users_router, channels_router, suggest_router, voices_router
from app.agents.agent2_discovery.routers import discovery_router, telegram_router

logger = logging.getLogger(__name__)


def _bootstrap_dev_user() -> None:
    # TODO: remove once real authentication is implemented
    from app.database import _get_session_factory
    from app.models import User
    from app.services.auth import _DEV_USER_ID
    from app.agents.agent1_setup.services import users as users_service
    from app.schemas.user import UserCreate

    db = _get_session_factory()()
    try:
        if db.get(User, _DEV_USER_ID) is None:
            users_service.create(
                db,
                UserCreate(name="Dev Admin", telegram_chat_id="0", primary_language="en"),
                user_id=_DEV_USER_ID,
            )
            logger.info("Dev user seeded: %s", _DEV_USER_ID)
        else:
            logger.info("Dev user exists: %s", _DEV_USER_ID)
    finally:
        db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _bootstrap_dev_user()

    # Telegram: webhook in production, long-polling in development
    from app.services import telegram_client
    webhook_url = (settings.telegram_webhook_url or "").strip()
    if webhook_url and not webhook_url.startswith("#"):
        await telegram_client.set_webhook(settings.telegram_webhook_url)
        logger.info("Telegram webhook registered: %s", settings.telegram_webhook_url)
    elif settings.telegram_bot_token:
        from app.agents.agent2_discovery.services.validation import handle_telegram_update
        await telegram_client.start_polling(handle_telegram_update)

    yield

    await telegram_client.stop_polling()


app = FastAPI(title="Content Factory", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # Vite dev server
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Agent 1 — Channel Setup
app.include_router(users_router)
app.include_router(channels_router)
app.include_router(suggest_router)
app.include_router(voices_router)

# Agent 2 — Content Discovery
app.include_router(discovery_router)
app.include_router(telegram_router)


@app.get("/api/health")
def health():
    return {"status": "ok"}
