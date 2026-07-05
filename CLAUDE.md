# CLAUDE.md — Content Factory Engineering Guide

This file is the **single source of truth** for Content Factory architecture, coding rules, prompting rules, and operational constraints.

It has two major parts:

1. **Platform architecture** — what exists today, how the pipeline flows, which modules own which responsibility, and what invariants must hold.
2. **Rules** — mandatory engineering, database, testing, naming, prompting, Claude Code/Codex, logging, reliability, and safety rules.

When architecture changes, update this file in the same change set.  
When a rule becomes obsolete, remove or replace it.  
Do not append temporary phase notes. This file describes the current product, not the development history.

---

# Part 1 — Platform Architecture

## 1. Product Overview

Content Factory is an automated media pipeline for generating multilingual long-form videos and standalone short episodes for YouTube, TikTok, Instagram, and Facebook.

The system:

- Discovers source stories from configured sources.
- Scores and deduplicates stories.
- Sends selected stories to the operator through Telegram for approval.
- Generates long-form scripts.
- Generates standalone short episode scripts linked to the long-form parent.
- Generates audio with TTS.
- Generates word-level timestamps with Whisper.
- Generates visual storyboards.
- Generates Flux images.
- Renders videos through Remotion.
- Stores all outputs in PostgreSQL and the local media filesystem.
- Runs through Celery workers and Celery Beat scheduling.

The current production-relevant pipeline covers:

- Agent 1 — Channel Setup
- Agent 2 — Story discovery, validation, long script generation, short episode planning
- Agent 3 — Audio and Whisper
- Agent 4 — Visual planning and media generation
- Agent 5 — Rendering
- Agent 6+ — not part of the current media-generation core yet

Deterministic script checks and script correction run inside Agent 2.

---

## 2. Core Technology Stack

| Area | Technology |
|---|---|
| Language | Python 3.11+ |
| API | FastAPI |
| Workers | Celery |
| Scheduler | Celery Beat |
| Queue/Broker | Redis |
| Database | PostgreSQL |
| ORM | SQLAlchemy 2.0 |
| Migrations | Alembic |
| AI text/reasoning | Claude API |
| TTS | Cartesia by default; ElevenLabs supported as legacy/provider option |
| Transcription | OpenAI Whisper API with word-level timestamps |
| Image generation | fal.ai Flux Schnell |
| Video rendering | Remotion, called from Python through subprocess |
| Notifications | Telegram Bot API |
| Credential encryption | Fernet |
| Frontend | React/Vite for Agent 1 UI |
| Media storage | Local filesystem under configured media path |

---

## 3. High-Level Pipeline

```mermaid
flowchart TD
    A[Agent 2 discovery] --> B[Content PENDING_APPROVAL]
    B --> C[Telegram approval]
    C --> D[Content APPROVED]
    D --> E[Long script generation]
    E --> F[Scripts validated]
    F --> G[Standalone short planner]
    G --> H[Child short scripts validated]
    F --> I[Parent Agent 3 audio + Whisper]
    H --> J[Child Agent 3 audio + Whisper]
    I --> K[Parent AUDIO_DONE]
    J --> L[Child AUDIO_DONE]
    K --> N[Parent Agent 4 visual pass]
    N --> N2[Parent PARENT_VISUALS_DONE]
    N2 --> O[Agent 5 parent main render]
    O --> P[Parent RENDERED]
    L --> Q[Child Agent 4 visual remap]
    N2 --> Q
    Q --> Q2[Child CHILD_SHORT_VISUALS_DONE]
    Q2 --> R[Agent 5 child vertical short render]
    R --> S[Child RENDERED]
```

Arrow explanation:

Discovery  
→ creates parent `Content` row  
→ sends Telegram validation  
→ approval marks parent as `APPROVED`  
→ Agent 2 generates and validates the parent source script  
→ Agent 2 generates and validates all required parent multilingual scripts  
→ parent scripts reach `SCRIPTS_VALIDATED`  
→ short planner creates child short contents and validated source scripts (requires the
parent's own validated source `Script` row — see `run_shorts_planner`)  
→ Agent 2 generates and validates all required child multilingual scripts  
→ child scripts reach `SCRIPTS_VALIDATED`  
→ parent Agent 3 generates parent audio and Whisper  
→ child short episodes are immediately eligible for Agent 3 audio  
→ parent reaches `AUDIO_DONE`  
→ each child generates its own audio and Whisper  
→ parent Agent 4 generates shared visual beats and writes `PARENT_VISUALS_DONE`  
→ child Agent 4 remaps parent visual beats to its own short narration — this
requires the parent's persisted `VideoSection(language="__visual__")` rows,
i.e. requires `PARENT_VISUALS_DONE` to have been reached at least once — and
writes `CHILD_SHORT_VISUALS_DONE`  
→ Agent 5 renders the long video once parent is `PARENT_VISUALS_DONE`, and each
child short once it is `CHILD_SHORT_VISUALS_DONE`  
→ each child renders as vertical `Short.tsx`  
→ parent and children reach `RENDERED` independently

Important ordering rule:

- Child script generation requires the parent's validated source `Script` row
  (`run_shorts_planner` aborts if it is missing). Short planning may use the
  parent source script, but parent `SCRIPTS_VALIDATED` is written only after
  the complete required parent script set is generated and validated.
- Agent 3 pickup must see only fully completed script sets: for both parent and
  child content, `SCRIPTS_VALIDATED` means the required source-language script
  exists, the required source-language script is validated, all configured
  multilingual scripts exist, and all configured multilingual scripts are validated.
- Child short videos require child audio and the parent's persisted visual
  beats (`PARENT_VISUALS_DONE` reached at least once for the parent).
- Child short videos do **not** require the parent MP4 to be fully rendered
  (parent `RENDERED`).
- Child short videos must never use parent audio.
- Parent content must never render cut-down parent shorts.

---

## 4. Runtime Status Model

### 4.1 Parent Long Content

```text
PENDING_APPROVAL
→ APPROVED
→ GENERATING_SCRIPTS
→ SCRIPTS_VALIDATED
→ GENERATING_AUDIO
→ AUDIO_DONE
→ GENERATING_VISUALS
→ PARENT_VISUALS_DONE
→ RENDERING
→ RENDERED
```

Optional/problem statuses:

```text
AWAITING_MANUAL_STORY
NEEDS_REVIEW
FAILED
```

### 4.2 Child Short Episodes

```text
GENERATING_SCRIPTS
→ SCRIPTS_VALIDATED
→ GENERATING_AUDIO
→ AUDIO_DONE
→ GENERATING_VISUALS
→ CHILD_SHORT_VISUALS_DONE
→ RENDERING
→ RENDERED
```

A child may revert from `GENERATING_VISUALS` back to `AUDIO_DONE` if its
parent has not yet reached `PARENT_VISUALS_DONE` — this is a normal wait, not
a failure, and the child is re-picked-up on the next Beat cycle.

Script completion rule:

- For both parent and child content, `SCRIPTS_VALIDATED` means: required source-language script exists; required source-language script is validated; all required multilingual scripts exist; all required multilingual scripts are validated; and no intermediate script status remains active.
- For parent content, `SCRIPTS_VALIDATED` means the source-language long script
  exists and is validated, and every configured multilingual script exists and
  is validated. If no channel target languages are configured, the validated
  source-language script is the complete required script set.
- For child short episodes, `SCRIPTS_VALIDATED` means the source-language short
  script exists and is validated, and every configured multilingual child short
  script exists and is validated. If no channel target languages are configured,
  the validated source-language short script is the complete required script set.
- `SCRIPTS_READY` is retired and must not be read or written by live runtime code.
- `SCRIPTS_VALIDATED_AWAITING_PARENT` is retired and must not be read or
  written by live runtime code. Child short audio is picked up from
  `SCRIPTS_VALIDATED` like any other content; there is no parent-audio gate
  status in the current architecture.
- `run_script_workflow()` owns the final parent `SCRIPTS_VALIDATED` transition.
- `run_shorts_planner()` owns the final child `SCRIPTS_VALIDATED` transition.

Child short audio rule:

- Child short contents become `SCRIPTS_VALIDATED` only after their complete
  required standalone short script set passes validation.
- Child short audio does not wait for parent `AUDIO_DONE`.
- Each child must generate its own audio and Whisper.

### 4.3 Status Ownership

| Transition | Owner |
|---|---|
| `PENDING_APPROVAL` → `APPROVED` | Telegram approval handler |
| `APPROVED` → `GENERATING_SCRIPTS` → `SCRIPTS_VALIDATED` | Agent 2 |
| `SCRIPTS_VALIDATED` → `GENERATING_AUDIO` → `AUDIO_DONE` | Agent 3 |
| `AUDIO_DONE` → `GENERATING_VISUALS` → `PARENT_VISUALS_DONE` / `CHILD_SHORT_VISUALS_DONE` (or back to `AUDIO_DONE` on child defer) | Agent 4 |
| `PARENT_VISUALS_DONE` / `CHILD_SHORT_VISUALS_DONE` → `RENDERING` → `RENDERED` | Agent 5 |
| any stage → `FAILED` | the agent active at that stage |

No agent writes a status outside its own row in this table. Agent 4 never
writes `RENDERING`/`RENDERED`; Agent 5 never writes
`GENERATING_VISUALS`/`PARENT_VISUALS_DONE`/`CHILD_SHORT_VISUALS_DONE`.

`VIDEO_DONE` and `GENERATING_VIDEO` are retired — no runtime code path reads
or writes them. They previously conflated Agent 4's visual-generation window
and Agent 5's render window into one ambiguous status with crossover
ownership; this section's split removes that ambiguity. `SCRIPTS_READY` and
`SCRIPTS_VALIDATED_AWAITING_PARENT` are also retired script-stage statuses;
current script/audio pickup uses only `SCRIPTS_VALIDATED`. No compatibility
shim was needed for the retired video statuses: nothing outside the render
pipeline itself ever filtered on `VIDEO_DONE`/`GENERATING_VIDEO` (no Agent
6/publishing consumer existed yet).

---

## 5. Core Database Tables

### 5.1 Configuration Tables

```text
users
channels
channel_config
channel_languages
channel_voices
channel_sources
channel_platforms
channel_publish_timing
proxy_config
```

### 5.2 Pipeline Tables

```text
content
scripts
content_validations
audio_files
video_sections
video_renders
publish_schedule
video_analytics
analytics_anomalies
```

### 5.3 Key Model Invariants

#### `content`

Parent content:

```text
is_short_episode = False
parent_content_id = NULL
short_part_number = NULL
short_total_parts = NULL
```

Child short content:

```text
is_short_episode = True
parent_content_id = parent Content.id
short_part_number = 1-based part index
short_total_parts = total number of parts
```

#### `scripts`

- The narrator text lives in `voice_script` — the only script-text column.
  (`video_script` was dropped by migration `002_drop_script_video_script_column.py`;
  it was always assembled identically to `voice_script`.)
- Parent rows contain the long-form `[INTRO]`/`[SECTION N: Title]`/`[OUTRO]`-marked script.
- Child short rows contain standalone short narration.
- Child short scripts are flat narration, not `[SECTION N]` structured long-form scripts.

#### `audio_files`

- Parent `AudioFile` contains parent audio and parent Whisper transcript.
- Child `AudioFile` contains child short audio and child Whisper transcript.
- `shorts_breakpoints`, `short_rehook_paths`, and `short_bridge_paths` are not part of the V2 schema.
- Parent and child audio rows store only their own audio metadata and Whisper transcript.

#### `video_sections`

- Parent shared visual pass writes rows using `language="__visual__"`.
- Parent per-language render can also write/load language-specific rows.
- Child short episodes do not generate a full visual pass.
- Child short episodes write remapped, timed beats under their actual language.

#### `video_renders`

Parent long render:

```text
content_id = parent id
format = "main"
short_order = NULL
```

Child short render:

```text
content_id = child id
format = "short"
short_order = child.short_part_number - 1
```

No parent content may create `VideoRender(format="short")`.

---

## 6. Project Structure

```text
content-factory/
├── CLAUDE.md
├── .env
├── alembic/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── database.py
│   ├── models/
│   ├── schemas/
│   ├── services/
│   │   ├── claude_client.py
│   │   ├── model_routing.py
│   │   ├── script_checks.py
│   │   ├── telegram_client.py
│   │   └── ...
│   ├── scheduler/
│   │   └── tasks.py
│   └── agents/
│       ├── agent1_setup/
│       ├── agent2_discovery/
│       ├── agent3_audio/
│       ├── agent4_visuals/
│       └── agent5_render/
├── remotion/
│   └── src/
│       ├── compositions/
│       │   ├── MainVideo.tsx
│       │   └── Short.tsx
│       └── components/
└── scripts/
```

---

## 6A. Service Ownership Boundaries

Scheduler and Celery tasks own orchestration only:

- scheduling
- enqueueing
- retries
- task coordination
- worker guard checks
- task-level status handoffs

Scheduler and Celery tasks must not own prompt construction, script generation,
storyboard logic, audio generation, Flux logic, render transformations, or media
generation logic. When those behaviors are currently coordinated from a task for
runtime sequencing, the business logic must live in the relevant agent service.

Agent ownership:

| Area | Owner | Owns |
|---|---|---|
| Story discovery and scripts | Agent 2 | discovery, validation handoff, long script generation, deterministic script validation, multilingual scripts, standalone short planning and child short script generation |
| Audio | Agent 3 | TTS generation, Whisper transcription, audio persistence, audio validation |
| Visuals | Agent 4 | storyboard generation and validation, beat generation, timestamp mapping, Flux prompt generation and validation, Flux image generation, media reuse, child short visual remap, `VideoSection` persistence, visual-readiness task orchestration |
| Rendering | Agent 5 | reading existing `VideoSection`/`AudioFile` rows, subtitles, Remotion props, rendering, render verification, `VideoRender` persistence |

Shared services own only generic infrastructure:

- API clients
- credential/security helpers
- common deterministic utilities
- serialization/parsing helpers
- generic storage/client abstractions

Shared services must not contain agent orchestration or agent-specific business
flows. Deterministic utilities may be used by agents when they remain generic and
side-effect free.

Current state (Phase 4D-D implemented — Agent 4 is the visual-ready producer,
Agent 5 is a render-only consumer, and status is the pickup source of truth):

- `app/agents/agent4_visuals/services/visual_orchestrator.py` owns visual
  generation end to end. `run_visual_generation_for_content(content_id, db)`
  is the Agent 4 **task** entrypoint (called from the
  `run_agent4_visual_generation_for_content` Celery task): it loads its own
  preconditions (`Content`, `Channel`, `ChannelConfig`, validated `Script`
  rows, `AudioFile` rows), transitions `AUDIO_DONE` -> `GENERATING_VISUALS`,
  and calls `run_visual_generation()` — storyboard generation, storyboard
  validation, Flux prompt generation, Flux image generation/cache reuse,
  child short narration remap, and all `VideoSection` persistence
  (`VideoSection(language="__visual__")` for parents and per-language
  `VideoSection` rows for both parent and child short). On success it writes
  `Content.status = "PARENT_VISUALS_DONE"` (parent) or
  `"CHILD_SHORT_VISUALS_DONE"` (child) — Agent 4 is the sole writer of these
  three statuses. On `CHILD_SHORT_VISUALS_DEFERRED` it reverts `Content.status`
  to `AUDIO_DONE` for re-pickup; on `VISUALS_FAILED` it sets `"FAILED"`.
- `app/agents/agent5_render/services/video.py` does not import any
  `app.agents.agent4_visuals` module and does not call Agent 4 in any form.
  `run_video_generation()` loads its own preconditions independently
  (`Content`, `Channel`, `ChannelConfig`, validated `Script` rows, `AudioFile`
  rows), requires `Content.status` to already be `PARENT_VISUALS_DONE`,
  `CHILD_SHORT_VISUALS_DONE`, or `RENDERING` (re-entrant retry), and
  transitions to `RENDERING` at the start. It reads existing `VideoSection`
  rows with a shared read-only loader (`app.services.video_sections.load_video_sections()`,
  roadmap 6.5 — previously a byte-duplicated private copy per agent) as a
  **defensive** check — it never writes that table, and never generates
  storyboards, runs Flux, performs remap, or falls back to Agent 4 in any way.
  If a language has no persisted `VideoSection` rows despite the status saying
  ready, Agent 5 defers that language (`RENDER_DEFERRED ...
  reason=visual_sections_missing`) rather than treating it as a hard failure.
  On success it writes `Content.status = "RENDERED"` — Agent 5 is the sole
  writer of `RENDERING`/`RENDERED`. Agent 5 owns subtitles, Remotion props,
  rendering, render verification, and `VideoRender` persistence.
- Agent 4 and Agent 5 are fully decoupled at the Celery layer, not just the
  Python-import layer: `pickup_audio_done` (status `AUDIO_DONE`, has
  `AudioFile`, no `VideoRender` yet) dispatches
  `run_agent4_visual_generation_for_content`. `pickup_visual_ready` (status
  `PARENT_VISUALS_DONE`/`CHILD_SHORT_VISUALS_DONE` — **status is the primary
  readiness signal**, not `VideoSection` existence) independently dispatches
  `run_agent5_render_for_content`; `VideoSection` row existence is checked
  again only as a defensive validation (a status/row mismatch is logged as a
  data-consistency warning and skipped, not silently rendered). Neither Celery
  task calls the other directly; the handoff is purely through `Content.status`
  polled every 15 minutes by Celery Beat (`pickup-audio-done`,
  `pickup-visual-ready`).
- Visual-readiness milestone logs (`PARENT_VISUALS_START`, `PARENT_VISUALS_DONE`,
  `CHILD_SHORT_VISUALS_START`, `CHILD_SHORT_VISUALS_DONE`,
  `CHILD_SHORT_VISUALS_DEFERRED`) are emitted from the Agent 4 orchestrator.
  Render milestone logs (`RENDER_START`, `RENDER_DONE`,
  `CHILD_SHORT_RENDER_START`, `CHILD_SHORT_RENDER_DONE`, `RENDER_DEFERRED`)
  stay in Agent 5.
- `VIDEO_DONE` and `GENERATING_VIDEO` are fully retired — see Section 4.3 for
  the complete status ownership table and the dependency graph (Section 3) for
  the parent→child script and visual dependencies this pickup model assumes.

---

## 7. Shared Services Architecture

### 7.1 Claude Client

File:

```text
app/services/claude_client.py
```

Responsibilities:

- Own the Anthropic client singleton.
- Apply retries/backoff.
- Log every Claude call.
- Support structured tool-use responses.
- Support tool/web-search calls where intentionally used.
- Enforce empty-response guards.
- Keep prompt transport separate from prompt content.

Public call helpers:

```text
call_claude()
call_claude_structured()
call_claude_with_tools()
```

`call_claude_with_usage()` was removed in Phase 10A-0 as confirmed-dead code
(zero callers anywhere in the repo) — it duplicated `call_claude()`'s shared
`_call_claude_core()` retry/caching/logging path with no caller ever using
the extra usage-dict return value. If a future caller needs token-usage
diagnostics from a free-form (non-structured) call, reintroduce it deliberately
rather than assuming it still exists from this section.

**`_call_claude_core()` scans every content block instead of indexing
`content[0]` (live-canary fix):** a real `--confirm` run hit `'NoneType'
object has no attribute 'strip'` because `response.content[0].text` came
back `None`/empty on an otherwise normal, non-tool-use `call_claude()` call
(`task=short_script`). An initial patch (retry on empty/null with the same
backoff `RateLimitError` gets) did not fix the underlying issue: a follow-up
real run reproduced the identical empty-`content[0]` symptom on 12/12
consecutive real API calls (4 short-script parts x 3 retries each), each
with real non-zero `output_tokens` — proof the failure was systematic, not
transient, and no amount of retrying the identical request would help. The
real mechanical bug: `_call_claude_core()` assumed `response.content[0]` was
always the answer, exactly the assumption `call_claude_with_tools()` (same
file) had already learned not to make for this model — it scans every block
for the first `type=="text"` entry with non-empty `.text` rather than
indexing `[0]`. `_call_claude_core()` now does the same scan, duck-typed on
`.type` rather than `isinstance(..., anthropic.types.TextBlock)` for the same
reason: a block type this SDK version doesn't model as a distinct class must
not be misread as fatal when a later block still carries the real text. Only
if no block yields usable text does it raise `ValueError` (`"Claude returned
no usable text block"`, including `stop_reason` and a `block_types` count
breakdown for diagnosis) and retry up to `_MAX_RETRIES` with the same backoff
the `RateLimitError`/`APITimeoutError` branch uses, as a safety net for a
genuinely transient case. Runtime proof:
`tests/test_claude_call_empty_block_retry.py`.

**`parse_claude_json()` tolerates raw control characters (live-canary fix):**
the same run hit `Claude JSON parse error: Invalid control character at: line
1 column 186` on an otherwise well-formed `short_script` response — a
multi-paragraph `voice_script` value contained a literal newline instead of
an escaped `\n`. `json.loads(cleaned, strict=False)` is now used instead of
strict parsing, which accepts raw control characters (tabs/newlines) inside
JSON string values without otherwise loosening validation — a genuinely
malformed JSON document still raises `ValueError` exactly as before. Runtime
proof: `tests/test_claude_call_empty_block_retry.py`.

Rules:

- Agents must never instantiate Anthropic clients directly.
- Agents must never bypass `claude_client.py`.
- Every Claude call must include a `task=` key.
- Every new task key must be added to `app/services/model_routing.py`.
- Use `call_claude_structured()` when code needs JSON or schema-like output.
- Use `call_claude_with_tools()` only when tool use is required.
- Do not put system prompts inside `claude_client.py`.

### 7.2 Model Routing

File:

```text
app/services/model_routing.py
```

Responsibilities:

- Map `task` keys to model IDs.
- Fail loudly on unknown task keys.
- Centralize model selection.
- Resolve primary/secondary model slots from `primary_model` and `secondary_model` environment-backed settings.

Rules:

- No ad-hoc model strings inside agents unless explicitly passed through a documented override.
- Unknown task → `ValueError`.
- Model choice is an architecture decision, not a random call-site detail.
- Cheap/fast tasks should use smaller models only when quality is not degraded.
- High-quality creative generation, source research, and complex visual storyboard tasks use the configured high-quality model.

Current model tier (roadmap 4.2 / audit S-2, exec-6):

- `app/config.py`'s `primary_model`/`secondary_model` defaults are both
  Sonnet-class (`"claude-sonnet-5"` / `"claude-sonnet-4-6"`) — no
  `MODEL_ROUTING` task defaults to Haiku anymore, including the
  `SECONDARY_MODEL`-tier quality gates (`script_quality_check`,
  `short_quality_check`) the audit specifically flagged as "the arbiter of
  your #1 priority is your weakest model." The `PRIMARY_MODEL`-tier creative
  tasks (`script_generation`, `native_adaptation`, `quality_rewrite`,
  `storyboard`, `visual_bible_generation`, `story_blueprint`,
  `section_generation`, `short_script`, plus `story_research`/
  `channel_suggestion`/`channel_research`/`story_gate_scoring`/`revision`)
  resolve to the configured primary model.
- `model_routing.DEFAULT_PRIMARY_MODEL`/`DEFAULT_SECONDARY_MODEL` are kept in
  sync with `config.py`'s defaults as a documentation reference, but are not
  themselves read by `resolve_model()`/`_configured_model()` — those always
  read the live `app.config.settings.primary_model`/`secondary_model` values
  (env-overridable). Do not let the two drift when either default changes.
- Changing which model ID a slot points to (e.g. adopting a newer Sonnet
  release) is a config-value change only — it never requires touching
  `MODEL_ROUTING`'s task→slot assignments.

### 7.3 Deterministic Script Checks

File:

```text
app/services/script_checks.py
```

Responsibilities:

- TTS compliance checks.
- Hook checks.
- Section transition checks.
- Sentence rhythm variance checks (Phase 11.1).
- Completeness checks.
- Length checks.
- Deterministic cleanup utilities.

This file has no per-function check inventory table (unlike Agent 4's
`validate_storyboard()` table in §11.4) — its checks are documented only as
the responsibility bullets above and via each function's own docstring.

Rules:

- Any issue that can be checked deterministically must be checked in Python, not by prompt.
- Claude may generate or revise, but Python decides pass/fail.
- TTS cleanup should run before expensive retries whenever possible.
- No script with remaining MAJOR deterministic issues should be silently marked validated.

`run_deterministic_checks()` was removed (roadmap 6.3 / audit §7): a
self-contained orchestrator whose own docstring claimed a caller that never
existed, confirmed by a repo-wide grep with zero real call sites. Every check
it bundled is already called directly by its real production caller:
`check_completeness()`/`check_minimum_length()`/`check_tts_compliance()` from
`_assemble_sections_with_diagnostics()`, `check_hook_quality()`/
`check_maximum_length()` from `_collect_quality_gate_issues()`, and
`check_length_coherence()` from `generate_multilingual_scripts()` (all in
`agent2/scripts.py`). `check_retention_structure()` — previously reachable
only through the now-deleted orchestrator — is now wired directly into
`_collect_quality_gate_issues()` as MINOR-only telemetry (roadmap 6.3 /
audit §6): its findings are logged (`QUALITY_GATE_RETENTION_STRUCTURE`) but
never converted to a `HIGH`-severity rewrite issue and never counted toward
`det_majors` — it never blocks the gate or triggers a rewrite.

### 7.4 Script Duration Estimation & WPM Calibration (roadmap 3.8 / audit G-7)

File:

```text
app/services/script_estimator.py
```

Every duration/word-count assumption in the pipeline was calibrated from a
guess, not measurement, and a real run showed each one was ~25% off: parent
narration measured 135 wpm (assumed 150), child Shorts measured 120 wpm
(planner math assumed 180 / "3 words per second"). `AudioFile` already has
the ground truth for every completed generation (`duration_ms` +
`whisper_transcript` word count) — this file's `compute_measured_wpm()` /
`get_calibrated_wpm()` compute a rolling per-language average from the most
recent real generations (`_ROLLING_WINDOW_SIZE` = 20 rows,
`_MIN_SAMPLES_FOR_CALIBRATION` = 3 before the measured average is trusted
over the static fallback) — no new table or persisted state; the calibration
is a live query over existing `AudioFile` rows.

Responsibilities:

- `compute_measured_wpm(db, language)` — rolling average of
  `len(whisper_transcript) / (duration_ms / 60_000)` over the most recent
  real `AudioFile` rows for a language; `None` if fewer than
  `_MIN_SAMPLES_FOR_CALIBRATION` usable rows exist (fail-open).
- `get_calibrated_wpm(db, language)` — the single call site wpm-dependent
  callers should use: the measured rolling average when `db` is provided and
  enough samples exist, otherwise the static `SPEECH_RATES` table (now only
  ever a cold-start fallback).
- `estimate_duration_sec(voice_script, language, db=None)` — unchanged
  static-fallback behavior when `db` is omitted (every pre-existing caller
  that does not pass `db` is unaffected); uses the calibrated rate when a
  session is passed. Wired into `script_workflow.py`'s
  `_persist_source_script()`/`_set_multilingual_durations()` and
  `scripts.py`'s `generate_multilingual_scripts()`.
- `storyboard.py`'s `_estimate_beat_count()` (diagnostic-only "estimated beat
  count" log — never the real per-segment `target_beat_count`, which is
  always derived from the segment's share of the real measured `duration_ms`)
  accepts an optional `wpm` override; `split_into_beats()` passes
  `get_calibrated_wpm(db, language)` when a `db` session is supplied (both
  `visual_orchestrator.py` call sites — fresh generation and the
  segment-retry path — now thread `db` through), falling back to the
  recalibrated static default (135, was 150) otherwise.

Rules:

- Pure/generic like the rest of §7 — no agent-specific orchestration lives
  here; it only computes a rate from existing rows and returns it.
- `SPEECH_RATES` remains the cold-start fallback only — do not treat it as
  authoritative once enough real `AudioFile` rows exist for a language.
- Do not add a persisted "rolling wpm" table/column — the rolling average is
  computed live from existing `AudioFile` rows on every call; revisit only if
  query cost on this table ever becomes a real, measured problem.

### 7.5 Shared VideoSection Loader (roadmap 6.5 / audit AR-2)

File:

```text
app/services/video_sections.py
```

Responsibilities:

- `load_video_sections(content_id, language, db) -> list[dict]` — the single
  loader for `VideoSection` rows, returning render/pipeline-ready beat dicts
  (JSON `generation_prompt` extras merged in, legacy text-card fields
  normalized). Both Agent 4 (`visual_orchestrator.py`, for `_load_shared_beats()`)
  and Agent 5 (`video.py`, for its per-language render-readiness check) import
  and call this same function — before this phase each carried its own
  byte-identical private copy.

Rules:

- Pure read, no agent-owned business logic — matches the rest of §7's shared
  services (deterministic utilities usable by any agent, no orchestration).
- Do not fork a third copy for a new caller — extend this function if a new
  field needs merging in, or add a new shared helper alongside it.
- Agent 5 importing from here is not a boundary violation: this module lives
  outside both `agent4_visuals` and `agent5_render`, so neither agent imports
  the other's package (CLAUDE.md §11A.1's "Agent 5 must not import
  `app.agents.agent4_visuals`" rule is about agent-to-agent imports, not
  shared `app/services/` utilities).

---

## 8. Agent 1 — Channel Setup

Agent 1 owns channel configuration and credential entry.

Current responsibilities:

- Conversational/dynamic setup UI.
- Field suggestions through Claude.
- Channel configuration persistence.
- Language setup.
- Per-language voice provider/model/Voice ID entry.
- Platform credential entry and verification.
- Fernet encryption before credential storage.
- Channel activation after required credentials are verified.

Key API area:

```text
app/agents/agent1_setup/
```

Rules:

- Credential handling is structured, not AI-driven.
- Raw credentials must never be logged.
- Credentials must be encrypted before storage.
- Credential verification must be explicit and logged without secrets.
- Claude may suggest channel fields, but not validate credentials.
- Voice IDs are operator-provided hard inputs. The setup UI may mark a Voice ID as locally validated for operator review, but it must not call a provider validation API unless a future backend phase explicitly implements one.

### 8.1 Content Factory V3 Groundwork Fields (schema-only, Phase Agent1-V3.2)

`code_report/agent1_v3_1_architecture_baseline_audit.md` audited the
current Agent 1 stack ahead of a V3 redesign (dynamic, conditional channel
setup). Phase Agent1-V3.2 added the minimum additive schema/model
groundwork for that redesign — five new `channel_config` columns. **None
of these fields are read by Agent 2, Agent 3, Agent 4, or Agent 5 yet.**
Setting any of them today changes no runtime behavior — they exist so
Agent 1's API/UI can start capturing operator intent before any agent
acts on it.

| Field | Type | Default | Currently supported value(s) | Coming-soon values (accepted, not yet executed) |
|---|---|---|---|---|
| `content_mode` | `str` (Pydantic `Literal`) | `"single_story"` | `"single_story"` — matches today's only real behavior (one discovery → one parent + standalone shorts per cycle) | `"limited_series"`, `"ongoing_series"` — reserved; no Agent 2 execution logic exists for either |
| `script_source` | `str` (Pydantic `Literal`) | `"reddit"` | `"reddit"` — matches Agent 2's current discovery default | `"ai_generated"`, `"user_provided"`, `"hybrid"` — reserved; Agent 2 always runs the same discovery→blueprint→script flow regardless of this value today |
| `output_mode` | `str` (Pydantic `Literal`) | `"youtube_and_shorts"` | `"youtube_and_shorts"` — matches today's only real behavior (parent render + standalone shorts via `run_shorts_planner`, per §9.4) | `"youtube_long_only"`, `"shorts_only"` — reserved; no agent currently branches on this value |
| `visual_style` | `str` (free-form, no DB enum) | `"documentary"` | **Read by Agent 2 and Agent 4** — Agent 2 injects it into `generate_story_blueprint()`, `generate_section()`, and `generate_short_episode_script()` user messages as "Visual style:" so narration and hook framing align with the channel's visual aesthetic; Agent 4 injects it into every `generate_storyboard_batch()` call as "Global visual direction:" applying a consistent mood/color/lighting constraint across all beats. See §11 (Agent 4) for the Agent 4 injection contract. | Intentionally a **separate column from `video_style_type`** (§19's existing field, same default); a future phase should decide whether to reconcile/deprecate one of them — do not silently merge them without updating this section |
| `image_style` | `str` (free-form, no DB enum) | `"photorealistic"` | **Read by Agent 2 and Agent 4** — Agent 2 injects it into the same three prompt functions as `visual_style` as "Image style:"; Agent 4 injects it into every `generate_storyboard_batch()` call as "Global image style:" applying a consistent Flux rendering approach across all beats. See §11 (Agent 4) for the Agent 4 injection contract. | No per-beat `image_style` override exists; the channel-level setting applies uniformly to every `flux_generated` beat |

Rules:

- `visual_style` and `image_style` are now wired into **both Agent 2 and
  Agent 4** — do not treat them as unread. Agent 2 receives them in
  `generate_story_blueprint()`, `generate_section()`, and
  `generate_short_episode_script()` via the `ScriptWorkflowContext`
  threading chain; Agent 4 receives them in
  `generate_storyboard_batch()` via `split_into_beats()` (see §11.4).
  All other fields (`content_mode`, `script_source`, `output_mode`)
  remain schema-only; do not make any other agent read them until a
  dedicated phase explicitly wires them in, documents the new behavior
  here, and proves it with a runtime test (§19.4).
- `content_mode`/`script_source`/`output_mode` are Pydantic `Literal`
  types in `app/schemas/channel.py` (`ContentMode`/`ScriptSource`/
  `OutputMode`) — the API rejects any value outside the table above.
  `visual_style`/`image_style` are plain strings (no enum), matching the
  existing looseness of `video_style_type`/`video_color_grade`.
- All five columns are `NOT NULL` with a `server_default` (migration
  `alembic/versions/004_add_v3_channel_config_fields.py`) — additive and
  backwards-compatible; every existing channel row is backfilled with the
  same default a brand-new row gets, with no manual data migration step.
- Do not add execution logic for `limited_series`/`ongoing_series`,
  or voice/credential verification under this section — those are
  explicitly separate, not-yet-scheduled phases per the V3.1 audit's
  phase plan.

### 8.2 V3 Config Rule Helpers (Phase Agent1-V3.3; enforced only at activation since V3.4)

File:

```text
app/agents/agent1_setup/services/v3_config_rules.py
```

Pure, local, side-effect-free helpers that classify §8.1's `content_mode`/
`script_source`/`output_mode` values along two independent axes:
**supported** (does the V3.2 Pydantic schema accept this value at all?)
and **executable** (does any agent actually run differently, or run at
all, for this value, today?). No database access, no network call, no
mutation of a `ChannelConfig` row.

**Only one combination is executable today:**
`content_mode="single_story"` + `script_source="reddit"` +
`output_mode="youtube_and_shorts"` — i.e. exactly what every channel
already does. Everything else is schema-supported (an operator can already
save it) but not yet executable:

| Field | Executable today | Supported, not executable | Reason |
|---|---|---|---|
| `content_mode` | `single_story` | `limited_series`, `ongoing_series` | Agent 2 has no multi-episode or open-ended series planning/execution logic |
| `script_source` | `reddit` (only when `content_mode="single_story"`) | `ai_generated` (alias: `claude_generated`), `user_provided`, `hybrid` | Agent 2's discovery flow always fetches a real source story today, for every content_mode — there is no AI-improvised, operator-supplied, or mixed script-origin path |
| `output_mode` | `youtube_and_shorts` | `shorts_only`, `youtube_long_only` | Nothing in Agent 2/Agent 5 reads `output_mode` yet; `run_shorts_planner()` always runs and Agent 5 always renders the parent main video, unconditionally |

`normalize_script_source()` maps `"claude_generated"` → `"ai_generated"` —
a pure mapping, never a DB write. The V3.2 schema itself only ever accepts
`"ai_generated"` (the alias only matters for a future internal caller that
bypasses the Pydantic schema).

`validate_v3_channel_config(config: dict) -> dict` returns
`{"executable": bool, "supported": bool, "issues": list[V3ConfigIssue]}`
— one `V3ConfigIssue` (`severity`/`field`/`code`/`message`) per
not-yet-executable or unsupported field, each `coming_soon_reason()`-backed
where applicable.

Rules:

- **Not wired into any route, the activation check, or any agent yet** —
  confirmed and tested directly (`scripts/smoke_agent1_v3_config_rules.py`):
  neither `routers/channels.py` nor `services/channels.py` imports this
  module. A future phase decides where enforcement belongs (the activation
  route, a new dedicated endpoint, or the frontend) — do not assume this
  module is already protecting anything.
- Do not add execution logic for any "supported, not executable" value
  under this section — that remains separate, not-yet-scheduled phase
  work per the V3.1 audit's phase plan.
- Do not let this module's normalization functions write to the database —
  they only ever return a value.
- **Update (Phase Agent1-V3.4):** this module is now used by
  `activation_readiness.check_activation_readiness()` (§8.3) — a channel
  whose `content_mode`/`script_source`/`output_mode` is not fully
  executable per this module can no longer be activated. It is still not
  called from anywhere in Agent 2/3/4/5, and it still performs no database
  access itself.

### 8.3 Channel Activation Readiness (Phase Agent1-V3.4)

File:

```text
app/agents/agent1_setup/services/activation_readiness.py
```

**The backend is the sole source of truth for activation readiness.** The
frontend's own "all platforms verified" gating on the Activate button
(`Tab2Credentials.jsx`) is a UI affordance only — it may mirror the
backend's rule for a responsive UI, but it must never be treated as
enforcement. `POST /api/agent1/channels/{id}/activate`
(`activate_channel()`) calls
`check_activation_readiness(channel)` and rejects activation (`400`) for
any channel that is not fully ready — a direct API call can no longer
activate an incomplete or partially-verified channel.

This replaces the previous, looser check
(`any(p.verified for p in channel.platforms)` — at least one verified
platform was enough) with a check that **all** selected platform rows
(every `ChannelPlatform` row that exists for the channel — a row only
exists once credentials were saved for that platform×language pair) must
be `verified=True`. This was a real mismatch the V3.1 audit found between
this route and the frontend's own (always-stricter) gating; it is now
fixed at the backend.

`check_activation_readiness(channel)` is a pure function over an
already-eager-loaded `Channel` ORM object (no queries of its own, no
network call) returning
`{"ready": bool, "issues": list[ReadinessIssue], "warnings": list}`. Checks
performed, all independent (every issue is collected, not just the first):

1. A `ChannelConfig` row exists.
2. The channel's V3 config is fully executable per §8.2's
   `validate_v3_channel_config()` — this is how `limited_series`/
   `ongoing_series` (and any other not-yet-executable V3 value) block
   activation.
3. At least one `ChannelLanguage` row exists.
4. Every configured language has at least one `ChannelVoice` row.
5. `script_source="reddit"` (the only executable script source today)
   requires at least one `ChannelSource` row.
6. At least one `ChannelPublishTiming` row exists.
7. At least one `ChannelPlatform` row exists.
8. **Every** existing `ChannelPlatform` row is `verified=True` — not just
   one of them.
9. `output_mode="youtube_and_shorts"` (the only executable output mode
   today) requires a `ChannelPlatform` row with `platform="youtube"`.

Logging: a blocked activation logs
`CHANNEL_ACTIVATION_BLOCKED channel_id=<id> issue_codes=[...]` at WARNING,
listing every blocking issue's machine-readable code.

**Credential save workflow (verify-before-store):** `POST /api/agent1/channels/{id}/credentials`
now calls `platform_verifier.verify()` on the raw (not-yet-encrypted) credentials before
encrypting or persisting anything. If verification fails the endpoint returns `400` and
nothing is stored. On success, `save_credential(db, ..., verified=True)` writes the
encrypted credential and sets `ChannelPlatform.verified = True` in one step — no separate
`/verify` call is needed for a newly-saved credential. The existing `POST .../verify`
endpoint remains for re-verifying previously-saved credentials.

**Pre-flight readiness endpoint:** `GET /api/agent1/channels/{id}/readiness` exposes
`check_activation_readiness()` as a read-only endpoint. `ActivationStep.jsx` calls this on
mount so the operator sees every blocking issue before clicking Activate — the issues list
renders inline as a checklist, and the Activate button is disabled until `ready=True`.
The `POST .../activate` route still calls `check_activation_readiness()` internally, so the
backend remains the sole enforcement point regardless of what the UI shows.

Rules:

- **Platform credential verification itself remains fully stubbed** — this
  phase did not touch `app/services/platform_verifier.py`. Activation now
  consistently relies on the existing `ChannelPlatform.verified` flag
  (requiring it `True` for every selected platform, not just one). Real
  per-platform credential verification is a separate, not-yet-scheduled
  phase.
- The `HTTPException` raised on a blocked activation still carries a plain
  string `detail` (not a structured object).
- Do not loosen check 8 back to "any platform verified" — that reopens the
  exact mismatch this phase fixed.
- Do not call `check_activation_readiness()` from Agent 2/3/4/5 — it is
  Agent 1's own activation gate only.

### 8.4 Setup Wizard UI (9-step redesign, supersedes Phase Agent1-V3.5)

Key API area:

```text
app/ui/src/App.jsx                          (owns the full step state machine)
app/ui/src/components/StepIndicator.jsx     (sticky top nav + "why this step" panel)
app/ui/src/components/ReadinessSidebar.jsx  (live progress checklist)
app/ui/src/components/StepShell.jsx         (generic per-step card/back/next chrome)
app/ui/src/components/ModeStep.jsx          (content_mode selection)
app/ui/src/components/CredentialsStep.jsx   (credential entry/verify, no activate bar)
app/ui/src/components/ActivationStep.jsx    (final readiness + activate + success screen)
app/ui/src/index.css, app/ui/src/form.css   (full visual reskin — dark purple/indigo theme)
```

Phase Agent1-V3.5's three-stage structure (`Tab0Discovery.jsx`,
`Tab1Config.jsx`'s six stacked accordion sections, `Tab2Credentials.jsx`)
was replaced by a single flattened, one-step-at-a-time wizard with 9 named
steps: **Mode → Concept → Languages → Voices → Schedule → Sources →
Platforms → Credentials → Activation**. This was an operator-directed
visual and structural redesign (modeled on an externally supplied
reference design) — not a new V3.x phase number, since no new backend
capability, schema, or executable/supported matrix changed. `Tab0Discovery.jsx`,
`tab0/ModeSelectionSection.jsx`, `Tab1Config.jsx`, `Tab2Credentials.jsx`,
and `Section.jsx` were deleted; `App.jsx` now owns all wizard state
directly (the per-section state and save handlers that used to live in
`Tab1Config.jsx` moved here unchanged).

**Most leaf section components kept their pre-existing logic** —
`LanguagesSection.jsx`, `VoicesSection.jsx`,
`ScheduleSection.jsx`, `SourcesSection.jsx`, `PlatformsSection.jsx`,
`CredentialRow.jsx`, `AISuggestionField.jsx`, and `ChannelList.jsx` remain
ordinary editable form components. `BasicInfoSection.jsx` is the exception:
it now owns the Agent 1 Research Ideas UX (§8.5). No new dependency was
added — no Tailwind, no icon library, no animation library; the visual
design is plain hand-written CSS.

**`BasicInfoSection.jsx` — two-path UX with progressive reveal (supersedes
earlier inline Research panel):** The Concept step now shows a description
textarea and two explicitly-separated action paths. "✨ Research Ideas" is
enabled when the description is **empty** (the operator has no idea yet and
wants Claude to find one); "✨ Validate Description" is enabled when the
description is **non-empty** (the operator has an idea and wants feedback).
Both paths call `POST /api/agent1/research-ideas`. The result appears in a
**modal dialog** (`ResearchDialog`) overlaid on the step, not inline below
the textarea. The editable channel-name/niche/tone fields are **hidden until
the operator takes an action** — either uses a recommendation (populates the
fields and closes the dialog), closes the dialog without applying, or clicks
"Skip — I'll fill in details manually" (progressive reveal). This fixes the
previously-inverted flow (Research was disabled when empty, enabled when
non-empty — the opposite of the intended design).

**`ScheduleSection.jsx` — dropdowns with explanations (Items 16/17):**
`visual_style` and `image_style` are now `<select>` dropdowns driven by
`VISUAL_STYLE_OPTIONS`/`IMAGE_STYLE_OPTIONS` in `constants.js` (structured
`{value, label, description}` objects), replacing the previous
`<input type="text">` + `<datalist>` pair. A per-option description line
renders below the selected value. `output_mode` has an `OUTPUT_MODE_DESCRIPTIONS`
sentence rendered below the dropdown explaining what that mode produces.
All three constants remain in sync with Agent 4's runtime behavior — the
dropdown labels match the values Agent 4 receives.

**`VoicesSection.jsx` — honest voice status labels:** "Validated ✓" and
"Not validated" (implying a provider API had been called) were replaced by
"Saved for review ✓" / "Not saved" and "Save Voice ID" button label. The
status bar below the Voice ID field now reads "Voice ID saved — will be
verified when the channel first runs audio generation." This matches the
actual architecture (no provider API call is made; the Voice ID is stored
and used on Agent 3's first TTS run).

**`Tab2Credentials.jsx` was split into two separate steps**, mirroring the
reference design's separate Credentials/Readiness steps: `CredentialsStep.jsx`
(the credential-row grid only — real Fernet-encrypted save + verify,
unchanged from before) and `ActivationStep.jsx` (the final review +
`POST .../activate` call + success screen). `ActivationStep.jsx` now loads
`GET /api/agent1/channels/{id}/readiness` on mount and displays a pre-flight
checklist with every blocking issue before the operator clicks Activate. The
Activate button is disabled while the readiness check is loading or when
`ready=False`. Issue codes are mapped to human labels in `ActivationStep.jsx`;
the API response format itself is untouched.

**`ModeStep.jsx` replaces `Tab0Discovery.jsx`/`ModeSelectionSection.jsx`**
with identical behavior: only `content_mode="single_story"` (`CONTENT_MODES`
in `constants.js`, mirroring §8.2's executable matrix) allows Continue;
`limited_series`/`ongoing_series` show a Coming Soon notice and make no
API call of any kind.

**`ReadinessSidebar.jsx` is new** — a live checklist (mode/concept/
languages/voices/schedule/sources/platforms/credentials) driven by the
same `completedSteps` state `App.jsx` already tracks; it is a pure
presentation layer over existing state, not a new readiness computation
(the real, authoritative readiness check remains §8.3's backend
`check_activation_readiness()`, surfaced in `ActivationStep.jsx`).

**`StepIndicator.jsx`'s context panel is deliberately a single, honest
"why this step" explanation** — the reference design this was modeled on
also had a second panel implying a live background AI process ("AI Agent
State"); that panel was not carried over because no such background
process exists, and CLAUDE.md §15/§25 forbid implying unimplemented
behavior. Every real AI-assisted action in this wizard (the ✨ suggest
buttons, ✨ Research Ideas, ✨ Suggest timing) already speaks for
itself at the point it runs.

**Per-language voice configuration (Agent 1 V3 voice tab)** —
voice setup is per publishing language, not per channel. For every selected
language, `VoicesSection.jsx` renders an independent Voice Card with
provider, model, Voice ID, and local setup status. Production providers
currently exposed by the UI are Cartesia (`sonic-3.5` default; `sonic-3`
and `sonic-2` also selectable) and ElevenLabs (`eleven_v3` default;
`eleven_multilingual_v2` also selectable). Saving voices writes one
`ChannelVoice` row per language with `language`, `provider`, `tts_model`,
and `voice_id`; it does not create a shared channel-level voice.

**No fake provider/API voice-test or OAuth simulation exists** —
the Voice Card's `Save Voice ID` button (formerly "Validate Voice") is a
local setup-state affordance for a non-empty operator-entered Voice ID; it
does not call Cartesia, ElevenLabs, Claude, or any external service. The
status display reads "Saved for review ✓" (not "Validated") — this matches
the actual architecture where the Voice ID is accepted and stored but only
verified on Agent 3's first real TTS run, not at setup time. No synthesized-tone
fake verification or simulated OAuth handshake was added anywhere. Agent 1's
Research Ideas feature (§8.5) is a real backend Claude call, not a simulated
background process, and it never verifies platform analytics or credentials.

Rules:

- Do not reintroduce a multi-section-at-once accordion view — the wizard
  is one step at a time by design, matching the approved reference.
- Do not add a simulated voice-test button or simulated OAuth flow — these
  were explicitly decided against; if a future phase wants real versions of
  either, that is new backend scope requiring its own CLAUDE.md update, not
  a frontend-only addition.
- Do not add Tailwind, an icon library, or an animation library to
  `app/ui/` without updating this section — the current design intentionally
  uses zero new dependencies.
- Keep `ReadinessSidebar.jsx`'s checklist driven by `App.jsx`'s
  `completedSteps` state, not a second readiness computation — the
  backend's `check_activation_readiness()` (§8.3) remains the sole source
  of truth for whether activation will actually succeed.
- Keep `constants.js`'s `CONTENT_MODES`/`SCRIPT_SOURCES`/`OUTPUT_MODES`
  `executable` flags in sync by hand with §8.2's `is_executable_*()`
  helpers — there is no shared source of truth between frontend and
  backend yet.
- Keep `VISUAL_STYLE_OPTIONS`/`IMAGE_STYLE_OPTIONS` in `constants.js`
  in sync with values that Agent 2's script prompts and Agent 4's
  storyboard prompt both recognize — both agents now receive these as
  free-form strings (Agent 2 via "Visual style:"/"Image style:" lines in
  blueprint/section/short-episode user messages; Agent 4 via "Global
  visual direction:"/"Global image style:" lines in the storyboard user
  message). Adding a new option here that neither agent's prompts
  explicitly document causes silent prompt mismatch.
- Research results open in a modal dialog (`ResearchDialog`) — do not
  reinline them below the textarea; that was an explicitly-fixed UX defect.

### 8.5 Research Ideas UX (Agent 1 V3.5c)

Agent 1's Concept step includes a real **Research Ideas** action for
`content_mode="single_story"` setup. The operator enters a rough channel
description, then clicks `✨ Research Ideas`; the frontend calls
`POST /api/agent1/research-ideas` and shows a rich recommendation panel
before the channel is saved or activated.

Backend ownership:

```text
app/agents/agent1_setup/routers/suggest.py      — `/api/agent1/research-ideas`
app/agents/agent1_setup/system_prompt.py        — `research_channel_ideas()` structured Claude prompt
app/schemas/research_ideas.py                   — request/response models
app/services/model_routing.py                   — `channel_research` Claude task key
```

The endpoint accepts `channel_description` (optional when `mode="explore"`),
`mode` (`"explore"` | `"validate"`, default `"validate"`), `content_mode`,
optional `target_languages`, and optional `target_platforms`.

`mode="explore"` — the operator has no channel idea yet; `channel_description`
may be empty. `research_channel_ideas()` injects a synthetic open-ended brief
so Claude proposes the best available niche opportunity from scratch.
`mode="validate"` — the operator has an idea and wants feedback;
`channel_description` is required and the endpoint returns HTTP 400 if empty.
The frontend sends `mode="explore"` for the **"✨ Research Ideas"** button and
`mode="validate"` for the **"✨ Validate Description"** button.

The endpoint calls Claude only through the shared `call_claude_structured()`
client pattern with task `channel_research`. It does **not** call YouTube,
TikTok, Instagram, Facebook, Reddit, credential-verification services, or any
scraping/tooling. If web-enabled Claude research tooling is unavailable, the
feature remains a structured AI estimate based only on the operator's
description (or the pipeline-constraint brief in explore mode) and the current
pipeline constraints.

Required response shape includes: recommended channel concept, why Claude
selected it, qualitative RPM potential, qualitative follower/subscriber
growth potential, platform suitability for YouTube/TikTok/Instagram/
Facebook, best script source (`reddit` or Claude Generated), recommended
output mode, visual style, image style, tone, target languages, target
platforms, suggested channel names, example video ideas, risks/difficulty,
a final recommendation summary, an `editable_config` object that maps
into the wizard's existing editable state, and a `references_used` array
of well-known subreddits, publications, or public resources Claude knows
from training data (empty when none apply — real web search is a future
phase, not wired yet).

The result is explicitly labeled **"AI market research estimate — not
verified platform analytics"**. Claude must not invent exact verified
numbers, exact RPM values, competitor analytics, audience sizes, or claims
that it checked live platforms. Qualitative labels (`low`/`medium`/`high`/
`very_high`) are allowed when paired with reasoning. `references_used`
entries must only be sources Claude is confident are real — it must not
fabricate URLs or source names.

Frontend behavior (two-path UX with progressive reveal):

- `BasicInfoSection.jsx` shows a description textarea and two action paths:
  **"✨ Research Ideas"** (enabled when description is empty — sends
  `mode="explore"`; no idea yet, Claude proposes one from scratch) and
  **"✨ Validate Description"** (enabled when description is non-empty —
  sends `mode="validate"`; operator has an idea and wants Claude to analyse
  it). Both paths call `POST /api/agent1/research-ideas` and open the same
  `ResearchDialog`.
- Research/Validate result opens in a **modal dialog** (`ResearchDialog`)
  overlaid on the step — not inline below the textarea.
- Editable fields (channel name, niche, tone) are hidden until the
  operator takes an action: closes the dialog (with or without applying),
  or clicks "Skip — I'll fill in details manually" (progressive reveal).
- `Use this recommendation` updates local React state only — channel name,
  description, niche, tone, script source, output mode, visual style, image
  style, languages, platforms, videos per week, and Reddit source rows when
  applicable — then closes the dialog and reveals the editable fields. It
  does not save the channel, activate the channel, create content, start
  Agent 2, or bypass operator review.
- Loading text is explicit: "Analyzing niche opportunities…", "Checking
  platform fit…", "Estimating monetization potential…", and "Preparing
  channel recommendation…".

Rules:

- Research Ideas is Agent 1 setup assistance only; it must never start
  script generation, discovery, credential verification, Telegram approval,
  or activation.
- Telegram validation remains mandatory later in the pipeline; Research
  Ideas does not approve or publish anything.
- Do not implement real platform API integrations, scraping, or credential
  verification under this feature.
- Do not wire `limited_series`, `ongoing_series`, `shorts_only`, or
  `ai_generated` execution into Agent 2/3/4/5 from this feature. Those
  values may be suggested into editable setup state, but activation/runtime
  executability remains governed by §8.2/§8.3 until future phases implement
  them.
- `references_used` is populated from Claude's training knowledge only —
  do not add `call_claude_with_tools()` web search under this section. A
  future phase may add real web citations; when it does, this section must
  be updated and the schema field's description updated to reflect that it
  may contain live-fetched URLs.
- Research result must open in the `ResearchDialog` modal — do not reinline
  it below the textarea (that was an explicitly-fixed UX defect).
- `mode="explore"` must never call an external API to discover ideas — Claude
  generates the opportunity from its training data using the synthetic brief
  in `research_channel_ideas()`. Do not wire real discovery or scraping into
  the explore path without explicitly scheduling it as a new phase and updating
  this section.
- Do not make `channel_description` required when `mode="explore"` — the
  entire point of explore mode is that the operator has nothing to describe yet.

### 8.6 Channel Configuration Snapshot Foundation (Phase Agent1-V3.6)

File:

```text
app/agents/agent1_setup/services/config_snapshot.py
```

Today every agent reads channel configuration live, via whatever
`channel.config`/`channel.languages`/`channel.voices`/`channel.sources`/
`channel.platforms`/`channel.publish_timings` happen to be at the moment
it runs — there is no stable, point-in-time record of what configuration
actually applied to a given content run. If an operator edits the channel
mid-pipeline, a later agent has no way to know whether it saw the same
configuration an earlier agent saw. This phase adds the **foundation
only** for fixing that: an immutable snapshot dict, captured once per
content run, carried on the `Content` row itself.

**Storage decision: a nullable JSONB column on `Content`
(`channel_config_snapshot`), not a new table.** `Content` already carries
a directly analogous nullable JSONB column for exactly this kind of
point-in-time captured data (`story_blueprint`, captured once at
blueprint-generation time and never mutated afterward) — reusing that
existing, proven pattern is lower-risk than introducing a new table with
its own model, relationship wiring, and migration surface for a value
that is always 1:1 with a single `Content` row and never queried
independently of it. A new table would only be justified if a snapshot
needed to be queried/joined across many content rows by its own fields,
or needed its own history (many snapshots per content) — neither applies
here: a content run has exactly one snapshot, captured exactly once.

`build_channel_config_snapshot(channel) -> dict` — a pure function over an
already-eager-loaded `Channel` (the same precondition §8.3's
`check_activation_readiness()` already documents: `config`/`languages`/
`voices`/`sources`/`platforms`/`publish_timings` must already be loaded).
No query of its own, no network/API call. Captures:

```text
channel_id, channel_config_id, content_mode, script_source, output_mode,
visual_style, image_style, languages (language + channel_name),
platforms (platform + language + verified + active — never
credentials_encrypted), videos_per_week, publish_timing_summary
(platform/language/timezone/days/hours), voices (language/provider/
tts_model/voice_id — never any ElevenLabs override field), source_summary
(source_type/source_value/language/trust_score), captured_at (UTC ISO
timestamp)
```

`ChannelConfig` is a one-to-one row keyed directly by `channel_id` (no
separate version column exists today) — `channel_config_id` is therefore
identical to `channel_id` when a config row exists, `None` when it does
not. This is documented as a known simplification rather than invented as
a fake version number; a future phase may add a real version column to
`ChannelConfig` if config history ever needs its own audit trail.

`validate_channel_config_snapshot(snapshot) -> list[ConfigSnapshotIssue]`
— deterministic structural validation only (every required top-level key
present; `channel_id` specifically non-empty). Returns an issue list
rather than raising, matching §8.2/§8.3's existing "collect every issue,
let the caller decide" convention. Never judges snapshot *content*
correctness (e.g. it does not re-derive what the snapshot "should" have
contained) — only that the shape a caller built is structurally complete.

`attach_snapshot_to_content(content, snapshot) -> None` — sets
`content.channel_config_snapshot` in memory only; no query, no commit.
**Immutability is enforced at this one point**: calling it a second time
on a content row that already has a snapshot raises `ValueError` rather
than overwriting — this is the only mechanism in this phase that actively
prevents a snapshot from being silently replaced.

Rules:

- **Not wired into any route, task, Celery job, or agent yet** — no code
  anywhere calls `build_channel_config_snapshot()` or
  `attach_snapshot_to_content()` outside this module's own smoke test. A
  future phase decides the actual generation-start hook (most likely
  inside Agent 2's `run_script_workflow()`, at the point parent `Content`
  moves `APPROVED` → `GENERATING_SCRIPTS`, but that decision is explicitly
  **not** made by this phase) and updates this section when it does.
- Do not call either function from Agent 2/3/4/5 until that future phase
  explicitly wires it in and documents the chosen hook here.
- Do not add a `channel_config_snapshots` table — the JSONB-column
  decision above is deliberate; revisit only if a real requirement for
  cross-content snapshot querying or multiple snapshots per content
  emerges.
- Never include `ChannelPlatform.credentials_encrypted` or any
  `ChannelVoice` override field in a snapshot — see CLAUDE.md §30; a
  snapshot must never become a second, less-protected copy of credential
  material.
- A snapshot, once attached, must never be mutated or re-attached — any
  future change to a content row's effective configuration belongs in a
  new content run, not a rewritten snapshot.

---

## 9. Agent 2 — Discovery, Scripts, and Standalone Short Planning

### 9.1 Agent 2 Responsibilities

Agent 2 owns:

- Story discovery.
- Deduplication.
- Story scoring.
- Telegram validation.
- Manual fallback story handling.
- Long-form script blueprinting.
- Section-by-section script generation.
- Script quality gate.
- Multilingual script generation.
- Standalone short episode planning and script creation.

Agent 2 must not:

- Generate audio.
- Generate images.
- Render videos.
- Decide publishing.
- Cut parent videos into shorts.

### 9.2 Agent 2 Flow

```mermaid
flowchart TD
    A[run_discovery] --> B[fetch_batch]
    B --> C[dedup check]
    C --> D[story gate scoring]
    D --> E[Content PENDING_APPROVAL]
    E --> F[send_for_validation]
    F --> G[Telegram APPROVE]
    G --> H[Content APPROVED]
    H --> I[run_agent2_scripts_for_content task]
    I --> J[run_script_workflow]
    J --> K[generate_story_blueprint]
    K --> L[generate_script_sections]
    L --> M[run_script_quality_gate]
    M --> N[persist validated source Script]
    N --> O[generate and validate multilingual scripts]
    O --> P[Content SCRIPTS_VALIDATED]
    P --> Q[run_shorts_planner]
    Q --> R[child Content rows]
    R --> S[child validated source short Script]
    S --> U[generate and validate child multilingual scripts]
    U --> T[Child SCRIPTS_VALIDATED]
```

### 9.3 Important Agent 2 Functions

#### `run_discovery`

File:

```text
app/agents/agent2_discovery/services/discovery.py
```

Input:

- Channel ID
- Database session
- Optional rejected stories list

Output:

- Parent `Content` row
- Story object
- Gate assessment

Responsibilities:

- Load channel and sources.
- Fetch a candidate story.
- Deduplicate.
- Score story.
- Persist parent content if accepted.
- Trigger manual fallback if discovery fails.

#### `fetch_batch`

File:

```text
app/agents/agent2_discovery/services/fetcher.py
```

Input:

- Source config
- Channel/niche context
- Rejected story exclusions

Output:

- Candidate `Story`

Rules:

- Must return direct story candidates when possible.
- Must accept rejected story exclusions.
- Must not block the whole pipeline on one duplicate candidate.
- Must not invent URLs.
- Must not validate business decisions by prompt alone.

Full source-material fetch (roadmap 4.1 / audit S-1, exec-2):

- **Problem:** discovery previously asked Claude for a `"200-500 word factual
  summary"` as `Story.body`. That summary became the *only* fact source for
  `generate_story_blueprint()`, every `generate_section()` call, the
  length-correction expansion path (`auto_correct_script()`), and Shorts
  grounding — a 9-minute documentary written from a paragraph, with every
  downstream truncation (`story.body[:4000]`/`[:8000]`) a no-op since the
  body was never that long to begin with.
- **Fix (no Reddit API key available — see below):** `fetch_batch()`'s
  `_SINGLE_STORY_SYSTEM_PROMPT` now instructs Claude to return the **full
  verbatim post text it actually retrieved via `web_search`, plus the top
  comments**, not a summary — explicit "do NOT condense, paraphrase, or
  write a factual summary" language, and an explicit instruction never to
  pad/invent text to reach a target length. `_SINGLE_STORY_REFORMAT_PROMPT`
  (the fallback pass when Claude returns prose instead of JSON) carries the
  same "reproduce it completely" instruction.
- **Why not a deterministic Reddit `.json` fetch** (the roadmap's original
  suggestion — Reddit's public per-post `.json` endpoint needs no OAuth/API
  key, just an HTTP GET): the operator does not have Reddit API access and
  Reddit increasingly rate-limits/blocks unauthenticated or datacenter-IP
  requests, so a Python-side deterministic fetch was judged unreliable for
  this deployment. Claude's `web_search` tool call was already the sole
  browsing mechanism (not a Reddit API call) — asking it to relay full text
  instead of a summary needed only a prompt change, no new fetch
  code/dependency. Trade-off, documented rather than hidden: this is less
  deterministic than a byte-exact HTTP fetch — Claude could still lightly
  condense despite the "verbatim" instruction — but it fully avoids scraping
  infrastructure this deployment cannot reliably run.
- **Shared ceiling:** `app/agents/agent2_discovery/services/story.py` defines
  `MAX_SOURCE_EXCERPT_CHARS = 60_000` — the single value every truncation
  point agrees on: `_story_from_dict()` (fetcher.py, defense-in-depth at
  Story-construction time), `Content.source_excerpt` persistence
  (`discovery.py`, `validation.py` — no migration needed, the column is
  already `Text`), and every Claude-call site that used to cap the body at
  4,000/6,000/8,000 chars (`generate_story_blueprint()`,
  `generate_section()`, `auto_correct_script()` in `system_prompt.py`, and
  `_apply_length_correction()` in `scripts.py`). `score_story_for_gate()`'s
  own `[:6000]` cap is deliberately untouched — gate scoring judges fit/
  quality, not narrative grounding, and does not need the full body.
- `call_claude_with_tools()`'s `max_tokens` for `story_research` raised from
  4096 to `8192` (this codebase's own established, already-proven output
  ceiling — matches `STORYBOARD_BATCH_MAX_TOKENS` in agent4 — chosen over an
  unverified higher value since a live API call to confirm one is not
  permitted). The reformat fallback's `max_tokens` and input-truncation cap
  were raised to match (`8192` / `MAX_SOURCE_EXCERPT_CHARS`, up from `2048`
  / `6000` chars) so a long real body surviving only in prose form is not
  lost before reformatting.
- `max_rounds` for the same `story_research` call is capped at `8`, down from
  `20` (roadmap 6.2 / audit C-4). Once this call only has to find and relay a
  story via `web_search` — no longer condense it into a summary — it does
  not need 20 tool-use round-trips to converge; `max_rounds` is a hard safety
  cap (`call_claude_with_tools()` raises `ValueError` if exceeded, caught by
  `fetch_batch()`'s own `except Exception: return []`), so this is a
  deliberate cost reduction, not a quality change.
- `models/content.py` required **no schema change/migration** —
  `Content.source_excerpt` was already an unbounded `Text` column; the fix is
  entirely prompt + truncation-constant changes.
- Story gate feasibility dimension (roadmap 4.5 / audit S-7): the old
  `stock_media_feasibility` score/floor is retired because this pipeline no
  longer searches Pexels/Unsplash/Pixabay stock media; visuals are generated.
  Agent 2 now asks for and gates on `image_generation_feasibility`: whether
  the story's key moments can be depicted as distinct concrete generated
  images. `score_story_assessment()` still aliases legacy cached
  `stock_media_feasibility` payloads into the new dimension for backward
  compatibility, but new Claude scoring schemas must require
  `image_generation_feasibility`.

#### `send_for_validation`

File:

```text
app/agents/agent2_discovery/services/validation.py
```

Input:

- Parent content
- Channel
- Assessment

Output:

- Telegram validation message

Rules:

- Telegram failures must log clearly.
- Markdown formatting failures may retry as plain text.
- Approval marks content `APPROVED`.
- Change flow before script generation must be handled explicitly; do not pretend script revision exists when no script exists.

#### `run_agent2_scripts_for_content`

File:

```text
app/scheduler/tasks.py
```

Input:

- Parent content ID

Output:

- Agent 2 script workflow task execution

Responsibilities:

- Load parent content.
- Guard that content is still `APPROVED`.
- Open and close the worker database session.
- Call Agent 2 `run_script_workflow`.
- Preserve task-level retry and failure logging.

#### `run_script_workflow`

File:

```text
app/agents/agent2_discovery/services/script_workflow.py
```

Input:

- Approved parent `Content`
- Database session

Output:

- Validated long scripts
- Child short content rows/scripts

Responsibilities:

- Move status to `GENERATING_SCRIPTS`.
- Generate story blueprint.
- Generate section scripts.
- Run quality gate.
- Persist validated source script.
- Generate and validate required multilingual scripts.
- Mark parent `SCRIPTS_VALIDATED` only after the complete required script set exists.
- Call `run_shorts_planner` after parent `SCRIPTS_VALIDATED`.

#### `generate_story_blueprint`

File:

```text
app/agents/agent2_discovery/system_prompt.py
```

Output fields:

- hook
- major_turns
- final_payoff
- comment_trigger
- midpoint_retention_trap
- suggested_section_count
- suggested_title

Rules:

- Use structured Claude call.
- Python validates required fields.
- Major turns drive section progression.
- Business logic remains in Python.

`midpoint_retention_trap` (roadmap 4.3 / audit S-3, §6): one concrete reveal
or counterintuitive fact, distinct from `final_payoff`, placed at roughly the
story's halfway point — the highest-leverage completion-rate lever current
research identifies, missing from the existing 25%/60% mini-hook cadence.
`generate_story_blueprint()` does not post-process or validate its content
(unlike `major_turns`'s ≥2-entry check) — an empty value is tolerated
fail-open by `generate_script_sections()` below (no constraint is injected).

#### `generate_script_sections`

File:

```text
app/agents/agent2_discovery/services/scripts.py
```

Responsibilities:

- Generate INTRO.
- Generate body sections one primary turn at a time.
- Generate OUTRO.
- Enforce TTS cleanup.
- Track coverage.
- Avoid collapsing multiple major turns into one section.
- Retry only when required.
- Preserve section progression.
- Inject the blueprint's `midpoint_retention_trap` into exactly one body
  section (roadmap 4.3 / audit S-3, §6).

Midpoint retention trap wiring:

- `_build_section_generation_context()` computes `midpoint_body_index =
  (max_body + 1) // 2` — a 1-based estimate of which body section lands
  nearest the story's halfway point, using `max_body` (the planned body-section
  count, itself derived from the blueprint's `suggested_section_count`) as
  the estimate. This is a best-effort estimate, not an exact recalculation
  against the final section count: `_should_stop_body_loop()` can still
  extend the real generated count past `max_body` (up to the `_MAX_BODY_SECTIONS`
  hard cap) when major turns remain uncovered.
- `_run_body_section_loop()` compares the current `body_index` against
  `context["midpoint_body_index"]`; only on that exact match does it pass
  `blueprint.get("midpoint_retention_trap")` through to
  `_generate_section_with_retry()` → `_call_section_generation()` →
  `generate_section()`. Every other section receives `None`.
- `generate_section()` injects it as a targeted "this section MUST deliver
  the blueprint's midpoint_retention_trap now" clause — additive to (not a
  replacement for) the full blueprint JSON already present in every section's
  user message, mirroring how `primary_required_turn` is both passively
  visible in the full blueprint and separately called out as an explicit
  MUST-clause for its one target section.
- INTRO and OUTRO never receive this constraint (`midpoint_retention_trap=None`
  in both `_generate_intro_section()` and `_generate_outro_section()`) — only
  the body-section loop computes and injects it.

Spoken-video delivery rules (roadmap 4.3 / audit S-3, §6): both
`_SECTION_GENERATION_SYSTEM_PROMPT` and `_SHORT_EPISODE_SYSTEM_PROMPT` (system_prompt.py)
now instruct: present tense for story events wherever the story allows it (vs.
past tense), direct address to the viewer ("you" / a rhetorical question)
at least once per section, mandatory contractions, and a "read-aloud test"
(rewrite any sentence that would not be said aloud to a friend). This is a
prompt-only change — no deterministic Python check enforces register (unlike
the existing TTS/word-count/hook checks), since spoken-register quality is a
subjective judgment call, not a pass/fail structural property.

Rules:

- One body section should primarily advance one major turn.
- Future turns may be foreshadowed, not resolved.
- TTS checks are deterministic.
- Major business decisions live in Python.
- Narrative completeness retry must not create infinite loops.

#### `run_script_quality_gate`

File:

```text
app/agents/agent2_discovery/services/scripts.py
```

Responsibilities:

- Run final deterministic cleanup.
- Run quality assessment.
- Run global narrative-coherence validation (`validate_script_globally`,
  Haiku) exactly once per quality-gate pass, on attempt 1 only.
- Skip expensive rewrite when issues are TTS-only and fixable in Python.
- Log cost estimates.

Rules:

- Do not call expensive rewrite for cheap deterministic issues.
- Clean before assessment and before final return.
- Log hash/trace when scripts move across stages.

Maximum-length gate (roadmap 3.8 / audit G-7):

- Before this phase, the quality gate only ever checked a *minimum* word
  count (`check_minimum_length()`) — nothing capped the upper end. A real
  production script reached 1,799 words against the documented 1,200-1,600
  word spec (§9.3's `_BASE_YOUTUBE_LONG_FORM_NATIVE`), producing a
  13.3-minute video instead of the intended ~9-10 minutes.
  `check_maximum_length()` (`app/services/script_checks.py`) closes this gap:
  MAJOR at 1,750 words (1,600 target + 150 tolerance) for
  `script_format="youtube_long"`, or 800 words (700 target + 100 tolerance)
  for any other format (matching `_BASE_SHORT_FORM_NATIVE`'s own 420-700
  word spec).
- Wired into `_collect_quality_gate_issues()` alongside `check_tts_compliance()`
  and `check_hook_quality()` — a MAJOR is converted to the same `HIGH`-severity
  rewrite-issue shape and folded into `all_issues`, so an over-length script
  goes through the exact same `rewrite_script_for_quality()` mechanism as any
  other quality-gate finding. No second rewrite path, no second retry counter.
- Applies to the source-language parent script only (`run_script_quality_gate()`'s
  own call, from `script_workflow.py`) — translated parent scripts have their
  own separate `length_parity` ratio check against the source
  (`_generate_validated_translated_parent_script()`, Phase 13.4), and child
  Shorts have their own separate `_MIN_SHORT_WORDS`/`_MAX_SHORT_WORDS` gate
  (see `run_shorts_planner` below) — neither is affected by this check.
- Logged via `QUALITY_GATE_BREAKDOWN`'s new `det_length_maj` field, alongside
  the pre-existing `det_tts_maj`/`det_hook_maj` counts.

Retention-structure telemetry (roadmap 6.3 / audit §6, §7):

- `check_retention_structure()` (`app/services/script_checks.py`) flags a
  chapter/section ending on a summary-pattern sentence (e.g. "So...",
  "Thus...", "In conclusion...") — dead air before the next section that
  research identifies as a completion-rate risk. It previously had no real
  caller (only reachable through the now-deleted `run_deterministic_checks()`
  orchestrator, §7.3).
- `_collect_quality_gate_issues()` now calls it directly and stores the
  result under `retention_det` — **always MINOR, always observability only**.
  Unlike `check_maximum_length()` above, its findings are never converted to
  a `HIGH`-severity rewrite issue and never counted in `det_majors`; they
  cannot trigger a rewrite or block the gate.
- Logged via `QUALITY_GATE_RETENTION_STRUCTURE` (one line per finding).

Global validation wiring (Phase 10A-0):

- `validate_script_globally()` used to run inside `generate_script_sections()`
  (before the quality gate ever saw the script) with its result only logged,
  never persisted, never acted on. It now runs once inside
  `run_script_quality_gate()` itself, via `_run_global_script_validation()`,
  which:
  - persists the result to the content's existing `ContentValidation` row —
    `script_validation_status` (`"PASSED"` | `"AUTO_CORRECTED"` |
    `"NEEDS_REVIEW"`) and `script_issues_log` (the raw issues list) — fields
    that existed on the model but were unused for this purpose before this
    phase;
  - returns its issues converted to the rewrite-issue shape
    (`severity="HIGH"`, `category="global_narrative"`), merged into the
    *same* `all_issues` list `_collect_quality_gate_issues()` already builds
    from Claude's quality-gate review and converted deterministic MAJORs —
    there is no second, parallel rewrite mechanism and no second retry
    counter; the existing `_MAX_QUALITY_REWRITES` cap bounds both issue
    sources together.
- `status_validation_status` mapping: `"PASSED"` when
  `validate_script_globally()` returns `status="PASS"`; `"AUTO_CORRECTED"`
  when it returns `status="NEEDS_FIX"` (since those issues are
  unconditionally forwarded into this same gate pass's rewrite mechanism,
  regardless of whether that specific rewrite attempt later succeeds);
  `"NEEDS_REVIEW"` if the Claude call itself fails (non-blocking — the gate
  continues with the script as-is, exactly as before this phase).
- Global-validation issues are folded into the rewrite issue list on attempt
  1 only — they are not re-added on subsequent attempts within the same
  gate pass, since the underlying global-coherence check is not re-run
  per attempt.
- A `ContentValidation` row is expected to already exist for the content by
  this point (created at discovery time, `run_discovery()`) — if missing,
  the result is logged and not persisted, but the gate does not fail.

#### `generate_multilingual_scripts`

File:

```text
app/agents/agent2_discovery/services/scripts.py
```

Responsibilities:

- Generate/adapt scripts into configured languages for parent and child content.
- Persist `Script` rows.
- Validate language outputs.
- Return the complete required validated script set to the caller.

Rules:

- Native adaptation, not literal translation.
- Maintain factual consistency.
- Do not silently shorten content across languages.
- Must not write `SCRIPTS_READY` or any terminal status.
- Must fail the script step rather than allowing `SCRIPTS_VALIDATED` when any
  required configured language is missing or unvalidated.

Existing-row reuse on retry (roadmap 4.4 / audit S-4, exec-7): a pre-existing
`Script` row for a required non-source language is reused unconditionally
only when `existing.validated` is already `True`. A row that exists but never
reached `validated=True` (e.g. left behind by a prior interrupted/failed run)
is **not** blindly force-marked valid via `_mark_script_validated()` — it goes
through the same `_generate_validated_translated_short_script()` /
`_generate_validated_translated_parent_script()` regeneration path a missing
language uses, then the result is written **into the existing row in place**
(same `Script.id`) rather than inserting a second row, since a second
`content_id`+`language`+`version=1` row would violate
`uq_script_content_language_version`. If regeneration fails, the existing
row is left as-is (still `validated=False`) and the language counts as
missing, failing the whole content exactly like a language with no row at
all — never silently accepted.

Parent source-script generation is a single shared service (roadmap 4.7 /
audit AR-1, §1): `script_workflow.generate_parent_source_script(content, db,
story=None, context=None)` owns the entire blueprint → sections → quality
gate → source-`Script` persistence sequence (including
`_persist_source_script()`, which computes `version = max(existing source
Script.version) + 1`, writes `validated=True`, estimates duration with the
calibrated estimator, and commits, plus `_merge_visual_intent_history()`).
Both `run_script_workflow()` (production/Celery) and
`test_pipeline/test_full_pipeline.py`'s STEP 3 (operator harness) call this
one function — neither reimplements any part of the sequence, so generation
parameters (visual/image style, audio tags, TTS block) and versioning cannot
diverge between harness reruns and Celery reruns. Multilingual generation,
the `SCRIPTS_VALIDATED` transition, and the shorts planner stay caller-owned
(the harness interleaves its own reuse checks and reporting between them).

Parent vs. child native-adaptation prompt selection (Phase 12.4):

- `generate_multilingual_scripts()` derives `content_kind` directly from
  `Content.is_short_episode` (`"parent_long_form"` or `"child_short"`) and
  passes it to `generate_native_script()` → `build_native_system_prompt()`.
  `content_kind` is the selector for which native-adaptation base prompt is
  used — it is independent of `ChannelConfig.script_format`, which is a
  channel-wide setting that does not vary per content row and must not be
  used to distinguish parent vs. child content.
- `content_kind="parent_long_form"` (default) preserves all pre-existing
  behavior unchanged: `script_format == "youtube_long"` selects
  `_BASE_YOUTUBE_LONG_FORM_NATIVE` (1200-1600 words, `[INTRO]`/`[SECTION N]`/
  `[OUTRO]` markers preserved); any other `script_format` selects
  `_BASE_SHORT_FORM_NATIVE` (420-700 words, sectioned short-form-platform
  format — distinct from a standalone child Short, see below).
- Long-form body section markers may include deterministic blueprint-turn titles
  (roadmap 4.6 / audit A-1 prerequisite): `assemble_script()` emits
  `[SECTION N: Title]` for body sections when a section carries `title`, derived
  from the primary `blueprint.major_turns` text. `[INTRO]` and `[OUTRO]` remain
  bare markers. Existing marker regexes intentionally tolerate the title suffix
  (`[SECTION N...]`) so completeness checks and translation section-count checks
  keep working while TTS/storyboard stages receive useful section context.
- `content_kind="child_short"` always selects the dedicated
  `_BASE_CHILD_SHORT_NATIVE` prompt, regardless of `script_format`. This
  prompt requires flat, unsectioned narration; forbids `[INTRO]`,
  `[SECTION N]`, `[OUTRO]`, or any bracketed structural marker; requires
  matching the source Short's approximate length; and requires preserving
  the source's cliffhanger/forward-tease intent and its minimum-necessary-context
  framing (the same standalone-clarity rules `_SHORT_EPISODE_SYSTEM_PROMPT`
  already enforces on the source-language Short, per §16/§9.4's
  `generate_short_episode_script`).
- Translated/adapted child Short scripts are validated by
  `_collect_translated_short_script_issues()` (section-marker presence,
  `check_tts_compliance()`, the shared `_MIN_SHORT_WORDS` calibrated floor,
  the shared `_MAX_SHORT_WORDS` hard cap, and a length-parity ratio against
  the source word count) and generated through
  `_generate_validated_translated_short_script()`, which retries up to
  `_MAX_SHORT_CORRECTION_ROUNDS` times with a corrective
  `override_instruction` on MAJOR findings — the same retry-and-log
  convention `_generate_validated_short_script()` already uses for the
  source-language Short script. On retry exhaustion the latest attempt is
  used non-blocking (logged `FAIL_USING_LATEST`), never silently dropped.
  This closes the gap where, before Phase 12.4, a translated child Short's
  output was persisted as `validated=True` with no length or
  section-marker check of any kind.
- Fixes the Phase 12.3-identified defect: before this phase, every child
  Short's multilingual adaptation used `_BASE_YOUTUBE_LONG_FORM_NATIVE` in
  real operation (no code path ever set `ChannelConfig.script_format` to
  anything but its `"youtube_long"` default), directly contradicting
  `Content.is_short_episode`'s flat-narration contract in §5.2.

Parent translated-script validation (Phase 13.4):

- Phase 12.4 closed the multilingual validation gap for child Shorts only.
  The parent branch of `generate_multilingual_scripts()` still called
  `generate_native_script()` directly and persisted its result as
  `validated=True` with **zero** deterministic check — confirmed by direct
  code reading, not assumed. Phase 13.4 closes this remaining gap with
  `_generate_validated_translated_parent_script()`, mirroring Phase 12.4's
  retry-and-validate pattern exactly, with its own separate retry budget
  (`_MAX_PARENT_TRANSLATION_CORRECTION_ROUNDS`, independent of
  `_MAX_SHORT_CORRECTION_ROUNDS`).
- `_collect_translated_parent_script_issues()` reuses `check_completeness()`
  and `check_tts_compliance()` from `app/services/script_checks.py` — the
  same functions already run for the source-language script
  (`_assemble_sections_with_diagnostics()`) — no structural-check logic is
  duplicated. `check_completeness()`
  alone covers required-marker presence, consecutive `[SECTION N]`
  numbering (catching most duplicated/malformed headers), non-empty section
  bodies, and terminal punctuation.
- Two source-comparison checks were added because no existing per-language
  check function takes the source script as a second input:
  - **`section_loss`** — the translation's `[SECTION N]` marker count must
    equal the source's. This catches a section silently dropped and the
    remainder renumbered consecutively, which `check_completeness()`'s
    purely-internal numbering check cannot detect on its own.
  - **`length_parity`** — translated word count must stay within
    `_PARENT_TRANSLATION_LENGTH_RATIO_MIN`-`_PARENT_TRANSLATION_LENGTH_RATIO_MAX`
    (0.6x-1.6x) of the source word count, checked immediately per-language
    during generation (distinct from the cross-language check below, which
    requires every language to already exist).
- `check_length_coherence()` (`script_checks.py`) was previously dead code:
  defined, with its own docstring claiming "the caller is
  `run_deterministic_checks()` which is invoked from
  `generate_multilingual_scripts()`," but never actually called from
  anywhere — confirmed by a repo-wide grep returning zero call sites before
  this phase. It is now wired into `generate_multilingual_scripts()`,
  called once after every language's `Script` row is persisted, exactly per
  its own documented placement constraint. It runs as an **observability-only
  diagnostic** (`MULTILINGUAL_LENGTH_COHERENCE_PASS`/`_FAIL`, logged, never
  retried) rather than a blocking gate — an outlier here may require
  touching an already-accepted sibling language to fix, which is out of
  scope for a single language's generation/retry pass.
- Logs: `PARENT_TRANSLATION_VALIDATION_PASS`,
  `PARENT_TRANSLATION_VALIDATION_RETRY`, `PARENT_TRANSLATION_VALIDATION_FAIL`
  (both the `FAIL_USING_LATEST` exhaustion case and the
  `reason=generation_error` case), `MULTILINGUAL_LENGTH_COHERENCE_PASS`/`_FAIL`.
- Does not touch Phase 12.4's child Short translation path
  (`_generate_validated_translated_short_script()`,
  `_collect_translated_short_script_issues()`) or Phase 13.2/13.3's child
  Short generation gates — confirmed unchanged.

### 9.4 Standalone Short Planning

#### `run_shorts_planner`

File:

```text
app/agents/agent2_discovery/services/scripts.py
```

Input:

- Parent content ID
- Channel
- Config
- Database session

Output:

- Child `Content` rows
- Child short `Script` rows

Current standalone short flow:

```text
Parent SCRIPTS_VALIDATED
→ shorts planner
→ child Content rows
→ child short scripts
→ child SCRIPTS_VALIDATED
```

Rules:

- Child short scripts are standalone.
- Child short scripts are not parent cuts.
- Child short source scripts and all required child multilingual scripts must
  be validated before the child row is marked `SCRIPTS_VALIDATED`.
- If child short rows already exist, do not create duplicates — and the
  check runs **before** the paid Claude plan call (roadmap 6.1 / audit S-8,
  C-2): a re-run against a parent whose Shorts already exist costs zero API
  calls, not one discarded planning call.
- Existing child rows in `SCRIPTS_VALIDATED` are picked up by normal Agent 3 audio pickup.
- No child with MAJOR deterministic script issues may be marked ready.

Short Quality Gate (Phase 13.2) and Parent/Child Overlap Detector (Phase 13.3):

- `_generate_validated_short_script()` (`scripts.py`) runs three gates per
  generation attempt, in order, sharing one `_MAX_SHORT_CORRECTION_ROUNDS`
  retry budget — no separate or expanded retry counter exists for either
  the overlap detector or the AI gate:
  1. **Structural** (`_collect_short_script_major_issues()`): calibrated
     `_MIN_SHORT_WORDS` floor (125 words, roadmap 3.6 / audit S-5), word cap,
     TTS compliance, hook opener, and — as of Phase 13.2 — section-marker
     presence (`_SHORT_TRANSLATION_MARKER_RE`, shared with the translated-Short
     check below). A structural MAJOR retries immediately; neither later
     gate is reached for that attempt.
  2. **Parent/child overlap** (`detect_parent_child_overlap()`, Phase 13.3):
     deterministic exact word-sequence reuse check between the child
     Short's narration and the parent long-form `voice_script`. Real
     production data showed some Shorts reused 21-24% of exact word
     sequences from the parent despite `_SHORT_EPISODE_SYSTEM_PROMPT`'s
     existing ORIGINALITY rule ("never lift a run of 6 or more consecutive
     words directly from it") — this is that same rule's deterministic
     Python-side enforcement (CLAUDE.md §15: business rules live in
     Python). Compares normalized (lowercased, punctuation-stripped) word
     tokens using `_OVERLAP_NGRAM_LENGTH`-word (6) sliding windows; any
     child-token position covered by an exact-match window against the
     parent is flagged, adjacent/overlapping matches are merged into one
     span, and `overlap_ratio = (total flagged child words) / (total child
     words)`. A ratio at or above `_OVERLAP_MAX_RATIO` (15% — comfortably
     below the 21-24% real-defect range, comfortably above what a few
     shared names/places could ever produce on their own) is one MAJOR
     `parent_child_overlap` issue, with up to `_OVERLAP_MAX_EXCERPTS` (3)
     concrete overlapping excerpts quoted directly in the issue description
     fed into `override_instruction`. An overlap MAJOR retries immediately;
     the AI quality gate is never reached for that attempt. If the parent
     `voice_script` is missing/empty, the check is skipped entirely
     (`PARENT_CHILD_OVERLAP_SKIPPED`, logged) — never crashes, never counts
     as a pass or a fail. Applies only to source-language Short generation
     against the parent long-form script — not to parent long-form
     generation itself, and not to Phase 12.4's multilingual child-Short
     translation/adaptation path (which compares a Short against its own
     source-language version in a different language, an unrelated
     comparison this detector does not apply to).
  3. **AI quality** (`_run_short_quality_gate()` →
     `assess_short_script_quality()`, `system_prompt.py`): a holistic,
     Haiku-judged retention review for flat short-form narration — hook
     strength in the first 1-2 sentences, clarity for a first-time viewer,
     emotional pull/curiosity gap, exactly one clear main reveal,
     cliffhanger intent preserved (skipped for the final part, which ends on
     a comment-trigger question instead), no over-recapping, no generic
     filler, TTS readability, and short-form retention pacing (re-hook every
     7-10 seconds). Only reached once a draft is structurally clean AND has
     passed the overlap detector — the same ordering `run_script_quality_gate()`
     already uses for parent scripts (deterministic checks before the
     holistic AI pass), extended one step further for Shorts.
- The Short quality prompt (`_SHORT_QUALITY_SYSTEM_PROMPT`) explicitly
  forbids requiring or suggesting `[INTRO]`/`[SECTION N]`/`[OUTRO]` markers
  or a long-form word arc — it judges flat narration on its own terms, not
  as a short documentary section. Deterministic code owns the hard duration
  band: `_MIN_SHORT_WORDS` (125) through `_MAX_SHORT_WORDS` (180 — recalibrated
  from 250 by roadmap 3.8 / audit G-7, since real Shorts measured ~120 wpm,
  not the previously-assumed 180 wpm / "3 words per second"), and both
  the source-language and translated/adapted Short validators emit MAJOR
  issues below the minimum or above the cap.
- On `NEEDS_REWRITE`, the AI gate's issues feed the same
  `override_instruction` mechanism the structural retry loop already uses —
  there is no separate rewrite-from-scratch call (unlike the parent gate's
  `rewrite_script_for_quality()`); the next attempt simply regenerates via
  `generate_short_episode_script()` with the issues appended.
- If the AI assessment call itself fails (malformed JSON, API error), the
  structurally-valid draft is accepted as-is and logged
  (`SHORT_AI_QUALITY_VALIDATION_FAIL reason=assessment_error`) — mirrors
  `run_script_quality_gate()`'s own fail-safe convention of never retrying
  against a failed assessment.
- On retry exhaustion, the latest attempt is used non-blocking, logged at
  WARNING — never silently dropped, never raises.
- Logs: `SHORT_AI_QUALITY_VALIDATION_PASS`, `SHORT_AI_QUALITY_VALIDATION_FAIL`
  (both the assessment-error and the NEEDS_REWRITE case).
- Post-audio assertion (roadmap 3.6 / audit S-5, exec-8): Agent 3 enforces
  `_MIN_SHORT_AUDIO_DURATION_MS = 61_000` immediately after child Short audio
  duration is known and before Whisper/persistence. A child language below
  the floor logs `CHILD_SHORT_AUDIO_TOO_SHORT`, rolls back that language, and
  is not counted as successful; if every language misses/fails, the child
  content becomes `FAILED`.
- Parent long-form quality validation (`run_script_quality_gate`,
  `assess_script_quality`, `rewrite_script_for_quality`) is untouched by this
  gate — it is a separate function, separate prompt, separate task key
  (`short_quality_check`, Haiku — see `model_routing.py`), reusing no part of
  the parent gate's rewrite mechanism.

Naming rule:

- Use product names such as `short_episode`, `standalone_short`, or `child_short`.
- Do not name functions, files, constants, classes, logs, or DB concepts after development phase labels such as `phase4`.
- Log messages may mention the current product concept, for example:
  - `STANDALONE_SHORTS_ALREADY_EXIST`
  - `AUDIO_PICKUP`
  - `CHILD_SHORT_AUDIO_START`
  - `SHORT_EPISODE_RENDER_DONE`
- Existing phase-labeled logs should be renamed when touched.

---

## 10. Agent 3 — Audio and Whisper

### 10.1 Agent 3 Responsibilities

Agent 3 owns:

- TTS generation.
- Audio file persistence.
- Duration measurement.
- Whisper transcription.
- AudioFile upsert.
- Audio pickup for any content with `SCRIPTS_VALIDATED`, including child short episodes.

Agent 3 must not:

- Generate parent short cuts.
- Generate parent short breakpoints.
- Generate parent rehooks/bridges under standalone-short architecture.
- Render video.
- Generate visual beats.

### 10.2 Agent 3 Flow

Parent:

```mermaid
flowchart TD
    A[Parent SCRIPTS_VALIDATED] --> B[run_agent3_audio_for_content]
    B --> C[run_audio_generation]
    C --> D[TTS]
    D --> E[save parent audio]
    E --> F[Whisper]
    F --> G[AudioFile upsert]
    G --> H[Parent AUDIO_DONE]
```

Child short:

```mermaid
flowchart TD
    A[Child SCRIPTS_VALIDATED] --> B[run_agent3_audio_for_content]
    B --> C[run_audio_generation]
    C --> D[short TTS with short pacing]
    D --> E[save child audio]
    E --> F[child Whisper]
    F --> G[child AudioFile upsert]
    G --> H[Child AUDIO_DONE]
```

### 10.3 Important Agent 3 Functions

#### `pickup_scripts_validated`

File:

```text
app/scheduler/tasks.py
```

Responsibilities:

- Find content rows in `SCRIPTS_VALIDATED`.
- Enqueue `run_agent3_audio_for_content`.

Rules:

- May pick parent and child content.
- Worker-side guards must prevent duplicate processing.
- It should not rely on parent/child timing assumptions.

#### `run_agent3_audio_for_content`

File:

```text
app/scheduler/tasks.py
```

Responsibilities:

- Run audio generation for one content ID.
- Mark that content `AUDIO_DONE` after successful AudioFile persistence.

Rules:

- Parent success must not release, flip, or enqueue child short audio.
- Child short audio is picked up from `SCRIPTS_VALIDATED` like any other content.
- Do not open unnecessary second sessions for critical orchestration if the current session has the needed state.

Temporary compatibility:

- `run_agent4_for_content` remains as a Celery task alias for old queued messages only.
- New code must call `run_agent3_audio_for_content`.

#### `ensure_child_short_audio_enqueued`

File:

```text
app/scheduler/tasks.py
```

Responsibilities:

- Temporary compatibility no-op for old imports or queued code paths.
- Return `0` and log that the parent-audio child gate has been removed.

Rules:

- Must not release, flip, or enqueue child short audio.
- Must not depend on parent `AUDIO_DONE`.
- New code must use `pickup_scripts_validated` for child audio pickup.

Deliberately kept during the roadmap 6.3 dead-code cleanup: this function and
`pickup_short_episodes_awaiting_parent` are listed in the audit's dead-code
table with the explicit caveat "delete once queues are confirmed drained" —
unlike every other item removed in that cleanup, deleting these two is an
operational judgment call (whether any already-enqueued Celery message from
before this architecture still references these task names) that cannot be
verified from source alone. Do not delete either without operator
confirmation that the relevant queues are drained.

#### `run_audio_generation`

File:

```text
app/agents/agent3_audio/services/audio.py
```

Responsibilities:

- Load content and validated scripts.
- Set status to `GENERATING_AUDIO`.
- Generate TTS per language.
- Save audio file.
- Run Whisper.
- Upsert `AudioFile`.
- Mark content `AUDIO_DONE`.

Rules:

- Parent content does not generate breakpoints, bookends, rehooks, bridges, or semantic short splits.
- Child content does not generate breakpoints, bookends, rehooks, or bridges.
- Child content uses its own script, audio, and Whisper.
- TTS skip-on-disk must still update/confirm `AudioFile` consistency.

Per-language hard assertions (roadmap 3.6/3.9, audit S-5/A-3, exec-8/exec-10)
— both run immediately after the step they gate, both roll back and
`continue` (skip that language, not counted toward `success_count`) rather
than persisting a known-bad `AudioFile` row:

- `_assert_short_audio_min_duration()` — child Short audio below
  `_MIN_SHORT_AUDIO_DURATION_MS` (61,000 ms) fails that language. Parent
  content is exempt (returns `True` unconditionally).
- `_assert_short_audio_has_transcript()` — child Short `transcribe()` result
  is empty (both OpenAI Whisper and the local `faster-whisper` fallback
  failed — see `agent3_audio.services.whisper.transcribe()`) fails that
  language. Parent content is exempt — a missing parent transcript is still
  tolerated exactly as before this phase, since the storyboard/timestamp
  pipeline already has its own proportional-fallback timing machinery. This
  is the audio-side half of a two-part fix; the render-side half is
  `agent5_render.services.video._collect_technical_blockers()`'s
  `empty_whisper_transcript_short` blocker (§11A.4) — each half is an
  independent safety net.

#### `generate_audio`

File:

```text
app/agents/agent3_audio/services/tts.py
```

Responsibilities:

- Prepare text.
- Select provider path.
- Generate audio bytes.
- Handle chunking and concatenation.

Rules:

- Provider-specific behavior must be behind a clear provider abstraction.
- Cartesia model defaults are two different things — do not conflate them
  (audit §10): the `channel_voices.tts_model` **column default** is `sonic-2`
  (what an old row with no explicit value resolves to), while the **setup-UI
  default** for newly configured voices is `sonic-3.5`
  (`app/ui/src/constants.js`). New channels therefore normally run
  `sonic-3.5`; `sonic-2` remains the legacy floor.
- Cartesia `sonic-2` uses the legacy request shape: `voice_id` plus
  `_experimental_voice_controls` with string speed labels and weighted emotion lists.
- Cartesia `sonic-3` and `sonic-3.5` use the generation-config request shape:
  `voice={"mode":"id","id":...}`, numeric `generation_config.speed`, and a
  single `generation_config.emotion` value. Unknown Cartesia model generations
  must fail clearly instead of silently reusing the wrong request format.
- Cartesia pronunciation dictionaries are configured per `ChannelVoice` with
  `cartesia_pronunciation_dict_id`; when unset, no pronunciation dictionary
  field is sent.
- Cartesia long-form scripts with `[INTRO]`, `[SECTION N]`, and `[OUTRO]`
  markers are sent as one TTS request per section so deterministic section
  delivery can vary emotion/intensity across the narrative arc.
- Multi-chunk TTS audio stitching uses ffmpeg re-encoding and inserts a short
  deterministic silence pad between chunks to avoid abrupt section-boundary joins.
- Section delivery is selected in Python from section metadata only: INTRO uses
  a restrained curious delivery; early body sections use tense buildup;
  reveal/climax-titled sections use scared/faster delivery, including common
  blueprint-turn variants such as "reveals" and "exposes"; OUTRO uses a slower
  somber delivery.
- Missing/unknown section metadata and child-short flat narration fall back to
  the configured channel-level emotion and speed profile; audio generation must
  not fail solely because section delivery cannot be inferred.
- ElevenLabs remains supported only as configured provider/legacy path.
- Voice, model, pronunciation dictionary, and fallback delivery choices come from `channel_voices`.

#### `ChannelVoice.cartesia_pronunciation_dict_id`

File:

```text
app/models/channel_voices.py
```

Rules:

- Nullable per-language Cartesia pronunciation dictionary id.
- Used only by Cartesia Sonic 3 / Sonic 3.5 request formatting.
- Allows unusual or invented words to be forced through a configured Cartesia
  pronunciation dictionary without hardcoding story-specific words in code.
- Existing `NULL` values mean omit the pronunciation dictionary field entirely.

#### `prepare_script_for_tts`

File:

```text
app/agents/agent3_audio/services/tts.py
```

Rules:

- Strip unsupported markers.
- Normalize TTS text.
- Add pacing markers deterministically; do not run AI pause-marker reviews.
- Reveal-pause insertion must require deterministic reveal-beat evidence; broad
  sentence starters such as "Then", "But", "The truth", or "It turned out" are
  insufficient by themselves.
- Per-section/per-chunk Haiku pause-marker review is retired (roadmap 5.2 /
  audit A-2, C-1); `prepare_script_for_tts()` must not call Claude.
- Tokenizers used by overlap, hook length, and TTS preparation must be
  Unicode-aware (`[^\W_]+`-style word classes) so accented translated text is
  not split into ASCII fragments. English phrase/opener/summary style checks
  remain source/English-scoped; do not treat them as multilingual validators.
- Short episodes may use different pacing caps.
- Never insert pauses that corrupt meaning.

#### `transcribe`

File:

```text
app/agents/agent3_audio/services/whisper.py
```

Rules:

- Use actual generated audio.
- Store word-level timestamps.
- Captions must derive from Whisper, not script text.
- Engine order (roadmap 6.7 / audit §8.6): OpenAI Whisper primary, local
  `faster-whisper` fallback — unless `settings.whisper_local_primary=True`
  (dev-mode flag, default `False`), which swaps the order so local
  transcription runs first and the hosted API is the fallback, zeroing out
  Whisper spend during iteration. Both orders end at the same empty-list
  fail-loud result when every engine fails (child Shorts are then blocked by
  `_assert_short_audio_has_transcript()` / the render-side
  `empty_whisper_transcript_short` blocker).

### 10.4 Phase 11 — Agent 3 TTS Pipeline Architecture Contract

This section is the ownership and execution-flow contract established by
Phase 11 (sub-phases 11.1-11.6). It exists so that a future phase touching
either Agent 2 (script wording) or Agent 3 (TTS preparation/delivery) can
tell, without re-reading every function body, exactly which agent owns a
given decision.

#### Ownership boundary

**Agent 2 owns** (`app/agents/agent2_discovery/`):

- Story wording and sentence construction.
- Sentence-rhythm generation (alternating short/long sentences, Phase 11.1).
- Narration content — facts, structure, section boundaries, what the
  narrator says.

**Agent 3 owns** (`app/agents/agent3_audio/`):

- TTS preparation (`prepare_script_for_tts()` — normalization, sentence
  length limiting).
- Deterministic pacing (`_apply_pacing_markers()`).
- Reveal-pause evidence logic (`_is_reveal_beat_sentence()`, Phase 11.5).
- No Claude/Haiku pause-marker review inside TTS prep; roadmap 5.2 retired the
  former `_review_pause_marker_placement()` pass because it ran once per
  section/chunk for little quality gain.
- Section-unit delivery selection (`_select_section_delivery()`, Phase 11.4).
- Provider request-payload construction (`_build_cartesia_tts_kwargs()` and
  the ElevenLabs equivalent in `generate_audio()`, Phase 11.3).
- Pronunciation dictionaries (`ChannelVoice.cartesia_pronunciation_dict_id`).
- Audio generation and chunk stitching (`_concat_mp3_chunks()`, Phase 11.6).

Agent 3 never rewrites narration words, reorders sentences, or changes
story content — the Phase 11 mechanism that touches text is the deterministic
pacing gate, constrained to punctuation/pause-marker placement only and
enforced in Python. The former Haiku review safety check was removed with the
review itself in roadmap 5.2.

**Future phases must not move ownership across this boundary without
updating this section first.** If a future phase needs Agent 3 to influence
narration wording (or Agent 2 to influence audio delivery), that is an
ownership-boundary change and requires an explicit CLAUDE.md update in the
same change set, per §32 — not a silent expansion of what either agent's
existing functions already do.

#### Real Agent 3 TTS runtime flow (Cartesia path, the default)

```text
voice_script (full narrator text, may carry [INTRO]/[SECTION N]/[OUTRO] markers)
↓
_split_script_into_section_units()   — one unit per section; flat narration
                                        (and all child shorts) is a single unit  (11.4)
↓ (per unit)
_select_section_delivery()           — deterministic emotion + speed_profile from
                                        section type/title; channel-level fallback
                                        when section metadata is missing            (11.4)
↓
prepare_script_for_tts(unit_text, tone=delivery.emotion, ...):
    1. strip section markers
    2. normalize_voice_script()            — punctuation/whitespace cleanup
    3. sentence-length limiter             — split sentences over the TTS word cap
    4. _apply_pacing_markers()             — deterministic reveal-pause insertion,
                                              gated by _is_reveal_beat_sentence()
                                              evidence scoring, not opener-matching
                                              alone                                  (11.5)
↓
_build_cartesia_tts_kwargs()         — legacy request shape for sonic-2, or the
                                        generation_config shape (numeric speed,
                                        single emotion, optional pronunciation
                                        dictionary id) for sonic-3/sonic-3.5         (11.3)
↓
client.tts.bytes()                   — Cartesia provider call, one per section unit
↓
WAV → MP3 per unit
↓ (after all units)
_concat_mp3_chunks()                 — ffmpeg re-encode + a short deterministic
                                        silence pad between chunks, to avoid abrupt
                                        section-boundary joins                       (11.6)
↓
final MP3 audio bytes
```

The ElevenLabs path (`provider="elevenlabs"`) differs: it chunks by character
limit (`_chunk_script_at_sections()`), not section units, so Phase 11.4's
per-section delivery selection and Phase 11.3's Cartesia request-shape
branching do not apply to it. It still runs every chunk through
`prepare_script_for_tts()` (so Phase 11.5's reveal-gated deterministic
pacing applies identically), uses ElevenLabs's own
native text-conditioning (`previous_text`/`next_text`) for cross-chunk
continuity instead of a silence pad, and still passes through
`_concat_mp3_chunks()` (Phase 11.6) for final concatenation when more than
one chunk exists.

#### Phase 11 Deliverables

Real implementation status, verified directly against the code in this
repository as of this section's last update (not copied from an earlier,
non-existent audit — see the Phase 11 close-out report,
`code_report/phase_11_closeout_documentation.md`, for how this was verified):

```text
✓ 11.1  Sentence rhythm improvements (Agent 2)
✓ 11.2  Deterministic-only TTS prep; Haiku pause-marker reviewer retired by roadmap 5.2 (Agent 3)
✓ 11.3  Cartesia Sonic 3 / Sonic 3.5 migration (Agent 3)
✓ 11.4  Section emotion/intensity variation (Agent 3)
✓ 11.5  Reveal-beat deterministic pause logic (Agent 3)
✓ 11.6  Chunk-boundary stitching mitigation (Agent 3)
```

Status: **COMPLETE** at the code level. The Phase 11.3 migration
(`alembic/versions/003_add_cartesia_pronunciation_dict_id.py`) has been
applied to the local dev database — `channel_voices.cartesia_pronunciation_dict_id`
exists. (This was previously tracked here as an open operational item; it
was surfaced by a real `test_full_pipeline.py --confirm` run failing with
`UndefinedColumn`, then resolved by running `alembic upgrade head`.)

#### Future Phase Safety

Phases after 11 are expected to stay inside their own layer:

- A future prompt-focused phase modifies prompts only.
- A future validation-focused phase modifies validation only.
- A future visuals-focused phase modifies Agent 4 visuals only.

None of those should change the Agent 3 ownership boundary established
above without updating this section first. If a future phase's scope
already has a number assigned by the time it starts, name it explicitly
here rather than leaving this list to go stale — but do not pre-assign
numbers or scope to phases that have not been defined yet.

---

## 11. Agent 4 — Visual Planning and Media Generation

### 11.1 Agent 4 Responsibilities

Agent 4 owns:

- Parent visual pass.
- Storyboard generation.
- Storyboard validation.
- Timestamp mapping.
- Parent `__visual__` `VideoSection` rows.
- Flux prompt generation.
- Flux image/media generation.
- Media cache/reuse.
- Standalone child-short visual remapping.
- Visual quality diagnostics.
- Media asset validation (existence, integrity, remote-path/sentinel
  checks — post-generation, blocking; persistence round-trip —
  observability only). See §11.4 `validate_visual_media_assets`.

Agent 4 must not:

- Generate audio.
- Recreate scripts.
- Render videos.
- Persist `VideoRender` rows.
- Cut parent videos into shorts.
- Use network assets during Remotion rendering.
- Call Agent 5 or import any `app.agents.agent5_render` module.

Agent 4 is triggered independently of Agent 5 (Phase 4D-C): `pickup_audio_done`
dispatches `run_agent4_visual_generation_for_content`, which loads its own
`Content`/`Channel`/`ChannelConfig`/`Script`/`AudioFile` rows and runs the
flows below. Agent 5 only discovers the resulting `VideoSection` rows later,
through its own independent `pickup_visual_ready` pickup.

### 11.2 Parent Agent 4 Visual Flow

```mermaid
flowchart TD
    A[Parent AUDIO_DONE] --> B[pickup_audio_done]
    B --> C[run_agent4_visual_generation_for_content]
    C --> C2[Parent GENERATING_VISUALS]
    C2 --> D[split_into_beats]
    D --> E[generate_storyboard_batch]
    E --> F[map beats to Whisper timestamps]
    F --> G[generate_all_beat_images]
    G --> H[save __visual__ VideoSection rows]
    H --> I[Parent PARENT_VISUALS_DONE]
```

### 11.3 Child Short Agent 4 Visual Flow

```mermaid
flowchart TD
    A[Child AUDIO_DONE] --> B[pickup_audio_done]
    B --> C[run_agent4_visual_generation_for_content]
    C --> C2[Child GENERATING_VISUALS]
    C2 --> D[check parent __visual__ rows]
    D -->|missing| E[defer back to Child AUDIO_DONE]
    D -->|exists| F[remap_beats_for_short]
    F --> G[reuse parent images or generate new]
    G --> H[map to child Whisper timestamps]
    H --> H2[apply Short visual hold cap]
    H2 --> I[save child VideoSection rows]
    I --> J[Child CHILD_SHORT_VISUALS_DONE]
```

### 11.4 Important Agent 4 Functions

#### `split_into_beats`

File:

```text
app/agents/agent4_visuals/subagents/storyboard.py
```

Responsibilities:

- Split long script into storyboard segments.
- Call storyboard generation per segment.
- Merge batches.
- Map beats to timestamps.
- Log estimate accuracy and cost.

Rules:

- Track estimated vs actual beats.
- Log token/cost diagnostics.
- Avoid unnecessary full-storyboard retries.
- Use deterministic validation when possible.
- Generation-time diversity constraints (roadmap 2.4 / audit G-5.1): the
  cross-segment ledger tracks cumulative `environment` counts plus recent
  `motif` values. After each batch, `_summarize_batch_for_continuity()` injects
  hard `FORBIDDEN environments for the next segment:` when any environment is
  above 35% of generated beats, and hard `FORBIDDEN motifs for the next
  segment:` when a motif appears more than 2 times in the recent 10-beat
  window. The next `generate_storyboard_batch()` user message carries those
  lines in `Previous segment context`; the storyboard prompt tells Claude to
  treat them as binding unless narration makes a value unavoidable. Emitted
  log marker: `STORYBOARD_DIVERSITY_CONSTRAINTS`.

#### `generate_storyboard_batch`

File:

```text
app/agents/agent4_visuals/system_prompt.py
```

Responsibilities:

- Ask Claude for structured visual beats.
- Return schema-validated storyboard batch.

**Storyboard prompt inputs (`PROMPT_VERSION` 4.1):**
`generate_storyboard_batch()` accepts optional `visual_style`, `image_style`,
and `visual_bible_context` arguments. `split_into_beats()` threads those from
the parent visual path and from the validation retry path, so both the first
storyboard pass and a MAJOR-finding retry see the same continuity context.

When visual style fields are non-empty, `_build_message()` prepends:

```
Global visual direction: <visual_style or "documentary">
Global image style: <image_style or "photorealistic">
```

When a visual bible exists, `_build_message()` also prepends:

```
Visual continuity bible (compact JSON):
{...compact JSON...}
```

The compact JSON is produced by
`cinematic_prompts.compact_visual_bible_for_storyboard()`. It keeps
story summary, style/config summary, character/location/motif continuity,
first-15 rules, and forbidden generic shots, but intentionally omits raw
`camera_language` and `lighting_rules` arrays. The storyboard system prompt
instructs Claude to write character/location continuity into the native
`flux_prompt` it returns, without copying JSON field names, camera-rule lists,
lighting-rule lists, or negative-prompt/Avoid text into the positive Flux
prompt. It also treats generated `FORBIDDEN environments...` and
`FORBIDDEN motifs...` lines in previous-segment context as hard diversity
constraints for the current batch.

**Final prompt validation (`PROMPT_VERSION` 4.1 / roadmap 2.2):** after
`apply_cinematic_prompts_to_beats()` enriches the storyboard and before
`apply_first15_enhancement_and_validation()`,
`visual_orchestrator._check_final_prompt_issues()` reruns the prompt-only
validator subset against the enriched `flux_prompt` values. Because first15
enhancement can also append to `flux_prompt`, the same gate runs again after
first15 and immediately before any fal.ai call. The subset is check 4
(`forbidden_flux_word`), checks 12-16 (`subject_presence`,
`environment_presence`, `low_information_prompt`, `flux_prompt_exact_duplicate`,
`flux_prompt_near_duplicate`), and check 19 (`ai_text_rendering_requested`).
MINOR findings are warning-only; any MAJOR finding fails that parent visual pass
or skips that child-short language before Flux generation. Logs use
`FINAL_PROMPT_VALIDATION_*`.

Rules:

- Use `call_claude_structured()`.
- Use forced tool output.
- Keep schema strict.
- Storyboard prompt must produce physical, camera-pointable visuals.
- Prompt must not rely on vague mood words.
- Prompt must support visual continuity and identity consistency.
- Do not hardcode visual style choices inside the prompt — the operator's
  `ChannelConfig.visual_style`/`image_style` values are the authoritative
  source and are injected per-call, not baked into the system prompt.

Retry behavior (two independent, bounded retry classes — never combined into
one retry path):

- **Truncation retry** — when `output_tokens >= STORYBOARD_BATCH_MAX_TOKENS`,
  retries once with a reduced `target_beat_count` to free token budget. Logs
  `STORYBOARD_RETRY_COST`. Raises if the retry is truncated again.
- **Shape retry** — when the response is structurally malformed (a required
  key missing, or present with the wrong type — e.g. `beats` returned as a
  string instead of a list on an otherwise complete, non-truncated response;
  a real model quirk observed in production) — retries the same segment once
  with the same `target_beat_count`, no reduction, since this is not a
  token-budget problem. Before treating a wrong-typed value as a failure,
  `_check_shape()` calls `_coerce_string_to_expected_type()`, which tries
  three recovery shapes in order on a non-`str`-typed field (`beats`,
  `global_notes`) that came back as a string: (1) the string is the JSON
  value verbatim; (2) the JSON value is wrapped in a markdown code fence
  (```` ```json ... ``` ````) — stripped before parsing; (3) a valid JSON
  value is followed by trailing non-JSON content (e.g. model commentary
  after the array) — recovered via `json.JSONDecoder.raw_decode`, which
  parses only the leading value; any discarded trailing content is logged as
  `STORYBOARD_SHAPE_COERCED_TRAILING_DATA`. If any of the three recovers the
  expected type, the value is substituted in place, logged as
  `STORYBOARD_SHAPE_COERCED`, and the response is treated as valid — no
  retry needed. This is deterministic re-parsing of a value Claude already
  produced, not blind trust: the coerced value still has to pass every later
  beat-level enum/type check in `_build_beat_section()`. If none of the three
  recoveries succeed, the real reason (the `JSONDecodeError` detail, or
  "parsed but wrong type") is logged as `STORYBOARD_SHAPE_COERCION_FAILED`
  before falling through to the existing failure path unchanged — this
  diagnostic exists specifically because the original coercion attempt
  swallowed the parse error with no record of why it failed, making a real
  production failure undiagnosable from the log alone. Logs
  `STORYBOARD_SHAPE_INVALID` (the actual malformed value, truncated to 200
  chars, every time the shape check fails on a value that could not be
  coerced) and `STORYBOARD_SHAPE_RETRY` before retrying. Raises (fail-loud,
  aborts the entire storyboard via the caller in `storyboard.py`) if the
  retry is also malformed and not coercible — there is no third attempt.

Quote-escaping prompt rule (`PROMPT_VERSION` 3.1 → 3.2): a real, recurring
production failure traced the "beats returned as a string" quirk above to a
specific cause — when the narration segment itself contains quoted dialogue
or a written note, the model sometimes embeds a literal, unescaped `"`
character from that quote inside a field value while manually stringifying
the `beats` array, breaking the JSON at that exact point. This is consistent,
not random (repeated calls on the same segment content failed at
nearly-identical character offsets), so it cannot be safely auto-repaired
after the fact — a heuristic re-escaping pass risks silently corrupting beat
content, which conflicts with the "no silent fallbacks" / "no unvalidated AI
output" rules in §15/§25. The fix is preventive, in `_STORYBOARD_SYSTEM_PROMPT`'s
hard rules: (1) no field value may contain a literal quotation mark character
— quoted narration content must be described or paraphrased without the
surrounding quote marks; (2) `beats` must always be a native JSON array, never
a JSON-encoded string. The existing hint exact-copy rule
(start_hint/end_hint "copied EXACTLY from the segment text") carries an
explicit documented exception for quotation marks specifically, since
matching already strips punctuation (`_normalize_phrase`/`_normalize_word` in
`storyboard.py`) and dropping a quote character from a hint does not affect
timestamp alignment.

Removed fields (Phase 6D-1, `STORYBOARD_SCHEMA_VERSION` 6.0 → 6.1):

- `why_this_visual` and `story_progression_role` were removed from
  `_BEAT_SCHEMA`, `_STORYBOARD_BATCH_SCHEMA`, and their corresponding
  per-beat instructions (`_STORYBOARD_SYSTEM_PROMPT` step 2b, step 13, and
  strict rule 4) — Claude no longer generates either field.
- Rationale: Phase 6B-0/6B-1 (`code_report/phase6b0_storyboard_cost_trace.md`,
  `code_report/phase6b1_storyboard_quality_proof.md`) found zero downstream
  consumers for either field across generation, persistence, validation, and
  rendering — both were generated and paid for on every beat, then silently
  discarded by `_build_beat_section()`. Phase 6D-1
  (`code_report/phase6d1_reasoning_scaffolding_ab_proof.md`) then ran a live
  A/B proof (real Claude calls, current schema vs. the same schema minus
  these two fields, same segment, same model) to directly test the one open
  question those phases left unresolved — whether generating the two fields
  acts as reasoning scaffolding that improves `visual_intent`/`flux_prompt`
  quality even though the fields themselves are discarded. Result: no
  measurable regression on visual_intent specificity, flux_prompt
  specificity, validator findings (which *decreased*), or image subject
  fidelity, against a real ~17.6% lower storyboard output payload on the
  tested segment.
- This is reported as single-trial evidence, not a statistically exhaustive
  proof (see the Phase 6D-1 report's confidence caveats) — if a future real
  production run surfaces a quality regression this removal did not predict,
  treat it as new evidence to act on, not as a reason to silently revert
  without updating this section.
- `_check_shape()` only validates the four top-level `storyboard_batch`
  keys (`storyboard_status`, `overall_style`, `beats`, `global_notes`), never
  per-beat field completeness — removing two `_BEAT_SCHEMA` properties
  required no change to that function, to `_build_beat_section()` (which
  already read every beat field via `.get()` with a default, never assumed
  required-key presence), or to `storyboard_validator.py` (confirmed, again,
  to reference neither field anywhere).

**`storyboard_status`/`overall_style`/`global_notes` made optional, defaulted
in place (live-canary fix, same dead-schema-weight pattern as the Phase 6D-1
removal above):** a real `--confirm` run's parent visual pass hit
`STORYBOARD_SHAPE_INVALID segment=[INTRO] key=storyboard_status
issue=missing present_keys=['beats']` on both the initial attempt and the one
shape-retry, aborting storyboard generation entirely and failing the whole
content item — despite the response carrying a complete, valid `beats`
array. Grepping confirmed the same zero-downstream-consumer pattern Phase
6D-1 found: `storyboard_status` is never read past this shape check,
`global_notes` is never read anywhere, and `overall_style` only feeds one
cost/diagnostic log line in `storyboard.py`. `_STORYBOARD_BATCH_SCHEMA`'s
`required` list is now `["beats"]` only; `_check_shape()` still strictly
requires and type-checks (with the existing string-coercion fallback)
**only** `beats` — the one field every downstream consumer
(`_build_beat_section()`, `validate_storyboard()`, Flux generation) actually
reads. A missing or wrong-typed auxiliary field is defaulted in place
(`""`/`""`/`[]`) with a logged `STORYBOARD_SHAPE_AUX_FIELD_MISSING`/
`STORYBOARD_SHAPE_AUX_FIELD_WRONG_TYPE` warning rather than raising — this is
a logged fallback on genuinely inert fields, not a silent one, and does not
loosen validation on `beats` itself (still fatal + retried if missing or
wrong-typed, exactly as before). Runtime proof:
`tests/test_storyboard_batch_auxiliary_field_defaults.py`.

#### `map_storyboard_beats_to_timestamps`

File:

```text
app/agents/agent4_visuals/subagents/storyboard.py
```

Responsibilities:

- Align storyboard beats to Whisper transcript.
- Produce timed visual sections.

Rules:

- Use actual Whisper timings.
- Allow fallback only when explicitly intended.
- Log fallback rate.

Hint-search proximity window (frozen-frame guard):

- Every beat carries an expected start position: the last real anchor's
  timestamp plus the `suggested_duration_sec` of each beat since it
  (re-synced to the real anchor on every successful match, so estimation
  error never accumulates).
- A start/end hint match is accepted only within a bounded window past that
  expected position: `_HINT_SEARCH_WINDOW_MS` (60 s) for full/5-token
  candidates, `_HINT_SHORT_PREFIX_WINDOW_MS` (15 s) for candidates of
  `_HINT_SHORT_PREFIX_MAX_TOKENS` (3) tokens or fewer. The tight window
  applies by candidate LENGTH, not cascade position — a hint whose full form
  is only 3 tokens is exactly as ambiguous as a 3-token prefix.
- Rationale (real defect, run `9500c231…` beat 50): narration reused the
  phrase "long before the …"; one beat's hint fell back to its 3-token
  prefix and matched the next occurrence of that phrase 285 s downstream —
  the unbounded forward search let one beat hold a single image for 4.7
  minutes and desynced every later beat. A match far beyond the expected
  position is worse than no match: an unmatched beat degrades locally
  (adjacent-anchor fallback), a mismatched one corrupts the whole remaining
  timeline.
- A rejected-but-existing distant match is logged as
  `HINT_MATCH_REJECTED_OUT_OF_WINDOW` (phrase, match_ms, expected_ms,
  window sizes) so the guard's firing is visible in production logs; the
  beat then falls back exactly like an unmatched beat.
- Runtime proof: `tests/test_hint_search_proximity_window.py` reconstructs
  the failure shape with a real synthetic transcript through the real
  mapping chain (nothing internal stubbed; this path calls no external API).

Anchor span sanity + proportional interpolation (second frozen-frame guard):

- After the matching loop, `_demote_out_of_span_anchors()` walks trusted
  anchors left to right (starting from a virtual anchor at 0 ms): a candidate
  anchor whose gap from the last trusted anchor exceeds
  `max(_SPAN_SANITY_FACTOR (4) × Σ suggested_duration of the covering beats,
  _SPAN_SANITY_MIN_MS (20 s))` is an anchor error — a wrong match can land
  INSIDE the proximity window (a duplicate phrase 30–50 s ahead) — and is
  demoted to fallback (`ANCHOR_SPAN_SANITY_DROPPED`, logged with gap/expected/
  threshold). Demotion runs before the fallback-rate stats (demoted beats are
  counted honestly) and before script_text extraction (a wrong match's text
  never ships). The 4× factor is deliberately generous: real speech runs
  ~1.3-1.4× slower than suggested durations, so it fires on genuine anchor
  errors, not ordinary drift.
- `_resolve_boundaries()` interpolates unmatched beats proportionally
  (weighted by `suggested_duration_sec`) between the surrounding trusted
  anchors — leading run from 0 ms to the first anchor, trailing run from the
  last anchor to `duration_ms`, full-duration tiling when no anchor exists.
  History: proportional interpolation existed here before (`_fill_gaps`), was
  removed in Phase 10A-0, and "inherit the previous anchor" became the only
  fallback — that inheritance let the last unmatched beat before the next
  anchor absorb the whole gap as a frozen frame while the beats between were
  crammed into intensity-floor slots (run `9500c231…`, beats 51–104). The
  reintroduction is deliberate; do not revert to anchor inheritance.
- Runtime proof: `tests/test_boundary_span_sanity.py` (in-window wrong anchor
  demoted while a later coverable anchor is kept exact; unmatched runs tile
  gaps evenly; weighted interpolation; no beat — terminal included — may
  freeze beyond the sanity threshold in the regression fixtures).

Visual hold cap (third frozen-frame guard — the last-line guarantee):

- Even if a bad anchor slips past BOTH guards above, no non-terminal parent
  beat may hold one image longer than `settings.parent_visual_max_hold_ms`
  (default 9000 ms). Applied in `split_into_beats()` after timestamp mapping
  via `_apply_visual_hold_cap()` — the same shared core the child Short path
  uses at its tighter `settings.short_visual_max_hold_ms` (6000 ms) ceiling —
  by shortening the over-cap beat and advancing the next existing beat. It
  never creates synthetic beats and never touches audio/subtitle timing; a
  terminal beat with no later beat to absorb the remainder is exempt.
- The cap is deliberately applied in `split_into_beats()` (parent path) and
  `remap_beats_for_short()` (child path), never inside the shared
  `map_storyboard_beats_to_timestamps()` — the two paths need different
  ceilings.
- Logs: `PARENT_VISUAL_HOLD_CAP_APPLIED` (parent) /
  `SHORT_VISUAL_HOLD_CAP_APPLIED` (child), with cap, capped-beat count,
  terminal exemption, and before/after max hold.
- Runtime proof: `tests/test_parent_visual_hold_cap.py` (shared-core unit
  checks plus the real `split_into_beats` chain — segment split, hint
  hardening, mapping with the guards above, cap — with only the paid
  `generate_storyboard_batch` Claude call stubbed).

#### `_run_parent_visuals` — stale-visuals guard (audit V-6b)

File:

```text
app/agents/agent4_visuals/services/visual_orchestrator.py
```

The shared `__visual__` `VideoSection` rows are keyed only by `content_id` —
nothing ties them to the source-language narration they were built from.
Before this guard, `_run_parent_visuals()` reused any existing `__visual__`
rows unconditionally; if the parent's source script was regenerated after
the visual pass already ran (operator `--force-scripts`, or any retry that
produces a materially different script), the OLD beats — hints, timings,
`flux_prompt` subject matter, all describing narration that no longer
exists — were silently reused against the NEW script.

- `_source_script_hash(content, scripts_by_lang)` returns the SHA-256 hex
  digest of `scripts_by_lang[content.source_language].voice_script`, or
  `None` when that script is unavailable (fail-open — a guard must never
  force an unwarranted regeneration off missing data).
- `_tag_beats_with_script_hash(beats, script_hash)` stamps the hash onto
  every beat as `source_script_sha256`, persisted the same way every other
  shared field is (`_beat_extras()` → `generation_prompt` JSON). Called
  once, before the FIRST `_save_shared_beats()` of a (re)generation pass —
  it survives to later saves of the same content because beats are mutated
  in place, not recreated.
- `_check_shared_beats_staleness(content_id, shared_beats, current_hash)`
  classifies loaded `__visual__` beats into three outcomes:
  - `"fresh"` — stored hash matches `current_hash` (or `current_hash` is
    `None`, i.e. nothing is comparable) → reuse exactly as before.
  - `"stale"` — stored hash present and differs → `_run_parent_visuals()`
    discards `shared_beats` entirely and calls `_run_visual_pass()` fresh
    (passing `script_hash=current_hash` so the new beats carry the new
    fingerprint), logged as `PARENT_VISUALS_STALE_SCRIPT_HASH`.
  - `"backfill"` — beats predate this guard (no stored hash at all) →
    stamped with `current_hash` and re-saved unchanged, `logged as
    STALE_VISUALS_CHECK_BACKFILL` — no regeneration is forced for
    pre-existing content; the next run has a baseline to compare against.
- Runtime proof: `tests/test_stale_visuals_guard.py` — unit tests for the
  three helpers, plus an integration test driving the real
  `_run_parent_visuals()` against a fake in-memory `VideoSection` table
  (real ORM instances, real `_beat_extras()`/`_save_video_sections()`/
  `load_video_sections()` (shared loader, roadmap 6.5) JSON round trip — only `_run_visual_pass()`,
  the paid Claude/fal.ai boundary, is stubbed) proving all three outcomes,
  including that the backfill save/reload round trip is real.

#### `validate_storyboard`

File:

```text
app/agents/agent4_visuals/subagents/storyboard_validator.py
```

Responsibilities:

- Run deterministic, Python-only checks on a merged beat list. No Claude
  calls, no I/O.
- Return every issue found (`MAJOR` and `MINOR`), letting the caller decide
  what to do with them.

There is exactly one implementation, called through the shared
`_collect_storyboard_issues()` / `_check_storyboard_issues()` helpers in
`app/agents/agent4_visuals/services/visual_orchestrator.py` — used by both
the parent storyboard path and the child short remap path. Neither path
forks or re-implements the validator. Parent MAJOR remediation is
segment-level: storyboard beats carry `storyboard_batch_label` provenance from
`split_into_beats()` through `_merge_batches()` and timestamp mapping, then
`_run_storyboard_validation()` retries only offending batch labels by passing
`retry_segment_constraints` plus `existing_beats` back into `split_into_beats()`;
unaffected batches are reused and the merged storyboard is remapped once. If
provenance is missing, the code logs `STORYBOARD_SEGMENT_RETRY_FALLBACK` and
falls back to the older full-storyboard retry behavior.

Checks (current full inventory):

| # | Check | Severity |
|---|---|---|
| 1 | `cover_frame_dark_contrast` — beat 0 must not use `color_grade="dark_contrast"` | MAJOR |
| 2 | *(retired — subtitles-only rendering: text cards no longer exist)* | — |
| 3 | *(retired — subtitles-only rendering: text cards no longer exist)* | — |
| 4 | `forbidden_flux_word` — `flux_prompt` must not contain a forbidden mood word | MAJOR |
| 5 | `environment_over_saturation` — no `environment` value may exceed 35% of all beats | MINOR |
| 6 | `consecutive_same_environment` — no 3 consecutive beats may share one `environment` | MINOR |
| 7 | *(retired — subtitles-only rendering: text cards no longer exist)* | — |
| 8 | `low_intensity_run` — no 3 consecutive beats may all be `beat_intensity="low"` | MINOR |
| 9 | `motif_repetition_in_window` — no `motif` may repeat more than 2 times in any 10-beat sliding window | MINOR |
| 10 | `near_duplicate_beat` — two beats within 2 positions sharing `environment`, `motif`, and `effect` simultaneously | MINOR |
| 11 | `ai_slideshow_risk` — 5+ consecutive beats sharing one value for `environment`, `motif`, or `effect` | MINOR |
| 12 | `subject_presence` — `flux_prompt` has fewer than 2 concrete-subject words after removing style/filler/technical boilerplate | MINOR |
| 13 | `environment_presence` — `flux_prompt` contains none of its declared `environment`'s expected keywords | MINOR |
| 14 | `low_information_prompt` — `flux_prompt` is under 4 words, or ≥50% generic filler words | MINOR |
| 15 | `flux_prompt_exact_duplicate` — `flux_prompt` is character-identical to an earlier beat's in the same storyboard | MINOR |
| 16 | `flux_prompt_near_duplicate` — `flux_prompt`'s word-token set overlaps ≥80% (Jaccard) with an earlier beat's | MINOR |
| 17 | `reuse_clustering` — 3+ consecutive beats reuse the identical `media_url` | MINOR |
| 18 | `excessive_reuse_ratio` — >60% of `flux_generated` child Short beats already have a non-empty `media_url` at validation time, exceeding the parent-reuse budget | MAJOR |
| 19 | `ai_text_rendering_requested` — `flux_generated` `flux_prompt` contains a quoted phrase or a "the text reads"/"sign that says"-style instruction asking the image model to render readable text (Phase 14.7) | MAJOR |
| 20 | `dark_contrast_unlit_prompt` — a beat uses `color_grade="dark_contrast"` while its `flux_prompt` names no bright light source (`has_bright_lighting_evidence()`); defense-in-depth only — `_build_beat_section()` already downgrades such beats to `desaturated` in place (`DARK_CONTRAST_GRADE_DOWNGRADED`), so this fires only on beats that bypassed beat building (audit G-6) | MINOR |
| 21 | `document_saturation` — document-ish beats (`motif="document"` or `visual_type="document"`) exceed 15% globally or more than 2 in any 10-beat window (roadmap 2.3 / audit G-2) | MAJOR |
| 22 | `visual_monotony` — aggregate retry trigger when `consecutive_same_environment` findings exceed 10, `ai_slideshow_risk` findings exceed 2, or `document_saturation` fires (roadmap 2.5 / audit G-5.2) | MAJOR |

Checks 12–16 validate `flux_prompt` *text quality* (subject/environment/
information clarity, exact/near duplication) — distinct from check 4, which
only catches an explicit forbidden mood word. Checks 17–18 validate *image
reuse patterns* (Phase 4E-E / roadmap 3.3) — only ever observable on the child remap path:
parent beats never have a `media_url` set at the point `validate_storyboard()`
runs (Flux hasn't generated yet — see the Ordering rule under
`remap_beats_for_short` above), so these two checks are naturally always
zero/LOW for parents with no child-specific branch required. Check 19
(Phase 14.7, §11.6) is a defense-in-depth complement to Phase 14.7's
Python-side prompt sanitization — it fires only on a beat the sanitization's
keyword detector missed, not on the normal sanitized happy path. Check 21
(roadmap 2.3 / audit G-2) is a MAJOR document-saturation guard: it treats
`motif="document"` and `visual_type="document"` as document-ish, then applies
both a global >15% cap and a local >2-per-10-beat-window cap. Check 22
(roadmap 2.5 / audit G-5.2) preserves the individual repetition findings as
MINOR diagnostics, then emits one aggregate MAJOR `visual_monotony` retry
reason when `consecutive_same_environment` findings exceed 10,
`ai_slideshow_risk` findings exceed 2, or `document_saturation` fires. All
checks live in the same function; none is a second validator.

Rules:

- MAJOR issues never hard-block the pipeline directly — the caller decides
  what to do (parent: retry once via full storyboard regeneration, then
  proceed regardless; child: no regeneration primitive, logs and proceeds
  immediately).
- MINOR issues are always logged at WARNING and never trigger a retry. Three
  of them — checks 10, 15, 16 (`near_duplicate_beat`,
  `flux_prompt_exact_duplicate`, `flux_prompt_near_duplicate`) — are the
  exception: `visual_orchestrator._repair_duplicate_prompts()` (the single
  call site for `storyboard_validator.repair_duplicate_flux_prompts()`,
  shared by the parent fresh-generation path, the parent flux-incomplete
  recovery path, and the child remap path) runs immediately before every
  Flux call and deterministically appends a composition-slot variation
  (camera distance + angle, drawn from a fixed 24-combination rotation —
  never random, never an AI call) to exactly the beat_order each check
  flags. Two beats with byte-identical `flux_prompt` text hash to the SAME
  cache file within one content's cache directory (audit G-5.3) — this
  closes that waste without spending a retry on a MINOR finding. Only
  beats still pending Flux generation (`media_url` empty) are ever
  rewritten; a beat whose media is already resolved elsewhere (a
  deliberate parent-image reuse, or an already-generated beat from a prior
  partial run) is never touched, by either check's flagging convention.
  Runtime proof: `tests/test_duplicate_prompt_repair.py`.
- Every `validate_storyboard()` call logs `VISUAL_REPEAT_RATE`,
  `AI_SLIDESHOW_RISK`, `FLUX_PROMPT_QUALITY`, `FLUX_DUPLICATE_RATE`,
  `CHILD_SHORT_REUSE_RATE`, and `CHILD_SHORT_REUSE_CLUSTERING` (all always,
  even when zero) as summary diagnostics — see §29.
- Do not add a second validator module for child-only, parent-only, or
  Flux-prompt-only checks — extend this function and its check list instead.
- Checks 12–18 are deterministic keyword/length/overlap/run-length
  heuristics — no Claude or fal.ai call. Checks 12–16 will have false
  positives/negatives on vocabulary outside their keyword lists; that is an
  accepted tradeoff for staying deterministic, not a defect to silently
  "fix" by adding an AI call.
- No retry or regeneration exists for checks 17–18 (or any child-path
  finding) — a finding is observability only. See the Remediation
  Classification note under `remap_beats_for_short` history in
  `code_report/phase4e_e_child_remap_validator.md` if/when retry or
  regeneration is ever scoped as a real phase.

#### `validate_visual_media_assets` — the single media validator (roadmap 6.4 / audit V-7, AR-2)

File:

```text
app/agents/agent4_visuals/services/media_validation.py
```

Two media validators used to exist with overlapping responsibility:
`storyboard_validator.validate_media_assets()` (observability-only,
per-language, ran on in-memory `beats` during generation via
`visual_orchestrator._check_media_assets()`) and this function
(blocking, whole-content, DB reload, ran once after `run_visual_generation()`
completes). Two validators for one concern was drift waiting to happen — the
blocking one is now the single source of truth; the other, and its wrapper
`_check_media_assets()`, are deleted.

Responsibilities:

- Load persisted `VideoSection` rows for a content (optionally filtered to
  one language) and run deterministic existence/integrity checks: missing
  path, remote URL, the legacy `__text_card__` sentinel, unsafe path
  (outside the media root/run root), missing/zero-byte/unreadable/
  unsupported-extension local file, and `PIL`-unreadable image — all
  `BLOCKING` (they fail the content, setting `Content.status = "FAILED"`).
- Optional `beats_by_lang: dict[str, list[dict]] | None` parameter — when
  the caller (`run_visual_generation_for_content()`) passes
  `result["beats_by_lang"]` from `run_visual_generation()`, this also runs
  the **persistence round-trip check** (folded in from the deleted
  `_check_media_assets()`): each beat's `media_url`/`media_type` is compared
  against what a fresh DB reload actually returns, per `(language,
  section_order)`. Findings — `persistence_row_missing`,
  `persistence_media_url_mismatch`, `persistence_media_type_mismatch` — are
  always appended to `warnings`, **never** `blocking_issues`, preserving the
  exact non-blocking severity the check had before the merge. Omitting
  `beats_by_lang` (every caller before this parameter existed) skips the
  round-trip check entirely — fully backward compatible.

Rules:

- This is the only remaining media validator — do not reintroduce a second
  one for a "faster" or "per-language" variant of the same concern; extend
  this function and its check list instead.
- Round-trip findings must stay in `warnings`, never `blocking_issues` —
  they are a regression guard for a future `_build_beat_section()`-style
  bug, not a business-rule gate; promoting them to blocking would be an
  unannounced behavior change past what roadmap 6.4 authorized.
- Called once, after `run_visual_generation()` returns
  (`run_visual_generation_for_content()`), not per-language during
  generation — the persistence round-trip requirement ("cannot check a
  round-trip before the row is saved") is satisfied because every
  language's `_save_video_sections()` + `db.commit()` has already completed
  by the time `run_visual_generation()` returns.

#### `_beat_extras` / `save_beat_review_metadata` — slimmed `generation_prompt` (roadmap 6.5 / audit AR-3)

Files:

```text
app/agents/agent4_visuals/services/visual_orchestrator.py   (_beat_extras)
app/agents/agent4_visuals/services/visual_review.py          (save_beat_review_metadata)
```

`generation_prompt` (the DB-persisted JSON blob per `VideoSection` row) used
to carry ~19 review-only fields alongside the fields the pipeline actually
consumes — first15 diagnostics (`first15_validation_status`, `first15_issues`,
`first15_strength_tags`, `first15_enhanced`, `first15_validation_summary`,
`is_first_15_seconds`), cinematic continuity metadata (`continuity_tags`,
`visual_bible_refs`, `location`, `character`), prompt-quality telemetry
(`prompt_quality_warnings`), and per-beat descriptive fields
(`negative_prompt`, `shot_type`, `subject`, `action`, `emotion`, `camera`,
`lighting`, `composition`). Nothing in the live pipeline reads any of these
back from a persisted row — only the local HTML review page
(`visual_review.py`) did — yet they were re-serialized on every
delete-then-insert, for every language, bloating the DB row for a
local-debug-only purpose.

- `_beat_extras()` (`visual_orchestrator.py`) now only serializes the fields
  something downstream actually consumes: `visual_intent`, `visual_type`,
  `visual_category`, `environment`, `motif`, `transition_to_next`,
  `media_url`, `media_type`, `media_strategy`, `text_card_style`,
  `source_script_sha256` (the last one load-bearing for the stale-visuals
  guard, §11.4 above). `overlay_text`/`overlay_position` are not persisted at
  all — the shared loader forces `""`/`"none"` on every load
  (subtitles-only rendering), so writing them was two dead keys per row.
- The 19 review-only fields instead go to a run-folder JSON file —
  `save_beat_review_metadata(content_id, language, beats)`
  (`visual_review.py`) writes `{media_path}/runs/{content_id}/visuals/beat_review_metadata.json`,
  keyed by language then a list of `{section_order, ...fields present on the
  beat...}` entries. Called once per language alongside every
  `_save_video_sections()` call (`_run_parent_visuals()`'s per-language
  loop, `_save_shared_beats()` for the `__visual__` row, and
  `_run_child_short_visuals()`'s per-language loop) — same call sites, same
  cadence, just a second, cheaper write instead of a bigger DB blob.
- `generate_visual_review_html()` reads this file
  (`_load_review_metadata_by_key()`, indexed by `(language, section_order)`)
  and merges it with the (now-slimmer) `generation_prompt` extras — the
  run-folder value takes priority, `generation_prompt` remains the fallback
  so **rows persisted before this change** (which still carry these fields
  in `generation_prompt`) keep rendering correctly in the review page with
  no migration needed.

Rules:

- Do not add review-only fields back to `_beat_extras()`/`generation_prompt`
  — extend `_REVIEW_METADATA_FIELDS` in `visual_review.py` instead.
- `save_beat_review_metadata()` must be called for every language
  `_save_video_sections()` is called for, at the same call site — a
  language saved to the DB but not to the review-metadata file silently
  loses its review diagnostics (not a pipeline failure, but a review-page
  gap).
- The merge priority (run-folder metadata over `generation_prompt` extras)
  must not be reversed — new content should always show the current,
  smaller-blob source of truth first.

#### `generate_all_beat_images`

File:

```text
app/agents/agent4_visuals/services/flux_generator.py
```

Responsibilities:

- Generate or reuse Flux images.
- Save local cache images.
- Attach local `media_url`.
- Fill hard-failed beats from neighbouring images
  (`fill_failed_beats_from_neighbors()`).

Rules:

- `media_url` must always be local, usually `cache/...`.
- Never pass HTTP URLs to Remotion props.
- Cache reuse must not create obvious visual repetition.
- Subtitles-only rendering (audit G-0/G-8): text cards are removed. Every
  beat is a Flux-generated image. On a hard generation failure the beat gets
  one environment-safe-prompt retry with a fresh cache key
  (`BEAT_IMAGE_HARD_RETRY`), then `fill_failed_beats_from_neighbors()` reuses
  the nearest neighbouring beat's image (`BEAT_IMAGE_NEIGHBOR_REUSED`) —
  never the retired `__text_card__` sentinel, never a text card. If NO beat
  generated at all, media_urls stay empty and Agent 5's missing-media
  blocker stops the render (fail loud, never a black or text-only frame).
- If cache reuse is controlled by prompt hash, log enough to debug repeated frames.

#### `remap_beats_for_short`

File:

```text
app/agents/agent4_visuals/subagents/storyboard.py
```

Responsibilities:

- Load parent visual beats.
- Ask Claude to map short narration phrases to parent beats.
- Decide reuse vs. new-image-needed per beat (does **not** call Flux itself —
  see Ordering rule below).
- Map beats to child Whisper timestamps.
- Apply the child Short visual hold cap.
- Return child short beats.

Rules:

- Use child short voice script.
- Use child short audio and Whisper.
- Use parent visual beats only as a visual source pool.
- Reuse parent images only when match score threshold passes.
- Threshold is enforced in Python, not prompt.
- Child Shorts have a hard parent-reuse budget of 60% (roadmap 3.3 / audit G-4.2): after thresholding, `remap_beats_for_short()` demotes the lowest-scoring reused beats to `media_url=""` until reuse is at or below 60%, forcing those beats into new portrait generation via `generate_pending_beat_images()`. Log marker: `CHILD_SHORT_REUSE_BUDGET_APPLIED`.
- The `short_storyboard_remap` schema includes `new_image_prompt` (roadmap 3.4 / audit V-3). For assignments with `match_score < 70`, it must be a complete portrait-safe Flux prompt for a new child Short image. `remap_beats_for_short()` uses that prompt for child-new beats and fails the remap with `CHILD_REMAP_NEW_PROMPT_MISSING` if it is empty, rather than inheriting a rejected parent prompt or falling back to raw narration. Reused beats may keep the parent prompt; high-score candidates forced into new generation by non-reusable parent media or the 60% reuse budget use `new_image_prompt` when supplied, otherwise a deterministic physical fallback prompt.
- Child remap MAJOR remediation is deterministic and child-only (roadmap 3.5 / audit G-4.3): after `validate_storyboard()` finds child MAJORs, `_run_child_short_visuals()` calls `remediate_child_major_storyboard_issues()` before logging-and-proceeding. The primitive only repairs beat-level prompt MAJORs (`forbidden_flux_word`, `ai_text_rendering_requested`) by stripping forbidden/readable-text language and regenerating that beat's `flux_prompt` from `visual_intent`, environment, and motif. It then re-runs `validate_storyboard()`; only remaining MAJORs are logged as proceed-anyway diagnostics. No Claude, Flux, or internal remap retry is invoked. Log markers: `CHILD_STORYBOARD_MAJOR_REMEDIATED`, `CHILD_STORYBOARD_MAJOR_REMEDIATION_DONE`.
- Log reuse stats.
- Child Short visual sections use `settings.short_visual_max_hold_ms` (default
  6000 ms) as a maximum non-terminal image hold target after timestamp mapping.
  If a non-terminal beat exceeds the cap, Agent 4 shortens that beat and
  advances the next existing visual beat earlier to cover the remaining
  narration span. It never creates fake beats, never changes child audio,
  and never changes Whisper/subtitle timing. A terminal beat with no later
  visual beat may remain above the cap, and the cap application is logged as
  `SHORT_VISUAL_HOLD_CAP_APPLIED`. Parent long-form beats have their own,
  looser hold cap (`settings.parent_visual_max_hold_ms`, default 9000 ms)
  applied in `split_into_beats()` — see the "Visual hold cap" rules under
  `map_storyboard_beats_to_timestamps` above; both paths share one
  implementation (`_apply_visual_hold_cap`), differing only in ceiling and
  log prefix.

Alignment-hint rule (Phase 6A-1):

- `map_storyboard_beats_to_timestamps()` aligns every beat — parent and
  child alike — exclusively via `beat["start_hint"]`/`beat["end_hint"]`. It
  has no fallback alignment path; a beat with empty hints always falls back
  to proportional timing.
- Child remap beats must carry `start_hint`/`end_hint` derived from
  `narration_phrase` before the beat list is passed to
  `map_storyboard_beats_to_timestamps()`. `remap_beats_for_short()` derives
  them with `_derive_child_alignment_hints()` — first/last 6-10 verbatim
  words of `narration_phrase`, word-sliced only (no rewriting, no
  punctuation invented, overlap allowed for short/medium phrases) — mirroring
  the parent storyboard schema's own `start_hint`/`end_hint` convention
  (`system_prompt.py`, "exact first/last 6-10 verbatim words").
- An assignment with a missing/empty `narration_phrase` is never given an
  invented hint (never derived from `visual_intent`, `flux_prompt`, or any
  parent visual metadata). It is logged (`CHILD_REMAP_HINT_MISSING`,
  including content_id/assignment index/long_beat_order) and tolerated in
  isolation. If missing-phrase assignments exceed
  `_CHILD_REMAP_HINT_MISSING_FAIL_RATIO` (30%) of one remap response, the
  whole call fails loud and returns `[]` (the same convention as the
  truncation/no-assignments failure paths below) rather than persisting a
  storyboard built mostly from un-alignable beats.
- This fixes the root cause traced in
  `code_report/phase6a0_child_timestamp_mapping_trace.md`: child beats
  previously had no `start_hint`/`end_hint` at all, so every child short
  hit 100% proportional fallback, and the boundary resolver's
  fallback-handling let one beat (typically the last) absorb all leftover
  duration — the frozen-image symptom. See
  `code_report/phase6a1_child_timestamp_hint_fix.md` for the runtime proof.
  The boundary-resolver behavior itself (`_resolve_boundaries()`/
  `_cleanup_micro_beats()`) was deliberately left unchanged in this phase —
  this fix addresses the data-flow gap that caused 100% fallback in the
  first place, not the resolver's handling of fallback in general.

Ordering rule (Phase 4E-E):

- `remap_beats_for_short()` does **not** call `generate_beat_image()` itself.
  A beat below the reuse threshold is returned with `media_url=""`
  (pending) instead. `generate_pending_beat_images()` (below) fills in
  pending beats' `media_url` afterward.
- The caller (`_run_child_short_visuals()` in `visual_orchestrator.py`) runs
  `_check_storyboard_issues()` — the same shared `validate_storyboard()` gate
  the parent path uses — on the result **before** calling
  `generate_pending_beat_images()`. This mirrors the parent path's order
  exactly: `_run_visual_pass()` runs `_run_storyboard_validation()` before
  `generate_all_beat_images()`.
- `_build_beat_section()` (the per-beat dict builder used by
  `map_storyboard_beats_to_timestamps()`, shared by both the parent
  storyboard path and this function) propagates `media_url`/`media_type`
  through unchanged. This matters only for the child path — the parent
  never has a `media_url` set at this point — but dropping the field here
  would silently erase every reuse-vs-pending decision this function makes.

Retry behavior:

- The Haiku `short_storyboard_remap` call uses
  `call_claude_structured_with_usage()` (not the usage-discarding
  `call_claude_structured()`) specifically so truncation can be detected:
  if the call returns no `assignments` and `output_tokens >= max_tokens`
  (`_SHORT_REMAP_MAX_TOKENS = 4096`), the forced tool-use input came back
  empty — the same truncation failure shape as `generate_storyboard_batch()`
  — and the call is retried once, unmodified. A real production failure
  (parent storyboard with many beats + a long short narration needing many
  narration-phrase assignments) hit this exact truncation at the old
  `max_tokens=1024` on every short for an affected parent. If the retry
  also truncates, `remap_beats_for_short()` logs and returns `[]` as before
  (fail-loud — the caller defers/fails the child, it does not invent beats).
  An `assignments` list that comes back empty **without** hitting the token
  cap is a different, non-truncation case and is not retried.

Preferred log names when touched:

```text
SHORT_EPISODE_REUSE_STATS
SHORT_EPISODE_RENDER
SHORT_EPISODE_RENDER_DONE
```

Avoid phase-labeled names in new code.

#### `generate_pending_beat_images`

File:

```text
app/agents/agent4_visuals/subagents/storyboard.py
```

Responsibilities (roadmap 3.1 / audit V-2, G-4.2 — portrait generation for
Shorts):

- Generate a **portrait** (`1080x1920`, `_SHORT_IMAGE_WIDTH`/
  `_SHORT_IMAGE_HEIGHT`) Flux image for **every** beat in the child Short,
  not only beats `remap_beats_for_short()` left pending. Two groups exist at
  the point this function runs:
  - "pending" beats (`media_url == ""`) — below the reuse match-score
    threshold, or the parent media was missing/unreusable.
  - "reused" beats — at/above the threshold, `media_url` still pointing
    directly at the *parent's* landscape (`1920x1080`) cache file.
  Both groups are regenerated fresh at portrait size using each beat's own
  `flux_prompt` (already the parent's prompt for a reused beat, inherited by
  `remap_beats_for_short()`), replacing whatever `media_url` the beat
  carried on entry. A Short must never render a landscape image cropped
  into a vertical frame.
- Every regenerated image — including a "reused" beat's portrait version —
  is cached under **this child's own** `cache/{child_content_id}/` directory,
  never the parent's, since a portrait render is a new artifact and not the
  reused parent file.
- Fill any hard-failed beat from the nearest neighbouring beat's image
  (`fill_failed_beats_from_neighbors()`) — subtitles-only rendering: the
  retired `__text_card__` sentinel is never produced.

Rules:

- Must run **after** `_check_storyboard_issues()`, never before — this is
  the entire point of the Phase 4E-E ordering alignment. `validate_storyboard()`
  still observes the pre-generation reuse-vs-pending split (checks 17/18 —
  `reuse_clustering`/`excessive_reuse_ratio`) exactly as before; this
  function's subsequent portrait regeneration of "reused" beats does not
  change what the validator saw.
- Every beat goes through the same `generate_beat_image_with_routing()`
  model-tier decision as the parent path (see §11.5) — conservative/Schnell
  by default — passing `width=1080, height=1920`.
- `image_router.build_cache_key_material()` disambiguates portrait from the
  parent's landscape cache by size: for the Schnell tier, a `size=` prefix
  is added to the cache-key material whenever the requested size is not the
  1920x1080 default, so a portrait generation can never collide with (or
  wrongly reuse) a landscape cache entry for the identical prompt text, and
  every pre-existing landscape cache entry keeps its original key
  unchanged.
- Costs one Schnell call (~$0.003/image) per beat that was previously
  "free" via reuse — an accepted, explicit cost/quality tradeoff (roadmap
  3.1), not an oversight.
- Is not a retry or regeneration mechanism for validator findings: it
  regenerates exactly the beats this phase defines (every beat, once),
  regardless of what `validate_storyboard()` found. A MAJOR/MINOR validator
  finding on a child beat does not change which beats this function
  generates.

### 11.5 Image Model Routing (Phase 14.6)

File:

```text
app/agents/agent4_visuals/services/image_router.py
```

Owns the *decision* of which image source/model a beat should use (reuse /
stock / Schnell / Dev / Pro-family) and the *payload normalization* for
whichever fal.ai Flux endpoint is selected. It does not call fal.ai —
`flux_generator.py` remains the only direct `fal_client` integration point;
`image_router.py` makes no `fal_client` import.

Core exports:

- `ImageRequest` / `ImageResult` / `ImageRoute` — pipeline-level dataclasses
  (provider-agnostic on the request/route side; `ImageResult.media_url` is
  always a local `cache/...` path or `None`, never a remote URL).
- `MODEL_CAPABILITIES` — capability table keyed by `schnell`, `dev`,
  `pro_1_1`, `pro_1_1_ultra`, `flux_2_pro`. Each entry declares `endpoint`,
  `size_mode` (`"image_size"` or `"aspect_ratio"`), `supports_steps`,
  `default_steps`, `supports_guidance`, `default_guidance`, `supports_seed`,
  `safety_mode` (`"enable_safety_checker"`, `"safety_tolerance"`, or
  `"both"`), and `output_formats`.
- `select_route(beat, content_id, ...)` — the routing decision function.
- `build_fal_payload(model_key, prompt, ...)` — pure payload builder; emits
  only the fields the chosen model's capability entry supports (Pro-family
  never receives a Schnell-shaped field, Flux 2 Pro never receives
  `num_inference_steps`/`guidance_scale`).
- `build_cache_key_material(model_key, prompt, ...)` — model-aware cache
  hash input, backward-compatible with every pre-Phase-14.6 cache entry for
  the Schnell tier (see Cache keys below).

Conservative-by-default contract (the load-bearing invariant of this
section — do not relax without an explicit operator config change):

- **The `purpose="text_card_background"` short-circuit in `select_route()`
  is legacy-inert**: text cards are removed (§11.6), no live caller passes
  that purpose anymore. The branch is retained only as a defensive guard;
  do not build new behavior on it.
- **Ordinary generated beats route to Schnell** unless
  `settings.image_routing_enabled` is `True` **and** the relevant tier flag
  (`image_routing_allow_dev` / `image_routing_allow_pro`) is `True` **and**
  the first-pass eligibility heuristic (`_heuristic_qualifies_for_higher_tier()`
  — cover frame, `beat_intensity="high"`, `visual_category="person"`, a
  reveal/climax keyword in `script_text`, or a future `thumbnail_candidate`
  flag) says the beat qualifies.
- **Pro-family usage is hard-capped per content** by
  `settings.image_routing_max_pro_per_content`, default `0` — Pro is never
  selected at all until an operator explicitly raises this above zero, even
  if `image_routing_allow_pro` is `True`.
- **Stock is reserved, never selected** — no stock provider exists yet;
  `_build_beat_section()` already overrides any stock `media_strategy` to
  `flux_generated` before beats reach the router (defensive branch only).
- **`schnell`'s endpoint is intentionally left as the repo's pre-existing
  literal** (`"fal-ai/flux-1/schnell"`), not updated to fal.ai's current
  documented name (`"fal-ai/flux/schnell"`, per Phase 14.5's research) —
  changing a live-traffic endpoint string cannot be verified without a live
  fal.ai call, which this and prior phases are forbidden from making. Dev/Pro
  endpoints use the Phase-14.5-verified documented names since they are
  never called by default and have no pre-existing production traffic to
  preserve.

Config flags (`app/config.py`, all default to preserving exact pre-14.6
behavior):

```text
image_routing_enabled              default False
image_routing_allow_dev            default False
image_routing_allow_pro            default False
image_routing_max_pro_per_content  default 0
```

Provider adapter (`flux_generator.py`):

- `_call_fal(prompt, cache_dir, media_path, cache_key_extra="", model_key="schnell")`
  resolves the endpoint and request payload from `image_router` — it no
  longer hardcodes a Schnell-shaped argument dict.
- `generate_beat_image(..., model_key="schnell")` — new trailing optional
  parameter; every pre-Phase-14.6 caller (including
  gets exactly the prior behavior unchanged. (The former text-card
  background caller was removed with §11.6.)
- `generate_beat_image_with_routing(beat, content_id, tier_counts)` — the
  one centralized call site that runs `image_router.select_route()` then
  `generate_beat_image()`. Both `generate_all_beat_images()` (parent) and
  `generate_pending_beat_images()` (child) call this for ordinary beats so
  the two paths make the identical routing decision with identical
  per-content Pro-tier bookkeeping (`tier_counts` — caller-scoped to one
  content item's generation run, never persisted).
- **The `reuse` route is honored** (routing audit close-out): a beat whose
  `media_url` is already a local `cache/` path is returned as-is — no
  generation call, no tier-count increment (previously the route was
  computed but ignored, so cache-holding beats inflated the "schnell" count
  in `IMAGE_ROUTE_TIER_COUNTS`). Guarded by a disk check: if the referenced
  file no longer exists (operator wiped `cache/`), the beat falls through to
  real regeneration (`IMAGE_ROUTE_REUSE_MISSING_ON_DISK`, logged) so a
  re-run self-heals instead of shipping a dead path the media validator
  would block on every retry.
- **Pro budget counts every pro-family selection** (`sum` over tier keys
  starting with `pro`), counted at *selection* time deliberately — a
  hard-failed Pro generation still consumes budget, so a flaky endpoint can
  never turn the per-content cap into unbounded Pro spend. Runtime proof:
  `tests/test_image_route_reuse_and_budget.py`.
- The Dev/Pro eligibility heuristic's `_REVEAL_KEYWORDS` are English-only
  (a non-English source script gets no reveal-based upgrades — same
  source-language-only convention as the stylistic script checks) and
  `thumbnail_candidate` is a future field nothing sets yet — both accepted,
  documented limits, not defects.

Cache keys (model-and-size-aware, backward-compatible by construction):

- For the Schnell tier at its pre-existing default landscape size
  (`1920x1080` — the size every pre-Phase-14.6 caller used),
  `build_cache_key_material()` reproduces the exact pre-Phase-14.6 material
  (`prompt` alone, or `f"{cache_key_extra}\n{prompt}"` when a namespace is
  given) — zero cache invalidation for any existing landscape cached image.
- For any non-Schnell tier, **or** the Schnell tier at any non-default size
  (e.g. `1080x1920` portrait — roadmap 3.1, §11.4's `generate_pending_beat_images`),
  the material is prefixed with `model={model_key}` and `size={width}x{height}`
  so a Dev/Pro/Flux-2-Pro generation, or a portrait Schnell generation, of
  the same prompt can never collide with (or accidentally reuse) a
  landscape Schnell cache entry.
- `flux_generator._call_fal()`/`generate_beat_image()`/
  `generate_beat_image_with_routing()` all accept optional `width`/`height`
  parameters defaulting to the landscape default (`1920x1080`) — every
  pre-existing caller (the parent path) gets identical pixels and an
  identical cache key to before this phase; only the child Short path
  passes `width=1080, height=1920`.

Logs:

```text
IMAGE_ROUTE_SELECTED             — every select_route() call (model/source/reason)
IMAGE_ROUTE_FALLBACK             — stock-disabled defensive fallback to schnell
IMAGE_ROUTE_REUSE_MISSING_ON_DISK — reuse-routed beat whose cached file is gone; regenerating
FAL_IMAGE_REQUEST_PREPARED       — payload keys for the model about to be called
FAL_IMAGE_GENERATED              — successful generation, with model/endpoint/media_url
FAL_IMAGE_FAILED                 — failed fal.ai call, with model/endpoint/exception type/message
IMAGE_ROUTE_TIER_COUNTS          — per-content tier usage summary, actual generations only
```

Rules:

- Agent 5 still receives only local `cache/...` `media_url` values from
  `VideoSection` rows — the router changes nothing about that contract;
  every `ImageResult`/generated path is downloaded to local disk before
  persistence, exactly as before this phase.
- Do not add a second fal.ai integration point — all endpoint selection and
  payload construction stays inside `image_router.py`'s capability table
  and `flux_generator.py`'s single `_call_fal()` call site.
- Do not change `MODEL_CAPABILITIES["schnell"]["endpoint"]` without an
  operator-run live canary outside Claude Code/Codex first (CLAUDE.md §19.1).
- Do not enable Dev/Pro tiers by default; do not raise
  `image_routing_max_pro_per_content` above `0` without an explicit
  operator decision.

### 11.6 AI Text Rendering Ban + Subtitles-Only Rendering (supersedes Phases 14.4/14.7/14.10b)

**Product rule (Golden Rule 11): Remotion renders subtitles ONLY.** The
subtitle track is the video's only text layer. Text cards
(`media_strategy="remotion_text_card"`, `TextCard.tsx`), per-beat overlay
text (`overlay_text`/`overlay_position`, `TextOverlay`), and the
subtitle-suppression machinery that coordinated them (Phase 14.10b's
`suppressWindows`/`computeOverlaySuppressWindows`) are all removed. The
Shorts "Part N of M" corner label is the single operator-opt-in exception
(`settings.short_part_label_enabled`, default off).

Why: image models cannot render legible text (generated documents/posters/
signs always carry gibberish), and the run-9500c231 audit (G-3) showed the
text-card mechanism double-printing — Flux-generated pseudo-text UNDER a
Remotion overlay — plus garbled text-card backgrounds being reused as plain
Short visuals. Narration and subtitles already convey any textual content;
on-screen text layers added failure modes without adding information.

What remains — prompt-side sanitization (the surviving half of Phase 14.7):

- **AI image models must never be asked to render readable text.** No
  `flux_prompt`, at any tier (§11.5), may request a quoted phrase, a
  "the text reads …", "label reading …", "sign that says …", or any other
  literal-text-rendering instruction.
- **Text-bearing narration moments are designed as physical scenes without
  legible text, using a named, generalizable technique taxonomy** (v4.2,
  roadmap 2.9 / audit G-3) — the storyboard prompt's "Every beat is a
  generated image" section teaches four reusable framing techniques rather
  than a fixed example list, so Claude can apply the rule to any
  text-bearing prop, not only the ones spelled out below:
    - ANGLE — side-on, from behind, or oblique (closed folder from its
      spine, phone face-down, a note glimpsed over a shoulder).
    - DISTANCE — the text-bearing surface as a small element of a wider or
      more distant shot (a calendar on a far wall, newspapers across a room).
    - LIGHTING — raking/grazing side light obscuring the surface with glare
      or shadow instead of lighting it for reading.
    - DETAIL — a physical, non-textual detail instead of the text itself
      (typewriter keys instead of the page, a worn corner instead of a
      headline, a wax seal instead of a letter's contents).
  The narration already says what the text said; the image only needs to
  establish the physical object.
- Detection — `flux_generator.is_text_prop_beat(beat)`: word-boundary
  keyword match on `flux_prompt`/`visual_intent`/`motif` against
  `_TEXT_PROP_KEYWORDS` (document, poster, calendar, sign, name tag, …).
  Word-boundary matters: plain substring matching once made "document"
  match inside "documentary photograph", silently rewriting valid prompts.
- Sanitization — `flux_generator.derive_text_prop_prompt(beat)`, wired into
  `storyboard._build_beat_section()` (shared by parent and child paths):
  derives a fresh prompt from `visual_intent` plus the detected prop label
  and environment scene words, injects the SAME ANGLE/DISTANCE/LIGHTING/
  DETAIL taxonomy as a positive framing clause
  (`flux_generator._select_text_prop_framing()`, chosen deterministically
  per beat — never random, so retries and segment-level regeneration stay
  byte-identical), and always appends the explicit no-readable-text clause
  alongside it (additive, not either/or — Flux prompting guidance favors
  positive description since Flux has no true negative-prompt channel).
  This is belt-and-suspenders behind the storyboard prompt's own guidance:
  it also fires for a child-remap beat, which has no storyboard-prompt
  pass of its own to apply the taxonomy at the source. Logged as
  `TEXT_PROP_PROMPT_SANITIZED`.
- Validator — check 19 (`ai_text_rendering_requested`, MAJOR) in
  `validate_storyboard()` fires on any beat whose prompt contains a quoted
  phrase or a literal-text-rendering instruction. No exemptions exist (text
  cards are gone). Defense-in-depth behind the keyword-based sanitizer.

Removed (do not reintroduce without a new product decision + CLAUDE.md
update):

- `derive_text_prop_overlay()` — the Phase 14.7 overlay-derivation half
  ("MISSING" labels, verbatim dates).
- `is_text_card_beat()`, `derive_text_card_background_prompt()`,
  `generate_text_card_background_image()` (Phase 14.4), and the
  `TEXT_CARD_BACKGROUND_*` log family.
- `_BEAT_SCHEMA` fields `media_strategy`, `stock_queries`,
  `fallback_flux_prompt`, `text_card_style`, `overlay_text`,
  `overlay_position` (`STORYBOARD_SCHEMA_VERSION` 6.1 → 7.0,
  `PROMPT_VERSION` 3.5 → 4.0). `_build_beat_section()` hardcodes
  `media_strategy="flux_generated"` and neutral overlay constants for DB
  column compatibility only.
- Remotion: `TextCard.tsx` (deleted), `TextOverlay`, `suppressWindows`
  props on `StandardSubtitles`/`KaraokeSubtitles`,
  `sectionHasActiveOverlay()`/`computeOverlaySuppressWindows()`. The
  no-media fallback is a plain dark fill with no placeholder text.
- Props: `_section_for_remotion()` emits no `overlay_text`/
  `overlay_position`/`text_card_style` keys, and the
  `overlay_text or script_text` fallback (which displayed a beat's entire
  narration as a giant card) is gone.

Legacy data handling:

- Rows persisted before this change may carry `remotion_text_card`,
  `visual_type="text_card"`, the `__text_card__` sentinel, or overlay text.
  The shared loader (`app.services.video_sections.load_video_sections()`,
  roadmap 6.5 — used by both Agent 4 and Agent 5) normalizes them on load:
  sentinel → `""` (regenerated by the flux-incomplete path or blocked by the
  missing-media blocker), `text_card` → `b-roll`, overlays blanked.
- The child remap reuse pool excludes legacy parent text-card beats before building the Claude candidate index
  (`_parent_media_reusable()`, audit G-4.1) — their backgrounds often carry
  baked-in pseudo-text.
- `validate_visual_media_assets()` (the single media validator since roadmap
  6.4) flags a surviving sentinel as `legacy_text_card_sentinel` and treats
  it as blocking — no text-card exemption remains.

Runtime proof: `tests/test_subtitles_only_rendering.py` (real props builders
writing real JSON with zero text-layer keys; real `generate_all_beat_images`
neighbour-reuse with only the paid fal.ai wrapper stubbed; beat-build
normalization; reuse-pool exclusions; retired-check silence; static surface
checks on schema/prompt/Remotion sources) and
`tests/test_text_prop_framing_guidance.py` (deterministic per-beat framing
selection; both the framing and no-readable-text clauses present together;
the real `_build_beat_section()` chain proving the framing clause survives
beat construction; static proof the storyboard prompt teaches all four
techniques as a general rule, not just worked examples).

### 11.7 Visual Amplification Rule — Amplify, Don't Illustrate (Phase 14.8)

A visual that simply shows the literal noun or action the narration just
named (narration: "she entered the room" → image: a room) is illustration,
not amplification, and reads as artificial/AI-generated. `_STORYBOARD_SYSTEM_PROMPT`
(`app/agents/agent4_visuals/system_prompt.py`) now carries an explicit rule
(Principle B2) requiring every beat to add at least one of: emotional
reaction, hidden threat, consequence, evidence, spatial tension,
symbolic/foreshadowing detail, human vulnerability, or aftermath — never
just the bare noun/action from the sentence it accompanies.

A second new section ("beat category rotation") instructs the model to
alternate between six rotation categories — human reaction, threatening
space, evidence/prop, environmental clue, consequence/aftermath,
motion/action — expressed entirely through the **existing**
`visual_category` / `visual_type` / `motif` beat fields (`person`/`place`/
`object`/`document` etc.). No new beat schema field was added for this;
`STORYBOARD_SCHEMA_VERSION` stays `6.1`. The rule explicitly names the
regression pattern to avoid: a run of object → object → room → object with
no human-reaction/threat/consequence variety.

This is a prompt-only change (`PROMPT_VERSION` 3.3 → 3.4). No new
deterministic validator check was added — the existing `validate_storyboard()`
checks (`consecutive_same_environment`, `motif_repetition_in_window`,
`near_duplicate_beat`, `ai_slideshow_risk`) already detect repeated
category/environment/motif runs, confirmed directly against a hand-built
object/room-repetition fixture rather than assumed. A small, equivalent
nudge ("prefer a reaction/consequence/evidence query over the literal
noun/action") was also added to `_SPLITTER_SYSTEM_PROMPT`, the legacy
section-splitter fallback prompt — additive only, no JSON-shape change to
that path's `search_query`/`suggested_visual` contract.

Rules:

- This principle applies to beat content choice only — it does not change
  the AI text-rendering ban (§11.6), image model routing (§11.5/Phase
  14.6), or the Short visual hold-cap (§11.4/Phase 14.3).
- Do not add a new beat schema field for "rotation category" — express it
  through `visual_category`/`visual_type`/`motif`, exactly as this phase
  does, unless a future phase produces concrete runtime evidence (CLAUDE.md
  §19.4) that the existing fields cannot carry the distinction.
- Do not add a quality-judgment validator check for "this prompt merely
  illustrates the narration" — that is a semantic judgment a deterministic
  Python check cannot make reliably; rely on the prompt rule plus the
  existing structural-repetition checks instead.

---

## 11A. Agent 5 — Rendering (render-only)

### 11A.1 Agent 5 Responsibilities

Agent 5 owns:

- Render-ready pickup for content with an existing `AudioFile` and at least
  one persisted `VideoSection` row.
- Parent/child render routing.
- Subtitle chunk generation.
- Remotion props generation.
- Main video rendering.
- Vertical short rendering.
- Render verification.
- `VideoRender` persistence.

Agent 5 must not:

- Generate audio.
- Recreate scripts.
- Cut parent videos into shorts.
- Generate or validate storyboards, generate or validate Flux prompts,
  generate media, perform child remapping, or persist `VideoSection` rows.
- Import any `app.agents.agent4_visuals` module, or call Agent 4 in any form.
- Fall back to visual generation when `VideoSection` rows are missing — it
  must defer instead.
- Store remote media URLs in props.

Current boundary (Phase 4D-C — fully implemented):

- Agent 4 (`app/agents/agent4_visuals/`) is the sole producer of visual-ready
  content: it loads its own preconditions, generates/remaps visuals, and
  persists `VideoSection` rows, all without any call from or into Agent 5.
- Agent 5 (`app/agents/agent5_render/`) is a pure consumer: it loads its own
  preconditions independently, reads existing `VideoSection` rows with a
  private read-only loader, and never writes that table.
- The two agents are decoupled at the Celery layer: `pickup_audio_done`
  dispatches Agent 4's visual task; `pickup_visual_ready` independently
  dispatches Agent 5's render task once `VideoSection` rows exist. Neither
  Celery task calls the other.
- Rendering starts only after the content has an `AudioFile` and at least one
  persisted `VideoSection` row; a language with neither is deferred, not
  failed, and not generated by Agent 5 as a fallback.
- Agent 5 render verification is deterministic technical verification only.
  When enabled, `verify_render()` checks that the rendered MP4 file exists,
  uses ffprobe for duration drift, audio/video stream presence, and expected
  resolution, and uses ffmpeg blackdetect/silencedetect for black-frame and
  interior-silence intervals. It is not AI quality verification and does not
  judge storyboard quality, Flux prompt quality, image quality, visual taste,
  narrative alignment, or creative continuity.

### 11A.2 Parent Agent 5 Render Flow

```mermaid
flowchart TD
    A[Parent AUDIO_DONE] --> B[pickup_audio_done]
    B --> C[run_agent4_visual_generation_for_content]
    C --> D[Parent PARENT_VISUALS_DONE]
    D --> E[pickup_visual_ready]
    E --> F[run_agent5_render_for_content]
    F --> F2[Parent RENDERING]
    F2 --> G[build_main_props]
    G --> H[render_main_video or chunked render]
    H --> I[verify_render]
    I --> J[VideoRender format=main]
    J --> K[Parent RENDERED]
```

### 11A.3 Child Short Agent 5 Render Flow

```mermaid
flowchart TD
    A[Child AUDIO_DONE] --> B[pickup_audio_done]
    B --> C[run_agent4_visual_generation_for_content]
    C --> D{parent __visual__ rows exist?}
    D -->|no| E[defer to Child AUDIO_DONE]
    D -->|yes| F[remap + persist child VideoSection rows]
    F --> F1[Child CHILD_SHORT_VISUALS_DONE]
    F1 --> G[pickup_visual_ready]
    G --> H[run_agent5_render_for_content]
    H --> H2[Child RENDERING]
    H2 --> I[build_short_props]
    I --> J[render_short Short.tsx]
    J --> K[verify_render]
    K --> L[VideoRender format=short]
    L --> M[Child RENDERED]
```

### 11A.4 Important Agent 5 Functions

#### `pickup_audio_done`

File:

```text
app/scheduler/tasks.py
```

Responsibilities:

- Find `AUDIO_DONE` content with an `AudioFile`.
- Skip content that already has the relevant `VideoRender` row.
- Enqueue `run_agent4_visual_generation_for_content` (Agent 4, not Agent 5).

Rules:

- Can pick parent and child content.
- Does not dispatch Agent 5 directly — Agent 5 pickup is a separate path.

#### `pickup_visual_ready`

File:

```text
app/scheduler/tasks.py
```

Responsibilities:

- Find content with `Content.status` in (`PARENT_VISUALS_DONE`,
  `CHILD_SHORT_VISUALS_DONE`) — status is the primary readiness signal.
- Defensively verify at least one persisted per-language `VideoSection` row
  exists (`language != "__visual__"`); skip and log a data-consistency
  warning if status says ready but the row is missing.
- Skip content that already has the relevant `VideoRender` row.
- Enqueue `run_agent5_render_for_content`.

Rules:

- This is the only path that hands ready content to Agent 5.
- Gates on `Content.status`, not on `VideoSection` row existence — the row
  check is a defensive validation only, per Phase 4D-D.

#### `run_agent5_render_for_content`

File:

```text
app/scheduler/tasks.py
```

Responsibilities:

- Run rendering orchestration for one content ID.
- Requires `Content.status` to already be `PARENT_VISUALS_DONE`,
  `CHILD_SHORT_VISUALS_DONE`, or `RENDERING`; never calls Agent 4.

Temporary compatibility:

- `run_agent5_for_content` remains as a Celery task alias for old queued messages only.
- New code must call `run_agent5_render_for_content`.

#### `run_video_generation`

File:

```text
app/agents/agent5_render/services/video.py
```

Responsibilities:

- Route parent vs child rendering.
- Manage status transitions.
- Load existing `VideoSection` rows (read-only) for each language.
- Persist video outputs.

Rules:

- Does not import or call any `app.agents.agent4_visuals` module.
- A language with no persisted `VideoSection` rows is deferred
  (`RENDER_DEFERRED ... reason=visual_sections_missing`), not generated.
- Parent renders only `format="main"`.
- Child renders only `format="short"`.
- Parent must not create short props or short renders.

#### `build_main_props`

File:

```text
app/agents/agent5_render/services/remotion_builder.py
```

Rules:

- Parent main props only.
- Validate media paths.
- Fail fast if remote URLs are present.

#### `build_short_props`

File:

```text
app/agents/agent5_render/services/remotion_builder.py
```

Rules:

- Child short props only.
- Use vertical layout.
- Use child audio.
- Use child timed beats.
- Subtitles-only rendering: no overlay/text-card key is emitted; every
  section is a plain local image clip.
- Do not add rehook/bridge audio unless a future architecture explicitly reintroduces standalone short bookends.

#### `render_main_video`

File:

```text
app/agents/agent5_render/services/renderer.py
```

Rules:

- Use `MainVideo.tsx`.
- Render 1920×1080.
- Use chunked render for long durations.
- Verify final output.

#### `render_short`

File:

```text
app/agents/agent5_render/services/renderer.py
```

Rules:

- Use `Short.tsx`.
- Render 1080×1920.
- Store as `VideoRender(format="short")`.
- Output belongs to the child content ID.

#### `_collect_technical_blockers`

File:

```text
app/agents/agent5_render/services/video.py
```

Responsibilities:

- Return deterministic technical blocker strings that stop a language's
  render before Remotion props are built. A non-empty result skips that
  language (returns `False` from `_process_language()`); other languages for
  the same content are unaffected.

Blockers:

| Blocker | Condition |
|---|---|
| `no_beats` | Zero persisted beats for this language |
| `missing_media_critical` | More than `_MISSING_MEDIA_BLOCK_RATIO` of beats have no local `cache/` media |
| `no_captions` | A real Whisper transcript exists but caption chunk-building itself failed |
| `empty_whisper_transcript_short` | `is_short_episode=True` and `audio.whisper_transcript` is empty (roadmap 3.9 / audit A-3, exec-10) |

Rules:

- `empty_whisper_transcript_short` is scoped to Shorts only
  (`is_short_episode` parameter, default `False` for backward compatibility)
  — parent long-form content keeps tolerating a missing transcript exactly
  as before, since the storyboard/timestamp pipeline already has its own
  proportional-fallback timing machinery. A Short with zero captions is
  materially worse: ~80% of Shorts/TikTok viewers watch muted, so a
  captionless vertical Short is close to unwatchable, not just imperfect.
- This is the render-side half of a two-part fix; the audio-side half is
  `agent3_audio.services.audio._assert_short_audio_has_transcript()` below —
  each half is an independent safety net (a Short that somehow reaches
  Agent 5 with an empty transcript despite Agent 3's own check is still
  blocked here).

### 11A.5 Subtitle/Overlay Collision Prevention (Phase 14.10b — RETIRED)

Retired by subtitles-only rendering (§11.6 / Golden Rule 11). The mechanism
existed to stop the global subtitle layer colliding with per-section
`TextOverlay`/`TextCard` layers; those layers no longer exist, so there is
nothing to suppress. `suppressWindows`, `sectionHasActiveOverlay()`,
`computeOverlaySuppressWindows()`, and the `showOverlay` crossfade gate were
removed from the Remotion components; `StandardSubtitles`/`KaraokeSubtitles`
render unconditionally as the video's only text layer. Subtitle chunk timing
data (`app/agents/agent5_render/services/subtitles.py`) was never touched by
either the original mechanism or its removal.

### 11A.6 Color Grade CSS Filter (Phase 14.11)

`MediaSection.tsx`'s `GRADE_FILTER` constant is the **render-active**
implementation of `color_grade` — applied directly as the CSS `filter`
style on every rendered clip (`filter: GRADE_FILTER[colorGrade] ?? "none"`
in `SingleClip`). `color_grade` is not descriptive metadata; whatever value
a beat carries genuinely changes the pixels Remotion renders.

A confirmed real defect (`code_report/phase_14_11_color_grade_cast_investigation.md`):
`cold_blue`'s filter originally used `hue-rotate(200deg)` — not a subtle
cool nudge, but a rotation of nearly half the hue wheel. CSS `hue-rotate`
shifts every hue in the image by the given angle; 200deg pushes skin tones
(~25-35deg, orange) into the blue family and green foliage (~90-130deg)
into violet/magenta — confirmed by direct hue-math, matching the reported
"unnatural purple/blue tint... including foliage and skin tones" defect.
Fixed by reducing the magnitude to `hue-rotate(15deg)` — small enough that
no recognizable real-world hue (skin, foliage, sky) crosses into an
unrelated color family; the "cool/muted" feel comes from the
already-present `saturate(70%)`/`brightness(80%)` components, not from an
extreme hue rotation.

Investigation also confirmed, and rules out as causes:

- Agent 4's storyboard prompt (`_STORYBOARD_SYSTEM_PROMPT`'s "Color grade
  integration" section) asks only for "naturally cool-toned lighting" for
  `cold_blue` — it never mentions purple/magenta and is never told the
  actual CSS value the render layer applies, so the cast was not
  prompt-induced.
- The image router (§11.5) sends no color/style parameters to any fal.ai
  model tier.
- No mix-blend-mode, full-frame overlay, or ffmpeg/ renderer-level color/LUT
  post-process exists anywhere else in the pipeline — `GRADE_FILTER` is the
  only mechanism capable of a wholesale hue-family shift.

Rules:

- Any grade using `hue-rotate` must keep the magnitude small enough that no
  representative real-world hue (skin ~25-35deg, foliage ~90-130deg, sky
  ~200-220deg) crosses into an unrelated hue family after rotation — verify
  with the same hue-math approach this phase used before changing the value
  again.
- **Dark-frame guard (audit G-6, wired):** `dark_contrast` on a source image
  with no bright light renders near-black. `_build_beat_section()` enforces
  the storyboard prompt's "well-lit source" rule deterministically: when the
  final `flux_prompt` (post-sanitization — exactly what Flux receives)
  carries no bright-lighting evidence
  (`storyboard_validator.has_bright_lighting_evidence()`), the grade is
  downgraded in place to `desaturated` and logged as
  `DARK_CONTRAST_GRADE_DOWNGRADED`. Validator check 20
  (`dark_contrast_unlit_prompt`, MINOR) keeps the invariant observable for
  any beat that bypasses beat building. Runtime proof:
  `tests/test_dark_frame_guard.py` (real mapping chain, no external API on
  the path).
- If a grade's prompt-side guidance (`system_prompt.py`) implies a specific
  CSS effect, state the actual CSS values in the prompt (as `dark_contrast`
  already does) so Claude's choices and the render-layer effect stay
  legible to whoever reads/maintains the prompt.

### 11A.7 Future Background Music Asset Strategy (Phase 15.2 — design only)

No background music/score layer exists in the pipeline yet. Phase 15.1
(`code_report/phase_15_1_music_intensity_curve_investigation.md`) confirmed
this and recommended a local royalty-free loop library plus a Remotion
volume curve as the safest future architecture. Phase 15.2
(`code_report/phase_15_2_music_asset_strategy.md`) defined, but did not
wire into any render path, the asset/metadata strategy a future
implementation phase will build on:

- **Ownership: Agent 5/Remotion**, not Agent 3 — voice/TTS generation is
  untouched by this strategy and must remain so.
- **Real audio files belong under `{media_path}/music/{filename}`** — the
  same runtime, git-ignored, operator-populated convention already used
  for `audio/`, `video/`, and `cache/` (§13). No audio file may ever be
  committed to this repository. `staticFile()` + the render command's
  existing `--public-dir <media_path>` flag already resolve this path the
  same way the narration `audio_file` prop does — no new Remotion bundler
  configuration is needed when a real implementation phase wires this up.
- **Metadata schema** lives at
  `app/agents/agent5_render/music/schema.py` (the required-field
  definitions and two pure, local-only validators —
  `validate_entry_metadata()` for shape/licensing,
  `validate_entry_asset_on_disk()` for local filesystem existence/format/
  duration) and
  `app/agents/agent5_render/music/music_library.example.json` (a
  template with fictitious placeholder entries only — never real track
  metadata, clearly marked `_TEMPLATE_ONLY: true`).
- **Licensing policy — default-deny, not default-allow:** every entry must
  carry a non-empty `license` and `source`, and
  `safe_for_commercial_use` must be explicitly `true` — missing, false, or
  any non-boolean value makes an entry ineligible for use. No unverified or
  undocumented asset may ever be selected by a future implementation.
- **Intensity/mood tagging** reuses the storyboard's existing
  `beat_intensity` enum (`high`/`medium`/`low`) as the `intensity_tier`
  field, rather than inventing a new scale — but `beat_intensity` is
  currently NOT forwarded by `_section_for_remotion()`/`SectionData`
  (`types.ts`) into Remotion props; a future implementation phase's first
  step is forwarding that one existing field, not adding a new data
  source.

Rules:

- Do not add a real audio file to this repository under any path.
- Do not treat `music_library.example.json` as real, usable, or
  legally-cleared data — every value in it is a placeholder.
- A future implementation phase must not select or mix any music-library
  entry whose `validate_entry_metadata()` result is non-empty, or whose
  `safe_for_commercial_use` is not exactly `true`.
- Do not add a music provider or live music-generation API as part of this
  strategy — the minimum viable architecture is a static local loop
  library only.

---

## 12. Parent vs Child Short Comparison

| Dimension | Parent long content | Child short episode |
|---|---|---|
| `content.is_short_episode` | `False` | `True` |
| Parent link | none | `parent_content_id` |
| Script | long structured script | standalone flat short narration |
| Audio | own full audio | own short audio |
| Whisper | parent transcript | child transcript |
| Breakpoints | empty | empty |
| Bookends/rehooks/bridges | disabled | disabled |
| Visual pass | full storyboard + Flux | remap parent visual beats, then cap non-terminal visual holds |
| Images | parent Flux cache, landscape 1920×1080 | every beat regenerated portrait 1080×1920 in child cache, reusing the parent's prompt where match score qualifies |
| Remotion composition | `MainVideo.tsx` | `Short.tsx` |
| Resolution | 1920×1080 | 1080×1920 |
| `VideoRender.format` | `"main"` | `"short"` |
| Render content ID | parent ID | child ID |
| Status flow | parent flow | child flow |

---

## 13. Artifact Map

### Local run folder structure

For every content run, local debugging artifacts are grouped under:

```text
{media_path}/runs/{content_id}/
```

Subfolders:

```text
script/
audio/
whisper/
visuals/
review/
renders/
```

`run_manifest.json` is created in the run root when the local run folders are
ensured.

Before visual beat generation, Agent 4 creates or loads a local visual bible:

```text
{media_path}/runs/{content_id}/visuals/visual_bible.json
```

Agent 4 also writes review-only beat metadata to the same `visuals/`
subfolder (roadmap 6.5 / audit AR-3 — moved out of the DB's
`generation_prompt` blob, which nothing in the live pipeline queried back):

```text
{media_path}/runs/{content_id}/visuals/beat_review_metadata.json
```

First15 diagnostics, cinematic continuity tags/references, prompt-quality
warnings, and per-beat descriptive fields (`subject`, `emotion`, `camera`,
etc.) live here instead, keyed by language then `section_order`. Written by
`save_beat_review_metadata()` once per language, alongside every
`_save_video_sections()` call. Local-debug/review infrastructure only —
`visual_review.html` reads it; nothing else does, and Agent 5 never depends
on it.

The visual bible defines story-level visual continuity: stable characters,
locations, motifs, global style, lighting rules, camera language, negative
prompt rules, first-15-seconds visual rules, and forbidden generic shots. It is
generated from the validated script, `Content.story_blueprint` when available,
and non-secret channel configuration such as `visual_style`, `image_style`,
`video_style_type`, `video_color_grade`, content mode, output mode, target
languages, and target platforms. If `Content.channel_config_snapshot` is
present, Agent 4 uses it as the config source; otherwise it reads live
non-secret channel/config rows. Parent content creates the canonical visual
bible. Child shorts reuse the parent visual bible when available so they do not
create separate visual worlds. The bible is local-debug/quality infrastructure;
it does not replace `VideoSection` rows and Agent 5 does not depend on it.

Agent 4 uses `visual_bible.json` as storyboard input, not as a replacement
template after validation. Parent storyboard generation passes a compact bible
JSON block into every `generate_storyboard_batch()` call; validation retries do
the same. After validation, `apply_cinematic_prompts_to_beats()` is append-only:
it preserves the Claude-authored `flux_prompt`, adds at most matched
character/location continuity clauses, and stores continuity metadata
(`continuity_tags`, `visual_bible_refs`, first-15 flag) on the in-memory beat
dict — as of roadmap 6.5, these are review-only fields that end up in
`beat_review_metadata.json` (see above), not in `VideoSection.generation_prompt`.
It must never prepend "Cinematic", add
`Avoid:`/negative-prompt lists, truncate phrases with ellipses, or inject raw
`camera_language` / `lighting_rules` entries. The enriched prompts are re-validated by `_check_final_prompt_issues()` before
first15, then re-validated again after first15 because that step can append to
`flux_prompt`; both passes use only checks 4, 12-16, and 19. MAJOR final-prompt
findings stop the parent pass or skip the affected child language. Agent 5 does
not generate, repair, or validate prompts.

Agent 4 also validates and strengthens first-15-second visual hooks with
deterministic checks before Flux generation. The check uses the Visual Bible
`first_15_seconds_rules`, forbidden generic shots, negative prompt rules,
global style, and config context to look for strong opening subjects, actions,
emotions, mysterious objects, specific horror clues, and story-specific
locations while avoiding generic dark streets, forests, empty rooms, abstract
symbols, and low-tension filler openings. Clearly unusable openings fail using
the existing Agent 4 failure convention; weak but usable openings are warnings.
The result is stored in existing `VideoSection.generation_prompt` metadata and
displayed in `visual_review.html`. Agent 5 does not own or repair
first-15-second visual quality. This is not the general visual
repetition/rhythm validator.

After successful Agent 4 visual generation, Agent 4 also writes a local review
page:

```text
{media_path}/runs/{content_id}/review/visual_review.html
```

The review page summarizes the content item and renders the persisted
`VideoSection` rows beat by beat with escaped narration, prompts, metadata,
media paths, warnings, and local image previews when files exist. It is
local-debug only: it is not a production readiness gate, does not replace
`VideoSection` rows, and Agent 5 does not depend on it.

Local agent-by-agent quality scripts live under `scripts/local_quality/`. They
are local development tools only: each script either inspects existing DB rows
and local artifacts or calls the existing agent service entrypoint when an
explicit external-cost flag is provided. They write JSON reports under
`{media_path}/runs/<content_id>/review/` (or `runs/channel_<channel_id>/review/`
for channel-only Agent 1 checks). They do not replace Celery/Redis
orchestration, do not own agent business logic, and do not add production
statuses. External-cost execution requires explicit flags such as `--real-ai`,
`--real-tts`, `--real-whisper`, `--real-flux`, `--real-render`, or
`--allow-external`.

Agent 4 validates persisted visual media assets before writing visual-ready
status. Blocking failures include missing media paths, remote URLs where local
media is required, unsafe paths outside the configured media root/run root,
missing files, non-file paths, zero-byte files, unsupported local media
extensions, and local image files that cannot be opened/read. Intentional
text-card sections are allowed without local image media when explicitly marked
by the persisted section metadata. This validation is deterministic and local:
it does not call AI providers or external media services. Agent 5 does not
repair Agent 4 media and does not own this validation.

This structure is local-debug organization only. It does not replace
PostgreSQL as the source of truth, does not change Celery orchestration, and
does not introduce production object storage. Existing artifact paths below
remain unchanged.

### Parent audio

```text
{media_path}/audio/{parent_content_id}/{language}.mp3
```

### Child short audio

```text
{media_path}/audio/{child_content_id}/{language}.mp3
```

### Parent Remotion props

```text
{media_path}/remotion_props/{parent_content_id}_{language}_main.json
```

### Child short Remotion props

```text
{media_path}/remotion_props/{child_content_id}_{language}_short_{short_order}.json
```

### Parent main video

```text
{media_path}/video/{parent_content_id}/{language}_main.mp4
```

### Child short video

```text
{media_path}/video/{child_content_id}/{language}_short_{short_order}.mp4
```

### Flux cache

Parent (landscape, 1920×1080):

```text
{media_path}/cache/{parent_content_id}/{prompt_hash}.jpg
```

Child (portrait, 1080×1920 — roadmap 3.1 / audit V-2, G-4.2): every child
Short beat's image, including a beat whose prompt was inherited from a
parent match, is generated fresh at portrait size and cached under the
child's own content ID — a child Short never renders a `media_url` under
the parent's cache directory:

```text
{media_path}/cache/{child_content_id}/{prompt_hash}.jpg
```

`prompt_hash` is size-aware for portrait requests
(`image_router.build_cache_key_material()`), so it never collides with the
parent's own landscape hash for the identical prompt text.

### Whisper transcripts

Stored in database:

```text
audio_files.whisper_transcript
```

No separate transcript file is required.

---

## 14. Celery and Beat Tasks

Primary task file:

```text
app/scheduler/tasks.py
```

Important tasks:

| Task | Responsibility |
|---|---|
| `dispatch_discovery` | start scheduled discovery |
| `run_agent2_for_channel` | discovery for one channel |
| `pickup_approved_content` | enqueue script generation |
| `run_agent2_scripts_for_content` | run Agent 2 script workflow task wrapper |
| `pickup_scripts_validated` | enqueue Agent 3 audio |
| `run_agent3_audio_for_content` | generate audio/Whisper |
| `pickup_short_episodes_awaiting_parent` | compatibility no-op for old queued child-release messages |
| `ensure_child_short_audio_enqueued` | compatibility no-op for old parent-audio child-release imports |
| `pickup_audio_done` | enqueue Agent 4 visual generation |
| `run_agent4_visual_generation_for_content` | generate storyboard/Flux visuals or child remap; persist `VideoSection` rows |
| `pickup_visual_ready` | enqueue Agent 5 render once status is `PARENT_VISUALS_DONE`/`CHILD_SHORT_VISUALS_DONE` |
| `run_agent5_render_for_content` | render video; requires visual-done status and existing `AudioFile`/`VideoSection` rows |

Rules:

- Beat tasks are safety nets, not the only orchestration mechanism for critical handoffs.
- Child short audio pickup must not depend on parent audio completion.
- Idempotency must be enforced at the worker/service layer.
- Duplicate task dispatch is tolerable only if worker-level guards prevent duplicate side effects.

---

# Part 2 — Rules

## 15. Golden Rules

1. **Architecture first.** Do not optimize until the flow is correct.
2. **Business rules live in Python.** Prompts generate; code decides.
3. **No silent fallbacks.** Every fallback must log.
4. **No legacy reactivation.** Removed parent-cut shorts must not return.
5. **No development-phase names in production code.** Use product/domain names.
6. **No direct provider clients from agents.** Use shared service wrappers.
7. **No unvalidated AI output.** Parse and validate.
8. **No remote media URLs in Remotion props.** Local paths only.
9. **No live external API tests by Claude Code/Codex.** Use smoke tests and mocked tests only.
10. **Update this file with architecture changes.**
11. **Remotion renders subtitles ONLY.** The subtitle track is the video's only
    text layer — no text cards, no overlay text, no placeholder labels. Every
    beat is a Flux-generated image. (The Shorts "Part N of M" corner label is
    the single operator-opt-in exception, behind `short_part_label_enabled`,
    default off.)

---

## 16. Naming Rules

### 16.1 Use Domain Names, Not Development Phase Names

Forbidden in new or touched code:

```text
phase1
phase2
phase3
phase4
phase5
phase6
phase_4
PHASE4
Phase4
```

Do not use development phase labels in:

- file names
- function names
- class names
- constants
- model fields
- log names
- comments that describe current architecture
- tests, unless the test is intentionally documenting old migration history

Use product/domain terms instead:

| Bad | Good |
|---|---|
| `phase4_short_render` | `render_standalone_short` |
| `PHASE4_AUDIO_SHORT` | `SHORT_EPISODE_AUDIO` |
| `phase4_child_audio` | `child_short_audio` |
| `phase4_reuse_stats` | `short_episode_reuse_stats` |
| `phase4_only_shorts` | `standalone_shorts_only` |

Historical migration filenames may keep old names if already applied. Do not edit applied migrations just for naming.

### 16.2 Function Naming

Functions must describe the domain action and the object being acted on.

Good:

```python
generate_story_blueprint()
generate_script_sections()
run_shorts_planner()
pickup_scripts_validated()
remap_beats_for_short()
build_short_props()
render_short()
```

Bad:

```python
do_phase4()
process_stuff()
handle_video()
new_flow()
run_magic()
fix_short()
```

Rules:

- Use verbs for functions.
- Use nouns for data objects.
- Use `ensure_` for idempotent state reconciliation.
- Use `create_` only when a new row/artifact is always created.
- Use `get_` for pure retrieval.
- Use `load_` for DB/file reads.
- Use `build_` for in-memory structures/props.
- Use `generate_` for AI or media generation.
- Use `render_` only for Remotion/video rendering.
- Use `validate_` for checks that return issues or raise.
- Use `run_` for task/service orchestrators.

### 16.3 Log Naming

Logs should answer:

- what happened
- to which content ID
- whether it was parent or child
- whether it was skipped/enqueued/generated/rendered
- why

Good log names:

```text
AUDIO_PICKUP
CHILD_SHORT_AUDIO_START
CHILD_SHORT_AUDIO_DONE
STANDALONE_SHORT_RENDER
STANDALONE_SHORT_RENDER_DONE
STANDALONE_SHORT_REUSE_STATS
PARENT_AUDIO_NO_SHORTS
STORYBOARD_FINAL
STORYBOARD_COST_ESTIMATE
```

Avoid vague logs:

```text
done
started
fix applied
phase4 complete
shorts ok
```

---

## 17. Python Coding Standards

### 17.1 General

- Python 3.11+.
- Use type hints on all public functions.
- Keep functions single-responsibility.
- Prefer pure functions for validation and transformations.
- Avoid global mutable state.
- Use `pathlib.Path` for filesystem paths.
- Use `datetime` with explicit timezone when persisting dates.
- Never hardcode secrets, paths, model IDs, or API keys.
- Use `app/config.py` for settings.

### 17.2 Error Handling

- Wrap external API calls in `try/except`.
- Log exception context without leaking secrets.
- Raise or mark failure explicitly.
- Do not swallow exceptions that leave DB state ambiguous.
- Do not use broad `except Exception` without a log and a clear status decision.

### 17.3 Idempotency

Any task that may rerun must be idempotent.

Rules:

- Check if DB rows already exist before creating.
- Check if files already exist before regenerating expensive artifacts.
- Upsert rather than duplicate where appropriate.
- If skipping because an artifact exists, verify that the DB row is consistent.
- Log idempotent skips.

### 17.4 Status Transitions

- Status transitions must happen in services/tasks, not prompts.
- Never rely on Claude to choose a DB status.
- Commit after meaningful status changes.
- If a task fails after setting an in-progress status, mark `FAILED` or leave a recoverable state intentionally.
- Document intentional deferrals.

### 17.5 Comments

- Comments must explain why, not restate what code does.
- Remove outdated comments immediately.
- Do not leave development history as current architecture comments.
- Use TODO only with a clear owner/context or explicit deferred feature.

---

## 18. SQLAlchemy and Database Rules


### 18.1 Alembic Migration Naming

Migration files must be sequential and descriptive:

```text
001_initial_schema.py
002_add_content_language_index.py
003_add_video_metadata_table.py
```

The Alembic revision id must match the numeric prefix:

```python
revision = "001"
down_revision = None

revision = "002"
down_revision = "001"
```

Rules:

- Never use random or generated Alembic revision ids.
- Never create multiple migrations for one logical schema change.
- During development, schema resets may consolidate migrations into `001_initial_schema.py` only when explicitly requested.
- In production or staging, never rewrite existing migration history.

- Use SQLAlchemy 2.0 style.
- Use `Mapped[...]` and `mapped_column(...)`.
- Use JSONB for PostgreSQL JSON columns.
- Add Alembic migration for every schema change.
- Never edit an applied migration.
- Use descriptive migration slugs.
- Keep model defaults and DB defaults aligned.
- Add indexes for high-volume status/pickup queries.
- Unique constraints must match idempotency requirements.
- Avoid business logic in model properties when service-level logic is clearer.

Expected useful constraints:

- `AudioFile(content_id, language)` unique.
- `Content.content_hash` unique.
- Consider uniqueness for `VideoSection(content_id, language, section_order)` if concurrency becomes a problem.
- Consider uniqueness for `VideoRender(content_id, language, format, short_order)` if duplicate renders appear.

---

## 19. Testing Rules

### 19.1 Hard Rule: No Live API Runs by Claude Code/Codex

Do not run commands that call real external services:

- Claude API
- Cartesia
- ElevenLabs
- fal.ai
- OpenAI Whisper
- Telegram
- YouTube
- Meta
- TikTok
- web search

Do not run:

```bash
python test_full_pipeline.py
```

unless the user explicitly runs it outside Claude Code/Codex and pastes logs.

Allowed validation:

- `ast.parse`
- import checks
- pure Python smoke tests
- mocked unit tests
- function-source inspection
- static assertions
- dry-run tests with stubs

### 19.2 Smoke Test Rules

Smoke tests must:

- avoid live API calls
- be deterministic
- use fixtures/stubs
- assert the exact invariant being changed
- print `SMOKE PASS`
- include assertion count when useful

### 19.3 Test Naming

Use descriptive names:

```text
scripts/smoke_child_short_audio_orchestration.py
scripts/smoke_standalone_short_render.py
scripts/smoke_storyboard_cost_logging.py
```

Avoid development phase names in new tests unless preserving old historical tests.

### 19.4 Runtime-Proof Requirements

Static/AST-based smoke checks confirm a change's *shape* — the right
functions exist, the right names are called in the right order, the right
import boundaries hold. They do not confirm a change's *data-flow
correctness* across more than one function. A real bug shipped past every
static smoke check for several phases this way: `_build_beat_section()`
silently dropped a beat's `media_url` during timestamp mapping, so every
child short silently regenerated a Flux image instead of reusing the
parent's — correct call graph, wrong data, undetected until a runtime
harness was actually run (see `code_report/phase4e_e_child_remap_validator.md`
§11 and `code_report/phase4e_e1_cost_bug_validation_addendum.md`).

This is now a mandatory development rule, not a suggestion: **runtime proof
is required**, in addition to static/AST smoke checks, whenever a phase
changes or depends on any of the following:

- data flowing through more than one function before being persisted or
  consumed (a field set in function A, read in function C, with an
  intermediate function B the change doesn't visibly touch);
- fields being preserved through a mapping, building, or persistence step
  (any `_build_*_section()`-style dict reconstruction, or any DB
  save-then-reload round trip);
- reuse, caching, or cost-sensitive behavior (anything where a "skip
  redundant work" decision could be silently defeated downstream);
- parent–child media or data inheritance (anything the child path expects
  to receive correctly from a parent-owned artifact);
- a "validate before generate" (or otherwise explicitly ordered)
  sequencing claim — ordering claims are easy to assert correctly in
  source and easy to silently break in a downstream function the change
  didn't touch;
- an async pickup/status handoff between two independently-scheduled
  Celery tasks (Agent N → Agent N+1 status polling, etc.).

A runtime proof must:

- use local/dev execution — a real local database and real application
  code for the entire internal chain under test, not a mocked-out version
  of the logic being proven;
- stub **only** paid external APIs (Claude, fal.ai, ElevenLabs, Cartesia,
  OpenAI Whisper, Remotion render execution, etc.) — never the internal
  functions whose data-flow correctness is being proven;
- capture the actual persisted or observed values (e.g. the real
  `VideoSection.media_url` after a save-and-reload, the real call count and
  arguments of a generation function, the real order two log lines were
  emitted in) — not merely that a function was called;
- prove the specific field/order/reuse/handoff behavior the phase depends
  on, not just that the code runs without raising.

A static/AST check that confirms "the right functions are called, in the
right order, with no forbidden import" is necessary but never sufficient on
its own for any item in the list above — pair it with a runtime proof.

Worked examples already in this codebase (use these as the template, not
just as historical record):

- **Child remap `media_url` propagation through `_build_beat_section()`** —
  `code_report/phase4e_e1_cost_bug_validation_addendum.md` Parts 1–2: real
  DB-backed fixtures, real `run_visual_generation_for_content()` calls, only
  Claude/Flux stubbed, asserting the actual persisted `media_url` values
  match the expected reuse/generate split.
- **Validate-before-generate ordering in child remap** —
  `code_report/phase4e_e_child_remap_validator.md` §7: a call-order recorder
  wrapping the real `validate_storyboard()` and `generate_beat_image()`,
  proving the actual invocation order rather than inferring it from source.
- **Persistence round-trip checks for media validation** —
  `code_report/phase4e_f_media_validator.md` §6: a real save-then-reload,
  plus a deliberately-injected, immediately-reverted regression to prove the
  detector fires when persistence actually breaks, not just that it stays
  silent when nothing is wrong.

This rule does not relax or replace any existing Testing Rule in §19.1/§19.2
— a runtime proof under this section must still avoid live external API
calls (stub them, exactly as the worked examples above do) and must still
be safe to run repeatedly (clean up every fixture row it creates, exactly as
the worked examples above do).

---

## 20. Claude Code / Codex Work Rules

When asked to modify code:

1. Read `CLAUDE.md` first.
2. Identify exact files and functions before editing.
3. Make the smallest change that satisfies the task.
4. Do not implement adjacent improvements unless requested.
5. Do not run live external API calls.
6. Add or update smoke tests.
7. Return:
   - root cause
   - files changed
   - implementation summary
   - smoke output
   - risks/open questions
8. If architecture changes, update `CLAUDE.md`.
9. If rules change, update the rules section.
10. If behavior changes, update logs and tests.

When asked for investigation only:

- Do not change code.
- Produce a report with file paths, function names, call paths, and risks.
- Mark uncertain points as `UNCERTAIN`.
- Do not guess.

When asked for prompts:

- Return only the prompt if the user requests that.
- Keep prompts implementation-ready.
- Include verification/smoke criteria.

---

## 21. Prompt Engineering Rules

### 21.1 General Prompt Rules

- Each prompt has one responsibility.
- Stable system prompt and dynamic user/context data must be separated.
- Long stable prompts should benefit from prompt caching.
- Every system prompt must expose a version identifier.
- Programmatic outputs should use structured calls.
- Use `call_claude_structured()` for JSON outputs.
- Validate every response in Python.
- Do not rely on "please follow the rules" when Python can enforce the rule.

### 21.2 JSON Output Rules

When Claude returns JSON:

- Use strict schemas when possible.
- Prefer forced tool-use.
- Validate required keys.
- Validate value types.
- Reject unknown keys unless explicitly allowed.
- Validate ranges and enums.
- Raise explicit error on schema mismatch.
- Log truncation and retry if needed.

Prompt text should include:

```text
Return ONLY valid JSON. No markdown. No code fence. No extra keys.
```

### 21.3 Script Prompt Rules

Script prompts must:

- Ground scripts in source material.
- Avoid invented facts.
- Use concrete scenes and actions.
- Respect channel niche and tone.
- Avoid generic documentary clichés.
- Preserve narrative progression.
- Keep section generation focused on one primary turn.
- Keep TTS constraints visible.
- Avoid long sentences.
- End with a strong, relevant comment trigger when required.
- Favor spoken-video delivery over book/documentary-page register (roadmap
  4.3 / audit S-3, §6): present tense for story events wherever the story
  allows it, direct address to the viewer at least once per section,
  mandatory contractions, and a read-aloud test (rewrite any sentence that
  would not be said aloud to a friend).
- Deliver the blueprint's `midpoint_retention_trap` — a reveal placed near
  the story's halfway point — as a targeted constraint on the one body
  section nearest that point, not just passively present in the blueprint.

Script prompts must not:

- invent names, locations, dates, statistics, or source details
- resolve future turns too early
- turn every body section into an abstract essay
- use business logic to decide statuses or retries

### 21.4 Visual Prompt Rules

Storyboard/Flux prompts must answer:

```text
What exact thing would the camera be pointing at right now?
```

Flux prompts should include:

1. concrete subject
2. camera framing
3. specific setting
4. lighting
5. era/context details
6. texture/material detail
7. negative constraints where useful

Avoid:

```text
atmospheric
cinematic
mysterious
eerie
ominous
dramatic
moody
haunting
unsettling
dark mood
generic hallway
generic worried face
generic old house
```

Do not overuse object-only shots.

Every recurring character should have a stable visual identity when story context requires it.

### 21.5 Visual Continuity Rules

Agent 4 visual output should preserve:

- character identity
- location continuity
- era consistency
- style consistency
- emotional escalation
- shot variety

Avoid:

- repeated identical frames too close together
- random new locations with no narrative reason
- random extra people
- distorted faces
- text artifacts in generated images
- cheap horror clichés

### 21.6 Short Episode Prompt Rules

Short episode scripts must:

- be standalone
- use dedicated hooks
- work without parent context
- be optimized for short-form retention
- maintain factual connection to parent story
- avoid `[SECTION N]` markers
- fit expected short duration
- pass TTS checks before validation
- follow the same spoken-video delivery rules as long-form sections (§21.3):
  present tense, direct address, contractions, read-aloud test

Short episode visual remap prompts must:

- use the child narration
- map only to relevant parent beats
- not force reuse when the match is weak
- allow new image generation when needed

---

## 22. Claude Client Usage Rules

Allowed pattern:

```python
from app.services.claude_client import call_claude_structured
from app.services.model_routing import resolve_model
```

Not allowed:

```python
import anthropic
client = anthropic.Anthropic(...)
```

Rules:

- All Claude calls must use shared client helpers.
- All calls must pass `task=`.
- `task` keys must exist in `MODEL_ROUTING`.
- Use `call_claude_structured()` for:
  - scoring
  - planning
  - storyboards
  - remaps
  - any JSON output used by code
- Use `call_claude_with_tools()` only for tool/search use cases.
- Use `call_claude()` only for free-form text where code does not require structured fields.
- Log prompt length, token usage, elapsed time, and truncation.
- If a response hits `max_tokens`, log and reduce scope or retry safely.

**Free-form-JSON migration (live-canary follow-up):** four functions in
`app/agents/agent2_discovery/system_prompt.py` used to call `call_claude()` +
`parse_claude_json()` to produce JSON that code parses/validates
(`generate_native_script()` — `task=native_adaptation`;
`generate_revised_scripts()` — `task=revision`; `assess_script_quality()` —
`task=script_quality_check`; `generate_short_episode_script()` —
`task=short_script`) — a direct violation of the "any JSON output used by
code" rule above, each flagged in its own code comment as
`"Intentional free-form JSON path: retained to avoid changing behavior in a
rule-cleanup pass"` (i.e. already identified and deliberately deferred by an
earlier pass). Two real `--confirm` run failures (§7.1's empty-text-block and
raw-control-character fixes) were both traced to exactly this free-form path,
on `generate_short_episode_script()` specifically — concrete evidence the
deferral was no longer free. All four now use `call_claude_structured()`
with a dedicated `input_schema` (`_NATIVE_ADAPTATION_SCHEMA`,
`_REVISION_SCHEMA`, `_SCRIPT_QUALITY_SCHEMA`, `_SHORT_EPISODE_SCHEMA`) —
forced tool-use has neither failure mode: it scans every content block for
the named `tool_use` block (never assumes `content[0]`), and the SDK
delivers `block.input` as an already-parsed dict, so there is no markdown-fence
stripping or JSON parsing of free text at all. `call_claude`/`parse_claude_json`
are no longer imported in that file. Runtime proof:
`tests/test_agent2_structured_json_migration.py`.

---

## 23. Model Routing Rules

Model choice must match task type.

High-quality/complex tasks:

- story research
- story gate scoring
- story blueprint when quality is critical
- section generation
- short script generation
- quality rewrite
- storyboard generation

Fast/cheap tasks:

- global validation
- script quality check
- shorts planner
- short storyboard remap
- content reformat
- simple suggestions

Rules:

- Do not use a cheap model for tasks requiring nuanced creative output unless quality has been validated.
- Do not use an expensive model for deterministic checks.
- Use Python for deterministic checks.
- `model_override` is allowed only when documented.

---

## 24. Logging Rules

Logs must be sufficient to debug:

- status changes
- retries
- external calls
- token usage
- cost estimates
- idempotency skips
- fallback use
- render paths
- artifact paths
- parent/child content relationships

Important log families:

```text
SCRIPT_COST_ESTIMATE
QUALITY_REWRITE_SKIPPED
FINAL_TTS_BACKSTOP
STORYBOARD_ESTIMATE
STORYBOARD_RETRY_COST
STORYBOARD_FINAL
STORYBOARD_COST_ESTIMATE
HINT_QUALITY_SUMMARY
PARENT_AUDIO_NO_SHORTS
AUDIO_PICKUP
CHILD_SHORT_AUDIO_START
CHILD_SHORT_AUDIO_DONE
PARENT_VISUALS_START
PARENT_VISUALS_DONE
CHILD_SHORT_VISUALS_DEFERRED
CHILD_SHORT_VISUALS_START
CHILD_SHORT_REUSE_STATS
STANDALONE_SHORT_REUSE_STATS
STANDALONE_SHORT_RENDER
STANDALONE_SHORT_RENDER_DONE
PRE_RENDER_ASSET_AUDIT
BEAT_IMAGE_HARD_RETRY
BEAT_IMAGE_NEIGHBOR_REUSED
DARK_CONTRAST_GRADE_DOWNGRADED
DUPLICATE_PROMPT_REPAIRED
PARENT_VISUALS_STALE_SCRIPT_HASH
STALE_VISUALS_CHECK_BACKFILL
TTS_SECTION_DELIVERY_SELECTED
TTS_SECTION_DELIVERY_FALLBACK
```

Rules:

- Warnings must indicate whether pipeline should continue.
- Do not spam per-beat logs unless debugging a beat-level failure.
- Aggregate noisy diagnostics into summary logs.
- Never log secrets.
- Never log raw credentials.
- Log content IDs and child/parent relationships.

---

## 25. Reliability Rules

- Prefer deterministic behavior over creative behavior.
- Validate every Claude JSON response in Python.
- Never trust AI output without schema validation.
- Business rules belong in Python.
- Prompts generate content; code enforces correctness.
- Never send partial data when expecting full regeneration.
- Log token usage, response time, and failures for every Claude call.
- Prompt changes must preserve backward compatibility unless explicitly approved.
- Every system prompt must have a version identifier.
- If Claude cannot determine an answer reliably, return an explicit failure instead of guessing.

---

## 26. Token Budget Rules

- Do not send more than 80% of model context.
- Truncate source material intentionally and log truncation.
- Prefer compact indexes over raw large objects.
- Prefer summaries over raw content when context is too large.
- Log prompt length and estimated token count.
- For storyboards, monitor output tokens per beat.
- If a batch approaches token limit, reduce target beats or split earlier.

---

## 27. Media Rules

### 27.1 Images

- Flux images must be stored locally.
- `media_url` must be local.
- HTTP image URLs must never reach Remotion.
- Text cards are removed (subtitles-only rendering, §11.6/Golden Rule 11):
  every beat is a Flux-generated image, Flux is never asked to render
  readable text, and Remotion draws no text except the subtitle track.
- Flux prompt hash caching is allowed.
- Cache reuse must not create poor viewer experience.
- Reuse parent *prompts* for child shorts only when visually relevant
  (match-score threshold, `remap_beats_for_short()`); the resulting image
  itself is always regenerated fresh at the Short's own portrait size
  (1080×1920) — a child Short must never render the parent's landscape
  pixels (roadmap 3.1 / audit V-2, G-4.2; see §11.4/§13).

### 27.2 Remotion Props

- Props must contain local file paths only.
- Parent props are for `MainVideo.tsx`.
- Child short props are for `Short.tsx`.
- Parent props must not contain parent-cut shorts.
- Validate props before render.
- Fail fast on remote URLs.

### 27.3 Rendering

Parent long video:

```text
MainVideo.tsx
1920x1080
VideoRender(format="main")
```

Child short:

```text
Short.tsx
1080x1920
VideoRender(format="short")
```

Rules:

- Long videos can use chunked render.
- Shorts should render as single vertical videos.
- Verify rendered files with deterministic technical checks when render
  verification is enabled: file existence, ffprobe duration/stream/resolution
  checks, blackdetect, and silencedetect.
- Render verification is not AI quality verification; visual quality and
  creative continuity belong to Agent 4 validation and diagnostics.
- Do not publish unverified renders.

---

## 28. Standalone Short Episode Rules

This is the current shorts architecture.

Rules:

- Shorts are child content rows.
- Shorts are standalone episodes.
- Shorts have their own scripts.
- Shorts have their own audio.
- Shorts have their own Whisper.
- Shorts use remapped parent visual beats.
- Shorts cap non-terminal visual holds with `settings.short_visual_max_hold_ms`
  (default 6000 ms) by advancing the next existing visual beat, without
  changing narration audio or subtitle timing and without creating fake beats.
- Every Short beat's Flux image is generated fresh at portrait size
  (1080×1920), cached under the child's own content ID — never the parent's
  landscape (1920×1080) cache file, even when the beat's `flux_prompt` was
  inherited from a high-match-score parent beat (roadmap 3.1 / audit V-2,
  G-4.2; see §11.4 `generate_pending_beat_images`).
- Shorts render vertically.
- Parent content does not render shorts.
- Parent content does not generate breakpoints for shorts.
- Parent content does not generate rehooks or bridges.
- Parent video cuts are not part of the current architecture.

Forbidden current behavior:

```text
parent audio → semantic breakpoints → parent short cuts
parent storyboard → cut_shorts()
parent VideoRender(format="short")
parent rehook/bridge audio for shorts
```

If any of these reappear, treat it as an architecture regression.

---

## 29. Visual Quality Rules

Agent 4 visual output should feel like a professional video, not an AI slideshow.

Required qualities:

- coherent recurring character identity
- coherent primary location
- strong first frame
- clear visual escalation
- meaningful shot variety
- low object-only filler rate
- low repeated-image rate
- environment diversity where narratively appropriate
- strong subject framing
- zero on-screen text except the subtitle track (subtitles-only rendering)

Diagnostics implemented (emitted by `validate_storyboard()` on every call,
parent and child alike — see §11.4):

```text
VISUAL_REPEAT_RATE     — always logged; % of beats flagged by
                          motif_repetition_in_window or near_duplicate_beat
AI_SLIDESHOW_RISK      — always logged; risk=HIGH if any ai_slideshow_risk
                          issue fired, else risk=LOW
FLUX_PROMPT_QUALITY    — always logged; % of flux_generated beats flagged by
                          subject_presence, environment_presence, or
                          low_information_prompt
FLUX_DUPLICATE_RATE    — always logged; % of flux_generated beats flagged by
                          flux_prompt_exact_duplicate or
                          flux_prompt_near_duplicate
CHILD_SHORT_REUSE_RATE — always logged; % of flux_generated beats that already
                          have a real (non-empty) media_url at validation time
                          — naturally always 0% for parents (see §11.4
                          remap_beats_for_short Ordering rule)
CHILD_SHORT_REUSE_CLUSTERING — always logged; risk=HIGH if any reuse_clustering
                          issue fired, else risk=LOW
CHILD_SHORT_REUSE_STATS — child remap reuse/generate stats (storyboard.py,
                          remap_beats_for_short); not a storyboard-validator
                          output, listed here only to correct prior drift —
                          this was previously mis-documented in this section
                          as STANDALONE_SHORT_REUSE_STATS, a name that was
                          never implemented under that spelling
```

Diagnostics not yet implemented (future work — do not assume these exist):

```text
ENVIRONMENT_DIVERSITY      — underlying checks exist (environment_over_saturation,
                              consecutive_same_environment) but are not exposed
                              under this specific log name
OBJECT_SHOT_RATE           — no check measures the proportion of object-category
                              beats; would require a new validate_storyboard()
                              rule, not yet written
CHARACTER_CONSISTENCY_WARN — no beat field captures character/identity continuity;
                              requires new capability (a new beat field or image
                              analysis), not just a new validator rule
```

Do not optimize visual diversity by forcing random locations. Story logic wins.

---

## 30. Security Rules

- Never commit `.env`.
- Never log credentials.
- Never log decrypted credentials.
- Encrypt credentials before storage.
- Decrypt only at execution/verification time.
- Do not expose bot tokens.
- Do not expose API keys.
- Keep authentication unimplemented until explicitly scheduled, but do not design routes as if they are safe for public deployment.

---

## 31. Environment Rules

Use `.env` only through config/settings.

Expected important environment variables:

```text
DATABASE_URL
REDIS_URL
ANTHROPIC_API_KEY
CARTESIA_API_KEY
ELEVENLABS_API_KEY
OPENAI_API_KEY
FAL_KEY
TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID
FERNET_KEY
REMOTION_PATH
```

Rules:

- Do not read environment variables directly in business logic.
- Use config wrappers.
- Fail clearly when required settings are missing.
- Optional provider keys should degrade only if that provider is not selected.

---

## 32. Documentation Rules

Update `CLAUDE.md` when:

- a new module is introduced
- a new invariant is created
- a pipeline status changes
- a DB table/field changes
- a task handoff changes
- a prompt contract changes
- a provider behavior changes
- a legacy path is removed

Do not use `CLAUDE.md` as a phase log.

Keep current architecture sections current. Remove obsolete sections.

All generated architecture, audit, investigation, and code-review reports must be
written under `code_report/`. Do not place report Markdown files in the repository
root or scatter them across source directories.

---

## 33. Architecture Change Checklist

Before changing a pipeline flow:

- [ ] identify parent vs child behavior
- [ ] identify affected statuses
- [ ] identify affected DB rows
- [ ] identify artifacts created/deleted
- [ ] identify Celery/Beat handoff
- [ ] identify idempotency behavior
- [ ] add logs
- [ ] add smoke tests
- [ ] update `CLAUDE.md`

---

## 34. Current Known Risks

These are current engineering risks to monitor, not future feature promises.

| Area | Risk |
|---|---|
| Duplicate task dispatch | Beat and inline orchestration may enqueue same child if guards fail |
| Stale props | Existing props files may hide newer DB/script/audio changes |
| Visual quality | Character identity and visual continuity need stronger controls |
| Storyboard cost | Storyboard output tokens per beat can be high |
| Child short script quality | Short scripts must not validate with remaining MAJOR TTS issues |
| Legacy fields | Breakpoint/bookend fields still exist and could be misused |
| Concurrency | VideoSection delete/insert patterns need care under parallel runs |
| Deferral loops | Child video can defer forever if parent visual pass fails |

---

## 35. Final Current Architecture Checklist

### Standalone short correctness

- Agent 2 creates child short content rows: required
- Agent 2 creates child short scripts: required
- Child rows become `SCRIPTS_VALIDATED` without waiting for parent audio: required
- Parent Agent 3 generates parent audio only: required
- Parent Agent 3 does not release or enqueue child audio after `AUDIO_DONE`: required
- Child Agent 3 generates child audio and Whisper: required
- Parent Agent 4 generates parent visual beats: required
- Child Agent 4 remaps parent visuals to child narration: required
- Child renders with `Short.tsx`: required
- Child render is `VideoRender(format="short")`: required
- Parent never renders shorts: required

### Visual readiness

- Storyboard cost logs: required
- Reuse stats logs: required
- Character consistency controls: should be strengthened
- Repetition diagnostics: should be monitored
- Cache reuse diagnostics: should be monitored
- Object-shot rate diagnostics: should be monitored

---

## 36. Definition of Done for Pipeline Work

A pipeline change is done only when:

- code path is implemented
- deterministic tests/smokes pass
- logs prove the path is used
- rerun behavior is idempotent
- failure path is explicit
- parent/child behavior is correct
- `CLAUDE.md` is updated if architecture changed
- no obsolete phase/development naming is introduced

---

## 37. Voice choice for the tests

- ElevenLabs: provider=elevenlabs, tts_model=eleven_v3, voice_id=nzFihrBIvB34imQBuxub
- Cartesia:   provider=cartesia,   tts_model=sonic-3.5, voice_id=65209f8e-6140-4a20-b819-3cc2e21da19b

---

End of file.