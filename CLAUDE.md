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
| Scheduler | Celery Beat |
| Base de données | PostgreSQL (local, pas Docker) |
| ORM | SQLAlchemy 2.0 |
| IA / Agents | Claude API — modèle : claude-sonnet-4-6 |
| Voix | ElevenLabs API |
| Sous-titres | OpenAI Whisper API (word-level timestamps) |
| Vidéo | Remotion (Node.js, appelé depuis Python via subprocess) |
| Vidéo IA | Runway API (max 5s, dernier recours uniquement) |
| Images stock | Pexels API + Unsplash API + Pixabay API |
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
- **Step A — Deterministic Python checks** (`app/services/script_checks.py`) — run first on ALL languages, no Claude call:
  - `check_completeness`: [INTRO]/[OUTRO]/[SECTION N] markers present, consecutive numbering, no empty bodies, terminal punctuation — MAJOR
  - `check_minimum_length`: voice_script ≥ 900 words (youtube_long) or ≥ 420 words (short-form) — MAJOR
  - `check_length_coherence`: cross-language — any lang >30% from median word count — MAJOR
  - `check_hook_quality`: first sentence after [INTRO] ≤15 words and no forbidden opener — MAJOR
  - `check_tts_compliance`: no sentence >18 words, no digit-runs, no forbidden chars `()/%&`, no abbreviations (Dr./vs./etc.), no ALL-CAPS words 3+ letters — MAJOR (NEW)
  - `check_retention_structure`: short-form sections ≤130 words or contain `?`; youtube_long non-last sections must not end with summary-pattern sentences — MINOR (NEW)
- **Step B — Claude validation** (`PROMPT_VERSION = "2.0"`) — only for languages with NO deterministic MAJOR:
  - TONE (MINOR), LINGUISTIC_NATURALNESS (MINOR), CONTENT_POLICY (MAJOR)
  - Claude never re-checks structural/length/hook/TTS issues — those are done in Python
- Auto-correction loop (max 3 rounds): corrects MAJOR issues per language; re-runs det checks on ALL langs after each round (length_coherence is cross-lang); re-runs Claude only on corrected languages that cleared det MAJOR; carries forward prior verdicts for untouched languages
- `source_excerpt` (≤8000 chars of original source body) injected into the correction prompt when `minimum_length` is among the issues — Claude expands from source facts, not filler
- MINOR issues logged to `script_issues_log`; Celery task passes to minor_handler for Telegram notification
- Output : scripts validés + durées estimées + breakpoints Shorts → Agent 4

### Agent 4 — Audio Generation
- Envoie chaque script voix à ElevenLabs (voice_id + emotion depuis channel_voices)
- **Sélection du modèle ElevenLabs** : piloté par `channel_voices.elevenlabs_model`
  - `eleven_v3` : audio tags (`<laugh>`, `<sigh>`, `<break time="0.5s" />`) + `speed_profile` (`slow`/`normal`/`fast`) + presets de stabilité (`v3_stability_preset`) — `audio_tags_enabled` (channel_config) doit être `true` pour qu'Agent 2 les injecte dans le script voix
  - `eleven_multilingual_v2` (défaut) : VoiceSettings classiques (`stability_override`, `similarity_override`, `style_override`, `speed_override`, `use_speaker_boost`)
- Reçoit fichier audio par langue → stocké sur serveur
- Mesure durée exacte en millisecondes
- Recalcule breakpoints Shorts basés sur durée audio réelle
- Appelle Whisper sur le fichier audio → timestamps mot par mot (pour sous-titres karaoké)
- **Bookend audio Shorts** : génère via Claude un re-hook audio et un bridge audio par Short — textes écrits par Claude depuis le contexte Whisper autour du breakpoint, synthétisés via ElevenLabs — stockés dans `audio_files.short_rehook_paths` et `short_bridge_paths` (JSONB lists indexées par short_index, `null` si inutile)
- Output : fichiers audio + durées exactes + breakpoints + transcriptions Whisper + bookends Shorts → Agent 5

### Agent 5 — Video Generation
- Agent autonome avec sous-agents internes
- Base : Remotion + stock Pexels/Unsplash/Pixabay téléchargé localement avant le rendu
- Runway : uniquement si (1) pas de stock pertinent ET (2) scène critique ET (3) max 5s ET (4) activé dans config — à éviter
- **Sous-agents :**
  - Storyboard Agent : génère les beats visuels depuis le script Whisper — chaque beat porte `visual_intent`, `visual_type`, `search_query`, `stock_query`, `broad_query`, `fallback_query`, `query_style` (`textural`|`environmental`|`action`), `environment`, `motif`, `transition_to_next`, `overlay_text`
  - Section Splitter : fallback si le Storyboard échoue — découpe le script en sections à intervalles égaux
  - Section Validator : valide chaque section (ton, durée, flow, impact visuel) — max 3 rounds, meilleure tentative si limite atteinte
  - Assembly Validator : vérifie l'ensemble avant rendu (répétitions d'environnement, URLs dupliquées, dérive de durée, saturation des overlays)
  - Shorts Cutter : regroupe les beats en segments Shorts, reframe 16:9→9:16, optimise les 3 premières secondes (hook), ajoute numéro de partie
- **Media fetch — architecture immediate-download :**
  - Pour chaque beat : Claude score des candidats de 3 providers (Pexels, Unsplash, Pixabay) sur 0–100
  - Boucle scorée : ≥72 au 1er essai → accepté immédiatement ; ≥70 au 2e → stop ; score < 55 après tous les essais → candidat rejeté + dark fallback ; sinon 3e essai avec rotation de requêtes (stock_query → search_query → broad_query → fallback_query → refetch_query Claude)
  - Déduplication cross-beats : une URL distante appliquée à un beat est exclue de tous les beats suivants du même run — évite les clips identiques dans la vidéo
  - **Invariant absolu** : le candidat sélectionné est téléchargé immédiatement via `asset_manager.py` avant d'être appliqué au beat — `beat["media_url"]` est toujours un chemin local (`cache/abc123.mp4`), jamais une URL http
  - Si le téléchargement échoue : candidat suivant dans la réponse scorée — jamais de repli sur l'URL distante
  - Provider disable-per-run : une erreur 403 Unsplash désactive Unsplash pour tous les beats restants du run
  - `asset_manager.py` : cache thread-safe SHA-256(url)[:24] + extension, déduplication par `threading.Event`
- **Rendu Remotion :**
  - Vidéo principale (16:9) : rendu chunked si durée > 90s (`chunk_duration_sec`) — chaque chunk est rendu séparément puis concaténé via ffmpeg
  - Shorts (9:16) : rendus individuellement
  - `PRE_RENDER_ASSET_AUDIT` : audit avant rendu — FAIL FAST si une URL http survit dans les props (ne télécharge pas, bloque le rendu)
  - `remotion_builder.py` : lève `ValueError` si une URL http est détectée lors de la construction des props
- **Sous-titres :**
  - Vidéo principale : style standard
  - Shorts : style karaoké (mot courant surligné en #FFD700)
  - Générés depuis timestamps Whisper (pas depuis le script)
- **Numéro de partie Shorts :** "Partie {n}/{total}" (FR), "Part {n} of {total}" (EN), "Parte {n}/{total}" (ES/IT), style et position depuis config canal
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
                         video_color_grade, runway_enabled, audio_tags_enabled
channel_languages      — id, channel_id, language, channel_name
channel_voices         — id, channel_id, language, provider, voice_id, emotion, music_style,
                         elevenlabs_model, stability_override, similarity_override,
                         style_override, speed_override, use_speaker_boost,
                         v3_stability_preset, speed_profile
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
                         shorts_breakpoints (JSONB), whisper_transcript (JSONB),
                         short_rehook_paths (JSONB), short_bridge_paths (JSONB)
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
- **Media assets (Agent 5)** : téléchargés immédiatement à la sélection du candidat — `media_url` est toujours un chemin local (`cache/`), jamais une URL http. Remotion et Chromium n'ouvrent aucune connexion réseau pendant le rendu
- **Rendu chunked (Agent 5)** : vidéos > 90s découpées en chunks de 90s, rendues séparément, concaténées via ffmpeg — élimine les crashes mémoire Chromium sur les vidéos longues
- **Banned query patterns (Agent 5)** : 14 patterns de requêtes interdits (clichés stock : couloirs sombres, silhouettes, brouillard, etc.) — si `stock_query` matche, `broad_query` est promu à sa place (Python enforce, jamais juste un warning). `query_style` (`textural`|`environmental`|`action`) catégorise le type visuel ciblé pour détecter et éviter les slideshows
- **Bookend audio Shorts (Agent 4)** : chaque Short reçoit un re-hook audio (clause d'accroche, joué en début) et un bridge audio (transition "suite au prochain épisode", joué après la narration principale) — générés par Claude + ElevenLabs, stockés en JSONB indexé par short_index

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
│   │   │   ├── system_prompt.py        # Storyboard + scoring + validation prompts
│   │   │   ├── subagents/
│   │   │   │   ├── storyboard.py       # Génère les beats visuels depuis Whisper
│   │   │   │   ├── section_splitter.py # Fallback : découpe égale par section
│   │   │   │   ├── section_validator.py
│   │   │   │   ├── assembly_validator.py
│   │   │   │   └── shorts_cutter.py
│   │   │   └── services/
│   │   │       ├── video.py            # Orchestrateur principal
│   │   │       ├── stock_fetcher.py    # Boucle scorée + sélection candidats
│   │   │       ├── asset_manager.py    # Téléchargement immédiat + cache local thread-safe
│   │   │       ├── media_localizer.py  # Audit de sécurité (plus de téléchargement)
│   │   │       ├── remotion_builder.py # Assemblage props JSON (valide l'absence d'URL http)
│   │   │       ├── renderer.py         # CLI Remotion + rendu chunked
│   │   │       └── subtitles.py        # Captions standard + karaoké Whisper
│   │   ├── agent6_metadata/
│   │   ├── agent7_publishing/
│   │   └── agent8_analytics/
│   ├── services/
│   │   ├── claude_client.py    # wrapper Claude API
│   │   ├── telegram_client.py  # wrapper Telegram Bot API
│   │   ├── elevenlabs_client.py
│   │   ├── whisper_client.py
│   │   ├── pexels_client.py
│   │   ├── pixabay_client.py
│   │   ├── unsplash_client.py
│   │   ├── runway_client.py
│   │   └── proxy_manager.py
│   ├── publishers/
│   │   ├── youtube.py
│   │   ├── tiktok.py
│   │   ├── instagram.py
│   │   └── facebook.py
│   ├── scheduler/
│   │   └── tasks.py            # Celery Beat + tâches périodiques
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

## Testing — HARD RULE
**NEVER run `test_full_pipeline.py` or any command that triggers real API calls.**
This includes: Claude API, ElevenLabs, web_search, Pexels/Unsplash/Pixabay, Runway, Telegram, YouTube, Meta.
Validate with static checks only: `ast.parse`, `importlib.import_module`, `inspect.getsource`, pure-Python unit tests with mocked/stubbed externals.
If a live run log is needed, tell the user to run it and paste the output here.

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


## Prompt Assembly Architecture (Block 2)

Agent 2's `system_prompt.py` uses a composed assembly model instead of monolithic prompt strings:

- `BASE_SCRIPT_PROMPT: dict[str, str]` — keyed by `script_format` — structural core (role, structure, word targets, forbidden openers, JSON schema)
- `RETENTION_BLOCK: dict[str, str]` — keyed by `script_format` — platform-specific retention mechanics (mini-hook placement, re-hook cadence, loop endings)
- `TTS_BLOCK: dict[str, str]` — keyed by `elevenlabs_model` — writing constraints baked in at generation time (shared core: ≤18 words, numbers as words, no abbreviations/ALL-CAPS; per-model pacing rules)
- `build_script_system_prompt(script_format, elevenlabs_model, audio_tags_enabled, niche, tone) -> str` — assembles the full generation prompt; block order is fixed for cache-friendliness per (format, model) pair
- `build_native_system_prompt(script_format, elevenlabs_model, audio_tags_enabled) -> str` — same assembly for native adaptation prompts
- `with_tts_block(prompt: str, model: str) -> str` — appends TTS_BLOCK to any existing prompt; used by rewrite, correction, and revision prompts

**Invariants:**
- TTS constraints are present in ALL script-producing prompts (generation, native adaptation, rewrite, correction, revision). Violations that reach Agent 3 are residual sentence-length overflow, not character-class failures.
- `generate_revised_scripts()` now returns `{"title", "video_script", "voice_script", "changes"}` — callers must handle the `changes` array (persisted to `ContentValidation.script_issues_log`; appended to the next Telegram message).
- `generate_native_script()` always receives a `hook_context` string (extracted from the optimised source script's first sentence if not provided by the caller) — native adaptations must preserve the opening hook's concrete specificity.
- `optimize_intro()` scores across 6 dimensions (honesty removed; fabrication is a hard disqualifier in the prompt). Python max total = 60.
- Source-language voice model is resolved in `tasks.py` before `generate_scripts()` is called. Target-language voice models are resolved in `generate_multilingual_scripts()` per language from `ChannelVoice`.
- `optimize_intro()` is now wired in `tasks.py` (called after the Script Quality Gate, before persistence and Telegram send).

**Over-cap Shorts fix (implemented in Block 4, folded):**
`_enforce_duration_bounds()` in `agent4_audio/services/breakpoints.py` now runs two passes:
Pass 1 (floor): drops cuts that would produce segments < 61s (unchanged). Pass 2 (ceiling):
scans resulting segments; for any segment > 91s, computes `n_pieces = ceil(seg_dur / 91s)`
and inserts `n_pieces - 1` equal-interval bisection cuts. If `piece_ms < 61s` the segment
cannot be split without violating the floor — it is logged as WARNING and left unchanged
(e.g. a ~115s segment that would split into two ~58s pieces is kept as-is). For the observed
FR 139.9s case: 2 pieces of ~69.95s — splits cleanly. The EN 115.7s case cannot be split
cleanly and logs a WARNING.

**Block 5 — Stock fetcher rewrite (implemented in Block 5):**
`score_media_candidates_with_claude()` in `agent5_video/system_prompt.py` migrated to
`call_claude_structured(task="media_scoring")` with `_MEDIA_RANKING_SCHEMA` + vision image
blocks (thumbnail URL-based). Old `_MEDIA_CANDIDATE_SCORING_SYSTEM_PROMPT`, `_parse_scoring_response`,
`_extract_json_object` removed. `_MEDIA_RANKING_SYSTEM_PROMPT` + `_MEDIA_RANKING_SCHEMA` added.
Prescreening (`_prescreen`: resolution ≥ 1280×720, landscape only, video ≥ beat_duration_sec) runs
before every Claude call via `_verified_candidates` (concurrent HEAD checks). `fetch_all_beats()`
now processes beats in parallel via `ThreadPoolExecutor(max_workers=6)` with thread-safe
`_ProviderStatus` (Lock), `used_urls_lock` (atomic URL claim-before-download), `env_best_lock`.
Dark fallback replaced with `_apply_env_reuse_or_text_card()`: (a) reuse highest-scored local clip
from same `environment` in this run; (b) if none → `visual_source="text_card"`, `media_url="__dark_fallback__"`.
`validate_beats_for_render` updated to allow `text_card` beats through the render gate.
Pixabay video `thumb_url` fixed (was `userImageURL` — an avatar, not a preview; now `""`).
Video-first implemented: for `query_style in ("action","environmental")` only video candidates sent
to Claude; images added as fallback if no videos pass prescreen.
`text_card` TextCard.tsx Remotion composition deferred to Block 6 (currently renders as dark frame;
`visual_source="text_card"` set in props so Block 6 can wire the composition).

**CDN access note (Block 5):**
URL-based vision image blocks (`source.type="url"`) fail for many stock CDN URLs (Pixabay,
Unsplash) because Anthropic's API servers cannot reach them (HTTP 400 "Unable to download").
Pexels photo URLs typically work. When vision fails, `_verified_candidates` HEAD-check passes
(CDN is reachable) but Claude API subsequently 400s — `score_media_candidates_with_claude` falls
back to `_deterministic_best_candidate`. This is a CDN access restriction in Anthropic's datacenter,
not a code bug. If vision hit rate needs to improve: switch to base64-encoded thumbnails (download
→ encode → pass as `source.type="base64"`). Not implemented in Block 5 — too expensive per beat.

**Block 6 — Render: pre-bundling, parallel chunks, bridge fps fix, TextCard beat, post-render verification (implemented in Block 6):**

`verify.py` (`agent5_video/services/verify.py`) — `verify_render(mp4_path, expected_duration_ms, fmt)` runs after every
Remotion render (gated by `VERIFY_RENDERS=true`): ffprobe checks duration ±2%, exactly one audio stream, correct
resolution (1920×1080 main / 1080×1920 short); `blackdetect` catches any black interval ≥3 s;
`silencedetect` catches any interior silence ≥4 s (ignoring first/last 1 s edge). Failure → `VerifyFailedError`
raised in `_run_renders()` → VideoRender row NOT saved → `content.status = "NEEDS_REVIEW"` → error log
(mp4 kept on disk for inspection). Shorts skip duration check (`expected_duration_ms=None`) because
bookend padding is variable.

`ensure_bundle()` in `renderer.py` — SHA-256 of `remotion/src/` tree + `package.json`/`package-lock.json`
determines bundle identity. Bundle stored under `remotion/bundles/{hash}/`. Reused on cache hit;
`npx remotion bundle` run on miss. Old bundles pruned to keep last 2. Gated by `REMOTION_PRE_BUNDLE=true`
(default `false`). `render_*` functions all accept `bundle_dir=` and pass it as the Remotion CLI entry point.

Parallel chunk rendering — `CHUNK_PARALLEL_WORKERS=N` (default 1). Set to 2 only after measuring
single-chunk peak RSS on the VPS (`/usr/bin/time -v npx remotion render ...`) and confirming
2× peak < 80% of total RAM. `render_main_video_chunked` uses `ThreadPoolExecutor(max_workers=N)`
for the render phase; audio-slice + props-write phases remain sequential (no thread-safety issues).

Bridge fps fix in `Short.tsx` — `shortCalculateMetadata` now computes `bridgeExtraFrames = Math.ceil(bridge_duration_ms / 1000 * 30)` using the stored `bridge_duration_ms` from props
instead of the old fixed 60-frame (2 s) buffer. Legacy rows without `bridge_duration_ms` fall back to 60 frames.
`rehook_duration_ms` likewise used to scope the rehook text overlay sequence.

Rehook text overlay — when `rehook_text` is non-null in Short props, a `RehookOverlay` component
displays it in gold (#FFD700) during the rehook audio window (fades in/out). This requires Block 3's
bookend dict shape `{"path", "duration_ms", "text"}` — `_bookend_text()` shim returns `""` for legacy str entries.

`TextCard.tsx` — `remotion/src/components/TextCard.tsx`: dark radial gradient + centred text + accent bar
+ subtle slow-zoom (1.0→1.05). Wired in `MediaSection.tsx` when `section.visual_type === "text_card"`.
Python side: `_section_for_remotion()` in `remotion_builder.py` overrides `visual_type = "text_card"`
when the beat dict has `visual_source == "text_card"` (set by stock_fetcher's env-reuse fallback cascade).

Bookend shims in `remotion_builder.py` — `_bookend_path()` / `_bookend_duration()` / `_bookend_text()`
handle both old bare-string and new dict JSONB shapes. `build_short_props()` now writes
`rehook_duration_ms`, `bridge_duration_ms`, `rehook_text` into the props JSON.

New settings in `config.py`:
- `CHUNK_PARALLEL_WORKERS` (int, default 1)
- `REMOTION_PRE_BUNDLE` (bool, default false)
- `VERIFY_RENDERS` (bool, default true)

**Block 7 — Telegram summary wiring + existing-props verify (implemented in Block 7):**

`run_story_scoring_gate()` now returns `tuple[Story, dict] | None` (was `Story | None`) — the second
element is the full Claude `assessment` dict from `assess_story_quality()`. `run_discovery()` unpacks
it and returns `(content, story, assessment)`. `run_agent2_for_channel` in `tasks.py` unpacks the
3-tuple, loads `target_languages` from `ChannelLanguage`, and passes both to `send_for_validation()`.
`send_for_validation()` now accepts `assessment` and `target_languages` and passes them to
`generate_telegram_summary()` — top-2 scoring dimensions and language list now appear in the Telegram
message. `estimated_duration_sec` still uses the word-count fallback (post-Agent 3 estimate requires
Agent 3 to have run; not yet wired because Telegram is sent before Agent 3 runs).

`_render_from_existing_props()` in `video.py` now calls `ensure_bundle()` and passes `bundle_dir` to
every render call; calls `verify_render()` after the main render and each short; raises
`VerifyFailedError` on failure. `_process_language` wraps the `_render_from_existing_props` call in a
`try/except VerifyFailedError` block that sets `content.status = "NEEDS_REVIEW"` and returns `False`,
matching the behavior of the `_run_renders` path.

---

## Claude Client Architecture

Shared infrastructure lives in app/services/claude_client.py:
- Anthropic client singleton (_get_client)
- Retry logic with exponential backoff
- Rate limit + timeout error handling
- Empty response guard
- Constants: _MAX_RETRIES, _BACKOFF_BASE
- Entry points: call_claude(), call_claude_with_usage(), call_claude_structured(), call_claude_with_tools()

Model routing lives in app/services/model_routing.py:
- MODEL_ROUTING dict: task key → model ID
- resolve_model(task, model_override=None) — resolution order: override → CLAUDE_TIER=dev → MODEL_ROUTING[task]
- Unknown task → ValueError (fail-loud, no default)
- CLAUDE_TIER=dev in .env forces Haiku globally for cheap dev iterations

All call_claude* functions require `task=` as a keyword-only argument. The task key is used for model
routing and appears in every log line for cost attribution.

call_claude_structured() uses forced tool-use (tool_choice={"type":"tool","name":schema_name}) to
guarantee structured JSON output without text parsing. Use for Block 4+ storyboard and scoring calls.

Each agent that uses Claude has its own system_prompt.py inside its folder:
- agent1_setup/system_prompt.py     — field suggestion prompt
- agent2_discovery/system_prompt.py — story discovery + script writing
- agent3_validation/system_prompt.py — script quality checking
- agent5_video/system_prompt.py     — scene splitting + visual decisions
- agent5_video/subagents/system_prompt.py — section validation
- agent6_metadata/system_prompt.py  — titles, descriptions, thumbnails
- agent8_analytics/system_prompt.py — pattern analysis + recommendations

Agent 7 does not use Claude (platform APIs only).
Agent 4 uses Claude only for Short bookend generation (`_BOOKEND_SYSTEM_PROMPT` in `agent4_audio/services/audio.py` — prompt is short, no separate system_prompt.py needed).

max_tokens per agent:
- Agent 1: 256   (short field values)
- Agent 2: 4096  (full scripts); generate_native_script uses a higher limit for multilingual adaptation
- Agent 3: 1024  (subjective validation — 3 checks only: tone, linguistic_naturalness, content_policy); 4096 for auto_correction
- Agent 4: 512   (bookend text per Short — short Claude call)
- Agent 5: 2048  (scene descriptions)
- Agent 6: 1024  (titles + descriptions)
- Agent 8: 2048  (weekly analysis report)

Rules:
- Never put a system prompt inside app/services/claude_client.py
- Never call the Anthropic client directly from an agent — always go through call_claude()
- Every call_claude* call must pass task= (keyword-only). Adding a new task requires adding it to MODEL_ROUTING first
- Prompt caching (cache_control ephemeral) applied automatically when system prompt exceeds 800 chars
- Every call logs: task, resolved model, cached(yes/no), input/output token counts

## Model Strategy (Block 0 — task routing)
- Model routing is per-task, not per-agent (app/services/model_routing.py)
- Dev and prod resolve models identically from MODEL_ROUTING — no env-based override exists.
  Testing in dev uses the same model as production; there is no Haiku dev-override.
- Sonnet tasks (high quality or tool-use): script_generation, native_adaptation, quality_rewrite,
  intro_optimization, auto_correction, storyboard, story_scoring, revision, story_research
  (web_search — Haiku does not support it), channel_suggestion (onboarding quality)
- Haiku tasks (fast/cheap): script_quality_check, script_validation, media_scoring, semantic_splits,
  bookends, telegram_summary, content_reformat, section_validation, section_splitting,
  visual_reinterpretation
- CLAUDE_TIER in .env is an inert ops label — it has no effect on model selection
- model_override= on individual calls bypasses routing (highest precedence; discouraged in production)

## Prompt Engineering Rules for Claude API Calls

When modifying or adding prompts used by the application, follow these rules:

1. Each Claude prompt must have one clear responsibility only.
2. Prefer strict JSON output schemas for any programmatic response.
3. Prompts must explicitly say: “Return ONLY valid JSON. No markdown. No code fence. No extra keys.”
4. Never rely only on prompt instructions for validation; add Python-side parsing and schema checks.
5. Do not fabricate sources, facts, URLs, RSS feeds, or subreddits.
6. For script generation, preserve factual grounding from the source material and avoid invented details.
7. For revision/correction tasks, apply minimal changes unless a full rewrite is explicitly requested.
8. Keep stable system prompts separate from dynamic user/context data to improve Claude prompt caching.
9. Do not truncate scripts when asking Claude to revise full scripts unless the task is explicitly partial.
10. After changing prompts, update related parsing/validation code and run tests or add tests when missing.

## Reliability Rules

When generating production code:

1. Prefer deterministic behavior over creative behavior.
2. Validate every Claude JSON response in Python.
3. Never trust AI output without schema validation.
4. Business rules belong in Python, not prompts.
5. Prompts generate content; code enforces correctness.
6. Never send partial data when expecting full regeneration.
7. Log token usage, response time, and failures for every Claude call.
8. All prompt changes must preserve backward compatibility unless explicitly approved.
9. Every system prompt must have a version identifier.
10. If Claude cannot determine an answer reliably, return an explicit error instead of guessing.

# Whenever Claude returns JSON:

- Parse with json.loads()
- Validate required keys
- Validate value types
- Reject unknown keys unless explicitly allowed
- Raise ValueError on schema mismatch

# Token Budget Rules

- Never send more than 80% of model context window.
- Truncate source material before sending to Claude.
- Prefer summaries over raw content when context exceeds 10k characters.
- Log prompt length and estimated token count.

# Determinism Rules

For:
- validation
- scoring
- ranking
- classification

Claude must:
- use fixed criteria
- avoid subjective wording
- produce repeatable outputs

# Business rules belong in Python.

Prompts may:
- generate content
- classify content
- summarize content

Prompts must NOT:
- implement workflow decisions
- decide retries
- decide database state transitions
- enforce authorization

# Every system prompt must expose:

PROMPT_VERSION = "x.y"

When behavior changes:
- increment version
- document reason

Current versions:
- Agent 2 system_prompt.py: PROMPT_VERSION = "3.0"
- Agent 3 system_prompt.py: PROMPT_VERSION = "2.0"
- Agent 5 system_prompt.py: PROMPT_VERSION = "2.2", STORYBOARD_SCHEMA_VERSION = "2.5"

# Hallucination Prevention

Claude must never:

- invent URLs
- invent RSS feeds
- invent Reddit communities
- invent source facts
- invent statistics

# If information is unavailable:
return explicit failure.

# Never send partial scripts to a revision prompt
when expecting a full-script response.
