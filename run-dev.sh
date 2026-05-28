#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

env_value() {
  local key="$1"
  local default="$2"
  local value
  value="$(awk -F= -v key="$key" '$1 == key { sub(/^[^=]+=/, ""); print }' .env 2>/dev/null | tail -n 1)"
  value="${value%\"}"
  value="${value#\"}"
  value="${value%\'}"
  value="${value#\'}"
  if [[ -n "${value}" ]]; then
    printf '%s\n' "${value}"
  else
    printf '%s\n' "${default}"
  fi
}

app_host="$(env_value APP_HOST 0.0.0.0)"
app_port="$(env_value APP_PORT 8000)"
app_base_url="$(env_value APP_BASE_URL "")"

if [[ -z "${app_base_url}" ]]; then
  if [[ "${app_host}" == "0.0.0.0" || "${app_host}" == "::" ]]; then
    app_base_url="http://127.0.0.1:${app_port}"
  else
    app_base_url="http://${app_host}:${app_port}"
  fi
fi

echo "Starting Trilium Study dev server"
echo "  Config: APP_HOST=${app_host} APP_PORT=${app_port}"
echo "  Open:   ${app_base_url}"
if [[ "${app_host}" == "0.0.0.0" || "${app_host}" == "::" ]]; then
  lan_ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  if [[ -n "${lan_ip}" ]]; then
    echo "  LAN:    http://${lan_ip}:${app_port}"
  fi
fi
echo

exec uv run python -m app.serve --reload
