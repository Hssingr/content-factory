from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://user:password@localhost:5432/content_factory"
    redis_url: str = "redis://localhost:6379"

    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"
    elevenlabs_api_key: str = ""
    openai_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_url: str = ""     # set in production; leave empty to use polling in dev
    telegram_webhook_secret: str = "" # random string for X-Telegram-Bot-Api-Secret-Token verification

    pexels_api_key: str = ""
    unsplash_api_key: str = ""
    runway_api_key: str = ""

    brightdata_username: str = ""
    brightdata_password: str = ""

    fernet_key: str = ""
    secret_key: str = "change-me-in-production"

    # Agent 3 — minutes to wait for user FIX reply before auto-proceeding on MINOR issues
    agent3_minor_timeout_minutes: int = 5

    remotion_path: str = "./remotion"
    media_path: str = "./media"      # base directory for audio/video files
    # Full path to the node binary to use for Remotion (must be Node ≥18).
    # Set in .env when nvm is used: NODE_BIN=/home/user/.nvm/versions/node/v20.19.6/bin/node
    node_bin: str = "node"


settings = Settings()
