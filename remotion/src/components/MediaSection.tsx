import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  ClipData,
  ColorGrade,
  Effect,
  SectionData,
  Transition,
} from "../types";

// Subtitles-only rendering (audit G-0/G-8): this component draws IMAGES only.
// The retired TextCard/TextOverlay layers (and the suppress-window machinery
// that coordinated them with the global subtitle components) are removed —
// the subtitle track, rendered at the composition level, is the video's only
// text layer. A section with no usable media renders a plain dark fill with
// no placeholder text of any kind.

interface Props {
  section:      SectionData;
  /** Frames to spend fading in from the previous section (0 for the very first). */
  crossfadeIn?: number;
  /** The previous section's transition_to_next — drives the entrance style/timing. */
  incomingTransition?: Transition;
}

// Frames for crossfade between clips within a section
const CLIP_CROSSFADE = 12;   // 0.4 s at 30 fps

// CSS filter strings per color grade
//
// cold_blue (Phase 14.11 fix): was `hue-rotate(200deg)`, a near-180-degree
// rotation of the ENTIRE hue wheel — not a subtle cool nudge. CSS hue-rotate
// shifts every hue by the given angle, so 200deg pushes skin tones (~25-35deg,
// orange) into the blue family and green foliage (~90-130deg) into
// violet/magenta — the confirmed root cause of the reported "unnatural
// purple/blue tint" defect (code_report/phase_14_11_color_grade_cast_investigation.md).
const GRADE_FILTER: Record<ColorGrade, string> = {
  desaturated:   "saturate(30%) brightness(85%)",
  cold_blue:     "saturate(70%) hue-rotate(15deg) brightness(80%)",
  warm_amber:    "saturate(120%) sepia(40%) brightness(105%)",
  dark_contrast: "contrast(140%) brightness(65%) saturate(75%)",
  neutral:       "none",
};

const DARK_FALLBACK_STYLE: React.CSSProperties = {
  width:      "100%",
  height:     "100%",
  background: "radial-gradient(ellipse at center, #1a1a2e 0%, #0a0a0f 70%)",
};

const isFallback = (url: string) =>
  !url ||
  url === "__text_card__";

export const MediaSection: React.FC<Props> = ({ section, crossfadeIn = 0, incomingTransition }) => {
  const { fps } = useVideoConfig();
  const frame   = useCurrentFrame();

  // Section duration derived from audio timings — NOT from composition durationInFrames,
  // which would be wrong here because useVideoConfig returns the root composition config.
  const sectionDurMs     = section.audio_end_ms - section.audio_start_ms;
  const sectionDurFrames = Math.max(1, Math.round((sectionDurMs / 1000) * fps));

  const transitionStyle = getTransitionStyle(incomingTransition, frame, crossfadeIn);

  // Resolve clips — fall back to legacy single-media fields when clips array is absent
  const clips: ClipData[] =
    section.clips?.length
      ? section.clips
      : [{ url: section.media_url, thumb: section.media_thumb, type: section.media_type }];

  const validClips = clips.filter((c) => !isFallback(c.url));

  // No usable local media — a plain dark fill, with NO placeholder text
  // (subtitles-only rendering). This state should be rare: Agent 4 fills
  // hard-failed beats from neighbouring images, and Agent 5's missing-media
  // blocker stops renders where too many beats have no media.
  if (validClips.length === 0) {
    return <AbsoluteFill style={{ ...DARK_FALLBACK_STYLE, ...transitionStyle }} />;
  }

  // Each clip gets an equal share of the section, with an overlap for crossfade
  const clipShareFrames = Math.ceil(sectionDurFrames / validClips.length);

  return (
    <AbsoluteFill style={{ overflow: "hidden", ...transitionStyle }}>
      {validClips.map((clip, i) => {
        // Clip starts CLIP_CROSSFADE frames early (except the first)
        const clipFrom = Math.max(0, i * clipShareFrames - (i > 0 ? CLIP_CROSSFADE : 0));
        const clipEnd  =
          i === validClips.length - 1
            ? sectionDurFrames
            : (i + 1) * clipShareFrames;
        const clipDur = Math.max(1, clipEnd - clipFrom);

        return (
          <Sequence key={`${section.order}-${i}`} from={clipFrom} durationInFrames={clipDur}>
            <SingleClip
              clip={clip}
              effect={section.effect}
              colorGrade={section.color_grade}
              crossfadeIn={i > 0 ? CLIP_CROSSFADE : 0}
              durationFrames={clipDur}
            />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

// ── Single clip renderer ───────────────────────────────────────────────────────

interface ClipProps {
  clip:          ClipData;
  effect:        Effect;
  colorGrade:    ColorGrade;
  crossfadeIn:   number;   // frames to fade in from 0 → 1
  durationFrames: number;  // full duration of this clip sequence
}

const SingleClip: React.FC<ClipProps> = ({
  clip,
  effect,
  colorGrade,
  crossfadeIn,
  durationFrames,
}) => {
  const frame    = useCurrentFrame();
  const progress = durationFrames > 1 ? frame / (durationFrames - 1) : 0;

  const opacity = crossfadeIn > 0
    ? interpolate(frame, [0, crossfadeIn], [0, 1], { extrapolateRight: "clamp" })
    : 1;

  const mediaStyle: React.CSSProperties = {
    width:     "100%",
    height:    "100%",
    objectFit: "cover",
    filter:    GRADE_FILTER[colorGrade] ?? "none",
    transform: getEffectTransform(effect, progress, frame),
    opacity,
  };

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {clip.type === "video" ? (
        <OffthreadVideo src={staticFile(clip.url)} style={mediaStyle} muted />
      ) : (
        <Img src={staticFile(clip.url)} style={mediaStyle} />
      )}
    </AbsoluteFill>
  );
};

// ── Effect transform ───────────────────────────────────────────────────────────

function getEffectTransform(effect: Effect, progress: number, frame: number): string {
  switch (effect) {
    case "slow_zoom":
      return `scale(${interpolate(progress, [0, 1], [1.0, 1.08])})`;

    case "zoom_out":
      return `scale(${interpolate(progress, [0, 1], [1.08, 1.0])})`;

    case "pan":
      return `translateX(${interpolate(progress, [0, 1], [0, -5])}%) scale(1.06)`;

    case "push_in":
      return `scale(${interpolate(progress, [0, 1], [1.0, 1.18])})`;

    case "parallax":
      return `scale(${interpolate(progress, [0, 1], [1.05, 1.14])}) translateX(${interpolate(progress, [0, 1], [-3, 3])}%)`;

    case "shake": {
      // Small per-frame jitter — sine/cosine at different rates avoid a circular pattern
      const jitterX = Math.sin(frame * 1.4) * 1.2;
      const jitterY = Math.cos(frame * 1.7) * 1.0;
      return `translate(${jitterX}px, ${jitterY}px) scale(1.04)`;
    }

    case "fade_in":
    case "cut":
    default:
      return "scale(1.0)";
  }
}

// ── Transition timing & style ──────────────────────────────────────────────────

/**
 * How many frames a section should overlap with the previous one for a given
 * transition_to_next value. "cut"/"none" read as a hard cut (no overlap).
 *
 * "dip_to_black" is retired (roadmap Phase A2, live-canary fix): a full fade
 * through black reads as a rendering failure while narration continues (a
 * real production run showed ~13 of these per parent video, ~0.67s of full
 * black each). Claude's storyboard prompt/schema no longer produce it, but a
 * VideoSection row persisted by an older run may still carry the literal
 * string in its stored `generation_prompt` JSON (`load_video_sections()`
 * merges that JSON verbatim, unlike the other legacy fields it normalizes) —
 * so it is kept here only as a case that maps onto the same crossfade
 * treatment, never its own through-black effect.
 */
export function transitionDurationFrames(transition?: Transition): number {
  switch (transition) {
    case "crossfade":
    case "dip_to_black": return 15;  // 0.5 s — classic dissolve (dip_to_black: legacy alias)
    case "whip_pan":     return 10;  // 0.33 s — fast directional blur
    case "zoom_blur":    return 12;  // 0.4 s — punch-in blur
    case "match_cut":    return 6;   // 0.2 s — near-instant, slight overlap
    case "cut":
    case "none":
    default:             return 0;
  }
}

/** Per-frame entrance style for the given incoming transition. */
function getTransitionStyle(
  transition: Transition | undefined,
  frame: number,
  crossfadeIn: number,
): React.CSSProperties {
  if (crossfadeIn <= 0 || frame >= crossfadeIn) {
    return { opacity: 1 };
  }
  const t = frame / crossfadeIn; // 0 → 1 across the transition window

  switch (transition) {
    case "whip_pan":
      return {
        opacity:   t,
        transform: `translateX(${interpolate(t, [0, 1], [25, 0])}%)`,
        filter:    `blur(${interpolate(t, [0, 1], [16, 0])}px)`,
      };

    case "zoom_blur":
      return {
        opacity:   t,
        transform: `scale(${interpolate(t, [0, 1], [1.25, 1])})`,
        filter:    `blur(${interpolate(t, [0, 1], [10, 0])}px)`,
      };

    case "match_cut":
    case "crossfade":
    default:
      return { opacity: t };
  }
}
