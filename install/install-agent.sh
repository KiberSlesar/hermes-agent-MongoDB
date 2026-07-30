#!/usr/bin/env bash
# ============================================================================
# Hermes Agent (Mongo) — curl installer
# ============================================================================
# No git clone. Installs Hermes with Mongo remote storage, then offers
# to connect this PC to the DB (one-time code from installDB / agent-add).
#
#   curl -fsSL https://raw.githubusercontent.com/<user>/<repo>/main/install/install-agent.sh | bash
#
# Private repo:
#   curl -fsSL -H "Authorization: Bearer $GH_TOKEN" \
#     https://raw.githubusercontent.com/<user>/<repo>/main/install/install-agent.sh | bash
#
# Env / flags:
#   HERMES_HOME              (default: ~/.hermes)
#   HERMES_MONGO_REPO        (default: KiberSlesar/hermes-agent-MongoDB-private)
#   HERMES_MONGO_REF         (default: main)
#   HERMES_SKIP_CONNECT=1    skip connect prompt
#   bash -s -- --connect     force connect wizard after install
#   bash -s -- --host IP:8743 --code ABCD-EFGH
# ============================================================================
set -euo pipefail

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
REPO="${HERMES_MONGO_REPO:-KiberSlesar/hermes-agent-MongoDB-private}"
REF="${HERMES_MONGO_REF:-main}"
SKIP_CONNECT="${HERMES_SKIP_CONNECT:-0}"
FORCE_CONNECT=0
ENROLL_HOST=""
ENROLL_CODE=""
SKIP_HERMES_BASE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --connect) FORCE_CONNECT=1; shift ;;
    --host) ENROLL_HOST="$2"; shift 2 ;;
    --code) ENROLL_CODE="$2"; shift 2 ;;
    --skip-base) SKIP_HERMES_BASE=1; shift ;;
    --hermes-home) HERMES_HOME_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,30p' "$0" 2>/dev/null || true
      exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say() { echo "${GREEN}→${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }
die() { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

auth_curl() {
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    curl -fsSL -H "Authorization: Bearer ${GH_TOKEN:-$GITHUB_TOKEN}" "$@"
  else
    curl -fsSL "$@"
  fi
}

echo ""
echo "${BOLD}⚕ Hermes Agent installer (Mongo)${NC}"
echo "  HERMES_HOME=$HERMES_HOME_DIR"
echo ""

command -v curl >/dev/null || die "curl is required"
command -v tar >/dev/null || die "tar is required"

mkdir -p "$HERMES_HOME_DIR/certs"
export HERMES_HOME="$HERMES_HOME_DIR"

# --- Base Hermes runtime (uv / launcher) if missing ---
if [[ $SKIP_HERMES_BASE -eq 0 ]] && ! command -v hermes >/dev/null 2>&1; then
  say "Installing Hermes base runtime…"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup || \
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup || \
    warn "Base installer failed — continuing with Mongo overlay only"
  # reload PATH hints
  export PATH="${HOME}/.local/bin:${PATH}"
  [[ -f "$HOME/.bashrc" ]] && source "$HOME/.bashrc" 2>/dev/null || true
fi

# --- Overlay: this MongoDB fork checkout ---
AGENT_DIR="$HERMES_HOME_DIR/hermes-agent"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

say "Downloading Mongo fork (${REPO}@${REF})…"
if ! auth_curl -L "https://api.github.com/repos/${REPO}/tarball/${REF}" -o "$TMP/src.tgz"; then
  die "Could not download ${REPO}. Set GH_TOKEN for a private repo."
fi
mkdir -p "$TMP/out"
tar -xzf "$TMP/src.tgz" -C "$TMP/out"
SRC=$(find "$TMP/out" -mindepth 1 -maxdepth 1 -type d | head -1)
[[ -n "$SRC" ]] || die "empty archive"

# Preserve local bootstrap/certs; replace agent source
mkdir -p "$AGENT_DIR"
# Move aside old tree
if [[ -d "$AGENT_DIR/.git" ]] || [[ -f "$AGENT_DIR/pyproject.toml" ]]; then
  say "Replacing existing checkout at $AGENT_DIR"
  rm -rf "$AGENT_DIR"
fi
mkdir -p "$(dirname "$AGENT_DIR")"
mv "$SRC" "$AGENT_DIR"

say "Installing Python package (editable)…"
if command -v uv >/dev/null 2>&1; then
  (cd "$AGENT_DIR" && uv pip install -e ".[all]" 2>/dev/null || uv pip install -e .) || \
    (cd "$AGENT_DIR" && python3 -m pip install -e .)
elif command -v python3 >/dev/null 2>&1; then
  (cd "$AGENT_DIR" && python3 -m pip install -e .)
else
  die "python3/uv required"
fi

# Ensure hermes on PATH points at this install when possible
if [[ -x "$AGENT_DIR/.venv/bin/hermes" ]]; then
  mkdir -p "$HOME/.local/bin"
  ln -sfn "$AGENT_DIR/.venv/bin/hermes" "$HOME/.local/bin/hermes"
  export PATH="$HOME/.local/bin:$PATH"
fi

echo ""
echo "${GREEN}✓ Agent installed${NC}"
echo "  Source: $AGENT_DIR"
echo ""

do_connect() {
  local host="$1" code="$2"
  if command -v hermes >/dev/null 2>&1 && hermes db connect --help >/dev/null 2>&1; then
    if [[ -n "$host" && -n "$code" ]]; then
      hermes db connect --host "$host" --code "$code"
    else
      hermes db connect
    fi
    return
  fi
  # Fallback: pure curl enroll (no hermes CLI yet)
  [[ -n "$host" && -n "$code" ]] || die "Need --host and --code (hermes CLI not ready)"
  say "Redeeming code via enroll API…"
  local name
  name=$(hostname -s 2>/dev/null || hostname || echo agent)
  auth_curl -X POST "http://${host}/enroll" \
    -H "Content-Type: application/json" \
    -d "{\"code\":\"${code}\",\"name\":\"${name}\"}" \
    -o "$TMP/bundle.tar.gz"
  mkdir -p "$TMP/bundle"
  tar -xzf "$TMP/bundle.tar.gz" -C "$TMP/bundle"
  local bdir
  bdir=$(find "$TMP/bundle" -mindepth 1 -maxdepth 1 -type d | head -1)
  cp "$bdir/bootstrap.yaml" "$HERMES_HOME_DIR/bootstrap.yaml"
  mkdir -p "$HERMES_HOME_DIR/certs"
  cp -f "$bdir/certs/"* "$HERMES_HOME_DIR/certs/" 2>/dev/null || true
  chmod 600 "$HERMES_HOME_DIR/bootstrap.yaml" "$HERMES_HOME_DIR/certs/agent.pem" 2>/dev/null || true
  say "Wrote bootstrap + certs to $HERMES_HOME_DIR"
  if command -v hermes >/dev/null 2>&1; then
    hermes storage status || true
  fi
}

if [[ -n "$ENROLL_HOST" || -n "$ENROLL_CODE" ]]; then
  [[ -n "$ENROLL_HOST" && -n "$ENROLL_CODE" ]] || die "--host and --code are both required"
  do_connect "$ENROLL_HOST" "$ENROLL_CODE"
  exit 0
fi

if [[ "$SKIP_CONNECT" == "1" && "$FORCE_CONNECT" -eq 0 ]]; then
  echo "Next: hermes db connect"
  exit 0
fi

if [[ ! -t 0 && "$FORCE_CONNECT" -eq 0 ]]; then
  warn "Non-interactive — run: hermes db connect"
  exit 0
fi

if [[ "$FORCE_CONNECT" -eq 1 ]]; then
  do_connect "" ""
  exit 0
fi

read -r -p "Connect this PC to Hermes DB now? [Y/n] " ans || ans=Y
ans=${ans:-Y}
if [[ "$ans" =~ ^[Yy] ]]; then
  echo "Enter values from the DB server (installDB / agent-add):"
  do_connect "" ""
else
  echo "Later: hermes db connect"
fi
echo ""
