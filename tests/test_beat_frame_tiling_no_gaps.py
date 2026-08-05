"""Task 2: shared ms boundaries must produce gap-free Remotion frame ranges."""

from app.agents.agent5_render.services.remotion_builder import (
    _sections_for_remotion,
    _tile_section_frame_ranges,
)


def test_awkward_millisecond_boundaries_tile_without_gaps_or_nominal_overlaps():
    sections = [
        {"section_order": 0, "audio_start_ms": 59_680, "audio_end_ms": 64_125},
        {"section_order": 1, "audio_start_ms": 64_125, "audio_end_ms": 68_620},
        {"section_order": 2, "audio_start_ms": 68_620, "audio_end_ms": 73_007},
    ]
    ranges = _tile_section_frame_ranges(
        sections, timeline_start_ms=59_680, duration_ms=13_327,
    )

    assert ranges[0][0] == 0
    assert all(left[1] == right[0] for left, right in zip(ranges, ranges[1:]))
    assert all(end > start for start, end in ranges)
    assert ranges[-1][1] == 400  # ceil(13.327 * 30)


def test_props_carry_the_same_shared_boundary_for_both_adjacent_beats():
    sections = [
        {"section_order": 10, "audio_start_ms": 0, "audio_end_ms": 4_445},
        {"section_order": 11, "audio_start_ms": 4_445, "audio_end_ms": 8_940},
    ]
    rendered = _sections_for_remotion(
        sections, timeline_start_ms=0, duration_ms=8_940,
    )
    assert rendered[0]["render_end_frame"] == rendered[1]["render_start_frame"]
    assert rendered[-1]["render_end_frame"] == 269

