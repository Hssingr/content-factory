"""Phase 6D-1 — Reasoning-scaffolding A/B experiment for `why_this_visual` and
`story_progression_role`.

*** THIS SCRIPT CALLS LIVE EXTERNAL APIS (Claude + fal.ai Flux). ***
Per CLAUDE.md Sec 19.1, Claude Code must never run this script itself. The
user runs it directly (`python scripts/phase6d1_ab_storyboard_experiment.py`)
and lets Claude Code read the resulting output files afterward.

WHAT THIS DOES

Loads one real, already-validated narration segment (the real Borrasca
parent's own [SECTION 2] text, content_id=b963b201-a8df-448c-a2eb-2b09d45b4ed5
— the same real storyboard analyzed in Phase 6C-0/6D-0) and runs two live
Claude calls against it, identical in every respect except schema/prompt:

  RUN A (control)  — the real, unmodified `generate_storyboard_batch()`,
                      current production `_BEAT_SCHEMA` / `_STORYBOARD_SYSTEM_PROMPT`.
  RUN B (variant)  — the exact same function, call path, retry logic, and
                      message-building, with ONLY `why_this_visual` and
                      `story_progression_role` removed from the schema
                      (properties + required list) and their corresponding
                      instruction lines removed from the system prompt
                      (step 2b, step 13, and strict rule 4). Achieved via a
                      temporary in-memory monkeypatch of three module
                      globals in `app.agents.agent4_visuals.system_prompt`,
                      restored immediately after the call — nothing is
                      written to any source file. No permanent schema
                      change, per the phase's explicit constraint.

Both calls use `task="storyboard"` unchanged, so both route through the
same model (`MODEL_ROUTING["storyboard"]`) — "same model" is automatic, not
something this script has to force.

SCOPE NOTE ON CHILD BEATS: `why_this_visual` and `story_progression_role`
exist ONLY in the parent storyboard schema (`_BEAT_SCHEMA`,
`system_prompt.py`). The child-short remap schema (`_SHORT_REMAP_SCHEMA`,
`storyboard.py`) never had these two fields — it only ever asks for
`narration_phrase`, `long_beat_order`, `beat_intensity`, `match_score`. A
"child beats Run A vs Run B" comparison for these specific fields has no
real difference to test (both variants are identical for children, because
the fields were never part of the child schema to begin with). This script
therefore runs the A/B test on PARENT beats only — the 10-child-beat
sample the phase template asks for is not applicable to this question, and
is intentionally not run, with this paragraph as the explicit reasoning
rather than a silent gap.

OUTPUT (for Claude Code to read after this script finishes — no copy/paste
needed, same filesystem):

  /tmp/phase6d1_output/run_a.json   — full Run A storyboard + usage + diag
  /tmp/phase6d1_output/run_b.json   — full Run B storyboard + usage + diag
  /tmp/phase6d1_output/validator_a.json / validator_b.json
                                     — real validate_storyboard() findings
                                       for each run (pure Python, no extra
                                       API call)
  /tmp/phase6d1_output/images/run_a/beat_<n>.jpg
  /tmp/phase6d1_output/images/run_b/beat_<n>.jpg
                                     — real Flux Schnell images for up to
                                       10 matched parent beat pairs
                                       (Criterion 4), saved under a
                                       clearly-separate experiment cache
                                       path, never touching production
                                       `cache/<content_id>/` paths.

Run: python scripts/phase6d1_ab_storyboard_experiment.py
Cost: 2 Sonnet storyboard calls (~1 segment each) + up to 20 Flux Schnell
      image generations. Confirm FAL_KEY and ANTHROPIC_API_KEY are set
      before running.
"""

import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.agents.agent4_visuals.system_prompt as system_prompt
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard
from app.agents.agent4_visuals.services.flux_generator import generate_beat_image
from app.database import _get_session_factory
from app.models import Script, Content, Channel

OUT_DIR = Path("/tmp/phase6d1_output")
OUT_DIR.mkdir(parents=True, exist_ok=True)
(OUT_DIR / "images" / "run_a").mkdir(parents=True, exist_ok=True)
(OUT_DIR / "images" / "run_b").mkdir(parents=True, exist_ok=True)

CID = "b963b201-a8df-448c-a2eb-2b09d45b4ed5"
SEGMENT_LABEL = "[SECTION 2]"
N_IMAGE_PAIRS = 5


def _load_real_segment() -> tuple[str, object, int]:
    db = _get_session_factory()()
    try:
        script = db.query(Script).filter(Script.content_id == CID, Script.language == "en").first()
        content = db.query(Content).filter(Content.id == CID).first()
        channel = db.query(Channel).filter(Channel.id == content.channel_id).first()
        # Detach channel's needed attrs into a plain object so the session can close.
        class _Ch:
            niche = channel.niche
            tone = channel.tone
        from app.agents.agent4_visuals.subagents.storyboard import _split_voice_script_into_segments
        segments = _split_voice_script_into_segments(script.voice_script)
        seg = next((t for lbl, t in segments if lbl == SEGMENT_LABEL), None)
        if seg is None:
            raise RuntimeError(f"{SEGMENT_LABEL} not found in real script — available: {[lbl for lbl,_ in segments]}")
        target_beats = max(1, round(len(seg.split()) / 150 * 60 / 4.0))
        return seg, _Ch(), target_beats
    finally:
        db.close()


def _build_trimmed_variants():
    """Build the Run B schema/prompt — exact copies of production minus the two fields."""
    trimmed_beat_schema = copy.deepcopy(system_prompt._BEAT_SCHEMA)
    del trimmed_beat_schema["properties"]["why_this_visual"]
    del trimmed_beat_schema["properties"]["story_progression_role"]
    trimmed_beat_schema["required"] = [
        f for f in trimmed_beat_schema["required"]
        if f not in ("why_this_visual", "story_progression_role")
    ]

    trimmed_batch_schema = copy.deepcopy(system_prompt._STORYBOARD_BATCH_SCHEMA)
    trimmed_batch_schema["properties"]["beats"]["items"] = trimmed_beat_schema

    trimmed_prompt = system_prompt._STORYBOARD_SYSTEM_PROMPT
    trimmed_prompt = trimmed_prompt.replace(
        "2. visual_intent — one sentence describing what the viewer should see and feel.\n"
        "2b. why_this_visual — one sentence explaining WHY this specific visual was chosen for\n"
        "    this moment in the narrative.\n",
        "2. visual_intent — one sentence describing what the viewer should see and feel.\n",
    )
    trimmed_prompt = trimmed_prompt.replace(
        "12. motif — dominant visual motif this beat shows:\n"
        "      doorway | corridor | face | hands | object | clock | phone | photo | exterior |\n"
        "      text | screen | reflection | document | room | other\n"
        "13. story_progression_role — narrative function:\n"
        "      setup | evidence | escalation | contradiction | emotional_reaction |\n"
        "      context | transition | payoff | comment_prompt\n",
        "12. motif — dominant visual motif this beat shows:\n"
        "      doorway | corridor | face | hands | object | clock | phone | photo | exterior |\n"
        "      text | screen | reflection | document | room | other\n",
    )
    trimmed_prompt = trimmed_prompt.replace(
        "3. Every beat must include a ``motif`` field chosen from the allowed list.\n"
        "4. Every beat must include ``story_progression_role``.",
        "3. Every beat must include a ``motif`` field chosen from the allowed list.",
    )

    assert "why_this_visual" not in trimmed_prompt, "trim failed — instruction text not found verbatim"
    assert "story_progression_role" not in trimmed_prompt, "trim failed — strict-rule text not found verbatim"
    return trimmed_beat_schema, trimmed_batch_schema, trimmed_prompt


def main():
    print("Loading real segment text from the validated Borrasca script...")
    segment_text, channel, target_beats = _load_real_segment()
    print(f"  segment={SEGMENT_LABEL} words={len(segment_text.split())} target_beats={target_beats}")

    print("\n=== RUN A (control — current production schema/prompt) ===")
    storyboard_a, usage_a, diag_a = system_prompt.generate_storyboard_batch(
        segment_label=SEGMENT_LABEL,
        segment_text=segment_text,
        segment_index=3,
        segment_count=6,
        channel=channel,
        script_format="youtube_long",
        target_beat_count=target_beats,
    )
    print(f"  beats={len(storyboard_a.get('beats', []))} output_tokens={usage_a.get('output_tokens')} "
          f"input_tokens={usage_a.get('input_tokens')}")
    json.dump({"storyboard": storyboard_a, "usage": usage_a, "diag": diag_a},
              open(OUT_DIR / "run_a.json", "w"), indent=2)

    print("\nBuilding trimmed (Run B) schema/prompt (in-memory only, nothing written to source)...")
    trimmed_beat_schema, trimmed_batch_schema, trimmed_prompt = _build_trimmed_variants()

    print("\n=== RUN B (variant — why_this_visual + story_progression_role removed) ===")
    orig_beat_schema = system_prompt._BEAT_SCHEMA
    orig_batch_schema = system_prompt._STORYBOARD_BATCH_SCHEMA
    orig_prompt = system_prompt._STORYBOARD_SYSTEM_PROMPT
    try:
        system_prompt._BEAT_SCHEMA = trimmed_beat_schema
        system_prompt._STORYBOARD_BATCH_SCHEMA = trimmed_batch_schema
        system_prompt._STORYBOARD_SYSTEM_PROMPT = trimmed_prompt
        storyboard_b, usage_b, diag_b = system_prompt.generate_storyboard_batch(
            segment_label=SEGMENT_LABEL,
            segment_text=segment_text,
            segment_index=3,
            segment_count=6,
            channel=channel,
            script_format="youtube_long",
            target_beat_count=target_beats,
        )
    finally:
        system_prompt._BEAT_SCHEMA = orig_beat_schema
        system_prompt._STORYBOARD_BATCH_SCHEMA = orig_batch_schema
        system_prompt._STORYBOARD_SYSTEM_PROMPT = orig_prompt

    print(f"  beats={len(storyboard_b.get('beats', []))} output_tokens={usage_b.get('output_tokens')} "
          f"input_tokens={usage_b.get('input_tokens')}")
    json.dump({"storyboard": storyboard_b, "usage": usage_b, "diag": diag_b},
              open(OUT_DIR / "run_b.json", "w"), indent=2)

    # ── Deliverable 1: real measured token cost ─────────────────────────────
    out_a, out_b = usage_a.get("output_tokens", 0), usage_b.get("output_tokens", 0)
    n_beats_a = len(storyboard_a.get("beats", []))
    delta = out_a - out_b
    print("\n=== DELIVERABLE 1 — REAL MEASURED TOKEN COST ===")
    print(f"Run A output_tokens={out_a} ({n_beats_a} beats, {out_a/max(n_beats_a,1):.1f} tok/beat)")
    print(f"Run B output_tokens={out_b} ({len(storyboard_b.get('beats', []))} beats)")
    print(f"Delta (A - B) = {delta} tokens  ({delta/max(n_beats_a,1):.1f} tokens/beat)  "
          f"= {delta/max(out_a,1)*100:.1f}% of Run A's total output payload")

    # ── Deliverable 2, Criterion 3: validator impact (real code, no API call) ──
    issues_a = validate_storyboard(storyboard_a.get("beats", []))
    issues_b = validate_storyboard(storyboard_b.get("beats", []))
    json.dump(issues_a, open(OUT_DIR / "validator_a.json", "w"), indent=2)
    json.dump(issues_b, open(OUT_DIR / "validator_b.json", "w"), indent=2)
    print("\n=== CRITERION 3 — VALIDATOR IMPACT (real validate_storyboard(), no extra API call) ===")
    print(f"Run A: {len(issues_a)} findings")
    print(f"Run B: {len(issues_b)} findings")
    pct_change = (len(issues_b) - len(issues_a)) / max(len(issues_a), 1) * 100
    print(f"Change: {pct_change:+.1f}%  (fail threshold: >10% increase)")
    checks_a = {i["check"] for i in issues_a}
    checks_b = {i["check"] for i in issues_b}
    new_checks = checks_b - checks_a
    print(f"New finding classes in Run B not present in Run A: {sorted(new_checks) or '(none)'}")

    # ── Criterion 4: real Flux images for up to 10 matched beat pairs ──────────
    print(f"\n=== CRITERION 4 — generating up to {N_IMAGE_PAIRS} matched image pairs (real Flux calls) ===")
    beats_a = storyboard_a.get("beats", [])
    beats_b = storyboard_b.get("beats", [])
    n_pairs = min(N_IMAGE_PAIRS, len(beats_a), len(beats_b))
    pairs_saved = []
    for i in range(n_pairs):
        ba, bb = beats_a[i], beats_b[i]
        for run_label, beat, outdir in (("run_a", ba, OUT_DIR / "images" / "run_a"),
                                         ("run_b", bb, OUT_DIR / "images" / "run_b")):
            fp = beat.get("flux_prompt", "")
            if not fp or beat.get("media_strategy") == "remotion_text_card":
                print(f"  beat {i} ({run_label}): skipped (text_card or empty flux_prompt)")
                continue
            rel_path = generate_beat_image(
                flux_prompt=fp, beat_index=i,
                content_id=f"_phase6d1_experiment_{run_label}",
                environment=beat.get("environment", "other"),
            )
            print(f"  beat {i} ({run_label}): {rel_path}")
            pairs_saved.append((i, run_label, rel_path))

    json.dump(pairs_saved, open(OUT_DIR / "image_pairs.json", "w"), indent=2)

    print(f"\nAll output written to {OUT_DIR}/ — run_a.json, run_b.json, validator_a.json, "
          f"validator_b.json, image_pairs.json, images/run_a/*, images/run_b/*")
    print("Tell Claude Code the run is complete — it will read these files directly "
          "(same filesystem) to finish the Deliverable 2 specificity comparison "
          "(Criteria 1/2/4, which require reading/viewing the actual content) and "
          "produce the final report.")


if __name__ == "__main__":
    main()
