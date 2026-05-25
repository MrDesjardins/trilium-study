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

If you need an explicit command, use:

```bash
.venv/bin/python -m app.kokoro_cli --input {input} --output {output}
```

## Production Install

Use [scripts/install-prod.sh](/home/miste/code/trilium-study/scripts/install-prod.sh) on the mini-pc. It:

- bootstraps `uv` automatically if the host does not already have it
- installs required system packages
- creates the virtual environment
- installs Python dependencies with the required extras
- validates the deployed `.env`
- preserves and reuses copied state directories
- runs Alembic migrations
- installs the systemd user unit
- verifies that the systemd user manager is actually available before enabling the service
- requires `loginctl linger` for the target user so the service survives after SSH logout

## Production Deploy From Workstation

Deploy from the workstation with [deploy.sh](/home/miste/code/trilium-study/deploy.sh).

The script now:

- rsyncs the repository to `pdesjardins@10.0.0.181:/home/pdesjardins/code/trilium-study`
- renders and uploads a production `.env`
- runs `scripts/install-prod.sh` remotely to reinstall deps, run migrations, and restart the service
- preserves the mini-pc `.state` by default so production remains the source of truth for runtime data

Typical deploy:

```bash
./deploy.sh
```

This default does not overwrite the mini-pc SQLite DB, YouTube auth files, or generated artifacts.

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

After that, `./deploy.sh` should run without prompting for credentials.

## One-Time Systemd Persistence Setup

Because the app runs as a systemd user service, the mini-pc user must have linger enabled once:

```bash
sudo loginctl enable-linger pdesjardins
```

Without this, the service can start during an SSH session and then disappear after logout.

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

After the one-time SSH, linger, and firewall setup, future updates should only require:

```bash
./deploy.sh
```

Recommended post-deploy checks:

```bash
ssh pdesjardins@10.0.0.181 "systemctl --user --no-pager --full status trilium-study.service | sed -n '1,20p'"
curl http://10.0.0.181:8083/healthz
```

## Maintenance And Support

Use these commands when supporting the production mini-pc.

### Service Status

Check the systemd user service:

```bash
ssh pdesjardins@10.0.0.181 "systemctl --user --no-pager --full status trilium-study.service"
```

Useful lifecycle commands:

```bash
ssh pdesjardins@10.0.0.181 "systemctl --user restart trilium-study.service"
ssh pdesjardins@10.0.0.181 "systemctl --user stop trilium-study.service"
ssh pdesjardins@10.0.0.181 "systemctl --user start trilium-study.service"
```

### Service Logs

Read recent systemd journal logs:

```bash
ssh pdesjardins@10.0.0.181 "journalctl --user -u trilium-study.service -n 100 --no-pager"
```

Follow logs live:

```bash
ssh pdesjardins@10.0.0.181 "journalctl --user -u trilium-study.service -f"
```

The app also writes file logs under `.state/logs`:

```bash
ssh pdesjardins@10.0.0.181 "tail -n 100 /home/pdesjardins/code/trilium-study/.state/logs/app.log"
ssh pdesjardins@10.0.0.181 "tail -n 50 /home/pdesjardins/code/trilium-study/.state/logs/app.jsonl"
```

Use the journal first for service start/stop failures, and `app.log` / `app.jsonl` for application behavior and job failures.

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

### Common Recovery Steps

If a deploy completed but the app is unavailable:

1. Check `systemctl --user status trilium-study.service`
2. Check `journalctl --user -u trilium-study.service -n 100 --no-pager`
3. Check local health: `curl http://127.0.0.1:8083/healthz`
4. If local works but remote does not, check `ufw`
5. If the service is down after a code update, rerun `./deploy.sh`

If you intentionally need to replace production state from this workstation:

```bash
COPY_STATE=1 ./deploy.sh
```

That should be treated as a recovery/bootstrap action, not the normal update path.
