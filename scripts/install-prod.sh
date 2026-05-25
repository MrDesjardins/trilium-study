#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-trilium-study.service}"
export PATH="${HOME}/.local/bin:${PATH}"

check_command() {
  local name="$1"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "Required command missing: $name" >&2
    exit 1
  fi
}

require_file() {
  local path="$1"
  local description="$2"
  if [[ ! -f "${path}" ]]; then
    echo "Missing ${description}: ${path}" >&2
    exit 1
  fi
}

ensure_systemd_user_available() {
  export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
  if systemctl --user show-environment >/dev/null 2>&1; then
    :
  elif command -v loginctl >/dev/null 2>&1 && command -v sudo >/dev/null 2>&1; then
    sudo -n loginctl enable-linger "$USER" >/dev/null 2>&1 || true
    if ! systemctl --user show-environment >/dev/null 2>&1; then
      echo "systemd user services are not available for $USER. Ensure lingering is enabled and the user manager is running." >&2
      exit 1
    fi
  else
    echo "systemd user services are not available for $USER. Ensure lingering is enabled and the user manager is running." >&2
    exit 1
  fi

  if command -v loginctl >/dev/null 2>&1; then
    local linger_state
    linger_state="$(loginctl show-user "$USER" -p Linger 2>/dev/null || true)"
    if [[ "${linger_state}" != "Linger=yes" ]]; then
      if command -v sudo >/dev/null 2>&1; then
        sudo -n loginctl enable-linger "$USER" >/dev/null 2>&1 || true
        linger_state="$(loginctl show-user "$USER" -p Linger 2>/dev/null || true)"
      fi
      if [[ "${linger_state}" != "Linger=yes" ]]; then
        echo "Persistent user services require loginctl linger for $USER. Run: sudo loginctl enable-linger $USER" >&2
        exit 1
      fi
    fi
  fi
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then
    return
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "uv is required and curl is not installed to bootstrap it" >&2
    exit 1
  fi
  curl -LsSf https://astral.sh/uv/install.sh | sh
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv installation completed but uv is still not on PATH" >&2
    exit 1
  fi
}

verify_env_file() {
  require_file "${APP_DIR}/.env" ".env"

  local app_port app_base_url app_host database_url youtube_token_file youtube_client_secrets
  app_port="$(awk -F= '/^APP_PORT=/{print $2}' "${APP_DIR}/.env" | tail -n 1)"
  app_base_url="$(awk -F= '/^APP_BASE_URL=/{sub(/^APP_BASE_URL=/,""); print}' "${APP_DIR}/.env" | tail -n 1)"
  app_host="$(awk -F= '/^APP_HOST=/{print $2}' "${APP_DIR}/.env" | tail -n 1)"
  database_url="$(awk -F= '/^DATABASE_URL=/{sub(/^DATABASE_URL=/,""); print}' "${APP_DIR}/.env" | tail -n 1)"
  youtube_token_file="$(awk -F= '/^YOUTUBE_TOKEN_FILE=/{sub(/^YOUTUBE_TOKEN_FILE=/,""); print}' "${APP_DIR}/.env" | tail -n 1)"
  youtube_client_secrets="$(awk -F= '/^YOUTUBE_CLIENT_SECRETS=/{sub(/^YOUTUBE_CLIENT_SECRETS=/,""); print}' "${APP_DIR}/.env" | tail -n 1)"

  if [[ -z "${app_port}" || -z "${app_base_url}" || -z "${app_host}" ]]; then
    echo "APP_HOST, APP_PORT, and APP_BASE_URL must be set in ${APP_DIR}/.env" >&2
    exit 1
  fi
  if [[ "${database_url}" != "sqlite:///./.state/trilium-study.db" ]]; then
    echo "DATABASE_URL must remain sqlite:///./.state/trilium-study.db for production installs" >&2
    exit 1
  fi
  if [[ -n "${youtube_token_file}" && ! -f "${APP_DIR}/${youtube_token_file}" ]]; then
    echo "Missing YouTube token file referenced by .env: ${APP_DIR}/${youtube_token_file}" >&2
    exit 1
  fi
  if [[ -n "${youtube_client_secrets}" && ! -f "${APP_DIR}/${youtube_client_secrets}" ]]; then
    echo "Missing YouTube client secrets referenced by .env: ${APP_DIR}/${youtube_client_secrets}" >&2
    exit 1
  fi
}

ensure_uv

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v espeak-ng >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y ffmpeg espeak-ng
  else
    echo "Missing required system packages: ffmpeg and/or espeak-ng" >&2
    exit 1
  fi
fi

cd "${APP_DIR}"
verify_env_file
mkdir -p "${APP_DIR}/.state/logs" "${APP_DIR}/.state/workspace"

uv venv .venv
. .venv/bin/activate
uv sync --extra dev --extra tts --extra youtube
python - <<'PY'
import importlib.util
import subprocess
import sys

MODEL = "en_core_web_sm"

if importlib.util.find_spec(MODEL) is None:
    try:
        subprocess.run([sys.executable, "-m", "spacy", "download", MODEL], check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "Failed to install required spaCy model en_core_web_sm for Kokoro English synthesis. "
            "Ensure network access is available during install and rerun scripts/install-prod.sh."
        ) from exc
PY
python -m app.bootstrap
python - <<'PY'
import importlib.util
required = ["numpy", "soundfile", "kokoro", "fastapi", "sqlalchemy", "openai", "googleapiclient", "en_core_web_sm"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"Missing required Python packages: {', '.join(missing)}")
PY

check_command ffmpeg
check_command espeak-ng
check_command systemctl
check_command rsync
ensure_systemd_user_available

mkdir -p "${HOME}/.config/systemd/user"
cp "systemd/${SERVICE_NAME}" "${HOME}/.config/systemd/user/${SERVICE_NAME}"
systemctl --user daemon-reload
systemctl --user enable --now "${SERVICE_NAME}"
systemctl --user restart "${SERVICE_NAME}"
systemctl --user --no-pager --full status "${SERVICE_NAME}" >/dev/null
