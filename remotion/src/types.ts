// ── Section ───────────────────────────────────────────────────────────────────

export type Effect     = "slow_zoom" | "fade_in" | "cut" | "pan" | "zoom_out"
                       | "push_in" | "shake" | "parallax";
export type ColorGrade = "desaturated" | "cold_blue" | "warm_amber" | "dark_contrast" | "neutral";
export type MediaType  = "image" | "video";

// Storyboard-beat enums — present when the Storyboard Agent designed this section
//
// "dip_to_black" is retired (roadmap Phase A2) — Claude's storyboard schema no
// longer offers it, so no freshly generated beat carries it. Kept in this union
// only so a VideoSection row persisted before this fix (whose stored
// generation_prompt JSON still has the literal string) still type-checks;
// MediaSection.tsx treats it identically to "crossfade" at render time.
export type Transition = "cut" | "crossfade" | "dip_to_black" | "whip_pan"
                       | "zoom_blur" | "match_cut" | "none";
// Subtitles-only rendering (audit G-0/G-8): the retired OverlayPosition /
// TextCardStyle types and the "text_card" visual_type are removed — Remotion
// draws no text except the subtitle track.
export type VisualType = "b-roll" | "action" | "text_overlay" | "document"
                       | "map" | "screenshot" | "generated_visual";

export interface ClipData {
  url:   string;
  thumb: string;
  type:  MediaType;
}

export interface SectionData {
  order:          number;
  clips:          ClipData[];   // 1-3 clips — cycled with crossfades within the section
  media_url:      string;       // legacy: first clip url (backward compat)
  media_thumb:    string;
  media_type:     MediaType;
  effect:         Effect;
  color_grade:    ColorGrade;
  audio_start_ms: number;
  audio_end_ms:   number;
  // Storyboard-beat fields — populated by the Storyboard Agent flow; legacy
  // sections carry neutral defaults ("", "b-roll", "cut", "none").
  visual_intent?:      string;
  visual_type?:        VisualType;
  transition_to_next?: Transition;
}

// ── Subtitles ─────────────────────────────────────────────────────────────────

export interface CaptionChunk {
  text:     string;
  start_ms: number;
  end_ms:   number;
}

export interface KaraokeWord {
  w: string; // word
  s: number; // start_ms
  e: number; // end_ms
}

export interface KaraokeChunk {
  words:        KaraokeWord[];
  start_ms:     number;
  end_ms:       number;
  active_color: string;
}

export interface StandardSubtitles {
  style:    "standard";
  captions: CaptionChunk[];
}

export interface KaraokeSubtitles {
  style:    "karaoke";
  captions: KaraokeChunk[];
}

// ── Composition props ─────────────────────────────────────────────────────────

// Index signature required by Remotion's CalculateMetadataFunction constraint
interface RemotionProps { [key: string]: unknown }

export interface MainVideoProps extends RemotionProps {
  content_id:  string;
  language:    string;
  audio_file:  string;
  duration_ms: number;
  sections:    SectionData[];
  subtitles:   StandardSubtitles;
  // Chunked rendering only (renderer.py's render_main_video_chunked): each
  // chunk is its own independent Remotion composition, so this chunk's own
  // first section has no local sections[idx - 1] to read an incoming
  // transition off of. leading_transition carries the real
  // transition_to_next of the section that immediately precedes this
  // chunk (the last section of the previous chunk) so it isn't silently
  // discarded as a hard cut. Absent/undefined for a non-chunked render and
  // for the very first chunk, where there is genuinely nothing before it.
  leading_transition?: Transition;
}

export interface ShortProps extends RemotionProps {
  content_id:   string;
  language:     string;
  audio_file:   string;
  short_index:  number;
  start_ms:     number;
  end_ms:       number;
  duration_ms:  number;
  sections:     SectionData[];
  subtitles:    KaraokeSubtitles;
  part_label:   string;
  total_parts:  number;
}
