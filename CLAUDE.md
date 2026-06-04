# CLAUDE.md — Content Factory

## Vue d'ensemble du projet
Système automatisé de création et publication de vidéos multilingues sur YouTube, TikTok, Instagram et Facebook. Le système fonctionne 24h/24 sans intervention humaine quotidienne. L'utilisateur interagit uniquement via Telegram pour valider les contenus.

---

## Stack technique

| Composant | Technologie |
|---|---|
| Langage | Python 3.11+ |
| Framework API | FastAPI |
| Queue / Workers | Celery + Redis |
| Scheduler | APScheduler |
| Base de données | PostgreSQL |
| ORM | SQLAlchemy |
| Containerisation | Docker + Docker Compose |
| IA / Agents | Claude API — modèle : claude-sonnet-4-6 |
| Voix | ElevenLabs API |
| Sous-titres | OpenAI Whisper API (word-level timestamps) |
| Vidéo | Remotion (Node.js, appelé depuis Python via subprocess) |
| Vidéo IA | Runway API (max 5s, dernier recours uniquement) |
| Images stock | Pexels API + Unsplash API (gratuits) |
| Thumbnails | Pillow (Python) + DALL-E fallback |
| Notifications | Telegram Bot API (gratuit) |
| Publication | YouTube Data API v3, Meta Graph API, TikTok API |
| Proxies | Brightdata résidentiel (1 profil par langue/région) |
| Chiffrement | Python Fernet (AES-256) pour les credentials |
| Serveur | Hetzner VPS Ubuntu 22.04 |

---

## Architecture des agents

Le pipeline est composé de 8 agents qui s'exécutent en séquence pour chaque vidéo.

### Agent 1 — Channel Setup
- UI web avec champs assistés par IA
- Tab 1 : configuration du canal (description, langues, noms, niche, voix, fréquence, shorts, sources, plateformes)
- Tab 2 : credentials par plateforme × langue avec vérification live
- Chaque champ se déverrouille quand le précédent est rempli
- Claude propose des suggestions, l'utilisateur valide ou modifie
- Sauvegarde en DRAFT jusqu'à validation des credentials
- Pipeline activé par ligne de credential vérifiée (activation partielle possible)
  > **Décision d'implémentation :** toutes les credentials doivent être vérifiées avant activation (choix UX pour fiabilité)

### Agent 2 — Content Discovery
- Déclenchement : schedule (cron) ou manuel via POST /run/{channel_id}
- Source la meilleure histoire dans la langue la plus riche pour la niche (pas forcément la langue de l'utilisateur)
- Scoring des histoires : pertinence + engagement (upvotes, commentaires) + fraîcheur
- Déduplication via SHA-256 hash (URL + titre) dans PostgreSQL
- Génère : titre + script vidéo + script voix dans la langue source
- Envoie résumé à l'utilisateur via Telegram (dans sa langue primaire)
- Boucle de validation : APPROVE ou CHANGE + feedback (limite configurable, défaut 3, max 5)
- Timeout configurable (défaut 24h) → auto-approve
- Sur limite atteinte : auto_approve ou needs_review (configurable par canal)
- Après validation : génère script natif par langue (pas traduction — adaptation culturelle)
- Tous les scripts sauvegardés en DB → passe à Agent 3

### Agent 3 — Script Validation
- Reçoit tous les scripts de toutes les langues simultanément
- Vérifications : cohérence de longueur entre langues (>30% = majeur), ton, complétude, breakpoints Shorts, conformité politique contenu, naturalité linguistique
- Problème MAJEUR → auto-correction (max 3 tentatives) → si échec : NEEDS_REVIEW + notification Telegram
- Problème MINEUR → notification Telegram "Reply FIX ou ignoré dans 5 min" → FIX : corrige et continue → pas de réponse : log + continue
- Output : scripts validés + durées estimées + breakpoints Shorts → Agent 4

### Agent 4 — Audio Generation
- Envoie chaque script voix à ElevenLabs (voice_id + emotion depuis channel_voices)
- Reçoit fichier audio par langue → stocké sur serveur
- Mesure durée exacte en millisecondes
- Recalcule breakpoints Shorts basés sur durée audio réelle
- Appelle Whisper sur le fichier audio → timestamps mot par mot (pour sous-titres karaoké)
- Output : fichiers audio + durées exactes + breakpoints + transcriptions Whisper → Agent 5

### Agent 5 — Video Generation
- Agent autonome avec sous-agents internes
- Base : Remotion + Pexels/Unsplash stock
- Runway : uniquement si (1) pas de stock pertinent ET (2) scène critique ET (3) max 5s ET (4) activé dans config — meilleur de ne pas utiliser
- **Sous-agents :**
  - Section Splitter : découpe le script en sections selon changements naturels de scène
  - Section Validator : valide chaque section (ton, durée, flow, impact visuel, source) — max 3 rounds, meilleure tentative si limite atteinte
  - Runway Decision : spawné uniquement si Runway envisagé, valide les 4 conditions
  - Assembly Validator : vérifie l'ensemble avant rendu (flow, pacing, durée totale vs audio)
  - Shorts Cutter : regroupe sections en Shorts, reframe 16:9→9:16, optimise les 3 premières secondes (hook), ajoute numéro de partie
- **Sous-titres :**
  - Vidéo principale : style standard
  - Shorts : style karaoké (mot courant surligné en #FFD700)
  - Générés depuis timestamps Whisper (pas depuis le script)
- **Numéro de partie Shorts :** "Partie {n}/{total}" (FR), "Part {n} of {total}" (EN), "Parte {n}/{total}" (ES/IT), style et position depuis config canal
- Remotion rend : vidéo principale (16:9) + tous les Shorts (9:16) en un seul pass
- Output : fichiers vidéo + Shorts → Agent 6

### Agent 6 — Thumbnails & Metadata
- Extrait la frame la plus impactante de la vidéo (scoring: contraste, action, impact visuel)
- Améliore avec Pillow : color grade, texte teaser, branding canal
- Fallback DALL-E si aucune frame satisfaisante
- Formats : 16:9 (YouTube/Facebook), 1:1 (Instagram), 9:16 (TikTok/Shorts)
- Génère titres + descriptions optimisés par plateforme par langue :
  - YouTube : titre SEO + description complète + timestamps + hashtags
  - TikTok : titre court + 3-5 hashtags
  - Instagram : hook émotionnel + hashtags
  - Facebook : titre conversationnel + description longue
- Envoie thumbnail + titre + description YouTube à l'utilisateur via Telegram (sa langue)
- Validation : APPROVE ou CHANGE, limite depuis config (défaut 3), auto-approve après 1h
- Output : thumbnails + métadonnées → Agent 7

### Agent 7 — Publishing
- Calcule le créneau de publication optimal par plateforme × langue × timezone
- Shorts publiés en décalé (shorts_spread_hours, défaut 6h entre chaque)
- Publie via proxy résidentiel régional (FR→IP française, EN/US→IP américaine, etc.)
- Vérifie credentials avant publication
- 3 tentatives par plateforme → PUBLISH_FAILED + notification Telegram si échec
- Stocke platform_video_id retourné par chaque API
- Update statut → PUBLISHED

### Agent 8 — Analytics (développé en dernier)
- Polling : 1h, 24h, 7j, 30j, 90j après publication
- Métriques : vues, likes, commentaires, partages, watch time, CTR, revenus
- Anomalie si déviation >50% de la moyenne du canal → notification Telegram
- Rapport hebdomadaire (chaque lundi) : max 5 recommandations actionnables
- Si pattern constant sur 3+ semaines → propose mise à jour config canal → approuvé via Telegram
- Dashboard UI : KPIs, tableau performance par canal, heatmap horaire, comparaison Shorts vs vidéo longue

---

## Schéma base de données — tables principales

```
users                  — id, name, telegram_chat_id, primary_language
channels               — id, user_id, name, niche, tone, active
channel_config         — channel_id, videos_per_week, shorts_rule,
                         validation_timeout_hours, validation_max_revisions,
                         validation_on_limit_reached, subtitle_style_main,
                         subtitle_style_shorts, subtitle_karaoke_active_color,
                         shorts_part_label_style, video_style_type,
                         video_color_grade, runway_enabled
channel_languages      — id, channel_id, language, channel_name
channel_voices         — id, channel_id, language, provider, voice_id, emotion, music_style
channel_sources        — id, channel_id, source_type, source_value, language, trust_score
channel_platforms      — id, channel_id, language, platform, platform_channel_id,
                         credentials_encrypted, verified, active
channel_publish_timing — id, channel_id, platform, language, timezone,
                         optimal_days (JSON), optimal_hour_start, optimal_hour_end,
                         shorts_spread_hours
proxy_config           — id, language, region, provider, proxy_url_encrypted, active

content                — id, channel_id, source_url, source_language, content_hash,
                         title, status, created_at, published_at
scripts                — id, content_id, language, video_script, voice_script,
                         estimated_duration_sec, shorts_breakpoints (JSON),
                         validated, version
content_validations    — id, content_id, telegram_message_id, status,
                         revision_count, sent_at, approved_at, timeout_at,
                         script_validation_status, script_issues_log,
                         self_correction_attempts

audio_files            — id, content_id, language, file_path, duration_ms,
                         shorts_breakpoints (JSON), whisper_transcript (JSON)
video_sections         — id, content_id, language, section_order, script_text,
                         audio_start_ms, audio_end_ms, visual_source,
                         search_query, generation_prompt, effect, color_grade,
                         runway_used, subagent_rounds, best_attempt_used
video_renders          — id, content_id, language, format, short_order,
                         file_path, duration_seconds, hook_modified, render_time_seconds

publish_schedule       — id, content_id, platform, language, scheduled_at,
                         published_at, proxy_region, platform_video_id,
                         status, retry_count, failure_reason
video_analytics        — id, content_id, platform, language, polled_at,
                         poll_type, views, likes, watch_time_seconds,
                         avg_view_duration_pct, ctr, revenue_usd
analytics_anomalies    — id, content_id, detected_at, type, metric,
                         expected_value, actual_value, notified_user
```

---

## Décisions clés

- **Notifications** : Telegram Bot API (gratuit, pas Twilio)
- **Langue source** : meilleure pour la niche, pas forcément celle de l'utilisateur
- **Scripts multilingues** : génération native par langue, pas traduction
- **Validation utilisateur** : toujours dans la langue primaire de l'utilisateur
- **Déduplication** : SHA-256(URL + titre) stocké dans content.content_hash
- **Credentials** : chiffrés Fernet avant INSERT, déchiffrés uniquement à l'exécution
- **Runway** : dernier recours absolu, max 5s, désactivable par canal
- **Sous-titres karaoké** : depuis timestamps Whisper sur fichier audio réel (pas le script)
- **Publication** : proxies résidentiels Brightdata par région linguistique
- **Shorts** : publiés en décalé, pas tous en même temps

---

## Structure du projet

```
content-factory/
├── CLAUDE.md
├── docker-compose.yml
├── .env                        # variables d'environnement (jamais en git)
├── alembic/                    # migrations DB
├── app/
│   ├── main.py                 # FastAPI entry point
│   ├── config.py               # settings depuis .env
│   ├── database.py             # SQLAlchemy session
│   ├── models/                 # modèles SQLAlchemy (1 fichier par table)
│   ├── schemas/                # Pydantic schemas
│   ├── agents/
│   │   ├── agent1_setup/
│   │   ├── agent2_discovery/
│   │   ├── agent3_validation/
│   │   ├── agent4_audio/
│   │   ├── agent5_video/
│   │   │   └── subagents/
│   │   ├── agent6_metadata/
│   │   ├── agent7_publishing/
│   │   └── agent8_analytics/
│   ├── services/
│   │   ├── claude_client.py    # wrapper Claude API
│   │   ├── telegram_client.py  # wrapper Telegram Bot API
│   │   ├── elevenlabs_client.py
│   │   ├── whisper_client.py
│   │   ├── pexels_client.py
│   │   ├── runway_client.py
│   │   └── proxy_manager.py
│   ├── publishers/
│   │   ├── youtube.py
│   │   ├── tiktok.py
│   │   ├── instagram.py
│   │   └── facebook.py
│   ├── scheduler/
│   │   └── scheduler.py        # APScheduler + Celery tasks
│   └── ui/                     # Frontend Agent 1 + Analytics
├── remotion/                   # projet Remotion séparé (Node.js)
│   ├── src/
│   │   ├── compositions/
│   │   │   ├── MainVideo.tsx
│   │   │   ├── Short.tsx
│   │   │   └── Subtitles.tsx
│   └── package.json
└── tests/
```

---

## Variables d'environnement (.env)

```
# Base de données
DATABASE_URL=postgresql://user:password@localhost:5432/content_factory

# Redis
REDIS_URL=redis://localhost:6379

# Claude API
ANTHROPIC_API_KEY=

# ElevenLabs
ELEVENLABS_API_KEY=

# OpenAI (Whisper)
OPENAI_API_KEY=

# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Pexels
PEXELS_API_KEY=

# Runway
RUNWAY_API_KEY=

# Brightdata
BRIGHTDATA_USERNAME=
BRIGHTDATA_PASSWORD=

# Chiffrement credentials
FERNET_KEY=

# Remotion
REMOTION_PATH=./remotion
```

---

## Ordre de développement

1. Schema DB + migrations Alembic
2. Agent 1 — Channel Setup UI
3. Agent 2 — Content Discovery + Telegram loop
4. Agent 3 — Script Validation
5. Agent 4 — Audio ElevenLabs + Whisper
6. Agent 5 — Video Remotion + sous-agents
7. Agent 6 — Thumbnails + Metadata
8. Agent 7 — Publishing + proxies
9. Agent 8 — Analytics (en dernier)

---

## Coût mensuel

| Service | Coût |
|---|---|
| Hetzner VPS | ~6€ |
| Claude API | ~20–50€ |
| ElevenLabs | ~22€ |
| Brightdata | ~10€ |
| Telegram | 0€ |
| Pexels/RSS | 0€ |
| **Total** | **~58–88€/mois** |
## Setup
- Always activate venv before any command: source venv/bin/activate
- Local PostgreSQL — no Docker needed
- DATABASE_URL set in .env

## Coding Standards
- Python 3.11+
- SQLAlchemy 2.0 style — Mapped / mapped_column only
- Type hints on all functions
- Pydantic for all data validation
- One responsibility per function
- All API calls wrapped in try/except with proper logging
- Environment variables via config.py only, never hardcoded
- Tests with pytest for all business logic
- Docstrings on all public functions
- No multi-line comment blocks inside model files
- JSONB (not JSON) for all JSON columns in PostgreSQL
- Mapped[list] for list columns, Mapped[dict] for object columns
- make README.md changes every time you see we need it 

## Schema decisions
- Videos are NOT stored locally after publishing
  — video_renders has no file_path
  — platform_title stored in publish_schedule after upload
- video_metadata table stores Agent 6 output
  (titles, descriptions, hashtags, thumbnail_file_path per platform/language)
- thumbnail_file_path is temporary — deleted after upload to platform
- created_at added to users and content_validations (useful, keep it)
## Authentication
Not implemented yet — to be added as the VERY LAST step before deployment.
For now all routes are unprotected.
When the time comes: JWT tokens + bcrypt password hashing.
The users table is already in the schema, ready for auth fields to be added.

## Docstring Standard
All public functions and classes must have a docstring using this exact format:

    def my_function(param1: str, param2: int = 0) -> str:
        """One-line summary of what it does.

        Args:
            param1: Description.
            param2: Description. Defaults to 0.

        Returns:
            Description of return value.

        Raises:
            ValueError: When and why this is raised.
            anthropic.APIError: On API failure.
        """

Rules:
- First line: one sentence, no period at end
- Args section: only if function has parameters
- Returns section: only if function returns something meaningful (not None)
- Raises section: only if function raises exceptions explicitly
- Private functions (prefixed with _) do not need docstrings
- One-liners are allowed only if the function name is self-explanatory


## Claude Client Architecture

Shared infrastructure lives in app/services/claude_client.py:
- Anthropic client singleton (_get_client)
- Retry logic with exponential backoff
- Rate limit + timeout error handling
- Empty response guard
- Constants: _MAX_RETRIES, _BACKOFF_BASE
- One base function: call_claude(system_prompt, user_message, max_tokens) -> str

Each agent that uses Claude has its own system_prompt.py inside its folder:
- agent1_setup/system_prompt.py     — field suggestion prompt
- agent2_discovery/system_prompt.py — story discovery + script writing
- agent3_validation/system_prompt.py — script quality checking
- agent5_video/system_prompt.py     — scene splitting + visual decisions
- agent5_video/subagents/system_prompt.py — section validation
- agent6_metadata/system_prompt.py  — titles, descriptions, thumbnails
- agent8_analytics/system_prompt.py — pattern analysis + recommendations

Agents 4 and 7 do not use Claude (ElevenLabs and platform APIs only).

max_tokens per agent:
- Agent 1: 256   (short field values)
- Agent 2: 4096  (full scripts)
- Agent 3: 1024  (validation output)
- Agent 5: 2048  (scene descriptions)
- Agent 6: 1024  (titles + descriptions)
- Agent 8: 2048  (weekly analysis report)

Rules:
- Never put a system prompt inside app/services/claude_client.py
- Never call the Anthropic client directly from an agent — always go through call_claude()
- Prompt caching (cache_control ephemeral) applied in call_claude() automatically
  when prompt exceeds 1024 tokens

## Model Strategy
- Development / testing: claude-haiku-4-5-20251001 (fast, cheap)
- Production: claude-sonnet-4-6 (full quality)
- Model is set via CLAUDE_MODEL in .env — never hardcoded
- Switch to Sonnet only when testing output quality, not pipeline logic