# Operations

## Local Setup

```bash
uv sync --extra dev --extra tts --extra youtube
uv run python -m app.bootstrap
./run-dev.sh
```

`app.bootstrap` now runs Alembic migrations to bring the SQLite schema to the current revision.

System packages required on Linux:

```bash
sudo apt-get update
sudo apt-get install -y ffmpeg espeak-ng
```

## YouTube Auth

Generate the persisted token file with:

```bash
uv run python -m app.youtube_auth
```

This writes the OAuth token JSON to the path configured in `YOUTUBE_TOKEN_FILE`.

## Port Configuration

Set the listening address in `.env`:

```bash
APP_HOST=0.0.0.0
APP_PORT=8017
APP_BASE_URL=http://10.0.0.181:8017
```

Use the same port in `APP_BASE_URL` so generated links and callbacks stay consistent.

## Kokoro

Preferred configuration:

- leave `KOKORO_COMMAND` blank
- set `KOKORO_VOICE`
- set `KOKORO_LANG_CODE`
- set `KOKORO_SPEED`

If you need an explicit command, use:

```bash
.venv/bin/python -m app.kokoro_cli --input {input} --output {output}
```

## Production Install

Use [scripts/install-prod.sh](/home/miste/code/trilium-study/scripts/install-prod.sh) on the mini-pc. It:

- installs required system packages
- creates the virtual environment
- installs Python dependencies with the required extras
- creates state directories
- runs Alembic migrations
- installs the systemd user unit
- verifies that the systemd user manager is actually available before enabling the service

Deploy from the workstation with [deploy.sh](/home/miste/code/trilium-study/deploy.sh).
