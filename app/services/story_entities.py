"""Deterministic, no-AI-call extraction of a story's recurring proper nouns.

Pure/generic like the rest of app/services/ (CLAUDE.md §7) — no agent-owned
business logic lives here; it only reads a story blueprint dict and returns a
list of strings. Extracted out of
``app.agents.agent4_visuals.subagents.storyboard.build_continuity_line_from_blueprint()``
(roadmap Phase B2) so Agent 5's caption punctuation-restoration pass
(``app.agents.agent5_render.services.subtitles``) can reuse the identical
extraction — recurring character/place names Whisper is most likely to
mishear are exactly the names a viewer most needs spelled correctly — without
importing Agent 4 business logic across the CLAUDE.md §6A ownership boundary.
Same shared-utility precedent as ``app.services.video_sections`` (§7.5) and
``app.services.script_source`` (§7.6).
"""

import re

_ENTITY_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")
_STOPWORDS = frozenset({
    "The", "This", "That", "These", "Those", "There", "Then", "But", "And",
    "For", "Not", "Now", "When", "What", "Why", "How", "Where", "Who",
    "Would", "Could", "Should", "Every", "Some", "Someone", "Something",
    "Years", "Each", "Once", "After", "Before", "Until", "While", "Even",
    "Still", "Just", "Only", "Never", "Always", "Maybe", "Perhaps", "Their",
    "They", "She", "His", "Her", "Him", "Its", "Instead", "Inside", "Outside",
})
_MIN_REPEATS  = 2
_MAX_ENTITIES = 5

_BLUEPRINT_TEXT_FIELDS = ("hook", "final_payoff", "midpoint_retention_trap", "central_question")


def extract_recurring_proper_nouns(blueprint: dict | None) -> list[str]:
    """Return capitalized words recurring at least twice across a story
    blueprint's own text fields (``hook``/``final_payoff``/
    ``midpoint_retention_trap``/``central_question``/``major_turns``),
    ordered by descending frequency then alphabetically, capped at 5.

    English-only heuristic (capitalization-based) — same accepted tradeoff as
    every other deterministic keyword/pattern check in this codebase (see
    CLAUDE.md §11.4's storyboard-validator checks). Returns ``[]`` when
    ``blueprint`` is falsy or nothing recurs — never invents a finding.
    """
    if not blueprint:
        return []
    text_fields = [
        str(blueprint.get(field) or "") for field in _BLUEPRINT_TEXT_FIELDS
    ] + [str(turn) for turn in (blueprint.get("major_turns") or [])]
    combined = " ".join(field for field in text_fields if field)
    if not combined:
        return []

    counts: dict[str, int] = {}
    for word in _ENTITY_RE.findall(combined):
        if word in _STOPWORDS:
            continue
        counts[word] = counts.get(word, 0) + 1

    return sorted(
        (word for word, count in counts.items() if count >= _MIN_REPEATS),
        key=lambda word: (-counts[word], word),
    )[:_MAX_ENTITIES]
