#!/usr/bin/env bash
# Run lesson generation on this workstation (local GPU) and write results to the
# production database and workspace on the mini-pc.
#
# The production service is stopped for the entire run so the production
# database cannot receive writes that the final whole-database push would lose.
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
LIST_COURSES=0
COURSE_ID=""
LESSON_IDS=()

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] [LESSON_ID...]

Run lesson generation locally using this machine's GPU, then sync the updated
production database and workspace artifacts back to the mini-pc. The remote
service is stopped for the whole run and restarted at the end.

Options:
  --list            List lessons from the production database and exit
  --list-courses    List courses with pending lesson counts and exit
  --course ID       Sync the Trilium catalog, then generate every pending
                    (not yet generated) lesson of the given course
  --force           Regenerate all pipeline stages
  -h, --help        Show this help

Environment overrides (same as deploy.sh):
  REMOTE_HOST       Default: 10.0.0.181
  REMOTE_USER       Default: pdesjardins
  REMOTE_DIR        Default: /home/pdesjardins/code/trilium-study
  SERVICE_NAME      Default: trilium-study.service
  SYNC_DIR          Default: .state/prod-sync
  SKIP_SERVICE_CONTROL  Set to 1 to skip remote stop/start (manual operator control)
  ALLOW_ARCHIVAL    Set to 1 to proceed when the catalog sync archives active courses

Exit codes:
  0  all lessons completed (or nothing to do)
  2  run finished but at least one lesson failed; completed work was pushed
  1  fatal error; production was not updated

Examples:
  $(basename "$0") --list-courses
  $(basename "$0") --course 3
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
    --list-courses)
      LIST_COURSES=1
      shift
      ;;
    --course)
      if [[ $# -lt 2 || ! "$2" =~ ^[0-9]+$ ]]; then
        echo "--course requires a numeric course ID" >&2
        exit 1
      fi
      COURSE_ID="$2"
      shift 2
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

if [[ -n "${COURSE_ID}" && "${#LESSON_IDS[@]}" -gt 0 ]]; then
  echo "Provide either --course or explicit lesson IDs, not both." >&2
  exit 1
fi

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
REMOTE_STATE="${REMOTE_DIR}/.state"
LOCAL_DB="${SYNC_DIR}/trilium-study.db"
LOCAL_WORKSPACE="${SYNC_DIR}/workspace"
RUN_LOG="${SYNC_DIR}/last-run.log"
SERVICE_STOPPED=0
SERVICE_STOP_MODE=""
SERVICE_RESTARTED=0
DB_PUSHED=0

cleanup() {
  local exit_code=$?
  if [[ "${SERVICE_STOPPED}" == "1" && "${SERVICE_RESTARTED}" != "1" ]]; then
    if [[ "${DB_PUSHED}" != "1" ]]; then
      echo "Run aborted before push; production database was NOT updated." >&2
    fi
    start_remote_service || echo "WARNING: failed to restart ${SERVICE_NAME}; restart it manually." >&2
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
  if [[ ! -f "${SYNC_DIR}/youtube-token.json" ]]; then
    echo "Warning: youtube-token.json not found on ${REMOTE_HOST}; upload stages will fail." >&2
  fi
}

pull_lesson_workspaces() {
  local workspace_dirs dir
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
  DB_PUSHED=1
}

push_lesson_workspaces() {
  local workspace_dirs dir
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

# The app runs `alembic upgrade head` at startup. If we push a database whose
# alembic stamp is newer than the migrations present in the production code
# tree, the service crash-loops. Refuse to run until production code is
# deployed with every migration this working tree has.
ensure_remote_code_has_local_migrations() {
  local local_revs remote_revs missing
  local_revs="$(cd "${APP_DIR}/migrations/versions" && ls -1 -- *.py | sort)"
  remote_revs="$(remote_exec "ls -1 '${REMOTE_DIR}/migrations/versions/'*.py 2>/dev/null | xargs -rn1 basename" | sort || true)"
  missing="$(comm -23 <(printf '%s\n' "${local_revs}") <(printf '%s\n' "${remote_revs}"))"
  if [[ -n "${missing}" ]]; then
    echo "Production code is missing migration(s) that exist locally:" >&2
    printf '  %s\n' ${missing} >&2
    echo "Run ./deploy.sh first so production can open the database this run will push." >&2
    exit 1
  fi
}

stop_remote_service() {
  if [[ "${SKIP_SERVICE_CONTROL}" == "1" ]]; then
    echo "SKIP_SERVICE_CONTROL=1; leaving remote service running."
    return 0
  fi

  echo "Stopping ${SERVICE_NAME} on ${REMOTE_HOST} for the duration of the run..."
  if remote_exec "sudo -n systemctl stop ${SERVICE_NAME}" 2>/dev/null; then
    SERVICE_STOPPED=1
    SERVICE_STOP_MODE="systemd"
    return 0
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
    return 0
  fi

  if [[ "${SERVICE_STOP_MODE}" == "kill" ]]; then
    echo "Waiting for systemd to restart ${SERVICE_NAME}..."
    sleep 6
    if remote_exec "curl -sf http://127.0.0.1:$(remote_app_port_from_prod || echo 8083)/healthz >/dev/null"; then
      echo "Production app is healthy again."
      return 0
    fi
    echo "Warning: health check failed after automatic restart." >&2
    return 0
  fi

  echo "Starting ${SERVICE_NAME} on ${REMOTE_HOST}..."
  remote_exec "sudo -n systemctl start ${SERVICE_NAME}"
  remote_exec "sudo -n systemctl --no-pager --full status ${SERVICE_NAME} | sed -n '1,12p'"
}

print_run_summary() {
  local completed failed
  completed="$(sed -n 's/^LESSON_RESULT \([0-9]\+\) completed$/\1/p' "${RUN_LOG}" | tr '\n' ' ')"
  failed="$(sed -n 's/^LESSON_RESULT \([0-9]\+\) failed$/\1/p' "${RUN_LOG}" | tr '\n' ' ')"
  echo ""
  echo "Run summary:"
  echo "  Completed: ${completed:-none}"
  echo "  Failed:    ${failed:-none}"
}

require_command ssh
require_command rsync
require_command uv

if [[ ! -f "${APP_DIR}/.env" ]]; then
  echo "Missing ${APP_DIR}/.env" >&2
  exit 1
fi

cd "${APP_DIR}"

# Read-only modes: no service stop needed (the sqlite3 .backup is consistent
# against a live database).
if [[ "${LIST_ONLY}" == "1" || "${LIST_COURSES}" == "1" ]]; then
  echo "Pulling production database from ${REMOTE_HOST}..."
  pull_prod_db
  prod_env
  if [[ "${LIST_COURSES}" == "1" ]]; then
    uv run python -m app.run_lesson --list-courses
  else
    uv run python -m app.run_lesson --list
  fi
  exit 0
fi

if [[ -z "${COURSE_ID}" && "${#LESSON_IDS[@]}" -eq 0 ]]; then
  echo "Provide --course COURSE_ID or lesson IDs." >&2
  usage >&2
  exit 1
fi

# Prepare everything cheap before taking production down.
ensure_remote_code_has_local_migrations
echo "Syncing local Python dependencies..."
# --inexact keeps other installed extras (e.g. dev) instead of removing them.
uv sync --inexact --extra tts --extra youtube --quiet

stop_remote_service

echo "Pulling production database from ${REMOTE_HOST}..."
pull_prod_db
pull_youtube_auth
prod_env

if [[ -n "${COURSE_ID}" ]]; then
  echo "Syncing Trilium catalog into the production database copy..."
  uv run python -m app.run_lesson --sync-catalog | tee "${SYNC_DIR}/last-sync.log"
  archived_courses="$(sed -n 's/^SYNC_ARCHIVED_COURSES \([0-9]\+\)$/\1/p' "${SYNC_DIR}/last-sync.log")"
  if [[ "${archived_courses:-0}" -gt 0 && "${ALLOW_ARCHIVAL:-0}" != "1" ]]; then
    echo "Catalog sync archived ${archived_courses} previously active course(s); aborting without pushing." >&2
    echo "Verify TRILIUM_PARENT_NOTE_ID in .env, or rerun with ALLOW_ARCHIVAL=1 if intentional." >&2
    exit 1
  fi

  echo "Resolving pending lessons for course ${COURSE_ID}..."
  # Write to a file instead of process substitution so a resolver failure
  # (course missing/archived) aborts the run under set -e.
  uv run python -m app.run_lesson --course "${COURSE_ID}" --print-ids > "${SYNC_DIR}/pending-ids.txt"
  mapfile -t LESSON_IDS < "${SYNC_DIR}/pending-ids.txt"
  if [[ "${#LESSON_IDS[@]}" -eq 0 ]]; then
    echo "No pending lessons for course ${COURSE_ID}; pushing catalog sync results back."
    push_prod_db
    start_remote_service
    SERVICE_RESTARTED=1
    echo "Done. View results at http://${REMOTE_HOST}:8083/"
    exit 0
  fi
  echo "Pending lesson(s): ${LESSON_IDS[*]}"
fi

echo "Pulling existing workspace artifacts for lesson(s): ${LESSON_IDS[*]}"
pull_lesson_workspaces

run_args=(python -m app.run_lesson)
if [[ "${FORCE_REGENERATE}" == "1" ]]; then
  run_args+=(--force-regenerate)
fi
run_args+=("${LESSON_IDS[@]}")

echo "Running local generation for lesson(s): ${LESSON_IDS[*]}"
set +e
uv run "${run_args[@]}" 2>&1 | tee "${RUN_LOG}"
RUN_RC=${PIPESTATUS[0]}
set -e

case "${RUN_RC}" in
  0|2) ;;
  *)
    echo "Generation aborted fatally (exit ${RUN_RC}); production was not updated." >&2
    exit "${RUN_RC}"
    ;;
esac

echo "Syncing results back to ${REMOTE_HOST}..."
push_prod_db
push_lesson_workspaces
start_remote_service
SERVICE_RESTARTED=1

print_run_summary
echo "Done. View results at http://${REMOTE_HOST}:8083/"
exit "${RUN_RC}"
