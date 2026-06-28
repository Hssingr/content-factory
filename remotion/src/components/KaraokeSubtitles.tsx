import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { KaraokeChunk } from "../types";
import { OverlaySuppressWindow } from "./MediaSection";

interface Props {
  captions:     KaraokeChunk[];
  activeColor?: string; // default #FFD700
  /**
   * Phase 14.10b — windows during which a section's own TextOverlay/TextCard
   * is showing; the global subtitle layer must not render during them (the
   * design decision is "the section overlay wins" — see
   * code_report/phase_14_10_double_subtitle_investigation.md /
   * phase_14_10b_subtitle_overlay_collision_fix.md). Optional and defaults to
   * empty so this component's existing API/behavior is unchanged for any
   * caller that does not pass it. Callers (Short.tsx) pass these already
   * shifted into Short-local ms, the same coordinate space as `captions`.
   */
  suppressWindows?: OverlaySuppressWindow[];
}

export const KaraokeSubtitles: React.FC<Props> = ({
  captions, activeColor = "#FFD700", suppressWindows = [],
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = (frame / fps) * 1000;

  const suppressed = suppressWindows.some(
    (w) => currentMs >= w.start_ms && currentMs < w.end_ms,
  );
  if (suppressed) return null;

  const activeChunk = captions.find(
    (c) => currentMs >= c.start_ms && currentMs < c.end_ms,
  );

  if (!activeChunk) return null;

  const chunkActiveColor = activeChunk.active_color || activeColor;

  return (
    <AbsoluteFill style={containerStyle}>
      <div style={boxStyle}>
        {activeChunk.words.map((word, idx) => {
          const isActive = currentMs >= word.s && currentMs < word.e;
          return (
            <span
              key={idx}
              style={{
                ...wordStyle,
                color:     isActive ? chunkActiveColor : "#ffffff",
                transform: isActive ? "scale(1.1)" : "scale(1)",
                display:   "inline-block",
                marginRight: "0.25em",
              }}
            >
              {word.w}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};

const containerStyle: React.CSSProperties = {
  justifyContent: "flex-end",
  alignItems:     "center",
  paddingBottom:  "8%",
};

const boxStyle: React.CSSProperties = {
  backgroundColor: "rgba(0, 0, 0, 0.70)",
  borderRadius:    10,
  padding:         "12px 24px",
  maxWidth:        "85%",
  textAlign:       "center",
  display:         "flex",
  flexWrap:        "wrap",
  justifyContent:  "center",
  lineHeight:      1.4,
};

const wordStyle: React.CSSProperties = {
  fontFamily:    "Arial, Helvetica, sans-serif",
  fontSize:      52,
  fontWeight:    "bold",
  textShadow:    "2px 2px 6px rgba(0,0,0,0.9)",
  transition:    "color 0.05s ease, transform 0.05s ease",
  letterSpacing: 0.5,
};
