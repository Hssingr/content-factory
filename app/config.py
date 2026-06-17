from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://user:password@localhost:5432/content_factory"
    redis_url: str = "redis://localhost:6379"

    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-6"  # legacy fallback; prefer task routing via MODEL_ROUTING
    # "dev" forces Haiku for every Claude call regardless of task routing.
    # "prod" uses MODEL_ROUTING (the default). Set CLAUDE_TIER=dev in .env during development.
    claude_tier: str = "prod"
    elevenlabs_api_key: str = ""
    cartesia_api_key: str = ""
    openai_api_key: str = ""

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_webhook_url: str = ""     # set in production; leave empty to use polling in dev
    telegram_webhook_secret: str = "" # random string for X-Telegram-Bot-Api-Secret-Token verification

    fal_key: str = ""
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

    # Controls whether storyboard beats with visual_type="generated_visual" are kept as
    # AI-generation placeholders (__generated_pending__) or silently replaced with a dark
    # text-overlay fallback. Default false — AI video generation is not implemented yet.
    generate_required_frame: bool = False

    # Download all remote media URLs to local disk before calling Remotion.
    # Eliminates "Page crashed!" caused by Chromium loading many remote video streams.
    # Set LOCALIZE_MEDIA_BEFORE_RENDER=false to skip (e.g. fast local test runs).
    localize_media_before_render: bool = True

    # Remotion --concurrency value for normal renders.
    # High-video-count renders (>40 video sections) are capped at render_concurrency // 2 (min 1).
    # Safe-retry renders always use concurrency=1.
    render_concurrency: int = 4

    # Chunked rendering: split long MainVideo renders into segments and concatenate with ffmpeg.
    # Reduces Chromium memory pressure for videos longer than chunk_duration_sec.
    # Set CHUNKED_RENDER_ENABLED=false to disable (e.g. short test videos or when ffmpeg unavailable).
    chunked_render_enabled: bool = True
    chunk_duration_sec: int = 90

    # Parallel chunk rendering: number of chunks rendered concurrently (ThreadPoolExecutor).
    # Default 1 (sequential) — safe for any VPS. Set CHUNK_PARALLEL_WORKERS=2 only after
    # measuring single-chunk peak RSS on the VPS (/usr/bin/time -v npx remotion render ...).
    # Rule: enable parallelism only when 2× single-chunk peak RSS < 80% of total RAM.
    chunk_parallel_workers: int = 1

    # Remotion pre-bundling: bundle the Remotion project once per code change instead of
    # re-bundling on every render call. Hash of src/ tree + package-lock determines reuse.
    # Set REMOTION_PRE_BUNDLE=true to enable; old bundles auto-pruned (keep last 2).
    remotion_pre_bundle: bool = False

    # Post-render verification: run ffprobe/blackdetect/silencedetect after every render.
    # Set VERIFY_RENDERS=false to skip (e.g. in CI or when ffmpeg is unavailable).
    verify_renders: bool = True


settings = Settings()
