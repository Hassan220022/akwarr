#!/usr/bin/env bash
# One-shot Akwarr + Jellyseerr setup for CT107
# Usage on CT107:
#   export AKWARR_API_KEY=... TMDB_API_KEY=... JELLYFIN_API_KEY=... JELLYSEERR_API_KEY=...
#   bash scripts/setup-homelab.sh
set -euo pipefail

AKWARR_DIR="${AKWARR_DIR:-/opt/akwarr}"
REPO="${AKWARR_REPO:-https://github.com/Hassan220022/akwarr.git}"
JELLYFIN_URL="${JELLYFIN_URL:-http://192.168.1.20:8096}"
JELLYSEERR_URL="${JELLYSEERR_URL:-http://127.0.0.1:5055}"

: "${AKWARR_API_KEY:?Set AKWARR_API_KEY}"
: "${TMDB_API_KEY:?Set TMDB_API_KEY}"
: "${JELLYFIN_API_KEY:?Set JELLYFIN_API_KEY}"
: "${JELLYSEERR_API_KEY:?Set JELLYSEERR_API_KEY}"

log() { printf '\n[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

require() { command -v "$1" >/dev/null 2>&1 || { echo "Missing: $1"; exit 1; }; }
require docker
require git
require curl
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "Missing: docker compose or docker-compose"
  exit 1
fi

log "Creating Arabic media folders"
mkdir -p /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging
chown -R 1000:1000 /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging 2>/dev/null || true
chmod -R 775 /media/Movie/Arabic /media/Serries/Arabic /media/Download/akwarr-staging
if [[ -d /media/arabic/movies ]]; then
  find /media/arabic/movies -mindepth 1 -maxdepth 1 -exec mv -n {} /media/Movie/Arabic/ \; 2>/dev/null || true
fi
if [[ -d /media/arabic/series ]]; then
  find /media/arabic/series -mindepth 1 -maxdepth 1 -exec mv -n {} /media/Serries/Arabic/ \; 2>/dev/null || true
fi

log "Cloning/updating Akwarr at ${AKWARR_DIR}"
if [[ -d "${AKWARR_DIR}/.git" ]]; then
  git -C "${AKWARR_DIR}" pull --ff-only || true
elif [[ ! -e "${AKWARR_DIR}" ]] || [[ -z "$(find "${AKWARR_DIR}" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
  git clone "${REPO}" "${AKWARR_DIR}"
else
  log "Using existing synced Akwarr directory at ${AKWARR_DIR}"
fi

log "Writing ${AKWARR_DIR}/.env"
cat >"${AKWARR_DIR}/.env" <<EOF
AKWARR_API_KEY=${AKWARR_API_KEY}
TMDB_API_KEY=${TMDB_API_KEY}
METADATA_LANGUAGE=ar
SAVE_AKWAM_ARTWORK=true
JELLYFIN_URL=${JELLYFIN_URL}
JELLYFIN_API_KEY=${JELLYFIN_API_KEY}
JELLYFIN_MOVIES_LIBRARY_NAME="Arabic Movies"
JELLYFIN_SERIES_LIBRARY_NAME="Arabic Series"
AKWAM_BASE=https://akwam.it
FLARESOLVERR_URL=http://flaresolverr:8191/v1
FLARESOLVERR_ENABLE=true
FLARESOLVERR_AUTO=true
ARIA2_RPC_URL=http://akwarr-aria2:6800/jsonrpc
ARIA2_SECRET=${ARIA2_SECRET:-P3TERX}
PREFERRED_QUALITIES=720p,1080p,480p
MOVIES_PATH=/media/Movie/Arabic
SERIES_PATH=/media/Serries/Arabic
STAGING_PATH=/media/Download/akwarr-staging
EOF

log "Starting Akwarr stack"
cd "${AKWARR_DIR}"
export MEDIA_ROOT=/media
export COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-akwarr}"
if [[ -z "${MEDIA_NETWORK_NAME:-}" ]] \
  && ! docker network inspect media_network >/dev/null 2>&1 \
  && docker network inspect media-stack_media_network >/dev/null 2>&1; then
  export MEDIA_NETWORK_NAME=media-stack_media_network
fi
export MEDIA_NETWORK_NAME="${MEDIA_NETWORK_NAME:-media_network}"
for volume in "${COMPOSE_PROJECT_NAME}_akwarr-radarr-config" "${COMPOSE_PROJECT_NAME}_akwarr-sonarr-config"; do
  docker volume create "${volume}" >/dev/null
  volume_path="$(docker volume inspect -f '{{ .Mountpoint }}' "${volume}")"
  chown -R 1000:1000 "${volume_path}"
done
"${COMPOSE[@]}" -f docker-compose.yml -f docker-compose.media-stack.yml up -d --build

log "Waiting for Akwarr APIs"
for _ in $(seq 1 30); do
  if curl -sf -H "X-Api-Key: ${AKWARR_API_KEY}" http://127.0.0.1:7879/api/v3/system/status >/dev/null \
    && curl -sf -H "X-Api-Key: ${AKWARR_API_KEY}" http://127.0.0.1:8990/api/v3/system/status >/dev/null; then
    break
  fi
  sleep 2
done

curl -sf -H "X-Api-Key: ${AKWARR_API_KEY}" http://127.0.0.1:7879/api/v3/system/status >/dev/null

add_servarr() {
  local kind="$1"
  local payload="$2"
  local label="$3"
  local existing
  local existing_id
  existing=$(curl -sS -H "X-Api-Key: ${JELLYSEERR_API_KEY}" "${JELLYSEERR_URL}/api/v1/settings/${kind}" || echo "[]")
  existing_id=$(python3 -c '
import json
import sys

label = sys.argv[1].lower()
try:
    data = json.loads(sys.stdin.read() or "[]")
except json.JSONDecodeError:
    data = []
for item in data:
    name = str(item.get("name") or "").lower()
    host = str(item.get("hostname") or "").lower()
    if label.lower() in name or "akwarr" in host:
        print(item.get("id") or "")
        break
' "${label}" <<<"${existing}")
  if [[ -n "${existing_id}" ]]; then
    log "Updating ${label}"
    curl -sS -X PUT \
      -H "X-Api-Key: ${JELLYSEERR_API_KEY}" \
      -H "Content-Type: application/json" \
      -d "${payload}" \
      "${JELLYSEERR_URL}/api/v1/settings/${kind}/${existing_id}"
    echo
    return 0
  fi
  log "Adding ${label}"
  curl -sS -X POST \
    -H "X-Api-Key: ${JELLYSEERR_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "${payload}" \
    "${JELLYSEERR_URL}/api/v1/settings/${kind}"
  echo
}

RADARR_PAYLOAD=$(cat <<JSON
{"name":"Radarr (Arabic)","hostname":"akwarr-radarr","port":7879,"apiKey":"${AKWARR_API_KEY}","useSsl":false,"baseUrl":"","activeProfileId":1,"activeProfileName":"Arabic 720p","activeDirectory":"/media/Movie/Arabic","minimumAvailability":"released","is4k":false,"isDefault":false,"enableSearch":true,"tagRequests":false,"syncEnabled":true}
JSON
)
SONARR_PAYLOAD=$(cat <<JSON
{"name":"Sonarr (Arabic)","hostname":"akwarr-sonarr","port":8990,"apiKey":"${AKWARR_API_KEY}","useSsl":false,"baseUrl":"","activeProfileId":1,"activeProfileName":"Arabic 720p","activeLanguageProfileId":1,"activeDirectory":"/media/Serries/Arabic","animeSeriesType":"standard","seriesType":"standard","enableSeasonFolders":true,"is4k":false,"isDefault":false,"enableSearch":true,"tagRequests":false,"syncEnabled":true}
JSON
)

add_servarr radarr "${RADARR_PAYLOAD}" "Radarr (Arabic)"
add_servarr sonarr "${SONARR_PAYLOAD}" "Sonarr (Arabic)"

log "Jellyfin libraries (run on Mac if CT113 reachable)"
if [[ -x "${AKWARR_DIR}/scripts/configure-jellyfin.sh" ]] && curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" "${JELLYFIN_URL}/System/Info/Public" >/dev/null 2>&1; then
  JELLYFIN_URL="${JELLYFIN_URL}" JELLYFIN_API_KEY="${JELLYFIN_API_KEY}" bash "${AKWARR_DIR}/scripts/configure-jellyfin.sh" || true
fi

log "Setup complete"
