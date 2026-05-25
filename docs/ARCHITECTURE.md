# Architecture

## Overview

`trilium-study` is a single-user FastAPI application that turns a configured Trilium parent note into a course and each direct child note into a lesson unit. Each lesson is processed through a durable staged pipeline:

1. `collect`
2. `normalize`
3. `script`
4. `flashcards`
5. `audio`
6. `video`
7. `upload`

The application persists state in SQLite and stores staged artifacts on disk under `.state/workspace` so expensive work can be reused after restarts or partial failures.

## Main Components

- `app/main.py`
  Server-rendered FastAPI UI, JSON endpoints, polling status APIs, flashcard study queue stats, browse/reset study actions, server-side audio-stream queue actions for uploaded YouTube lessons, app bootstrap, and background runner startup.
- `app/content.py`
  Trilium ETAPI client plus recursive lesson collection and normalized text assembly, including HTML cleanup and duplicate-block suppression so downstream script generation sees cleaner study material.
- `app/jobs.py`
  Single-worker durable job runner for course sync and per-lesson pipeline execution, including queued-state handling, duplicate-job protection, failed-stage resumability, and bounded automatic retries for lesson jobs.
- `app/status.py`
  Runtime dependency checks, queue position calculation, stage progress modeling, ETA helpers, and generated-script length metadata for the UI.
- `app/pipeline.py`
  LLM-backed script validation and expansion with minimum narration-length gates based on cleaned source text, including a looser middle-band floor so study-worthy 8-11 minute scripts are not rejected unnecessarily, comprehension-first prompting that pushes for examples and alternate explanations when needed, multi-attempt in-stage script retries that escalate to section-by-section teaching requirements before the lesson job fails, flashcard generation, Kokoro TTS integration, ffmpeg rendering sized to narration duration, YouTube upload integration, and SM-2 review scheduling.
- `app/models.py`
  SQLAlchemy schema for courses, lessons, artifacts, jobs, job events, uploads, flashcards, and reviews.
- `app/migrate.py` and `migrations/`
  Alembic-backed schema migration system used for bootstrap and deployment.
- `app/kokoro_cli.py`
  Repo-native CLI wrapper for Kokoro WAV synthesis when an explicit command path is desired.
- `app/youtube_auth.py`
  One-time OAuth bootstrap that generates the persisted YouTube token file.
- `deploy.sh` and `scripts/install-prod.sh`
  Workstation-to-mini-pc deployment path that renders a production `.env`, copies SQLite/runtime state, reinstalls dependencies, runs migrations, and restarts the user service.

## Persistence Model

SQLite stores:

- course and lesson metadata
- lesson stage state and errors
- job execution history
- upload metadata
- flashcards and review history
- flashcard scheduling state that can be reset without deleting historical review rows
- Alembic also tracks schema version state via `alembic_version`.

Disk artifacts store:

- raw note snapshots
- normalized lesson text
- generated scripts
- script provenance including source/script word counts and estimated narration length
- flashcard JSON
- audio files
- video files

## External Dependencies

- Trilium ETAPI for note traversal
- OpenAI Responses API for script and flashcard generation
- Kokoro for local TTS generation
- spaCy English model `en_core_web_sm` for Kokoro's English G2P path via `misaki`
- `ffmpeg` for MP4 rendering
- YouTube Data API v3 for unlisted uploads
- optional local YouTube audio-stream queue API reachable from the same host

## Runtime Expectations

- `ffmpeg` must be installed on the host.
- `espeak-ng` must be installed when using Kokoro for English fallback and phoneme support.
- Python dependencies should be installed with `uv sync --extra dev --extra tts --extra youtube`.
- Production installs must preinstall `en_core_web_sm` before the service handles English Kokoro synthesis so the runtime path never depends on spaCy auto-download behavior inside systemd.
- `.env` must contain valid Trilium, OpenAI, and YouTube settings.
- Network binding is controlled by `APP_HOST` and `APP_PORT` in `.env`, which are consumed by the systemd unit in production.
