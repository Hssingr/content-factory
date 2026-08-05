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
→ GENERATING_METADATA
→ METADATA_PENDING_APPROVAL
→ METADATA_APPROVED
```

The four statuses past `RENDERED` (Agent 6 — §11B) are identical, string-for-
string, across all three content shapes (parent, child-of-parent Short, Solo
Short) — unlike the visuals stage, there is no shape-specific defer behavior
here, since neither thumbnail-only-on-youtube-row gating (§5.2) nor
title/description generation depend on which Agent 4 code path produced the
render. `RENDERED` remains the sole readiness signal Agent 6 polls on
(`Content.status == "RENDERED"`, never `VideoRender` row existence) — a
`NEEDS_REVIEW` content item, which can carry real `VideoRender` rows from
whichever language did pass render verification, is deliberately excluded
from Agent 6 pickup and does not auto-progress past it.

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
→ GENERATING_METADATA
→ METADATA_PENDING_APPROVAL
→ METADATA_APPROVED
```

Past `RENDERED`, a child-of-a-parent Short follows the exact same Agent 6
sequence as a parent (§4.1) — only the thumbnail sub-step is skipped for it
(text metadata is generated regardless of render format, §5.2/§11B).

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
| `RENDERED` → `GENERATING_METADATA` → `METADATA_PENDING_APPROVAL` / `METADATA_APPROVED` | Agent 6 |
| any stage → `FAILED` | the agent active at that stage |

No agent writes a status outside its own row in this table. Agent 4 never
writes `RENDERING`/`RENDERED`; Agent 5 never writes
`GENERATING_VISUALS`/`PARENT_VISUALS_DONE`/`CHILD_SHORT_VISUALS_DONE`; Agent 6
never writes `RENDERING`/`RENDERED` and never reads/writes
`PublishSchedule.content_id` (Agent 7's future job, not built yet).

`METADATA_PENDING_APPROVAL` → `METADATA_APPROVED` has two independent
triggers, mirroring the Telegram `PENDING_APPROVAL` → `APPROVED` precedent
(§9.3's `send_for_validation`): a human `POST /api/agent6/content/{id}/approve`
call (bypasses the timer early, §11B), or `check_metadata_auto_approve()`'s
periodic sweep once `Content.metadata_generated_at` is older than
`settings.metadata_auto_approve_seconds` (default 3600). `FAILED` for Agent 6
means **zero `VideoMetadata` rows produced for the content item** — mirroring
`run_video_generation()`'s `successful > 0` convention exactly; a content item
with some, but not all, `(language, platform)` pairs succeeding is *not* a
total failure and still reaches `METADATA_PENDING_APPROVAL` (every per-pair
failure — a transport error, an unknown platform, a missing thumbnail — is
independently non-fatal, logged, and simply leaves that pair's row absent or
partial).

Stuck-state recovery (fresh full-system audit §1.2): `Content.updated_at`
(migration `010_add_content_updated_at.py`, touched by every ORM UPDATE) is
the staleness signal for in-progress statuses. Each Beat pickup also
re-dispatches rows stuck in its stage's in-progress status past a
per-stage threshold (`_STALE_RECOVERY_MINUTES` in `app/scheduler/tasks.py`:
GENERATING_SCRIPTS/GENERATING_AUDIO 60 min, GENERATING_VISUALS 120 min,
RENDERING 180 min, GENERATING_METADATA 90 min — deliberately above
worst-case legitimate runtimes so a live worker is never raced; logged
`STUCK_STATE_RECOVERY`). `GENERATING_METADATA`'s 90 min is derived, not
guessed: up to ~24 sequential `platform_metadata_generation` Claude calls
(6 languages x 4 platforms) plus one thumbnail Flux generation puts
worst-case legitimate runtime around ~40 min, so 90 min leaves genuine
margin (§11B). Stale RENDERING
rows dispatch directly, bypassing the has-render skip (a partial render must
not strand the row). To make both this sweep and Celery's own task retries
actually re-enter the service, the Agent 2/3 task guards accept their own
in-progress status (`GENERATING_SCRIPTS` / `GENERATING_AUDIO`) in addition
to their entry statuses — both services are re-entrant (source scripts
version up, validated rows are reused, on-disk audio/transcripts are
reused).

`VIDEO_DONE` and `GENERATING_VIDEO` are retired — no runtime code path reads
or writes them. They previously conflated Agent 4's visual-generation window
and Agent 5's render window into one ambiguous status with crossover
ownership; this section's split removes that ambiguity. `SCRIPTS_READY` and
`SCRIPTS_VALIDATED_AWAITING_PARENT` are also retired script-stage statuses;
current script/audio pickup uses only `SCRIPTS_VALIDATED`. No compatibility
shim was needed for the retired video statuses: nothing outside the render
pipeline itself ever filtered on `VIDEO_DONE`/`GENERATING_VIDEO` (no Agent
6/publishing consumer existed yet).

### 4.4 Solo Short (`output_mode="shorts_only"`)

A Solo Short — a standalone short episode with no parent at all (§5.3,
§9.6, §11.4) — is itself the primary content unit for its channel and
follows the **same string-for-string status flow as a parent** (§4.1), not
the child-of-a-parent flow's defer-back-to-`AUDIO_DONE` behavior (§4.2):

```text
PENDING_APPROVAL
→ APPROVED
→ GENERATING_SCRIPTS
→ SCRIPTS_VALIDATED
→ GENERATING_AUDIO
→ AUDIO_DONE
→ GENERATING_VISUALS
→ CHILD_SHORT_VISUALS_DONE
→ RENDERING
→ RENDERED
→ GENERATING_METADATA
→ METADATA_PENDING_APPROVAL
→ METADATA_APPROVED
```

Past `RENDERED`, a Solo Short follows the exact same Agent 6 sequence as a
parent. Agent 6's thumbnail gate is `is_short_episode == False`
(§11B) — a Solo Short is `is_short_episode=True`, so like a child-of-a-parent
Short it gets text metadata (title/description/hashtags) for every
configured, verified platform but no thumbnail, driven purely by
`is_short_episode`, never by `parent_content_id`.

The one deliberate difference from §4.1's parent flow: the visuals-done
status is `CHILD_SHORT_VISUALS_DONE`, not `PARENT_VISUALS_DONE` — reused
verbatim from §4.2 rather than a new status, since Agent 5's render-format
decision and `pickup_visual_ready`'s status filter are already
`is_short_episode`-driven, not keyed to which Agent 4 code path produced
the visuals (§11.4). A Solo Short never reverts to `AUDIO_DONE` the way a
child-of-a-parent Short can — that revert exists only inside
`_run_child_short_visuals()`'s parent-readiness gate (§11.4), which a
Solo Short's dispatch never reaches, since it has no parent to wait on.

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
video_metadata
publish_schedule
video_analytics
analytics_anomalies
```

`video_metadata` (Agent 6, §11B) is keyed at `(content_id, language, platform)`
grain — one row per configured, verified platform per language, for every
content shape (parent, child-of-parent Short, Solo Short alike; §11B's Check 4
matrix). `thumbnail_file_path` is set only on the `platform="youtube"` row for
a parent long-form content item — never on a child-of-parent Short or Solo
Short row (both are `is_short_episode=True`, and thumbnail generation is
parent-long-form-only), and never on a non-youtube platform row.
`thumbnail_text` is a **distinct field from `title`** — the short overlay
phrase Pillow burns onto the thumbnail image, never the same value as
`title` and never rendered from `title` — do not assume they are related.

### 5.3 Key Model Invariants

#### `content`

Parent content:

```text
is_short_episode = False
parent_content_id = NULL
short_part_number = NULL
short_total_parts = NULL
```

Child short content (a Short cut from — and remapped against — a parent's own beats):

```text
is_short_episode = True
parent_content_id = parent Content.id
short_part_number = 1-based part index
short_total_parts = total number of parts
```

Solo Short — a standalone short episode with no parent at all
(`output_mode="shorts_only"`, §8.1/§9.6/§11.4): itself the primary content
unit for its channel, discovered and approved exactly like a parent, but
short-form throughout. Exactly one per discovery cycle — never a multi-part
series. Do not confuse this with "standalone short architecture"
elsewhere in this document, which refers to a child-of-a-parent Short being
its own independently-rendered `Content` row (§16.2) — a Solo Short has no
parent to be "standalone" from in that sense; it never had one:

```text
is_short_episode = True
parent_content_id = NULL
short_part_number = 1
short_total_parts = 1
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
- `generated_at` (migration `013_add_audio_generated_at.py`) records when the
  persisted audio generation was produced. Agent 3 refreshes it on regeneration;
  WPM calibration orders by it because the UUIDv4 primary key has no chronological
  meaning.
- `section_boundaries` (nullable JSONB, migration `011_add_audio_file_section_boundaries.py`,
  roadmap Phase B1): real per-`[INTRO]`/`[SECTION N]`/`[OUTRO]` audio spans
  captured at TTS-generation time — `[{"section_type", "section_index",
  "start_ms", "end_ms"}]`. `NULL` for pre-existing rows and for any TTS path
  that can't guarantee one chunk per section. See §10.3 `generate_audio` and
  §11.4 `_remap_beats_timing` for the full producer/consumer chain.

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
│   │   ├── flux_client.py
│   │   └── ...
│   ├── scheduler/
│   │   └── tasks.py
│   └── agents/
│       ├── agent1_setup/
│       ├── agent2_discovery/
│       ├── agent3_audio/
│       ├── agent4_visuals/
│       ├── agent5_render/
│       └── agent6_metadata/
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

**`call_claude_structured()`/`call_claude_structured_with_usage()` reject a
truncated tool_use response instead of silently returning it (live-canary
fix):** a real Agent 1 run (`research_channel_ideas()`, task=`channel_research`)
returned a 200 whose `primary_recommendation` was missing 15 of its 18
required fields, only surfacing downstream as a wall of unrelated-looking
FastAPI `ResponseValidationError` "Field required" errors. Root cause:
Anthropic still returns a syntactically-valid `tool_use` `input` dict when
generation is cut off by `max_tokens` mid-object — it is just missing
whatever properties weren't generated yet — and the block-scanning loop
returned that partial dict unconditionally, the same way it already returns
a real, complete one. `stop_reason == "max_tokens"` is an unambiguous
truncation signal (never set on a genuinely completed `tool_use` block), so
the loop now raises `ValueError` (task/schema/max_tokens/output_tokens in
the message) the moment it detects that condition, instead of a caller's
Pydantic response model discovering the corruption several layers away with
a confusing error. Every other `stop_reason` (`tool_use`, `end_turn`, etc.)
is completely unaffected — this only changes behavior for responses that
were already truncated and silently wrong. `research_channel_ideas()`'s own
`max_tokens` was also doubled 4096 → 8192 at the time (that schema's real
shape — 18 required top-level fields, several long-form text fields, a
`platform_suitability` array with per-platform reasoning, a nested
`editable_config` object — was genuinely too rich for the old budget, not
just occasionally unlucky) — **superseded by the two-call split below**:
`research_channel_ideas()` no longer makes one call against that combined
schema at all, see §8.5. This truncation guard itself remains exactly as
described — it is generic client-layer protection, unrelated to how any one
caller's schema is shaped. Runtime proof:
`tests/test_claude_structured_truncation.py`.

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
  `MODEL_ROUTING` task defaults to Haiku anymore. At the time of this audit,
  that included the `SECONDARY_MODEL`-tier quality gates
  (`script_quality_check`, `short_quality_check`) the audit specifically
  flagged as "the arbiter of your #1 priority is your weakest model" — both
  task keys, along with `quality_rewrite` and `global_validation`, were
  subsequently retired entirely by the Elimination Mandate
  (`code_report/forensic_output_audit_borrasca_run.md`, D1.1/D1.2/D1.3; see
  §9.3/§9.4) — they no longer exist in `MODEL_ROUTING` at all, on any tier.
  `visual_bible_generation` was likewise retired by the same mandate's D2.1
  (see §11.4/§13) — the visual-bible layer it routed is deleted entirely,
  replaced by a deterministic, no-AI-call continuity line. The
  `PRIMARY_MODEL`-tier creative tasks (`script_generation`,
  `native_adaptation`, `storyboard`, `story_blueprint`, `section_generation`,
  `short_script`, plus `story_research`/`channel_suggestion`/
  `channel_research`/`channel_concept_validation`/`publish_timing_suggestion`/
  `story_gate_scoring`) resolve to the configured primary model.
  `publish_timing_suggestion` (prompt engineering audit §2.7) is
  `suggest_publish_timing()`'s own task key, split out of the shared
  `channel_suggestion` key it previously reused — same `PRIMARY_MODEL` slot,
  purely so its cost/latency logs no longer blur with `suggest_field()`'s
  free-text single-value calls (which keep `channel_suggestion`).
  `channel_concept_validation` (two-call research-ideas redesign, §8.5) is
  `research_channel_ideas()`'s step-1 task key, split out the same way and
  for the same reason — same `PRIMARY_MODEL` slot as `channel_research`
  (step 2), purely so a large forced-tool-use schema never again has to be
  satisfied in one call.
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
- Source-material floor check (roadmap 4b / audit P1-5 —
  `check_source_material_floor()`; see §9.3's `fetch_batch` entry).
- `SOLO_SHORT_SCRIPT_FORMAT` (output_mode `shorts_only` roadmap, §9.6) — the
  single shared `script_format` value ("solo_short") every Solo Short call
  site uses in place of the channel's own `ChannelConfig.script_format`:
  `check_source_material_floor()`'s and Agent 4's `_BEAT_SECONDS_BY_FORMAT`'s
  branching both key on `== "youtube_long"`, so any non-`"youtube_long"`
  string already satisfies "not long-form" — this constant exists purely so
  the three Solo Short call sites (`discovery.py`'s floor check,
  `_run_solo_short_script_workflow()`'s floor check, Agent 4's
  `split_into_beats()` call inside `_run_solo_short_visuals()`) share one
  name instead of three ad-hoc string literals.
- Deterministic cleanup utilities.
- `split_sentences()` — the single public sentence splitter (fresh
  full-system audit §3.5): every sentence-level rule in Agent 2/script-checks
  counts sentences with this one tokenizer (abbreviation- and
  closing-quote-aware); do not add another ad-hoc `re.split` on
  sentence-ending punctuation.

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
over the static fallback). Recency uses `AudioFile.generated_at`, never its
random UUIDv4 primary key. The calibration is a live query over existing
`AudioFile` rows; no rolling aggregate is persisted.

Responsibilities:

- `compute_measured_wpm(db, language, is_short_episode=None, channel_id=None)` — rolling
  average of `len(whisper_transcript) / (duration_ms / 60_000)` over the most
  recent real `AudioFile` rows for a language; `None` if fewer than
  `_MIN_SAMPLES_FOR_CALIBRATION` usable rows exist (fail-open).
  `is_short_episode` (fresh full-system audit §3.4) scopes the window to
  parent long-form (`False`) or child Short (`True`) rows via a `Content`
  join — the two run at measurably different rates (~135 vs ~120 wpm), and a
  blended average drifts with whichever kind dominates the recent window.
  `channel_id` scopes samples to the content channel because TTS provider,
  model, voice, and speed settings are channel-owned; script-duration callers
  pass it so unrelated channels cannot contaminate their estimate.
  Callers that know the content kind pass it (script_workflow: parent;
  generate_multilingual_scripts: `content.is_short_episode`;
  split_into_beats diagnostics: parent).
- `get_calibrated_wpm(db, language, is_short_episode=None, channel_id=None)` — the single call site wpm-dependent
  callers should use: the measured rolling average when `db` is provided and
  enough samples exist, otherwise the static `SPEECH_RATES` table (now only
  ever a cold-start fallback).
- `estimate_duration_sec(voice_script, language, db=None, is_short_episode=None, channel_id=None)` — unchanged
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

### 7.6 Shared Script Source Normalization

File:

```text
app/services/script_source.py
```

`normalize_script_source(script_source: str) -> str` — the single canonical
mapping of `ChannelConfig.script_source` aliases (currently only
`"claude_generated"` → `"ai_generated"`) to their V3.2-schema spelling. Pure,
side-effect-free: never queries or writes anything.

Extracted out of `app.agents.agent1_setup.services.v3_config_rules` (§8.2)
specifically so Agent 2 can read the canonical `script_source` value without
importing Agent 1 business logic across the §6A ownership boundary — the
same shared-utility precedent as `app.services.video_sections` (§7.5).
`v3_config_rules.py` imports and re-exports it (`from
app.services.script_source import normalize_script_source`) so existing
callers reaching it as `v3_config_rules.normalize_script_source(...)` keep
working unchanged.

Callers: `app.agents.agent2_discovery.services.discovery` (script-source
branching in `run_discovery()`), `app.agents.agent2_discovery.services.script_workflow`
(the `"ai_generated"` expansion branch in `generate_parent_source_script()`),
`app.agents.agent1_setup.services.v3_config_rules` (executability checks,
§8.2), and `app.agents.agent1_setup.services.activation_readiness` (the
reddit-sources and niche/tone checks, §8.3). See §9.5 for the Agent 2
AI-Generated Story Discovery flow this module enables.

Rules:

- Pure/generic like the rest of §7 — no agent-specific orchestration lives
  here.
- Do not fork a second copy of the alias table — extend
  `_SCRIPT_SOURCE_ALIASES` in this file if a new alias is ever needed.

### 7.7 Shared Recurring-Proper-Noun Extraction (roadmap Phase B2)

File:

```text
app/services/story_entities.py
```

`extract_recurring_proper_nouns(blueprint: dict | None) -> list[str]` — pure,
deterministic, no AI call: capitalized words recurring at least twice across
a story blueprint's own text fields (`hook`/`final_payoff`/
`midpoint_retention_trap`/`central_question`/`major_turns`), English-only
keyword heuristic, capped at 5, ordered by descending frequency then
alphabetically. Returns `[]` when `blueprint` is falsy or nothing recurs.

Extracted out of
`app.agents.agent4_visuals.subagents.storyboard.build_continuity_line_from_blueprint()`
(which now calls this function and only adds its own "Visual continuity:
..." sentence formatting on top — behavior is unchanged, proven by the
pre-existing `tests/test_elimination_mandate_1c_deterministic_continuity.py`
passing unmodified) so Agent 5's caption punctuation-restoration pass
(`app.agents.agent5_render.services.subtitles`, §11A.4) can reuse the exact
same recurring-proper-noun list — the character/place names Whisper is most
likely to mishear are exactly the names a viewer most needs spelled
correctly — without importing Agent 4 business logic across the §6A
ownership boundary. Same shared-utility precedent as
`app.services.video_sections` (§7.5) and `app.services.script_source`
(§7.6).

Rules:

- Pure/generic like the rest of §7 — no agent-specific orchestration lives
  here.
- Do not fork a second copy of the extraction regex/stopword list — extend
  this file if a new field or rule is ever needed, and update both callers'
  documentation (§11.4, §11A.4) if the change affects their behavior.

### 7.8 Shared Flux Client (Agent 6 roadmap Phase A)

File:

```text
app/services/flux_client.py
```

`_call_fal()`, `generate_beat_image()`, and the constants they depend on
(`_ENV_SAFE_PROMPTS`, default width/height, generation/download timeouts)
were extracted here from `app.agents.agent4_visuals.services.flux_generator`
— the exact same shared-utility precedent as `app.services.video_sections`
(§7.5) and `app.services.script_source` (§7.6): a pure, generic,
beat-agnostic function gets moved to `app/services/` the moment a second
agent genuinely needs it, never forked. `flux_generator.py` imports and
re-exports both functions so every pre-existing Agent 4 call site is
unaffected — callers reaching them as `flux_generator.generate_beat_image(...)`
keep working unchanged, matching the same re-export convention
`v3_config_rules.py` already uses for `normalize_script_source` (§8.2).

This exists specifically so Agent 6's thumbnail generation
(`app.agents.agent6_metadata.services.thumbnail`) can call the one true
fal.ai integration point without importing Agent 4's private package across
the §6A ownership boundary — Agent 6 imports `app.services.flux_client`
directly, never `app.agents.agent4_visuals`. §11.5's "one fal.ai
integration point" rule still holds exactly: there is still only one
`_call_fal()` in the whole codebase, just relocated.

`generate_beat_image_with_routing()`, `image_router.py`'s capability table,
and every beat-specific/pixel-defect-healing function
(`_ensure_beat_image_healthy()`, `_detect_image_defect()`,
`_average_pixel_hash()`, `fill_failed_beats_from_neighbors()`) stayed in
Agent 4 — they are genuinely storyboard-sequence-specific (duplicate-
across-beats detection, neighbor-fill fallback, composition-slot rotation)
and do not apply to a single standalone thumbnail with no sequence to be
consistent within. Agent 6 calls `flux_client.generate_beat_image()`
directly with an explicit, fixed `model_key` (its own `_resolve_model_key()`
resolving `"dev"`/`"schnell"` from the same
`IMAGE_ROUTING_ENABLED`/`IMAGE_ROUTING_ALLOW_DEV` kill switches §11.5's beat
router already respects) rather than the beat-oriented routing heuristic,
which reads beat fields (`beat_intensity`, `visual_category`, `script_text`)
a standalone thumbnail has no equivalent for.

Rules:

- Pure/generic like the rest of §7 — no agent-specific orchestration lives
  here.
- Do not add a second fal.ai integration point anywhere — every endpoint
  selection and payload construction for Flux stays behind this module's
  `_call_fal()` and `image_router.py`'s capability table.
- Do not fork a second copy of `generate_beat_image()`/`_call_fal()` for a
  future agent's needs — extend this module, or add agent-specific routing
  logic in that agent's own service (as Agent 6's `_resolve_model_key()`
  and Agent 4's `image_router.py` both already do), never duplicate the
  fal.ai call itself.

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
| `script_source` | `str` (Pydantic `Literal`) | `"reddit"` | **Read by Agent 2** — `"reddit"` (existing discovery default; `fetch_batch()`, web_search-grounded) and `"ai_generated"` (alias `"claude_generated"`; AI-Generated Story Discovery — `generate_story_premise()`/`expand_story_premise()`, no `ChannelSource` required; see §9.5) | `"user_provided"`, `"hybrid"` — reserved; no execution logic exists for either |
| `output_mode` | `str` (Pydantic `Literal`) | `"youtube_and_shorts"` | **Fully executable for all three values** — `"youtube_and_shorts"` (parent render + standalone shorts via `run_shorts_planner`, per §9.4), `"youtube_long_only"` (same parent pipeline; `run_script_workflow()` skips the shorts planner, logged `SHORTS_PLANNER_SKIPPED` — post-roadmap deep audit, the first runtime consumer of this column), and `"shorts_only"` (a Solo Short — one standalone short episode per discovery cycle with no parent Content row at all, written directly from source material; see §9.6/§11.4 and code_report/output_mode_shorts_only_and_youtube_long_only_roadmap.md) | none — this field has no remaining coming-soon values |
| `visual_style` | `str` (free-form, no DB enum) | `"story_driven"` (roadmap 4a / audit P1-9 — was `"documentary"`) | **Read by Agent 2 and Agent 4** — Agent 2 injects it into `generate_story_blueprint()`, `generate_section()`, and `generate_short_episode_script()` user messages as "Visual style:" so narration and hook framing align with the channel's visual aesthetic; Agent 4 injects it into every `generate_storyboard_batch()` call as "Global visual direction:" applying a consistent mood/color/lighting constraint across all beats. See §11 (Agent 4) for the Agent 4 injection contract. | **Reconciled (migration 009):** the former duplicate `video_style_type` column is dropped — its only consumer chain (Agent 5 `channel_style` → props `config.style`) ended in a Remotion prop no component ever read. `visual_style` is the single channel-level style column |
| `image_style` | `str` (free-form, no DB enum) | `"photorealistic"` | **Read by Agent 2 and Agent 4** — Agent 2 injects it into the same three prompt functions as `visual_style` as "Image style:"; Agent 4 injects it into every `generate_storyboard_batch()` call as "Global image style:" applying a consistent Flux rendering approach across all beats. See §11 (Agent 4) for the Agent 4 injection contract. | No per-beat `image_style` override exists; the channel-level setting applies uniformly to every `flux_generated` beat |
| `narration_pov` | `str` (Pydantic `Literal`, roadmap 4a / audit P1-9) | `"third_person"` | **Read by Agent 2 only** — `"third_person"` (standard narrator voice) or `"first_person_storytime"` (narrate as the protagonist, "I"/"me"/"my" — the r/nosleep storytime format). Threaded via `ScriptWorkflowContext` into `generate_story_blueprint()`, `generate_section()`, `generate_shorts_plan()`, `generate_short_episode_script()`, and `generate_native_script()` (native adaptation is instructed to preserve the source's POV, never convert it) as "Narration POV:". Real behavioral rules exist in `_STORY_BLUEPRINT_SYSTEM_PROMPT`, `_SECTION_GENERATION_SYSTEM_PROMPT`, `_SHORTS_PLANNER_SYSTEM_PROMPT`, and `_SHORT_EPISODE_SYSTEM_PROMPT` — this is not a passthrough label. | Not read by Agent 4 — visual storyboard direction does not depend on narration person |

Rules:

- `visual_style`, `image_style`, and `narration_pov` are now wired into
  **both Agent 2 and Agent 4** (narration_pov: Agent 2 only) — do not treat
  them as unread. Agent 2 receives them in `generate_story_blueprint()`,
  `generate_section()`, `generate_short_episode_script()`, and (for
  `visual_style`/`image_style`/`narration_pov`) `generate_native_script()`
  via the `ScriptWorkflowContext` threading chain and
  `generate_multilingual_scripts()`'s own `ChannelConfig` load; Agent 4
  receives `visual_style`/`image_style` in `generate_storyboard_batch()` via
  `split_into_beats()` (see §11.4). `script_source` is now also read by
  Agent 2 — see §9.5 for the `"ai_generated"` branch in
  `run_discovery()`/`generate_parent_source_script()`. `output_mode` is now
  read by Agent 1 (`run_discovery()`/`_create_manual_fallback()`, to set
  `is_short_episode` at Content-row creation time for `shorts_only`
  channels — §9.6), Agent 2 (`run_script_workflow()`'s top-level branch,
  and the existing `youtube_long_only` skip, §9.3), and Agent 4
  (`run_visual_generation()`'s 3-way dispatch, §11.4) — all three
  executable values are now wired in, not just `youtube_long_only`.
  `content_mode` remains schema-only; do not make any agent read it until a
  dedicated phase explicitly wires it in, documents the new behavior here,
  and proves it with a runtime test (§19.4).
- `content_mode`/`script_source`/`output_mode`/`narration_pov` are Pydantic
  `Literal` types in `app/schemas/channel.py` (`ContentMode`/`ScriptSource`/
  `OutputMode`/`NarrationPov`) — the API rejects any value outside the table
  above. `visual_style`/`image_style` are plain strings (no enum), matching
  the existing looseness those descriptors have always had (no DB enum).
- Every Agent 2 `ChannelConfig.narration_pov` read passes through
  `services/narration_pov.py::normalize_narration_pov()`. Canonical values
  pass unchanged; missing config defaults silently to `third_person`; a
  non-empty legacy/unknown value maps to the closest supported value and
  logs `NARRATION_POV_NORMALIZED` at WARNING with channel ID, raw value, and
  result. Migration `014_normalize_legacy_narration_pov.py` repairs the
  observed persisted `first_person` token to `first_person_storytime`; the
  read-time backstop remains for stale snapshots or manually written rows.
- All six columns are `NOT NULL` with a `server_default` (migrations
  `alembic/versions/004_add_v3_channel_config_fields.py` for the first five,
  `008_add_narration_pov_and_story_driven_default.py` for `narration_pov`
  plus the `visual_style`/`video_style_type` default change to
  `"story_driven"`) — additive and backwards-compatible; every existing
  channel row is backfilled with the same default a brand-new row gets,
  with no manual data migration step.
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

**Two combinations are executable today (AI-Generated Story Discovery —
see `code_report/ai_generated_story_discovery_design.md` and §9.5 — and the
Solo Short build, see `code_report/output_mode_shorts_only_and_youtube_
long_only_roadmap.md` and §9.6/§11.4):**
`content_mode="single_story"` + `script_source` in `{"reddit", "ai_generated"}`
+ `output_mode` in `{"youtube_and_shorts", "youtube_long_only", "shorts_only"}`.
Everything else is schema-supported (an operator can already save it) but
not yet executable:

| Field | Executable today | Supported, not executable | Reason |
|---|---|---|---|
| `content_mode` | `single_story` | `limited_series`, `ongoing_series` | Agent 2 has no multi-episode or open-ended series planning/execution logic |
| `script_source` | `reddit`, `ai_generated` (alias: `claude_generated`) — both only when `content_mode="single_story"` | `user_provided`, `hybrid` | `reddit` fetches a real source story via web_search; `ai_generated` synthesizes an original premise from channel niche/tone/description (`generate_story_premise()`/`expand_story_premise()`, no `ChannelSource` needed) — no operator-supplied or mixed script-origin path exists yet |
| `output_mode` | `youtube_and_shorts`, `youtube_long_only`, `shorts_only` | none | `youtube_long_only` skips `run_shorts_planner()` in `run_script_workflow()` (`SHORTS_PLANNER_SKIPPED`); `shorts_only` produces a Solo Short — a standalone short episode with no parent, discovered as `is_short_episode=True` from the start (§9.6) and visually generated via its own original storyboard pass (§11.4), never `run_shorts_planner()` |

`normalize_script_source()` (`app/services/script_source.py` — shared with
Agent 2, see §7.6) maps `"claude_generated"` → `"ai_generated"` — a pure
mapping, never a DB write. The V3.2 schema itself only ever accepts
`"ai_generated"` (the alias only matters for a future internal caller that
bypasses the Pydantic schema). `v3_config_rules.py` re-exports it (imports
it at module level) so existing callers reaching it as
`v3_config_rules.normalize_script_source(...)` keep working.

`validate_v3_channel_config(config: dict) -> dict` returns
`{"executable": bool, "supported": bool, "issues": list[V3ConfigIssue]}`
— one `V3ConfigIssue` (`severity`/`field`/`code`/`message`) per
not-yet-executable or unsupported field, each `coming_soon_reason()`-backed
where applicable.

Rules:

- **`v3_config_rules.py` itself is still never imported by
  `routers/channels.py`, `services/channels.py`, or any Agent 2/3/4/5
  module** — confirmed and tested directly
  (`scripts/smoke_agent1_v3_config_rules.py`). AI-Generated Story Discovery
  (§9.5) does not change this: Agent 2 reads `normalize_script_source()`
  from the shared `app.services.script_source` module (§7.6), never from
  this file directly, preserving the CLAUDE.md §6A ownership boundary
  (Agent 2 must not import Agent 1 business logic). This file's own
  `is_executable_*()`/`validate_v3_channel_config()` functions remain
  Agent-1-activation-only.
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
5. `script_source="reddit"` requires at least one `ChannelSource` row.
   `script_source="ai_generated"` requires none (§9.5 — grounded in
   niche/tone/description only).
6. At least one `ChannelPublishTiming` row exists.
7. At least one `ChannelPlatform` row exists.
8. **Every** existing `ChannelPlatform` row is `verified=True` — not just
   one of them.
9. `output_mode="youtube_and_shorts"` (the only executable output mode
   today) requires a `ChannelPlatform` row with `platform="youtube"`.
10. `script_source="ai_generated"` requires both `Channel.niche` and
    `Channel.tone` to be non-empty (`ai_generated_missing_niche` /
    `ai_generated_missing_tone`) — with no `ChannelSource` fallback for this
    path, they are the only grounding input
    `generate_story_premise()`/`expand_story_premise()` have to work with.

Also collects (non-blocking, `warnings`, never `issues`):

11. A niche↔tone contradiction (roadmap 4a / audit P1-9, §8.7). Does not
    affect the `ready` boolean.

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
`ScheduleSection.jsx`, `PlatformsSection.jsx`,
`CredentialRow.jsx`, `AISuggestionField.jsx`, and `ChannelList.jsx` remain
ordinary editable form components. `BasicInfoSection.jsx` is the exception:
it now owns the Agent 1 Research Ideas UX (§8.5) and, since the frontend/
backend audit of 2026-07-30, also owns the inline Reddit-sources UI
directly (the standalone `SourcesSection.jsx` component was unused dead
code — never imported after this redesign — and was deleted). No new
dependency was added — no Tailwind, no icon library, no animation library; the visual
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

**`BasicInfoSection.jsx` (Concept step) — dropdowns with explanations (Items
16/17; corrected here — these live in `BasicInfoSection.jsx`, not
`ScheduleSection.jsx`, which owns only publish-timing fields):**
`visual_style` and `image_style` are `<select>` dropdowns driven by
`VISUAL_STYLE_OPTIONS`/`IMAGE_STYLE_OPTIONS` in `constants.js` (structured
`{value, label, description}` objects), replacing the previous
`<input type="text">` + `<datalist>` pair. A per-option description line
renders below the selected value. `output_mode` has an `OUTPUT_MODE_DESCRIPTIONS`
sentence rendered below the dropdown explaining what that mode produces.
`narration_pov` (roadmap 4a / audit P1-9) is a fourth dropdown in the same
row group, driven by `NARRATION_POV_OPTIONS` — "Third Person (default)" or
"First Person / Storytime", each with a description line. All four constants
remain in sync with Agent 2/Agent 4's runtime behavior — the dropdown labels
match the values those agents receive. `VISUAL_STYLE_OPTIONS`'s first entry
is `story_driven` (the current `ChannelConfig.visual_style` default); the
prior default value, `documentary`, remains selectable as an ordinary option.

**`VoicesSection.jsx` — honest voice status labels:** "Validated ✓" and
"Not validated" (implying a provider API had been called) were replaced by
"Saved for review ✓" / "Not saved" and "Save Voice ID" button label. The
status bar below the Voice ID field now reads "Voice ID saved — will be
verified when the channel first runs audio generation." This matches the
actual architecture (no provider API call is made; the Voice ID is stored
and used on Agent 3's first TTS run).

**`VoicesSection.jsx` — two voice cards per language (roadmap Phase D1,
gender-aware voice configuration):** each language now renders a "Feminine
Voice" card (recommended default) and a "Masculine Voice (optional)" card,
both independently optional — mirroring the backend's per-`(language,
gender)` `ChannelVoice` row. `App.jsx`'s `voices` state changed from
`{ [language]: {...} }` to `{ [language]: { feminine: {...}|null,
masculine: {...}|null } }`; `handleVoices()` emits a `replaceVoices()` entry
only for a `(language, gender)` pair with a non-empty `voice_id` — a
language may end up with just feminine, just masculine, or both.
`api.replaceVoices()`/the `PUT /channels/{id}/voices` router needed no
change (already a generic `list[VoiceEntry]` passthrough); `VoiceEntry`
gained a `gender: VoiceGender = "feminine"` field (§10.3 has the full
backend consumer chain).

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
- Keep `NARRATION_POV_OPTIONS` in sync with the `NarrationPov` Pydantic
  `Literal` in `app/schemas/channel.py` — unlike `VISUAL_STYLE_OPTIONS`/
  `IMAGE_STYLE_OPTIONS`, `narration_pov` is a closed enum on the backend
  (only `"third_person"`/`"first_person_storytime"` validate), so a UI
  option value outside that pair would be rejected by the API, not silently
  ignored.
- Research results open in a modal dialog (`ResearchDialog`) — do not
  reinline them below the textarea; that was an explicitly-fixed UX defect.

Post-roadmap deep audit changes to this wizard (all implemented):

- **`TONES` covers tension registers**: the dropdown previously offered only
  documentary/conversational/educational/entertaining/investigative — for a
  horror/true-crime niche, the audited P1-9 misconfiguration
  (tone=documentary) was effectively *forced* by the UI, while the
  niche↔tone warning (§8.7) recommended values the dropdown didn't offer.
  It now includes `suspenseful`/`ominous`/`dramatic`/`humorous`/
  `inspirational` as well, and the Agent 1 suggest prompt's tone vocabulary
  (`system_prompt.py`, PROMPT_VERSION 1.4) matches it, with an explicit
  "tension niches need a tension tone" rule. Keep the two lists in sync.
- **`AISuggestionField`'s select renders out-of-list values**: an AI
  suggestion or research recommendation can legitimately set a value outside
  the preset list (the backend columns are free-form) — the select now shows
  it as a "(custom)" option instead of silently displaying the wrong entry.
- **The `shorts_rule` dropdown is removed**: `ChannelConfig.shorts_rule` was
  persisted but read by nothing at runtime — "Never create Shorts" still
  created Shorts (a lying control), and it overlapped `output_mode`. Shorts
  on/off is now controlled by `output_mode` alone (see §8.1/§8.2 —
  `youtube_long_only` is executable and skips the planner). The DB column
  and schema field remain (default `"auto"`, legacy, no migration) but no
  UI control writes them; do not reintroduce the dropdown.
- **Dead constants removed** from `constants.js`: `EMOTIONS`, `MUSIC_STYLES`
  (no music layer exists — §11A.7 is design-only), `SHORTS_RULES`, and
  `USE_CASES` — none had a JSX consumer.
- **`channel_config` dead columns dropped (migration 009)**: `shorts_rule`,
  `subtitle_style_main`, `subtitle_style_shorts` (styles are hardcoded by
  format — main=standard, Shorts=karaoke), `shorts_part_label_style` (the
  part label is env-flag controlled), `runway_enabled` (no Runway
  integration exists), `strict_quality_gate` (its gate was deleted by the
  Elimination Mandate), and `video_style_type`/`video_color_grade` (dead-end
  consumer chain — the props `config` key they fed was never read by any
  Remotion component; the per-beat `VideoSection.color_grade` is the real
  render mechanism, §11A.6). The props `config` key, `VideoConfig` TS type,
  and `channel_style`/`channel_color_grade` builder parameters were removed
  with them. Kept live: `videos_per_week`, `validation_*` (3),
  `subtitle_karaoke_active_color`, `script_format`, `allow_legacy_fallback`
  (repurposed: proportional-fallback tolerance), `audio_tags_enabled`, and
  the six V3 fields. Operator must run `alembic upgrade head`.
- **The ElevenLabs voice-browser chain was removed** (as of this phase, then
  reintroduced — see §8.8): `VoicePicker.jsx` had zero importers at the time,
  so the whole dead chain went with it — `api.getVoices`, the
  `GET /api/agent1/voices` router (`routers/voices.py`), and
  `services/elevenlabs.py` (`get_shared_voices()`). Voice IDs remain
  operator-provided hard inputs (§8) — §8.8's reintroduction does not change
  that; it only helps the operator find a real ID to paste, it still never
  auto-validates or auto-fills without an explicit operator click. The
  `ChannelVoice.music_style`/`use_case` columns remain as legacy nullable
  columns (no migration); nothing reads them.

### 8.5 Research Ideas UX (Agent 1 V3.5c)

Agent 1's Concept step includes a real **Research Ideas** action for
`content_mode="single_story"` setup. The operator enters a rough channel
description, then clicks `✨ Research Ideas`; the frontend calls
`POST /api/agent1/research-ideas` and shows a rich recommendation panel
before the channel is saved or activated.

Backend ownership:

```text
app/agents/agent1_setup/routers/suggest.py      — `/api/agent1/research-ideas`
app/agents/agent1_setup/system_prompt.py        — `research_channel_ideas()` — two sequential structured Claude calls, merged in Python
app/schemas/research_ideas.py                   — request/response models
app/services/model_routing.py                   — `channel_concept_validation` (step 1) and `channel_research` (step 2) Claude task keys
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

**Two-call design (supersedes the original single-call implementation):**
`research_channel_ideas()` makes two sequential `call_claude_structured()`
calls instead of one, then deterministically merges them in Python:

- **Step 1 — `channel_concept_validation`** (`_CONCEPT_VALIDATION_SYSTEM_PROMPT`/
  `_CONCEPT_VALIDATION_SCHEMA`): turns the description (real or, in explore
  mode, the synthetic open-ended brief) into a fully-populated, directly
  executable `editable_config` — channel_name, description, niche, tone,
  script_source, output_mode, visual_style, image_style, languages,
  platforms, videos_per_week, subreddits/story_generation_prompt — plus a
  `description_issues` array (gaps/assumptions the description required
  filling; empty when clear) and a nullable `assumption_note`. 13 required
  leaf fields, `max_tokens=2048`.
- **Step 2 — `channel_research`** (`_RESEARCH_NARRATIVE_SYSTEM_PROMPT`/
  `_RESEARCH_NARRATIVE_SCHEMA`): receives step 1's finalized
  `editable_config` (as `finalized_editable_config`) plus
  `description_issues`/`assumption_note` as context, and analyzes/explains
  that already-decided configuration — `recommended_channel_concept`,
  `why_selected`, `rpm_potential`, `follower_growth_potential`,
  `platform_suitability`, `suggested_channel_names`, `example_video_ideas`,
  `risks_difficulty`, `final_recommendation_summary`, `references_used`. It
  is never given `best_script_source` or any `recommended_output_mode`/
  `recommended_visual_style`/`recommended_image_style`/`recommended_tone`/
  `recommended_target_languages`/`recommended_platforms` field to fill in —
  those don't exist in its schema at all. 10 required top-level fields,
  `max_tokens=8192` (unchanged from the old combined schema — this is where
  all the genuinely long-form prose lives).
- **`_merge_research_steps(concept, narrative)`** combines both into the
  response shape below: `best_script_source` and the six `recommended_*`
  fields are always copied directly from step 1's `editable_config`
  (`output_mode`→`recommended_output_mode`, `visual_style`→
  `recommended_visual_style`, etc.) — Claude is never asked to produce the
  same decision twice. `research_label` is a hardcoded Python constant
  (`_RESEARCH_LABEL`) — it was always the same fixed string, so nothing
  asks Claude to generate it anymore.

**Why this exists (two real production incidents, one root cause class):**
the original single-call schema required ~30 leaf fields (18 under
`primary_recommendation`, 12 more nested inside its own `editable_config`)
in one forced-tool-use response and failed twice with FastAPI
`ResponseValidationError` — Anthropic's forced tool-use does not strictly
enforce a JSON Schema's `required` list. Both times, `editable_config`
itself was always complete and correct; what got dropped was either most of
the narrative fields (a genuine `max_tokens` truncation — see §7.1's
`call_claude_structured()` truncation-guard note) or exactly the six
`recommended_*` fields, each a literal semantic duplicate of one of
`editable_config`'s own fields — Claude appears to treat a top-level value
as redundant once an identical decision already exists elsewhere in the
same response, and simply omits it. The original fix for the second
incident (`_backfill_recommendation_from_editable_config()`, since removed)
patched around this by copying from `editable_config` when the top-level
copy was missing. This two-call split removes the duplication at the
schema level instead: step 2 structurally cannot omit `best_script_source`
or any `recommended_*` field, because it is never asked for them.

The endpoint calls Claude only through the shared `call_claude_structured()`
client pattern. It does **not** call YouTube, TikTok, Instagram, Facebook,
Reddit, credential-verification services, or any scraping/tooling. If
web-enabled Claude research tooling is unavailable, the feature remains a
structured AI estimate based only on the operator's description (or the
pipeline-constraint brief in explore mode) and the current pipeline
constraints.

Required response shape (unchanged from before this redesign — the merge
guarantees byte-compatibility, so the frontend needed no changes) includes:
recommended channel concept, why Claude selected it, qualitative RPM
potential, qualitative follower/subscriber growth potential, platform
suitability for YouTube/TikTok/Instagram/Facebook, best script source
(`reddit` or `ai_generated`), recommended output mode, visual style, image
style, tone, target languages, target platforms, suggested channel names,
example video ideas, risks/difficulty, a final recommendation summary, an
`editable_config` object that maps into the wizard's existing editable
state, and a `references_used` array of well-known subreddits,
publications, or public resources Claude knows from training data (empty
when none apply — real web search is a future phase, not wired yet).
`alternative_ideas` was removed from the schema entirely (confirmed unused
by the frontend — no reader anywhere in `app/ui/src`); re-add only if a real
consumer needs it.

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
- `editable_config.visual_style`/`.image_style` (step 1,
  `_CONCEPT_VALIDATION_SCHEMA`) are **enum-constrained** to the canonical
  preset values (`_VISUAL_STYLE_VALUES`/`_IMAGE_STYLE_VALUES` in
  `agent1_setup/system_prompt.py`, mirroring `constants.js`'s
  `VISUAL_STYLE_OPTIONS`/`IMAGE_STYLE_OPTIONS`) — post-roadmap deep audit:
  a free-string recommendation could previously set a value the setup
  dropdowns could not display. Keep the Python lists and the JS lists in
  sync when adding a preset. `editable_config.output_mode` accepts
  `youtube_long_only` now that it is executable (§8.2);
  `recommended_output_mode`/`recommended_visual_style`/`recommended_image_style`
  are always derived from `editable_config`'s own values (see the two-call
  design above) so they can never disagree with it.
- Do not ask Claude for `best_script_source` or any `recommended_*` field a
  second time — these are always derived deterministically in Python from
  `editable_config`'s own fields (`research_channel_ideas()`'s merge step,
  `_merge_research_steps()`). If a new duplicate-prone field is ever added
  to `editable_config`, add its Python-side derivation there rather than
  asking Claude for two independent copies of the same decision.

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

### 8.7 Niche/Tone Contradiction Check (roadmap 4a / audit P1-9)

File:

```text
app/agents/agent1_setup/services/niche_tone_check.py
```

A real production run showed `Channel.tone="documentary"` configured
alongside `Channel.niche="Reddit horror story narration"`. Every script and
storyboard prompt received "Channel tone: documentary" and correctly obeyed
it — register threading works exactly as designed (§9.3/§21.3) — producing
a neutral, observational script for a channel whose actual content is
horror narration. Nothing validated niche↔tone coherence at configuration
time.

`detect_niche_tone_contradiction(niche, tone) -> list[dict]` is a pure,
deterministic, keyword-based heuristic — no AI call, no DB access. It
matches `niche` against a fixed list of tension/dread keywords (`horror`,
`thriller`, `true crime`, `mystery`, `nosleep`, etc.) and flags a
contradiction only when at least one matches **and** `tone` (case-insensitive,
trimmed) is one of a fixed set of neutral/informational values
(`documentary`, `educational`, `informational`, `neutral`, `analytical`,
`news`, `calm`). Returns an empty list when either input is empty or no
contradiction is detected — never invents a finding.

Wired into `activation_readiness.check_activation_readiness()` (§8.3) as
item 10 — appended to the response's `warnings` list with
`severity="WARNING"`, never `issues`. **This never blocks activation** —
unlike every other check in that function, a niche/tone contradiction is a
flag for operator awareness, not a gate. `ActivationStep.jsx`'s pre-flight
checklist may surface `warnings` entries for operator visibility, but the
backend's `ready` boolean is computed from `issues` only.

Rules:

- Never promote a niche/tone finding from `warnings` to `issues` — that
  would block activation on a heuristic guess, which this module explicitly
  is (keyword matching has known false positives/negatives, same accepted
  tradeoff as the deterministic storyboard-validator heuristics in §11.4).
- Do not add an AI call to this check — the entire point is a free,
  deterministic, no-tokens-spent flag at setup time.
- Do not call this module from Agent 2/3/4/5 — like `check_activation_readiness()`
  itself, it is Agent 1 setup-time tooling only.

### 8.8 Provider Voice-Catalog Browsing (reintroduces, redesigns §8.4's removed ElevenLabs voice-browser chain)

Voice IDs remain operator-provided hard inputs (§8): this feature never
auto-validates or auto-fills a Voice ID without an explicit operator click —
it only lets the operator search the TTS provider's own voice catalog,
paginated and filtered the way that provider's own platform lets you browse
it, instead of needing to already know a raw Voice ID to paste in.

Files:

```text
app/agents/agent1_setup/services/cartesia_voices.py     — Cartesia GET /voices
app/agents/agent1_setup/services/elevenlabs_voices.py   — ElevenLabs Voice Library (voices.get_shared())
app/agents/agent1_setup/routers/voices.py               — GET /api/agent1/voices
app/schemas/voice_browse.py                             — VoiceSummary / VoiceBrowseResponse
app/ui/src/components/tab1/VoicePicker.jsx              — search/filter/paginate/preview UI
```

Backend:

- `GET /api/agent1/voices?provider=cartesia|elevenlabs&search=&gender=&language=&accent=&age=&use_case=&cursor=&page_size=`
  dispatches to `cartesia_voices.list_voices()` or
  `elevenlabs_voices.list_shared_voices()` based on `provider`. `accent`/`age`/
  `use_case` are ElevenLabs-only and silently ignored for Cartesia (that
  provider's catalog has no such filters).
- **Cartesia** (`cartesia_voices.list_voices()`): the installed `cartesia` SDK's
  `Voices.list()` takes no query parameters at all (see
  `cartesia/voices.py` in site-packages) — real pagination/filtering
  requires calling Cartesia's REST `GET https://api.cartesia.ai/voices`
  directly via `httpx`, reusing the SDK's own `X-API-Key`/`Cartesia-Version`
  header convention (`cartesia/resource.py`), with `DEFAULT_CARTESIA_VERSION`
  imported from the SDK rather than a hardcoded duplicate string so it can
  never drift from whatever API version the pinned SDK targets. Params sent:
  `limit`, `starting_after` (cursor), `q` (search), `gender`
  (`masculine`|`feminine`|`gender_neutral`), `language`,
  `expand[]=preview_file_url`.
  **Dual response-shape handling (live-canary fix):** a real first call
  crashed with `AttributeError: 'list' object has no attribute 'get'` —
  Cartesia's current public docs describe a paginated envelope
  (`{"data": [...], "has_more": bool, "next_page": str}`), but the pinned
  `DEFAULT_CARTESIA_VERSION` ("2024-06-10", the same version Agent 3's TTS
  generation already uses successfully) returns a **bare JSON array**
  instead, with none of the sent query params applied server-side.
  `list_voices()` now branches on `isinstance(body, dict)` vs a bare list: a
  dict uses the documented envelope fields directly (forward-compatible with
  a future SDK/version bump); a bare list is treated as the full unfiltered
  catalog, with `search`/`gender`/`language` filtering
  (`_matches_filters()`) and offset-based pagination applied client-side in
  Python so the feature still delivers real pagination/filtering regardless
  of which shape the account's pinned API version returns. `next_cursor` is
  opaque either way — a Cartesia voice ID in the envelope case, a
  stringified offset in the client-side case — and a cursor that doesn't
  parse as either falls back to the first page rather than raising.
- **ElevenLabs** (`elevenlabs_voices.list_shared_voices()`): calls the
  installed SDK's `client.voices.get_shared()` (backed by
  `GET /v1/shared-voices`, the real Voice Library search endpoint — distinct
  from `GET /v2/voices`, which only lists voices already in the operator's
  own account) through the existing shared `elevenlabs_client.get_client()`
  singleton (§7/CLAUDE.md's "no second provider integration point" rule) —
  no new ElevenLabs client construction. Real params used: `page_size`,
  `page` (offset-based, not cursor-based — `next_cursor` is a stringified
  page number), `search`, `gender`, `age`, `accent`, `language`, `use_cases`.
- Both service functions normalize their provider's response into the same
  `VoiceSummary` shape (`voice_id, name, description, gender, language,
  accent, age, preview_url, provider`) and the same pagination envelope
  (`voices, has_more, next_cursor`) so the frontend picker is fully
  provider-agnostic and never needs to interpret what `next_cursor` means.
- Both fail open (empty list, `has_more=False`, logged at WARNING/ERROR) on
  a missing API key or any request failure — same convention as every other
  Agent 1 suggestion call; browsing failing never blocks manually pasting a
  Voice ID.

**Live preview for voices with no static sample (`GET
/api/agent1/voices/preview?provider=cartesia&voice_id=`,
`cartesia_voices.synthesize_preview()`):** ElevenLabs' Voice Library entries
each carry a real, static `preview_url` — the frontend plays it directly,
instantly, for free. Cartesia's account/API version returns **no preview
audio field of any kind** for any voice (confirmed directly against the
installed SDK's own `VoiceMetadata` TypedDict — `id, name, description,
embedding, is_public, user_id, created_at, language, base_voice_id`; no
`preview_url`/`preview_file_url` exists in this API version's voice objects
at all, so the `expand[]=preview_file_url` param above is requested but
never actually returned). Cartesia's own web app previews a voice the same
way its dashboard does — by synthesizing a short fixed phrase on demand with
the real TTS endpoint — so this does the same thing:

- `synthesize_preview(voice_id)` always uses `model_id="sonic-2"` (the
  legacy `_experimental_voice_controls` request shape) regardless of what
  `tts_model` the operator's channel is actually configured with — the
  installed `cartesia` SDK version does not implement the `voice=`/
  `generation_config=` kwargs Sonic 3/3.5 need at all (see
  `agent3_audio/services/tts.py`'s `_cartesia_sdk_supports_generation_config()`,
  §10.3), so passing them here would raise a raw `TypeError`. A Voice ID is
  portable across Cartesia model versions — `sonic-2` is enough to hear what
  the *voice* sounds like, which is all a setup-time preview needs; the
  channel's real generation still uses whatever `tts_model` is actually
  configured.
- Synthesizes one fixed phrase ("Hello, this is a quick preview of this
  voice for your channel's narration.") and caches the resulting WAV to
  `{media_path}/cache/voice_previews/cartesia/{voice_id}.wav` — the phrase
  and model are both fixed, so the same `voice_id` always produces the same
  bytes, safe to reuse across operators/sessions (same reuse philosophy as
  this codebase's Flux image cache, §11.4/§13). A cache hit skips Cartesia
  entirely.
- `voice_id` is validated against `^[A-Za-z0-9_-]+$` before it ever reaches
  a filesystem path or an API call — fails closed on anything else (defense
  against path traversal via the cache filename).
- Fails open (`None` → the router returns `503`) on a missing API key, a
  malformed `voice_id`, or any synthesis failure. The frontend
  (`VoicePicker.jsx`'s `preview()`) only calls this endpoint for
  `provider === "cartesia"` voices that have no `preview_url`; ElevenLabs
  voices always use their real static URL and never reach this endpoint —
  requesting it for a non-Cartesia provider is a `400`, since it would
  indicate a frontend bug, not a runtime condition to tolerate.

Frontend:

- `VoicePicker.jsx` **is** each voice card's "Voice ID" field in
  `VoicesSection.jsx` — there is no separate plain text input anymore. The
  field renders as a select-styled trigger (`.voice-picker-trigger`) showing
  the current value (resolved to a friendly name when that voice happens to
  already be in the fetched result set this session, otherwise the raw ID
  string) or a placeholder when empty, scoped to that card's
  `(provider, language, gender)`. Opening it fetches the first page
  immediately; a debounced search box and a "show all genders" checkbox
  (default: filtered to the card's own gender) re-fetch from page one; a
  "Load more" button appends using the previous response's `next_cursor`.
  Each result shows gender/age/accent/language metadata and a play/stop
  preview button — for a static `preview_url` it plays instantly; for a
  Cartesia voice with none, clicking it calls the live-synthesis endpoint
  above, showing a distinct loading state (`…`) on the button until the
  browser can play the fetched clip, and a dismissible error line in the
  panel if synthesis fails. A "paste a voice ID directly" row inside the
  open panel preserves manual
  entry for an operator who already knows their ID — pressing Enter or
  clicking "Use ID" there calls the exact same `onSelect()`/`changeVoiceId()`
  path a picked search result does, so there is still only one write path
  regardless of how the ID was obtained.
- Reuses `.voice-picker`/`.voice-picker-trigger`/`.voice-picker-dropdown`/
  `.voice-list`/`.voice-row`/`.voice-preview-btn` CSS classes already present
  in `form.css` (left over, unused, from the original deleted
  `VoicePicker.jsx`); `.voice-picker-manual` (the paste-directly row) is the
  only new CSS this phase added.

Rules:

- Do not add a third provider integration point for this — `cartesia_voices.py`
  calls Cartesia's REST API directly (the SDK has no listing params to
  reuse); `elevenlabs_voices.py` must keep going through
  `elevenlabs_client.get_client()`, never construct its own `ElevenLabs(...)`
  instance.
- Do not make voice browsing a prerequisite for saving a Voice ID — an
  operator who already knows their ID must still be able to paste it
  directly (the "paste a voice ID directly" row inside the open panel) and
  never be forced to search/select from fetched results first.
- Do not persist anything from a browse call — `GET /api/agent1/voices` is
  read-only; the only write path remains `PUT /channels/{id}/voices`.
- Keep both providers' normalized output in the same `VoiceSummary` shape —
  do not let `VoicePicker.jsx` grow provider-specific branches for basic
  rendering; provider differences belong in the two backend service
  modules, not the shared picker component.

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
    A[run_discovery] -->|script_source=reddit| B[fetch_batch]
    A -->|script_source=ai_generated, see 9.5| B2[generate_story_premise]
    B --> C[dedup check]
    B2 --> C
    C --> C2[source-material floor check — reddit only, see 9.5]
    C2 --> D[story gate scoring]
    D --> E[Content PENDING_APPROVAL]
    E --> F[send_for_validation]
    F --> G[Telegram APPROVE]
    G --> H[Content APPROVED]
    H --> I[run_agent2_scripts_for_content task]
    I --> J[run_script_workflow]
    J --> J2[generate_parent_source_script]
    J2 -->|ai_generated only| J3[_ensure_ai_story_expanded / expand_story_premise, see 9.5]
    J2 --> J4[source-material floor check]
    J3 --> J4
    J4 --> K[generate_story_blueprint]
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

- Load channel and sources (skipped for `script_source="ai_generated"` —
  see §9.5).
- Fetch a candidate story (`fetch_batch()` for `"reddit"`,
  `generate_story_premise()` for `"ai_generated"` — see §9.5).
- Deduplicate.
- Score story (informational only — see below; never gates persistence).
- Persist parent content and send it for Telegram approval, for every
  candidate that clears dedup + the source-material floor, **regardless of
  the score-gate verdict**.
- Trigger manual fallback only when no candidate could be found/produced at
  all (dedup or floor exhaustion — wording branches on `script_source`, see
  §9.5).

**The score gate never blocks the pipeline (operator decision, supersedes
two earlier designs in sequence — first silent auto-reject, then a
notify-but-still-block fix — both replaced by this one).** A verdict from
`decide_story_acceptance()` is still computed and logged
(`gate_verdict=PASSED`/`BELOW_FLOOR`), but `run_discovery()` no longer
branches on it for persistence: **every** candidate that clears dedup + the
source-material floor is persisted as `Content` (`status="PENDING_APPROVAL"`)
and sent to Telegram, whether it scored 95 or 30. Regenerating a candidate
because an AI judge scored the first one poorly remains forbidden by the
Agent 2 no-quality-retry rule (unchanged) — but rejecting outright is a
distinct, separate decision this codebase no longer makes automatically
either. The human operator is the sole decision-maker: `build_telegram_message()`
(`system_prompt.py`) renders the real `overall_score`, a ✅/⚠️ verdict icon,
every failed gate by name (e.g. `emotional_stakes 45 < 55`) when present,
and the top-2 dimension scores — the operator replies `APPROVE` to proceed
with a low-scoring story anyway, or `CHANGE` (with feedback) to reject it
and trigger rediscovery, using the exact same APPROVE/CHANGE mechanism
every other story already goes through. `run_discovery()`'s 3rd return
value is the **weighted** `story_score` dict from `score_story_assessment()`
(`overall_score`/`dimension_scores`/`failed_gates`/`operator_review_flags`)
— not the raw pre-weighting `{"scores": {...}}` shape `score_story_for_gate()`
returns; `build_telegram_message()`'s `assessment` parameter requires this
weighted shape to render anything (a raw dict has no `overall_score` key
and is silently skipped). `run_discovery()` still returns `None` only when
no candidate could be found/produced at all (dedup/floor exhaustion, or a
hard scoring-call exception) — never because of a low score. Runtime proof:
`tests/test_discovery_never_blocks_on_score.py`.

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
- Must prefer the primary source post/thread over a wiki/fandom/aggregator
  page that describes or summarizes it, even when the secondary page ranks
  highly in search or is itself long — see the "Discovery-time floor gate"
  entry under `run_discovery` below for the deterministic backstop when this
  prompt rule alone isn't followed.

Full source-material fetch (roadmap 4.1 / audit S-1, exec-2):

- **Problem:** discovery previously asked Claude for a `"200-500 word factual
  summary"` as `Story.body`. That summary became the *only* fact source for
  `generate_story_blueprint()`, every `generate_section()` call, the
  since-deleted length-correction expansion path, and Shorts
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
  4,000/6,000/8,000 chars (`generate_story_blueprint()` and
  `generate_section()` in `system_prompt.py`; the auto-correction call sites
  this list originally included were deleted by the post-roadmap deep
  audit). `score_story_for_gate()`'s
  own `[:6000]` cap is deliberately untouched — gate scoring judges fit/
  quality, not narrative grounding, and does not need the full body.
- `call_claude_with_tools()`'s `max_tokens` for `story_research` was raised
  from 4096 to `8192` (this codebase's own established, already-proven output
  ceiling at the time — matched `STORYBOARD_BATCH_MAX_TOKENS` in agent4),
  then to **`16384`** (prompt engineering follow-up — see the multi-part
  stories entry below). Unlike the original 4096→8192 bump, 16384 is not an
  unverified guess: Claude Sonnet 5 supports up to 128K output tokens, and
  since `claude_client.py` never streams (every call goes through the SDK's
  non-streaming path, `_get_client()`'s `timeout=httpx.Timeout(600.0, ...)`),
  the real ceiling to respect is the Anthropic SDK's own non-streaming guard
  (raises before sending if it estimates the request would run past ~10
  minutes wall-clock) — 16384 tokens sits comfortably inside that budget at
  any realistic generation rate. `_STORY_REFORMAT_MAX_TOKENS` was raised to
  match, staying equal to `_STORY_FETCH_MAX_TOKENS` by convention.
- **Multi-part stories (prompt engineering follow-up):** a real operator run
  picked "We Used to Live Here (Part 1)" — a genuine multi-part r/nosleep
  series — and relayed only 428 words (that one part's own length), correctly
  rejected by the discovery-time floor gate (`_fails_source_material_floor()`,
  see `run_discovery` below) but at the cost of discarding a high-engagement
  candidate and paying for a whole new discovery search. `_SINGLE_STORY_SYSTEM_PROMPT`
  now has an explicit "Multi-part stories" rule: detect a serialized title
  ("Part N", "Chapter N", etc.), search for and retrieve every part before
  answering, and return the **combined verbatim text of every part** as
  `body` (canonical `url` = the earliest part's URL; `title` describes the
  whole series, not just the part first found). This is why `max_tokens` was
  raised — one response can now legitimately carry several parts' worth of
  body text. The discovery-time floor gate remains the safety net for
  standalone stories that are genuinely too thin, or for a series whose later
  parts don't exist yet/can't be found — the prompt explicitly forbids
  inventing missing parts to fill the gap.
- `max_rounds` for the same `story_research` call is capped at `8`, down from
  `20` (roadmap 6.2 / audit C-4). Once this call only has to find and relay a
  story via `web_search` — no longer condense it into a summary — it does
  not need 20 tool-use round-trips to converge; `max_rounds` is a hard safety
  cap (`call_claude_with_tools()` raises `ValueError` if exceeded, caught by
  `fetch_batch()`'s own `except Exception: return []`), so this is a
  deliberate cost reduction, not a quality change. Unchanged by the
  multi-part rule — the model can issue multiple searches within a single
  tool-use round (a real run's first round alone made 22 server-side tool
  calls), so finding additional parts of a series does not obviously need
  more rounds; only the per-round output-token ceiling needed raising.
- `models/content.py` required **no schema change/migration** —
  `Content.source_excerpt` was already an unbounded `Text` column; the fix is
  entirely prompt + truncation-constant changes.
- Story gate feasibility dimension (roadmap 4.5 / audit S-7): the old
  `stock_media_feasibility` score/floor is retired because this pipeline no
  longer searches Pexels/Unsplash/Pixabay stock media; visuals are generated.
  Agent 2 asks for and gates on `image_generation_feasibility`: whether
  the story's key moments can be depicted as distinct concrete generated
  images. The legacy key aliases (`stock_media_feasibility`,
  `emotional_tension`, `controversy_or_debate_potential`) are removed — no
  producer exists; every assessment comes fresh from `score_story_for_gate()`'s
  forced tool-use schema.
- Story gate slimmed 19 → 8 dimensions, 12 → 5 pass conditions (fresh
  full-system audit §2.5): four near-duplicate pairs merged into their
  strongest member, the five ≤0.02-weight dimensions dropped. Weighted
  dimensions (sum 1.0): visual_storytelling_potential .20,
  scroll_stopper_potential/emotional_stakes/central_mystery/
  social_media_clickability .15 each, conflict_or_contradiction/
  image_generation_feasibility .10 each; `rights_ip_risk` stays unweighted.
  Floors: overall ≥65; visual_storytelling ≥55; emotional_stakes ≥55;
  scroll_stopper ≥55; image_generation_feasibility ≥40 — every floor is a
  chance for one noisy LLM dimension to reject a usable story, so only the
  non-negotiables kept one.
- Story gate rights/IP dimension (roadmap Phase 5 P3-5): new scoring
  payloads must include `rights_ip_risk` (0 = low apparent rights risk,
  100 = high apparent monetized-adaptation/takedown risk such as famous
  authored fiction, franchises, named characters/worlds, or rights-managed
  creepypasta). It is **not** part of `_DIMENSION_WEIGHTS`, does not affect
  `overall_score`, and never adds a `failed_gates` entry. When
  `rights_ip_risk >= 70`, `score_story_assessment()` emits an
  `operator_review_flags` entry and `build_telegram_message()` surfaces a
  separate rights/IP review line so the operator can decide before approval.
  Do not turn this heuristic into an automatic legal/copyright rejection gate.

Source-material floor at the discovery→script handoff (roadmap 4b / audit
P1-5): the roadmap-4.1 fix above ("relay the full verbatim post text, do NOT
condense") did not survive contact with reality — a real production run
still logged `body_chars=3070` for a ~45,000-word novella (`web_search`
returns snippets and the model summarizes regardless of the prompt
instruction), and no floor existed downstream to catch it. Consequence: a
1,384-word documentary was expanded from a ~560-word summary — every "detail"
beyond that thin source was necessarily model-invented, and no validator can
check faithfulness against a source that isn't there.

- `app/services/script_checks.py`'s `check_source_material_floor(source_excerpt,
  language, script_format)` is a pure, deterministic word-count check —
  mirrors `check_minimum_length()`'s own per-format floor immediately above it
  in that file: the source must carry at least as many words as the shortest
  acceptable script (900 words for `script_format="youtube_long"`, 420 for
  any other format). A script cannot be responsibly grounded in a source
  thinner than the shortest script it is itself allowed to produce.
- Wired into `app/agents/agent2_discovery/services/script_workflow.py`'s
  `generate_parent_source_script()` — the single shared blueprint→sections→
  quality-gate→persist entrypoint (see below) — via
  `_passes_source_material_floor()`, called first, before `_build_story()`,
  before `_mark_generating_scripts()`, and before any Claude call. On a
  failing check it logs `SOURCE_MATERIAL_FLOOR_FAILED` at ERROR, sets
  `Content.status = "FAILED"`, commits, and returns `None` — the
  discovery→script handoff fails loud rather than spending a blueprint/
  section/quality-gate Claude call grounding a full script in material that
  cannot support it. This is the "fail the handoff" branch of the roadmap's
  documented either/or; deterministic duration-scaling ("scale duration to
  source volume" — i.e. dynamically shrinking the target word count instead
  of failing) was not implemented, since it would require threading a
  per-content word-count ceiling through blueprint generation, section-count
  planning, and every quality-gate length constant, for a channel-level
  target the operator explicitly configured — failing loud and letting
  discovery try the next candidate story is the simpler, lower-blast-radius
  fix consistent with this phase's scope.
- Deterministic Reddit `.json` fetch remains **not implemented**, unchanged
  from the roadmap-4.1 rationale above (no Reddit API key available, and
  unauthenticated/datacenter-IP requests are increasingly blocked) — this
  phase does not revisit that trade-off; it only stops a too-thin source from
  silently reaching script generation.
- Runtime proof: `tests/test_source_material_floor.py` — unit coverage for
  `check_source_material_floor()`'s per-format floor, and a full-chain proof
  that a thin `source_excerpt` reaches `generate_parent_source_script()` and
  fails before `generate_story_blueprint()`/`generate_script_sections()`/
  `run_script_quality_gate()` are ever called (each poisoned to raise if
  touched), while a well-grounded source proceeds to `GENERATING_SCRIPTS`
  exactly as before this change.

**Discovery-time floor gate (prompt engineering follow-up) — the same check
now also runs before Telegram approval, not only after it.** A real operator
run showed the post-approval check above catching a genuinely bad pick only
after the operator had already spent their attention approving it: `run_discovery()`
accepted a candidate whose `source_excerpt` turned out to be a fandom-wiki page
*describing* an 8-part r/nosleep series (527 words), not the series itself —
Claude's `web_search` cited a secondary summary page instead of the primary
Reddit thread, so even the "relay full verbatim text, do not condense"
instruction in `_SINGLE_STORY_SYSTEM_PROMPT` had nothing substantial to relay.

- `_SINGLE_STORY_SYSTEM_PROMPT` (`fetcher.py`) gained an explicit rule: prefer
  the primary source itself over a wiki/fandom/"best of"/news page that
  describes or reviews it, even when that secondary page ranks highly in
  search results or is itself long enough to look substantial.
- `run_discovery()`'s targeted-retry loop and `_nuclear_retry()` both now call
  `_fails_source_material_floor()` (a thin wrapper around
  `check_source_material_floor()`, same 900/420-word floors) on every
  candidate immediately after the existing duplicate check, before it can
  become `story` and reach `Content`/Telegram creation. A failing candidate is
  rejected exactly like a duplicate — appended to `accumulated_rejected` with
  a `feedback` string ("source material was too thin... find a different
  story and relay the actual primary source text") threaded into the next
  `fetch_batch()` call's exclusion block — and retried through the same
  ladder (2 targeted retries → nuclear retry → manual Telegram fallback) the
  duplicate-detection path already used.
- This does **not** replace the post-approval check in
  `generate_parent_source_script()` above — that one remains the backstop for
  the manually-submitted-story path (`_handle_manual_story_input()` in
  `validation.py`), which never goes through `run_discovery()`'s retry loop
  and so never passes through this gate. The two checks are the same
  deterministic function at two different points in the pipeline, not
  duplicated logic with drift risk.
- Runtime proof: `tests/test_discovery_source_material_floor_gate.py` — real
  `run_discovery()` against a fake DB, with only `fetch_batch()` (paid
  `story_research`) and `score_story_for_gate()` (paid `story_gate_scoring`)
  stubbed; proves a thin candidate's title/url/feedback reach the next
  `fetch_batch()` call's `rejected_stories` and that the persisted `Content`
  row's `source_excerpt` is always the eventually-accepted rich candidate's
  body, never the rejected thin one's; and that an all-candidates-thin run
  (including the nuclear retry) falls through to the manual fallback with no
  `Content` row ever created.

**Both floor checks above are `script_source="reddit"`-only.** Neither the
discovery-time gate (`_fails_source_material_floor()` in `run_discovery()`'s
retry loop / `_nuclear_retry()`) nor the initial post-approval check in
`generate_parent_source_script()` (before the AI-generated-story-expansion
step below) applies to `script_source="ai_generated"` — a short premise is
short by design at discovery time, and `generate_parent_source_script()`
expands it into a full story via `_ensure_ai_story_expanded()` *before*
running this same `check_source_material_floor()` on the expanded body. See
§9.5 for the full AI-Generated Story Discovery design.

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
- A non-APPROVE reply (CHANGE) is **story-level feedback** (fresh full-system
  audit §1.3): `_handle_change()` rejects the story (validation REJECTED,
  content FAILED), re-dispatches `run_agent2_for_channel` with the rejected
  story *and the operator's feedback text* in `rejected_stories` (fetch_batch
  renders it as an "Operator feedback:" line in the exclusion block), and
  replies honestly (`STORY_FEEDBACK_REDISCOVERY`). After
  `validation_max_revisions` consecutive rejections the configured limit
  policy applies. The old revise-the-script flow (generate_revised_scripts,
  `revision` task key) was unreachable dead code — no script exists while a
  validation is PENDING — and is deleted; do not pretend script revision
  exists when no script exists.
- `build_telegram_message()` (`system_prompt.py`) includes a deterministic
  2-3 sentence **preview** line (`_build_telegram_preview()`, first
  `split_sentences()`-derived sentences of `Content.source_excerpt`, no
  Claude call) between the `Source:` and `Top signals:` lines whenever
  `source_excerpt` is non-empty — the operator otherwise sees only a title
  and a source label with no sense of what the story is actually about,
  which is a hard blocker for `script_source="ai_generated"` specifically
  (there is no URL to click for that path; the source label is a static
  "AI-generated original story premise" string). `send_for_validation()` and
  `_handle_manual_story_input()` both pass `content.source_excerpt`; the
  parameter defaults to `""` (omitted from the message) for any caller that
  doesn't have it.
- `test_pipeline/test_full_pipeline.py`'s `_run_telegram_approval()` mirrors
  this same APPROVE/CHANGE branching, not just APPROVE — a real gap found
  after the AI-Generated Story Discovery feature (§9.5) shipped: the
  harness's polling loop previously marked content `APPROVED` regardless of
  what the operator actually typed back, silently discarding a CHANGE
  reply's feedback and proceeding to script generation on a story the
  operator had just rejected. It now rejects the candidate exactly like
  `_handle_change()` (`Content.status="FAILED"`,
  `ContentValidation.status="REJECTED"`, feedback appended to
  `script_issues_log`), calls `run_discovery()` again in-process with the
  feedback threaded into `rejected_stories`, sends the new candidate for
  approval, and repeats — chatbot-style iteration, capped by the same
  `validation_max_revisions` config the production path uses (then
  auto-approves the last candidate, mirroring `_apply_limit_policy()`'s
  default). See §9.5 for the AI-generated-story interaction this uncovered.

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
- Call `run_shorts_planner` after parent `SCRIPTS_VALIDATED` — unless
  `ChannelConfig.output_mode == "youtube_long_only"`, which skips the
  planner entirely (`SHORTS_PLANNER_SKIPPED`, post-roadmap deep audit).

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
- character_descriptors (roadmap Phase C2)
- era_setting (roadmap Phase C3)
- protagonist_gender (roadmap Phase D1)

Rules:

- Use structured Claude call.
- Python validates required fields.
- Major turns drive section progression.
- Business logic remains in Python.
- `major_turns` schema carries `"maxItems": 5` (prompt engineering audit
  §2.6), matching the prompt's own stated "2-5 narrative turns" rule
  structurally instead of leaving the ceiling to the model's self-discipline
  — `suggested_section_count` was already clamped to `[2, 5]` in Python, but
  `major_turns` itself was not.

`midpoint_retention_trap` (roadmap 4.3 / audit S-3, §6): one concrete reveal
or counterintuitive fact, distinct from `final_payoff`, placed at roughly the
story's halfway point — the highest-leverage completion-rate lever current
research identifies. (The former 25%/60% mini-hook placement rule was deleted
from the section prompt — per-section generation cannot know total word count
or which section lands at those marks; this Python-targeted midpoint trap is
the correct mechanism for placed retention beats.)
`generate_story_blueprint()` does not post-process or validate its content
(unlike `major_turns`'s ≥2-entry check) — an empty value is tolerated
fail-open by `generate_script_sections()` below (no constraint is injected).

`narration_pov` (roadmap 4a / audit P1-9): threaded alongside
`visual_style`/`image_style` as "Narration POV:" in the user message. The
system prompt instructs phrasing `hook`/`central_question`/`final_payoff` so
they can be delivered in the configured POV — first-person ("I heard it
three nights in a row") for `"first_person_storytime"`, or the default
third-person register otherwise. See §8.1 for the full threading chain.

`character_descriptors` / `era_setting` (roadmap Phase C2/C3, operator
video-output audit): a real production run showed a story's protagonist
rendered as 6+ visually different women across beats, and a separate run
shipped modern anachronisms (a modern flag, a modern waterfront, contemporary
clothing) inside a 6th-century Byzantine story — nothing locked character
appearance or period/setting across the storyboard. Both fields are generated
ONCE at blueprint time, piggybacking on the existing blueprint Claude call
(`max_tokens` raised 1024 → 1536 for the added headroom) — zero extra AI
calls, and NOT a revert of the deleted visual bible (Elimination Mandate
D2.1, §11.4) — deterministic, blueprint-derived data consumed by Python, no
per-batch generation call.

- `character_descriptors`: up to 5 structured entries for recurring NAMED
  characters. Each requires name, apparent age band, build, hair color/length/
  style, facial hair, distinguishing facial features, clothing garment/colors,
  and one signature accessory. Python serializes these fields in a fixed order
  and injects that exact line into every matching person beat. Required in the
  schema, but a
  legitimately empty list (`[]`) is valid for an event-focused/ensemble
  story with no describable individual character — never forced.
- `era_setting`: one phrase naming the story's historical period AND
  physical setting (e.g. "6th-century Byzantine Constantinople"). Required
  in the schema; the model estimates it even for a story that doesn't state
  it explicitly ("contemporary/present-day, unspecified U.S. city" for a
  modern story).
- Neither field is post-processed or validated in Python beyond passthrough
  (same fail-open convention as `midpoint_retention_trap` above) — consumed
  deterministically by Agent 4's `build_continuity_line_from_blueprint()`
  (§11.4) via the existing `continuity_line` mechanism, no new parameter
  chain needed anywhere downstream.
- A blueprint generated before this phase shipped simply lacks these two
  keys — every consumer treats a missing key as "nothing to add," never
  forcing a regeneration of legacy content.

`protagonist_gender` (roadmap Phase D1, gender-aware voice configuration):
`"feminine"` | `"masculine"` | `"unspecified"` — the gender of the story's
central figure (whoever the story is centrally about, or who narrates it in
first-person mode), generated once at blueprint time on the same Claude call
(no extra AI cost). `"unspecified"` is returned only when there is
genuinely no single clear protagonist (an ensemble cast, or an event-focused
story). `generate_story_blueprint()` returns this value completely
untouched — normalization (`"unspecified"`/missing → `"feminine"`) is
`_resolve_target_gender()`'s job in Agent 3 (§10.3), not this function's;
inspecting the raw blueprint always shows exactly what Claude returned. See
§10.3 for the full consumer chain and §8.1/§8.4 for the `ChannelVoice`
gender configuration this drives. Native adaptation resolves the same value
for parent and child content (children inherit the parent blueprint when
needed), passes it as `Narrator gender (for grammatical agreement)`, and
requires participle/adjective agreement for both narrator and named
characters. French adaptation additionally prefers natural spoken French
over literal calques.

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
- Preserve section progression.
- Inject the blueprint's `midpoint_retention_trap` into exactly one body
  section (roadmap 4.3 / audit S-3, §6).

**No section-level regeneration retry (Elimination Mandate, roadmap
`code_report/forensic_output_audit_borrasca_run.md` D1.4):** each section
(`_generate_section_once()`) is exactly one Claude call. A real production run
showed the deleted retry loop's corrective override instruction (built from
TTS/hook/rhythm/transition findings) pushed every retried section toward
shorter, choppier sentences, producing a machine-gun monotone across the whole
script — the retry mechanism was actively hurting narration quality, not
improving it. The deterministic TTS backstop (`normalize_tts_chars`/
`split_long_sentences`) still runs on every section — a mechanical text fix,
not a regeneration. TTS/hook/rhythm/transition findings from
`_collect_section_issues()` are logged (WARNING for MAJORs, INFO for MINOR
rhythm/transition findings) and never trigger a second generation call. The
narrative-completeness retry pass (which used to regenerate INTRO/OUTRO/a
body section targeted at whichever `check_narrative_completeness()` finding
failed) is deleted for the same reason — see
`_log_narrative_completeness_telemetry()`: the pure-Python completeness check
still runs and its findings are logged, but never regenerate a section.

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
  `_generate_section_once()` → `_call_section_generation()` →
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

Narration POV wiring (roadmap 4a / audit P1-9): `narration_pov` is threaded
the same way `visual_style`/`image_style` already were, not as a new
per-function parameter chain of its own —
`_build_section_generation_context()` stores it in the shared `context`
dict, and `_generate_intro_section()`/`_run_body_section_loop()`/
`_generate_outro_section()` all read `context.get("narration_pov",
"third_person")` when calling `_generate_section_once()` →
`_call_section_generation()` → `generate_section()`. Every section of one
script receives the same value — there is no per-section POV override.
`_SECTION_GENERATION_SYSTEM_PROMPT` carries a real behavioral rule (not a
passthrough label): `"third_person"` narrates about the story's people
using third-person pronouns and names; `"first_person_storytime"` narrates
AS the protagonist using "I"/"me"/"my" throughout, with other people in the
story still referred to in third person from the protagonist's perspective.
Never mixed within one section.

Spoken-video delivery rules (roadmap 4.3 / audit S-3, §6): both
`_SECTION_GENERATION_SYSTEM_PROMPT` and `_SHORT_EPISODE_SYSTEM_PROMPT` (system_prompt.py)
now instruct: present tense for story events wherever the story allows it (vs.
past tense), direct address to the viewer ("you" / a rhetorical question)
at least once per section, mandatory contractions, and a "read-aloud test"
(rewrite any sentence that would not be said aloud to a friend). This is a
prompt-only change — no deterministic Python check enforces register (unlike
the existing TTS/word-count/hook checks), since spoken-register quality is a
subjective judgment call, not a pass/fail structural property.

Hook by construction, not by validator (roadmap 4c / audit P1-4): a real
production run showed the blueprint's own `hook` generated — already shaped
to pass `check_hook_quality()`'s rules (≤15 words, concrete, withheld
mechanism) — but buried at sentence 6 of the assembled INTRO, with the
script instead opening on a "character roster" sentence
("A boy named Sam Walker moved to Drisking, Missouri as a kid. He had a
sister named Whitney...") that `check_hook_quality()` passed anyway, since
it too was concrete and ≤15 words. The validator checks the *shape* of
whichever sentence happens to come first, not *which* sentence that should
be — a false negative by design.

- `_apply_hook_by_construction(result, hook)` (`scripts.py`) is a pure,
  deterministic transform: it prepends the blueprint's `hook` string as the
  script's literal first sentence (adding terminal punctuation if missing),
  and is a no-op when `hook` is empty, the generated text already opens with
  it verbatim, or the opening sentence is already a close paraphrase of it
  (avoids a duplicated hook). It never removes or rewrites any of Claude's
  generated content — the previous opening sentence(s) simply move to second
  position onward, exactly like the audited roster sentence now would.
- **Paraphrase no-op (live-canary fix):** a real production run showed the
  original verbatim-only `startswith()` check miss a paraphrase — Claude's
  own INTRO opened "...planning to steal from her family for one single
  night" against a blueprint hook of "...planning one night's theft" —
  different wording, same content, so the hook got prepended anyway,
  producing a duplicate-idea stutter as the video's literal first two
  sentences. `_opens_with_hook_paraphrase(script_text, hook)` closes this:
  token overlap (words >3 chars, ≥70% of the hook's content tokens,
  `_HOOK_PARAPHRASE_OVERLAP_THRESHOLD`) between the hook and just the
  generated opening sentence (`split_sentences()`), reusing
  `_get_content_tokens()` — no AI call. Runtime proof (real production
  strings, not synthetic): `tests/test_hook_by_construction.py`.
- **Multi-sentence hook window (live-canary fix,
  `code_report/render_pipeline_media_assembly_audit_2704ad21.md`):** a real
  production run shipped a **two-sentence** blueprint `hook` ("I swore my
  father's oath at nine. Now I stand on a mountain that wants to bury forty
  thousand men.") whose generated INTRO opened with a paraphrase spanning
  its own first two sentences. Comparing the full two-sentence hook against
  only the generated text's first sentence structurally capped achievable
  overlap at ~36% of the hook's tokens — under the 70% threshold even
  though the opening fully covered the hook's content — so the hook was
  prepended again, shipping the exact "said twice" duplicate this mechanism
  exists to prevent. `_opens_with_hook_paraphrase()` now sizes its
  comparison window to the hook's own sentence count
  (`split_sentences(hook)`) rather than a fixed single sentence — an
  *N*-sentence hook is compared against the generated text's first *N*
  sentences. A single-sentence hook (every pre-existing case) reproduces
  the exact prior behavior unchanged (window size 1).
- Wired into `_generate_section_once()` — called immediately after the raw
  Claude section result is returned and before `_clean_generated_section()`'s
  TTS backstop, gated on the same `check_hook: bool` flag that already
  identifies "this is the INTRO" (only `_generate_intro_section()` passes
  `check_hook=True`; body sections and OUTRO pass `check_hook=False` and are
  untouched). Running it before the TTS backstop means the prepended hook
  text is normalized/length-checked along with the rest of the section, not
  bypassed.
- `check_hook_quality()` itself is unchanged and keeps running as telemetry
  inside `_collect_section_issues()`/`_collect_quality_gate_issues()`/
  `check_narrative_completeness()` — per the Elimination Mandate (D1.4/D1.5),
  none of these call sites have triggered a regeneration since before this
  phase. This phase does not change that; it only makes the sentence the
  check observes deterministic instead of left to Claude's discretion. Even
  a blueprint `hook` that itself fails the check's own ≤15-word rule is still
  constructed as the opening line and still never triggers a retry — the
  check remains observability-only, exactly as the roadmap requires.
- Runtime proof: `tests/test_hook_by_construction.py` — unit coverage for
  `_apply_hook_by_construction()`, plus a full-chain proof through the real
  `_generate_intro_section()` reproducing the exact audited defect shape
  (Claude opens on a roster sentence) and confirming the assembled INTRO
  opens with the blueprint hook instead, with exactly one Claude call (no
  retry) in every case including the failing-hook case.
- `check_narrative_completeness()`'s comment_trigger check (prompt
  engineering audit §2.7) gained a fifth, shape-only check alongside its
  existing presence check: `comment_trigger` must be ≤20 words and end with
  `?` — both stated as rules in `_STORY_BLUEPRINT_SYSTEM_PROMPT` but
  previously unverified by anything (the pre-existing check only compared
  `comment_trigger` against the OUTRO's actual last sentence via 50% token
  overlap, which tolerates a 40-word, question-mark-free trigger as long as
  the OUTRO echoes it closely). Telemetry only, same as every other finding
  in this function — no retry, no regeneration.

Prior full-text continuity (real production defect — SECTION 4 daughter-fate
contradiction): `prior_sections_summary` only ever carried each section's
`{label, summary, reveals, open_questions}` into later `generate_section()`
calls — never the section's literal `script_text`. A real production horror
script shipped internally contradictory: blueprint turn[2] said "the father,
exposed as his own daughter's killer" (murder), but SECTION 4 said the
daughter "was alive when firefighters broke through the hatch" (imprisonment,
not murder) — the compressed summary/reveals never captured the specific
wording SECTION 2 committed to, so SECTION 4 had no way to see it.

- `generate_section()` (`system_prompt.py`) gained `prior_full_text: str =
  ""`, threaded into the user message as a new "Prior sections (full text —
  established continuity; do not contradict any fact, character state, or
  fate stated here)" block, placed immediately after the existing "Prior
  sections summary" block — additive to, not a replacement for,
  `prior_sections_summary`, which still feeds `prior_summary_text` (the last
  section's summary) into `check_section_transition()`'s recap-detection,
  unchanged.
- No new state field was added to `_create_section_loop_state()`:
  `state["sections"]` already accumulates every generated section's final
  `{label, script_text, title?}` in order, only ever appended to (via
  `_append_generated_section()`) after that section's Claude call already
  returned — so at the moment each later `_generate_section_once()` call is
  made, `state["sections"]` already holds exactly "everything generated so
  far" with nothing from the in-flight call. `assemble_script(state["sections"])`
  is reused verbatim to format it, giving prompt content and the final
  shipped output one identical `[INTRO]`/`[SECTION N: Title]`/`[OUTRO]`
  marker convention, with no drift risk between them.
  `_generate_intro_section()` passes `prior_full_text=""` (no prior sections
  exist yet); `_run_body_section_loop()`/`_generate_outro_section()` pass
  `assemble_script(state["sections"])`.
- `_SECTION_GENERATION_SYSTEM_PROMPT`'s FORBIDDEN MATERIAL rule (anti-
  repetition only) now explicitly covers the new full-text block too, and a
  new adjacent LOCKED CONTINUITY rule tells Claude the full text is binding
  fact context it must never contradict — deliberately two separate
  obligations (don't repeat it; don't contradict it), not alternatives.
- Generation-input change only — no new Claude call, no post-hoc validator.
  Explicitly not a reintroduction of `validate_script_globally()` (removed
  per the Elimination Mandate, D1.2, above): that was a Claude call judging
  an already-finished script whose findings nobody ever fixed; this instead
  gives every section-generation call richer, more literal input up front.
- Runtime proof: `tests/test_prior_section_continuity.py` — stubbed
  `generate_section`, full real accumulation chain (INTRO → body loop →
  OUTRO unmocked), including a planted-fact propagation test reproducing the
  exact production defect string ("exposed as his own daughter's killer")
  and confirming it reaches every later call's `prior_full_text` verbatim.

Rules:

- One body section should primarily advance one major turn.
- Future turns may be foreshadowed, not resolved.
- TTS checks are deterministic.
- Major business decisions live in Python.
- No section-generation retry of any kind (TTS/hook/rhythm/transition/
  narrative-completeness) — every finding is telemetry only. Do not
  reintroduce a corrective-override regeneration loop without a new
  operator decision superseding the Elimination Mandate.
- The INTRO's opening line is constructed from the blueprint's `hook`
  deterministically — do not revert to relying on `check_hook_quality()`
  (or any other validator) to enforce which sentence comes first.
- `prior_full_text` (assembled from `state["sections"]`) and
  `prior_sections_summary` are two separate, additive continuity mechanisms
  — do not remove or restructure one without checking the other's call
  sites (`check_section_transition()` still depends on
  `prior_sections_summary`).

#### `run_script_quality_gate`

File:

```text
app/agents/agent2_discovery/services/scripts.py
```

**No AI call of any kind (Elimination Mandate, roadmap
`code_report/forensic_output_audit_borrasca_run.md` D1.1/D1.2 — supersedes
the former AI assess/rewrite loop and global validation described below).**
`run_script_quality_gate(scripts, script_format="youtube_long",
language="source")` now:

- Applies the deterministic TTS backstop (`_apply_final_tts_backstop()` —
  mechanical `normalize_tts_chars`/`split_long_sentences`, not a regeneration).
- Runs deterministic structural checks (`_collect_quality_gate_issues()`):
  `check_tts_compliance()`, `check_hook_quality()`, `check_maximum_length()`,
  `check_retention_structure()`.
- Logs any MAJOR finding as telemetry (`"telemetry only, no rewrite"`) —
  never triggers a rewrite, never blocks.
- Logs a cost estimate (`SCRIPT_COST_ESTIMATE`, now section-generation calls
  only — there is no rewrite-call cost to add).
- Returns the (TTS-backstopped) script unchanged otherwise.

**Deleted entirely — not capped, not demoted:** `assess_script_quality()`,
`rewrite_script_for_quality()`, both their prompts
(`_SCRIPT_QUALITY_SYSTEM_PROMPT`, `_SCRIPT_QUALITY_REWRITE_BASE`) and schemas
(`_SCRIPT_QUALITY_SCHEMA`, `_QUALITY_REWRITE_SCHEMA`), the up-to-2-rewrite
loop (`_MAX_QUALITY_REWRITES`), `_has_tts_only_high_issues()`,
`_apply_tts_only_quality_cleanup()`, `_apply_post_rewrite_cleanup()`,
`_apply_final_quality_cleanup()`, `validate_script_globally()` (plus
`_GLOBAL_VALIDATION_SYSTEM_PROMPT`/`_GLOBAL_VALIDATION_SCHEMA`) and its
caller `_run_global_script_validation()`. Task keys `script_quality_check`,
`quality_rewrite`, and `global_validation` were removed from
`model_routing.py`'s `MODEL_ROUTING` table — calling `resolve_model()` with
any of them now raises `ValueError`.

Why: a real production run showed two paid rewrites still left the script
`NEEDS_REWRITE`, with the flagged repetitions (duplicated reveals, an
unresolved open loop, a continuity contradiction between sections) shipped
unfixed in the final script regardless — pure cost, zero effect. The rewrite
persona was also hardcoded `"documentary scriptwriter"`, actively pulling
every channel's register toward documentary style irrespective of the
channel's configured tone (see the register-fix note under §8.1/§9.3's
`visual_style`/tone discussion). `validate_script_globally()`'s own findings
(4 real issues in the audited run) shipped unfixed too, for the same reason:
folding a finding into a rewrite-issue list does not guarantee the rewrite
call actually fixes it, and the mechanism paid for the diagnosis twice
(assessment + rewrite) without a corresponding quality gain.

Maximum-length check (roadmap 3.8 / audit G-7) still runs, as telemetry:

- `check_maximum_length()` (`app/services/script_checks.py`) flags MAJOR at
  1,750 words (1,600 target + 150 tolerance) for `script_format="youtube_long"`,
  or 800 words (700 target + 100 tolerance) for any other format — real
  production evidence showed a script reaching 1,799 words against the
  documented 1,200-1,600 word spec, producing a 13.3-minute video instead of
  the intended ~9-10 minutes. Source generation now also receives a
  deterministic prompt target: 1,550 words for `youtube_long`, divided by the
  planned INTRO + body + OUTRO count and stated on every section call. This
  is target-only construction — no trim or quality retry. The finding is logged
  (`QUALITY_GATE_BREAKDOWN`'s `det_length_maj` field) but no longer triggers
  a rewrite.
- Applies to the source-language parent script only — translated parent
  scripts have their own separate `length_parity` ratio check
  (`_generate_validated_translated_parent_script()`, Phase 13.4), and child
  Shorts have their own separate `_MIN_SHORT_WORDS`/`_MAX_SHORT_WORDS` check
  (see `run_shorts_planner` below) — neither is affected by this section.

Retention-structure telemetry (roadmap 6.3 / audit §6, §7) — unchanged by
the mandate, since it was already telemetry-only before this phase:

- `check_retention_structure()` (`app/services/script_checks.py`) flags a
  chapter/section ending on a summary-pattern sentence (e.g. "So...",
  "Thus...", "In conclusion...") — dead air before the next section that
  research identifies as a completion-rate risk.
- `_collect_quality_gate_issues()` calls it directly and stores the result
  under `retention_det` — **always MINOR, always observability only**; never
  counted in `det_majors`, cannot trigger anything.
- Logged via `QUALITY_GATE_RETENTION_STRUCTURE` (one line per finding).

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
- Preserve the source script's narration POV — never convert first-person to
  third-person or vice versa during translation/adaptation.
- A question stays a question (pre-next-test roadmap Tier 2 R6): an
  interrogative source sentence must remain a complete, natural interrogative
  sentence in the target language — all three native base prompts carry this
  rule (a real FR adaptation shipped the final viewer-facing question with a
  period instead of a question mark). Prompt-level only; no deterministic
  question-parity check exists.

`narration_pov` threading (roadmap 4a / audit P1-9): this function already
loads its own `ChannelConfig` row (for `script_format`), so it reads
`config.narration_pov` directly from that same load rather than accepting a
new parameter — no signature change was needed. The value is passed to
`_generate_validated_translated_parent_script()` /
`_generate_validated_translated_short_script()`, which forward it to
`generate_native_script()` as "Narration POV:". Unlike `generate_section()`,
this is **not** an instruction to write in that POV — it is an instruction
to **preserve whatever POV the source script already uses** (both native
base prompts carry a "never convert one to the other" rule); the value only
tells the adapter what to look for and protect during translation.

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

Before any of that sequence runs, `generate_parent_source_script()` first
calls `_passes_source_material_floor()` (roadmap 4b / audit P1-5 — see the
"Source-material floor" entry under `fetch_batch` above), which runs
`check_source_material_floor()` against `content.source_excerpt`. A failing
check sets `Content.status = "FAILED"` and returns `None` immediately —
before `_build_story()`, before `Content.status` moves to
`GENERATING_SCRIPTS`, and before any Claude call — so both callers (Celery
and the harness) already get this protection with no change of their own.

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
  `_generate_validated_translated_short_script()` — a **single**
  `generate_native_script()` call per language (Elimination Mandate
  extension, post-roadmap deep audit): the former retry loop (up to
  `_MAX_SHORT_CORRECTION_ROUNDS` corrective re-generations with an
  `override_instruction`) is deleted — it was the identical
  full-regeneration + corrective-override mechanism audit P1-6 measured
  making source-language Short drafts worse across rounds, and it always
  ended in FAIL_USING_LATEST (non-blocking) anyway. MAJOR findings are now
  telemetry only (`CHILD_SHORT_TRANSLATION_ISSUES`); a hard generation
  exception still returns `None` (transport failure, that language counts
  as missing). This preserves the Phase 12.4 gap-closure (before it, a
  translated child Short was persisted `validated=True` with no check of
  any kind) — the checks still run, they just never buy a re-roll.
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
  `_generate_validated_translated_parent_script()`. Like the child-Short
  path above, it is a **single** `generate_native_script()` call per
  language since the post-roadmap deep audit — the former
  `_MAX_PARENT_TRANSLATION_CORRECTION_ROUNDS` retry budget is deleted;
  MAJOR findings are telemetry only
  (`PARENT_TRANSLATION_VALIDATION_ISSUES`).
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
  `PARENT_TRANSLATION_VALIDATION_ISSUES` (telemetry only, no retry),
  `PARENT_TRANSLATION_VALIDATION_FAIL` (`reason=generation_error` — the
  transport-failure case), `MULTILINGUAL_LENGTH_COHERENCE_PASS`/`_FAIL`.
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

- `run_script_workflow()` skips this planner entirely when
  `ChannelConfig.output_mode == "youtube_long_only"` (logged
  `SHORTS_PLANNER_SKIPPED`) — the first real runtime consumer of
  `output_mode` (post-roadmap deep audit; see §8.1/§8.2).
- Child short scripts are standalone.
- Child short scripts are not parent cuts.
- Child short source scripts and all required child multilingual scripts must
  be validated before the child row is marked `SCRIPTS_VALIDATED`.
- If child short rows already exist, do not create duplicates — and the
  check runs **before** the paid Claude plan call (roadmap 6.1 / audit S-8,
  C-2): a re-run against a parent whose Shorts already exist costs zero API
  calls, not one discarded planning call.
- Existing child rows in `SCRIPTS_VALIDATED` are picked up by normal Agent 3 audio pickup.
- Reads `config.narration_pov` (falling back to `"third_person"` when
  `config` is `None`, and normalizing invalid persisted values as described
  in §8.1) alongside `visual_style`/`image_style`; it forwards the canonical
  value both to `generate_shorts_plan()` and to `_generate_short_script()` →
  `generate_short_episode_script()` — the same channel-wide value every
  other Agent 2 call site uses.
- No child with MAJOR deterministic script issues may be marked ready.
- `generate_shorts_plan()` receives the full parent `voice_script`
  untruncated (prompt engineering audit §2.1) — the former `[:8000]` slice
  was the same truncation bug class already fixed for
  `generate_short_episode_script()` below (a script at the 1,750-word
  `check_maximum_length()` ceiling runs ~9,600-10,500 chars), and it hit
  the *planner* earlier in the pipeline: the part split, `main_reveal`, and
  `cliffhanger` for parts 4-5 of a 5-part plan were being decided without
  the planner ever seeing the story's actual ending.

Structural checks and Parent/Child Overlap Detector — single generation call,
telemetry only (Elimination Mandate, roadmap
`code_report/forensic_output_audit_borrasca_run.md` D1.3 — supersedes Phase
13.2's AI Short Quality Gate and the correction-round retry loop):

- `_generate_short_script()` (`scripts.py`) calls `generate_short_episode_script()`
  exactly **once** per part, passing the **full** long-form voice script for
  fact grounding (the former `[:6000]` slice cut off the story's ending, so
  late parts were grounded on one-sentence plan summaries — fresh full-system
  audit §2.4). A real production run showed the deleted
  correction loop made drafts *worse* across rounds (word count went
  205 → 196 → **218**, still over the 180-word cap, and shipped anyway), and
  the deleted AI Short Quality Gate's `status=="PASSED"` + non-empty `issues`
  contract mismatch burned a retry into a worse draft. Both are gone —
  `_MAX_SHORT_CORRECTION_ROUNDS` and `_run_short_quality_gate()` no longer
  exist, and `assess_short_script_quality()` /
  `_SHORT_QUALITY_SYSTEM_PROMPT` / `_SHORT_QUALITY_SCHEMA` /
  `task="short_quality_check"` were deleted from `system_prompt.py` and
  `model_routing.py` entirely — not disabled, not capped, removed.
- **Word-floor regeneration (operator-approved Elimination Mandate
  exception, 2026-07-16 — the binding Short duration rule):** a Short MUST
  never run under 61 seconds (Agent 3's hard audio gate below); it SHOULD
  stay at or under ~90 seconds, but exceeding 90 s is telemetry-only and
  never fails anything. When the single draft's word count is under
  `_MIN_SHORT_WORDS`, `_generate_short_script()` makes exactly ONE
  regeneration call with an explicit minimum-words constraint
  (`word_floor_note` parameter of `generate_short_episode_script()`) and
  keeps the LONGER of the two drafts — a deterministic count-triggered
  choice, never an AI quality judgment. Still-under-floor after that is
  telemetry (`SHORT_SCRIPT_STILL_UNDER_FLOOR`); the 61 s audio gate remains
  the final enforcement. Logs `SHORT_SCRIPT_WORD_FLOOR_REGEN`. Runtime
  proof: `tests/test_short_word_floor_regen.py`.
- **Structural checks** (`_collect_short_script_major_issues()`): calibrated
  `_MIN_SHORT_WORDS` floor (190 words — see the calibrated band note below), word cap
  (`_MAX_SHORT_WORDS` = 270), TTS compliance, hook opener, and section-marker
  presence (`_SHORT_TRANSLATION_MARKER_RE`, shared with the translated-Short
  check below) still run on the generated draft. Any MAJOR is logged
  at WARNING (`"telemetry only, no retry"`) — over-cap in particular never
  triggers anything (a slightly-long Short ships, per the operator rule).
- **Parent/child overlap** (`detect_parent_child_overlap()`, Phase 13.3):
  deterministic exact word-sequence reuse check between the child Short's
  narration and the parent long-form `voice_script`, unchanged in its own
  detection logic — compares normalized (lowercased, punctuation-stripped)
  word tokens using `_OVERLAP_NGRAM_LENGTH`-word (6) sliding windows;
  `overlap_ratio` at or above `_OVERLAP_MAX_RATIO` (15%) is logged as a
  MAJOR finding. It still runs once on the single draft and is still
  skipped entirely (`PARENT_CHILD_OVERLAP_SKIPPED`, logged) when the parent
  `voice_script` is missing/empty — it no longer feeds an
  `override_instruction` for a next attempt, because there is no next
  attempt. Applies only to source-language Short generation against the
  parent long-form script — not to parent long-form generation itself, and
  not to Phase 12.4's multilingual child-Short translation/adaptation path.
- **Tier 4 content prompt rules (`PROMPT_VERSION` 5.2 → 5.3):** planner and
  Short-writer cold-open identity must stay in first person when
  `narration_pov="first_person_storytime"` (never a third-person naming
  sentence followed by I/me/my); every cliffhanger names its concrete
  unresolved threat/person/event/place/object; sunk-cost logic is explicitly
  a pressured narrator/character rationalization, not objective advice.
  Generation guidance was 140–170 words when Tier 4 shipped — superseded by
  the 2026-07-16 recalibration below.
- **Current Short content rules (`PROMPT_VERSION` 5.5):** every planned part
  owns an exclusive narrative span. The writer receives every sibling
  `main_reveal` as `other_parts_main_reveals` (forbidden territory) and
  must stop inside its own span. First-person Shorts name their narrator in
  the first five seconds; third-person persona references are unambiguous;
  month/year jumps use spoken bridges; one-off atmospheric details are
  explained or dropped; closing questions are precise and story-anchored.
  These are construction rules only. Parent/child overlap remains telemetry
  only, and no child-vs-child blocker/retry was introduced.
- Deterministic code still owns the duration band, recalibrated
  (`PROMPT_VERSION` 5.4, 2026-07-16) to the measured **~176 wpm
  post-silence-compression** Short narration rate (run 41f7eeb8: 246 words
  → 83.7 s — Tier 1's interior-silence compression obsoleted every ~120 wpm
  calibration): `_MIN_SHORT_WORDS` (190 — ~65 s at the measured rate; at
  the OLD 135 floor a compliant draft was ~46 s, far under the hard 61 s
  gate) through `_MAX_SHORT_WORDS` (270 — ~92 s). Prompts/schema target
  210–260 words with an explicit never-under-190 rule. The floor triggers
  the single word-floor regeneration above; the cap is pure telemetry —
  never enforced by regeneration or trim (operator rule: over ~90 s ships).
- A hard exception from the single `generate_short_episode_script()` call
  (bad JSON, API error) returns `None` immediately — the caller
  (`run_shorts_planner()`) removes that child content row
  (`_remove_failed_short_content()`) rather than retrying. This is a
  transport failure, not a quality judgment, and is unaffected by the
  Elimination Mandate — the mandate deletes content-quality re-roll loops
  (retry because a draft wasn't good enough), not transport-failure handling
  (a hard exception on the one attempt made).
- Post-audio assertion (roadmap 3.6 / audit S-5, exec-8): Agent 3 enforces
  `_MIN_SHORT_AUDIO_DURATION_MS = 61_000` immediately after child Short audio
  duration is known and before Whisper/persistence. A child language below
  the floor logs `CHILD_SHORT_AUDIO_TOO_SHORT`, rolls back that language, and
  is not counted as successful; if every language misses/fails, the child
  content becomes `FAILED`.
- Parent long-form quality validation (`run_script_quality_gate`) is a
  separate mechanism — see §9.3 above. It was independently stripped of its
  own AI assess/rewrite loop by the same Elimination Mandate (D1.1/D1.2);
  this section's D1.3 changes did not need to touch it since it was already
  addressed there.
- The translated/adapted child Short path
  (`_generate_validated_translated_short_script()`, Phase 12.4) was outside
  D1.3's named scope but has since been brought in line by the post-roadmap
  deep audit: it too is now a single generation call with telemetry-only
  findings — see §9.3's `generate_multilingual_scripts` entry. No
  quality-judging retry loop remains anywhere in Agent 2.

Naming rule:

- Use product names such as `short_episode`, `standalone_short`, or `child_short`.
- Do not name functions, files, constants, classes, logs, or DB concepts after development phase labels such as `phase4`.
- Log messages may mention the current product concept, for example:
  - `STANDALONE_SHORTS_ALREADY_EXIST`
  - `AUDIO_PICKUP`
  - `CHILD_SHORT_AUDIO_START`
  - `SHORT_EPISODE_RENDER_DONE`
- Existing phase-labeled logs should be renamed when touched.

### 9.5 AI-Generated Story Discovery (`script_source="ai_generated"`)

Design record: `code_report/ai_generated_story_discovery_design.md`.

Problem this solves: `run_discovery()`'s default path (`fetch_batch()`) makes
a real, expensive `call_claude_with_tools()` call with the `web_search` tool
on every discovery attempt — multiple tool-use rounds and real
`story_research` token cost just to pick one candidate. For a channel whose
content doesn't need to be grounded in a real discovered post, this is
avoidable spend. `ChannelConfig.script_source="ai_generated"` (alias
`"claude_generated"`, normalized by `app.services.script_source.normalize_script_source()`,
§7.6) is a real, executable alternative: Claude writes an original story or,
when the channel explicitly requests it, an accuracy-bound historical
narrative, grounded only in the channel's existing `niche`/`tone`/`description` — no
new persisted "story brief" field, and no `ChannelSource` requirement.

**Two-stage design — real generation cost is spent only after operator
approval:**

1. **Discovery time (cheap):** `generate_story_premise()` synthesizes a
   short, concrete premise (3–6 sentences) — no `web_search` tool call. This
   premise goes through the *same* dedup/scoring/Telegram-approval machinery
   every reddit-discovered candidate uses, completely unchanged.
2. **On operator APPROVE (the only point real cost is spent):**
   `expand_story_premise()` expands the approved premise into a full story
   body, gated on the *existing* `check_source_material_floor()` failing —
   the same fail-loud mechanism a too-thin Reddit story already uses, not a
   new one.

File:

```text
app/agents/agent2_discovery/services/story_generator.py
```

#### `generate_story_premise`

Input: `channel` (niche/tone/description), target `language`, optional
`rejected_stories` (titles + operator feedback, threaded as an "avoid
similar plots/themes" instruction — synthetic URLs carry no meaning to
Claude here, so only titles/feedback are used).

Output: `list[Story]` — `[Story]` on success, `[]` on any failure (transport
error, empty title/body). Mirrors `fetch_batch()`'s contract exactly so
`run_discovery()` can call either interchangeably. Constructs
`Story(url=f"discovery://ai_generated/{channel.id}/{uuid4()}",
source_type="ai_generated", source_value="claude_synthesis", language=language)`
— the URL's per-call `uuid4()` is why `_is_duplicate()` can structurally
never fire true for this path (see the exclusion-seeding rule below).

A premise longer than `_PREMISE_SOFT_WORD_CEILING` (150 words) is logged as
a warning (`"longer than expected... Claude ignored the length instruction"`)
but never re-rolled — Elimination Mandate (§9.3/§23): a quality judgment is
telemetry only, not a retry trigger.

Uses `call_claude_structured(task="story_synthesis", ...)` — the system
prompt defaults to original fiction and never permits fake Reddit framing
(no invented usernames, "OP", or fabricated engagement). A channel brief
explicitly about long-deceased historical figures/events may use those real
subjects under the accuracy contract below; living/recent public figures and
named franchises/fictional worlds remain forbidden.

**Title/description must be direct, not a mystery teaser (operator
feedback, live-canary fix):** a real run produced vague, suspense-style
titles/descriptions ("Do not resolve the mystery/tension in the
premise... create curiosity, not satisfy it" was the original instruction)
that told the operator nothing about what the story actually covered before
they had to approve it — this is an approval screen, not a viewer-facing
hook. The prompt now requires a clear, direct title naming the specific
subject/situation, and a 2-4 sentence description stating the situation,
setting, and people/conflict plainly enough that the operator could
describe the story to someone else after reading it once — e.g. title
"Hannibal: Carthage's War Against Rome", description "The story follows
Hannibal of Carthage leading his army, elephants included, across the
Mediterranean and the Alps to fight Rome." The eventual resolution may
still be left open (that's normal story structure); the *subject* must
never be hidden to manufacture suspense.

**Performance assets are explicit without turning the approval pitch into a
teaser (`PROMPT_VERSION` 5.6):** premise construction chooses a specific
episode rather than a cradle-to-grave survey and names a factual
high-stakes/counterintuitive hook-source moment, a personal consequence,
real conflict/contradiction, distinct visual settings/actions, and a
nameable thumbnail image/object. The description remains a plain operator
pitch. For `ai_generated` scoring, `scroll_stopper_potential` judges
whether those explicitly described facts contain a moment the future script
could open on; it does not demand that the approval description itself use
viewer-hook rhetoric and may not invent an unmentioned moment.

**Historical-accuracy framing (pre-next-test roadmap Tier 4 R12):** all
three `story_generator.py` system prompts — premise, expansion, and
conversational revision — apply the same conditional rule when the channel
brief/premise/operator direction concerns real history: verifiable geography,
distance, chronology, people, roles, relationships, and outcomes must be
accurate; no invented route/time span/event for drama; debated traditions are
attributed (for example, "later chroniclers claimed"); every named person is
introduced by role or relationship on first mention. This is prompt grounding,
not an AI judge/retry loop.

**Channel description is the specific subject-matter brief, not
background color (same fix):** the prompt now explicitly tells Claude that
`channel.description` — not just `niche`/`tone` — names the specific kind
of story to write (e.g. "ancient military history and famous battles"),
and that niche/tone only narrow the category/register. `expand_story_premise()`
(below) previously omitted `channel.description` from its own user message
entirely — grounding the full expansion on niche/tone only, with the
description's influence surviving only indirectly through the already-
approved premise text. It now receives `channel.description` directly too,
so a long/nuanced channel description isn't lost between the premise and
the full expansion.

#### `expand_story_premise`

Input: the human-approved `premise` text, `channel`, `script_format` (drives
the target word count), `language`.

Output: the expanded story body string, or `None` on any failure (transport
error, empty body). Single Claude call
(`call_claude_structured(task="story_synthesis", ...)`), **no retry loop
beyond transport-failure handling** — per the Elimination Mandate, a
too-short-but-real result is not re-rolled here; it is left for the
*existing* `check_source_material_floor()` call downstream to catch,
exactly like it already does for a too-thin Reddit candidate.

Word-count target scales with `script_format`
(`_EXPANSION_WORD_TARGETS = {"youtube_long": 2200}`, default `600` for any
other format). The richer 2,200-word source is intentionally larger than the
1,550-word assembled-script target, so section generation compresses grounded
material instead of inflating a thin source. The expansion target is explicitly
both goal and ceiling: plan the arc, stay close, do not exceed, and stop rather
than pad when complete. Python returns the single model result unchanged
— there is no trim/retry mechanism. The system prompt requires the expansion to
preserve the approved premise's situation/tension/tone faithfully (never
invent a different plot), write a complete narrative with a real resolution
(not an outline), and keep the same conditional fiction/history and rights-
safety rules as the premise prompt.

The structured-output ceiling is 8,192 tokens. The first production attempt
after raising the source target to 2,200 words hit the former 4,096-token
ceiling exactly (usage reported 4,096 output tokens) and returned no usable
`body`. Raising headroom fixes truncation without changing the word target,
adding a retry, or accepting a partial story.

#### `run_discovery()` branching (`discovery.py`)

`run_discovery()` reads `normalize_script_source(config.script_source)`
once, near the top, and branches on it:

- **Sources:** the `ChannelSource` requirement (`if not sources: return
  None`) is skipped entirely for `ai_generated` — `_resolve_ai_story_language()`
  resolves the target language from the first `ChannelLanguage` row instead
  (the same informal "first = primary" convention the frontend already
  relies on; a documented soft spot, not a schema-guaranteed primary
  language — see the design doc §5.1).
- **Exclusion seeding:** since every `ai_generated` candidate carries a
  per-call-unique synthetic URL, `_is_duplicate()` can structurally never
  return `True` for this path — the prompt-level "avoid similar
  plots/themes" list is the *only* anti-repetition mechanism available.
  `_seed_ai_generated_exclusions()` proactively seeds `accumulated_rejected`
  with the channel's recent content history (title + URL, same
  `_MAX_NUCLEAR_EXCLUSION` cap `_nuclear_retry()` uses) **before the first**
  `generate_story_premise()` call — not only after an in-loop rejection,
  which would let attempt 1 repeat any premise the channel already
  published.
- **Source-material floor:** skipped entirely at discovery time
  (`floor_reason = None if is_ai_generated else
  _fails_source_material_floor(...)`) — a short premise is short by design
  at this stage; see `generate_parent_source_script()` below for where the
  floor check applies to this path instead.
- **Nuclear retry:** skipped entirely — `_nuclear_retry()` is never called
  for `ai_generated`. Dedup is structurally moot (see above), and the
  exclusion list is already proactively seeded, so there is nothing a
  full-history nuclear pass would add. Falls straight through to the manual
  Telegram fallback on exhaustion.
- **Story-gate scoring:** unchanged in position — still runs on the premise
  text. `score_story_for_gate()` (`system_prompt.py`) detects
  `story.source_type == "ai_generated"` and swaps the
  upvotes/comments/`published_at` metadata line for an honest "no
  real-world engagement signal exists for this story" note, so
  `scroll_stopper_potential`/`social_media_clickability` are not penalized
  for an absence that isn't real. A low score does **not** trigger
  regenerating a new premise — the logged `attempt=N/3` budget covers
  duplicate or thin-source replacement only, never AI-quality re-rolls
  (would violate the Agent 2 no-quality-retry rule) — but a low score also
  no longer aborts the discovery call the way it once did: the gate never
  blocks the pipeline (see the general `run_discovery` entry above), so the
  premise is persisted and sent to Telegram with its score attached
  regardless, and the operator decides.
- **Manual fallback wording:** `_create_manual_fallback()` takes a new
  `is_ai_generated: bool = False` parameter and sends a different Telegram
  message on exhaustion ("send a story premise or full story text
  manually") instead of asking for "a Reddit post URL", which would be
  irrelevant for a channel with no `ChannelSource`.
- **Telegram approval message:** `build_telegram_message()` (`system_prompt.py`)
  detects the synthetic `discovery://ai_generated/` URL prefix and renders
  `"AI-generated original story premise (no external source)"` in the
  `Source:` line instead of the raw internal URL.

#### `generate_parent_source_script()` expansion step (`script_workflow.py`)

New step, inserted before the existing `_passes_source_material_floor()`
call and after `_load_script_workflow_context()`: when
`normalize_script_source(context.config.script_source)  == "ai_generated"`,
calls `_ensure_ai_story_expanded(content, context, db)` and returns `None`
immediately if it returns `False`. Everything else in
`generate_parent_source_script()` — `_build_story()` onward — is unchanged;
this is the only new step in the entire blueprint→sections→quality-gate→
persist pipeline.

`_ensure_ai_story_expanded(content, context, db) -> bool`:

- **Idempotent no-op** when `content.source_excerpt` already clears
  `check_source_material_floor()` (e.g. a stuck-state-recovery re-entry that
  already expanded it on a prior attempt) — logged
  `AI_STORY_EXPANSION_SKIPPED reason=already_clears_floor`. Both this check
  and the caller's subsequent floor check use the same
  `check_source_material_floor()` implementation, so they can never disagree
  about "clears the floor".
- Otherwise calls `expand_story_premise()` and persists the result **in
  place** to `content.source_excerpt` (committing immediately), so the
  `_passes_source_material_floor()` call right after this one judges the
  updated value, never a stale one.
- Returns `False` (and sets `Content.status = "FAILED"`, logged
  `AI_STORY_EXPANSION_FAILED`) only on an outright expansion failure
  (`expand_story_premise()` returned `None` — transport failure or empty
  body). A **real but still-too-short** expansion is deliberately *not*
  re-rolled here (Elimination Mandate) — it returns `True`, and the existing
  `_passes_source_material_floor()` call catches it exactly like it already
  catches a too-thin Reddit story.

**Story-staleness guard (post-implementation review):** `generate_parent_source_script()`
accepts an optional pre-built `story` argument (§9.3's `_build_story()` doc
above) — when `script_source == "ai_generated"`, any `story` the caller
passed in is now discarded and rebuilt via `_build_story(content)`
immediately after `_ensure_ai_story_expanded()` returns, *before* the
existing `if story is None: story = _build_story(content)` line is ever
reached. A caller-supplied `story` for this path is guaranteed to predate
the expansion — for `test_pipeline/test_full_pipeline.py` specifically
(the only real caller that ever passes a non-`None` `story` — see below),
it is literally the discovery-time premise `Story` returned by
`run_discovery()`, not the full expanded body just written to
`content.source_excerpt`. Without this guard the blueprint/sections would
silently ground on a 3-6 sentence premise instead of the real story.
`run_script_workflow()` (production) is unaffected either way — it always
calls with `story=None`.

**`test_pipeline/test_full_pipeline.py` no longer threads a captured
`Story` through at all** (root-cause fix, not just the defense above):
`_run_step_scripts()` calls `generate_parent_source_script(content, db)`
with no `story` argument, exactly matching production's `run_script_workflow()`
convention. The harness used to capture `Story` from its own
`run_discovery()` call and pass it through, which — beyond the ai_generated
staleness case above — would also have gone stale for **any** script_source
after a CHANGE-driven re-discovery (see the `_run_telegram_approval()` entry
under §9.3 `send_for_validation`): the harness's own `content` variable gets
replaced with a new row from a second `run_discovery()` call, but the
originally-captured `story` object would still describe the first,
now-rejected candidate. `_build_story(content)` always reads whatever
`content.source_excerpt` currently holds, so it can never drift from the
`content` row actually being processed — this is why `Story.upvotes`/
`comments`/`published_at`/`source_type`/`source_value` (dropped by
`_build_story()`, only used by `score_story_for_gate()` during discovery
itself, never by blueprint/section generation) are safe to lose.

#### Conversational premise revision (operator correction)

The original CHANGE design (§9.3's `send_for_validation` entry) treated
every non-APPROVE reply as "reject this candidate, search for a different
one" — correct for `"reddit"` (a real discovered post can't be "revised",
only swapped for a different one), but wrong for `"ai_generated"`: an
operator correction pointed out that the Telegram exchange for a
Claude-synthesized premise should be a real, continuing conversation — the
operator comments, Claude adjusts the SAME premise, and this repeats until
APPROVE — not a discard-and-search-again loop that throws away everything
already discussed.

`validation._handle_change()` now branches on `script_source`
(`normalize_script_source(config.script_source)`, loaded once at the top):
`"ai_generated"` dispatches to `_handle_change_ai_generated()`; every other
value keeps the original reject-and-rediscover behavior unchanged (moved
into the same function, gated behind the branch, not duplicated).

`_handle_change_ai_generated(validation, content, channel, config, feedback, db)`:

- **Same `content`/`ContentValidation` rows throughout the whole exchange**
  — never rejected, never replaced. Only `content.title`/`content.source_excerpt`
  change, round after round.
- **Conversation history is persisted in `content.story_blueprint
  ["ai_conversation"]`** — the same pre-approval scratch-shape convention
  `_handle_manual_story_input()` already uses for `rejected_stories` on the
  manual-fallback placeholder row (no new DB column; `story_blueprint` is
  free pre-approval, since the real blueprint doesn't exist until after
  `generate_story_blueprint()` runs post-approval). Each turn is either
  `{"role": "assistant", "title": ..., "body": ...}` (a premise Claude
  proposed) or `{"role": "operator", "feedback": ...}` (the operator's
  reply). On the first CHANGE reply for a content row, turn 1 is seeded from
  whatever `content.title`/`content.source_excerpt` currently hold (the
  discovery-time premise) before the operator's feedback is appended, so the
  transcript always starts complete.
- Calls `story_generator.revise_story_premise(channel, language, conversation)`
  — the FULL transcript, not just the latest feedback — so a later revision
  can never contradict or silently discard an earlier one (e.g. the operator
  asks for "set it in Rome" in round 1, then "add Hannibal" in round 2 — the
  round-2 call still sees "set it in Rome" and must not drop it).
- On success, appends the new `{"role": "assistant", ...}` turn, updates
  `content.title`/`content.source_excerpt` in place, and returns a fresh
  `build_telegram_message()` showing the revision — the caller
  (`handle_telegram_update()`) sends it as the next message and updates
  `ContentValidation.telegram_message_id` exactly like every other CHANGE
  reply, so the operator's next reply anchors to the revised version.
- On a `revise_story_premise()` transport failure, the feedback turn is
  still persisted (so the next attempt's transcript reflects it) but
  `title`/`source_excerpt` are left unchanged, and the operator is told to
  retry or approve the version already shown — no silent content loss.
- `validation.revision_count`/`script_issues_log` bookkeeping and the
  `validation_max_revisions` cap are unchanged from the reddit path (same
  `_apply_limit_policy()` call on exhaustion) — only what happens *below*
  the cap differs.

`story_generator.revise_story_premise(channel, language, conversation) ->
dict | None`: single Claude call
(`call_claude_structured(task="story_synthesis", ...)`), no retry loop
beyond transport-failure handling (Elimination Mandate — same convention as
`generate_story_premise()`/`expand_story_premise()`). Renders `conversation`
into a numbered transcript in the user message (every assistant premise
version, every operator feedback line, in order) and instructs Claude to
address the operator's most recent feedback while staying consistent with
everything discussed before it. Same directness/rights-safety and historical-
accuracy rules as `generate_story_premise()`'s prompt (§9.5 above) — a revision
must be just as direct and clear as the original, never a mystery teaser. Returns
`{"title": ..., "body": ...}` on success, `None` on any failure.

`test_pipeline/test_full_pipeline.py`'s `_run_telegram_approval()` mirrors
the identical branch for the synchronous operator harness: a per-content
`conversation: list[dict]` (ai_generated) alongside the existing
`rejected_stories: list[dict]` (reddit/etc.) — a CHANGE reply either revises
`content` in place via `revise_story_premise()` and `continue`s the loop
(next iteration's `send_for_validation()` naturally sends the revised
version), or rejects and calls `run_discovery()` for a replacement, exactly
as before. `max_revisions` and the timeout/APPROVE handling are shared,
unbranched code — only the CHANGE branch itself differs.

Runtime proof: `tests/test_telegram_chatbot_and_story_freshness.py`
(`TestHandleChangeAiGeneratedConversation`, real `_handle_change()`/
`_handle_change_ai_generated()`, and `TestTelegramApprovalLoop`'s
`test_ai_generated_change_revises_same_story_conversationally`, real
`_run_telegram_approval()`) — both prove the second revision's conversation
includes the first revision's title/body (not just the original premise),
the content/validation rows never change identity, `run_discovery()` is
never called for this path, and the reddit path's reject-and-rediscover
behavior is unaffected (regression coverage in the same file plus the
pre-existing `tests/test_story_feedback_change_flow.py`).

#### Model routing and activation

- `story_synthesis` task key (`model_routing.py`) → `PRIMARY_MODEL` — used
  by `generate_story_premise()`, `expand_story_premise()`, and
  `revise_story_premise()` (conversational revision, above).
- `v3_config_rules.is_executable_script_source()` (§8.2) treats
  `{"reddit", "ai_generated"}` as executable for `content_mode="single_story"`
  — `ai_generated` is no longer "reserved for a future phase".
- `activation_readiness.check_activation_readiness()` (§8.3) gains check 10:
  `script_source="ai_generated"` requires both `Channel.niche` and
  `Channel.tone` non-empty (`ai_generated_missing_niche`/
  `ai_generated_missing_tone`) — with no `ChannelSource` fallback for this
  path, they are the only grounding input this feature has.
- Setup UI (`app/ui/src/constants.js`'s `SCRIPT_SOURCES`,
  `BasicInfoSection.jsx`): the `ai_generated` option is enabled
  (`executable: true`, no "(Coming soon)" suffix); selecting it shows an
  informational note (no new input field, per the "no new persisted config
  field" product decision) confirming stories are generated from the
  niche/tone already entered on the same screen, instead of the Reddit
  sources block.

#### Explicit scope boundaries

- No new DB column, no migration — `Content.source_excerpt` already holds
  the premise pre-expansion and the full body post-expansion; no other
  field was free for this purpose (see the design doc §2.1 for why).
- No new `Content` status — reuses `PENDING_APPROVAL` → `APPROVED` →
  `GENERATING_SCRIPTS` exactly as the reddit path; the expansion step is
  folded into the existing `APPROVED` → `GENERATING_SCRIPTS` transition.
- No changes to Agent 3, Agent 4, Agent 5, multilingual script generation,
  or the shorts planner — all of them consume `Content`/`Script` rows the
  same way regardless of how the parent story originated.
- The production Telegram CHANGE/feedback flow (`_handle_change()`, §9.3
  `send_for_validation`) was originally built origin-agnostic (reject and
  re-dispatch discovery for any `script_source`) but was corrected by an
  operator during post-implementation review: for `"ai_generated"` this is
  now conversational premise revision, not reject-and-rediscover — see
  "Conversational premise revision" above. The reddit path's original
  behavior is unchanged. (The *operator harness's own* approval loop was a
  separate, earlier-found gap — see the `_run_telegram_approval()` entry
  under §9.3 `send_for_validation` and the story-staleness guard above.)
- `"user_provided"` and `"hybrid"` remain reserved, unexecuted
  `script_source` values — do not wire execution logic for either without a
  new design record and CLAUDE.md update.

Runtime proof: `tests/test_ai_generated_script_source.py` — Agent 1
executability/activation unit tests; `generate_story_premise()`/
`expand_story_premise()`/`revise_story_premise()` unit tests with the paid
`call_claude_structured` boundary stubbed (including the conversation-
transcript-ordering proof for `revise_story_premise()`); a runtime proof
(CLAUDE.md §19.4) driving the real `run_discovery()` and
`generate_parent_source_script()` chains against a fake DB (only the paid
Claude boundaries stubbed) covering: zero-`ChannelSource` channels succeed;
exclusions are seeded before the first premise call; nuclear retry is never
invoked; the floor check never fires on a short premise at discovery time;
`_ensure_ai_story_expanded()`'s idempotency, persist-then-recheck ordering,
and failure path; and the unmodified reddit path re-run as a regression
guard. `tests/test_telegram_chatbot_and_story_freshness.py` covers the
conversational-revision CHANGE path specifically — see "Conversational
premise revision" above for the full runtime-proof description.

### 9.6 Solo Short Script Generation (`output_mode="shorts_only"`)

Design record: `code_report/output_mode_shorts_only_and_youtube_long_only_roadmap.md`.

A Solo Short is a standalone short episode with **no parent Content row at
all** — `is_short_episode=True`, `parent_content_id=NULL` (§5.3, §4.4).
Unlike the child-of-a-parent Shorts §9.4 covers (which are always cut from
an already-validated long-form script), a Solo Short's own script IS the
final output — written directly from the discovered/generated source
material, never derived from a long-form script or a multi-part plan.

**Discovery (`discovery.py`) — reuses `run_discovery()` unchanged, no new
discovery mechanism.** `config.output_mode` is already loaded near the top
of `run_discovery()`; when it is `"shorts_only"`, both `run_discovery()`'s
own `Content(...)` construction and `_create_manual_fallback()`'s
placeholder-row construction set `is_short_episode=True,
short_part_number=1, short_total_parts=1` (`parent_content_id` stays
`NULL`, the column's own default) — the row is a Solo Short from the
moment it is discovered, all the way through Telegram approval. The
discovery-time source-material floor check
(`_fails_source_material_floor()`/`_nuclear_retry()`) and
`generate_parent_source_script()`'s post-approval floor check both use
`SOLO_SHORT_SCRIPT_FORMAT` (§7.3 — the 420-word floor) instead of the
channel's own `script_format` for a `shorts_only` channel, since a
~200-260 word Short needs far less source material than a 1,550-word
long-form script.

**Script generation — `run_script_workflow()`'s top-level dispatch:**
```python
def run_script_workflow(content, db):
    if content.is_short_episode:
        return _run_solo_short_script_workflow(content, db)
    # existing parent logic, unchanged — including the run_shorts_planner()
    # call at the end, which a Solo Short structurally never reaches
```
`_run_solo_short_script_workflow(content, db)` (`script_workflow.py`) is
modeled on `generate_parent_source_script()` but swaps the long-form
blueprint→sections→quality-gate middle for a single script-generation call:

1. `_load_script_workflow_context()`, then `dataclasses.replace(context,
   script_format=SOLO_SHORT_SCRIPT_FORMAT)` — one substitution point that
   threads the short-appropriate format through every reused helper
   (`_ensure_ai_story_expanded()`, `_passes_source_material_floor()`,
   `generate_story_blueprint()`) with no per-function parameter changes.
2. `ai_generated` expansion (`_ensure_ai_story_expanded()`, reused verbatim
   — channel-level, not content-shape-specific) and the source-material
   floor check (`_passes_source_material_floor()`, reused verbatim).
3. `generate_story_blueprint(story, context.channel, ...)` — confirmed
   fully reusable (takes only `story.body` = `content.source_excerpt` +
   channel/style config, nothing long-form-specific). Persisted to
   `content.story_blueprint` exactly like the parent path — this is what
   gives a Solo Short its own `character_descriptors`/`era_setting`/
   `protagonist_gender`/`hook` for Agent 3 voice-gender selection (§10.3)
   and Agent 4 visual continuity (§11.4), with zero parent involvement.
4. **New** `generate_solo_short_script(story, blueprint, channel,
   channel_voice, visual_style, image_style, narration_pov,
   word_floor_note="")` (`system_prompt.py`, new `_SOLO_SHORT_SYSTEM_PROMPT`
   / `_SOLO_SHORT_SCHEMA`, task key `solo_short_script` — same
   `PRIMARY_MODEL` tier as `short_script`, split out purely so cost/latency
   logs don't blur, same convention as `publish_timing_suggestion`).
   Framed as writing THE complete, self-contained short directly from
   `story.body` — not "one part of a plan." Cannot reuse
   `generate_short_episode_script()` (§9.4): that function hard-requires
   `main_reveal`/`cliffhanger`/`other_parts_main_reveals`, which only exist
   because `generate_shorts_plan()` produced a multi-part plan first.
   Reuses the same calibrated word band (`_MIN_SHORT_WORDS`–
   `_MAX_SHORT_WORDS`, ~190-270 words) and the same §21.6 short-episode
   prompt rules (standalone/cold-open test, spoken-video delivery, no
   clickbait filler, an ORIGINALITY rule against copying the source
   material's own phrasing).
5. Word-floor regeneration — the same single-retry pattern
   `_generate_short_script()` uses (scripts.py): one extra
   `generate_solo_short_script()` call with a `word_floor_note` if the
   first draft is under `_MIN_SHORT_WORDS`, keeping the longer draft.
   Logged `SOLO_SHORT_WORD_FLOOR_REGEN`.
6. Structural checks — `_collect_short_script_major_issues()` (scripts.py,
   the same function the per-part planner path already uses). Telemetry
   only (Elimination Mandate). `detect_parent_child_overlap()` is skipped
   entirely — no parent script exists to compare against.
7. Persisted via `_persist_source_script()` — already threaded to pass
   `content.is_short_episode` (not a hardcoded `False`) into
   `estimate_duration_sec()`'s wpm calibration, so a Solo Short's own
   duration estimate correctly uses the short-form (~120 wpm) calibration
   window, not the long-form (~135 wpm) one.
8. `generate_multilingual_scripts()` — called exactly as the parent path
   calls it, **zero code changes needed**: its `content_kind` branch
   (`"child_short" if content.is_short_episode else "parent_long_form"`,
   scripts.py) is driven purely by `is_short_episode`, never
   `parent_content_id`, so it already selects the flat, unsectioned
   native-adaptation prompt correctly for a Solo Short.
9. `content.status = "SCRIPTS_VALIDATED"` — the existing status string,
   unchanged meaning.
10. Returns without ever calling `run_shorts_planner()` — structurally
    unreachable from this branch (the dispatch above returns before
    `run_script_workflow()`'s own `run_shorts_planner()` call is reached),
    **plus** `run_shorts_planner()` itself refuses to act on
    already-short content as a second, independent line of defense: its
    very first check after loading the target content is
    `if long_content.is_short_episode: log SHORTS_PLANNER_SKIPPED
    reason=content_is_already_a_short; return`. This protects any future
    caller of `run_shorts_planner()` (a direct script, an admin tool, a
    retry path), not just the one call path that exists today.

Log names: `SOLO_SHORT_SCRIPT_START`, `SOLO_SHORT_SCRIPT_DONE`,
`SOLO_SHORT_WORD_FLOOR_REGEN`, `SOLO_SHORT_SCRIPT_STILL_UNDER_FLOOR`,
`SOLO_SHORT_SCRIPT_ISSUES` (mirroring the equivalent `SHORT_SCRIPT_*`/
`CHILD_SHORT_TRANSLATION_ISSUES` names §9.4 already uses).

Rules:

- Do not thread `output_mode` any deeper than this dispatch — every reused
  helper below it (`_ensure_ai_story_expanded()`,
  `_passes_source_material_floor()`, `generate_story_blueprint()`,
  `generate_multilingual_scripts()`) reads only `is_short_episode` or the
  already-substituted `context.script_format`, never `output_mode` itself.
- Never call `generate_parent_source_script()`, `generate_script_sections()`,
  or `run_script_quality_gate()` from the Solo Short path — those are the
  long-form blueprint→sections→quality-gate pipeline and do not apply here.
- `run_shorts_planner()`'s own-content guard must never be loosened back to
  "only checked by the caller" — see the two-line-of-defense reasoning above.

Runtime proof: `tests/test_solo_short_pipeline.py` — real
`run_script_workflow()`/`_run_solo_short_script_workflow()` chain against a
fake DB (only `generate_story_blueprint()`/`generate_solo_short_script()`/
`generate_native_script()` stubbed at the paid-Claude boundary), proving:
`SCRIPTS_VALIDATED` reached without `run_shorts_planner()` or
`generate_parent_source_script()` ever being called (both poisoned to
raise); the 450-word source excerpt (clears the 420-word Solo Short floor,
would have failed the 900-word long-form floor) reaches script generation;
real `Script` rows persisted and validated for every configured language;
the word-floor regeneration path fires and keeps the longer draft; and no
child `Content` row is ever created. `tests/test_solo_short_prerequisite_
fixes.py` covers `run_shorts_planner()`'s own guard directly (both the
refusal on a short row and the unaffected real-parent case).

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

Removed compatibility shims (post-roadmap deep audit): the old-queued-message
aliases `run_agent4_for_content` (→ `run_agent3_audio_for_content`),
`run_agent5_for_content` (→ `run_agent5_render_for_content`), the
`pickup_short_episodes_awaiting_parent` no-op task, and the
`ensure_child_short_audio_enqueued` no-op helper are deleted. None was in the
Beat schedule, none had a caller anywhere in the repo, and the architecture
is self-healing against a stale queued message referencing them: every
pickup task is status-driven and re-polls every 15 minutes, so a dropped
unknown-task message costs at most one Beat cycle of latency. This resolves
the "delete once queues are confirmed drained" caveat from the roadmap 6.3
dead-code cleanup — the operator authorized removal in the post-roadmap
deep audit.

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

**Gender-aware voice selection (roadmap Phase D1, gender-aware voice
configuration):** `ChannelVoice` now allows one row per
`(channel_id, language, gender)` (migration `012_add_channel_voice_gender.py`
— every pre-existing row backfills to `gender="feminine"` via
`server_default`, identical to the single-voice behavior every channel had
before this phase). The voice map built at the top of
`run_audio_generation()` changed from `dict[str, ChannelVoice]` (language
only) to `dict[str, dict[str, ChannelVoice]]` (language → gender):

- `_resolve_target_gender(content, db)` reads `protagonist_gender` from
  `content.story_blueprint` (§9.3) and normalizes anything other than a real
  `"feminine"`/`"masculine"` value — `"unspecified"`, missing entirely (a
  legacy blueprint predating this phase), or a malformed blueprint — to
  `"feminine"`. Child Shorts never get their own blueprint (only parent
  script generation runs `generate_story_blueprint()`), so a child reads its
  parent's blueprint via `parent_content_id`, the same convention
  `agent5_render`'s `_resolve_proper_nouns_for_content()` (§11A.4, roadmap
  Phase B2) already established for this exact parent/child blueprint-
  sharing need. Called once per content, before the per-language loop.
- `_select_channel_voice(voices_by_lang, language, target_gender)` — an
  exact `(language, target_gender)` match wins. Otherwise falls back to
  whichever gender IS configured for that language (there is at most one
  alternative, since gender is binary today) and logs
  `VOICE_GENDER_FALLBACK language=<lang> wanted=<gender> used=<gender>` at
  WARNING (CLAUDE.md §3's "no silent fallbacks" rule) — content must never
  fail or skip a language solely because its preferred gender isn't
  configured. Returns `None`, unchanged from the pre-existing behavior, only
  when the language has no voice configured at all ("No voice configured for
  lang=%s — skipping").
- The operator may configure just feminine, just masculine, or both per
  language — nothing requires both. `activation_readiness.py`'s voice check
  (§8.3, rule 4) already only required "at least one row per language" via a
  set comprehension over `channel.voices`, so it needed no code change when
  the DB started allowing a second row per language — verified directly,
  not assumed.
- `protagonist_gender` is also threaded into
  `generate_native_script()` for translated-script grammatical agreement;
  voice choice and wording consume the same persisted blueprint fact.

Runtime proof: `tests/test_gender_aware_voices.py` — real
`generate_story_blueprint()` call (only `call_claude_structured` stubbed)
proving `protagonist_gender` survives untouched; `_resolve_target_gender()`
and `_select_channel_voice()` unit tests covering every normalization/
fallback/child-inheritance case; and a real `run_audio_generation()`
integration test (same `_FakeDb` pattern as
`test_audio_duration_transcript_alignment_and_props_mirror.py`) proving a
masculine-protagonist blueprint actually selects the masculine
`ChannelVoice` row passed into the real `generate_audio()` call — not just
that the helper functions return correct values in isolation — including
the child-inherits-parent-gender path through a real `db.get()` lookup.

**Whisper resume skips re-transcription when a transcript already exists
(Elimination Mandate D1.6, `code_report/forensic_output_audit_borrasca_run.md`):**
when the audio file for a (content, language) pair is already on disk (the
existing TTS-skip branch) *and* an `AudioFile` row already exists for that
pair with both a non-zero `duration_ms` and a non-empty `whisper_transcript`,
`run_audio_generation()` reuses those stored values verbatim and skips both
the ffprobe duration re-measurement and the `transcribe()` call entirely
(logged as `RESUME_TRANSCRIPT_REUSED`). A real production run showed a
resumed/retried content item paying for Whisper transcription of the same
616-second audio file up to 3 times with zero new information each time —
the audio on disk had not changed, so re-transcribing it could never produce
a different result. If the existing row is missing a transcript or duration
(e.g. an interrupted prior run), the code falls through to the
`measure_audio_duration_ms()` + `transcribe()` path unchanged (roadmap 2a —
`measure_audio_duration_ms()` itself replaced the mutagen header read this
paragraph used to describe; see `generate_audio`/§10.4 below) — this is a
reuse shortcut for the unchanged-audio case only, not a new
skip-Whisper-always behavior.

**Language second chance + loud missing-language marker (never-block
follow-up, run 41f7eeb8):** a real run lost `fr_main` to ONE ElevenLabs read
timeout on section 6/6 — the per-language `except` continued, the run logged
`1/2 language(s) succeeded → AUDIO_DONE`, and FR silently never existed
again. Now: a TTS/storage-failed language is re-queued for exactly one more
full attempt at the END of the run (`AUDIO_LANGUAGE_RETRY`; skip-on-disk
makes already-generated parts cheap), and a language that fails both
attempts logs `AUDIO_LANGUAGE_MISSING` at ERROR while the content still
proceeds to `AUDIO_DONE` — never block, never silent. Re-running Agent 3
for the content backfills just the missing language. Upstream of this,
`tts._call_tts_with_transport_retry()` gives every per-section provider
request 3 total attempts with short backoff (`TTS_TRANSPORT_RETRY`;
`TypeError` propagates immediately — the Cartesia SDK-shape signal), and
`elevenlabs_client.get_client()` constructs the SDK with `timeout=300.0`
(the ~60s default read timeout was the root cause). Transport retries, not
quality re-rolls — Elimination Mandate-compatible. Runtime proof:
`tests/test_transport_retry_and_language_second_chance.py`.

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

#### `measure_audio_duration_ms`

File:

```text
app/agents/agent3_audio/services/storage.py
```

**The single duration-measurement point for every caller** (roadmap 2a /
audit P0-1, `code_report/forensic_output_audit_borrasca_run.md`) — used by
both `save_audio()` (fresh generation) and the on-disk resume path in
`run_audio_generation()`. Root cause this replaces: a real production run
stored `duration_ms=161724` for a real 616,835 ms audio file. The prior
measurement (`mutagen.MP3(path).info.length`) reads only the first frame
header it finds; combined with a since-deleted raw-byte-concat fallback (see
`generate_audio` below), a multi-chunk mp3 with several concatenated headers
was measured as just its first chunk's length. That corrupted duration then
poisoned every downstream timeline: Agent 4's beat budget, timestamp
mapping, the hold cap, Remotion's composition length (the final video cut
off 71% of the narration), and the WPM calibration rolling window
(`script_estimator`).

`measure_audio_duration_ms(path)` shells out to `ffprobe -show_format` and
reads `format.duration` — the container's real stream length, not a single
frame header — so it is correct regardless of how many chunks were
concatenated into the file. It raises `RuntimeError` (fail loud) if ffprobe
is unavailable, exits non-zero, or reports no parseable duration, rather
than silently returning a wrong value — a corrupting fallback is worse than
a failed run. `mutagen` is no longer a dependency of this codebase (removed
from `requirements.txt`); no code path reads an audio file header directly
for duration.

#### `_assert_duration_transcript_alignment`

File:

```text
app/agents/agent3_audio/services/audio.py
```

**Cross-timeline invariant (roadmap 2b / audit P0-2,
`code_report/forensic_output_audit_borrasca_run.md`).** Three independent
timelines flow into a render: `AudioFile.duration_ms`, Whisper word
timestamps, and `VideoSection` spans derived from them. A real production
run shipped with `duration_ms=161724` and the SAME row's
`whisper_transcript` already showing a last word ending at ~616,580 ms —
nothing anywhere compared the two, so the corrupted duration silently
poisoned Agent 4's beat budget, timestamp mapping, the hold cap, Remotion's
composition length, and the WPM calibration rolling window. One assertion —
`duration_ms` within ~2% of Whisper's last word end, fail loud otherwise —
stops this class of corruption at Agent 3, before any Claude, Flux, or
render spend.

Called in `run_audio_generation()` immediately after the final `transcript`
value is known (whether freshly transcribed or reused from D1.6's resume
path — see §10.4's Whisper resume rule above), before `_upsert_audio_file()`
persists anything:

- `transcript` empty → returns `True` unconditionally (a no-op pass). This
  is not a rule exemption: parent long-form content already tolerates a
  missing transcript (falls back to proportional timing downstream, see
  `_assert_short_audio_has_transcript`); there is nothing to compare
  `duration_ms` against when no transcript exists.
- `transcript` non-empty → `drift = |duration_ms − last_word.end_ms| /
  duration_ms`. `drift > 0.02` (2%) fails that language: logs
  `AUDIO_DURATION_TRANSCRIPT_MISMATCH` at ERROR, rolls back, `continue`s
  (not counted toward `success_count`) — the same fail-that-language,
  not-the-whole-run convention `_assert_short_audio_min_duration()` and
  `_assert_short_audio_has_transcript()` already use.
- Applies to **both parent and child content** — unlike the two Shorts-only
  assertions above, the corruption this guards against hit *parent* audio
  in the audited run, not just Shorts.

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
  deterministic silence pad between chunks to avoid abrupt section-boundary
  joins. **ffmpeg re-encoding is mandatory whenever there is more than one
  chunk (roadmap 2a / audit P0-1,
  `code_report/forensic_output_audit_borrasca_run.md`)** — there is no raw
  byte-concat fallback: `_concat_mp3_chunks()` raises `RuntimeError` if
  ffmpeg is missing, or if the silence-pad generation or concat step fails.
  A real production run showed the deleted fallback (used when ffmpeg was
  missing) produce an mp3 with multiple concatenated headers, which a
  header-reading duration measurement then silently read as just the first
  chunk's length (161.7s stored for a real 616.8s file) — corrupting every
  downstream timeline. A failed run is recoverable; a silently corrupting
  fallback is not.
- Section delivery is selected in Python from section metadata only: INTRO uses
  a restrained curious delivery; early body sections use tense buildup;
  reveal/climax-titled sections use scared/faster delivery, including common
  blueprint-turn variants such as "reveals" and "exposes"; OUTRO uses a slower
  somber delivery.
- Missing/unknown section metadata and child-short flat narration fall back to
  the configured channel-level emotion and speed profile; audio generation must
  not fail solely because section delivery cannot be inferred.
- ElevenLabs v3 uses the same deterministic section delivery selector as Cartesia, resolves `VoiceSettings` per section, and adds one deterministic v3 audio tag per marked section; non-v3 ElevenLabs models remain the configured provider/legacy char-chunk path.
- Voice, model, pronunciation dictionary, and fallback delivery choices come from `channel_voices`.
- **Loudness normalization (roadmap Phase A3, operator video-output audit):**
  a real production run measured a parent's EN narration at -22.4 LUFS
  against the SAME content's FR narration at -18.3 LUFS — no loudness
  normalization existed anywhere in the pipeline, so perceived volume varied
  by provider/voice/language with nothing correcting it.
  `tts._normalize_loudness(mp3_bytes)` applies a single-pass ffmpeg
  `loudnorm` filter (`I=-15.0:TP=-1.0:LRA=11.0` — raised from the original
  `-17.0`/`-1.5` per an independent review of real output,
  `code_report/8abd7fea_independent_video_output_review.md` finding 8, which
  measured accurate but short-form-platform-quiet output at the prior
  target; -15 LUFS matches TikTok/Reels/Shorts norms while -1.0 dBTP still
  leaves real headroom under clipping — output re-encoded at
  192kbps) — a deliberate simplicity tradeoff (one pass, not the more
  precise two-pass measure-then-apply variant): the goal is consistent,
  reasonable loudness across languages/providers, not broadcast-spec-exact
  loudness. ffmpeg is already a hard dependency of this pipeline
  (`_concat_mp3_chunks()`, `measure_audio_duration_ms()`), so a missing/failing
  ffmpeg raises `RuntimeError` here the same way those do rather than
  silently skipping normalization. Applied unconditionally at both of
  `generate_audio()`'s return points — the single point every provider path
  (Cartesia and ElevenLabs, single-chunk and multi-chunk alike) funnels
  through — so no call site needs its own opt-in. Empty input returns empty
  output unchanged. Never runs on the skip-on-disk TTS resume path (an
  already-generated file on disk was normalized once, at generation time,
  and is trusted thereafter — same convention as CLAUDE.md §10.3's
  `RESUME_TRANSCRIPT_REUSED` reuse contract). Logs
  `AUDIO_LOUDNESS_NORMALIZED` with target/input/output sizes. Runtime proof:
  `tests/test_loudness_normalization.py` — real ffmpeg both to generate test
  audio at known loudness and to independently re-measure the normalized
  result via a second `ebur128` pass (not a mock of the effect); covers a
  quiet source pulled up to target, a loud source pulled down to target, the
  ffmpeg-failure error path, and that both `generate_audio()` provider
  branches actually route their result through it.
- **Interior-silence compression (pre-next-test roadmap Tier 1 R1):** a real
  production run measured 31% of a 12:40 video as near-digital silence (219
  gaps ≥0.5s, median 1.1s) — paragraph-per-sentence scripts × ElevenLabs v3
  per-utterance pauses, with no pause bound anywhere.
  `tts._compress_interior_silence(mp3_bytes)` uses
  `settings.audio_max_interior_silence_ms` (default 650; ≤0 disables) via a
  deterministic two-pass ffmpeg: a read-only `silencedetect` pre-pass returns
  the input **unchanged** when no qualifying silence exists (no re-encode
  generation loss); otherwise an `atrim`/`concat` segment splice keeps the
  two longest non-leading gaps per chunk at up to 900 ms; every other
  qualifying gap retains 650 ms plus `_ORDINARY_SILENCE_EXCESS_RETENTION`
  (30%) of its own excess above that (bounded by the 900 ms ceiling) rather
  than being flattened to one identical duration — an independent review of
  real output (`code_report/8abd7fea_independent_video_output_review.md`
  finding 1) measured this flattening as an audible, metronomic "assembled
  from chunks" rhythm, since every capped pause came out bit-for-bit
  identical. Leading silence is never removed.
  **Applied per TTS chunk, in both
  provider paths, BEFORE `_concat_mp3_chunks()` and BEFORE
  `_compute_section_boundaries()`** — the chunk list handed to boundary
  computation is the compressed one, so `section_boundaries`, Whisper,
  beats, and captions all derive from the same compressed audio and can
  never desync (compressing the final file after boundaries were measured
  would corrupt Phase B1's spans non-uniformly). Leading chunk silence is
  never trimmed; a chunk's trailing silence is capped (it sits between
  sections after concat). Fails loud (`RuntimeError`) on ffmpeg failure,
  same contract as loudnorm/concat. Never runs on the skip-on-disk resume
  path. Logs `AUDIO_SILENCE_COMPRESSED` with the normal cap, dramatic
  count/cap, input/output duration, and removed duration. Runtime proof:
  `tests/test_tier1_audio_continuity.py`
  (real ffmpeg synthesizes known speech/silence layouts and independently
  re-measures results, including the compression-before-boundaries ordering
  proof through the real `generate_audio()` chain).
- **Post-normalization section loudness arc:** long-form section metadata now
  carries the existing delivery-selector reason into finalization. After
  loudnorm, one deterministic ffmpeg pass applies intro −1.0 dB, early
  buildup 0 dB, late buildup +1.0 dB, climax +1.5 dB, and outro −1.5 dB.
  The arced file is re-measured and `section_boundaries` are recomputed
  against its actual duration, so no timing consumer can drift. Flat Shorts
  and paths without trustworthy section metadata are unchanged.
- **`generate_audio()` returns `tuple[bytes, section_boundaries]`, not bare
  bytes (roadmap Phase B1):** the second element is a list of
  `{"section_type", "section_index", "start_ms", "end_ms"}` dicts describing
  each `[INTRO]`/`[SECTION N]`/`[OUTRO]` section's real span in the final
  audio — `[]` when the script had no section markers, or the provider/model
  path can't guarantee one TTS chunk per section (the legacy non-v3
  ElevenLabs char-limit chunking path, `_chunk_script_at_sections()`, can
  merge several sections into one chunk to stay under `max_chars`; boundary
  tracking is skipped entirely for it rather than computed from
  unreliable data). Cartesia (always one chunk per section) and ElevenLabs
  `eleven_v3` (same) both track it; every other ElevenLabs model does not.
  `tts._generate_cartesia_audio()` likewise returns a 3-tuple (`concatenated
  bytes, per-chunk bytes list, per-chunk section metadata`) so
  `generate_audio()`'s shared `_finalize_audio_with_boundaries()` helper
  (normalize loudness, then compute boundaries) can consume either
  provider's chunks identically.
- **`tts._compute_section_boundaries(section_meta, chunk_bytes_list,
  final_duration_ms)`** is the deterministic (no AI call) boundary
  calculator: it ffprobe-measures each pre-concatenation chunk's own real
  duration, builds cumulative *raw* boundary points using the same fixed
  silence pad `_concat_mp3_chunks()` inserts between chunks, then rescales
  every point by one shared factor so the sequence sums exactly to
  `final_duration_ms` — the real, independently-measured duration of the
  actual persisted (concatenated + loudness-normalized) audio. Building
  shared boundary points (not independently-computed start/end per section)
  is what guarantees section *i*'s end always equals section *i+1*'s start
  exactly — no beat can ever fall into an unmapped gap between two sections.
  `AudioFile.section_boundaries` (§5.3) is where this list is persisted;
  `_remap_beats_timing()` (§11.4) is its consumer.
- On the audio-persist side, `audio.py`'s `run_audio_generation()` only has
  real section boundaries on a **fresh** generation, or when reusing an
  already-persisted row whose transcript was also reused (D1.6
  `RESUME_TRANSCRIPT_REUSED` — the same on-disk-file-unchanged reuse
  contract this section already documents). Any other resume path (file on
  disk but no valid prior `AudioFile` row) sets `section_boundaries=None` —
  the final concatenated file alone cannot reconstruct per-section timing —
  which `_remap_beats_timing()` already treats as "no data available" and
  falls back to whole-video stretch for, no regeneration forced.

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

The ElevenLabs path (`provider="elevenlabs"`) now branches by model. For
`eleven_v3`, long-form section-marked scripts use `_split_script_into_section_units()`
just like Cartesia, run `_select_section_delivery()` for each section, resolve
per-section `VoiceSettings`, and prefix one deterministic v3 audio tag per
section (`[whispers]` for intro/early buildup, `[dramatic pause]` for late
buildup, `[gasps]` for climax-titled sections, `[sighs]` for outro). This keeps
horror/reveal/somber delivery from being silently discarded when an operator
chooses ElevenLabs v3. Non-v3 ElevenLabs models keep the legacy character-limit
chunking path (`_chunk_script_at_sections()`) and channel-level delivery.
Every ElevenLabs chunk still runs through `prepare_script_for_tts()` (so Phase
11.5's reveal-gated deterministic pacing applies identically). The three
independent v3 tag sources never stack on one sentence (fresh full-system
audit §2.6 P-6): the pacing inserter skips a sentence already carrying any
tag, and the per-section delivery prefix replaces a generic leading
`[dramatic pause]` slow-open (the more intentional tag wins) but yields to
any other script-embedded leading tag — v3's contract is max one tag per
sentence. Additionally (pre-next-test roadmap Tier 1 R3), the generic
slow-open is suppressed for the whole section when the section's script
text already carries an **authored** v3 tag anywhere — captured at
`_apply_pacing_markers()` entry, before any insertion, so tags the function
itself inserts never count: a section that embeds its own
AUDIO_TAGS_INSTRUCTION tag already has intentional pacing, and stacking the
generic slow-open onto it measurably inflated dead air in a real run.
Text-conditioning
(`previous_text`/`next_text`) remains non-v3 only when the SDK supports it, and
all multi-chunk/multi-section output still passes through `_concat_mp3_chunks()`
(Phase 11.6) for final concatenation.

French ElevenLabs v3 voices use the existing per-voice stability path.
Migration 015 sets matching French v3 rows without an explicit numeric
`stability_override` to `v3_stability_preset="robust"` (0.85), addressing
measured French prosody fragmentation without a new provider call or a
language-specific runtime branch. The next operator run must measure the
voice-level effect; code cannot predict provider prosody magnitude offline.

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

**Storyboard prompt inputs (`PROMPT_VERSION` 4.9, `STORYBOARD_SCHEMA_VERSION`
7.2):** the dead "Visual Continuity Bible"
prompt section is replaced by documentation of the real one-line
"Visual continuity:" hint; the TECHNICAL flux-prompt step is image-style-aware
(never appends "photorealistic" to an illustrated/painted style); the
per-10-beat composition quota block is deleted (it contradicted Principle A
and pushed toward the document-saturation pattern check 21 flags); the
low-intensity band is 3.0–6.0 s (matching the hard constraint and
INTENSITY_FLOOR_MS); `visual_type="text_overlay"`/`visual_category="text"`
and the never-read `storyboard_status`/`global_notes` fields are removed from
the schema; and the style vocabulary documents every UI preset
(constants.js). Details below reflect the surviving structure.

**`dip_to_black` removed from the transition vocabulary (roadmap Phase A2,
`PROMPT_VERSION` 4.6 → 4.7, `STORYBOARD_SCHEMA_VERSION` 7.1 → 7.2, operator
video-output audit):** a real production run measured ~13 `dip_to_black`
beats in one parent video (≈8.7s of full-black frames while narration
continued uninterrupted) — a mid-narration fade to black reads as a
rendering failure to a viewer, not an intentional edit. `"dip_to_black"` is
removed from `_BEAT_SCHEMA`'s `transition_to_next` enum and from the prompt
text enumerating the choosable values (both in `system_prompt.py`); Claude
can no longer select it for new beats. `storyboard._VALID_TRANSITIONS`
(the Python-side enforcement `_safe_enum()` uses in `_build_beat_section()`)
drops it too, so a stray `"dip_to_black"` value from any source (a stale
in-memory dict, a non-forced-tool-use path) normalizes to
`_DEFAULT_TRANSITION` (`"cut"`) rather than passing through. Already-persisted
legacy rows are handled at the render layer instead: `MediaSection.tsx`
treats a legacy `dip_to_black` transition as a plain crossfade (same 15-frame/
0.5s dissolve, no fade-through-black `filter` styling) — old rows render
sanely without a data migration. `remotion/src/types.ts`'s `Transition` union
keeps `"dip_to_black"` for type-compatibility with those legacy rows only; no
code path writes it anymore. Runtime proof: `tests/test_dip_to_black_removed.py`
(schema/prompt no longer offer it; `_safe_enum()` normalizes a stray value;
the real `_build_beat_section()` chain proves it can never survive into a
freshly-built beat) plus a clean `tsc --noEmit` compile for the Remotion side.

**Storyboard prompt inputs:**
`generate_storyboard_batch()` accepts optional `visual_style`, `image_style`,
and `continuity_line` arguments. `split_into_beats()` threads those from the
parent visual path and from the validation retry path, so both the first
storyboard pass and a MAJOR-finding retry see the same continuity hint.

When visual style fields are non-empty, `_build_message()` prepends:

```
Global visual direction: <visual_style or "story_driven">
Global image style: <image_style or "photorealistic">
```

**De-hardcoded "documentary" (roadmap 4a / audit P1-9):** the storyboard
prompt's identity line used to open "You are a visual director and editor
for an automated multilingual **documentary** video production system" —
regardless of the channel's actual configured niche/tone/visual_style. It
now reads "...for an automated multilingual video production system",
dropping the assumed format entirely, since the actual style is always
channel-configured via the "Global visual direction"/"Global image style"
lines above, never assumed by the identity line. Both runtime fallback
defaults (`visual_style or "documentary"` in the Python f-string above, and
the prompt's own "when no global direction is specified... default to
documentary/photorealistic" instruction) changed to `"story_driven"` —
matching `ChannelConfig.visual_style`'s new default (§8.1). `"documentary"`/
`"realistic_documentary"` remain listed as legitimate example values in the
style-vocabulary section (§11.4's checks table below is unaffected) — only
the assumed-when-unconfigured default changed.

When `continuity_line` is non-empty, `_build_message()` also prepends that
one plain line verbatim (no JSON, no compaction) — e.g.:

```
Visual continuity: these names recur throughout this story — give each a
stable, consistent physical identity/appearance across beats: Sam, Whitney,
Borrasca.
```

**`continuity_line` gained two more lines (roadmap Phase C2/C3, operator
video-output audit):** `build_continuity_line_from_blueprint()` now returns
up to three newline-joined sentences — the pre-existing recurring-proper-noun
line above, plus:

```
Character identities (use these exact physical descriptions whenever
depicting them, in every beat, for consistency): Belisarius, mid-30s — tall,
dark beard, bronze cuirass.
Era/setting lock: this story is set in 6th-century Byzantine Constantinople
— every beat's props, clothing, architecture, technology, and background
details must be authentic to this period and place; no anachronistic modern
elements unless the story is explicitly contemporary.
```

Both lines are built from `content.story_blueprint`'s `character_descriptors`/
`era_setting` fields (§9.3) by `_format_character_descriptors_line()`/
`_format_era_setting_line()` — pure formatting, no AI call, each independently
skipped (returns `""`) when its blueprint field is absent/empty, including for
every blueprint generated before this phase shipped (no regeneration forced).
This directly targets two real production defects: a story's protagonist
rendered as 6+ visually different women with nothing enforcing consistency,
and modern anachronisms (a modern flag, a modern waterfront, contemporary
clothing) inside a 6th-century Byzantine story.

**Style-negative-constraints per `image_style` (roadmap Phase C2):** the same
run also shipped ONE video mixing photoreal-cinematic, flat cartoon, and
anime-style beats. `_IMAGE_STYLE_NEGATIVE_CONSTRAINTS` (a deterministic
Python dict keyed by the 9 canonical `image_style` values, kept in sync with
`app/ui/src/constants.js`'s `IMAGE_STYLE_OPTIONS` per §8.4) adds one more
`direction_lines` entry whenever `image_style` matches a known key:

```
Style constraints (this image style must NOT look like): no photorealism, no
live-action photography, no anime linework, no comic-book panel borders, not
a flat/childish kids-cartoon look
```

An unrecognized/custom `image_style` value gets no constraint line — fail-open,
matching the existing "apply the closest reading for any other string"
convention already used for `visual_style`. The prompt instructs Claude to
honor this by choosing `flux_prompt`'s own descriptive words, never by
copying the negative phrase into `flux_prompt` itself (which stays purely
positive/descriptive, per the pre-existing "no Avoid: suffix" rule).

**Beat-count and hint-length prompt tightening (roadmap Phase C4):** two real
production defects — beat under-delivery (Claude returning as few as ~65% of
the requested `target_beat_count`, e.g. 12 of 18) and a 100%-invalid-hint-rate
segment from under-length `start_hint`/`end_hint` values — traced to soft
prompt language ("aim for this count", a bare "6-10 words" range with no
stated floor rationale). Both the strict-rules beat-count instruction and the
per-beat hint-length instruction were rewritten as explicit requirements with
stated consequences (a low beat count forces long static holds that read as a
slideshow; a short hint is ambiguous and gets treated as lower-confidence,
degrading that beat's timing accuracy) — prompt-only, no schema change, so
`STORYBOARD_SCHEMA_VERSION` did not bump (only `PROMPT_VERSION`, 4.7 → 4.8).

**Beat/timestamp alignment hardening (pre-next-test roadmap Tier 3,
`PROMPT_VERSION` 4.8 → 4.9; schema unchanged):** the Hannibal audit found five
consecutive montage beats claiming one identical oath/frostbite narration span;
four could not produce distinct anchors and were interpolated over later audio.
It also found 113/360 hints below the six-word floor.

- `_harden_hints()` now expands a 1–5-word hint deterministically from its real
  occurrence in the current segment text. Start hints extend right and end hints
  extend left to six words (with boundary-only expansion in the other direction),
  using the existing language-aware matching normalization and literal-quote
  stripping. Searches advance with beat order so a repeated phrase cannot jump
  back to an earlier occurrence. Already-valid hints are untouched; an unlocatable
  or otherwise unsafe hint is also untouched and remains telemetry-only in
  `HINT_QUALITY_SUMMARY`. No AI call or regeneration loop is involved.
- After `_merge_batches()` and immediately before timestamp mapping,
  `_collapse_duplicate_hint_runs()` merges every consecutive run whose normalized
  `(start_hint, end_hint)` pair is identical. The first beat/`flux_prompt` stays
  authoritative, raw suggested durations are summed then clamped once to that
  beat's intensity tier, the run's final transition is retained, and global beat
  order is renumbered. Log: `STORYBOARD_DUPLICATE_HINT_RUN_COLLAPSED` (INFO).
- The storyboard prompt now forbids consecutive identical hint pairs, requires a
  montage over one narration span to be one strongest-image beat, and requires a
  sensory reveal's `visual_intent`/`flux_prompt` to depict the reveal in the beat
  containing it rather than an earlier callback or later phrase.
- A successful per-section language remap now logs
  `VISUAL_TIMING_SECTION_ANCHORED` at INFO (formerly DEBUG); its fallback remains
  INFO. Normal production logs therefore identify which remap path actually ran.

Runtime proof: `tests/test_tier3_beat_timestamp_alignment.py` (8 tests, no live
API calls; the sole prompt boundary is stubbed) covers normalized five-beat run
collapse through the real timestamp mapper, tier-capped duration/transition/order
behavior, quote-stripped expansion, forward repeated-phrase selection,
idempotence, telemetry-only unsafe fallback, real prompt assembly, and the INFO
anchoring marker. Full offline suite: 748 test methods + 145 subtests, zero
regressions.

Runtime proof for all of Phase C2/C3/C4 above:
`tests/test_character_era_style_continuity.py` — real `generate_story_blueprint()`
call (only `call_claude_structured` stubbed) proving `character_descriptors`/
`era_setting` survive untouched; `_format_character_descriptors_line()`/
`_format_era_setting_line()`/`build_continuity_line_from_blueprint()` unit
tests including the legacy-blueprint (missing-keys) degrade-to-pre-existing-
output proof; and a real `generate_storyboard_batch()` call (only
`call_claude_structured_with_usage` stubbed) capturing the actual constructed
`user_message` to prove the character-identity line, era-lock line, and
per-`image_style` constraint line all reach the real Claude-call boundary —
not just that the formatting functions produce correct strings in isolation.

**The entire visual-bible layer is deleted (Elimination Mandate, roadmap
`code_report/forensic_output_audit_borrasca_run.md` D2.1):** the Claude
generation call, `cinematic_prompts.compact_visual_bible_for_storyboard()`,
`apply_cinematic_prompts_to_beats()` enrichment, and first-15
enhancement/validation are all gone — `app/agents/agent4_visuals/services/cinematic_prompts.py`
and `.../first15_validator.py` no longer exist as files. A real production
run showed the bible return 0 locations, 0 recurring motifs, and 0 first-15
rules — a paid Claude call and token cost on every storyboard batch for a
total no-op. `continuity_line` (see `storyboard.build_continuity_line_from_blueprint()`)
replaces it: a pure-Python, no-AI-call extraction of proper nouns (character
names, named places/objects) that recur at least twice across the blueprint's
own text fields (`hook`, `major_turns`, `final_payoff`,
`midpoint_retention_trap`, `central_question`) — the one continuity signal
genuinely worth carrying into every storyboard batch. Built once per content
in `visual_orchestrator._run_visual_pass()` from `content.story_blueprint`
and threaded as a plain string through both the fresh-generation and
segment-level retry calls in `storyboard.py` — never regenerated per segment,
never persisted as a separate artifact.

**Final prompt validation is deleted (D2.4):**
`visual_orchestrator._check_final_prompt_issues()` and its
`_FINAL_PROMPT_VALIDATION_CHECKS` constant no longer exist. That function
existed only to re-validate `flux_prompt` after the bible-enrichment and
first15-enhancement mutation passes — with both of those passes deleted,
there is nothing between storyboard generation and Flux that mutates
`flux_prompt`, so there is nothing left to re-validate. The single
deterministic `validate_storyboard()` call that already runs right after
storyboard generation (`_run_storyboard_validation()` /
`_check_storyboard_issues()`) remains as the sole validator, per D3 of the
mandate ("the single deterministic `validate_storyboard()` remains as
telemetry").

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
(`""`/`""`/`[]`) rather than raising. Expected missing fields log
`STORYBOARD_SHAPE_AUX_FIELD_MISSING` at DEBUG; a present but wrong-typed value
still logs `STORYBOARD_SHAPE_AUX_FIELD_WRONG_TYPE` at WARNING because it is a
real schema mismatch. Neither case loosens validation on `beats` itself
(still fatal + retried if missing or wrong-typed, exactly as before). Runtime proof:
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

`suggested_duration_sec` input sanitization (prompt engineering audit §2.2 —
protects the inputs the three guards below depend on):

- The storyboard prompt states `suggested_duration_sec` must fall within its
  `beat_intensity` tier as a "hard constraint" (high 1.0-2.5s, medium
  2.5-4.0s, low 3.0-6.0s), but nothing enforced it before this fix — and this
  value is not cosmetic: it directly feeds the expected-position accumulator
  the hint-search proximity window compares matches against, and the
  anchor-span-sanity gap threshold, both below. An out-of-range value from
  Claude silently skewed the accuracy of both guards.
- `storyboard._normalize_suggested_duration_sec(value, beat_intensity)`
  clamps the raw value into `_INTENSITY_MIN_DURATION_SEC`/
  `_INTENSITY_MAX_DURATION_SEC` (mirroring the prompt's own tier bounds
  verbatim — deliberately separate from `INTENSITY_FLOOR_MS`, which floors
  the actual POST-timestamp-mapping beat span, a different concern). Missing/
  non-numeric/non-finite input falls back to the tier's midpoint. Applied in
  `_build_beat_section()`, the same "Python enforces, never trusts Claude's
  values blindly" convention already applied to every enum field there.
- A clamp firing is logged as `STORYBOARD_DURATION_OUT_OF_TIER`
  (beat_order/intensity/raw/normalized) — not a failure, just visibility
  into how often Claude's raw values disagree with its own stated rule.

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

- Even if a bad anchor slips past BOTH guards above, no non-terminal direct-
  storyboard beat may exceed its content-shape ceiling: parents use
  `settings.parent_visual_max_hold_ms` (default 9000 ms), while Solo Shorts
  use `settings.short_visual_max_hold_ms` (default 6000 ms). A lowered parent
  value of 7000 ms (independent review of real
  output, `code_report/8abd7fea_independent_video_output_review.md` finding
  9) was tried and reverted: a real run
  (`code_report/36e0ec63-3ce1-467e-aaba-251a9a85844c_logs.txt`) showed the
  tighter cap forced a chain of consecutive over-cap beats near the end of a
  116-beat parent video, which pushed the terminal beat's forced start past
  its own fixed end — a zero-width last section that failed Agent 5's props
  sanity check and failed the render for both languages. Applied in
  `split_into_beats()` after timestamp mapping
  via `_apply_visual_hold_cap()` — the same shared core the child Short path
  uses at its tighter `settings.short_visual_max_hold_ms` (6000 ms, same
  revert history) ceiling —
  by shortening the over-cap beat and advancing the next existing beat. It
  never creates synthetic beats and never touches audio/subtitle timing; a
  terminal beat with no later beat to absorb the remainder is exempt. Each
  shortening is now clamped so the receiving beat always keeps at least
  `_HOLD_CAP_MIN_REMAINING_SPAN_MS` (500 ms) of its own span — a defensive
  floor added alongside the revert so this cascading-collapse class of bug
  can't recur even if either cap is tuned again in the future; a clamp
  engaging is logged (`*_CASCADE_CLAMPED`), never silent.
- The cap is deliberately selected from `is_short_episode` and applied in
  `split_into_beats()` (parent and Solo Short direct-storyboard paths) and
  `remap_beats_for_short()` (child path), after the shared
  `map_storyboard_beats_to_timestamps()` cleanup/micro-merge step — the two
  paths need different ceilings and both must cap after cleanup.
- Parent hold-cap telemetry must include the real `content_id` when called
  from Agent 4 orchestration; `content_id=unknown` is allowed only for
  direct unit/helper calls that do not have content context.
- `map_storyboard_beats_to_timestamps()` receives the caller's cap as
  `max_hold_ms` for deterministic fail-loud invariants only; it still does
  not mutate the holds itself. After micro-beat cleanup,
  `beats_after_cleanup * max_hold_ms` must be at least `duration_ms`, or the
  mapper returns `None` and logs `VISUAL_DENSITY_INVARIANT_FAILED`. This is
  the missing `beat_count × max_hold ≥ duration` guard from audit P1-1.
- Micro-beat cleanup clamps every boundary to the real audio duration and
  counts how many beats collapsed to zero-length after cleanup. If more than
  30% are effectively discarded, mapping fails loud with
  `MICRO_MERGE_FAIL_DISCARD_RATIO` instead of shipping a sparse visual plan.
  Non-fatal discard telemetry is logged as `MICRO_MERGE_DISCARDED`.
- Logs: `PARENT_VISUAL_HOLD_CAP_APPLIED` (parent) /
  `SHORT_VISUAL_HOLD_CAP_APPLIED` (child), with cap, capped-beat count,
  terminal exemption, and before/after max hold.
- Runtime proof: `tests/test_parent_visual_hold_cap.py` (shared-core unit
  checks plus the real `split_into_beats` chain — segment split, hint
  hardening, mapping with the guards above, cap — with only the paid
  `generate_storyboard_batch` Claude call stubbed) and
  `tests/test_visual_density_invariant.py` (density invariant, >30%
  micro-merge discard, and post-cleanup cap behavior).

#### `_run_parent_visuals` — stale-visuals guard (audit V-6b + roadmap 2d / audit P0-3)

File:

```text
app/agents/agent4_visuals/services/visual_orchestrator.py
```

The shared `__visual__` `VideoSection` rows are keyed only by `content_id` —
nothing ties them to the source-language narration or audio they were built
from. Before this guard, `_run_parent_visuals()` reused any existing
`__visual__` rows unconditionally; if the parent's source script was
regenerated after the visual pass already ran (operator `--force-scripts`,
or any retry that produces a materially different script), the OLD beats —
hints, timings, `flux_prompt` subject matter, all describing narration that
no longer exists — were silently reused against the NEW script.

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
  - `"stale"` — stored hash present and differs → discard.
  - `"backfill"` — beats predate this guard (no stored hash at all) →
    stamped with `current_hash` and re-saved unchanged; no regeneration is
    forced for pre-existing content; the next run has a baseline to compare
    against.

**Second, independent fingerprint — source audio duration (roadmap 2d /
audit P0-3, `code_report/forensic_output_audit_borrasca_run.md`):** the
script-hash fingerprint alone does not catch a duration correction on an
*unchanged* script — exactly the audited incident's own repair scenario: the
19 corrupt-timing `__visual__` beats carried a matching `source_script_sha256`,
so a re-run after fixing a corrupted `duration_ms` in place would still say
"fresh" and silently reuse the broken timeline.

- `_source_audio_duration_ms(content, audio_by_lang)` returns the
  source-language `AudioFile.duration_ms`, or `None` when unavailable
  (same fail-open contract as the script hash).
- `_tag_beats_with_audio_duration(beats, duration_ms)` stamps
  `source_audio_duration_ms` onto every beat — same `_beat_extras()`
  persistence convention as the script hash.
- `_check_audio_duration_staleness(content_id, shared_beats, current_duration_ms)`
  — identical `"fresh"`/`"stale"`/`"backfill"` contract, logged as
  `PARENT_VISUALS_STALE_AUDIO_DURATION` on a mismatch.

`_run_parent_visuals()` runs **both** classifications independently and
combines them: **either** fingerprint being `"stale"` discards `shared_beats`
entirely and calls `_run_visual_pass()` fresh (passing both `script_hash` and
tagging the fresh `source_duration_ms`, logged
`PARENT_VISUALS_STALE_SCRIPT_HASH` / `PARENT_VISUALS_STALE_AUDIO_DURATION`
respectively); any `"backfill"` outcome stamps just that missing fingerprint
and re-saves (logged `STALE_VISUALS_CHECK_BACKFILL`) without forcing a
regeneration. The two dimensions are independent — a script-hash match does
not exempt a beat from the duration check, and vice versa.

Runtime proof: `tests/test_stale_visuals_guard.py` (script-hash dimension,
pre-existing) and `tests/test_stale_visuals_audio_fingerprint_and_props_rebuild.py`
(audio-duration dimension and the combined guard) — unit tests for each
helper, plus integration tests driving the real `_run_parent_visuals()`
against a fake in-memory `VideoSection` table (real ORM instances, real
`_beat_extras()`/`_save_video_sections()`/`load_video_sections()` (shared
loader, roadmap 6.5) JSON round trip — only `_run_visual_pass()`, the paid
Claude/fal.ai boundary, is stubbed), including the exact P0-3 repair
scenario: matching script hash, corrected duration → regenerates.

#### `_remap_beats_timing` — per-language visual timing / the parent "décalage" fix (roadmap Phase B1)

File:

```text
app/agents/agent4_visuals/services/visual_orchestrator.py
```

**Root cause:** the shared `__visual__` beats are timed against exactly one
language's real Whisper transcript (`source_language`, normally English).
Every other configured language's beats were previously produced by
`_remap_beats_timing_whole_video()` — one global `ratio =
target_duration_ms / source_duration_ms` applied to every beat — which
implicitly assumes narration pacing is uniform across the whole video. It
isn't: a real production run measured up to ±20s of visual/narration drift
in a non-source language render, growing over the video's length wherever
that language's phrasing pacing diverged from the source's.

**Fix:** each language's real per-section audio spans
(`AudioFile.section_boundaries` — see §5.3/§10.3, captured deterministically
at TTS-generation time, no AI call) let beats be remapped **per
`[INTRO]`/`[SECTION N]`/`[OUTRO]` section** instead of video-wide — bounding
drift to within one section (~2-3s) rather than the whole video's length.

- `_build_section_anchor_map(source_sections, target_sections)` pairs up
  source/target sections 1:1, in order, by `(section_type, section_index)` —
  **never** by label text, which is itself translated per language. Returns
  `(None, reason)` — never guessing a correspondence — when either side is
  missing/empty, section counts differ (a translated script legitimately can
  drop/gain a section: §9.3's `section_loss` check is telemetry-only, not
  blocking), a structural (type/index) mismatch exists at the same position,
  or either side has a degenerate (non-positive) span.
- `_remap_beats_timing()` calls this first; when it returns real pairs, beats
  go through `_remap_beats_timing_per_section()` (each beat mapped using the
  local stretch ratio of whichever section it *starts* within — a beat
  straddling a section boundary is still mapped entirely by its start
  section, a deliberately bounded simplification). Otherwise it falls back
  to the original `_remap_beats_timing_whole_video()`, logged
  `VISUAL_TIMING_WHOLE_VIDEO_STRETCH_FALLBACK reason=<reason>` — never
  silent (Golden Rule 3). A successful anchored remap logs
    `VISUAL_TIMING_SECTION_ANCHORED` at INFO.
- The final "clamp last beat to exactly `target_duration_ms`" step is shared
  by both paths, applied once after either produces its result — unchanged
  from the pre-existing whole-video-stretch convention.
- `_run_parent_visuals()` threads `source_sections` (the source language's
  own `AudioFile.section_boundaries`) and `target_sections` (each language's
  own) into every `_remap_beats_timing()` call in its per-language loop. The
  source language itself is unaffected — `target_duration_ms ==
  source_duration_ms` already short-circuits to a no-op before any section
  logic runs.
- Fully backward compatible with content generated before this phase: a
  `None`/missing `section_boundaries` (pre-existing `AudioFile` rows, or any
  language whose TTS path can't guarantee one chunk per section — see
  §10.3) falls through to the exact pre-existing whole-video behavior, with
  no regeneration forced.

Runtime proof: `tests/test_section_anchored_visual_timing.py` — real
ffmpeg-measured `tts._compute_section_boundaries()` unit tests; a real
end-to-end `tts.generate_audio()` Cartesia-path test (real WAV generation +
real ffmpeg encode/concat/normalize, only a fake `cartesia` SDK module as
the paid boundary) proving real per-section boundaries track each section's
actual (different) spoken duration, not a naive equal split; every
`_build_section_anchor_map()` fallback reason; a concrete side-by-side
comparison proving `_remap_beats_timing_per_section()`'s result differs
from (and stays inside the correct section unlike) what
`_remap_beats_timing_whole_video()` produces on identical input; and a
`_run_parent_visuals()` integration test (same fake in-memory `VideoSection`
table pattern as `test_stale_visuals_guard.py`) proving
`AudioFile.section_boundaries` set for one language really reaches the
persisted beats for a second, independently-processed language.

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
forks or re-implements the validator.

**MAJOR findings are telemetry only on both paths (Elimination Mandate D1.5,
`code_report/forensic_output_audit_borrasca_run.md`):** `_run_storyboard_validation()`
(parent) and the MAJOR-handling block in `_run_child_short_visuals()` (child)
both log MAJOR findings and return the storyboard unchanged — neither
triggers a regeneration of any kind. This replaces two deleted mechanisms:
the parent's segment-level storyboard retry (beats carried
`storyboard_batch_label` provenance from `split_into_beats()` so
`_run_storyboard_validation()` could regenerate only the offending batch
labels via `retry_segment_constraints`/`existing_beats`, falling back to a
full-storyboard retry via `STORYBOARD_SEGMENT_RETRY_FALLBACK` when
provenance was missing) and the child's deterministic beat-level prompt
remediation (`remediate_child_major_storyboard_issues()`, which stripped
forbidden/readable-text language and regenerated `flux_prompt` from
`visual_intent` for `forbidden_flux_word`/`ai_text_rendering_requested`
MAJORs). A real production run showed a failed retry/remediation already
just logged and proceeded with the original beats — since MAJOR findings
never gated the pipeline either way, both mechanisms were surgical
regeneration effort with no provable quality gain. `storyboard_batch_label`
and the rest of the batch-provenance fields (`storyboard_segment_label`,
`storyboard_segment_index`, `storyboard_batch_local_order`) are no longer
stamped onto beats at all — they had no consumer beyond the deleted retry.

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
| 10 | `near_duplicate_beat` — two beats anywhere in the same storyboard/Short share `environment`, `motif`, and `effect` simultaneously | MINOR |
| 11 | `ai_slideshow_risk` — 5+ consecutive beats sharing one value for `environment`, `motif`, or `effect` | MINOR |
| 12 | `subject_presence` — `flux_prompt` has fewer than 2 concrete-subject words after removing style/filler/technical boilerplate | MINOR |
| 13 | `environment_presence` — `flux_prompt` contains none of its declared `environment`'s expected keywords | MINOR |
| 14 | `low_information_prompt` — `flux_prompt` is under 4 words, or ≥50% generic filler words | MINOR |
| 15 | `flux_prompt_exact_duplicate` — `flux_prompt` is character-identical to an earlier beat's in the same storyboard | MINOR |
| 16 | `flux_prompt_near_duplicate` — `flux_prompt`'s word-token set overlaps ≥80% (Jaccard) with an earlier beat's | MINOR |
| 17 | *(retired — fresh full-system audit §3.1: no physical child image reuse exists; the reuse split the check observed is gone)* | — |
| 18 | *(retired — same; the match_score is a prompt-inheritance decision only)* | — |
| 19 | `ai_text_rendering_requested` — `flux_generated` `flux_prompt` contains a quoted phrase or a "the text reads"/"sign that says"-style instruction asking the image model to render readable text (Phase 14.7) | MAJOR |
| 20 | `dark_contrast_unlit_prompt` — a beat uses `color_grade="dark_contrast"` while its `flux_prompt` names no bright light source (`has_bright_lighting_evidence()`); defense-in-depth only — `_build_beat_section()` already downgrades such beats to `desaturated` in place (`DARK_CONTRAST_GRADE_DOWNGRADED`), so this fires only on beats that bypassed beat building (audit G-6) | MINOR |
| 21 | `document_saturation` — document-ish beats (`motif="document"` or `visual_type="document"`) exceed 15% globally or more than 2 in any 10-beat window (roadmap 2.3 / audit G-2) | MAJOR |
| 22 | `visual_monotony` — aggregate MAJOR finding when `consecutive_same_environment` findings exceed 10, `ai_slideshow_risk` findings exceed 2, or `document_saturation` fires (roadmap 2.5 / audit G-5.2); telemetry only, per D1.5 below — no longer a retry trigger | MAJOR |

Checks 12–16 validate `flux_prompt` *text quality* (subject/environment/
information clarity, exact/near duplication) — distinct from check 4, which
only catches an explicit forbidden mood word. Checks 17–18 are retired (no
beat of either path carries a `media_url` when `validate_storyboard()` runs).
`validate_storyboard()` runs exactly ONCE per path (audit §3.3): the issues
list from the validation gate is forwarded into
`repair_duplicate_flux_prompts(issues=...)`, which no longer re-runs the full
validator to find its targets. Check 19
(Phase 14.7, §11.6) is a defense-in-depth complement to Phase 14.7's
Python-side prompt sanitization — it fires only on a beat the sanitization's
keyword detector missed, not on the normal sanitized happy path. Check 21
(roadmap 2.3 / audit G-2) is a MAJOR document-saturation guard: it treats
`motif="document"` and `visual_type="document"` as document-ish, then applies
both a global >15% cap and a local >2-per-10-beat-window cap. Check 22
(roadmap 2.5 / audit G-5.2) preserves the individual repetition findings as
MINOR diagnostics, then emits one aggregate MAJOR `visual_monotony` finding
when `consecutive_same_environment` findings exceed 10, `ai_slideshow_risk`
findings exceed 2, or `document_saturation` fires. All checks live in the
same function; none is a second validator.

Rules:

- MAJOR issues never hard-block the pipeline, and never trigger any
  regeneration on either path (Elimination Mandate D1.5, see above) — the
  caller only logs and proceeds, on both the parent and child paths alike.
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
  closes that waste without spending a retry on a MINOR finding. Parent
  paths preserve the pending-only default (`media_url` empty) so prompt
  metadata cannot drift from an already-generated image. The child Short
  path passes `include_resolved=True` because it clears every inherited
  parent `media_url` and regenerates the beat as a new portrait image in
  the same run; resolved parent-reuse prompts therefore receive the same
  append-only variation before that new provider call. Runtime proof:
  `tests/test_duplicate_prompt_repair.py`.
- After each newly generated local Flux image, Agent 4 computes a deterministic
  64-bit average pixel hash and compares it with earlier accepted images from
  the same generation call. A Hamming distance of 3 or less triggers exactly
  one reroll with the same composition-slot variation rotation above, then
  accepts/logs the result with no loop and no quality judgment. This runs in
  both parent `generate_all_beat_images()` and child Short portrait
  `generate_pending_beat_images()`, and only compares local pixels already on
  disk. Runtime proof: `tests/test_pixel_duplicate_reroll.py`.
- Every `validate_storyboard()` call logs `VISUAL_REPEAT_RATE`,
  `AI_SLIDESHOW_RISK`, `FLUX_PROMPT_QUALITY`, and `FLUX_DUPLICATE_RATE`
  (always, even when zero) as summary diagnostics — see §29.
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
  unsupported-extension local file, `PIL`-unreadable image, image aspect
  mismatches (parent expects 16:9; Shorts expect 9:16, with small fal rounding
  tolerance), `near_black_image` (roadmap Phase A1 — mean grayscale
  luminance below `_NEAR_BLACK_LUMINANCE_FLOOR = 24.0`, the same floor
  `flux_generator.py`'s generation-time reroll uses, as a persistence-layer
  backstop; see `generate_all_beat_images` above), and `letterboxed_image`
  (Tier 5 R19 — paired near-black edge bands with a clearly bright inset
  center) — all `BLOCKING`-severity, **but blocking no longer always means
  FAILED (post-Tier-5 live-canary fix, operator never-block policy):** the
  orchestrator splits blocking findings by `REPAIRABLE_MEDIA_CODES`
  (`near_black_image`, `letterboxed_image`, `local_image_aspect_mismatch` —
  the last one joined after run 41f7eeb8's fal size anomaly).
  Per-image, regeneration-fixable findings trigger
  `_repair_flagged_media()` — the same two-attempt healing ladder the
  generation-time gate uses (operator preference for regeneration over
  neighbor duplication, 2026-07-16): the corrective-clause prompt, then a
  deterministic prompt rewrite (`_build_defect_rewrite_prompt()`), each via
  `generate_beat_image` with a `media_repair:`/`media_repair_rewrite:`
  cache-key extra so even an unchanged-prompt aspect repair produces a
  fresh artifact, each verified against pixel defects AND expected aspect;
  nearest already-validated neighbor's image only as terminal fallback,
  then every language's
  `VideoSection` row re-pointed by rewriting the `generation_prompt` JSON
  (the only place `media_url` lives — the shared loader merges extras over
  columns) — then one re-validation; findings that somehow survive log
  `AGENT4_MEDIA_VALIDATION_UNREPAIRED` at ERROR and the content **proceeds**.
  Only STRUCTURAL findings (everything not in `REPAIRABLE_MEDIA_CODES`)
  still set `Content.status = "FAILED"` — those mean the render itself
  would break. Public detector aliases `detect_image_letterbox()` /
  `detect_image_pixel_defect()` exist so the generation-time gates and the
  repair pass reuse the validator's own pixel checks — one implementation,
  never forked. Runtime proof: `tests/test_media_repair_never_block.py`.
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
app/agents/agent4_visuals/review_metadata.py                 (canonical field sets)
app/agents/agent4_visuals/services/visual_orchestrator.py     (_beat_extras)
app/agents/agent4_visuals/services/visual_review.py           (save_beat_review_metadata)
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

**Update (Elimination Mandate D2.1, `code_report/forensic_output_audit_borrasca_run.md`):**
the first15 diagnostics and cinematic continuity fields (`continuity_tags`,
`visual_bible_refs`, first15's fields) are no longer produced by anything —
`cinematic_prompts.py` and `first15_validator.py` (the two generators of
these fields) are deleted entirely, not just skipped. `review_metadata.py`'s
`LEGACY_REVIEW_METADATA_FIELDS` and the HTML rendering still reference these
keys, degrading gracefully to empty/dash for any content generated after
this change while still rendering correctly for pre-existing
`beat_review_metadata.json` files left by older runs — the same read-only
legacy-display pattern used for `visual_bible.py` below. This is intentional
and requires no further cleanup: `visual_review.py` is local-debug-only
tooling (§13), not a production consumer.

- `_beat_extras()` (`visual_orchestrator.py`) now only serializes the fields
  something downstream actually consumes: `visual_intent`, `visual_type`,
  `visual_category`, `environment`, `motif`, `transition_to_next`,
  `media_url`, `media_type`, `media_strategy`, `text_card_style`,
  `source_script_sha256` (the last one load-bearing for the stale-visuals
  guard, §11.4 above). `overlay_text`/`overlay_position` are not persisted at
  all — the shared loader forces `""`/`"none"` on every load
  (subtitles-only rendering), so writing them was two dead keys per row.
- `review_metadata.py` separates `LEGACY_REVIEW_METADATA_FIELDS` (the 19
  historical enrichment keys retained for old-artifact compatibility) from
  `LIVE_REVIEW_SNAPSHOT_FIELDS` (the fields the current structured storyboard
  and timestamp pipeline actually produces: visual intent/type/category,
  environment, Flux prompt, effect/grade/transition/motif, intensity/duration,
  hints, and media identity). `REVIEW_METADATA_FIELDS` combines both for the
  artifact writer. This fixes the empty-file regression: the old writer selected
  only legacy keys whose producers had been deleted, so every current row
  contained `section_order` and nothing else. No AI fields or calls were added.
- The combined live snapshot and any present legacy review fields go to a run-folder JSON file —
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
  — add live artifact fields to `LIVE_REVIEW_SNAPSHOT_FIELDS` or legacy-only
  compatibility fields to `LEGACY_REVIEW_METADATA_FIELDS` in
  `review_metadata.py`; never fork a second field list in the writer.
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

### Phase A′ runtime contracts (2026-08-05)

- Agent 5 converts beat millisecond starts into one shared 30-fps boundary
  array in `remotion_builder._tile_section_frame_ranges()`. Props carry
  `render_start_frame`/`render_end_frame`; adjacent nominal ranges must satisfy
  `left.render_end_frame == right.render_start_frame`. Remotion may extend an
  incoming Sequence backward for a crossfade, but its nominal start is fully
  opaque and its nominal end remains the shared boundary.
- Agent 4's deterministic text-prop fallback preserves the image-style clause
  first, removes meta-rule vocabulary, and selects generic physical-detail
  variants by `beat_order`. Storyboard prompts own contextual variation and
  era/locale correctness; Python remains a deterministic fallback only.
- Solo Shorts above 110% of `_MAX_SHORT_WORDS` receive one and only one
  ceiling-constrained regeneration. `SOLO_SHORT_WORD_CEILING_REGEN` records the
  trigger and the shorter draft is retained; this is not a quality loop.
- The word floor/ceiling this ceiling-regen (and the mirror floor-regen)
  gates on is no longer only the static `_MIN_SHORT_WORDS`/`_MAX_SHORT_WORDS`
  (190/270, calibrated from one historical run at ~175 wpm). A real
  production run (content f71183e3) measured a different configured voice
  at ~142 wpm, which let a 270-word-compliant Short still land at ~114s
  instead of the assumed ~92s. `scripts._resolve_short_word_bounds(db,
  language, channel_id)` now computes the floor/ceiling from
  `script_estimator.compute_measured_wpm(..., is_short_episode=True,
  channel_id=...)` — the same rolling real-`AudioFile`-average mechanism
  §7.4 already defines — once at least 3 completed Shorts exist for that
  exact (channel, language); below that sample count it returns the static
  190/270 unchanged (cold-start fallback, byte-identical to the prior
  behavior). `_run_solo_short_script_workflow()` calls this once per run and
  threads the result through both the floor and ceiling regen checks in
  place of the module constants. The per-part planner path
  (`_generate_short_script()`/`run_shorts_planner()`) still reads the static
  constants directly — not yet wired to per-channel calibration.
- A storyboard batch delivering fewer than `ceil(0.70 * requested)` beats gets
  one follow-up call for exactly `requested - delivered`. The same segment and
  continuity context and the existing in-call truncation retry are reused.
  `STORYBOARD_SHORTFALL_TOPUP` records the bounded call; an under-delivering or
  failed follow-up is accepted and logged as `STORYBOARD_SHORTFALL_ACCEPTED`.
  Never top up the top-up.
- **The shortfall-topup call can restate content the original delivery
  already covered (content 069d8d06 diagnostic, 2026-08-05).** A real
  production Solo Short (a Potosí/mita history) shipped 9 consecutive beats
  (beat_order 11-19) holding for round-number 1.0/2.0/3.0 s intervals with
  images unrelated to the narration playing at that instant — the reviewer-
  observed 47-65 s misalignment. Root cause, confirmed by re-deriving the
  content's own persisted `beat_review_metadata.json`: the segment's
  `target_beat_count` exceeded `_MAX_BEATS_PER_BATCH` and was pre-split into
  two parts (`_split_segment_for_batching`); part 1 under-delivered (11 of
  ~17-20 requested beats), triggering the shortfall topup above — which
  re-sends the *identical* `segment_text` with nothing telling the model
  which beats already exist for it. The topup call independently re-told
  part 1's narration from its own beginning; its beats' `start_hint`/
  `end_hint` values were near-verbatim restatements of beats already
  delivered (e.g. topup beat 12's hint "That mountain is Cerro Rico, high in
  the" vs. delivered beat 1's own hint "That mountain is Cerro Rico, high in"
  — 93% token overlap), so none of them could hint-match forward of the
  transcript cursor the original delivery's own beats had already advanced
  past — every one fell back to proportional interpolation, packing a real
  ~11 s gap between two correctly-matched anchors with beats showing the
  wrong images. **Explicitly not a diacritics/normalization bug** — that was
  the initial hypothesis (the story's proper nouns Potosí/asientos/Genoese)
  and was investigated first per this codebase's own "verify the hypothesis
  before fixing" convention (§9.3's John Law diagnostic precedent above); it
  was not confirmed as this incident's primary cause, though a real,
  independent diacritic-folding bug was found and fixed alongside it (next
  bullet) since `normalize_for_matching` genuinely needed it regardless.
  Three independent fixes, each a deterministic, no-AI safety net:
  - **Duplicate-topup rejection** (the direct root-cause fix) —
    `storyboard._filter_duplicate_topup_beats(topup_beats, delivered_beats,
    language)`: a topup beat whose `start_hint` + `end_hint` token set
    overlaps any single already-delivered beat's hint tokens by at least
    `_TOPUP_DUPLICATE_HINT_OVERLAP` (0.65, module constant) is dropped
    before being appended, logged `STORYBOARD_TOPUP_DUPLICATE_DROPPED` per
    beat and `STORYBOARD_TOPUP_DUPLICATE_SUMMARY` once per topup call.
    Re-deriving the real 069d8d06 hints through this exact check confirms it
    catches the clearest restatements (beats 11/12/15/16, 91-100% overlap)
    but not every one (beats 13/14/17 sat at 0.38/0.46/0.60 — genuine
    partial paraphrase, under threshold by design rather than tuned to this
    one sample) — the two mechanisms below are the remaining defense for
    whatever a hint-overlap check alone does not catch.
  - **Diacritic-folding symmetry** — `app.shared.text_normalize
    .normalize_for_matching()` (hint side, shared with Agent 3) did not fold
    accented characters, while `storyboard._normalize_word()` (transcript
    side) already did via NFD decomposition — a real, independently
    confirmed asymmetry bug (not this incident's primary cause, but a
    genuine defect regardless): a hint spelled "Potosí" could fail to match
    a transcript spelled "Potosi" or vice versa. `normalize_for_matching()`
    now strips diacritics (NFD, drop combining marks) before anything else,
    fixing every caller, every language, not just this one story.
  - **Bounded fuzzy tolerance for proper nouns** —
    `storyboard._search_subsequence_fuzzy()`, wired into `_locate_phrase()`
    as a last-resort attempt after every exact/prefix attempt fails: tokens
    of at least `_FUZZY_MIN_TOKEN_LEN` (5) characters may match with up to
    `_FUZZY_MAX_EDITS` (1) Levenshtein edit — deterministic bounded DP, no
    AI, same proximity-window bound as the exact search. Logged
    `HINT_FUZZY_MATCH`.
  - **Fallback-cluster thinning** — `storyboard._drop_surplus_fallback_beats()`,
    called between anchor-demotion and boundary resolution in
    `map_storyboard_beats_to_timestamps()`: when a run of consecutive
    unmatched beats shares a real anchor-to-anchor span too small to give
    each one at least `settings.min_fallback_beat_hold_ms` (1 600 ms
    default, settings-configurable), the run is deterministically thinned to
    as many evenly-spaced beats as the span can support (accounting for the
    preceding anchor's own weighted share of that same interpolated span),
    dropping the rest and renumbering `beat_order` contiguously — logged
    `STORYBOARD_FALLBACK_CLUSTER_THINNED`. A beat that plays for one second
    against the wrong image is worse than one fewer beat over that gap.
  - **Fallback script_text inheritance** — a beat that still falls back
    (matches `None`) no longer persists `script_text=""`; it is assigned the
    real transcript words spoken during its own resolved
    `[audio_start_ms, audio_end_ms)` span via `storyboard._words_in_ms_span()`
    (`script_text_source="proportional_fallback_span_text"` when non-empty,
    the pre-existing `"empty_fallback_no_transcript_span"` only when the span
    genuinely has no transcript coverage) — `script_text_missing` stays
    `True` either way, so a downstream repair/review pass can see what the
    beat's image actually plays against instead of nothing.
  Runtime proof: `tests/test_storyboard_topup_duplicate_rejection.py`,
  `tests/test_hint_matching_diacritics.py`,
  `tests/test_fallback_min_hold_floor.py`; `tests/test_hint_search_proximity_window.py`
  and `tests/test_boundary_span_sanity.py` updated for the script_text
  inheritance change (both already asserted a specific fallback beat's
  `script_text`/`script_text_source`, now updated to the new, non-empty
  contract); `tests/test_storyboard_script_text_empty_fallback.py`'s static
  source check updated from a literal `script_text = ""` string match to the
  new `_words_in_ms_span(...)` call it replaced.
- **Shortfall top-up is now span-scoped, not full-segment-rescoped (follow-up
  root-cause fix, 2026-08-05)** — the duplicate-topup-rejection filter above
  stopped the 069d8d06 misalignment from shipping, but the topup call still
  wastefully re-sent the *entire* segment text and relied on the 65%-overlap
  filter to discard whatever came back duplicated (which measurably missed
  partial-paraphrase beats, e.g. the 0.38/0.46/0.60-overlap beats noted
  above). `storyboard._locate_uncovered_span(delivered_beats, sub_text,
  language)` locates each delivered beat's `end_hint` (falling back to
  `start_hint`) as a forward-only word-token subsequence within the
  segment's own text — mirroring the real Whisper hint matcher's monotonic-
  cursor contract, but at the text level, before any audio timestamp
  exists — and returns the single largest uncovered gap between what was
  successfully located (normally the trailing "everything after the last
  beat" span, but this also naturally captures a large interior gap if one
  or more beats' hints couldn't be located at all). The topup call then
  receives `segment_text=<uncovered span only>`, a `target_beat_count`
  derived from the span's own word count at the segment's original
  words-per-beat ratio (not the raw `sub_target_beat_count - delivered`
  shortfall), and a new one-line `continuation_framing` parameter on
  `generate_storyboard_batch()` ("Earlier beats already cover the preceding
  narration of this segment. Generate beats for this remaining passage
  only...") — deliberately a separate line from `previous_segment_summary`
  (which is about the PRIOR segment, a different concept). When the
  uncovered span is under `_TOPUP_MIN_UNCOVERED_WORDS` (8) words, no topup
  call is made at all (logged `STORYBOARD_SHORTFALL_TOPUP_SKIPPED
  reason=uncovered_span_too_thin`) — there is nothing real left to ask for.
  The 65%-overlap duplicate filter (`_filter_duplicate_topup_beats`) stays
  wired in unchanged as the safety net *behind* span-scoping, not replaced by
  it. `STORYBOARD_SHORTFALL_TOPUP` now also logs `span_start_word`/
  `span_end_word`/`span_words`. Runtime proof:
  `tests/test_storyboard_shortfall_topup.py` (span-scoped call receives only
  the uncovered words, never the already-covered ones; target derived from
  span length; the too-thin-span skip path; duplicate filter still exercised
  downstream) and `tests/test_storyboard_topup_duplicate_rejection.py`
  (updated — its own fixture's disconnected `voice_script` has no real hint
  text to locate, so span-scoping correctly falls back to the full segment
  for that file's fixtures; the duplicate-filter behavior it tests is
  unaffected).
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

**Image luminance gate (roadmap Phase A1, operator video-output audit):** a
real production run shipped a beat (`ba20460bad5b66074367dcc3.jpg`) whose
Flux generation was a 100% black JPEG (`signalstats YAVG=0.0`) — it passed
every existing gate, since media validation only checked existence/
readability/aspect, never pixel content, and the unrelated §11A.6 dark-frame
guard only fires on `dark_contrast`-graded prompts (this beat was
`warm_amber`). ~9 more images in the same run measured under the floor,
producing multi-second blackdetect spans in the rendered video in both
languages.

- `flux_generator._mean_luminance(media_url, media_path)` — opens the local
  image with PIL and returns `ImageStat.Stat(image.convert("L")).mean[0]`
  (mean grayscale value, 0-255); returns `None` (logged
  `LUMINANCE_CHECK_UNAVAILABLE`) if the file can't be read, exactly the
  same fail-open contract `_average_pixel_hash()` already uses for the
  pixel-duplicate check below it in the same file.
- **Unified image health gate (`flux_generator._ensure_beat_image_healthy()`
  — supersedes the former separate dark/letterbox/size gates, explicit
  operator preference 2026-07-16: regenerate with a changed prompt rather
  than duplicate a neighbor's image):** every successfully generated image
  is checked against ALL repairable pixel-defect classes at once —
  near-black (`_LUMINANCE_MEAN_FLOOR = 24.0`), baked-in letterbox, and
  off-aspect dimensions (the fal size anomaly: ONE 1072x1536 image returned
  for a 1080x1920 portrait request while the identical payload produced 20+
  correct portraits in the same run) — via `_detect_image_defect()`, which
  reuses `media_validation`'s own public detectors
  (`detect_image_pixel_defect()`, `detect_image_wrong_aspect()`; single
  implementation, never forked). A defective image gets a bounded
  TWO-attempt healing ladder: (1) corrective-clause reroll — the beat's own
  prompt plus `_WELL_LIT_REROLL_CLAUSE` (near-black) or
  `_FULL_BLEED_REROLL_CLAUSE` (letterbox); a size defect keeps the prompt
  unchanged — always with a fresh cache key (`heal1:` — the identical
  prompt+size would hash straight back to the cached bad artifact); (2) a
  deterministic prompt REWRITE (`_build_defect_rewrite_prompt()` —
  `visual_intent` base, or the environment-safe prompt when the intent is
  too thin, plus a composition-slot variation from the shared 24-slot
  rotation, plus BOTH corrective clauses; pure string construction, no AI
  call, `heal2:` cache key). Every regenerated image is re-checked against
  ALL defect classes, not just the triggering one. Only when both attempts
  fail does the beat hand off to `fill_failed_beats_from_neighbors()` — the
  rare terminal fallback (something must fill the beat or the render defers
  on missing media). Wired into both `generate_all_beat_images()` (parent)
  and `storyboard.generate_pending_beat_images()` (child Short, portrait).
  Bounded, deterministic-triggered — an operator-approved never-block-policy
  mechanism, not a quality-judging loop. Logs: `BEAT_IMAGE_DEFECT`,
  `BEAT_IMAGE_HEALED`, `BEAT_IMAGE_HEAL_ATTEMPT_FAILED`/
  `_ATTEMPT_STILL_DEFECTIVE`, `BEAT_IMAGE_HEAL_EXHAUSTED`. Runtime proof:
  `tests/test_media_repair_never_block.py`,
  `tests/test_transport_retry_and_language_second_chance.py`,
  `tests/test_luminance_gate.py`.
- Backstopped at persistence time: `media_validation._validate_image_luminance()`
  re-measures every referenced local image and adds a **blocking**
  `near_black_image` finding to `validate_visual_media_assets()` (§11.4
  below) if a near-black image somehow still reaches the DB (e.g. a beat
  whose reroll path was bypassed) — `Content.status` is failed rather than
  shipping the frame.
- Runtime proof: `tests/test_luminance_gate.py` — real PIL image I/O (only
  the paid `_call_fal()` fal.ai boundary stubbed), covering the reroll
  succeeding, the reroll still failing and handing off to neighbor-fill, a
  bright image never triggering a reroll, the child Short path, and both
  `_mean_luminance()`/`_validate_image_luminance()` directly.
- `generate_all_beat_images(beats, content_id, width=1920, height=1080)`
  gained optional `width`/`height` parameters (output_mode `shorts_only`
  roadmap, see `_run_solo_short_visuals` below) — threaded into all four
  internal calls (`generate_beat_image_with_routing`, the hard-retry
  `generate_beat_image`, `_dedupe_generated_image_once`,
  `_ensure_beat_image_healthy`), all of which already accepted width/height
  end-to-end. Defaults match the pre-existing landscape call exactly, so
  every existing caller (the parent path) is unaffected.

### Merge-status sweep follow-up (2026-08-05, code_report/TODO Task 2)

Five smaller, independent fixes from the same session as the beat-sync
work above. Channel-agnostic, deterministic, no new AI-judged loops:

- **Text-prop sanitizer now scoped to the PRIMARY subject** —
  `flux_generator.is_text_prop_beat()` still detects a text-bearing keyword
  anywhere (the overall on/off gate), but `derive_text_prop_prompt()` now
  only applies the full material-texture reframe when
  `_is_primary_subject_text_prop(beat)` is true: a structured
  `visual_type`/`motif == "document"` classification, or the keyword falling
  within the first `_PRIMARY_SUBJECT_WINDOW_WORDS` (8) words of the prompt
  body right after the style-marker match (`_STYLE_MARKER_RE.search()`, not
  the coarser comma-chunk-granular `_style_anchor_chunks()` — a real prompt's
  style words and subject clause typically share one comma-chunk with no
  separator, e.g. "cinematic cartoon illustration of a rough hand-carved
  mine tunnel entrance", so chunk-level stripping would discard the subject
  along with the style words). A keyword merely present elsewhere in the
  scene ("...a folded newspaper tucked under a passerby's arm...") is left
  alone. Every non-full-reframe path (peripheral mention, or primary subject
  but no `_TEXT_PROP_MATERIAL_REFRAMES` table entry) goes through the new
  `_derive_deemphasis_prompt()` — keeps the ORIGINAL prompt, appends one
  `"{keyword} out of focus and unreadable in the background"` clause — never
  the old generic `"...interacting with the object in close-up"` fallback
  line, which the task's own audit flagged as a literal `"the object"`
  placeholder. The full-reframe path also now preserves the beat's
  `environment` field (`_ENVIRONMENT_PHRASES` lookup) and any era/period
  phrase already in the original prompt (`_ERA_DESCRIPTOR_RE`, best-effort)
  instead of discarding them — and, since `_style_anchor_chunks()`'s
  chunk-level anchor can itself still contain the leaking subject noun for
  the same fused-clause reason, the reframe's own style-anchor extraction
  falls back to the `_STYLE_MARKER_RE`-based slice whenever the chunk-level
  anchor still contains the matched keyword. Runtime proof:
  `tests/test_text_prop_sanitizer_scope.py`.
- **Regime-insignia / hate-symbol rule added to the storyboard prompt**
  (`PROMPT_VERSION` 5.4 → 5.5) — a channel-agnostic rule next to the
  existing era/anachronism paragraph: depict any period, regime, or
  conflict context, of any era or side, through architecture, dress,
  objects, and setting only — never regime insignia, flags, uniform
  emblems, or extremist/hate symbols. Prompt-only, no schema change.
  Runtime proof: `tests/test_storyboard_regime_insignia_rule.py`.
- **Adjacent-beat perceptual-hash reroll — already present, broader in
  scope than asked** — `flux_generator._dedupe_generated_image_once()` /
  `_find_pixel_collision()` (§11.4's `validate_storyboard` checks table
  area) already compare every newly generated image's 64-bit average pixel
  hash (`_PIXEL_HASH_SIZE=8`) against a running `pixel_ledger` of every
  prior accepted image in the same generation call — a Hamming distance
  `<= _PIXEL_HASH_COLLISION_MAX_DISTANCE` (3) triggers exactly one
  deterministic composition-variation reroll. This is a strict superset of
  "compare each beat to its immediate neighbor only" (the ledger includes
  the immediate neighbor as its most recent entry); implementing a narrower
  adjacent-only check alongside would be redundant. No change made.
- **B2 ceiling-regen instruction now states an explicit target below the
  cap, with consequence framing** — `script_workflow.py`'s
  `_CEILING_REGEN_TARGET_MARGIN` (15 words) is subtracted from `max_words`
  to produce the regen call's real target (255 words at the static
  270-word default, scaling proportionally under per-channel calibration,
  §7.4/roadmap-3.8-style) — the prior wording asked for "no more than
  {max_words}", which left a retry with zero margin against ordinary
  pacing variance. The injected note now also states why: landing right at
  the ceiling risks tipping back over it and pushing the Short past its
  target watch time. Runtime proof:
  `tests/test_b2_ceiling_regen_target_wording.py`.
- **`forbidden_flux_word` "cinematic" false-positive fixed** — a real
  production run flagged this check MAJOR on 38/38 beats for a
  `image_style="cinematic_cartoon"` channel, because
  `system_prompt.py`'s own style vocabulary instructs Claude to open nearly
  every beat with "cinematic cartoon illustration of ..." — the word
  "cinematic" is in `FORBIDDEN_FLUX_WORDS` (intended to catch genuinely
  mood-only prompts with no physical subject), but here it was the
  operator-approved style NAME, not evidence of a subjectless prompt.
  `storyboard_validator.py`'s check 4 now drops "cinematic" from a beat's
  `found_forbidden` set specifically when `"cinematic cartoon"` or
  `"cinematic_cartoon"` appears in the prompt (either spelling) — a
  genuinely mood-only "a cinematic, atmospheric shot..." prompt unconnected
  to that style name is still flagged. Runtime proof:
  `tests/test_forbidden_flux_word_cinematic_exemption.py`.

#### `_run_solo_short_visuals` — original storyboard generation for a Solo Short (output_mode `shorts_only` roadmap)

File:

```text
app/agents/agent4_visuals/services/visual_orchestrator.py
```

A Solo Short (§4.4/§5.3/§9.6 — `is_short_episode=True`,
`parent_content_id=NULL`) needs an **original** storyboard generated
directly from its own script, exactly like a parent does — **not**
`remap_beats_for_short()`'s remap-from-parent path (below), which has no
parent beats to remap from and would hard-fail it. `run_visual_generation()`'s
dispatch (§11.1) is a 3-way branch, not the 2-way branch it used to be:

```python
is_short_episode = bool(getattr(content, "is_short_episode", False))
parent_content_id = getattr(content, "parent_content_id", None)

if is_short_episode and parent_content_id:
    return _run_child_short_visuals(...)          # remap path, unchanged
if is_short_episode and not parent_content_id:
    return _run_solo_short_visuals(...)            # NEW
return _run_parent_visuals(...)                    # unchanged
```
Before this, `is_short_episode=True` alone routed unconditionally into
`_run_child_short_visuals()`, which immediately returns `VISUALS_FAILED`
the instant it finds no `parent_content_id` — a Solo Short reaching this
dispatch was a reachable-but-guaranteed-failure case, not merely
unreachable.

`_run_solo_short_visuals(content, channel, scripts_by_lang, audio_by_lang,
db, visual_style="", image_style="")` reuses `_run_visual_pass()` — the
same generation sequence `_run_parent_visuals()` uses (`split_into_beats()`,
storyboard validation, duplicate-prompt repair, save-before-Flux, Flux
generation, save-after-Flux) — which gained `is_short_episode`/`width`/
`height` parameters (defaults preserve the parent path exactly) so both
callers share one implementation instead of forking it:

- `script_format=SOLO_SHORT_SCRIPT_FORMAT` (§7.3), `is_short_episode=True`
  — threaded into `split_into_beats()`'s wpm-calibration scoping (the same
  Finding-A fix `_persist_source_script()`/`_set_multilingual_durations()`
  got — see §9.6).
- `width=1080, height=1920` — portrait, threaded into
  `generate_all_beat_images()`.
- Recurring-character image reuse is retired. Every person beat makes its own
  normal image-generation call. Deterministic Python matches recurring names in
  person beats, strips Claude-authored appearance clauses, and injects the
  blueprint's canonical descriptor verbatim after the channel style prefix.
  The stored canonical line is preserved through text-prop sanitization, defect
  rewrite, child remapping, persistence/reload, and duplicate-prompt repair.
- `allow_legacy_fallback=False` — hardcoded; this dead toggle is never set
  by the UI for any content shape (§11.4's `split_into_beats` entry).
- Milestone logs are `SOLO_SHORT_VISUALS_START`/`SOLO_SHORT_VISUALS_DONE`
  when `is_short_episode=True` (distinct from `PARENT_VISUALS_START`/
  `_DONE` — `_run_visual_pass()` itself selects the log name).

Idempotency: reuses already-persisted `__visual__` beats when every one
already has a local image (a basic completeness check, mirroring the
`existing_complete` check `_run_child_short_visuals()` already uses).
**Deliberately does not yet carry `_run_parent_visuals()`'s full
script-hash/audio-duration staleness-fingerprint guard** (the stale-visuals
guard documented under `_run_parent_visuals` above) — a documented scope
boundary, not an oversight: a Solo Short whose script is regenerated after
this ran once would need its stale beats cleared manually until a future
phase extends that same guard to this path too.

Terminal status: `content.status = "CHILD_SHORT_VISUALS_DONE"` — the
existing string, not a new one. Agent 5's render-format decision
(`render_fmt = "short" if is_short_episode else "main"`) and
`pickup_visual_ready`'s status filter are already `is_short_episode`-driven,
not keyed to which Agent 4 code path produced the visuals — reusing this
string means **zero changes were needed anywhere in
`app/scheduler/tasks.py`**.

Rules:

- Do not route a Solo Short into `_run_child_short_visuals()` — that
  function's parent-readiness gate (`if not parent_content_id: ...
  VISUALS_FAILED`) and its `CHILD_SHORT_VISUALS_DEFERRED`-to-`AUDIO_DONE`
  revert both assume a parent exists; neither applies to a Solo Short.
- Do not give `_run_solo_short_visuals()` its own copy of the storyboard
  generation sequence — extend `_run_visual_pass()` (shared with the parent
  path) instead, the same way this phase did.

Runtime proof: `tests/test_solo_short_pipeline.py` — proves the dispatch
routes a parentless short to `_run_solo_short_visuals()` (not the old
remap path), that it calls the real `_run_visual_pass()` with
`is_short_episode=True, width=1080, height=1920,
script_format=SOLO_SHORT_SCRIPT_FORMAT`, that the idempotency reuse path
never regenerates when beats are already complete, and — independently —
that `_run_visual_pass()` itself correctly threads width/height into
`generate_all_beat_images()` for both the Solo Short case and the
unmodified parent-default case.

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
- Use parent visual beats only as a PROMPT source pool (fresh full-system
  audit §3.1): `match_score >= 70` inherits the matched parent beat's
  `flux_prompt` for visual continuity; below 70 uses the remap model's
  `new_image_prompt`. Every beat leaves the remap with `media_url=""` —
  the child path has no physical image reuse (portrait regeneration made it
  impossible), so the former 60% reuse budget, demotion pass, and
  `CHILD_SHORT_REUSE_BUDGET_APPLIED` log are deleted. Stats log:
  `CHILD_SHORT_PROMPT_INHERITANCE_STATS`.
- Threshold is enforced in Python, not prompt.
- `visual_style`/`image_style` are threaded into the remap call (orchestrator →
  `remap_beats_for_short()`) as "Global visual direction/Global image style"
  lines so newly written child prompts match the channel look.
  `channel_niche`/`channel_tone` are threaded the same way (prompt
  engineering audit §2.3) — every other creative prompt in the pipeline
  (blueprint, section generation, short episode script, parent storyboard)
  already received niche/tone; the remap call, which writes brand-new Flux
  prompts for any `match_score < 70` phrase, did not.
- Child visual idempotency (audit §3.6, extended by a stale-visuals guard —
  prompt engineering audit follow-up): before remapping a language,
  `_run_child_short_visuals()` checks whether complete persisted
  `VideoSection` rows exist (every beat has a local `cache/` image) *and*
  whether their stamped `source_script_sha256`/`source_audio_duration_ms`
  fingerprints (same `_beat_extras()` persistence convention the parent's
  own stale-visuals guard uses, §11.4 `_run_parent_visuals`) still match the
  current child script/audio. Fresh + complete → reuse without re-running
  remap or regenerating any portrait, logged `CHILD_SHORT_VISUALS_REUSED`.
  Stale (script or audio changed since these beats were persisted) →
  discard and regenerate, logged `CHILD_SHORT_VISUALS_STALE`. Rows that
  predate this guard (no stored fingerprint) are stamped and re-saved
  unchanged on the next run, logged
  `CHILD_SHORT_VISUALS_FINGERPRINT_BACKFILLED` — no regeneration forced for
  pre-existing content, matching the parent guard's own backfill contract.
- The `short_storyboard_remap` schema includes `new_image_prompt` (roadmap 3.4 / audit V-3). For assignments with `match_score < 70`, it must be a complete portrait-safe Flux prompt for a new child Short image. `remap_beats_for_short()` uses that prompt for below-threshold beats and fails the remap with `CHILD_REMAP_NEW_PROMPT_MISSING` if it is empty, rather than inheriting a rejected parent prompt or falling back to raw narration. At-or-above-threshold beats inherit the matched parent beat's prompt; a high-score assignment whose parent beat was excluded from the candidate pool (legacy text-card / missing media) uses `new_image_prompt` when supplied, otherwise a deterministic physical fallback prompt.
- Child remap MAJOR handling is telemetry only (Elimination Mandate D1.5, `code_report/forensic_output_audit_borrasca_run.md` — supersedes the former roadmap 3.5 / audit G-4.3 deterministic remediation primitive): after `validate_storyboard()` finds child MAJORs, `_run_child_short_visuals()` logs them and proceeds with the beats unchanged. The deleted primitive (`remediate_child_major_storyboard_issues()`) used to strip forbidden/readable-text language and regenerate `flux_prompt` from `visual_intent`/environment/motif for `forbidden_flux_word`/`ai_text_rendering_requested` MAJORs — removed because MAJOR findings never blocked the pipeline either way, so the repair pass added complexity without a provable quality gain. No Claude, Flux, or internal remap retry is invoked (unchanged).
- Log reuse stats.
- Child Short visual sections use `settings.short_visual_max_hold_ms` (default
  6000 ms — see the "Visual hold cap" rules above for the reverted 5000 ms
  attempt and why) as a maximum non-terminal image hold target after
  timestamp mapping and micro-beat cleanup.
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

- Must run **after** the storyboard validation gate, never before — the
  Phase 4E-E validate-then-generate ordering. Every beat arrives pending
  (`media_url=""` — audit §3.1); the retired reuse checks 17/18 no longer
  exist to observe a split.
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

- `ImageRoute` — the routing decision returned by `select_route()`
  (model/source/reason only, no payload). The former `ImageRequest`/
  `ImageResult` dataclasses were removed by the post-roadmap deep audit —
  they had zero users anywhere (speculative abstraction from Phase 14.6
  that no caller ever adopted).
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
  `num_inference_steps`/`guidance_scale`). It must also normalize requested
  dimensions into fal-legal values per tier: Flux Pro v1.1 is scaled into its
  1440px max-side envelope while preserving aspect, and aspect-ratio models
  derive portrait/landscape orientation from the requested frame.
- `build_cache_key_material(model_key, prompt, ...)` — model-aware cache
  hash input, backward-compatible with every pre-Phase-14.6 cache entry for
  the Schnell tier (see Cache keys below).

Operator-enabled Dev-routing contract:

- The former `purpose="text_card_background"` short-circuit in
  `select_route()` (and the `purpose` parameter itself) is deleted — text
  cards are removed product-wide (§11.6) and no live caller ever passed a
  non-default purpose (post-roadmap deep audit).
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
  endpoints use the Phase-14.5-verified documented names. Dev is enabled by
  default under the Tier 5 R16 operator decision; Pro remains disabled.

Config flags (`app/config.py`; Tier 5 R16 defaults):

```text
image_routing_enabled              default True
image_routing_allow_dev            default True
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
  every generated path is downloaded to local disk before persistence,
  exactly as before this phase.
- Do not add a second fal.ai integration point — all endpoint selection and
  payload construction stays inside `image_router.py`'s capability table
  and `flux_generator.py`'s single `_call_fal()` call site.
- Do not change `MODEL_CAPABILITIES["schnell"]["endpoint"]` without an
  operator-run live canary outside Claude Code/Codex first (CLAUDE.md §19.1).
- Dev routing is operator-enabled by default. Do not enable Pro by default or raise
  `image_routing_max_pro_per_content` above `0` without an explicit
  operator decision.
- The current deployment intentionally overrides both Dev routing switches to
  false. Keep Flux Dev off until the operator changes that decision; code
  defaults do not override environment configuration.
- Reference-image identity locking remains design-only. The repository has no
  verified reference-capable provider endpoint/payload contract yet. Do not
  invent one: endpoint selection, retention/cost policy, and a live operator
  canary are prerequisites before implementation.

### 11.6 AI Text Rendering Ban + Subtitles-Only Rendering (supersedes Phases 14.4/14.7/14.10b)

**Product rule (Golden Rule 11): Remotion renders subtitles ONLY.** The
subtitle track is the video's only text layer. Text cards
(`media_strategy="remotion_text_card"`, `TextCard.tsx`), per-beat overlay
text (`overlay_text`/`overlay_position`, `TextOverlay`), and the
subtitle-suppression machinery that coordinated them (Phase 14.10b's
`suppressWindows`/`computeOverlaySuppressWindows`) are all removed. The
Shorts "Part N of M" corner label is the single operator-approved exception
(`settings.short_part_label_enabled`, default on) — suppressed when
`short_total_parts <= 1` (Finding E, §11A.4's `build_short_props` entry): a
single-part Short (every Solo Short, §4.4/§9.6, always has exactly one
part) must never render a misleading "Part 1 of 1". `Short.tsx` mounts the
label outside any time-limited sequence, so when it IS shown it remains
visible for the full Short.

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
  legible text, using a named, generalizable technique taxonomy** (v5.2,
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
  `storyboard._build_beat_section()` (shared by parent and child paths). Phase
  A3.1 replaces ineffective clause stacking with a small deterministic mapping
  from generic text-object classes (document/board/sign/screen/newspaper/map/
  poster/calendar/book) to material details such as paper edges, seals, chalk
  dust, screen glow, or hands. The original text-bearing subject is omitted so
  Schnell is not asked to draw it again, and exactly one consolidated no-text/
  no-artifact clause is appended. `_build_defect_rewrite_prompt()` uses the same
  reframe, so the existing heal ladder does not reintroduce the bad subject.
  It still fires for a child-remap beat (no storyboard-prompt pass of its own)
  and is logged as `TEXT_PROP_PROMPT_SANITIZED`.
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
- **Duration drift is checked against the source audio file, not the DB
  (roadmap 2c / audit P2-5, `code_report/forensic_output_audit_borrasca_run.md`),
  and is enabled for Shorts too (roadmap 2c / audit P2-6).**
  `verify_render(mp4_path, audio_file_path, fmt)` measures expected duration
  by running ffprobe directly on `audio_file_path` — it never reads
  `AudioFile.duration_ms`. A real production run showed why this matters:
  `expected_duration_ms=audio.duration_ms` meant a corrupted DB row made a
  161.7s corrupted render "verify" against itself — circular validation that
  a real ffprobe measurement of the actual audio file would have caught.
  Both Short render call sites (`_run_short_render()`) now pass
  `audio_file_path=audio.file_path` instead of the previous
  `expected_duration_ms=None` ("no bookend padding") — Shorts get the same
  duration-drift check parent renders always got, closing the gap on the
  format with the least margin for error.

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

- Tier 5 R19 detects a baked-in horizontal or vertical letterbox when both
  opposing edge bands are near-black while the inset center is bright
  (finding code `letterboxed_image`); uniformly dark frames remain the
  responsibility of the existing near-black luminance gate. Post-Tier-5
  live-canary fix (operator never-block policy): this finding — like
  `near_black_image` — is repaired via `_repair_flagged_media()` and never
  sets `Content.status = "FAILED"`; see §11.4
  `validate_visual_media_assets` and `generate_all_beat_images`'s
  letterbox reroll gate.

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

(The old `run_agent5_for_content` Celery alias was removed by the
post-roadmap deep audit — see §10.3's removed-shims note.)

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
- A `NEEDS_REVIEW` set by per-language render verification survives the final
  status write (fresh full-system audit §3.7) — `run_video_generation()`
  never overwrites it with `RENDERED`/`FAILED` (logged
  `RENDER_NEEDS_REVIEW`).

#### `_props_are_stale`

File:

```text
app/agents/agent5_render/services/video.py
```

**Second reuse trap fixed by the same phase (roadmap 2d / audit P0-3,
`code_report/forensic_output_audit_borrasca_run.md`)** — independent of the
stale-visuals guard above, since it catches staleness at a different layer.
`_process_language()`'s "props on disk → skip to render" phase check
previously trusted an existing props JSON file unconditionally: a real
production run logged "Props found on disk — skipping to render" and
rendered a prior run's stale props file verbatim, even after the underlying
`VideoSection`/`AudioFile` data had since changed (the audited repair
scenario: a corrected `duration_ms`).

Neither `VideoSection` nor `AudioFile` carries a row-level modification
timestamp, so `_props_are_stale(existing_props_file, beats, duration_ms)`
compares three cheap, deterministic proxies for "the underlying data changed
since these props were written" — the props file's own persisted
`duration_ms`, its section count, and its last section's `audio_end_ms` —
against the same three values freshly computed from the current DB-backed
`beats`/`duration_ms`. Any mismatch, or a missing/unparseable props file, is
treated as stale (logged `PROPS_STALE_REBUILDING` /
`PROPS_STALENESS_CHECK_FAILED`). `_process_language()` calls this before
taking the existing-props shortcut: a stale result skips
`_render_from_existing_props()` entirely and falls through to the normal
rebuild path (technical blockers → props sanity check → fresh
`build_main_props()`/`build_short_props()` → render) instead of reusing the
stale file.

Runtime proof: `tests/test_stale_visuals_audio_fingerprint_and_props_rebuild.py`
— unit tests for every mismatch dimension plus the missing/corrupt-file
cases, and a wiring-level test proving `_process_language()` actually skips
`_render_from_existing_props()` when `_props_are_stale()` returns `True` and
takes the shortcut when it returns `False`.

#### `_render_from_existing_props` — resume path must make the same chunking decision as a fresh build (live-canary fix)

File:

```text
app/agents/agent5_render/services/video.py
```

**Root cause (real production incident, surfaced while investigating the R6
timeout revert above):** content `2704ad21-853c-47ff-b496-c92d147b9339`'s
first render attempt went through the fresh-build path
(`_process_language()` → `build_main_props()` → `_run_renders()`), which
correctly chunked its ~12.7-minute EN parent video
(`use_chunked = settings.chunked_render_enabled and audio.duration_ms >
settings.chunk_duration_sec * 1000`, §11A.4's `_run_renders`/
`render_main_video_chunked`). The process was interrupted before every
language finished. On resume, `en_main.json` already existed on disk and
`_props_are_stale()` (above) correctly judged it not stale — nothing about
the beat/audio data had changed — so `_process_language()` took the
"props found on disk → skip to render" shortcut into
`_render_from_existing_props()`. That function's main-video branch used to
call `render_main_video()` **directly and unconditionally**, with no
`duration_ms`-vs-`chunk_duration_sec` check of any kind — the same
12.7-minute video that had correctly chunked on its first attempt rendered
as one giant unchunked Chromium composition on resume, exactly the memory/
time pressure chunking exists to relieve. Two independent code paths
(`_run_renders()` for a fresh build; `_render_from_existing_props()`'s own
inline render+verify+persist block for a resume) reached the renderer with
logic that had silently drifted apart.

**Fix:** `_render_from_existing_props()`'s main-video branch now delegates
directly to `_run_renders()` — the exact same function the fresh-build path
already calls — instead of duplicating render+`verify_render()`+
`VideoRender` persistence inline. `_run_renders()` already computes its own
`bundle_dir` via `ensure_bundle()`, so `_render_from_existing_props()` no
longer needs its own separate call to it either. The chunking decision,
post-render verification, and `VideoRender` persistence can no longer drift
between the two entry points, because there is only one implementation now.
The pre-existing `_render_exists()` already-rendered short-circuit
(§11A.4's `render_short_render`/`_render_exists`) is unchanged — it still
guards the call to `_run_renders()` exactly as it guarded the old inline
block.

Runtime proof: `tests/test_existing_props_chunking_consistency.py` (5
tests) — a real `_render_from_existing_props()` → `_run_renders()` call
chain (only `render_main_video`/`render_main_video_chunked`, the paid
Remotion boundary, are mocked) proving: the exact audited 759,864ms
duration now dispatches to `render_main_video_chunked()` on the
existing-props path; a duration under `chunk_duration_sec` still uses the
single-pass `render_main_video()`; `settings.chunked_render_enabled=False`
is still honored on this path; the `VideoRender` row is still persisted
with the correct `content_id`/`language`/`format`; and the pre-existing
already-rendered short-circuit still skips both renderers entirely.

#### `build_main_props`

File:

```text
app/agents/agent5_render/services/remotion_builder.py
```

Rules:

- Parent main props only.
- Validate media paths.
- Fail fast if remote URLs are present.
- Fail fast (`ValueError`) if the last section end or last caption end
  drifts from `duration_ms` by more than 2% — see
  `_assert_timeline_alignment` below (roadmap 2b / audit P0-2).

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
- Fail fast (`ValueError`) if the last section end or last filtered caption
  end drifts from `duration_ms` (= `end_ms − start_ms`) by more than 2% —
  see `_assert_timeline_alignment` below (roadmap 2b / audit P0-2).

**Part-label suppression for single-part Shorts (Finding E, output_mode
`shorts_only` roadmap):** `build_short_props()` itself just passes
`short.get("part_label", "")` straight through to the props file — the
actual decision of *what* label string to pass lives one layer up, in
`video.py`'s `_process_language()`, at the `short_dict` construction:
```python
"part_label": (
    _build_part_label(language, part_number, short_total_parts or 1)
    if settings.short_part_label_enabled and (short_total_parts or 1) > 1
    else ""
),
```
Before this fix, the condition was only `settings.short_part_label_enabled`
— a Solo Short (`short_total_parts=1`, always) would render a literal
"Part 1 of 1" label under the default (enabled) setting, since nothing
checked whether there was more than one part to number. The
`short_total_parts` value itself comes straight from the `Content` row
(`run_video_generation()`, not a caller override) — see §4.4/§5.3/§9.6 for
why a Solo Short always has `short_total_parts=1`. No `Short.tsx` change
was needed: it already renders the `PartLabel` overlay purely on a truthy
string check, so passing `""` correctly suppresses it.

Runtime proof: `tests/test_subtitles_only_rendering.py`
(`TestPartLabelSuppressedForSingleParts` — real `_process_language()` call,
proving suppression at `total_parts=1`, the label still showing at
`total_parts=3`, and that the enabled-flag check is additive, not
replaced) and `tests/test_solo_short_pipeline.py`
(`TestSoloShortRenderEndToEnd` — the same proof end-to-end through the real
`run_video_generation()` entrypoint, not just `_process_language()` in
isolation).

#### `_assert_timeline_alignment`

File:

```text
app/agents/agent5_render/services/remotion_builder.py
```

**Mirrors Agent 3's cross-timeline invariant at the props-building layer**
(roadmap 2b / audit P0-2, `code_report/forensic_output_audit_borrasca_run.md`
— see `_assert_duration_transcript_alignment` in §10.3). Called at the top
of both `build_main_props()` and `build_short_props()`, before
`_ensure_props_dir()`/any file write, so a violation never produces a
partial or corrupted props file on disk.

Compares `duration_ms` against `max(audio_end_ms)` across `sections` and
`max(end_ms)` across `captions` (2% tolerance each, independently). Raises
`ValueError` — not a logged-and-continue warning — since this is the last
checkpoint before a corrupted timeline reaches Remotion; the caller
(`_process_language()` in `agent5_render/services/video.py`) already wraps
per-language props building in a `try/except Exception` that rolls back and
skips just that language, matching the same fail-that-language convention
Agent 3's mirror uses. A `duration_ms <= 0` or an empty `sections`/`captions`
list is a no-op pass (nothing to compare against) — this check adds a
disaster guard, it does not invent a new structural requirement.

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

#### `render_main_video_chunked` — chunk boundaries snap to real beat edges (live-canary fix)

File:

```text
app/agents/agent5_render/services/renderer.py
```

**Root cause (`code_report/render_pipeline_media_assembly_audit_2704ad21.md`):** a long parent
video is rendered as several independent ~`settings.chunk_duration_sec`-second Remotion
compositions, concatenated with an ffmpeg stream-copy concat (`_concatenate_chunks()`, no
re-encode). Chunk boundaries used to be the fixed timestamp `chunk_idx * chunk_duration_sec *
1000`, and a beat (`VideoSection`) was assigned to a chunk by an **overlap** test
(`s_start < chunk_end and s_end > chunk_start`) — a beat whose span straddled that exact
millisecond matched both the chunk ending there and the chunk starting there, so it was
independently clipped and rendered into **both**: full playback at the tail of one chunk, then
an immediate replay from its own animation progress = 0 for a short fragment (real production
data measured fragments as small as 240 ms) at the head of the next. A real production render
hit this at **8 of 8** internal chunk boundaries in a 9-chunk video. Separately, because each
chunk is its own independent composition, `MainVideo.tsx`'s `idx === 0` case for a chunk's own
first section always read as "no incoming transition" (a hard cut), discarding whatever
`crossfade`/`whip_pan`/`match_cut` was actually authored into the storyboard at that spot — the
real preceding beat lives in the *previous* chunk's composition and is invisible to this one.
Together these produced exactly the reported symptoms: missing/inconsistent transitions and
short flash-like image fragments at every chunk seam.

- `_compute_chunk_boundaries(all_sections, duration_ms, chunk_sec)` — a pure function that
  snaps every **internal** chunk boundary to the nearest real section edge (some beat's own
  `audio_end_ms`) instead of a fixed multiple of `chunk_sec`. Sections are contiguous by
  construction (`map_storyboard_beats_to_timestamps()` never leaves a gap), so any beat's
  `audio_end_ms` is always a safe hard split point. A nominal boundary with no unused
  section-edge candidate nearby is simply dropped (fewer/longer chunks, never a beat-splitting
  cut) — a few seconds of drift around the configured `chunk_duration_sec` is harmless to the
  crash-avoidance goal chunking exists for.
- Section membership changed from the old overlap test to an exclusive, start-based test
  (`chunk_start_ms <= s_start < chunk_end_ms`) — unambiguous once boundaries are guaranteed real
  section edges, so a beat can never again land in two chunks.
- Each chunk's props now carry `leading_transition`: the real `transition_to_next` of the
  section that truly precedes this chunk (looked up from the full, un-chunked section list, not
  discarded). `MainVideoProps` (`remotion/src/types.ts`) and `MainVideo.tsx` read this for the
  chunk's own first section (`idx === 0`) instead of hardcoding `undefined`; a non-chunked
  render and the very first chunk are unaffected (nothing legitimately precedes them).
- Runtime proof: `tests/test_chunked_render_boundary_fix.py` — `_compute_chunk_boundaries()`
  unit coverage (no section ever straddles a returned boundary; the no-candidates fallback), and
  a full-chain integration test through the real `render_main_video_chunked()` (only the
  ffmpeg/Remotion subprocess calls stubbed) reading back the real chunk props files written to
  disk, proving every beat appears in exactly one chunk at its exact original timestamp span and
  that `leading_transition` matches the real preceding beat on every chunk after the first. Also
  re-verified directly against content 2704ad21's real 180-beat EN section list pulled from the
  database: same chunk count (9) as before the fix, zero straddling sections, max 4.1 s drift
  from the nominal 90-second marks.

#### `MediaSection.tsx` — clip mount window, effect direction variety, and crossfade clamp (render pipeline remediation, Tier 1)

File:

```text
remotion/src/components/MediaSection.tsx
remotion/src/compositions/MainVideo.tsx
remotion/src/compositions/Short.tsx
```

Deep-audit findings (`code_report/render_pipeline_python_to_remotion_deep_audit.md`,
`code_report/render_pipeline_remediation_roadmap.md` Tier 1, R1–R4) — a second, independent root
cause of "transitions are inconsistent/missing" beyond the chunk-boundary fix above, since it
occurs within a single composition at ordinary beat-to-beat cuts, chunked or not:

- **R1 — transition frame-budget mismatch (real mid-video black gaps).** `MainVideo.tsx`/
  `Short.tsx` mount a beat's `<MediaSection>` for `sectionDurFrames + crossfadeIn` frames, with the
  extra `crossfadeIn` runway added entirely at the **front** (an early start, so the beat's own
  incoming fade-in can play) — the true nominal end is unchanged. Inside `MediaSection.tsx`, the
  last clip's inner `<Sequence>` previously covered only `sectionDurFrames` — `crossfadeIn` frames
  short of the outer mount window's real end — so the clip's image vanished before the mount
  window actually closed. Whether that produced a visible black flash depended on the *next*
  beat's own incoming transition being cheaper (fewer frames) than the current beat's, a pattern
  with no coordination between consecutive beats and confirmed to recur throughout real production
  data (e.g. a `crossfade`-then-`cut` pair produces a 15-frame/0.5s gap). Fixed by sizing the last
  clip's `Sequence` to `totalMountFrames = sectionDurFrames + crossfadeIn` instead of just
  `sectionDurFrames` — the clip's own visible window now exactly matches the outer mount span.
- **R2 — `pan`/`parallax` direction variety.** Both effects previously moved in one fixed direction
  in every beat of every video (`pan`: always `translateX(0 → -5%)`; `parallax`: always
  `translateX(-3% → 3%)`) — a real, visible repetitiveness pattern across an entire render.
  `getEffectTransform()` now takes a `seed` parameter (`SingleClip`'s new `effectSeed` prop, fed
  from the beat's own `section.order`); `directionSign = seed % 2 === 0 ? 1 : -1` flips both
  effects' direction — deterministic alternation, no AI call, no schema change, the same
  beat-order-parity pattern already used for composition-slot variation on duplicate Flux prompts
  (§11.4).
- **R3 — residual motion for static effects.** `fade_in`/`cut` previously fell through to a hard
  `scale(1.0)` freeze — a beat could sit completely still for up to 9 seconds (the parent visual
  hold cap, §11.4), reading as stalled/broken video rather than a deliberate static choice. Now
  returns a whisper-subtle `scale(interpolate(progress, [0,1], [1.0, 1.02]))` instead.
- **R4 — defensive crossfade-length clamp.** `MainVideo.tsx`/`Short.tsx` now compute
  `crossfadeIn = Math.min(rawCrossfadeIn, ownDurFrames, prevDurFrames)` (own/previous beat duration
  derived independently from each section's `audio_start_ms`/`audio_end_ms`) before using it for
  both the outer `Sequence` timing and the `MediaSection` `crossfadeIn` prop — a crossfade can no
  longer be requested longer than either neighboring beat's own real duration. Safe by construction
  today (Agent 4's `INTENSITY_FLOOR_MS` already floors every beat well above the longest possible
  crossfade, §11.4), but the render layer now has its own defense if that upstream invariant is
  ever violated by a future change.

Verification: `npx tsc --noEmit` (clean compile, all `remotion/src/` sources type-checked) plus the
existing static-source-inspection Python test suite
(`tests/test_subtitles_only_rendering.py`, `tests/test_dip_to_black_removed.py`,
`tests/test_chunked_render_boundary_fix.py`, and the broader render/transition/short/chunk-scoped
subset — 123/123 passing, zero regressions). No Jest/component-render test harness exists for the
Remotion side (§19.1 forbids live renders regardless); `tsc --noEmit` plus static Python checks are
this codebase's established verification method for Remotion source changes.

#### `renderer.py` — pinned encode settings, chunk artifact lifecycle, bundle lock, concurrency cap (render pipeline remediation, Tier 2)

File:

```text
app/agents/agent5_render/services/renderer.py
```

`code_report/render_pipeline_python_to_remotion_deep_audit.md` /
`code_report/render_pipeline_remediation_roadmap.md` Tier 2, R5, R7–R10 — operational hardening,
independent of the Tier 1 viewer-visible fixes above:

- **R5 — explicit CRF on the chunk-concat fallback re-encode.** `_concatenate_chunks()` tries a
  lossless stream-copy concat first; if that fails (mismatched codec parameters between
  independently-rendered chunks — a real, non-hypothetical failure mode), it falls back to a
  libx264/aac re-encode. That fallback now passes `-crf 18` (module-level `_TARGET_CRF = 18`,
  shared with R7) instead of leaving CRF unset — ffmpeg's own libx264 default is CRF 23, a real,
  silent quality regression against the CRF 18 every chunk was already rendered at, previously
  with zero log signal that the recovery path had produced a softer result than normal.
- **R6 — subprocess timeouts — implemented, then REVERTED the same day.** Every `subprocess.run()`
  call site briefly gained a `timeout=`/`except subprocess.TimeoutExpired` guard
  (`_run_remotion()` at 600s, `_build_and_move_bundle()`'s bundle-build call at 300s,
  `_slice_audio_for_chunk()` at 120s, `_concatenate_chunks()`'s `_run_concat()` at 600s), reasoning
  that no layer above `renderer.py` recovers a genuine hang. On the very first real resume after
  this shipped, a genuinely long parent render (content `2704ad21-853c-47ff-b496-c92d147b9339`,
  ~12.7 min EN audio, hitting the `_render_from_existing_props()` non-chunked path — see the open
  observation below) was killed at exactly 600s while still legitimately in progress:
  `Remotion render timed out after 600s for MainVideo`. Operator's explicit instruction: *"we
  don't need any timeout here it must take whatever time it needs."* Every `timeout=` kwarg and
  `TimeoutExpired` handler was removed; all four `subprocess.run()` calls are unbounded again,
  matching pre-Tier-2 behavior exactly. **Do not reintroduce a subprocess timeout anywhere in this
  file without a new, explicit operator decision** — a long render legitimately taking longer than
  any fixed ceiling is normal, expected behavior for this pipeline, not a hang. Regression-guarded
  by `tests/test_render_pipeline_remediation_tier2.py::TestNoSubprocessTimeoutEnforced` (asserts no
  `timeout` kwarg reaches `subprocess.run()` at any of the four call sites, plus a direct proof
  that a call still "succeeds" with no kwarg present regardless of how long it notionally runs).
  **Open observation surfaced by this same incident, not yet actioned:** the log showed
  `_render_from_existing_props()` (`video.py`) calling non-chunked `render_main_video()` directly
  for a 12.7-minute video — that fast-resume path never checks `duration_ms` against
  `settings.chunk_duration_sec` the way `_run_renders()` does, so it bypasses chunking entirely
  regardless of video length. This is a separate, real gap, not fixed as part of this reversal.
- **R7 — pinned final encode settings.** `_run_remotion()`'s CLI invocation now passes
  `--crf 18 --pixel-format yuv420p --audio-bitrate 320k` explicitly instead of relying on
  `@remotion/renderer`'s implicit library defaults (which happen to already be these exact values
  today) — a deliberate, version-stable quality target that can't silently drift on a future
  dependency bump with no diff in this repository to review. Flag names were confirmed against the
  installed `@remotion/renderer`/`@remotion/cli` package source
  (`options/crf.js`'s `cliFlag = 'crf'`, `options/audio-bitrate.js`'s `cliFlag = 'audio-bitrate'`,
  `parse-command-line.js`'s `parsedCli['pixel-format']`), never guessed.
- **R8 — chunk artifact cleanup.** Confirmed on real disk: every chunked render's per-chunk
  MP4s/audio-slices/props-JSONs previously persisted forever after a successful concat (roughly
  doubling that render's on-disk footprint), and stale chunks from a prior differently-sized run
  were never cleaned either (`_concatenate_chunks()` only ever reads the current run's own
  `final_chunk_paths`, so e.g. an orphaned `chunk_008.mp4` from a prior 9-chunk run would silently
  persist even after a later run produces only 8 chunks). `render_main_video_chunked()` now
  `shutil.rmtree()`s then recreates the `chunks/{language}/` directory at the very start, before
  writing anything (fixes stale orphans), and `shutil.rmtree()`s it again immediately after
  `_concatenate_chunks()` returns successfully (fixes the steady-state doubling) — success-path-only:
  any exception raised before that point (a chunk crash, a concat failure) leaves the directory in
  place, since the per-chunk MP4s/props remain genuinely useful for manual post-mortem inspection.
- **R9 — `ensure_bundle()` concurrent-render lock.** `remotion bundle` ignores `--out` and always
  writes to the single shared `remotion/build/` directory — two workers hitting a cold bundle cache
  for the same source hash at once (the normal situation right after a code deploy) would otherwise
  both bundle into, and then both try to move, that same path. Currently dormant
  (`settings.remotion_pre_bundle` defaults to `False`) but a real landmine for whoever enables it.
  The check-build-move sequence is now serialized with a per-tree-hash advisory file lock
  (`fcntl.flock(LOCK_EX)` on `bundles/{hash}.lock`, blocking-acquire); the bundle existence check is
  repeated once the lock is held (double-checked locking), so a caller that was blocked waiting for
  another worker to finish bundling the identical hash reuses the completed bundle instead of
  building its own. The actual build-and-move logic moved into a new `_build_and_move_bundle()`
  helper, called only while the lock is held.
- **R10 — beat-density concurrency cap.** `app/config.py`'s `render_concurrency` comment has long
  documented "High-video-count renders (>40 video sections) are capped at render_concurrency // 2
  (min 1)," but no code implemented it — chunking-by-duration bounds how long a single Remotion
  composition plays for, not how many simultaneously-decoded images it may ask Chromium to hold in
  memory at once, which is exactly what a beat-dense chunk/render can still pressure regardless of
  chunk duration. New `_capped_concurrency(base, n_sections)` (halves, floor 1, above
  `_HIGH_SECTION_COUNT_THRESHOLD = 40`) and `_count_props_sections(props_path)` (reads
  `len(props["sections"])`; `0` on any read failure — fail-open, never blocks a render) are wired
  into `render_main_video()` and `render_short()` (capped against that render's own total section
  count) and into `render_main_video_chunked()`'s `_render_chunk()` closure — capped **per chunk**,
  using each chunk's own `n_sections` (already tracked in `chunk_specs`), not the whole render's
  total, since a chunk's own beat density is what pressures its own independent Chromium process.
  `app/config.py`'s existing comment needed no wording change — it now accurately describes real
  behavior.

Verification: `ast.parse` (clean), the full existing Python test suite (742 passed / 145 subtests
passed, zero regressions), and 18 runtime-proof tests in
`tests/test_render_pipeline_remediation_tier2.py` — real `renderer.py` code paths throughout
(`_capped_concurrency()`, `_count_props_sections()`, `render_main_video()`,
`render_main_video_chunked()`, `ensure_bundle()`, `_concatenate_chunks()`, `_run_remotion()`,
`_slice_audio_for_chunk()` all run unmodified); only `subprocess.run()` itself is ever mocked, per
§19.1 — no live API/render calls anywhere, including a real-threads proof for R9 (two
`threading.Thread`s actually racing to acquire the same `fcntl.flock`, with a real 0.2s sleep
forcing genuine overlap, proving the second caller's bundle-build subprocess call never fires) and
`TestNoSubprocessTimeoutEnforced` guarding R6's reversal.

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

#### `build_standard_subtitles` / `build_karaoke_subtitles` — caption punctuation restoration (roadmap Phase B2)

File:

```text
app/agents/agent5_render/services/subtitles.py
```

Cross-validation against the operator's own frame-by-frame video review
confirmed OpenAI Whisper's word-level `words` array strips punctuation and
apostrophes, splits French elisions ("Sainte Sophie n était plus" instead of
"Sainte-Sophie n'était plus"), splits numbers into separate digit tokens
("30"/"000" instead of one "30,000"/"30 000"), and occasionally mishears a
recurring proper noun entirely (a real production run showed "Belisarius"
transcribed as "Narcissus"). `_chunk_transcript()`'s own sentence/clause
punctuation regexes (`_SENTENCE_END_RE`/`_CLAUSE_END_RE`) were always
correct — they just never had real punctuation to match against, so
chunking silently fell through to the hard word-count/duration ceiling on
every caption, a hidden secondary bug this phase also fixes as a byproduct.

Both functions gained two optional parameters, fully backward compatible
(omitting `voice_script` reproduces the exact pre-existing raw-Whisper-word
behavior — no caller is forced to change):

- `voice_script: str = ""` — the punctuated narrator `Script.voice_script`
  text for this language. When non-empty, caption display text is restored
  from it while timing stays Whisper's own, via
  `_restore_punctuated_words()`.
- `proper_nouns: list[str] | None = None` — the story's recurring proper
  nouns (`app.services.story_entities.extract_recurring_proper_nouns()`,
  §7.7) for Whisper-mishearing correction.

`_restore_punctuated_words(whisper_transcript, voice_script, proper_nouns)`
— deterministic, no AI call, stdlib-only (`difflib`):

1. `_correct_named_entities()` — a pre-pass over Whisper's raw words: any
   word whose normalized form is a close fuzzy match
   (`difflib.get_close_matches()`, cutoff 0.72, words ≥4 chars only) to a
   known story proper noun is replaced with the noun's canonical spelling.
   Timing is never touched. This is a narrow, honest safety net — it
   catches close mishearings ("Belisaire" → "Belisarius") but not a wildly
   different substitution (SequenceMatcher ratio for "Narcissus" vs
   "Belisarius" is ~0.42, well under the cutoff); those are instead
   recovered by mechanism 2 below, which doesn't depend on word similarity
   at all.
2. `difflib.SequenceMatcher(None, whisper_norms, script_norms,
   autojunk=False)` — monotonic sequence alignment between the two
   near-identical word sequences (same narration, same order), using
   Python's well-tested Ratcliff/Obershelp algorithm rather than a
   hand-rolled aligner. `autojunk=False` is required: the default heuristic
   (built for text-diffing, not our use case) would down-weight common
   connector words on a 1200+ word script, hurting alignment quality.
   `_normalize_token()` (lowercase, strip all punctuation including
   apostrophes) is comparison-only — display text always comes from the
   original, unstripped token.
3. Opcodes drive the output: `equal` blocks pair each Whisper word 1:1 with
   its script counterpart, using the script's punctuated text for display
   and Whisper's own timing. `replace` blocks up to `_MAX_MERGE_SPAN` (6)
   tokens on either side are merged into ONE display unit — the script's
   full span joined with spaces, spanning the combined Whisper timing; this
   is what makes a split number ("30"+"000") or elision ("n"+"était")
   display as one atomic, correctly-punctuated unit, and what recovers a
   simple word substitution ("Narcissus"→"Belisarius") through alignment
   alone, no dictionary needed. An oversized `replace` block, or a `delete`
   block (Whisper-only words with no script counterpart), falls open to
   Whisper's own raw text — never fabricated, never dropped. `insert`
   blocks (script-only words with no Whisper timing to anchor to) are
   skipped — never given a guessed timestamp.
4. `_tokenize_script()` strips `[INTRO]`/`[SECTION N]`/`[OUTRO]` markers AND
   every other bracketed span (`_BRACKET_TAG_RE`, pre-next-test roadmap
   Tier 2 R5 — ElevenLabs v3 audio tags like `[dramatic pause]` embedded in
   `voice_script` by Agent 2's eleven_v3 TTS block; three confirmed
   production leaks shipped a tag verbatim as caption display text via a
   replace-block merge before this scrub existed; safe by construction
   because no bracket content is ever spoken, so it can never legitimately
   align to a Whisper word), and pre-merges French-style space-grouped
   numbers (`_merge_number_groups()`:
   a leading all-digit token followed by one or more exact 3-digit groups,
   e.g. "30" + "000" → "30 000") into one atomic script token before
   alignment even runs — a script already using commas ("30,000") is
   already one whitespace token and needs no merging.

Returns the exact same shape as `whisper_transcript` itself
(`[{"word": str, "start": float, "end": float}, ...]`) — a drop-in
replacement anywhere a raw Whisper transcript is consumed, so
`_chunk_transcript()` itself needed zero changes.

Wiring (`agent5_render/services/video.py`):

- `_resolve_proper_nouns_for_content(content, db)` — parent content reads
  its own `story_blueprint`; child Shorts (which never get their own
  blueprint — only parent script generation runs
  `generate_story_blueprint()`) read the parent's instead via
  `parent_content_id`, the same "children inherit the parent's value"
  convention documented for the not-yet-implemented `protagonist_gender`
  field. Returns `[]`, never raises, when no blueprint is reachable.
  Computed once per content in `run_video_generation()`, before the
  per-language loop.
- `_process_language()` passes `voice_script=script.voice_script or ""` and
  `proper_nouns=proper_nouns` into both `build_standard_subtitles()` and
  `build_karaoke_subtitles()` calls.

Runtime proof: `tests/test_caption_punctuation_restoration.py` (28 tests —
`subtitles.py` has zero app-internal imports, stdlib-only, so these run with
no stub harness at all) covers every mechanism above with representative
fixtures mirroring the audited defects (French elision, English/French
number splitting, name substitution recovered by alignment alone vs. by the
entity dictionary, Whisper-only insertions, script-only words dropped, an
oversized mismatch block falling back safely, and the sentence-boundary
chunking side-effect fix) plus full backward-compatibility proof.
`tests/test_caption_proper_nouns_wiring.py` (7 tests) proves the
field-set-in-Agent-4-blueprint → read-in-Agent-5-render chain: parent vs.
child blueprint resolution, and that `_process_language()` actually forwards
`voice_script`/`proper_nouns` as real call arguments to both subtitle
builders (not just that the parameters exist).

Rules:

- Never invent display text or timing — every fallback path (oversized
  mismatch, Whisper-only word, script-only word) either uses Whisper's own
  real text/timing or skips the word entirely; nothing is guessed.
- `_MAX_MERGE_SPAN`, `_ENTITY_MATCH_CUTOFF`, and `_ENTITY_MIN_WORD_LEN` are
  deliberately conservative — this is the same accepted
  false-positive/false-negative tradeoff as every other deterministic
  keyword/heuristic check in this codebase (CLAUDE.md §11.4's
  storyboard-validator checks); do not "fix" a missed edge case by adding
  an AI call here.
- Do not fork a second alignment implementation for standard vs. karaoke
  styles — both call the same `_restore_punctuated_words()` /
  `_chunk_transcript()` pair; extend those, not a per-style copy.

**Shorts caption vertical position (roadmap Phase B2):**
`remotion/src/components/KaraokeSubtitles.tsx`'s `containerStyle.paddingBottom`
was raised from `32%` to `39.8%` of the 1920px-tall Short canvas (~150px) so
captions clear TikTok/Reels/Shorts platform UI (caption/description overlay,
like/share rail, progress bar) that occupies the bottom of the frame.
`StandardSubtitles.tsx` (main 16:9 video) is unaffected — no platform UI
chrome overlaps a horizontal video the same way.

Karaoke word tokens use `marginRight: "0.45em"`. The prior 0.25em
gap was narrower than Arial Black's natural space at the 82px Short caption
size and repeatedly made clean adjacent tokens look fused. Caption data and
standard main-video subtitles are unchanged.

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

## 11B. Agent 6 — Thumbnails, Titles & Descriptions

Design record: `code_report/agent6_metadata_roadmap.md` (full Checks 1-6
investigation, detailed design, and per-phase implementation record).

### 11B.1 Agent 6 Responsibilities

Agent 6 owns:

- Per-platform title/description/hashtag generation, one Claude call per
  `(language, verified platform)` pair.
- YouTube thumbnail generation: one shared, language-agnostic Flux base
  image per content item, plus a per-language Pillow text overlay burned
  from that language's own `thumbnail_text`.
- Deterministic, Python-enforced platform limits (title/description/hashtag
  length caps, thumbnail-overlay word/char caps) — Claude proposes, Python
  decides, exactly like every other generation stage in this pipeline.
- The `RENDERED` → `GENERATING_METADATA` → `METADATA_PENDING_APPROVAL` →
  `METADATA_APPROVED` status sequence, scheduler pickup, and the
  time-based auto-approve sweep.
- A minimal backend manual-review API surface (approve early, edit a
  `VideoMetadata` row) — no operator-facing UI ships with this agent.

Agent 6 must not:

- Publish anything to any platform (YouTube, TikTok, Instagram, Facebook) —
  it only writes `VideoMetadata` rows; actual publishing is Agent 7's job,
  not yet built.
- Own credential verification of any kind.
- Read or write `PublishSchedule.content_id` — that remains Agent 7's job.
- Import `app.agents.agent4_visuals` or `app.agents.agent5_render` — Flux
  access goes through the shared `app.services.flux_client` (§7.8), never
  Agent 4's private package.
- Write `RENDERING`/`RENDERED` (Agent 5's statuses) or any visual-stage
  status.
- Generate parent short cuts, breakpoints, rehooks, or bridges — none of
  that concept exists at this stage of the pipeline.
- Reuse Agent 4's beat-oriented pixel-defect/health-check machinery
  (`_ensure_beat_image_healthy()` and friends) — a standalone thumbnail is
  a different problem shape from a 100+-beat storyboard sequence; Agent 6
  owns its own small, self-contained validation instead (§11B.4).

### 11B.2 Agent 6 Flow

```mermaid
flowchart TD
    A[Content RENDERED] --> B[pickup_rendered_content]
    B --> C[run_agent6_metadata_for_content]
    C --> C2[Content GENERATING_METADATA]
    C2 --> D["(a) platform_metadata_generation per language x platform"]
    C2 --> E["(b) thumbnail base image — parent long-form + verified youtube only"]
    D --> F["(c) per-language Pillow overlay from thumbnail_text"]
    E --> F
    F --> G[persist VideoMetadata rows]
    G --> H[Content METADATA_PENDING_APPROVAL]
    H -->|operator POST .../approve| I[Content METADATA_APPROVED]
    H -->|check_metadata_auto_approve sweep, timer elapsed| I
```

**Runtime sequencing is not the same as build order and is easy to get
backwards:** the thumbnail base image (language-agnostic) has no dependency
on metadata generation and may run before/after/concurrently with it, but
the per-language Pillow overlay has a hard dependency on that language's
`thumbnail_text` — an *output* of metadata generation, not of the
thumbnail-prompt call. `run_metadata_generation_for_content()` therefore
always sequences (a) metadata generation for every pair first, (b) the base
image, (c) only once both exist, the per-language overlay. A language whose
metadata call failed skips its own overlay — logged, non-fatal to every
other language, the same per-item independent-degrade convention every
other agent's per-language loop already uses.

### 11B.3 Important Agent 6 Functions

#### `generate_platform_metadata`

File:

```text
app/agents/agent6_metadata/system_prompt.py
```

One `call_claude_structured(task="platform_metadata_generation", ...)` call
per `(platform, language)` pair — no content-quality retry loop (Elimination
Mandate convention, §9.3/§23); a transport failure returns `None`, never
raises, and the caller skips that pair non-fatally. `_PLATFORM_STYLE_GUIDANCE`
gives each of the four platforms a genuinely distinct instruction line — not
one generic call reused four times. The schema always includes
`thumbnail_text`, but it is only ever *requested* (and only ever read/used)
for `platform="youtube"` — the overlay phrase is never generated as a
separate call, and is never the same value as `title`.

#### `generate_thumbnail_prompt`

File:

```text
app/agents/agent6_metadata/system_prompt.py
```

One `call_claude_structured(task="thumbnail_prompt_generation", ...)` call,
grounded in `story_blueprint.hook`/`.character_descriptors`/`.era_setting` +
`ChannelConfig.visual_style`/`.image_style`. Appends the same no-text clause
Agent 4's text-prop sanitization already uses (§11.6) and rejects/retries
once on a quoted phrase or "sign that says"/"text reads" pattern before
falling back to a deterministic safe-template prompt — never raises.

#### `generate_thumbnail_base_image`

File:

```text
app/agents/agent6_metadata/services/thumbnail.py
```

Stages 1-3 of the thumbnail pipeline: Flux generation at 1280x720 via the
shared `app.services.flux_client.generate_beat_image()` (§7.8), with
`model_key` resolved by `_resolve_model_key()` — `"dev"` when
`settings.image_routing_enabled and settings.image_routing_allow_dev`, else
`"schnell"` (reuses Agent 4's existing kill switches; no third,
thumbnail-specific env var). Deterministic validation (file exists, PIL
decodable, dimensions exactly 1280x720 — hard equality, no tolerance band)
with exactly one retry on a fresh cache key; a second failure is non-fatal —
logged, `thumbnail_file_path` stays `NULL`, metadata generation proceeds
(YouTube falls back to its own platform-default thumbnail selection). Gated
to parent long-form content (`is_short_episode == False`) with at least one
verified `platform="youtube"` `ChannelPlatform` row — every child-of-parent
Short, every Solo Short, and every channel with no YouTube target skips this
entirely, with zero Flux calls. On success, the validated image is copied to
the canonical, predictable path
`{media_path}/thumbnails/{content_id}/base.jpg` — never the raw Flux
prompt-hash cache filename — which every later idempotency check
(`_ensure_base_image()`, a PATCH re-composite) keys on directly.

#### `composite_thumbnail_overlay`

File:

```text
app/agents/agent6_metadata/services/thumbnail.py
```

Stages 4-5: burns one language's `thumbnail_text` (already
platform-limit-enforced — never the title) onto a copy of the shared base
image, Pillow-only, zero paid calls, zero network. Before rendering, checks
that the bundled font (`assets/fonts/ArchivoBlack-Regular.ttf`, OFL-licensed)
covers every glyph in the text via a Private-Use-Area bbox-comparison
heuristic (`_font_covers_text()` — see CLAUDE.md §34's own risk entry for
this technique's documented limitation); an uncovered glyph skips the
overlay for that language non-fatally, logged
`THUMBNAIL_FONT_GLYPH_MISSING`. Final output is re-validated (dimensions
still exactly 1280x720, re-openable, saved under YouTube's real 2MB upload
ceiling via a quality-backoff loop) before being returned. Never regenerates
the base image — this function is also the PATCH-endpoint re-composite's
single call site (§11B.4), so a `thumbnail_text` edit is always Pillow-only.

#### `run_metadata_generation_for_content`

File:

```text
app/agents/agent6_metadata/services/metadata_orchestrator.py
```

The real per-content entrypoint. Deliberately owns **no `Content.status`
transition of its own** — that is `run_agent6_metadata_for_content()`'s job
(the Celery task wrapper, below) — so this function stays directly callable
from tests with no status side effect. Resolves languages from the
intersection of real `VideoRender` rows (format matching the content's
shape) and validated `Script` rows (§11B's Check 2 Finding 2.4 — a language
can have a validated script yet still have failed to render, and Agent 6
must only describe languages that actually produced a video); resolves
platforms from every verified `ChannelPlatform` row for the channel, with no
shape filtering (text metadata is generated for every configured platform
regardless of render format — only the thumbnail sub-step is
`is_short_episode`-gated); resolves `story_blueprint` with the same
child-of-parent-Short-inherits-from-parent fallback
`agent5_render.services.video._resolve_proper_nouns_for_content()` already
established (§11A.4) — a Solo Short never attempts this lookup at all,
since it has no parent.

Sequences (a) metadata generation for every `(language, platform)` pair,
(b) `_ensure_base_image()` — idempotent, keyed on the canonical
`thumbnails/{content_id}/base.jpg` path from `generate_thumbnail_base_image()`
above, never re-derived from a Flux cache hash; a content item whose base
image already exists on disk from a prior run costs zero additional Flux
calls, (c) per-language overlay + `VideoMetadata` upsert, only once both (a)
and (b) exist. YouTube descriptions get real, Python-computed timestamps
(`build_youtube_timestamps()`, from `VideoSection.audio_start_ms` — never
Claude-generated) appended before platform-limit enforcement runs, so the
length check sees the true final text.

**A failure in `platform_limits.enforce_platform_limits()` (e.g. an unknown
platform from a stale/legacy `ChannelPlatform` row) degrades only that one
`(language, platform)` pair** — caught, logged
(`METADATA_PLATFORM_LIMITS_UNKNOWN_PLATFORM`), and skipped via `continue`,
never aborting the whole content item or discarding every other pair's
already-generated metadata. This is the general per-pair degrade contract
every failure mode in this orchestrator follows: a transport failure, a
missing thumbnail, an uncovered font glyph, or an unknown platform each
independently costs only their own pair, never the whole run.

Returns `True` if at least one `VideoMetadata` row was produced, `False`
otherwise — the exact contract `run_agent6_metadata_for_content()` uses to
define total failure (below).

#### `pickup_rendered_content` / `run_agent6_metadata_for_content` / `check_metadata_auto_approve`

File:

```text
app/scheduler/tasks.py
```

`pickup_rendered_content()` dispatches every content item with
`Content.status == "RENDERED"` — the sole readiness signal (nothing else in
the repo ever reads or filters on `RENDERED`, so this pickup is purely
additive). Deliberately excludes `NEEDS_REVIEW` content: that status is a
distinct, explicit operator-attention flag a content row can end up in
instead of `RENDERED` even with real `VideoRender` rows present, and
metadata generation must never silently proceed on a render Agent 5 flagged
as broken.

`run_agent6_metadata_for_content()` wraps the orchestrator and owns the
surrounding status machine: `GENERATING_METADATA` before calling, then on
success `METADATA_PENDING_APPROVAL` + `Content.metadata_generated_at =
now()`, or on **total failure — defined explicitly as zero `VideoMetadata`
rows produced for the content item**, `FAILED` (mirroring
`run_video_generation()`'s `successful > 0` convention exactly, §11A.4). A
content item with some, but not all, pairs succeeding is *not* a total
failure and still reaches `METADATA_PENDING_APPROVAL`.

`check_metadata_auto_approve()` mirrors `check_validation_timeouts()`'s
shape exactly (timestamp column + periodic sweep comparing against `now()`,
§9.3's Telegram-approval precedent): every 15 minutes, promotes
`METADATA_PENDING_APPROVAL` content whose `metadata_generated_at` is older
than `settings.metadata_auto_approve_seconds` (default 3600) to
`METADATA_APPROVED`. Scoped to `Content` directly, not a new side-table —
unlike the Telegram sweep's `_apply_limit_policy()`, this transition carries
no per-channel policy branching, so it is pure status-machine mechanics
owned directly in `tasks.py` rather than delegated to an agent service.

#### Manual approve/edit API surface

File:

```text
app/agents/agent6_metadata/routers/metadata.py
```

Backend only — no operator-facing review screen ships with this agent.

- `POST /api/agent6/content/{content_id}/approve` — sets
  `Content.status = "METADATA_APPROVED"` early, bypassing the auto-approve
  timer, exactly the way a human Telegram `APPROVE` reply already bypasses
  `check_validation_timeouts()`'s sweep. Requires
  `Content.status == "METADATA_PENDING_APPROVAL"`; any other status is a
  `400`.
- `PATCH /api/agent6/video-metadata/{id}` — **not a bare field write.**
  Every edited field is re-run through the same `platform_limits`
  enforcement generation already uses before persisting — an operator
  cannot paste a value past the deterministic cap Python otherwise
  guarantees everywhere else. A `thumbnail_text` edit on a
  `platform="youtube"` row additionally triggers a Pillow-only re-composite
  via `composite_thumbnail_overlay()` from the base image already on disk —
  zero paid calls, zero Flux interaction — since without it, an edited
  `thumbnail_text` would sit correctly in the DB row while the
  already-composited image on disk kept showing the old text. Setting
  `thumbnail_text` on any non-youtube row is rejected (`400`) — the schema
  invariant (`thumbnail_text` NULL on every non-youtube row) is enforced at
  the edit boundary too, not just at generation time.

### 11B.4 Platform Limits — Python Constants, Enforced Deterministically

File:

```text
app/agents/agent6_metadata/services/platform_limits.py
```

```python
PLATFORM_LIMITS = {
    "youtube":   {"title_max": 100,  "description_max": 5000,  "hashtag_max": None},
    "tiktok":    {"title_max": 150,  "description_max": 2200,  "hashtag_max": 5},
    "instagram": {"title_max": 125,  "description_max": 2200,  "hashtag_max": 30},
    "facebook":  {"title_max": 255,  "description_max": 63206, "hashtag_max": None},
}
THUMBNAIL_TEXT_MAX_CHARS = 30
THUMBNAIL_TEXT_MAX_WORDS = 5
```

`enforce_platform_limits(platform, title, description, hashtags)` truncates
at a word boundary (never mid-word), caps hashtags to `hashtag_max` keeping
Claude's own ordering, logs every truncation
(`METADATA_LIMIT_ENFORCED platform=... field=... original_len=...
final_len=...`), and **raises `ValueError` for an unrecognized platform
key** (fail-loud, mirroring `resolve_model()`'s unknown-task-key
convention) — callers must catch this per-pair, never let it abort a whole
content item (§11B.3's orchestrator entry above shows the required catch
site). `enforce_thumbnail_text_limit(text)` applies the same word-boundary
shape against `THUMBNAIL_TEXT_MAX_CHARS`/`_MAX_WORDS`, called both at
generation time and by the PATCH endpoint — one implementation, never
forked.

### 11B.5 Database Migration

`alembic/versions/017_add_agent6_metadata_fields.py` — additive only:

```python
op.add_column("content", sa.Column("metadata_generated_at", sa.DateTime(timezone=True), nullable=True))
op.add_column("video_metadata", sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
op.add_column("video_metadata", sa.Column("thumbnail_text", sa.String(64), nullable=True))
```

No new tables.

Rules:

- Do not reintroduce a text-card-style overlay concept here — Agent 6's
  thumbnail overlay is a static marketing image burned once per language,
  not a Remotion-rendered video layer (Golden Rule 11/§11.6 governs the
  video's own text layer, unrelated to this).
- Do not let `thumbnail_text` and `title` drift toward being treated as the
  same value anywhere in future code — they are deliberately independent
  fields with independent limits.
- Do not add a real per-channel font/styling `ChannelConfig` column under
  this section without a dedicated design phase — the current bundled font
  (Archivo Black) and fixed overlay styling are documented defaults, not
  yet configurable (roadmap's still-open Open Decision item).

---

## 12. Parent vs Child Short vs Solo Short Comparison

| Dimension | Parent long content | Child short episode | Solo Short (`output_mode="shorts_only"`) |
|---|---|---|---|
| `content.is_short_episode` | `False` | `True` | `True` |
| Parent link | none | `parent_content_id` | none (`parent_content_id=NULL`) — this is the field that distinguishes it from a child short episode, not `is_short_episode` alone |
| `short_part_number`/`short_total_parts` | `NULL`/`NULL` | 1-based index / total parts | `1`/`1` — always exactly one part |
| Script | long structured script | standalone flat short narration, derived from/grounded in the parent's finished script | standalone flat short narration, written directly from raw source material — no parent script exists to derive from |
| Audio | own full audio | own short audio | own short audio |
| Whisper | parent transcript | child transcript | own transcript |
| Breakpoints | empty | empty | empty |
| Bookends/rehooks/bridges | disabled | disabled | disabled |
| Visual pass | full original storyboard + Flux | **remap** parent visual beats, then cap non-terminal visual holds | full **original** storyboard + Flux (§11.4's `_run_solo_short_visuals`) — same mechanism as the parent, not a remap; no parent beats exist to remap from |
| Images | parent Flux cache, landscape 1920×1080 | every beat regenerated portrait 1080×1920 in child cache, reusing the parent's prompt where match score qualifies | every beat generated fresh, portrait 1080×1920, from its own original storyboard prompts (no parent prompt to inherit) |
| Remotion composition | `MainVideo.tsx` | `Short.tsx` | `Short.tsx` |
| Resolution | 1920×1080 | 1080×1920 | 1080×1920 |
| `VideoRender.format` | `"main"` | `"short"` | `"short"` |
| Render content ID | parent ID | child ID | its own ID |
| Status flow | parent flow (§4.1) | child flow (§4.2, may defer `GENERATING_VISUALS`→`AUDIO_DONE` waiting on its parent) | parent-shaped flow (§4.4) — never defers waiting on a parent, since it has none |
| Part label (`Short.tsx`) | n/a | shown (`settings.short_part_label_enabled`, real `total_parts>1`) | always suppressed (`total_parts=1`, Finding E, §11A.4) |
| Agent 6 thumbnail (§11B) | generated (`is_short_episode == False`, verified YouTube platform required) | never generated (`is_short_episode == True`) — text metadata only | never generated (`is_short_episode == True`) — text metadata only, same as a child short episode |

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

**The visual-bible layer is deleted (Elimination Mandate D2.1,
`code_report/forensic_output_audit_borrasca_run.md`).** No new
`visual_bible.json` is ever generated — a real production run showed it
returning 0 locations, 0 recurring motifs, and 0 first-15 rules, a paid
Claude call and token cost for a total no-op on every storyboard batch. It
is replaced by `continuity_line`
(`storyboard.build_continuity_line_from_blueprint()`, §11.4): a pure-Python,
no-AI-call extraction of proper nouns that recur across the blueprint's own
text, threaded as one plain sentence into every `generate_storyboard_batch()`
call. `app/agents/agent4_visuals/services/cinematic_prompts.py` and
`.../first15_validator.py` — the enrichment and first-15
enhancement/validation modules that consumed the old bible — no longer
exist as files, and `visual_orchestrator._check_final_prompt_issues()` (the
re-validation pass that existed only to re-check `flux_prompt` after those
two mutation passes) is deleted with them. `app/agents/agent4_visuals/services/visual_bible.py`
keeps only two read-only helpers (`get_visual_bible_path()`,
`load_visual_bible_for_content()`) so `visual_review.html` can still render
a `visual_bible.json` left on disk by a run from before this change; no
current code path writes that file.

Agent 4 still writes review-only beat metadata to the `visuals/` subfolder
(roadmap 6.5 / audit AR-3 — moved out of the DB's `generation_prompt` blob,
which nothing in the live pipeline queried back):

```text
{media_path}/runs/{content_id}/visuals/beat_review_metadata.json
```

Current storyboard fields (`visual_intent`, `flux_prompt`, visual enums,
timing hints, intensity/duration, and media identity) are snapshotted here,
keyed by language then `section_order`; legacy prompt-quality/descriptive keys
(`subject`, `emotion`, `camera`, etc.) are retained when an older input carries
them. Written by `save_beat_review_metadata()` once per language, alongside
every `_save_video_sections()` call. Local-debug/review
infrastructure only — `visual_review.html` reads it; nothing else does, and
Agent 5 never depends on it. Its `continuity_tags`/`visual_bible_refs` and
first15 diagnostic fields are dead going forward (nothing generates them
any more) but degrade gracefully to empty/dash for new content while still
rendering correctly for pre-existing files — see §11.4's `_beat_extras` /
`save_beat_review_metadata` entry.

Storyboard generation now flows straight from Claude's structured output
into `validate_storyboard()` (`_run_storyboard_validation()` /
`_check_storyboard_issues()`) with no intermediate enrichment or mutation
pass — that single deterministic validator is the sole
validation/telemetry step between storyboard generation and Flux
generation, per D3 of the mandate. Agent 5 still does not generate, repair,
or validate prompts.

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
| `pickup_audio_done` | enqueue Agent 4 visual generation |
| `run_agent4_visual_generation_for_content` | generate storyboard/Flux visuals or child remap; persist `VideoSection` rows |
| `pickup_visual_ready` | enqueue Agent 5 render once status is `PARENT_VISUALS_DONE`/`CHILD_SHORT_VISUALS_DONE` |
| `run_agent5_render_for_content` | render video; requires visual-done status and existing `AudioFile`/`VideoSection` rows |
| `pickup_rendered_content` | enqueue Agent 6 metadata generation once status is `RENDERED` (never `NEEDS_REVIEW`) |
| `run_agent6_metadata_for_content` | generate titles/descriptions/hashtags/thumbnail; persist `VideoMetadata` rows |
| `check_metadata_auto_approve` | promote `METADATA_PENDING_APPROVAL` content past `settings.metadata_auto_approve_seconds` to `METADATA_APPROVED` |

Rules:

- Beat tasks are safety nets, not the only orchestration mechanism for critical handoffs.
- Every pickup also re-dispatches rows stuck in its stage's in-progress
  status past the per-stage `_STALE_RECOVERY_MINUTES` threshold (stuck-state
  recovery — see §4.3); the Agent 2/3 task guards accept their own
  in-progress status so Celery retries and the sweep actually re-enter the
  service.
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
    the single operator-approved exception, behind
    `short_part_label_enabled`, default on.)

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

**"Solo Short" vs. "standalone short architecture" — two distinct terms, do
not conflate them.** This document already uses "standalone short
architecture" (and derived names like `standalone_shorts_only`,
`STANDALONE_SHORTS_ONLY`, `render_standalone_short`) to mean *"each
child-of-a-parent Short is its own independently-rendered `Content` row"*
— see §9.4/§28. **"Solo Short"** (§4.4/§5.3/§9.6/§11.4) is a different,
newer concept: *"a short episode with no parent at all"*
(`output_mode="shorts_only"`, `is_short_episode=True,
parent_content_id=NULL`). A child-of-a-parent Short is always
"standalone-rendered" in the first sense; a Solo Short never had a parent
to be standalone *from* in that sense — it was never a cut of anything.
Keep new function/log/doc names for the parentless-short concept using
"Solo Short"/`solo_short_*` (e.g. `_run_solo_short_visuals()`,
`generate_solo_short_script()`, `SOLO_SHORT_SCRIPT_START`) — never reuse
"standalone" for it, and never assume an existing `standalone_*`/
`STANDALONE_*` symbol refers to a Solo Short.

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
- A blank line is a long audible TTS pause — scripts use one ONLY at a
  genuine scene/beat change (roughly 3-6 paragraphs per long-form section,
  at most 3-4 breaks in a Short), never after every sentence (pre-next-test
  roadmap Tier 1 R2, `_TTS_SHARED_CORE`; a real run's per-sentence
  paragraphs rendered 31% of the video as silence).
- End with a strong, relevant comment trigger when required.
- Comment triggers and Short final CTAs must be story-specific, not reusable
  channel boilerplate. Child Shorts must not copy the parent's
  `blueprint.comment_trigger` verbatim or near-verbatim; prompt guidance handles
  diversity, not a retry loop.
- Short title spoiler checks are telemetry-only: deterministic token overlap
  against the part reveal/cliffhanger and blueprint final payoff logs
  `SHORT_TITLE_SPOILER_TELEMETRY`, but never rewrites, retries, or blocks the
  generated child script.
- Favor spoken-video delivery over book/documentary-page register (roadmap
  4.3 / audit S-3, §6): present tense for story events wherever the story
  allows it, direct address to the viewer at least once per section,
  mandatory contractions, and a read-aloud test (rewrite any sentence that
  would not be said aloud to a friend).
- Deliver the blueprint's `midpoint_retention_trap` — a reveal placed near
  the story's halfway point — as a targeted constraint on the one body
  section nearest that point, not just passively present in the blueprint.
- Honor the channel's configured `narration_pov` (roadmap 4a / audit P1-9):
  third-person narrator voice by default, or first-person "storytime"
  narration ("I"/"me"/"my") when configured — never mixed within one
  section, and never hardcoded to one register regardless of config.
- Trust construction, not the prompt alone, for hook placement (roadmap 4c /
  audit P1-4): the blueprint's `hook` is deterministically prepended as the
  INTRO's literal opening line in Python (`_apply_hook_by_construction()`),
  regardless of where Claude itself chose to place similar content. Do not
  rely on `check_hook_quality()` (shape-only: ≤15 words, no forbidden
  opener) to guarantee which sentence opens the script — it cannot and must
  not be asked to.

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

- be standalone, including the literal first sentence
- use dedicated hooks
- work without a title card, part number, earlier sentence, or parent context
- pass the cold-open deletion test in the planner, source Short writer, and
  child-Short native adaptation prompt: the opening must name the person,
  event, object, or situation needed to understand it; backward-dependent
  fragments such as "they weren't hypothetical" must have their missing
  referent repaired without adding recap or revealing the payoff
- be optimized for short-form retention
- maintain factual connection to parent story
- avoid `[SECTION N]` markers
- fit expected short duration
- pass TTS checks before validation
- follow the same spoken-video delivery rules as long-form sections (§21.3):
  present tense, direct address, contractions, read-aloud test
- follow the same channel-configured `narration_pov` as the parent long-form
  script (roadmap 4a / audit P1-9)
- avoid generic clickbait filler and canned suspense phrases (prompt
  engineering audit §2.5) — a short, example-anchored instruction (2-4
  stock-phrase examples), not a long banned-word list: the long-form section
  prompt's own 15-entry banned-phrase list was deliberately not ported over
  verbatim, to avoid re-loading a prompt this codebase just finished
  trimming down (§9.3's section-prompt subtraction, v4.7)

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
`tests/test_agent2_structured_json_migration.py`. (Historical note:
`assess_script_quality()` and `_SCRIPT_QUALITY_SCHEMA`, migrated here, were
later deleted entirely by the Elimination Mandate — §9.3's
`run_script_quality_gate` entry — not just re-migrated again; this
structured-call migration history remains accurate for the other three
functions, which are still live.)

---

## 23. Model Routing Rules

Model choice must match task type.

High-quality/complex tasks:

- story research
- story gate scoring
- story blueprint when quality is critical
- section generation
- short script generation
- storyboard generation
- AI-generated story synthesis (`story_synthesis` — premise + post-approval
  expansion, §9.5) — original creative fiction or an explicitly requested,
  accuracy-bound historical narrative becomes the sole source material for
  the rest of the pipeline, same quality bar as a real discovered story
- `thumbnail_prompt_generation` (Agent 6, §11B) — a weak Flux prompt
  directly costs click-through, the same "quality matters" bar as
  storyboard/story_blueprint
- `platform_metadata_generation` (Agent 6, §11B) — titles/descriptions
  directly affect audience acquisition/SEO, the same bar `channel_suggestion`
  already uses ("poor suggestions harm onboarding quality"); title/thumbnail
  quality has no downstream corrective pass the way `shorts_planner`'s
  output does, unlike the fast/cheap tier below

Fast/cheap tasks:

- shorts planner
- short storyboard remap
- content reformat
- simple suggestions

Retired tasks (Elimination Mandate, `code_report/forensic_output_audit_borrasca_run.md`
D1.1/D1.2/D1.3/D2.1, extended by the post-roadmap deep audit) — do not
reintroduce without a new operator decision superseding the mandate:
`quality_rewrite`, `global_validation`, `script_quality_check`,
`short_quality_check`, `visual_bible_generation`, `auto_correction` (the
auto_correct_script prompt-repair layer, deleted with its caller),
`script_generation` (never had a call site — section generation uses
`section_generation`), and `revision` (fresh full-system audit §1.3 — the
script-revision chain was unreachable dead code; Telegram CHANGE is now
story-level feedback with no Claude call). None of these exist in
`MODEL_ROUTING` anymore; `resolve_model()` raises `ValueError` for all
eight.

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
CHILD_SHORT_PROMPT_INHERITANCE_STATS
STANDALONE_SHORT_RENDER
STANDALONE_SHORT_RENDER_DONE
PRE_RENDER_ASSET_AUDIT
BEAT_IMAGE_HARD_RETRY
BEAT_IMAGE_NEIGHBOR_REUSED
BEAT_IMAGE_DEFECT
BEAT_IMAGE_HEALED
BEAT_IMAGE_HEAL_EXHAUSTED
SHORT_SCRIPT_WORD_FLOOR_REGEN
TTS_TRANSPORT_RETRY
AUDIO_LANGUAGE_RETRY
AUDIO_LANGUAGE_MISSING
AGENT4_MEDIA_REPAIR_DONE
MEDIA_REPAIR_REGENERATED
MEDIA_REPAIR_NEIGHBOR_FALLBACK
AGENT4_MEDIA_VALIDATION_UNREPAIRED
DARK_CONTRAST_GRADE_DOWNGRADED
DUPLICATE_PROMPT_REPAIRED
PARENT_VISUALS_STALE_SCRIPT_HASH
PARENT_VISUALS_STALE_AUDIO_DURATION
STALE_VISUALS_CHECK_BACKFILL
CHILD_SHORT_VISUALS_STALE
CHILD_SHORT_VISUALS_FINGERPRINT_BACKFILLED
STORYBOARD_DURATION_OUT_OF_TIER
STORYBOARD_DUPLICATE_HINT_RUN_COLLAPSED
STORYBOARD_HINT_EXPANDED
STORYBOARD_TOPUP_DUPLICATE_DROPPED
STORYBOARD_TOPUP_DUPLICATE_SUMMARY
STORYBOARD_FALLBACK_CLUSTER_THINNED
HINT_FUZZY_MATCH
NARRATION_POV_NORMALIZED
TTS_SECTION_DELIVERY_SELECTED
TTS_SECTION_DELIVERY_FALLBACK
RESUME_TRANSCRIPT_REUSED
AUDIO_SILENCE_COMPRESSED
AUDIO_DURATION_TRANSCRIPT_MISMATCH
PROPS_STALE_REBUILDING
PROPS_STALENESS_CHECK_FAILED
LUMINANCE_CHECK_UNAVAILABLE
AUDIO_LOUDNESS_NORMALIZED
AUDIO_SECTION_LOUDNESS_ARC_APPLIED
AUDIO_SECTION_BOUNDARIES_COMPUTED
VISUAL_TIMING_SECTION_ANCHORED
VISUAL_TIMING_WHOLE_VIDEO_STRETCH_FALLBACK
VOICE_GENDER_FALLBACK
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
- Storyboard beat-count overshoot is handled deterministically after each
  batch returns: if `len(beats) > target_beat_count + 2`, keep the earliest
  `target_beat_count + 2` beats, trim the tail, and log
  `STORYBOARD_BEAT_COUNT_OVERSHOOT_TRIMMED`. Do not regenerate or retry solely
  for overshoot.

---

## 27. Media Rules

### 27.1 Images

- Flux images must be stored locally.
- Downloaded image dimensions must match the render format aspect ratio: parent
  16:9, Shorts 9:16. A 4:3 generated image is a blocking media-validation
  failure, not a crop-at-render fallback.
- A generated image whose mean grayscale luminance falls below the
  near-black floor (24.0/255) gets one well-lit reroll, then falls back to
  neighbor-fill (roadmap Phase A1); a near-black image that still reaches
  the DB is a blocking `near_black_image` media-validation failure, not a
  frame Agent 5 renders as-is. See §11.4 `generate_all_beat_images` and
  `validate_visual_media_assets`.
- `media_url` must be local.
- HTTP image URLs must never reach Remotion.
- Text cards are removed (subtitles-only rendering, §11.6/Golden Rule 11):
- Storyboard timestamp fallback beats must keep `script_text=""` and set
  `script_text_source="empty_fallback_no_transcript_span"` plus
  `script_text_missing=true`; never copy `visual_intent` into `script_text`.
  `STORYBOARD_SCRIPT_TEXT_EMPTY_FALLBACK` logs the affected beat orders.
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
- Fail fast if the last section end or last caption end drifts from
  `duration_ms` by more than 2% (roadmap 2b / audit P0-2,
  `_assert_timeline_alignment` in §11A.4) — the props-building mirror of
  Agent 3's cross-timeline invariant (§10.3).

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
- Duration verification's expected value comes from ffprobe on the source
  audio file, never `AudioFile.duration_ms` (roadmap 2c / audit P2-5) —
  applies to both `main` and `short` formats (roadmap 2c / audit P2-6; Shorts
  are no longer exempt).
- Render verification is not AI quality verification; visual quality and
  creative continuity belong to Agent 4 validation and diagnostics.
- Do not publish unverified renders.

---

## 28. Standalone Short Episode Rules

This is the current shorts architecture.

Rules:

- **Binding duration rule (operator, 2026-07-16): a Short MUST never run
  under 61 seconds** — enforced by Agent 3's hard
  `_MIN_SHORT_AUDIO_DURATION_MS` gate, protected upstream by the 190-word
  script floor + single word-floor regeneration (§9.4). A Short SHOULD stay
  at or under ~90 seconds, but exceeding ~90 s is telemetry-only and never
  fails, trims, or regenerates anything.
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
CHILD_SHORT_PROMPT_INHERITANCE_STATS — child remap prompt-inheritance stats
                          (storyboard.py, remap_beats_for_short): inherited
                          parent prompts vs. new prompts per Short. Replaces
                          the retired CHILD_SHORT_REUSE_RATE /
                          CHILD_SHORT_REUSE_CLUSTERING / CHILD_SHORT_REUSE_STATS
                          reuse diagnostics (no physical reuse exists —
                          audit §3.1)
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
METADATA_AUTO_APPROVE_SECONDS
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
| Stale props | Mitigated (roadmap 2d / audit P0-3): `_props_are_stale()` rebuilds when the props file's duration/section-count/last-section-end no longer match the current DB beats. Heuristic, not a full content hash — a change that doesn't move any of those three proxies (e.g. `flux_prompt` text edited with identical timing) would not be caught |
| Visual quality | Mitigated: Python injects a byte-identical full blueprint descriptor into every matching person prompt, while `era_setting` locks period/place in each storyboard batch. Images are independently sampled, so the provider can still drift despite identical text; no image-analysis consistency validator exists. |
| Storyboard cost | Storyboard output tokens per beat can be high |
| Child short script quality | Short scripts must not validate with remaining MAJOR TTS issues |
| Legacy fields | Breakpoint/bookend fields still exist and could be misused |
| Concurrency | VideoSection delete/insert patterns need care under parallel runs |
| Deferral loops | Child video can defer forever if parent visual pass fails |
| Metadata auto-approve sweep | A channel with a persistently-`NEEDS_REVIEW` render (or any render that never reaches `RENDERED`) never reaches Agent 6 and therefore never auto-approves metadata — intentional by design (`pickup_rendered_content()` filters on `Content.status == "RENDERED"` only, never on `VideoRender` row existence, per roadmap Check 2 Finding 2.3), not a bug, but worth naming so a future audit doesn't mistake it for one |
| Thumbnail font-glyph-coverage heuristic | `thumbnail.py`'s `_font_covers_text()` is a bbox-equality probe against a Private-Use-Area codepoint (U+E000), not a real cmap membership check (no `fontTools` dependency exists in this codebase). It assumes the probe codepoint is itself undefined in the bundled font — true for the current Archivo Black text face, but not guaranteed for an icon/symbol font (e.g. Font Awesome) that deliberately assigns real glyphs into the Private Use Area. Any future per-channel font-choice phase (§8.1's still-open pattern, roadmap Open decisions) must re-verify this probe against whichever font it adds, not assume it transfers unchanged |

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
- Character consistency controls: blueprint-derived locked descriptors plus Dev routing for qualifying person beats; reference-image identity lock remains design-only
- Era-aware diversity: locked-era environment frequency is telemetry-only; vary motif/framing and use other when needed
- Local image integrity: near-black and baked-in letterbox frames are detected, repaired (reroll → neighbor fallback), and never content-blocking; structural media findings still fail the content
- Child remap phrase integrity: exact/resliced/non-verbatim telemetry; normalized matches are re-sliced from authoritative narration
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
