import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";

interface TextCardProps {
  /** Overlay text to display — typically the beat's overlay_text field. */
  text:           string;
  /** CSS transition/opacity style injected by MediaSection for crossfades. */
  style?:         React.CSSProperties;
  /** Accent colour — defaults to the karaoke gold used elsewhere in the pipeline. */
  accentColor?:   string;
}

/**
 * Fallback composition element for beats that have no usable stock media.
 *
 * Renders a dark gradient background with the beat's overlay_text centred on
 * screen.  A subtle slow-zoom effect keeps the frame from feeling static.
 * Wired in MediaSection when section.visual_type === "text_card".
 */
export const TextCard: React.FC<TextCardProps> = ({
  text,
  style = {},
  accentColor = "#FFD700",
}) => {
  const frame    = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  // Subtle slow zoom: 1.0 → 1.05 over the full section duration
  const progress  = durationInFrames > 1 ? frame / (durationInFrames - 1) : 0;
  const scale     = interpolate(progress, [0, 1], [1.0, 1.05]);

  // Fade in quickly at the start (first 8 frames ≈ 0.27 s)
  const opacity   = interpolate(frame, [0, 8], [0, 1], { extrapolateRight: "clamp" });

  return (
    <AbsoluteFill
      style={{
        background: "radial-gradient(ellipse at center, #1a1a2e 0%, #0a0a0f 70%)",
        display:       "flex",
        alignItems:    "center",
        justifyContent: "center",
        transform:     `scale(${scale})`,
        overflow:      "hidden",
        ...style,
      }}
    >
      {text ? (
        <div
          style={{
            opacity,
            padding:    "0 80px",
            textAlign:  "center",
            maxWidth:   "90%",
          }}
        >
          {/* Accent bar */}
          <div
            style={{
              width:           64,
              height:          4,
              backgroundColor: accentColor,
              margin:          "0 auto 28px",
              borderRadius:    2,
            }}
          />
          <span
            style={{
              color:       "#ffffff",
              fontSize:    60,
              fontFamily:  "Arial, Helvetica, sans-serif",
              fontWeight:  "bold",
              lineHeight:  1.3,
              textShadow:  "2px 2px 8px rgba(0,0,0,0.9)",
            }}
          >
            {text}
          </span>
        </div>
      ) : (
        // Empty text — render pure dark background (invisible beat, not an error)
        <div />
      )}
    </AbsoluteFill>
  );
};
