import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { KaraokeChunk } from "../types";

interface Props {
  captions:     KaraokeChunk[];
  activeColor?: string; // default #FFD700
}

export const KaraokeSubtitles: React.FC<Props> = ({ captions, activeColor = "#FFD700" }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const currentMs = (frame / fps) * 1000;

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
