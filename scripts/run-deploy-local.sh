#!/usr/bin/env bash
# Run from your Mac Terminal (not Cursor sandbox). Requires env vars — see DEPLOYMENT.md
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${LOG:-/tmp/akwarr-deploy.log}"

: "${AKWARR_API_KEY:?export AKWARR_API_KEY=...}"
: "${TMDB_API_KEY:?export TMDB_API_KEY=...}"
: "${JELLYFIN_API_KEY:?export JELLYFIN_API_KEY=...}"
: "${JELLYSEERR_API_KEY:?export JELLYSEERR_API_KEY=...}"
export JELLYFIN_URL="${JELLYFIN_URL:-http://192.168.1.20:8096}"

exec > >(tee -a "$LOG") 2>&1
echo "=== deploy $(date) ==="
bash "${ROOT}/scripts/deploy-ct107.sh"
bash "${ROOT}/scripts/test-homelab.sh"
echo "=== done $(date) ==="
