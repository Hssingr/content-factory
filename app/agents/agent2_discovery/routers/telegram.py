import logging

from fastapi import APIRouter, HTTPException, Request
from telegram import Bot, Update

from app.config import settings
from app.agents.agent2_discovery.services.validation import handle_telegram_update

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram-webhook"])

# Lazy Bot instance for JSON parsing (no async send needed here)
_parse_bot: Bot | None = None


def _get_parse_bot() -> Bot | None:
    global _parse_bot
    if _parse_bot is None and settings.telegram_bot_token:
        _parse_bot = Bot(token=settings.telegram_bot_token)
    return _parse_bot


@router.post("/webhook", status_code=200)
async def telegram_webhook(request: Request):
    """Receive Telegram update events via webhook (production mode).

    Telegram calls this endpoint for every incoming message/reply.
    In development, prefer polling (set in main.py lifespan when
    TELEGRAM_WEBHOOK_URL is empty).

    Telegram webhook verification: if TELEGRAM_WEBHOOK_SECRET is set in .env,
    the X-Telegram-Bot-Api-Secret-Token header must match.
    """
    # Optional secret-token verification
    if settings.telegram_webhook_secret:
        incoming = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if incoming != settings.telegram_webhook_secret:
            logger.warning("Webhook received with invalid secret token")
            raise HTTPException(status_code=403, detail="Unauthorized")

    body = await request.json()

    bot = _get_parse_bot()
    if bot is None:
        logger.error("TELEGRAM_BOT_TOKEN not set — cannot parse update")
        return {"ok": False}

    update = Update.de_json(body, bot)
    await handle_telegram_update(update, None)   # context not used in our handler

    return {"ok": True}
