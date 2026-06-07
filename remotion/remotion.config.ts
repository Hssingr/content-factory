import { Config } from "@remotion/cli/config";

Config.setVideoImageFormat("jpeg");
Config.setJpegQuality(85);
Config.setConcurrency(2);

// Serve local media files (audio) through the static server
// The renderer passes absolute paths which Chromium reads as file:// URIs
Config.setChromiumOpenGlRenderer("angle");
