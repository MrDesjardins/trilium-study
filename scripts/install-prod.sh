#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-trilium-study.service}"

check_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Required command missing: $name" >&2
    exit 1
  fi
}

ensure_systemd_user_available() {
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if systemctl --user show-environment >/dev/null 2>&1; then
    return
  fi
  if command -v loginctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    sudo loginctl enable-linger "$USER" >/dev/null 2>&1 || true
  fi
  if ! systemctl --user show-environment >/dev/null 2>&1; then
    echo "systemd user services are not available for $USER. Ensure lingering is enabled and the user manager is running." >&2
    exit 1
  fi
}

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required but not installed" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v espeak-ng >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y ffmpeg espeak-ng
  else
    echo "Missing required system packages: ffmpeg and/or espeak-ng" >&2
    exit 1
  fi
fi

mkdir -p "${APP_DIR}/.state/logs" "${APP_DIR}/.state/workspace"

cd "${APP_DIR}"
if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "Missing ${APP_DIR}/.env" >&2
  exit 1
fi

uv venv .venv
. .venv/bin/activate
uv sync --extra dev --extra tts --extra youtube
python -m app.bootstrap
python - <<'PY'
import importlib.util
required = ["numpy", "soundfile", "kokoro", "fastapi", "sqlalchemy", "openai", "googleapiclient"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"Missing required Python packages: {', '.join(missing)}")
PY

check_command ffmpeg
check_command espeak-ng
check_command systemctl
ensure_systemd_user_available

mkdir -p "${HOME}/.config/systemd/user"
cp "systemd/${SERVICE_NAME}" "${HOME}/.config/systemd/user/${SERVICE_NAME}"
systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"
systemctl --user restart "${SERVICE_NAME}"
systemctl --user --no-pager --full status "${SERVICE_NAME}" >/dev/null
