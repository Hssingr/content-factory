"""Phase 6D-1B — post-implementation verification: one real storyboard generation
against the now-permanently-trimmed production schema (why_this_visual /
story_progression_role removed, STORYBOARD_SCHEMA_VERSION 6.1).

*** THIS SCRIPT CALLS THE LIVE CLAUDE API. ***
Per CLAUDE.md Sec 19.1, Claude Code must never run this script itself. Run it
directly and let Claude Code read the output afterward (same filesystem).

Uses the same real, validated [SECTION 2] segment from the Borrasca parent
script used in Phase 6C-0/6D-0/6D-1, so results are directly comparable to
the Phase 6D-1 Run A/Run B figures. No monkeypatching this time — this is a
plain call against the real, permanently-changed production
generate_storyboard_batch().

Run: python scripts/phase6d1b_verify_real_storyboard.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.agent4_visuals import system_prompt
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard
from app.database import _get_session_factory
from app.models import Script, Content, Channel

OUT_DIR = Path("/tmp/phase6d1b_output")
OUT_DIR.mkdir(parents=True, exist_ok=True)

CID = "b963b201-a8df-448c-a2eb-2b09d45b4ed5"
SEGMENT_LABEL = "[SECTION 2]"


def main():
    db = _get_session_factory()()
    try:
        script = db.query(Script).filter(Script.content_id == CID, Script.language == "en").first()
        content = db.query(Content).filter(Content.id == CID).first()
        channel = db.query(Channel).filter(Channel.id == content.channel_id).first()

        class _Ch:
            niche = channel.niche
            tone = channel.tone

        from app.agents.agent4_visuals.subagents.storyboard import _split_voice_script_into_segments
        segments = _split_voice_script_into_segments(script.voice_script)
        seg = next((t for lbl, t in segments if lbl == SEGMENT_LABEL), None)
        target_beats = max(1, round(len(seg.split()) / 150 * 60 / 4.0))
        ch = _Ch()
    finally:
        db.close()

    print(f"schema_version={system_prompt.STORYBOARD_SCHEMA_VERSION} "
          f"prompt_version={system_prompt.PROMPT_VERSION}")
    print(f"segment={SEGMENT_LABEL} words={len(seg.split())} target_beats={target_beats}")

    storyboard, usage, diag = system_prompt.generate_storyboard_batch(
        segment_label=SEGMENT_LABEL,
        segment_text=seg,
        segment_index=3,
        segment_count=6,
        channel=ch,
        script_format="youtube_long",
        target_beat_count=target_beats,
    )

    beats = storyboard.get("beats", [])
    print(f"beats={len(beats)} output_tokens={usage.get('output_tokens')} "
          f"input_tokens={usage.get('input_tokens')} was_truncated={diag['was_truncated']} "
          f"attempt_count={diag['attempt_count']}")

    has_dead_fields = any("why_this_visual" in b or "story_progression_role" in b for b in beats)
    print(f"any beat still contains why_this_visual/story_progression_role: {has_dead_fields}")

    issues = validate_storyboard(beats)
    print(f"validator findings: {len(issues)}")
    for i in issues:
        print(f"  [{i['severity']}] beat={i['beat_order']} check={i['check']}")

    json.dump({"storyboard": storyboard, "usage": usage, "diag": diag,
               "validator_issues": issues, "has_dead_fields": has_dead_fields},
              open(OUT_DIR / "result.json", "w"), indent=2)
    print(f"\nFull output written to {OUT_DIR}/result.json")
    print("Tell Claude Code the run is complete.")


if __name__ == "__main__":
    main()
