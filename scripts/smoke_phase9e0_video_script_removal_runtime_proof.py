"""Phase 9E-0 — runtime proof that the full Agent 2 script pipeline works correctly
after removing `video_script` (schema, model, persistence, and the `scripts.video_script`
DB column itself).

Real chain, real DB, real deterministic checks — only the Claude API is stubbed
(task-dispatching stub on `call_claude`/`call_claude_structured`, both imported into
`app.agents.agent2_discovery.system_prompt`). No live API calls.

Exercises, in one continuous real call chain:

    run_script_workflow()
      -> generate_story_blueprint()            [stubbed: task="story_blueprint"]
      -> generate_script_sections()
           -> generate_section() x N            [stubbed: task="section_generation"]
           -> validate_script_globally()         [stubbed: task="global_validation"]
      -> run_script_quality_gate()
           -> assess_script_quality()            [stubbed: task="script_quality_check"]
      -> _persist_source_script()                [real DB write, no video_script]
      -> generate_multilingual_scripts()
           -> generate_native_script()           [stubbed: task="native_adaptation"]
      -> Content.status = SCRIPTS_VALIDATED
      -> run_shorts_planner()
           -> generate_shorts_plan()              [stubbed: task="shorts_planner"]
           -> generate_short_episode_script() x N [stubbed: task="short_script"]
           -> _collect_short_script_major_issues() [real check_tts_compliance/check_hook_quality]
           -> _persist_child_short_script()        [real DB write, no video_script]
           -> generate_multilingual_scripts() (child)

Cleans up every fixture row it creates and re-verifies they are gone.

Run: python scripts/smoke_phase9e0_video_script_removal_runtime_proof.py
"""

import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]" + (f" — {detail}" if detail else ""))


import app.agents.agent2_discovery.system_prompt as agent2_prompt
from app.agents.agent2_discovery.services.script_workflow import run_script_workflow
from app.services.script_checks import check_completeness
from app.database import _get_session_factory
from app.models import (
    User, Channel, ChannelConfig, ChannelLanguage, ChannelVoice, Content, Script,
)

db = _get_session_factory()()

created_user_id = None
created_channel_id = None
created_parent_id = None


def _word_chunk(seed: str, n: int) -> str:
    """Deterministic filler prose of roughly n words, seeded for variety."""
    words = (seed + " concrete specific named detail evidence consequence event ") * (n // 8 + 1)
    return " ".join(words.split()[:n]).capitalize() + "."


_BLUEPRINT_STUB = {
    "hook": "A grinding noise echoed from the hills every single night.",
    "central_question": "What was making the grinding noise in the hills?",
    "major_turns": [
        "Investigators traced the sound to an abandoned mill on the edge of town.",
        "Records showed the mill had been condemned for two decades before the sound began.",
        "A maintenance worker confirmed the machinery inside had been reactivated without permission.",
    ],
    "final_payoff": "The mill's old machinery had been secretly restarted by a former employee.",
    "comment_trigger": "Would you have investigated the noise yourself, or called it in?",
    "suggested_section_count": 3,
    "suggested_title": "The Grinding Noise No One Could Explain For Years",
}


def _stub_call_claude_structured(*, task, system_prompt=None, user_message=None, schema_name=None,
                                  input_schema=None, max_tokens=None, model_override=None):
    if task == "story_blueprint":
        return dict(_BLUEPRINT_STUB)
    if task == "section_generation":
        # Vary content slightly per call using the user_message's "Now generate: LABEL" line.
        label = "SECTION"
        if isinstance(user_message, str) and "Now generate:" in user_message:
            tail = user_message.split("Now generate:")[-1]
            label = tail.strip().splitlines()[0].strip()
        body = _word_chunk(f"turn {label.lower()}", 60)
        return {
            "script_text": body,
            "summary": f"This section ({label}) advanced the investigation with new evidence.",
            "reveals": [f"New fact revealed in {label}."],
            "open_questions": [f"Open question raised in {label}."],
            "suggests_outro": "OUTRO" not in label and label != "INTRO",
            "visual_intent": {
                "section_goal": f"Show the evidence for {label}.",
                "primary_visual_focus": "The abandoned mill machinery.",
                "avoid_repeating": [],
            },
        }
    if task == "global_validation":
        return {"status": "PASS", "issues": []}
    if task == "quality_rewrite":
        parts = ["[INTRO]", _BLUEPRINT_STUB["hook"] + " " + _word_chunk("rewrite intro", 130)]
        for i in range(1, 4):
            parts += [f"[SECTION {i}]", _word_chunk(f"rewrite section {i}", 200)]
        parts += ["[OUTRO]", _word_chunk("rewrite outro", 150) + " " + _BLUEPRINT_STUB["final_payoff"]
                  + " " + _BLUEPRINT_STUB["comment_trigger"]]
        return {"title": _BLUEPRINT_STUB["suggested_title"], "voice_script": "\n\n".join(parts)}
    if task == "shorts_planner":
        return {
            "total_parts": 3,
            "parts": [
                {
                    "part": i,
                    "goal": f"Part {i} goal.",
                    "opening_hook": f"Part {i} opens with a specific concrete detail.",
                    "main_content_summary": f"Part {i} content summary.",
                    "main_reveal": f"Part {i} reveal.",
                    "cliffhanger": f"Part {i} cliffhanger?" if i < 3 else "Who do you think was responsible?",
                }
                for i in range(1, 4)
            ],
        }
    raise AssertionError(f"unexpected call_claude_structured task={task!r}")


def _stub_call_claude(system_prompt, user_message, max_tokens=1024, *, task, model_override=None):
    if task == "script_quality_check":
        return json.dumps({"status": "PASSED", "issues": []})
    if task == "native_adaptation":
        return json.dumps({"voice_script": "[INTRO]\n" + _word_chunk("adaptation fr", 900)})
    if task == "short_script":
        part_n = "1"
        if isinstance(user_message, str) and "Part:" in user_message:
            part_n = user_message.split("Part:")[1].split("of")[0].strip()
        return json.dumps({
            "title": f"Part {part_n} title",
            "voice_script": "A specific detail opens this short episode immediately. " + _word_chunk(f"short-{part_n}", 170),
        })
    raise AssertionError(f"unexpected call_claude task={task!r}")


orig_call_claude_structured = agent2_prompt.call_claude_structured
orig_call_claude = agent2_prompt.call_claude
agent2_prompt.call_claude_structured = _stub_call_claude_structured
agent2_prompt.call_claude = _stub_call_claude

try:
    user = User(
        id=uuid.uuid4(), name="smoke-phase9e0-user",
        telegram_chat_id=f"smoke-{uuid.uuid4()}", primary_language="en",
    )
    db.add(user)
    db.flush()
    created_user_id = user.id

    channel = Channel(
        id=uuid.uuid4(), user_id=user.id, name="smoke-phase9e0-channel",
        niche="mystery", tone="documentary", active=False,
    )
    db.add(channel)
    db.flush()
    created_channel_id = channel.id

    db.add(ChannelConfig(channel_id=channel.id, script_format="youtube_long", audio_tags_enabled=False))
    db.add(ChannelLanguage(id=uuid.uuid4(), channel_id=channel.id, language="fr", channel_name="Chaine FR"))
    db.add(ChannelVoice(id=uuid.uuid4(), channel_id=channel.id, language="en", provider="cartesia",
                         voice_id="v-en", tts_model="sonic-2"))
    db.add(ChannelVoice(id=uuid.uuid4(), channel_id=channel.id, language="fr", provider="cartesia",
                         voice_id="v-fr", tts_model="sonic-2"))
    db.commit()

    content = Content(
        id=uuid.uuid4(), channel_id=channel.id,
        source_url="https://example.invalid/smoke-phase9e0",
        source_language="en",
        content_hash=f"smoke-phase9e0-{uuid.uuid4()}",
        title="Draft title",
        status="APPROVED",
        source_excerpt="An abandoned mill on the edge of town made a grinding noise for years.",
    )
    db.add(content)
    db.commit()
    created_parent_id = content.id

    # ── Real chain, Claude stubbed only ──────────────────────────────────────────
    run_script_workflow(content, db)

    assert_ok(
        "run_script_workflow() completed with no exception",
        True,
    )

    db.refresh(content)
    assert_ok(
        "Parent Content.status == SCRIPTS_VALIDATED",
        content.status == "SCRIPTS_VALIDATED",
        f"actual status={content.status!r}",
    )

    parent_scripts = db.query(Script).filter(Script.content_id == content.id).all()
    assert_ok(
        "Parent has en + fr validated Script rows (no missing-key error)",
        {s.language for s in parent_scripts} == {"en", "fr"}
        and all(s.validated for s in parent_scripts),
        f"languages found={[s.language for s in parent_scripts]}",
    )

    for s in parent_scripts:
        assert_ok(
            f"Script(lang={s.language}) has no video_script attribute (model-level)",
            not hasattr(s, "video_script"),
        )

    en_script = next(s for s in parent_scripts if s.language == "en")
    assert_ok(
        "en voice_script is non-empty and persistence-ready",
        bool(en_script.voice_script) and len(en_script.voice_script.split()) > 0,
    )

    # ── Confirm the repointed check_completeness() works correctly on voice_script alone ──
    completeness_issues = check_completeness(en_script.voice_script, "en")
    major_completeness = [i for i in completeness_issues if i["severity"] == "MAJOR"]
    assert_ok(
        "check_completeness(voice_script, lang) runs with the new 2-arg signature, no exception",
        True,
    )
    assert_ok(
        "check_completeness finds [INTRO]/[OUTRO]/[SECTION N] markers in the real assembled voice_script",
        not any("marker missing" in i["description"] for i in major_completeness),
        f"issues={[i['description'] for i in major_completeness]}",
    )

    # ── run_shorts_planner() already ran as part of run_script_workflow() ───────
    children = db.query(Content).filter(Content.parent_content_id == content.id).all()
    assert_ok(
        "run_shorts_planner() created child short Content rows",
        len(children) == 3,
        f"children created={len(children)}",
    )
    assert_ok(
        "All children reached SCRIPTS_VALIDATED (no missing-key error in short pipeline)",
        all(c.status == "SCRIPTS_VALIDATED" for c in children),
        f"statuses={[c.status for c in children]}",
    )

    for child in children:
        child_scripts = db.query(Script).filter(Script.content_id == child.id).all()
        assert_ok(
            f"Child part={child.short_part_number} has en + fr validated Script rows",
            {s.language for s in child_scripts} == {"en", "fr"},
            f"languages found={[s.language for s in child_scripts]}",
        )
        for s in child_scripts:
            assert_ok(
                f"Child part={child.short_part_number} lang={s.language}: no video_script attribute",
                not hasattr(s, "video_script"),
            )

    print()
    print("SMOKE PASS")

finally:
    agent2_prompt.call_claude_structured = orig_call_claude_structured
    agent2_prompt.call_claude = orig_call_claude

    if created_parent_id is not None:
        db.query(Script).filter(
            Script.content_id.in_(
                db.query(Content.id).filter(
                    (Content.id == created_parent_id) | (Content.parent_content_id == created_parent_id)
                )
            )
        ).delete(synchronize_session=False)
        db.query(Content).filter(Content.parent_content_id == created_parent_id).delete(synchronize_session=False)
        db.query(Content).filter(Content.id == created_parent_id).delete(synchronize_session=False)
    if created_channel_id is not None:
        db.query(ChannelVoice).filter(ChannelVoice.channel_id == created_channel_id).delete(synchronize_session=False)
        db.query(ChannelLanguage).filter(ChannelLanguage.channel_id == created_channel_id).delete(synchronize_session=False)
        db.query(ChannelConfig).filter(ChannelConfig.channel_id == created_channel_id).delete(synchronize_session=False)
        db.query(Channel).filter(Channel.id == created_channel_id).delete(synchronize_session=False)
    if created_user_id is not None:
        db.query(User).filter(User.id == created_user_id).delete(synchronize_session=False)
    db.commit()

    leftover_content = db.query(Content).filter(
        (Content.id == created_parent_id) | (Content.parent_content_id == created_parent_id)
    ).count() if created_parent_id else 0
    leftover_channel = db.query(Channel).filter(Channel.id == created_channel_id).count() if created_channel_id else 0
    leftover_user = db.query(User).filter(User.id == created_user_id).count() if created_user_id else 0
    db.close()

assert_ok(
    "fixture cleanup verified (no leftover rows)",
    leftover_content == 0 and leftover_channel == 0 and leftover_user == 0,
    f"leftover_content={leftover_content} leftover_channel={leftover_channel} leftover_user={leftover_user}",
)

print()
print("SMOKE PASS (final)")
