# Multi Audio Stream Test

This repository is a focused proof-of-concept for one problem: play a multi-audio Seedr video in a web browser and change audio tracks while the movie keeps playing.

## Architecture

Seedr direct file URL → FFmpeg demux/remux → HLS master playlist → HLS.js → HTML5 video

The server keeps the original video as a single rendition and publishes every detected audio stream as an HLS alternate audio rendition. The browser switches the selected audio rendition with HLS.js instead of replacing the video source.

This follows the track-selection model used by adaptive streaming players. Seedr documents HLS for its built-in streaming player, and HLS.js exposes nextAudioTrack for audio-rendition switching.

## Render Free

This is intentionally a test service. It runs one FFmpeg process and generates HLS files under /tmp. Render Free uses a small 0.1 CPU / 512 MB instance and an ephemeral filesystem, so this is not a production media server.

## Environment variables

- MEDIA_SOURCE_URL — Seedr direct file URL.
- HLS_SEGMENT_SECONDS — HLS segment length; default 4.
- MAX_AUDIO_TRACKS — maximum audio streams exposed; default 8.
- STARTUP_TIMEOUT — time allowed for FFmpeg to create the master playlist.

## Test

Open the deployed URL, wait for the manifest, start playback, and change Audio track. The video element is not replaced when the selection changes.
