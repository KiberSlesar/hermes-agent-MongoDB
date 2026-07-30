#!/usr/bin/env bash
# ============================================================================
# Hermes Agent (Mongo) — curl installer
# ============================================================================
# Installs the Mongo fork and forces `hermes` on PATH to that checkout
# (upstream /usr/local/bin/hermes has no `db connect`).
#
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/install-agent.sh | bash
#
# Env / flags:
#   HERMES_HOME              (default: ~/.hermes)
#   HERMES_SKIP_CONNECT=1    skip connect prompt
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
      sed -n '2,20p' "$0" 2>/dev/null || true
      exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say() { echo "${GREEN}→${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }
die() { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

can_prompt() {
  [[ "$SKIP_CONNECT" != "1" ]] && [[ -r /dev/tty ]]
}
ask() {
  local __var="$1" __prompt="$2" __def="${3:-}" __ans=""
  if [[ -n "$__def" ]]; then
    printf "%s" "$__prompt" > /dev/tty
    read -r __ans < /dev/tty || true
    __ans=${__ans:-$__def}
  else
    printf "%s" "$__prompt" > /dev/tty
    read -r __ans < /dev/tty || true
  fi
  printf -v "$__var" '%s' "$__ans"
}

auth_curl() {
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    curl -fsSL -H "Authorization: Bearer ${GH_TOKEN:-$GITHUB_TOKEN}" "$@"
  else
    curl -fsSL "$@"
  fi
}

echo ""
echo "${BOLD}Hermes Agent installer (Mongo)${NC}"
echo "  HERMES_HOME=$HERMES_HOME_DIR"
echo ""

command -v curl >/dev/null || die "curl is required"
command -v tar >/dev/null || die "tar is required"
command -v python3 >/dev/null || die "python3 is required"

mkdir -p "$HERMES_HOME_DIR/certs"
export HERMES_HOME="$HERMES_HOME_DIR"

# Optional base deps (browser tools etc.) — ignore if hermes already present
if [[ $SKIP_HERMES_BASE -eq 0 ]] && ! command -v hermes >/dev/null 2>&1; then
  say "Installing Hermes base runtime (for deps)…"
  curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash -s -- --skip-setup || \
    curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash -s -- --skip-setup || \
    warn "Base installer failed — continuing with Mongo overlay only"
  export PATH="${HOME}/.local/bin:${PATH}"
fi

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

if [[ -d "$AGENT_DIR" ]]; then
  say "Replacing existing checkout at $AGENT_DIR"
  rm -rf "$AGENT_DIR"
fi
mkdir -p "$(dirname "$AGENT_DIR")"
mv "$SRC" "$AGENT_DIR"

# Prefer upstream sealed venv if present (has deps); else create local venv
UPSTREAM_VENV=""
for cand in /usr/local/lib/hermes-agent/venv "$HOME/.local/lib/hermes-agent/venv"; do
  [[ -x "$cand/bin/python" ]] && UPSTREAM_VENV="$cand" && break
done

say "Installing Mongo fork into a dedicated venv…"
if [[ -n "$UPSTREAM_VENV" ]]; then
  # Install editable into upstream venv so imports resolve
  "$UPSTREAM_VENV/bin/pip" install -e "$AGENT_DIR" -q || \
    "$UPSTREAM_VENV/bin/pip" install -e "$AGENT_DIR"
  PY="$UPSTREAM_VENV/bin/python"
elif command -v uv >/dev/null 2>&1; then
  (cd "$AGENT_DIR" && uv venv .venv && uv pip install -e .)
  PY="$AGENT_DIR/.venv/bin/python"
else
  python3 -m venv "$AGENT_DIR/.venv"
  "$AGENT_DIR/.venv/bin/pip" install -U pip -q
  "$AGENT_DIR/.venv/bin/pip" install -e "$AGENT_DIR"
  PY="$AGENT_DIR/.venv/bin/python"
fi

# Force hermes launcher to Mongo fork (upstream binary has no `db connect`)
install_mongo_launcher() {
  local dest="$1"
  mkdir -p "$(dirname "$dest")"
  cat > "$dest" <<EOF
#!/usr/bin/env bash
# Hermes Mongo fork launcher — do not replace with upstream hermes
export HERMES_HOME="\${HERMES_HOME:-$HERMES_HOME_DIR}"
export PYTHONPATH="$AGENT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m hermes_cli.main "\$@"
EOF
  chmod +x "$dest"
}

mkdir -p "$HOME/.local/bin"
install_mongo_launcher "$HOME/.local/bin/hermes"
install_mongo_launcher "$HERMES_HOME_DIR/bin/hermes"
export PATH="$HOME/.local/bin:$HERMES_HOME_DIR/bin:$PATH"

# Shadow /usr/local/bin/hermes when we can (upstream installs there)
if [[ -w /usr/local/bin ]] || command -v sudo >/dev/null 2>&1; then
  if [[ -e /usr/local/bin/hermes ]] && [[ ! -e /usr/local/bin/hermes.upstream ]]; then
    say "Backing up upstream hermes → /usr/local/bin/hermes.upstream"
    if [[ -w /usr/local/bin ]]; then
      cp -a /usr/local/bin/hermes /usr/local/bin/hermes.upstream 2>/dev/null || true
    else
      sudo cp -a /usr/local/bin/hermes /usr/local/bin/hermes.upstream 2>/dev/null || true
    fi
  fi
  if [[ -w /usr/local/bin ]]; then
    install_mongo_launcher /usr/local/bin/hermes
  else
    sudo tee /usr/local/bin/hermes >/dev/null <<EOF
#!/usr/bin/env bash
export HERMES_HOME="\${HERMES_HOME:-$HERMES_HOME_DIR}"
export PYTHONPATH="$AGENT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m hermes_cli.main "\$@"
EOF
    sudo chmod +x /usr/local/bin/hermes
  fi
fi

hash -r 2>/dev/null || true

# Verify Mongo commands exist
if ! "$PY" -m hermes_cli.main db --help >/dev/null 2>&1; then
  die "Mongo hermes_cli failed to load from $AGENT_DIR"
fi
say "hermes → Mongo fork ($AGENT_DIR)"
echo "  which hermes: $(command -v hermes || echo missing)"
if hermes db --help >/dev/null 2>&1; then
  say "Verified: hermes db connect is available"
else
  warn "PATH still points at old hermes — use: $HERMES_HOME_DIR/bin/hermes db connect"
  export PATH="$HERMES_HOME_DIR/bin:$HOME/.local/bin:$PATH"
fi

echo ""
echo "${GREEN}OK Agent installed${NC}"
echo "  Source: $AGENT_DIR"
echo "  Launcher: $(command -v hermes)"
echo ""

curl_enroll() {
  local host="$1" code="$2" name
  host="${host#http://}"; host="${host#https://}"
  name=$(hostname -s 2>/dev/null || hostname || echo agent)
  say "Redeeming code via enroll API (curl fallback)…"
  auth_curl -X POST "http://${host}/enroll" \
    -H "Content-Type: application/json" \
    -d "{\"code\":\"${code}\",\"name\":\"${name}\"}" \
    -o "$TMP/bundle.tar.gz"
  mkdir -p "$TMP/bundle"
  tar -xzf "$TMP/bundle.tar.gz" -C "$TMP/bundle"
  local bdir
  bdir=$(find "$TMP/bundle" -mindepth 1 -maxdepth 1 -type d | head -1)
  [[ -n "$bdir" ]] || die "empty enroll bundle"
  cp "$bdir/bootstrap.yaml" "$HERMES_HOME_DIR/bootstrap.yaml"
  mkdir -p "$HERMES_HOME_DIR/certs"
  cp -f "$bdir/certs/"* "$HERMES_HOME_DIR/certs/" 2>/dev/null || true
  chmod 600 "$HERMES_HOME_DIR/bootstrap.yaml" "$HERMES_HOME_DIR/certs/agent.pem" 2>/dev/null || true
  say "Wrote bootstrap + certs to $HERMES_HOME_DIR"
  # seed if CLI works
  if hermes storage seed >/dev/null 2>&1; then
    hermes storage seed || true
  elif "$PY" -m hermes_cli.main storage seed >/dev/null 2>&1; then
    "$PY" -m hermes_cli.main storage seed || true
  fi
  hermes storage status 2>/dev/null || "$PY" -m hermes_cli.main storage status || true
}

do_connect() {
  local host="$1" code="$2"
  export PATH="$HERMES_HOME_DIR/bin:$HOME/.local/bin:$PATH"
  hash -r 2>/dev/null || true

  if [[ -z "$host" ]] && can_prompt; then
    ask host "Control-plane address (IP:8743): " "127.0.0.1:8743"
  fi
  if [[ -z "$code" ]] && can_prompt; then
    ask code "One-time code: "
  fi
  [[ -n "$host" && -n "$code" ]] || die "Need host and code (or run interactively on a TTY)"

  if hermes db connect --help >/dev/null 2>&1; then
    hermes db connect --host "$host" --code "$code" && return 0
  fi
  if "$PY" -m hermes_cli.main db connect --help >/dev/null 2>&1; then
    "$PY" -m hermes_cli.main db connect --host "$host" --code "$code" && return 0
  fi
  curl_enroll "$host" "$code"
}

if [[ -n "$ENROLL_HOST" || -n "$ENROLL_CODE" ]]; then
  [[ -n "$ENROLL_HOST" && -n "$ENROLL_CODE" ]] || die "--host and --code are both required"
  do_connect "$ENROLL_HOST" "$ENROLL_CODE"
  exit 0
fi

if [[ "$SKIP_CONNECT" == "1" && "$FORCE_CONNECT" -eq 0 ]]; then
  echo "Next: hermes db connect"
  echo "  or: $HERMES_HOME_DIR/bin/hermes db connect"
  exit 0
fi

if [[ "$FORCE_CONNECT" -eq 1 ]]; then
  do_connect "" ""
  exit 0
fi

if ! can_prompt; then
  warn "No TTY — run: hermes db connect --host IP:8743 --code ABCD-EFGH"
  echo "  Launcher: $HERMES_HOME_DIR/bin/hermes"
  exit 0
fi

ask ans "Connect this PC to Hermes DB now? [Y/n] " "Y"
if [[ "$ans" =~ ^[Yy] ]]; then
  echo "Enter values from the DB server (agent-add):"
  do_connect "" ""
else
  echo "Later: hermes db connect"
  echo "  $HERMES_HOME_DIR/bin/hermes db connect"
fi
echo ""
