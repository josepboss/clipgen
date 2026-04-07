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

**Web UI (recommended):**
```bash
python main.py
# → open http://localhost:5000
```

**CLI:**
```bash
python main.py "<youtube_url>"
python main.py "<youtube_url>" --top 3 --output my_clips
```

### Project Structure

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point |
| `processor.py` | Pipeline orchestration |
| `downloader.py` | pytube download → yt-dlp fallback, saves as `input.mp4` |
| `transcript.py` | Transcript extraction + Whisper fallback |
| `ai_selector.py` | OpenRouter AI scoring |
| `video_editor.py` | FFmpeg cut + OpenCV face tracking + vertical encode |
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

---

## TikTok Publisher Module

Playwright-based TikTok auto-publisher with multi-user session isolation.

### Structure

| Path | Purpose |
|------|---------|
| `src/publisher/browser.js` | Playwright Chromium factory; loads/saves user sessions |
| `src/publisher/platforms/tiktok.js` | TikTok upload automation (video + caption + public) |
| `src/publisher/session-login.js` | One-time login script per user (HEADLESS=false) |
| `src/publisher/scheduler.js` | Optional standalone Node.js queue processor |
| `publisher_db.py` | SQLite queue (publisher.db) — CRUD helpers |
| `publisher_routes.py` | Flask Blueprint for `/publisher/*` API endpoints |
| `publisher_scheduler.py` | Python background scheduler thread |

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/publisher/session/:userId` | Check if session exists |
| `POST` | `/publisher/login/:userId` | Trigger browser login (HEADLESS=false only) |
| `POST` | `/publisher/queue/:userId` | Add video to publish queue |
| `GET` | `/publisher/queue/:userId` | Get queue status |
| `DELETE` | `/publisher/queue/:userId/:itemId` | Remove a queue item |
| `POST` | `/publisher/publish/:userId` | Manually trigger next publish |

### Session Setup (per user)

```bash
# On a machine with a display (not headless):
HEADLESS=false node src/publisher/session-login.js <userId>
# Log in when Chrome opens → session saved to sessions/<userId>/tiktok.json
# Copy that JSON file to the server's sessions/ directory
```

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLISH_INTERVAL_MINUTES` | `30` | How often scheduler polls queue |
| `SESSION_DIR` | `./sessions` | Path to user session storage |
| `HEADLESS` | `true` | Set `false` for login debugging |
| `LOGIN_TIMEOUT_SEC` | `120` | How long to wait for manual login |

### Queue Item Statuses

`pending` → `running` → `done` / `failed` / `session_expired`

When `session_expired`, the user must re-run `session-login.js` to refresh.

### Security Notes

- Sessions stored at `sessions/{userId}/tiktok.json` — **never committed** (gitignored)
- No passwords stored anywhere — only browser storage state (cookies + localStorage)
- Each user's session is fully isolated

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
