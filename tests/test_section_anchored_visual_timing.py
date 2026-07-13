"""Runtime proof for roadmap Phase B1 — per-language visual timing for
parent renders (the "décalage" fix).

Root cause (confirmed against the code, see the operator's plan notes): the
old whole-video proportional stretch (`_remap_beats_timing()`) assumed
narration pacing is uniform across a video's whole length. It isn't — a
real production run measured non-source-language visual/narration drift up
to ±20s. This phase captures each language's real per-section
([INTRO]/[SECTION N]/[OUTRO]) audio boundaries at TTS-generation time
(`tts.py`), persists them on `AudioFile.section_boundaries`, and uses them
to remap beats section-by-section instead of video-wide
(`visual_orchestrator.py`), bounding drift to within one section.

This file proves, with only paid external boundaries stubbed (no Claude, no
fal.ai, no real Cartesia/ElevenLabs network calls — CLAUDE.md §19.1):
  1. `tts._compute_section_boundaries()` — real ffmpeg-measured chunk
     durations, correctly rescaled and contiguous.
  2. `tts.generate_audio()`'s Cartesia path — real WAV generation + real
     ffmpeg encode/concat/normalize, with a fake `cartesia` SDK boundary —
     produces real, non-uniform per-section boundaries matching each
     section's actual (different) spoken duration.
  3. `visual_orchestrator._build_section_anchor_map()` — every fallback
     reason (missing data, count mismatch, structural mismatch, degenerate
     span) correctly refuses to guess a correspondence.
  4. `visual_orchestrator._remap_beats_timing()` — per-section anchoring
     produces a beat placement that tracks each section's own local
     stretch ratio, concretely different from (and more accurate than)
     what the old whole-video stretch would produce on the identical input.
  5. `visual_orchestrator._run_parent_visuals()` — the full field-set-in-A,
     read-in-C chain: AudioFile.section_boundaries (Agent 3) really flows
     into the beats persisted for a second language (Agent 4), using the
     same real in-memory VideoSection table pattern as
     test_stale_visuals_audio_fingerprint_and_props_rebuild.py.
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import tempfile
import unittest
import uuid
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.modules.setdefault("fal_client", SimpleNamespace(SyncClient=None, FalClientError=Exception))
sys.modules.setdefault("elevenlabs", SimpleNamespace(ElevenLabs=object))
sys.modules.setdefault("elevenlabs.types", SimpleNamespace(VoiceSettings=object))
sys.modules.setdefault("openai", SimpleNamespace(OpenAI=object))


class _FakeTTS:
    """Fake cartesia.Cartesia().tts — returns real WAV tones of a
    caller-configured, per-call duration so real per-section timing is
    genuinely proportional to real (different) content, not uniform."""

    def __init__(self, durations_sec: list[float]):
        self._durations_sec = durations_sec
        self.calls: list[dict] = []

    def bytes(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        seconds = self._durations_sec[index]
        sample_rate = 22_050
        frame_count = int(sample_rate * seconds)
        frames = bytearray()
        amplitude = 12_000
        freq = 300.0 + index * 40.0
        for i in range(frame_count):
            sample = int(amplitude * math.sin(2.0 * math.pi * freq * i / sample_rate))
            frames.extend(struct.pack("<h", sample))
        with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
            with wave.open(tmp.name, "wb") as wav_file:
                wav_file.setnchannels(1)
                wav_file.setsampwidth(2)
                wav_file.setframerate(sample_rate)
                wav_file.writeframes(bytes(frames))
            return Path(tmp.name).read_bytes()


class _FakeCartesiaClient:
    def __init__(self, durations_sec: list[float]):
        self.tts = _FakeTTS(durations_sec)

    def __call__(self, api_key=None):
        return self


def _install_fake_cartesia(durations_sec: list[float]):
    fake_client = _FakeCartesiaClient(durations_sec)
    sys.modules["cartesia"] = SimpleNamespace(Cartesia=fake_client)
    # A minimal cartesia.tts.TTS with a .bytes(**kwargs) signature — enough
    # for _cartesia_sdk_supports_generation_config()'s introspection to
    # succeed cleanly instead of hitting its ModuleNotFoundError fail-safe
    # (harmless either way for tts_model="sonic-2" below, but this keeps
    # the test output free of an unrelated expected-warning log line).
    fake_tts_module = SimpleNamespace(TTS=SimpleNamespace(bytes=lambda self, **kwargs: b""))
    sys.modules["cartesia.tts"] = fake_tts_module
    return fake_client


def _make_real_mp3_chunk(seconds: float, freq: float = 440.0) -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "chunk.mp3"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
                "-c:a", "libmp3lame", "-b:a", "128k", str(path),
            ],
            check=True, capture_output=True,
        )
        return path.read_bytes()


from app.agents.agent3_audio.services import tts
from app.agents.agent4_visuals.services import visual_orchestrator as vo
from app.models import VideoSection


# ── 1. tts._compute_section_boundaries() — real ffmpeg measurement ──────────

class ComputeSectionBoundariesTest(unittest.TestCase):
    def test_boundaries_are_contiguous_and_proportional_to_real_chunk_durations(self):
        chunks = [_make_real_mp3_chunk(0.30), _make_real_mp3_chunk(0.60), _make_real_mp3_chunk(0.20)]
        section_meta = [
            {"section_type": "intro", "section_index": None},
            {"section_type": "body", "section_index": 1},
            {"section_type": "outro", "section_index": None},
        ]
        concatenated = tts._concat_mp3_chunks(chunks)
        final_duration_ms = tts._measure_mp3_bytes_duration_ms(concatenated)

        boundaries = tts._compute_section_boundaries(section_meta, chunks, final_duration_ms)

        self.assertEqual(len(boundaries), 3)
        self.assertEqual(boundaries[0]["start_ms"], 0)
        self.assertEqual(boundaries[-1]["end_ms"], final_duration_ms)
        # Contiguous: no gaps, no overlaps.
        for prev, nxt in zip(boundaries, boundaries[1:]):
            self.assertEqual(prev["end_ms"], nxt["start_ms"])
        # Proportional: the 0.60s middle section must be roughly double the
        # 0.30s intro and triple the 0.20s outro (allowing real encoder slop).
        spans = [b["end_ms"] - b["start_ms"] for b in boundaries]
        self.assertGreater(spans[1], spans[0] * 1.5)
        self.assertGreater(spans[1], spans[2] * 2.0)
        # section_type/section_index carried through unchanged.
        self.assertEqual(
            [(b["section_type"], b["section_index"]) for b in boundaries],
            [("intro", None), ("body", 1), ("outro", None)],
        )

    def test_empty_or_mismatched_input_returns_no_boundaries(self):
        self.assertEqual(tts._compute_section_boundaries([], [], 1000), [])
        self.assertEqual(
            tts._compute_section_boundaries([{"section_type": "intro"}], [], 1000), [],
        )
        self.assertEqual(
            tts._compute_section_boundaries(
                [{"section_type": "intro"}], [b"a", b"b"], 1000,
            ),
            [],
        )

    def test_non_positive_final_duration_returns_no_boundaries(self):
        chunks = [_make_real_mp3_chunk(0.1)]
        section_meta = [{"section_type": "intro", "section_index": None}]
        self.assertEqual(tts._compute_section_boundaries(section_meta, chunks, 0), [])


# ── 2. generate_audio() Cartesia path — real end-to-end, fake SDK boundary ──

class GenerateAudioCartesiaSectionBoundariesTest(unittest.TestCase):
    def test_real_per_section_boundaries_reflect_real_unequal_section_durations(self):
        durations = [0.25, 0.70, 0.40, 0.15]  # INTRO, SECTION 1, SECTION 3, OUTRO
        fake_client = _install_fake_cartesia(durations)

        voice = SimpleNamespace(
            provider="cartesia", voice_id="voice-test", tts_model="sonic-2",
            emotion="dramatic", speed_profile="normal", speed_override=None,
            cartesia_pronunciation_dict_id=None,
        )
        script = "\n".join([
            "[INTRO]",
            "The recorder started on its own.",
            "[SECTION 1: Buildup]",
            "Then I found the missing photo inside the locked drawer.",
            "[SECTION 3: The reveal]",
            "But the voice on the tape was mine.",
            "[OUTRO]",
            "The house was silent after that.",
        ])

        audio_bytes, boundaries = tts.generate_audio(script, voice, is_short_episode=False)

        self.assertEqual(len(fake_client.tts.calls), 4)
        self.assertGreater(len(audio_bytes), 0)
        self.assertEqual(len(boundaries), 4)
        self.assertEqual(
            [(b["section_type"], b["section_index"]) for b in boundaries],
            [("intro", None), ("body", 1), ("body", 3), ("outro", None)],
        )
        self.assertEqual(boundaries[0]["start_ms"], 0)
        final_duration_ms = tts._measure_mp3_bytes_duration_ms(audio_bytes)
        self.assertEqual(boundaries[-1]["end_ms"], final_duration_ms)
        for prev, nxt in zip(boundaries, boundaries[1:]):
            self.assertEqual(prev["end_ms"], nxt["start_ms"])

        # The real proof this isn't a naive equal split: SECTION 1 (0.70s)
        # must be the longest span, OUTRO (0.15s) the shortest.
        spans = {b["section_index"] or b["section_type"]: b["end_ms"] - b["start_ms"] for b in boundaries}
        longest = max(spans.values())
        shortest = min(spans.values())
        self.assertEqual(longest, spans[1])
        self.assertEqual(shortest, spans["outro"])


# ── 3. _build_section_anchor_map() — every fallback reason ──────────────────

def _section(section_type, section_index, start_ms, end_ms) -> dict:
    return {
        "section_type": section_type, "section_index": section_index,
        "start_ms": start_ms, "end_ms": end_ms,
    }


class BuildSectionAnchorMapTest(unittest.TestCase):
    def test_missing_either_side_falls_back(self):
        source = [_section("intro", None, 0, 1000)]
        pairs, reason = vo._build_section_anchor_map(None, source)
        self.assertIsNone(pairs)
        self.assertEqual(reason, "missing_section_boundaries")
        pairs, reason = vo._build_section_anchor_map(source, [])
        self.assertIsNone(pairs)
        self.assertEqual(reason, "missing_section_boundaries")

    def test_count_mismatch_falls_back(self):
        source = [_section("intro", None, 0, 1000), _section("outro", None, 1000, 2000)]
        target = [_section("intro", None, 0, 1500)]
        pairs, reason = vo._build_section_anchor_map(source, target)
        self.assertIsNone(pairs)
        self.assertIn("section_count_mismatch", reason)

    def test_structural_mismatch_falls_back(self):
        source = [_section("intro", None, 0, 1000), _section("body", 1, 1000, 2000)]
        target = [_section("intro", None, 0, 1200), _section("body", 2, 1200, 2400)]
        pairs, reason = vo._build_section_anchor_map(source, target)
        self.assertIsNone(pairs)
        self.assertEqual(reason, "section_structure_mismatch")

    def test_degenerate_span_falls_back(self):
        source = [_section("intro", None, 0, 0), _section("outro", None, 0, 1000)]
        target = [_section("intro", None, 0, 500), _section("outro", None, 500, 1500)]
        pairs, reason = vo._build_section_anchor_map(source, target)
        self.assertIsNone(pairs)
        self.assertEqual(reason, "degenerate_section_span")

    def test_valid_matching_sections_pair_up_in_order(self):
        source = [_section("intro", None, 0, 1000), _section("outro", None, 1000, 2000)]
        target = [_section("intro", None, 0, 1500), _section("outro", None, 1500, 3000)]
        pairs, reason = vo._build_section_anchor_map(source, target)
        self.assertEqual(reason, "ok")
        self.assertEqual(len(pairs), 2)
        self.assertIs(pairs[0][0], source[0])
        self.assertIs(pairs[0][1], target[0])


# ── 4. _remap_beats_timing() — per-section anchoring vs. whole-video stretch ─

class RemapBeatsTimingPerSectionTest(unittest.TestCase):
    def setUp(self):
        # Source: two 10s sections, INTRO [0,10000) then OUTRO [10000,20000).
        self.source_sections = [
            _section("intro", None, 0, 10_000),
            _section("outro", None, 10_000, 20_000),
        ]
        # Target: INTRO stretches 2x (0-20000), OUTRO stays 1x (20000-30000)
        # — a deliberately UNEVEN per-section ratio, the exact scenario a
        # single whole-video ratio cannot represent correctly.
        self.target_sections = [
            _section("intro", None, 0, 20_000),
            _section("outro", None, 20_000, 30_000),
        ]
        # Beat 0 is anchored 500ms before the INTRO/OUTRO boundary; beat 1
        # trails it so beat 0 is never the *last* beat — the shared final
        # clamp (last beat's end == target_duration_ms exactly, matching
        # the pre-existing whole-video-stretch convention) would otherwise
        # override beat 0's own computed end and make this test meaningless.
        self.beats = [
            {"beat_order": 0, "audio_start_ms": 9_500, "audio_end_ms": 9_800},
            {"beat_order": 1, "audio_start_ms": 9_800, "audio_end_ms": 20_000},
        ]

    def test_per_section_anchoring_used_when_data_is_valid(self):
        result = vo._remap_beats_timing(
            self.beats, target_duration_ms=30_000, source_duration_ms=20_000,
            source_sections=self.source_sections, target_sections=self.target_sections,
        )
        # Beat 0 is inside the INTRO section (local ratio 2x): a beat at
        # 9500ms/9800ms, 500/200ms before the 10000ms section end, maps to
        # 19000/19600ms — 1000/400ms before the target INTRO's own 20000ms end.
        self.assertEqual(result[0]["audio_start_ms"], 19_000)
        self.assertEqual(result[0]["audio_end_ms"], 19_600)

    def test_per_section_result_differs_from_whole_video_stretch_on_same_input(self):
        per_section = vo._remap_beats_timing(
            self.beats, target_duration_ms=30_000, source_duration_ms=20_000,
            source_sections=self.source_sections, target_sections=self.target_sections,
        )
        whole_video = vo._remap_beats_timing_whole_video(
            self.beats, target_duration_ms=30_000, source_duration_ms=20_000,
        )
        # Whole-video: ratio = 30000/20000 = 1.5x uniformly →
        # 9500*1.5=14250, 9800*1.5=14700 — nowhere near the real INTRO/OUTRO
        # boundary (20000ms) the beat should sit just before.
        self.assertEqual(whole_video[0]["audio_start_ms"], 14_250)
        self.assertEqual(whole_video[0]["audio_end_ms"], 14_700)
        self.assertNotEqual(per_section[0]["audio_start_ms"], whole_video[0]["audio_start_ms"])
        # The per-section result stays within its own (correct) 10s-wide
        # target section; the whole-video result lands ~5.75s into the
        # wrong section entirely — the exact décalage this phase fixes.
        self.assertGreaterEqual(per_section[0]["audio_start_ms"], self.target_sections[0]["start_ms"])
        self.assertLess(per_section[0]["audio_start_ms"], self.target_sections[0]["end_ms"])

    def test_falls_back_to_whole_video_stretch_when_section_data_missing(self):
        with patch.object(vo.logger, "info") as mock_log:
            result = vo._remap_beats_timing(
                self.beats, target_duration_ms=30_000, source_duration_ms=20_000,
                source_sections=None, target_sections=None,
            )
        expected = vo._remap_beats_timing_whole_video(self.beats, 30_000, 20_000)
        self.assertEqual(result[0]["audio_start_ms"], expected[0]["audio_start_ms"])
        self.assertTrue(
            any("VISUAL_TIMING_WHOLE_VIDEO_STRETCH_FALLBACK" in str(c) for c in mock_log.call_args_list),
        )

    def test_last_beat_clamped_to_exact_target_duration_regardless_of_path(self):
        beats = [
            {"beat_order": 0, "audio_start_ms": 0, "audio_end_ms": 9_000},
            {"beat_order": 1, "audio_start_ms": 9_000, "audio_end_ms": 19_999},
        ]
        result = vo._remap_beats_timing(
            beats, target_duration_ms=30_000, source_duration_ms=20_000,
            source_sections=self.source_sections, target_sections=self.target_sections,
        )
        self.assertEqual(result[-1]["audio_end_ms"], 30_000)

    def test_same_duration_is_a_no_op_and_never_touches_section_data(self):
        result = vo._remap_beats_timing(
            self.beats, target_duration_ms=20_000, source_duration_ms=20_000,
            source_sections="not-a-real-list-would-crash-if-touched",
            target_sections="not-a-real-list-would-crash-if-touched",
        )
        self.assertEqual(result, self.beats)


# ── 5. _run_parent_visuals() — AudioFile.section_boundaries really flows ────
# through into the beats persisted for a second language (real in-memory
# VideoSection table, same pattern as
# test_stale_visuals_audio_fingerprint_and_props_rebuild.py).

class _FakeQuery:
    def __init__(self, table: list, predicate=None):
        self._table = table
        self._predicate = predicate or (lambda row: True)

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return [row for row in self._table if self._predicate(row)]

    def delete(self):
        matched = self.all()
        for row in matched:
            self._table.remove(row)
        return len(matched)


class _FakeDb:
    def __init__(self):
        self.video_sections: list[VideoSection] = []
        self.commit_count = 0

    def query(self, model):
        if model is VideoSection:
            return _FakeQuery(self.video_sections)
        return _FakeQuery([])

    def add(self, row):
        self.video_sections.append(row)

    def flush(self):
        pass

    def commit(self):
        self.commit_count += 1


class RunParentVisualsSectionAnchoringIntegrationTest(unittest.TestCase):
    def test_target_language_beats_use_per_section_anchoring_from_persisted_audio_files(self):
        content_id = uuid.uuid4()
        db = _FakeDb()
        content = SimpleNamespace(id=content_id, source_language="en", story_blueprint=None)
        channel = SimpleNamespace(id=uuid.uuid4(), niche="mystery", tone="tense")

        scripts_by_lang = {
            "en": SimpleNamespace(voice_script="Original narration text"),
            "fr": SimpleNamespace(voice_script="Texte de narration original"),
        }
        # EN: INTRO [0,10000) + OUTRO [10000,20000). FR: INTRO stretches 2x
        # to [0,20000), OUTRO stays [20000,30000) — an uneven per-section
        # ratio a whole-video stretch (1.5x uniform) cannot represent.
        en_sections = [
            {"section_type": "intro", "section_index": None, "start_ms": 0, "end_ms": 10_000},
            {"section_type": "outro", "section_index": None, "start_ms": 10_000, "end_ms": 20_000},
        ]
        fr_sections = [
            {"section_type": "intro", "section_index": None, "start_ms": 0, "end_ms": 20_000},
            {"section_type": "outro", "section_index": None, "start_ms": 20_000, "end_ms": 30_000},
        ]
        audio_by_lang = {
            "en": SimpleNamespace(duration_ms=20_000, whisper_transcript=[], section_boundaries=en_sections),
            "fr": SimpleNamespace(duration_ms=30_000, whisper_transcript=[], section_boundaries=fr_sections),
        }

        def _beat(order, start_ms, end_ms) -> dict:
            return {
                "beat_order": order, "section_order": order,
                "audio_start_ms": start_ms, "audio_end_ms": end_ms,
                "script_text": f"narration {order}", "visual_intent": f"intent {order}",
                "visual_type": "b-roll", "visual_category": "object", "environment": "indoor_office",
                "flux_prompt": "concrete subject, wide shot, photorealistic", "effect": "cut",
                "color_grade": "desaturated", "transition_to_next": "cut", "motif": "object",
                "beat_intensity": "medium", "suggested_duration_sec": (end_ms - start_ms) / 1000,
                "media_strategy": "flux_generated", "media_url": "cache/parent-id/abc.jpg",
                "media_type": "image",
            }

        # Beat 1 trails beat 0 so beat 0 is never the *last* beat — the
        # shared final clamp (last beat's end == target_duration_ms exactly)
        # would otherwise override beat 0's own computed end.
        fresh_beats = [_beat(0, 9_500, 9_800), _beat(1, 9_800, 20_000)]

        def fake_run_visual_pass(**kwargs):
            return fresh_beats, 20_000

        with patch(
            "app.agents.agent4_visuals.services.visual_orchestrator._run_visual_pass",
            side_effect=fake_run_visual_pass,
        ):
            result = vo._run_parent_visuals(
                content_id=content_id, content=content, scripts_by_lang=scripts_by_lang,
                audio_by_lang=audio_by_lang, channel=channel, script_format="youtube_long",
                allow_legacy_fallback=True, db=db,
            )

        self.assertEqual(result["status"], "PARENT_VISUALS_DONE")
        en_beats = result["beats_by_lang"]["en"]
        fr_beats = result["beats_by_lang"]["fr"]

        # EN is the source language — duration matches exactly, no-op remap.
        self.assertEqual(en_beats[0]["audio_start_ms"], 9_500)
        self.assertEqual(en_beats[0]["audio_end_ms"], 9_800)

        # FR must use the section-anchored 2x INTRO ratio (19000/19600), not
        # a whole-video 1.5x ratio (14250/14700) — proves AudioFile.section_boundaries
        # (set in Agent 3, on a completely different content/language row)
        # really reached this remap call for the "fr" language.
        self.assertEqual(fr_beats[0]["audio_start_ms"], 19_000)
        self.assertEqual(fr_beats[0]["audio_end_ms"], 19_600)


if __name__ == "__main__":
    unittest.main()
