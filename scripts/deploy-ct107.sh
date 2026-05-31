#!/usr/bin/env bash
set -euo pipefail
SSH_TARGET="${SSH_TARGET:-media}"
LOCAL_SRC="${LOCAL_SRC:-/Users/mikawi/Developer/akwarr}"
REMOTE_DIR="/opt/akwarr"

: "${AKWARR_API_KEY:?export AKWARR_API_KEY=...}"
: "${TMDB_API_KEY:?export TMDB_API_KEY=...}"
: "${JELLYFIN_API_KEY:?export JELLYFIN_API_KEY=...}"
: "${JELLYSEERR_API_KEY:?export JELLYSEERR_API_KEY=...}"
export JELLYFIN_URL="${JELLYFIN_URL:-http://192.168.1.20:8096}"

log() { printf '[deploy] %s\n' "$*"; }
run_remote() { ssh -o BatchMode=yes "${SSH_TARGET}" "$@"; }
optional_setup_env=""
if [[ -n "${TOTAL_BANDWIDTH_MBIT:-}" ]]; then
  optional_setup_env+=" TOTAL_BANDWIDTH_MBIT=${TOTAL_BANDWIDTH_MBIT}"
fi
if [[ -n "${ARIA2_BANDWIDTH_LIMIT_PERCENT:-}" ]]; then
  optional_setup_env+=" ARIA2_BANDWIDTH_LIMIT_PERCENT=${ARIA2_BANDWIDTH_LIMIT_PERCENT}"
fi
if [[ -n "${ARIA2_MAX_OVERALL_DOWNLOAD_LIMIT:-}" ]]; then
  optional_setup_env+=" ARIA2_MAX_OVERALL_DOWNLOAD_LIMIT=${ARIA2_MAX_OVERALL_DOWNLOAD_LIMIT}"
fi

log "Syncing -> ${SSH_TARGET}:${REMOTE_DIR}"
run_remote "mkdir -p ${REMOTE_DIR}"
rsync -az --delete \
  --exclude '.venv' --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '.git' \
  "${LOCAL_SRC}/" "${SSH_TARGET}:${REMOTE_DIR}/"

log "Running setup-homelab.sh on CT107"
run_remote "AKWARR_DIR=${REMOTE_DIR} AKWARR_API_KEY=${AKWARR_API_KEY} TMDB_API_KEY=${TMDB_API_KEY} JELLYFIN_URL=${JELLYFIN_URL} JELLYFIN_API_KEY=${JELLYFIN_API_KEY} JELLYSEERR_API_KEY=${JELLYSEERR_API_KEY}${optional_setup_env} bash ${REMOTE_DIR}/scripts/setup-homelab.sh"

log "Configuring Jellyfin libraries on CT113 (via LAN)"
bash "${LOCAL_SRC}/scripts/configure-jellyfin.sh"

log "Verification"
run_remote "docker ps --format 'table {{.Names}}\t{{.Status}}' | grep -E 'akwarr|NAMES'"
run_remote "curl -sf -H 'X-Api-Key: ${AKWARR_API_KEY}' http://127.0.0.1:7879/api/v3/system/status"
