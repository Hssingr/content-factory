import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { CaptionChunk } from "../types";

interface Props {
  captions: CaptionChunk[];
}

export const StandardSubtitles: React.FC<Props> = ({ captions }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = (frame / fps) * 1000;

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
