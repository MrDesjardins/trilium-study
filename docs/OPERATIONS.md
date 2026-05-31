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

For local development, set the listening address in `.env`:

```bash
APP_HOST=0.0.0.0
APP_PORT=8017
APP_BASE_URL=http://10.0.0.181:8017
```

Use the same port in `APP_BASE_URL` so generated links and callbacks stay consistent.

For the mini-pc deployment, `deploy.sh` renders a remote `.env` automatically with:

```bash
APP_HOST=0.0.0.0
APP_PORT=8083
APP_BASE_URL=http://10.0.0.181:8083
```

The workstation `.env` remains the source template for the rest of the configuration.

## Audio Stream Queue

If the mini-pc also hosts the separate YouTube audio-stream queue service, this app can push uploaded lesson videos into that queue through a server-side lesson action.

Default configuration:

```bash
AUDIO_QUEUE_URL=http://127.0.0.1:8000/queue/add
```

This is intended to stay loopback-only on the mini-pc. The Trilium Study app calls it server-to-server, so the queue API does not need to be exposed to the LAN.

## Kokoro

Preferred configuration:

- leave `KOKORO_COMMAND` blank
- set `KOKORO_VOICE`
- set `KOKORO_LANG_CODE`
- set `KOKORO_SPEED`

For English Kokoro voices, production installs also need the spaCy model `en_core_web_sm`. `scripts/install-prod.sh` now installs it ahead of service startup so Kokoro does not attempt a runtime model download from inside the systemd service.

Set `HF_TOKEN` in `.env` if Hugging Face Hub warns about unauthenticated requests while Kokoro loads model assets. The TTS path exports that configured token for `huggingface_hub` before Kokoro imports. The app suppresses known non-actionable Kokoro/PyTorch runtime warnings during TTS generation, including `weight_norm` deprecation noise, single-layer LSTM dropout warnings, and NNPACK unsupported-hardware fallback messages. Treat new warnings outside that set as actionable until checked.

If you need an explicit command, use:

```bash
.venv/bin/python -m app.kokoro_cli --input {input} --output {output}
```

## Production Install

Use [scripts/install-prod.sh](/home/miste/code/trilium-study/scripts/install-prod.sh) on the mini-pc for one-time host bootstrap or whenever the systemd unit definition changes materially. It:

- bootstraps `uv` automatically if the host does not already have it
- installs required system packages
- creates the virtual environment
- installs Python dependencies with the required extras
- preinstalls the `en_core_web_sm` spaCy model required by Kokoro English synthesis
- validates the deployed `.env`
- preserves and reuses copied state directories
- runs Alembic migrations
- renders and installs a systemd system service under `/etc/systemd/system`
- enables and restarts the service through `sudo systemctl`

Important:

- run `scripts/install-prod.sh` as the application user, not with `sudo`
- the script uses `sudo` internally only for the system-service install/restart steps
- running the whole script under `sudo` will create root-owned runtime files and install `uv` under `/root`

For routine code deploys after that one-time bootstrap, use [scripts/update-prod.sh](/home/miste/code/trilium-study/scripts/update-prod.sh). It:

- creates or reuses the virtual environment
- syncs Python dependencies and required extras
- validates the deployed `.env`
- reruns Alembic migrations through `app.bootstrap`
- restarts the already-installed systemd service

This matches the simpler update pattern used by the other Python services under `~/code`.

## Production Deploy From Workstation

Deploy from the workstation with [deploy.sh](/home/miste/code/trilium-study/deploy.sh).

The script now:

- rsyncs the repository to `pdesjardins@10.0.0.181:/home/pdesjardins/code/trilium-study`
- renders and uploads a production `.env`
- runs `scripts/update-prod.sh` remotely to reinstall deps, run migrations, and restart the existing service
- preserves the mini-pc `.state` by default so production remains the source of truth for runtime data

Typical deploy:

```bash
./deploy.sh
```

This default does not overwrite the mini-pc SQLite DB, YouTube auth files, or generated artifacts.
The primary code sync excludes `.state`; production state is copied from the workstation only when `COPY_STATE=1`.

Optional knobs:

```bash
COPY_STATE=1 ./deploy.sh
REMOTE_APP_PORT=8083 ./deploy.sh
REMOTE_APP_BASE_URL=http://10.0.0.181:8083 ./deploy.sh
```

Use `COPY_STATE=1` only for intentional bootstrap or recovery when you want to replace the remote runtime state from this workstation. That copies the full `.state` directory, including:

- `.state/trilium-study.db`
- `.state/youtube-token.json`
- `.state/client_secret_*.json`
- existing workspace artifacts

## Passwordless Deploy Setup

Preferred setup is SSH key auth from this workstation to the mini-pc.

Generate a key if needed:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519
```

Install the public key on the mini-pc once:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub pdesjardins@10.0.0.181
```

Or let the deploy helper do that one-time step:

```bash
CONFIGURE_SSH_KEY=1 ./deploy.sh
```

After that, `./deploy.sh` should run without prompting for SSH credentials.

For fully unattended deploys, the mini-pc user also needs non-interactive `sudo` for the existing `trilium-study.service` restart and status commands. If that is not configured yet, `scripts/update-prod.sh` will fail fast with an actionable message instead of hanging on a password prompt.

## One-Time LAN Access Setup

If the mini-pc firewall is enabled, allow the application port once:

```bash
sudo ufw allow 8083/tcp
sudo ufw reload
```

Confirm:

```bash
sudo ufw status verbose
```

Without this, the app can be healthy on the mini-pc but still unreachable from other machines on the LAN.

## Future Updates

After the one-time SSH, service install, and firewall setup, future updates should only require:

```bash
./deploy.sh
```

## Local GPU Generation With Production Database

When this workstation has a faster GPU than the mini-pc, generate lessons locally and write the results back to production with [scripts/generate-local-prod.sh](/home/miste/code/trilium-study/scripts/generate-local-prod.sh).

The script:

1. stops `trilium-study.service` on the mini-pc so SQLite is not locked
2. copies the production database and any existing lesson workspace artifacts to `.state/prod-sync/`
3. runs the full lesson pipeline locally (Kokoro TTS uses this machine's GPU)
4. copies the updated database and generated artifacts back to the mini-pc
5. restarts the production service

List lessons from production:

```bash
./scripts/generate-local-prod.sh --list
```

Generate one or more lessons:

```bash
./scripts/generate-local-prod.sh 42
./scripts/generate-local-prod.sh 42 43 44
./scripts/generate-local-prod.sh --force 42
```

Requirements on this workstation:

- passwordless SSH to the mini-pc (same as `./deploy.sh`)
- passwordless `sudo systemctl stop/start trilium-study.service` on the mini-pc
- local `.env` with valid Trilium, OpenAI, and Kokoro settings
- `uv sync --extra tts --extra youtube` dependencies installed

The script uses your local `.env` for Trilium and OpenAI, but copies the production SQLite database and YouTube auth files from the mini-pc for the duration of the run. Results appear in the production UI at `http://10.0.0.181:8083/` when the sync completes.

To run against the synced production database without the remote sync wrapper:

```bash
DATABASE_URL=sqlite:///$(pwd)/.state/prod-sync/trilium-study.db \
WORKSPACE_DIR=$(pwd)/.state/prod-sync/workspace \
uv run python -m app.run_lesson 42
```

Recommended post-deploy checks:

```bash
ssh pdesjardins@10.0.0.181 "sudo systemctl --no-pager --full status trilium-study.service | sed -n '1,20p'"
curl http://10.0.0.181:8083/healthz
```

## Maintenance And Support

Use these commands when supporting the production mini-pc.

### Service Status

Check the system service:

```bash
ssh pdesjardins@10.0.0.181 "sudo systemctl --no-pager --full status trilium-study.service"
```

Useful lifecycle commands:

```bash
ssh pdesjardins@10.0.0.181 "sudo systemctl restart trilium-study.service"
ssh pdesjardins@10.0.0.181 "sudo systemctl stop trilium-study.service"
ssh pdesjardins@10.0.0.181 "sudo systemctl start trilium-study.service"
```

### Service Logs

Read recent systemd journal logs:

```bash
ssh pdesjardins@10.0.0.181 "sudo journalctl -u trilium-study.service -n 100 --no-pager"
```

Follow logs live:

```bash
ssh pdesjardins@10.0.0.181 "sudo journalctl -u trilium-study.service -f"
```

The app also writes file logs under `.state/logs`:

```bash
ssh pdesjardins@10.0.0.181 "tail -n 100 /home/pdesjardins/code/trilium-study/.state/logs/app.log"
ssh pdesjardins@10.0.0.181 "tail -n 50 /home/pdesjardins/code/trilium-study/.state/logs/app.jsonl"
```

Use the journal first for service start/stop failures, and `app.log` / `app.jsonl` for application behavior and job failures.

If the service exits immediately after startup, check whether another process already holds the configured app port:

```bash
ssh pdesjardins@10.0.0.181 "ss -ltnp '( sport = :8083 )'"
ssh pdesjardins@10.0.0.181 "ps -fp <pid>"
```

This service now follows the same `uv run python -m ...` pattern as the other Python services under `~/code`. If a stale manual process is holding the port, stop it and then restart the unit:

```bash
ssh pdesjardins@10.0.0.181 "kill <pid>"
ssh pdesjardins@10.0.0.181 "sudo systemctl restart trilium-study.service"
```

### Health Checks

Check the app from another machine:

```bash
curl http://10.0.0.181:8083/healthz
```

Check locally on the mini-pc:

```bash
ssh pdesjardins@10.0.0.181 "curl http://127.0.0.1:8083/healthz"
```

If the local check works but the remote check fails, the usual cause is firewall or LAN routing rather than the app itself.

### Database And State

The production SQLite database lives at:

```bash
/home/pdesjardins/code/trilium-study/.state/trilium-study.db
```

Quick lesson count check:

```bash
ssh pdesjardins@10.0.0.181 "sqlite3 /home/pdesjardins/code/trilium-study/.state/trilium-study.db 'select count(*) from lessons;'"
```

Create a timestamped production backup:

```bash
ssh pdesjardins@10.0.0.181 "cp /home/pdesjardins/code/trilium-study/.state/trilium-study.db /home/pdesjardins/code/trilium-study/.state/trilium-study.db.$(date +%Y%m%d-%H%M%S).bak"
```

Important runtime state on the mini-pc:

- `.state/trilium-study.db`
- `.state/youtube-token.json`
- `.state/client_secret_*.json`
- `.state/workspace/`
- `.state/logs/`

Do not overwrite these during routine deploys. That is why `deploy.sh` defaults to `COPY_STATE=0`.

### Stale YouTube Upload Recovery

If a lesson has a stored YouTube URL but the external video was deleted or removed, clear only the upload state so the lesson can be uploaded again without losing script, flashcards, audio, video, or review history.

First create a backup:

```bash
ssh pdesjardins@10.0.0.181 "cp /home/pdesjardins/code/trilium-study/.state/trilium-study.db /home/pdesjardins/code/trilium-study/.state/trilium-study.db.$(date +%Y%m%d-%H%M%S).bak"
```

Then clear the stale upload row and reset the upload artifact for the affected lesson:

```bash
ssh pdesjardins@10.0.0.181 "sqlite3 /home/pdesjardins/code/trilium-study/.state/trilium-study.db \"begin; delete from youtube_uploads where lesson_id = <lesson_id>; update lesson_artifacts set state = 'pending', metadata_json = null, error = null where lesson_id = <lesson_id> and artifact_type = 'youtube_upload'; commit;\""
```

Afterward, use the app's lesson `Force Generate All` action to create a new script, audio, video, and upload with the current generation rules.

### Common Recovery Steps

If a deploy completed but the app is unavailable:

1. Check `sudo systemctl status trilium-study.service`
2. Check `sudo journalctl -u trilium-study.service -n 100 --no-pager`
3. Check local health: `curl http://127.0.0.1:8083/healthz`
4. If local works but remote does not, check `ufw`
5. If the service is down after a code update, rerun `./deploy.sh`

If you intentionally need to replace production state from this workstation:

```bash
COPY_STATE=1 ./deploy.sh
```

That should be treated as a recovery/bootstrap action, not the normal update path.
