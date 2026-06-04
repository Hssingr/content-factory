import logging
from typing import Awaitable, Callable

import httpx
from telegram import Bot, Message, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from app.config import settings

logger = logging.getLogger(__name__)

_bot: Bot | None = None
_app: Application | None = None

# Type alias for message handlers injected by callers
MessageHandlerFunc = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def _get_bot() -> Bot:
    global _bot
    if _bot is None:
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
        _bot = Bot(token=settings.telegram_bot_token)
    return _bot


async def send_message(
    chat_id: str | int,
    text: str,
    parse_mode: str = ParseMode.MARKDOWN,
) -> str | None:
    """Send a text message. Returns the Telegram message_id as a string, or None on failure."""
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — send_message skipped")
        return None
    try:
        msg: Message = await _get_bot().send_message(
            chat_id=chat_id, text=text, parse_mode=parse_mode
        )
        return str(msg.message_id)
    except Exception as exc:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)
        return None


async def send_photo(
    chat_id: str | int,
    photo_url: str,
    caption: str = "",
    parse_mode: str = ParseMode.MARKDOWN,
) -> str | None:
    """Send a photo by URL with an optional caption. Returns message_id or None."""
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — send_photo skipped")
        return None
    try:
        msg: Message = await _get_bot().send_photo(
            chat_id=chat_id, photo=photo_url, caption=caption, parse_mode=parse_mode
        )
        return str(msg.message_id)
    except Exception as exc:
        logger.error("Failed to send Telegram photo to %s: %s", chat_id, exc)
        return None


async def set_webhook(url: str) -> bool:
    """Register a webhook URL with Telegram. Call once on production startup."""
    if not settings.telegram_bot_token:
        return False
    try:
        result = await _get_bot().set_webhook(url=url)
        logger.info("Telegram webhook set → %s (success=%s)", url, result)
        return bool(result)
    except Exception as exc:
        logger.error("Failed to set Telegram webhook: %s", exc)
        return False


async def delete_webhook() -> bool:
    """Remove the current webhook (used when switching to polling mode)."""
    if not settings.telegram_bot_token:
        return False
    try:
        result = await _get_bot().delete_webhook()
        logger.info("Telegram webhook deleted")
        return bool(result)
    except Exception as exc:
        logger.error("Failed to delete Telegram webhook: %s", exc)
        return False


async def start_polling(on_message: MessageHandlerFunc) -> Application | None:
    """Start long-polling in the background (dev mode).

    Registers `on_message` as the handler for all non-command text messages.
    Returns the Application instance so the caller can stop it cleanly.

    Args:
        on_message: async callable (update, context) → None

    Returns:
        Running Application, or None if TELEGRAM_BOT_TOKEN is not set.
    """
    global _app
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — Telegram polling disabled")
        return None

    # Remove any existing webhook so polling can receive updates
    await delete_webhook()

    _app = ApplicationBuilder().token(settings.telegram_bot_token).build()
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    await _app.initialize()
    await _app.start()
    await _app.updater.start_polling()

    logger.info("Telegram polling started")
    return _app


def send_message_sync(
    chat_id: str | int,
    text: str,
    parse_mode: str = ParseMode.MARKDOWN,
) -> str | None:
    """Synchronous send via direct HTTP — safe for Celery tasks (no event loop needed).

    Returns the Telegram message_id as a string, or None on failure.
    """
    if not settings.telegram_bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN not set — send_message_sync skipped")
        return None

    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"

    # Try with requested parse_mode first; fall back to plain text on 400
    # (Markdown fails when the message contains unescaped special characters)
    for mode in (parse_mode, None):
        payload: dict = {"chat_id": str(chat_id), "text": text}
        if mode:
            payload["parse_mode"] = mode
        try:
            resp = httpx.post(url, json=payload, timeout=10)
            if resp.status_code == 400 and mode is not None:
                logger.warning("Telegram 400 with parse_mode=%s — retrying as plain text", mode)
                continue
            resp.raise_for_status()
            return str(resp.json()["result"]["message_id"])
        except httpx.HTTPStatusError as exc:
            logger.error("Sync Telegram send failed: %s", exc)
            return None
        except Exception as exc:
            logger.error("Sync Telegram send failed: %s", exc)
            return None
    return None


async def stop_polling() -> None:
    """Stop polling and shut down the Application cleanly."""
    global _app
    if _app is None:
        return
    try:
        await _app.updater.stop()
        await _app.stop()
        await _app.shutdown()
        logger.info("Telegram polling stopped")
    except Exception as exc:
        logger.error("Error stopping Telegram polling: %s", exc)
    finally:
        _app = None
