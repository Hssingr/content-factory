"""Agent 6 — thumbnails, titles & descriptions.

Roadmap: code_report/agent6_metadata_roadmap.md. Runs after Agent 5 renders a
content item (Content.status == "RENDERED") and produces, per content:
thumbnail (parent long-form only, platform="youtube" only — see
CLAUDE.md-style Check 4 in the roadmap), titles/descriptions/hashtags per
configured platform and language. Metadata sits in
METADATA_PENDING_APPROVAL until either an operator approves it or
settings.metadata_auto_approve_seconds elapses, then reaches
METADATA_APPROVED — the clean handoff point for Agent 7 (publishing, not yet
built).

Implemented through Phase E: base-image + per-language thumbnail overlay
(Phases C/D), platform metadata generation and deterministic limit
enforcement (Phase D), and the RENDERED -> GENERATING_METADATA ->
METADATA_PENDING_APPROVAL -> METADATA_APPROVED status machine, scheduler
wiring, and manual approve/edit API surface (Phase E). See the roadmap's
Execution Phases section for the per-phase implementation record.
"""
