# Workspace

## Overview

pnpm workspace monorepo using TypeScript, plus a Python 3.10 CLI tool (ClipGen) for generating vertical short-form video clips from YouTube URLs.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands (TypeScript)

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

## ClipGen — Python YouTube Clip Generator

Generates vertical (9:16, 1080×1920) short clips from a YouTube URL using AI segment scoring.

### Usage

```bash
python main.py "<youtube_url>"
python main.py "<youtube_url>" --top 3 --output my_clips
```

### Project Structure

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point |
| `processor.py` | Pipeline orchestration |
| `transcript.py` | Transcript extraction + fallback |
| `ai_selector.py` | OpenRouter AI scoring |
| `video_editor.py` | FFmpeg + OpenCV processing |
| `utils.py` | Shared helpers |
| `requirements.txt` | Python dependencies |

### Dependencies

- **Python**: 3.10
- **System**: FFmpeg 7.1
- **pip**: pytube, youtube-transcript-api, opencv-python-headless, python-dotenv, requests, yt-dlp, numpy

### Secrets Required

- `OPENROUTER_API_KEY` — AI segment scoring via OpenRouter (qwen/qwen3-32b, fallback: deepseek/deepseek-chat)

### Pipeline Steps

1. Download video via yt-dlp (best quality MP4)
2. Fetch transcript (YouTube API → Whisper fallback)
3. Build 30–90s coherent segments
4. AI scores all segments; selects top N
5. Cut clips with FFmpeg (stream copy, no drift)
6. Face detection via OpenCV Haar cascade
7. Smart crop + scale to 1080×1920 H.264/AAC

### Output

Clips saved as `output/clip_01.mp4`, `clip_02.mp4`, …

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
