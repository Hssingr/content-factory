import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  ClipData,
  ColorGrade,
  Effect,
  OverlayPosition,
  SectionData,
  Transition,
} from "../types";

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
const GRADE_FILTER: Record<ColorGrade, string> = {
  desaturated:   "saturate(30%) brightness(85%)",
  cold_blue:     "saturate(70%) hue-rotate(200deg) brightness(80%)",
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
  url === "__dark_fallback__" ||
  url === "__runway_pending__" ||
  url === "__generated_pending__";

// ── Overlay text placement ─────────────────────────────────────────────────────

const OVERLAY_POSITION_STYLE: Record<OverlayPosition, React.CSSProperties> = {
  center:      { top: "50%", left: "50%", transform: "translate(-50%, -50%)", textAlign: "center" },
  lower_third: { bottom: "12%", left: "8%", right: "8%", textAlign: "left" },
  top_left:    { top: "8%", left: "8%", textAlign: "left" },
  top_right:   { top: "8%", right: "8%", textAlign: "right" },
  none:        { display: "none" },
};

interface TextOverlayProps {
  text:     string;
  position: OverlayPosition;
}

const TextOverlay: React.FC<TextOverlayProps> = ({ text, position }) => {
  if (!text || position === "none") return null;

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{ position: "absolute", maxWidth: "84%", ...OVERLAY_POSITION_STYLE[position] }}>
        <span
          style={{
            color:       "#ffffff",
            fontSize:    56,
            fontFamily:  "Arial, Helvetica, sans-serif",
            fontWeight:  "bold",
            lineHeight:  1.25,
            textShadow:  "2px 2px 6px rgba(0,0,0,0.85)",
          }}
        >
          {text}
        </span>
      </div>
    </AbsoluteFill>
  );
};

// Placeholder rendered for generated_visual beats until AI-image generation lands
const GeneratedPlaceholder: React.FC = () => (
  <AbsoluteFill style={{ ...DARK_FALLBACK_STYLE, display: "flex", alignItems: "center", justifyContent: "center" }}>
    <span
      style={{
        color:         "rgba(255,255,255,0.25)",
        fontSize:      28,
        fontFamily:    "Arial, Helvetica, sans-serif",
        letterSpacing: 4,
        textTransform: "uppercase",
      }}
    >
      Generated visual
    </span>
  </AbsoluteFill>
);

export const MediaSection: React.FC<Props> = ({ section, crossfadeIn = 0, incomingTransition }) => {
  const { fps } = useVideoConfig();
  const frame   = useCurrentFrame();

  // Section duration derived from audio timings — NOT from composition durationInFrames,
  // which would be wrong here because useVideoConfig returns the root composition config.
  const sectionDurMs     = section.audio_end_ms - section.audio_start_ms;
  const sectionDurFrames = Math.max(1, Math.round((sectionDurMs / 1000) * fps));

  const transitionStyle = getTransitionStyle(incomingTransition, frame, crossfadeIn);
  const overlayPosition = section.overlay_position ?? "none";

  // text_overlay beats need no stock media — Storyboard Agent decided to render
  // the narration as on-screen text over a dark background.
  if (section.visual_type === "text_overlay") {
    return (
      <AbsoluteFill style={{ ...DARK_FALLBACK_STYLE, ...transitionStyle }}>
        <TextOverlay text={section.overlay_text ?? ""} position={overlayPosition === "none" ? "center" : overlayPosition} />
      </AbsoluteFill>
    );
  }

  // Resolve clips — fall back to legacy single-media fields when clips array is absent
  const clips: ClipData[] =
    section.clips?.length
      ? section.clips
      : [{ url: section.media_url, thumb: section.media_thumb, type: section.media_type }];

  const validClips = clips.filter((c) => !isFallback(c.url));

  // generated_visual beats (or any beat whose media fetch never produced a usable
  // clip) render a neutral placeholder rather than a plain dark fallback.
  if (section.visual_type === "generated_visual" || validClips.length === 0) {
    return (
      <AbsoluteFill style={transitionStyle}>
        <GeneratedPlaceholder />
        <TextOverlay text={section.overlay_text ?? ""} position={overlayPosition} />
      </AbsoluteFill>
    );
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
      <TextOverlay text={section.overlay_text ?? ""} position={overlayPosition} />
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
        <OffthreadVideo src={clip.url} style={mediaStyle} muted />
      ) : (
        <Img src={clip.url} style={mediaStyle} />
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
 */
export function transitionDurationFrames(transition?: Transition): number {
  switch (transition) {
    case "crossfade":    return 15;  // 0.5 s — classic dissolve
    case "dip_to_black": return 20;  // 0.67 s — fades through black
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
    case "dip_to_black":
      return {
        opacity: t,
        filter:  `brightness(${interpolate(t, [0, 1], [0, 100])}%)`,
      };

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
