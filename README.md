# trilium-study

Single-user FastAPI service that treats a configured Trilium parent note as a course, each direct child as a lesson, cleans noisy HTML-heavy lesson content into study-friendly source text, validates and expands lesson notes into detailed study scripts with narration-length safeguards, and builds flashcards, narration, MP4s, and unlisted YouTube upload records with durable SQLite-backed job state.

## Local setup

```bash
uv sync --extra dev --extra tts --extra youtube
cp .env.example .env
uv run python -m app.bootstrap
./run-dev.sh
```

## Environment

Required settings live in `.env`:

- `OPENAI_API_KEY`
- `TRILIUM_URL`
- `TRILIUM_ETAPI_TOKEN`
- `TRILIUM_PARENT_NOTE_ID`

Optional integrations:

- `APP_HOST`
- `APP_PORT`
- `AUDIO_QUEUE_URL`
- `KOKORO_COMMAND`
- `KOKORO_VOICE`
- `KOKORO_LANG_CODE`
- `KOKORO_SPEED`
- `YOUTUBE_CLIENT_SECRETS`
- `YOUTUBE_TOKEN_FILE`

To generate the persisted YouTube OAuth token after setting those values:

```bash
uv sync --extra youtube
uv run python -m app.youtube_auth
```

## Kokoro TTS

You do not need to set `KOKORO_COMMAND` if you use the built-in Kokoro path. Install the TTS dependencies and leave `KOKORO_COMMAND` blank:

```bash
uv sync --extra tts
```

On Ubuntu/Debian, install `espeak-ng` as well:

```bash
sudo apt-get install espeak-ng
```

The app will then synthesize audio directly with Kokoro using:

- `KOKORO_VOICE=af_heart`
- `KOKORO_LANG_CODE=a`
- `KOKORO_SPEED=1.0`

If you want an explicit command anyway, use:

```bash
KOKORO_COMMAND=".venv/bin/python -m app.kokoro_cli --input {input} --output {output}"
```

## Architecture

- `app/main.py`: FastAPI app, pages, and JSON endpoints
- `app/jobs.py`: durable single-worker lesson pipeline runner
- `app/migrate.py`: Alembic migration entry point
- `app/content.py`: Trilium API access and recursive lesson collection
- `app/pipeline.py`: script, flashcard, audio, video, YouTube, and SM-2 helpers
- `app/kokoro_cli.py`: repo-native Kokoro WAV generator
- `app/youtube_auth.py`: one-time OAuth token bootstrap for YouTube uploads
- `app/models.py`: SQLite schema for courses, lessons, artifacts, jobs, uploads, and flashcards
- `deploy.sh`: renders production `.env`, preserves remote production state by default, and installs/restarts the mini-pc system service

## Tests

```bash
uv run pytest
```

## Deployment

For the mini-pc deployment flow, one-time host setup, and future update steps, see [docs/OPERATIONS.md](/home/miste/code/trilium-study/docs/OPERATIONS.md).

For production support tasks such as service status, logs, health checks, and SQLite backups, use the maintenance section in [docs/OPERATIONS.md](/home/miste/code/trilium-study/docs/OPERATIONS.md).

## Database

Schema changes are managed through Alembic migrations and are applied by `uv run python -m app.bootstrap`.

## Project Docs

- [docs/ARCHITECTURE.md](/home/miste/code/trilium-study/docs/ARCHITECTURE.md)
- [docs/TESTING.md](/home/miste/code/trilium-study/docs/TESTING.md)
- [docs/OPERATIONS.md](/home/miste/code/trilium-study/docs/OPERATIONS.md)
