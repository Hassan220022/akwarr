#!/usr/bin/env bash
set -euo pipefail
: "${JELLYFIN_URL:?}"
: "${JELLYFIN_API_KEY:?}"

urlencode() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$1"
}

library_has_path() {
  local name="$1"
  local path="$2"
  curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" \
    "${JELLYFIN_URL}/Library/VirtualFolders" \
    | python3 -c '
import json
import sys

name, path = sys.argv[1], sys.argv[2]
for lib in json.load(sys.stdin):
    if lib.get("Name") == name and path in (lib.get("Locations") or []):
        raise SystemExit(0)
raise SystemExit(1)
' "$name" "$path"
}

library_exists() {
  local name="$1"
  curl -sf -H "X-Emby-Token: ${JELLYFIN_API_KEY}" \
    "${JELLYFIN_URL}/Library/VirtualFolders" \
    | python3 -c '
import json
import sys

name = sys.argv[1]
for lib in json.load(sys.stdin):
    if lib.get("Name") == name:
        raise SystemExit(0)
raise SystemExit(1)
' "$name"
}

add_lib() {
  local name="$1"
  local type="$2"
  local path="$3"
  local encoded_name
  local encoded_path
  if library_exists "${name}"; then
    echo "Library exists: ${name}"
    return 0
  fi
  encoded_name="$(urlencode "${name}")"
  encoded_path="$(urlencode "${path}")"
  curl -sS -X POST \
    -H "X-Emby-Token: ${JELLYFIN_API_KEY}" \
    -H "Content-Type: application/json" \
    -d '{"LibraryOptions":{"PreferredMetadataLanguage":"ar","SaveLocalMetadata":true}}' \
	    "${JELLYFIN_URL}/Library/VirtualFolders?name=${encoded_name}&collectionType=${type}&paths=${encoded_path}&refreshLibrary=true" \
    && echo "Created/updated library: ${name}" || echo "Library ${name} may already exist"
}

ensure_path() {
  local name="$1"
  local path="$2"
  local encoded_name
  encoded_name="$(urlencode "${name}")"
  if library_has_path "${name}" "${path}"; then
    echo "Library path OK: ${name} -> ${path}"
    return 0
  fi
  curl -sS -X POST \
    -H "X-Emby-Token: ${JELLYFIN_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"Name\":\"${name}\",\"Path\":\"${path}\"}" \
    "${JELLYFIN_URL}/Library/VirtualFolders/Paths?name=${encoded_name}" \
    && echo "Added library path: ${name} -> ${path}"
}

remove_path_if_present() {
  local name="$1"
  local path="$2"
  local encoded_name
  local encoded_path
  encoded_name="$(urlencode "${name}")"
  encoded_path="$(urlencode "${path}")"
  if ! library_has_path "${name}" "${path}"; then
    return 0
  fi
  curl -sS -X DELETE \
    -H "X-Emby-Token: ${JELLYFIN_API_KEY}" \
    "${JELLYFIN_URL}/Library/VirtualFolders/Paths?name=${encoded_name}&path=${encoded_path}" \
    && echo "Removed stale library path: ${name} -> ${path}"
}

add_lib "Arabic Movies" "movies" "/cc/Movie/Arabic"
add_lib "Arabic Series" "tvshows" "/cc/Serries/Arabic"
ensure_path "Arabic Movies" "/cc/Movie/Arabic"
ensure_path "Arabic Series" "/cc/Serries/Arabic"
remove_path_if_present "Arabic Movies" "/cc/arabic/movies"
remove_path_if_present "Arabic Series" "/cc/arabic/series"
