#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-10.0.0.181}"
REMOTE_USER="${REMOTE_USER:-pdesjardins}"
REMOTE_DIR="${REMOTE_DIR:-/home/pdesjardins/code/trilium-study}"
SERVICE_NAME="${SERVICE_NAME:-trilium-study.service}"
REMOTE_APP_HOST="${REMOTE_APP_HOST:-0.0.0.0}"
REMOTE_APP_PORT="${REMOTE_APP_PORT:-8083}"
REMOTE_APP_BASE_URL="${REMOTE_APP_BASE_URL:-http://${REMOTE_HOST}:${REMOTE_APP_PORT}}"
COPY_STATE="${COPY_STATE:-0}"
STATE_PATH="${STATE_PATH:-.state}"
SSH_KEY_PATH="${SSH_KEY_PATH:-${HOME}/.ssh/id_ed25519}"
CONFIGURE_SSH_KEY="${CONFIGURE_SSH_KEY:-0}"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

if [[ ! -f ".env" ]]; then
  echo "Missing local .env" >&2
  exit 1
fi

render_remote_env() {
  local target="$1"
  awk \
    -v app_host="${REMOTE_APP_HOST}" \
    -v app_port="${REMOTE_APP_PORT}" \
    -v app_base_url="${REMOTE_APP_BASE_URL}" '
      BEGIN {
        replaced_host = 0
        replaced_port = 0
        replaced_base = 0
      }
      /^APP_HOST=/ {
        print "APP_HOST=" app_host
        replaced_host = 1
        next
      }
      /^APP_PORT=/ {
        print "APP_PORT=" app_port
        replaced_port = 1
        next
      }
      /^APP_BASE_URL=/ {
        print "APP_BASE_URL=" app_base_url
        replaced_base = 1
        next
      }
      { print }
      END {
        if (!replaced_host) print "APP_HOST=" app_host
        if (!replaced_port) print "APP_PORT=" app_port
        if (!replaced_base) print "APP_BASE_URL=" app_base_url
      }
    ' .env > "${target}"
}

configure_remote_ssh_key() {
  local public_key_path="${SSH_KEY_PATH}.pub"
  if [[ ! -f "${SSH_KEY_PATH}" || ! -f "${public_key_path}" ]]; then
    echo "SSH key pair missing at ${SSH_KEY_PATH}. Generate it first or set SSH_KEY_PATH." >&2
    exit 1
  fi
  mkdir -p "${HOME}/.ssh"
  chmod 700 "${HOME}/.ssh"
  ssh-copy-id -i "${public_key_path}" "${REMOTE_USER}@${REMOTE_HOST}"
}

if [[ "${CONFIGURE_SSH_KEY}" == "1" ]]; then
  configure_remote_ssh_key
fi

render_remote_env "${TMP_DIR}/.env"

rsync -az --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.pytest_cache' \
  ./ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

scp "${TMP_DIR}/.env" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/.env"

if [[ "${COPY_STATE}" == "1" && -d "${STATE_PATH}" ]]; then
  rsync -az \
    --delete \
    "${STATE_PATH}/" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/${STATE_PATH}/"
fi

ssh "${REMOTE_USER}@${REMOTE_HOST}" "cd ${REMOTE_DIR} && chmod +x scripts/update-prod.sh && APP_DIR=${REMOTE_DIR} SERVICE_NAME=${SERVICE_NAME} ./scripts/update-prod.sh"
