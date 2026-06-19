// ── Section ───────────────────────────────────────────────────────────────────

export type Effect     = "slow_zoom" | "fade_in" | "cut" | "pan" | "zoom_out"
                       | "push_in" | "shake" | "parallax";
export type ColorGrade = "desaturated" | "cold_blue" | "warm_amber" | "dark_contrast" | "neutral";
export type MediaType  = "image" | "video";

// Storyboard-beat enums — present when the Storyboard Agent designed this section
export type Transition = "cut" | "crossfade" | "dip_to_black" | "whip_pan"
                       | "zoom_blur" | "match_cut" | "none";
export type OverlayPosition = "center" | "lower_third" | "top_left" | "top_right" | "none";
export type VisualType = "b-roll" | "action" | "text_overlay" | "document"
                       | "map" | "screenshot" | "generated_visual" | "text_card";
export type TextCardStyle = "chat" | "document" | "statistic" | "quote" | "default";

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
  overlay_text?:       string;
  overlay_position?:   OverlayPosition;
  text_card_style?:    TextCardStyle;
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

export interface VideoConfig {
  style:       string;
  color_grade: string;
}

// Index signature required by Remotion's CalculateMetadataFunction constraint
interface RemotionProps { [key: string]: unknown }

export interface MainVideoProps extends RemotionProps {
  content_id:  string;
  language:    string;
  audio_file:  string;
  duration_ms: number;
  sections:    SectionData[];
  subtitles:   StandardSubtitles;
  config:      VideoConfig;
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
  config:       VideoConfig;
}
