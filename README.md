# Content Factory

Automated multilingual video creation and publishing system for YouTube, TikTok, Instagram, and Facebook. Runs 24/7 without daily human intervention. User interacts only via Telegram to validate content.

---

## Tech Stack

| Component | Technology |
|---|---|
| Language | Python 3.11+ |
| API Framework | FastAPI |
| Queue / Workers | Celery + Redis |
| Scheduler | Celery Beat |
| Database | PostgreSQL |
| ORM | SQLAlchemy 2.0 |
| AI / Agents | Claude API — `claude-sonnet-4-6` (web_search tool for discovery) |
| Voice | ElevenLabs API (shared-voices endpoint) |
| Subtitles | OpenAI Whisper API (word-level timestamps) |
| Video | Remotion (Node.js, called from Python via subprocess) |
| Video AI | Runway API (max 5s, last resort only) |
| Stock images | Pexels API + Unsplash API |
| Thumbnails | Pillow + DALL-E fallback |
| Notifications | Telegram Bot API |
| Publishing | YouTube Data API v3, Meta Graph API, TikTok API |
| Proxies | Brightdata residential (1 profile per language/region) |
| Encryption | Python Fernet (AES-256) for credentials |
| Server | Hetzner VPS Ubuntu 22.04 |

---

## Project Structure

```
content-factory/
├── CLAUDE.md
├── README.md
├── requirements.txt
├── .env.example
├── .gitignore
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 0001_initial_schema.py
│       ├── 0002_video_metadata_and_schema_fixes.py
│       ├── 0003_add_channel_description.py
│       ├── 0004_add_voice_use_case.py
│       ├── 0005_add_user_pipeline_schedule.py
│       └── 0006_add_audio_and_quality_config.py
└── app/
    ├── main.py                    # FastAPI entry point + Telegram polling/webhook + dev bootstrap
    ├── config.py                  # pydantic-settings from .env
    ├── database.py                # SQLAlchemy Base, lazy engine, get_db()
    ├── models/                    # ORM models — 1 file per table (20 tables)
    ├── schemas/                   # Pydantic schemas (user, channel, suggest, content)
    ├── services/                  # Shared services
    │   ├── auth.py                # get_current_user_id() — stubbed (TODO: real JWT)
    │   ├── claude_client.py       # call_claude() + call_claude_with_tools() base
    │   ├── crypto.py              # Fernet AES-256 encrypt/decrypt
    │   ├── platform_verifier.py   # Credential verification stubs
    │   └── telegram_client.py     # Async Telegram Bot (webhook + polling)
    ├── agents/
    │   ├── agent1_setup/          # Channel Setup agent
    │   │   ├── system_prompt.py   # suggest_field(), suggest_publish_timing()
    │   │   ├── routers/           # users, channels, suggest, voices
    │   │   └── services/          # users, channels, elevenlabs
    │   ├── agent2_discovery/      # Content Discovery agent
    │   │   ├── system_prompt.py   # generate_scripts(), generate_telegram_summary(),
    │   │   │                      # generate_native_script(), generate_revised_scripts()
    │   │   ├── routers/           # discovery, telegram webhook
    │   │   └── services/          # discovery, fetcher (Claude web_search), story,
    │   │                          # scripts (multilingual), validation (Telegram loop)
    │   └── agent5_video/          # Video Generation agent
    │       ├── system_prompt.py   # Storyboard generation, candidate scoring, section validation
    │       │                      # PROMPT_VERSION 2.1 / STORYBOARD_SCHEMA_VERSION 2.4
    │       ├── subagents/
    │       │   ├── storyboard.py         # Visual beats from Whisper timestamps + script
    │       │   ├── section_splitter.py   # Fallback: equal-interval section split
    │       │   ├── section_validator.py  # Claude validation loop (max 3 rounds, best-attempt fallback)
    │       │   ├── assembly_validator.py # Macro checks: env repetition, duplicate URLs, duration drift
    │       │   └── shorts_cutter.py      # Groups beats into Shorts segments with part labels
    │       └── services/
    │           ├── video.py              # Orchestrator: steps 1-9 per language
    │           ├── stock_fetcher.py      # Scored candidate loop (Pexels/Unsplash/Pixabay + Claude scoring)
    │           ├── asset_manager.py      # Thread-safe immediate download cache (SHA-256 filename)
    │           ├── media_localizer.py    # Safety audit — FAIL FAST on any remote URL in props
    │           ├── remotion_builder.py   # JSON props builder — raises ValueError on http URL
    │           ├── renderer.py           # Remotion CLI + chunked render (90s chunks, ffmpeg concat)
    │           └── subtitles.py          # Standard captions + karaoke chunks from Whisper
    ├── scheduler/
    │   ├── __init__.py            # Celery app + 8 beat tasks
    │   └── tasks.py               # 10 Celery tasks (6 periodic + 4 on-demand)
    ├── publishers/                # (upcoming — Agent 7)
    └── ui/                        # React SPA (Vite 4 + React 18)
        ├── package.json
        ├── vite.config.js         # proxy /api → localhost:8000
        └── src/
            ├── App.jsx            # Channel list view + Setup view (2 tabs)
            ├── constants.js       # Languages, tones, platforms, use_cases…
            ├── api/agent1.js      # Fetch client for all Agent 1 + Agent 2 routes
            └── components/
                ├── ChannelList.jsx          # List with View / Edit / Delete
                ├── Tab1Config.jsx           # 6-section channel setup form
                ├── Tab2Credentials.jsx      # Platform credential grid
                ├── tab1/                    # Section components
                │   ├── BasicInfoSection.jsx
                │   ├── LanguagesSection.jsx
                │   ├── VoicesSection.jsx    # ElevenLabs picker + shared emotion
                │   ├── VoicePicker.jsx      # Searchable voice catalog with audio preview
                │   ├── ScheduleSection.jsx  # Claude timing suggestions + editable grid
                │   ├── SourcesSection.jsx   # Source collection builder (✨ suggest)
                │   └── PlatformsSection.jsx
                └── tab2/
                    ├── CredentialRow.jsx
                    └── platformFields.js
```

---

## Setup

### Prerequisites
- Python 3.11+ with a virtualenv
- PostgreSQL 14+
- Node.js 14+ (for the React SPA)

### 1. Clone and create virtualenv
```bash
git clone <repo>
cd content-factory
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment
```bash
cp .env.example .env
# Required: DATABASE_URL, FERNET_KEY, ANTHROPIC_API_KEY
# Required for Agent 2: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
# Optional: ELEVENLABS_API_KEY, CLAUDE_MODEL (default: claude-sonnet-4-6)
```

Generate a Fernet key:
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 3. Create database
```bash
sudo -u postgres psql -c "CREATE USER cf_postgres WITH PASSWORD 'postgres';"
sudo -u postgres psql -c "CREATE DATABASE content_factory OWNER cf_postgres;"
alembic upgrade head   # applies all 6 migrations
```

### 4. Start the API server
```bash
uvicorn app.main:app --reload --log-level info
```

On first startup, a static dev user (UUID `00000000-...-0001`) is seeded automatically.
**Auth is stubbed** — no `Authorization` header needed in development.
If `TELEGRAM_BOT_TOKEN` is set and `TELEGRAM_WEBHOOK_URL` is empty, Telegram polling starts automatically.

> **TODO:** Replace static dev user with real JWT Bearer token authentication.

### 5. Start the React UI (optional, for Agent 1 setup)
```bash
cd app/ui && npm install && npm run dev   # → http://localhost:5173
```

---

## API

Interactive docs: **http://localhost:8000/docs**  
Auth is stubbed in development — no token needed.

### Health
| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness check |

### Users
| Method | Path | Description |
|---|---|---|
| GET | `/api/agent1/users/me` | Get current user (pipeline_run_hour, pipeline_timezone) |

### Channels
| Method | Path | Description |
|---|---|---|
| POST | `/api/agent1/channels` | Create a DRAFT channel |
| GET | `/api/agent1/channels` | List all channels |
| GET | `/api/agent1/channels/{id}` | Get channel with config, languages, voices, sources, timings |
| PUT | `/api/agent1/channels/{id}` | Update name / description / niche / tone |
| DELETE | `/api/agent1/channels/{id}` | Delete a DRAFT channel (blocked if active) |
| PUT | `/api/agent1/channels/{id}/config` | Upsert channel config (videos_per_week, shorts_rule, subtitle styles…) |
| PUT | `/api/agent1/channels/{id}/languages` | Replace language list |
| PUT | `/api/agent1/channels/{id}/voices` | Replace voice assignments per language |
| PUT | `/api/agent1/channels/{id}/sources` | Replace content sources |
| PUT | `/api/agent1/channels/{id}/timings` | Upsert publish timing per platform × language |
| POST | `/api/agent1/channels/{id}/suggest-timing` | Claude-generated optimal publish schedule per language |
| POST | `/api/agent1/channels/{id}/credentials` | Save Fernet-encrypted platform credentials |
| POST | `/api/agent1/channels/{id}/verify` | Verify credentials (stub — returns true) |
| POST | `/api/agent1/channels/{id}/activate` | Activate pipeline (requires ≥1 verified credential) |

### Voices
| Method | Path | Description |
|---|---|---|
| GET | `/api/agent1/voices?language=fr&use_case=narration` | List up to 10 trending ElevenLabs voices |

### AI Suggestions
| Method | Path | Description |
|---|---|---|
| POST | `/api/agent1/suggest` | Claude field suggestion (niche, tone, name, sources…) in user's language |

### Content Discovery (Agent 2)
| Method | Path | Description |
|---|---|---|
| POST | `/api/agent2/run/{channel_id}` | Manually trigger full discovery pipeline (202 Accepted) |
| GET | `/api/agent2/content` | List content items with status filter |

### Telegram
| Method | Path | Description |
|---|---|---|
| POST | `/api/telegram/webhook` | Receive Telegram updates (production webhook mode) |

---

## Scheduler (Celery Beat)

6 periodic tasks + 4 on-demand tasks. Start with:
```bash
celery -A app.scheduler worker --loglevel=info    # workers
celery -A app.scheduler beat --loglevel=info      # beat scheduler
```

| Task | Schedule | What it does |
|---|---|---|
| `dispatch_discovery` | Every 6h | Finds channels overdue for content and fires `run_agent2_for_channel` |
| `check_validation_timeouts` | Every 15 min | Auto-approves or flags NEEDS_REVIEW for expired Telegram validations |
| `pickup_approved_content` | Every 15 min | Fires `run_multilingual_generation` for APPROVED content |
| `schedule_content_creation` | Every hour | D-1 trigger: fires discovery at user's configured `pipeline_run_hour` when next publish is tomorrow |
| `dispatch_publishing` | Every 30 min | Logs content due for publish (placeholder — Agent 7 will upload) |
| `pickup_scripts_ready` | Every 15 min | Fires `run_agent3_validation` for SCRIPTS_READY content |
| `pickup_scripts_validated` | Every 15 min | Fires `run_agent4_for_content` for SCRIPTS_VALIDATED content |
| `pickup_audio_done` | Every 15 min | Fires `run_agent5_for_content` for AUDIO_DONE content |

---

## Database Schema

20 tables across 5 migrations:

**Channel setup** — `users`, `channels`, `channel_config`, `channel_languages`, `channel_voices`, `channel_sources`, `channel_platforms`, `channel_publish_timing`, `proxy_config`

**Content pipeline** — `content`, `scripts`, `content_validations`

**Media production** — `audio_files`, `video_sections`, `video_renders`, `video_metadata`

**Distribution & analytics** — `publish_schedule`, `video_analytics`, `analytics_anomalies`

Notable schema decisions:
- `content.content_hash` — SHA-256(URL + title) unique index for deduplication
- `channel_platforms.credentials_encrypted` — Fernet-encrypted JSON blob
- `users.pipeline_run_hour` / `users.pipeline_timezone` — D-1 generation trigger time per user

---

## Development Roadmap

| Phase | Description | Status |
|---|---|---|
| 1 | DB schema + 5 Alembic migrations (20 tables) | ✅ Done |
| 2 | Agent 1 — Channel Setup (25 API endpoints + React SPA) | ✅ Done |
| 3 | Agent 2 — Content Discovery + Telegram validation + Celery scheduler | ✅ Done |
| 4 | Agent 3 — Script Validation (MAJOR auto-correct, MINOR Telegram, duration + Shorts breakpoints) | ✅ Done |
| 5 | Agent 4 — Audio (ElevenLabs + Whisper) | ✅ Done |
| 6 | Agent 5 — Video (Remotion + sub-agents) | ✅ Done |
| 7 | Agent 6 — Thumbnails + Metadata | 🔜 |
| 8 | Agent 7 — Publishing + proxies | 🔜 |
| 9 | Agent 8 — Analytics (last) | 🔜 |

---

## Known limitations / deferred

- **Authentication**: Static dev user UUID (`00000000-...-0001`) — real JWT deferred to final step
- **Platform verification**: All four platform verifiers (YouTube, TikTok, Instagram, Facebook) are stubs that always return `true`
- **Agent 7 publishing**: `dispatch_publishing` logs due content but does not upload — placeholder
- **Claude web_search**: Discovery uses `web_search_20250305` (Sonnet 4.6 required — Haiku not supported)
