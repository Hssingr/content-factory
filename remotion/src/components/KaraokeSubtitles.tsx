import React from "react";
import { AbsoluteFill, useCurrentFrame, useVideoConfig } from "remotion";
import { KaraokeChunk } from "../types";

// Subtitles-only rendering (audit G-0/G-8): this is the video's ONLY text
// layer — the per-section overlay layers and the suppress-window machinery
// that coordinated with them are removed. Subtitles render unconditionally.
interface Props {
  captions:     KaraokeChunk[];
  activeColor?: string; // default #FFD700
}

export const KaraokeSubtitles: React.FC<Props> = ({
  captions, activeColor = "#FFD700",
}) => {
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
  // 39.8% of the 1920px-tall Short canvas — raised ~150px above the prior
  // 32% (operator video-output audit, roadmap Phase B2) to clear TikTok/
  // Reels/Shorts platform UI (caption/description, like/share rail,
  // progress bar) that occupies the bottom of the frame.
  paddingBottom:  "39.8%",
  paddingLeft:    48,
  paddingRight:   48,
};

const boxStyle: React.CSSProperties = {
  backgroundColor: "rgba(0, 0, 0, 0.76)",
  borderRadius:    8,
  padding:         "14px 28px",
  maxWidth:        "92%",
  textAlign:       "center",
  display:         "flex",
  flexWrap:        "wrap",
  justifyContent:  "center",
  lineHeight:      1.08,
};

const wordStyle: React.CSSProperties = {
  fontFamily:    "Arial Black, Arial, Helvetica, sans-serif",
  fontSize:      82,
  fontWeight:    900,
  textShadow:    "0 4px 12px rgba(0,0,0,0.95)",
  transition:    "color 0.05s ease, transform 0.05s ease",
  letterSpacing: 0,
};
