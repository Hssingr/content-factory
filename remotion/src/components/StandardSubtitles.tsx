import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { CaptionChunk } from "../types";
import { OverlaySuppressWindow } from "./MediaSection";

interface Props {
  captions: CaptionChunk[];
  /**
   * Phase 14.10b — windows during which a section's own TextOverlay/TextCard
   * is showing; the global subtitle layer must not render during them (the
   * design decision is "the section overlay wins" — see
   * code_report/phase_14_10_double_subtitle_investigation.md /
   * phase_14_10b_subtitle_overlay_collision_fix.md). Optional and defaults to
   * empty so this component's existing API/behavior is unchanged for any
   * caller that does not pass it.
   */
  suppressWindows?: OverlaySuppressWindow[];
}

export const StandardSubtitles: React.FC<Props> = ({ captions, suppressWindows = [] }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = (frame / fps) * 1000;

  const suppressed = suppressWindows.some(
    (w) => currentMs >= w.start_ms && currentMs < w.end_ms,
  );
  if (suppressed) return null;

  const active = captions.find(
    (c) => currentMs >= c.start_ms && currentMs < c.end_ms,
  );

  if (!active) return null;

  return (
    <AbsoluteFill style={containerStyle}>
      <div style={boxStyle}>
        <span style={textStyle}>{active.text}</span>
      </div>
    </AbsoluteFill>
  );
};

const containerStyle: React.CSSProperties = {
  justifyContent: "flex-end",
  alignItems:     "center",
  paddingBottom:  "6%",
};

const boxStyle: React.CSSProperties = {
  backgroundColor: "rgba(0, 0, 0, 0.65)",
  borderRadius:    8,
  padding:         "10px 20px",
  maxWidth:        "80%",
  textAlign:       "center",
};

const textStyle: React.CSSProperties = {
  color:       "#ffffff",
  fontSize:    48,
  fontFamily:  "Arial, Helvetica, sans-serif",
  fontWeight:  "bold",
  lineHeight:  1.3,
  textShadow:  "2px 2px 4px rgba(0,0,0,0.8)",
  letterSpacing: 0.5,
};
