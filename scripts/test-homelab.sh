#!/usr/bin/env bash
# Post-deploy verification — run on Mac (needs LAN) or CT107
set -uo pipefail

: "${AKWARR_API_KEY:?Set AKWARR_API_KEY}"
: "${JELLYSEERR_API_KEY:?Set JELLYSEERR_API_KEY}"
JELLYFIN_URL="${JELLYFIN_URL:-http://192.168.1.20:8096}"
JELLYFIN_API_KEY="${JELLYFIN_API_KEY:-}"
SSH_TARGET="${SSH_TARGET:-media}"
export JELLYFIN_URL JELLYFIN_API_KEY JELLYSEERR_API_KEY

pass=0
fail=0

check() {
  local name="$1"
  shift
  printf '\n=== %s ===\n' "$name"
  if "$@"; then
    echo "PASS: $name"
    pass=$((pass + 1))
  else
    echo "FAIL: $name"
    fail=$((fail + 1))
  fi
}

check "SSH to CT107" ssh -o BatchMode=yes -o ConnectTimeout=10 "${SSH_TARGET}" "echo ok"

check "Docker akwarr containers" ssh -o BatchMode=yes "${SSH_TARGET}" \
  "docker ps --format '{{.Names}}' | grep -q akwarr-radarr && docker ps --format '{{.Names}}' | grep -q akwarr-sonarr"

check "Akwarr Radarr API" ssh -o BatchMode=yes "${SSH_TARGET}" \
  "curl -sf -H 'X-Api-Key: ${AKWARR_API_KEY}' http://127.0.0.1:7879/api/v3/system/status | grep -q Akwarr"

check "Akwarr Sonarr API" ssh -o BatchMode=yes "${SSH_TARGET}" \
  "curl -sf -H 'X-Api-Key: ${AKWARR_API_KEY}' http://127.0.0.1:8990/api/v3/system/status | grep -q Akwarr"

check "Radarr root folder" ssh -o BatchMode=yes "${SSH_TARGET}" \
  "curl -sf -H 'X-Api-Key: ${AKWARR_API_KEY}' http://127.0.0.1:7879/api/v3/rootfolder | grep -q '/media/Movie/Arabic'"

check "Arabic media dirs" ssh -o BatchMode=yes "${SSH_TARGET}" \
  "test -d /media/Movie/Arabic && test -d /media/Serries/Arabic"

if [[ -n "${JELLYFIN_API_KEY}" ]]; then
  check "Jellyfin API" bash -c \
    'curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" "${JELLYFIN_URL}/System/Info/Public" >/dev/null'

  check "Jellyfin Arabic libraries" bash -c \
    'curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" "${JELLYFIN_URL}/Library/VirtualFolders" | grep -q "Arabic Movies" && curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" "${JELLYFIN_URL}/Library/VirtualFolders" | grep -q "/cc/Movie/Arabic" && curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" "${JELLYFIN_URL}/Library/VirtualFolders" | grep -q "Arabic Series" && curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" "${JELLYFIN_URL}/Library/VirtualFolders" | grep -q "/cc/Serries/Arabic"'
fi

check "Jellyseerr Radarr settings" bash -c \
  'curl -sf -H "X-Api-Key: ${JELLYSEERR_API_KEY}" "https://jellyseerr.mikawi.org/api/v1/settings/radarr" | grep -qi "akwarr-radarr"'

check "Jellyseerr Sonarr settings" bash -c \
  'curl -sf -H "X-Api-Key: ${JELLYSEERR_API_KEY}" "https://jellyseerr.mikawi.org/api/v1/settings/sonarr" | grep -qi "akwarr-sonarr"'

check "TMDB lookup via Akwarr" ssh -o BatchMode=yes "${SSH_TARGET}" \
  "curl -sf -H 'X-Api-Key: ${AKWARR_API_KEY}' 'http://127.0.0.1:7879/api/v3/movie/lookup?term=tmdb:792307' | grep -q tmdbId"

printf '\n\n========== RESULT: %s passed, %s failed ==========\n' "$pass" "$fail"
exit $(( fail > 0 ? 1 : 0 ))
