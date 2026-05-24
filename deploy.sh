#!/usr/bin/env bash
set -euo pipefail

REMOTE_HOST="${REMOTE_HOST:-10.0.0.181}"
REMOTE_USER="${REMOTE_USER:-pdesjardins}"
REMOTE_DIR="${REMOTE_DIR:-/home/pdesjardins/code/trilium-study}"
SERVICE_NAME="${SERVICE_NAME:-trilium-study.service}"

rsync -az --delete \
  --exclude '.git' \
  --exclude '.venv' \
  --exclude '.pytest_cache' \
  --exclude '.state' \
  ./ "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

ssh "${REMOTE_USER}@${REMOTE_HOST}" "cd ${REMOTE_DIR} && chmod +x scripts/install-prod.sh && APP_DIR=${REMOTE_DIR} SERVICE_NAME=${SERVICE_NAME} ./scripts/install-prod.sh"
