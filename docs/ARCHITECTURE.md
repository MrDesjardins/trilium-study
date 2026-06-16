# Architecture

## Overview

`trilium-study` is a single-user FastAPI application that turns a configured Trilium catalog note into multiple courses. Each direct child of the catalog note is treated as a course, and each direct child of a course note is treated as a lesson unit. Each lesson is processed through a durable staged pipeline:

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
  Server-rendered FastAPI UI, catalog sync, grouped multi-course dashboard, JSON endpoints, polling status APIs, flashcard study queue stats, in-place due-queue reviews via `Accept: application/json` on the review POST, browse/reset study actions, server-side audio-stream queue actions for uploaded YouTube lessons, app bootstrap, and background runner startup.
- `app/content.py`
  Trilium ETAPI client plus recursive lesson collection and normalized text assembly, including HTML cleanup and duplicate-block suppression so downstream script generation sees cleaner study material.
- `app/jobs.py`
  Single-worker durable job runner for course sync and per-lesson pipeline execution, including queued-state handling, duplicate-job protection, failed-stage resumability, and bounded automatic retries for lesson jobs.
- `app/status.py`
  Runtime dependency checks, queue position calculation, stage progress modeling, ETA helpers, and generated-script length metadata for the UI.
- `app/pipeline.py`
  LLM-backed script validation and expansion with minimum narration-length gates based on cleaned source text, including a looser middle-band floor so study-worthy 8-11 minute scripts are not rejected unnecessarily, comprehension-first prompting that pushes for examples and alternate explanations when needed, plain-spoken narration validation that rejects Markdown-like script output before TTS, multi-attempt in-stage script retries that escalate to section-by-section teaching requirements before the lesson job fails, flashcard generation, Kokoro TTS integration, ffmpeg rendering sized to narration duration, YouTube upload integration, and SM-2 review scheduling.
- `app/models.py`
  SQLAlchemy schema for courses, lessons, artifacts, jobs, job events, uploads, flashcards, and reviews.
- `app/migrate.py` and `migrations/`
  Alembic-backed schema migration system used for bootstrap and deployment.
- `app/kokoro_cli.py`
  Repo-native CLI wrapper for Kokoro WAV synthesis when an explicit command path is desired.
- `app/youtube_auth.py`
  One-time OAuth bootstrap that generates the persisted YouTube token file.
- `app/run_lesson.py`
  CLI entry point for synchronous local lesson generation against the configured database and workspace.
- `deploy.sh`, `scripts/update-prod.sh`, `scripts/install-prod.sh`, and `scripts/generate-local-prod.sh`
  Workstation-to-mini-pc deployment path that renders a production `.env`, excludes `.state` during routine code syncs, copies SQLite/runtime state only when requested, and runs a non-interactive update flow for routine deploys. A separate one-time host bootstrap script handles privileged package and systemd unit installation. `generate-local-prod.sh` runs GPU-backed generation on the workstation and syncs results back to production.

## Persistence Model

SQLite stores:

- course and lesson metadata
- course and lesson archive markers used to hide missing Trilium notes without deleting generated state
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
- script provenance including source/script word counts, estimated narration length, validation notes, and added-context metadata kept separate from spoken narration
- flashcard JSON
- audio files
- video files

Catalog sync preserves generated state. Courses or lessons that disappear from the configured catalog hierarchy are marked archived and hidden from the default dashboard/API, but their database rows, flashcards, upload metadata, and workspace artifacts remain available for recovery or future reactivation. If an archived course or lesson reappears in Trilium under the catalog, sync clears the archive marker and makes it active again.

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
- `.env` must contain valid Trilium, OpenAI, and YouTube settings. `TRILIUM_PARENT_NOTE_ID` should point to the catalog/root note whose direct children are course notes.
- Network binding is controlled by `APP_HOST` and `APP_PORT` in `.env`. The production systemd unit now follows the same pattern as the other Python services under `~/code`: `uv run python -m app.serve`, with the application loading `.env` itself before Uvicorn starts.
