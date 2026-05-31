#!/usr/bin/env bash
# Publish Akwarr to GitHub (run locally after gh auth login)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v gh >/dev/null; then
  echo "Install GitHub CLI: https://cli.github.com/"
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "Run: gh auth login"
  exit 1
fi

if [[ ! -d .git ]]; then
  git init -b main
fi

git add -A
if git diff --cached --quiet; then
  echo "Nothing to commit."
else
  git commit -m "Initial Akwarr release: Jellyseerr shim for Arabic Akwam media."
fi

if git remote get-url origin >/dev/null 2>&1; then
  echo "Remote origin already set."
else
  gh repo create akwarr --public --source=. --remote=origin --description "Radarr/Sonarr API shim for Jellyseerr — Arabic Akwam downloads for Jellyfin"
fi

git push -u origin main
echo "Done: $(gh repo view --json url -q .url)"
