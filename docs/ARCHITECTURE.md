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
  Server-rendered FastAPI UI, JSON endpoints, polling status APIs, flashcard study queue stats, browse/reset study actions, app bootstrap, and background runner startup.
- `app/content.py`
  Trilium ETAPI client plus recursive lesson collection and normalized text assembly.
- `app/jobs.py`
  Single-worker durable job runner for course sync and per-lesson pipeline execution, including queued-state handling, duplicate-job protection, failed-stage resumability, and bounded automatic retries for lesson jobs.
- `app/status.py`
  Runtime dependency checks, queue position calculation, stage progress modeling, and ETA helpers for the UI.
- `app/pipeline.py`
  LLM-backed script validation and expansion, flashcard generation, Kokoro TTS integration, ffmpeg rendering sized to narration duration, YouTube upload integration, and SM-2 review scheduling.
- `app/models.py`
  SQLAlchemy schema for courses, lessons, artifacts, jobs, job events, uploads, flashcards, and reviews.
- `app/migrate.py` and `migrations/`
  Alembic-backed schema migration system used for bootstrap and deployment.
- `app/kokoro_cli.py`
  Repo-native CLI wrapper for Kokoro WAV synthesis when an explicit command path is desired.
- `app/youtube_auth.py`
  One-time OAuth bootstrap that generates the persisted YouTube token file.

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
- flashcard JSON
- audio files
- video files

## External Dependencies

- Trilium ETAPI for note traversal
- OpenAI Responses API for script and flashcard generation
- Kokoro for local TTS generation
- `ffmpeg` for MP4 rendering
- YouTube Data API v3 for unlisted uploads

## Runtime Expectations

- `ffmpeg` must be installed on the host.
- `espeak-ng` must be installed when using Kokoro for English fallback and phoneme support.
- Python dependencies should be installed with `uv sync --extra dev --extra tts --extra youtube`.
- `.env` must contain valid Trilium, OpenAI, and YouTube settings.
- Network binding is controlled by `APP_HOST` and `APP_PORT` in `.env`, which are consumed by the systemd unit in production.
