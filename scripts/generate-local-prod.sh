#!/usr/bin/env bash
# Run lesson generation on this workstation (local GPU) and write results to the
# production database and workspace on the mini-pc.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
REMOTE_HOST="${REMOTE_HOST:-10.0.0.181}"
REMOTE_USER="${REMOTE_USER:-pdesjardins}"
REMOTE_DIR="${REMOTE_DIR:-/home/pdesjardins/code/trilium-study}"
SERVICE_NAME="${SERVICE_NAME:-trilium-study.service}"
SYNC_DIR="${SYNC_DIR:-${APP_DIR}/.state/prod-sync}"
SKIP_SERVICE_CONTROL="${SKIP_SERVICE_CONTROL:-0}"
FORCE_REGENERATE=0
LIST_ONLY=0
LESSON_IDS=()

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] LESSON_ID [LESSON_ID...]

Run lesson generation locally using this machine's GPU, then sync the updated
production database and workspace artifacts back to the mini-pc.

Options:
  --list            List lessons from the production database and exit
  --force           Regenerate all pipeline stages
  -h, --help        Show this help

Environment overrides (same as deploy.sh):
  REMOTE_HOST       Default: 10.0.0.181
  REMOTE_USER       Default: pdesjardins
  REMOTE_DIR        Default: /home/pdesjardins/code/trilium-study
  SERVICE_NAME      Default: trilium-study.service
  SYNC_DIR          Default: .state/prod-sync
  SKIP_SERVICE_CONTROL  Set to 1 to skip remote stop/start (manual operator control)

Examples:
  $(basename "$0") --list
  $(basename "$0") 42
  $(basename "$0") --force 42 43
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --list)
      LIST_ONLY=1
      shift
      ;;
    --force)
      FORCE_REGENERATE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ ! "$1" =~ ^[0-9]+$ ]]; then
        echo "Expected numeric lesson ID, got: $1" >&2
        exit 1
      fi
      LESSON_IDS+=("$1")
      shift
      ;;
  esac
done

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
REMOTE_STATE="${REMOTE_DIR}/.state"
LOCAL_DB="${SYNC_DIR}/trilium-study.db"
LOCAL_WORKSPACE="${SYNC_DIR}/workspace"
SERVICE_STOPPED=0
SERVICE_STOP_MODE=""
GENERATION_SUCCEEDED=0

cleanup() {
  local exit_code=$?
  if [[ "${SERVICE_STOPPED}" == "1" && "${GENERATION_SUCCEEDED}" != "1" ]]; then
    echo "Generation did not complete successfully; production database was not updated." >&2
  fi
  if [[ "${SERVICE_STOPPED}" == "1" && "${GENERATION_SUCCEEDED}" != "1" ]]; then
    start_remote_service || true
  fi
  exit "${exit_code}"
}
trap cleanup EXIT

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Required command missing: $1" >&2
    exit 1
  fi
}

remote_exec() {
  ssh "${REMOTE}" "$@"
}

prod_env() {
  export DATABASE_URL="sqlite:///${LOCAL_DB}"
  export WORKSPACE_DIR="${LOCAL_WORKSPACE}"
  export LOG_DIR="${SYNC_DIR}/logs"
  if [[ -f "${SYNC_DIR}/youtube-token.json" ]]; then
    export YOUTUBE_TOKEN_FILE="${SYNC_DIR}/youtube-token.json"
  fi
  local secrets
  secrets="$(find "${SYNC_DIR}" -maxdepth 1 -name 'client_secret_*.json' -print -quit || true)"
  if [[ -n "${secrets}" ]]; then
    export YOUTUBE_CLIENT_SECRETS="${secrets}"
  fi
}

pull_prod_db() {
  local remote_backup="${REMOTE_STATE}/trilium-study.sync-backup.db"
  mkdir -p "${SYNC_DIR}"
  remote_exec "test -f '${REMOTE_STATE}/trilium-study.db'"
  echo "Creating consistent SQLite backup on ${REMOTE_HOST}..."
  remote_exec "sqlite3 '${REMOTE_STATE}/trilium-study.db' \".backup '${remote_backup}'\""
  rsync -az "${REMOTE}:${remote_backup}" "${LOCAL_DB}"
  remote_exec "rm -f '${remote_backup}'"
}

pull_youtube_auth() {
  mkdir -p "${SYNC_DIR}"
  rsync -az "${REMOTE}:${REMOTE_STATE}/youtube-token.json" "${SYNC_DIR}/" 2>/dev/null || true
  rsync -az "${REMOTE}:${REMOTE_STATE}/client_secret_"*.json "${SYNC_DIR}/" 2>/dev/null || true
}

pull_lesson_workspaces() {
  local lesson_id workspace_dirs dir
  mkdir -p "${LOCAL_WORKSPACE}"
  mapfile -t workspace_dirs < <(
    cd "${APP_DIR}"
    prod_env
    uv run python -m app.run_lesson --workspace-dirs "${LESSON_IDS[@]}"
  )
  for dir in "${workspace_dirs[@]}"; do
    [[ -n "${dir}" ]] || continue
    remote_exec "mkdir -p '${REMOTE_STATE}/workspace/${dir}'" >/dev/null 2>&1 || true
    rsync -az "${REMOTE}:${REMOTE_STATE}/workspace/${dir}/" "${LOCAL_WORKSPACE}/${dir}/" 2>/dev/null || true
  done
}

push_prod_db() {
  local backup_name="trilium-study.db.$(date +%Y%m%d-%H%M%S).bak"
  remote_exec "cp '${REMOTE_STATE}/trilium-study.db' '${REMOTE_STATE}/${backup_name}'"
  rsync -az "${LOCAL_DB}" "${REMOTE}:${REMOTE_STATE}/trilium-study.db"
}

push_lesson_workspaces() {
  local lesson_id workspace_dirs dir
  mapfile -t workspace_dirs < <(
    cd "${APP_DIR}"
    prod_env
    uv run python -m app.run_lesson --workspace-dirs "${LESSON_IDS[@]}"
  )
  for dir in "${workspace_dirs[@]}"; do
    [[ -n "${dir}" ]] || continue
    if [[ -d "${LOCAL_WORKSPACE}/${dir}" ]]; then
      remote_exec "mkdir -p '${REMOTE_STATE}/workspace/${dir}'"
      rsync -az "${LOCAL_WORKSPACE}/${dir}/" "${REMOTE}:${REMOTE_STATE}/workspace/${dir}/"
    fi
  done
}

remote_app_port_from_prod() {
  remote_exec "awk -F= '/^APP_PORT=/{print \$2}' '${REMOTE_DIR}/.env' | tail -n 1"
}

stop_remote_service() {
  if [[ "${SKIP_SERVICE_CONTROL}" == "1" ]]; then
    echo "SKIP_SERVICE_CONTROL=1; leaving remote service running."
    return
  fi

  echo "Stopping ${SERVICE_NAME} on ${REMOTE_HOST}..."
  if remote_exec "sudo -n systemctl stop ${SERVICE_NAME}" 2>/dev/null; then
    SERVICE_STOPPED=1
    SERVICE_STOP_MODE="systemd"
    return
  fi

  local app_port
  app_port="$(remote_app_port_from_prod)"
  app_port="${app_port:-8083}"
  echo "Passwordless sudo unavailable; stopping listener on port ${app_port}..."
  remote_exec "
    set -euo pipefail
    pids=\$(ss -ltnp '( sport = :${app_port} )' 2>/dev/null | sed -n 's/.*pid=\\([0-9]\\+\\).*/\\1/p' | sort -u)
    if [[ -z \"\${pids}\" ]]; then
      exit 0
    fi
    for pid in \${pids}; do
      kill \"\${pid}\" 2>/dev/null || true
    done
    sleep 1
    for pid in \${pids}; do
      kill -0 \"\${pid}\" 2>/dev/null && kill -9 \"\${pid}\" 2>/dev/null || true
    done
  "
  SERVICE_STOPPED=1
  SERVICE_STOP_MODE="kill"
}

start_remote_service() {
  if [[ "${SKIP_SERVICE_CONTROL}" == "1" ]]; then
    return
  fi

  if [[ "${SERVICE_STOP_MODE}" == "kill" ]]; then
    echo "Waiting for systemd to restart ${SERVICE_NAME}..."
    sleep 6
    if remote_exec "curl -sf http://127.0.0.1:$(remote_app_port_from_prod || echo 8083)/healthz >/dev/null"; then
      echo "Production app is healthy again."
      return
    fi
    echo "Warning: health check failed after automatic restart." >&2
    return
  fi

  echo "Starting ${SERVICE_NAME} on ${REMOTE_HOST}..."
  remote_exec "sudo -n systemctl start ${SERVICE_NAME}"
  remote_exec "sudo -n systemctl --no-pager --full status ${SERVICE_NAME} | sed -n '1,12p'"
}

require_command ssh
require_command rsync
require_command uv

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "Missing ${APP_DIR}/.env" >&2
  exit 1
fi

cd "${APP_DIR}"

echo "Pulling production database from ${REMOTE_HOST}..."
pull_prod_db
pull_youtube_auth

if [[ "${LIST_ONLY}" == "1" ]]; then
  prod_env
  uv run python -m app.run_lesson --list
  GENERATION_SUCCEEDED=1
  exit 0
fi

if [[ "${#LESSON_IDS[@]}" -eq 0 ]]; then
  echo "No lesson IDs provided." >&2
  usage >&2
  exit 1
fi

echo "Pulling existing workspace artifacts for lesson(s): ${LESSON_IDS[*]}"
pull_lesson_workspaces

run_args=(python -m app.run_lesson "${LESSON_IDS[@]}")
if [[ "${FORCE_REGENERATE}" == "1" ]]; then
  run_args=(python -m app.run_lesson --force-regenerate "${LESSON_IDS[@]}")
fi

echo "Running local generation for lesson(s): ${LESSON_IDS[*]}"
prod_env
uv sync --extra tts --extra youtube --quiet
uv run "${run_args[@]}"

echo "Syncing results back to ${REMOTE_HOST}..."
stop_remote_service
push_prod_db
push_lesson_workspaces
start_remote_service
GENERATION_SUCCEEDED=1

echo "Done. View results at http://${REMOTE_HOST}:8083/"
