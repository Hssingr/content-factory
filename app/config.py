from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://user:password@localhost:5432/content_factory"
    redis_url: str = "redis://localhost:6379"

    anthropic_api_key: str = ""
    # Roadmap 4.2 / audit S-2 (exec-6): both slots are Sonnet-class so no
    # MODEL_ROUTING task defaults to Haiku anymore. (The SECONDARY_MODEL-tier
    # quality gate this note originally referenced, script_quality_check, was
    # itself deleted by the Elimination Mandate — see
    # code_report/forensic_output_audit_borrasca_run.md D1.1.)
    primary_model: str = "claude-sonnet-5"
    secondary_model: str = "claude-sonnet-4-6"
    # "dev" forces the configured secondary model for every Claude call except hard quality/tool exceptions.
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

    fernet_key: str = ""
    secret_key: str = "change-me-in-production"

    remotion_path: str = "./remotion"
    media_path: str = "./media"      # base directory for audio/video files
    # Full path to the node binary to use for Remotion (must be Node ≥18).
    # Set in .env when nvm is used: NODE_BIN=/home/user/.nvm/versions/node/v20.19.6/bin/node
    node_bin: str = "node"

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

    # Agent 4 child Shorts: cap individual visual holds so sparse remaps do not
    # leave one image on screen for long short-form spans.
    short_visual_max_hold_ms: int = 6000

    # Shorts "Part N of M" corner label. Remotion renders subtitles ONLY
    # (audit G-0) — this label is the single allowed, operator-approved
    # exception. The operator requires this series-position label throughout
    # every standalone Short, so it is enabled by default.
    short_part_label_enabled: bool = True

    # Agent 4 parent long-form: last-line hold cap after timestamp mapping. Even
    # if every upstream guard (hint proximity window, anchor span sanity) is
    # defeated, no non-terminal parent beat may hold a single image longer than
    # this. Applied in split_into_beats() by advancing the next beat — never by
    # creating beats or touching audio/subtitle timing.
    parent_visual_max_hold_ms: int = 9000

    # Post-render verification: run ffprobe/blackdetect/silencedetect after every render.
    # Set VERIFY_RENDERS=false to skip (e.g. in CI or when ffmpeg is unavailable).
    verify_renders: bool = True

    # Dev-mode transcription (roadmap 6.7 / audit §8.6): when True, local
    # faster-whisper runs FIRST and the OpenAI Whisper API is the fallback —
    # zeroes out Whisper spend during iteration. Default False: OpenAI Whisper
    # stays primary in production (quality: word timings from the hosted model).
    whisper_local_primary: bool = False

    # Agent 3 TTS post-processing (pre-next-test roadmap Tier 1 R1): any
    # interior narration silence longer than this is capped to this length.
    # A real production run measured 31% of a 12:40 video as near-digital
    # silence (219 gaps ≥0.5s, median 1.1s) — paragraph-per-sentence scripts
    # × ElevenLabs v3 per-utterance pauses, with nothing bounding pause
    # length anywhere. Applied per TTS chunk, BEFORE concatenation and
    # section-boundary computation, so Whisper, beats, captions, and
    # section_boundaries all derive from the compressed audio and can never
    # desync. Set to 0 (or negative) to disable compression entirely.
    audio_max_interior_silence_ms: int = 650

    # Agent 4 image model routing (Phase 14.6 foundation; Tier 5 R16 enabled by
    # operator decision). Qualifying cover/high-intensity/person/reveal beats
    # use Flux Dev; ordinary beats remain on Schnell. Environment variables can
    # still disable either switch for a cost-constrained deployment.
    image_routing_enabled: bool = True
    image_routing_allow_dev: bool = True
    image_routing_allow_pro: bool = False
    # Hard cap on Pro-family images per content item. Default 0 means Pro is
    # never selected even if image_routing_allow_pro is true, until an
    # operator explicitly raises this.
    image_routing_max_pro_per_content: int = 0


settings = Settings()
