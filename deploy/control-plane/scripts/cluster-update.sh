#!/usr/bin/env bash
# DB-side fleet update without requiring a Mongo-agent bootstrap.
#
# Downloads client tarball, refreshes control-plane scripts, publishes
# hermes_shared.fleet_release. Agents then run: hermes update
#
#   HERMES_DB_HOME=~/hermes-db \
#     ./scripts/cluster-update.sh --version 0.19.7 --ref main
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export HERMES_DB_HOME="${HERMES_DB_HOME:-$ROOT}"
export HERMES_CONTROL_DIR="${HERMES_CONTROL_DIR:-$HERMES_DB_HOME}"

VERSION="${HERMES_FLEET_VERSION:-}"
REF="${HERMES_MONGO_REF:-main}"
REPO="${HERMES_MONGO_REPO:-KiberSlesar/hermes-agent-MongoDB}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: cluster-update.sh [--version X] [--ref main] [--repo owner/repo]"
      exit 0
      ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

if [[ -f "$HERMES_DB_HOME/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_DB_HOME/.env"
  set +a
fi

if [[ -z "$VERSION" && -f "$HERMES_DB_HOME/hermes-agent/hermes_cli/__init__.py" ]]; then
  VERSION="$(python3 -c "
import re, pathlib
t = pathlib.Path('$HERMES_DB_HOME/hermes-agent/hermes_cli/__init__.py').read_text(encoding='utf-8', errors='replace')
m = re.search(r'^__version__\\s*=\\s*[\"\\']([^\"\\']+)[\"\\']', t, re.M)
print(m.group(1) if m else '')
" 2>/dev/null || true)"
fi
[[ -n "$VERSION" ]] || { echo "ERROR: set --version or HERMES_FLEET_VERSION" >&2; exit 1; }

RELEASE_DIR="$HERMES_DB_HOME/releases/$REF"
mkdir -p "$RELEASE_DIR"
TGZ="$RELEASE_DIR/src.tgz"
echo "-> Downloading $REPO@$REF …"
URL1="https://codeload.github.com/${REPO}/tar.gz/${REF}"
URL2="https://api.github.com/repos/${REPO}/tarball/${REF}"
if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
  curl -fsSL -H "Authorization: Bearer ${GH_TOKEN:-$GITHUB_TOKEN}" -L "$URL2" -o "$TGZ" \
    || curl -fsSL -L "$URL1" -o "$TGZ"
else
  curl -fsSL -L "$URL1" -o "$TGZ" || curl -fsSL -L "$URL2" -o "$TGZ"
fi

SCRIPTS="$HERMES_DB_HOME/scripts"
echo "-> Refreshing control-plane scripts in $SCRIPTS …"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
tar -xzf "$TGZ" -C "$TMP"
# Copy deploy/control-plane/scripts/*
found=$(find "$TMP" -type d -path '*/deploy/control-plane/scripts' | head -1 || true)
if [[ -n "$found" ]]; then
  mkdir -p "$SCRIPTS"
  cp -a "$found"/. "$SCRIPTS"/
  chmod +x "$SCRIPTS"/*.sh 2>/dev/null || true
fi

export HERMES_CONTROL_DIR="$HERMES_DB_HOME"
export HERMES_MONGO_REF="$REF"
export HERMES_MONGO_REPO="$REPO"
echo "-> Publishing fleet_release ${VERSION}@${REF} …"
python3 "$SCRIPTS/publish_fleet_release.py" \
  --version "$VERSION" \
  --ref "$REF" \
  --repo "$REPO" \
  --published-by cluster-update

# Optional: restart orch/enroll if units exist
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user restart hermes-orchestrator hermes-enroll 2>/dev/null || true
fi

echo "OK. On each agent PC run: hermes update"
echo "  artifact: $TGZ"
