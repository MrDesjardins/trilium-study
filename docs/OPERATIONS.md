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
- copies the full `.state` directory, including:
  - `.state/trilium-study.db`
  - `.state/youtube-token.json`
  - `.state/client_secret_*.json`
  - existing workspace artifacts
- runs `scripts/install-prod.sh` remotely to reinstall deps, run migrations, and restart the service

Typical deploy:

```bash
./deploy.sh
```

Optional knobs:

```bash
COPY_STATE=0 ./deploy.sh
REMOTE_APP_PORT=8083 ./deploy.sh
REMOTE_APP_BASE_URL=http://10.0.0.181:8083 ./deploy.sh
```

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
