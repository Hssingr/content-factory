import React from "react";
import { Composition } from "remotion";
import { MainVideo, mainVideoCalculateMetadata } from "./compositions/MainVideo";
import { Short, shortCalculateMetadata } from "./compositions/Short";
import { MainVideoProps, ShortProps } from "./types";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/*
        MainVideo — 16:9 landscape (1920×1080)
        Duration and fps are computed from the inputProps via calculateMetadata.
        The placeholder durationInFrames here is overridden at render time.
      */}
      <Composition
        id="MainVideo"
        component={MainVideo as React.FC<MainVideoProps>}
        calculateMetadata={mainVideoCalculateMetadata}
        width={1920}
        height={1080}
        fps={30}
        durationInFrames={1}
        defaultProps={
          {
            content_id:  "",
            language:    "en",
            audio_file:  "",
            duration_ms: 0,
            sections:    [],
            subtitles:   { style: "standard", captions: [] },
            config:      { style: "documentary", color_grade: "desaturated" },
          } satisfies MainVideoProps
        }
      />

      {/*
        Short — 9:16 vertical (1080×1920)
        Duration computed from props.duration_ms via calculateMetadata.
      */}
      <Composition
        id="Short"
        component={Short as React.FC<ShortProps>}
        calculateMetadata={shortCalculateMetadata}
        width={1080}
        height={1920}
        fps={30}
        durationInFrames={1}
        defaultProps={
          {
            content_id:    "",
            language:      "en",
            audio_file:    "",
            short_index:   0,
            start_ms:      0,
            end_ms:        0,
            duration_ms:   0,
            sections:      [],
            subtitles:     { style: "karaoke", captions: [] },
            part_label:    "",
            total_parts:   1,
            hook_modified: true,
            config:        { style: "documentary", color_grade: "desaturated" },
          } satisfies ShortProps
        }
      />
    </>
  );
};
