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

require_noninteractive_service_sudo() {
  local sudo_output
  if sudo_output="$(sudo -n systemctl status "${SERVICE_NAME}" >/dev/null 2>&1)"; then
    return
  fi

  echo "Service ${SERVICE_NAME} is not installed or the current user cannot manage it with passwordless sudo." >&2
  echo "Run scripts/install-prod.sh once on the host with a TTY to install or update the systemd unit." >&2
  echo "For unattended deploys afterward, configure sudoers so ${USER} can run systemctl for ${SERVICE_NAME} without a password." >&2
  exit 1
}

require_file() {
  local path="$1"
  local description="$2"
  if [[ ! -f "${path}" ]]; then
    echo "Missing ${description}: ${path}" >&2
    exit 1
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

ensure_spacy_model() {
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
            "Ensure network access is available during install and rerun scripts/update-prod.sh."
        ) from exc
PY
}

check_python_packages() {
  python - <<'PY'
import importlib.util
required = ["numpy", "soundfile", "kokoro", "fastapi", "sqlalchemy", "openai", "googleapiclient", "en_core_web_sm"]
missing = [name for name in required if importlib.util.find_spec(name) is None]
if missing:
    raise SystemExit(f"Missing required Python packages: {', '.join(missing)}")
PY
}

check_command rsync
check_command systemctl
check_command sudo

require_noninteractive_service_sudo

ensure_uv

cd "${APP_DIR}"
verify_env_file
mkdir -p "${APP_DIR}/.state/logs" "${APP_DIR}/.state/workspace"

uv venv --allow-existing .venv
. .venv/bin/activate
uv sync --extra dev --extra tts --extra youtube
ensure_spacy_model
python -m app.bootstrap
check_python_packages

check_command ffmpeg
check_command espeak-ng

sudo -n systemctl restart "${SERVICE_NAME}"
sudo -n systemctl --no-pager --full status "${SERVICE_NAME}" >/dev/null
