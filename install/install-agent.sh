#!/usr/bin/env bash
# ============================================================================
# Hermes Agent (Mongo) вЂ” curl installer
# ============================================================================
# Installs the Mongo fork and forces `hermes` on PATH to that checkout
# (upstream /usr/local/bin/hermes has no `db connect`).
#
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh | bash
#
# Env / flags:
#   HERMES_HOME              (default: ~/.hermes)
#   HERMES_SKIP_CONNECT=1    skip connect prompt
#   HERMES_YES=1             replace existing install without prompting
#   HTTPS_PROXY / HTTP_PROXY / TELEGRAM_PROXY — GitHub tarball egress
#   bash -s -- --yes --host IP:8743 --code ABCD-EFGH
# ============================================================================
set -euo pipefail

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"

# Load local .env (TELEGRAM_PROXY etc.) then promote messaging proxy → HTTPS_PROXY
# so curl can reach GitHub on boxes that only have Telegram egress configured.
if [[ -f "$HERMES_HOME_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$HERMES_HOME_DIR/.env" 2>/dev/null || true
  set +a
fi
if [[ -z "${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}" ]]; then
  _msg_proxy="${TELEGRAM_PROXY:-${DISCORD_PROXY:-${GATEWAY_PROXY_URL:-}}}"
  if [[ -n "$_msg_proxy" ]]; then
    export HTTPS_PROXY="$_msg_proxy"
    export HTTP_PROXY="$_msg_proxy"
    export https_proxy="$_msg_proxy"
    export http_proxy="$_msg_proxy"
  fi
  unset _msg_proxy
fi
if [[ -n "${HTTPS_PROXY:-${HTTP_PROXY:-}}" && -z "${NO_PROXY:-${no_proxy:-}}" ]]; then
  export NO_PROXY="127.0.0.1,localhost,::1,192.168.88.33,192.168.88.44,.local"
  export no_proxy="$NO_PROXY"
fi

REPO="${HERMES_MONGO_REPO:-KiberSlesar/hermes-agent-MongoDB}"
# Old private clone name was renamed; rewrite stale env overrides.
if [[ "$REPO" == "KiberSlesar/hermes-agent-MongoDB-private" ]]; then
  REPO="KiberSlesar/hermes-agent-MongoDB"
fi
REF="${HERMES_MONGO_REF:-main}"
SKIP_CONNECT="${HERMES_SKIP_CONNECT:-0}"
YES="${HERMES_YES:-0}"
FORCE_CONNECT=0
ENROLL_HOST=""
ENROLL_CODE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --connect) FORCE_CONNECT=1; shift ;;
    --host) ENROLL_HOST="$2"; shift 2 ;;
    --code) ENROLL_CODE="$2"; shift 2 ;;
    --yes|-y) YES=1; shift ;;
    # Kept as a compatibility no-op: this installer is self-contained and
    # never invokes the upstream Hermes installer.
    --skip-base) shift ;;
    --hermes-home) HERMES_HOME_DIR="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,20p' "$0" 2>/dev/null || true
      exit 0 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say() { echo "${GREEN}в†’${NC} $*" >&2; }
warn() { echo "${YELLOW}!${NC} $*" >&2; }
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

confirm_replace_existing_installation() {
  local existing=""
  existing="$(command -v hermes 2>/dev/null || true)"
  [[ -n "$existing" || -d "$AGENT_DIR" ]] || return 0

  echo "An existing Hermes installation was found:" >&2
  [[ -n "$existing" ]] && echo "  command: $existing" >&2
  [[ -d "$AGENT_DIR" ]] && echo "  checkout: $AGENT_DIR" >&2
  echo "Replacing it removes its launcher/runtime only." >&2
  echo "HERMES_HOME data and the existing DB connection (bootstrap.yaml + certs) are kept." >&2
  if [[ "$YES" == "1" ]]; then
    say "HERMES_YES=1 / --yes: replacing existing install"
  elif can_prompt; then
    local answer=""
    printf "%s" "Update / replace existing Hermes runtime? [y/N] " > /dev/tty
    read -r answer < /dev/tty || true
    if [[ ! "$answer" =~ ^[Yy]$ ]]; then
      echo "Installation cancelled; existing Hermes was left unchanged." >&2
      exit 0
    fi
  else
    die "Existing Hermes found. Re-run interactively, or set HERMES_YES=1 / pass --yes."
  fi

  [[ -d "$AGENT_DIR" ]] && rm -rf "$AGENT_DIR"
  if [[ -n "$existing" && -e "$existing" ]]; then
    if [[ -w "$(dirname "$existing")" ]]; then
      rm -f "$existing"
    else
      sudo rm -f "$existing"
    fi
  fi
}

auth_curl() {
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    # A stale token must not make a public installation fail. Retain the
    # authenticated attempt for private forks, then retry anonymously.
    curl -fsSL -H "Authorization: Bearer ${GH_TOKEN:-$GITHUB_TOKEN}" "$@" || \
      curl -fsSL "$@"
    return
  fi
  curl -fsSL "$@"
}

# Avoid getcwd failures when the shell's cwd was deleted (e.g. old checkout).
cd "${HOME:-/}" >/dev/null 2>&1 || cd / >/dev/null 2>&1 || true

echo ""
echo "${BOLD}Hermes Agent installer (Mongo)${NC}"
echo "  HERMES_HOME=$HERMES_HOME_DIR"
echo ""

command -v curl >/dev/null || die "curl is required"
command -v tar >/dev/null || die "tar is required"
command -v python3 >/dev/null || die "python3 is required"

mkdir -p "$HERMES_HOME_DIR/certs"
export HERMES_HOME="$HERMES_HOME_DIR"

AGENT_DIR="$HERMES_HOME_DIR/hermes-agent"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

say "Downloading Mongo fork (${REPO}@${REF})вЂ¦"
if ! auth_curl -L "https://api.github.com/repos/${REPO}/tarball/${REF}" -o "$TMP/src.tgz"; then
  die "Could not download ${REPO}. Set GH_TOKEN for a private repo."
fi
mkdir -p "$TMP/out"
tar -xzf "$TMP/src.tgz" -C "$TMP/out"
SRC=$(find "$TMP/out" -mindepth 1 -maxdepth 1 -type d | head -1)
[[ -n "$SRC" ]] || die "empty archive"

confirm_replace_existing_installation
mkdir -p "$(dirname "$AGENT_DIR")"
mv "$SRC" "$AGENT_DIR"

ensure_venv_support() {
  if python3 -c "import ensurepip" 2>/dev/null; then
    return 0
  fi
  say "Installing python3-venv (ensurepip missing)вЂ¦"
  . /etc/os-release 2>/dev/null || true
  local ver
  ver="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq "python${ver}-venv" python3-venv python3-pip || \
      sudo apt-get install -y -qq python3-venv python3-pip || true
  fi
  python3 -c "import ensurepip" 2>/dev/null || return 1
  return 0
}

ensure_uv() {
  export PATH="${HOME}/.local/bin:${PATH}"
  command -v uv >/dev/null 2>&1 && return 0
  say "Installing uvвЂ¦"
  # Must not leak installer text into command substitutions (pick_python).
  if ! curl -fsSL https://astral.sh/uv/install.sh | sh >/dev/null 2>&1; then
    curl -fsSL https://astral.sh/uv/install.sh | sh >&2 || return 1
  fi
  export PATH="${HOME}/.local/bin:${PATH}"
  # shellcheck disable=SC1090
  [[ -f "$HOME/.local/bin/env" ]] && source "$HOME/.local/bin/env" 2>/dev/null || true
  command -v uv >/dev/null 2>&1
}

py_majmin() {
  local bin="$1"
  [[ -n "$bin" ]] || { echo "0.0"; return 0; }
  "$bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0"
}

# Prefer a supported interpreter when system Python is 3.14+ (many deps lag).
# ONLY the interpreter path is printed on stdout.
pick_python() {
  local sysver want
  sysver="$(py_majmin python3)"
  if python3 -c "import sys; raise SystemExit(0 if sys.version_info < (3,14) else 1)" 2>/dev/null; then
    echo "python3"
    return 0
  fi
  if ensure_uv; then
    say "System Python is ${sysver} вЂ” installing CPython 3.12 via uvвЂ¦"
    uv python install 3.12 >/dev/null >&2
    want="$(uv python find 3.12 2>/dev/null | tail -n1 | tr -d '\r')"
    if [[ -n "$want" && -x "$want" ]]; then
      echo "$want"
      return 0
    fi
  fi
  echo "python3"
}

say "Installing Mongo fork into a dedicated venvвЂ¦"
rm -rf "$AGENT_DIR/.venv" 2>/dev/null || true

# Install uv once up-front (logs on stderr only)
ensure_uv || true

PY_BASE="$(pick_python | tail -n1 | tr -d '\r')"
[[ -n "$PY_BASE" ]] || PY_BASE="python3"
say "Using interpreter: $PY_BASE ($(py_majmin "$PY_BASE"))"

# Slow / flaky PyPI links exceed uv's 30s default.
export UV_HTTP_TIMEOUT="${UV_HTTP_TIMEOUT:-180}"
export UV_HTTP_RETRIES="${UV_HTTP_RETRIES:-10}"
export UV_REQUEST_TIMEOUT="${UV_REQUEST_TIMEOUT:-$UV_HTTP_TIMEOUT}"

if command -v uv >/dev/null 2>&1; then
  say "uv pip install (HTTP timeout=${UV_HTTP_TIMEOUT}s, retries=${UV_HTTP_RETRIES})"
  pip_ok=0
  for attempt in 1 2 3 4 5; do
    if (cd "$AGENT_DIR" && uv venv .venv --python "$PY_BASE" && uv pip install -e .); then
      pip_ok=1
      break
    fi
    warn "uv pip install failed (attempt $attempt/5); retrying…"
    sleep $((5 * attempt))
  done
  [[ "$pip_ok" -eq 1 ]] || die "uv pip install failed after retries (PyPI timeout/network). Retry with UV_HTTP_TIMEOUT=300 UV_HTTP_RETRIES=15"
  PY="$AGENT_DIR/.venv/bin/python"
elif ensure_venv_support && "$PY_BASE" -m venv "$AGENT_DIR/.venv"; then
  "$AGENT_DIR/.venv/bin/pip" install -U pip -q
  "$AGENT_DIR/.venv/bin/pip" install -e "$AGENT_DIR"
  PY="$AGENT_DIR/.venv/bin/python"
else
  warn "venv unavailable вЂ” installing with pip --user"
  "$PY_BASE" -m pip install -U pip --user -q || true
  "$PY_BASE" -m pip install -e "$AGENT_DIR" --user
  PY="$PY_BASE"
fi

[[ -x "$PY" ]] || die "No working Python for Hermes ($PY)"
"$PY" -c "import hermes_cli" 2>/dev/null || \
  PYTHONPATH="$AGENT_DIR${PYTHONPATH:+:$PYTHONPATH}" "$PY" -c "import hermes_cli" || \
  die "hermes_cli import failed after install"

# Force hermes launcher to Mongo fork (upstream binary has no `db connect`)
install_mongo_launcher() {
  local dest="$1"
  mkdir -p "$(dirname "$dest")"
  local tmp
  tmp="$(mktemp)"
  cat > "$tmp" <<EOF
#!/usr/bin/env bash
# Hermes Mongo fork launcher вЂ” replaces upstream hermes
export HERMES_HOME="\${HERMES_HOME:-$HERMES_HOME_DIR}"
cd "$AGENT_DIR" || exit 1
export PYTHONPATH="$AGENT_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec "$PY" -m hermes_cli.main "\$@"
EOF
  chmod +x "$tmp"
  if [[ -w "$(dirname "$dest")" ]] || [[ -w "$dest" ]]; then
    mv -f "$tmp" "$dest"
  else
    sudo mv -f "$tmp" "$dest"
    sudo chmod +x "$dest"
  fi
}

mkdir -p "$HOME/.local/bin" "$HERMES_HOME_DIR/bin"
install_mongo_launcher "$HOME/.local/bin/hermes"
install_mongo_launcher "$HERMES_HOME_DIR/bin/hermes"

# Always overwrite the PATH-winning hermes (usually /usr/local/bin/hermes)
HERMES_ON_PATH="$(command -v hermes 2>/dev/null || true)"
if [[ -n "$HERMES_ON_PATH" ]]; then
  if [[ "$HERMES_ON_PATH" != "$HOME/.local/bin/hermes" && "$HERMES_ON_PATH" != "$HERMES_HOME_DIR/bin/hermes" ]]; then
    say "Replacing PATH hermes at $HERMES_ON_PATH with Mongo launcher"
    if [[ -e "${HERMES_ON_PATH}.upstream" ]]; then
      :
    elif [[ -e "$HERMES_ON_PATH" ]]; then
      if [[ -w "$(dirname "$HERMES_ON_PATH")" ]]; then
        cp -a "$HERMES_ON_PATH" "${HERMES_ON_PATH}.upstream" 2>/dev/null || true
      else
        sudo cp -a "$HERMES_ON_PATH" "${HERMES_ON_PATH}.upstream" 2>/dev/null || true
      fi
    fi
    install_mongo_launcher "$HERMES_ON_PATH"
  fi
fi
# Also force /usr/local/bin/hermes even if not currently on PATH resolution quirks
if [[ -e /usr/local/bin/hermes ]] || [[ -d /usr/local/bin ]]; then
  install_mongo_launcher /usr/local/bin/hermes || true
fi

export PATH="$HERMES_HOME_DIR/bin:$HOME/.local/bin:/usr/local/bin:$PATH"
hash -r 2>/dev/null || true

# HARD REQUIREMENT: hermes db connect + hermes mongo status must work after install
verify_db_connect() {
  "$HERMES_HOME_DIR/bin/hermes" db connect --help >/dev/null 2>&1 && return 0
  "$HOME/.local/bin/hermes" db connect --help >/dev/null 2>&1 && return 0
  hermes db connect --help >/dev/null 2>&1 && return 0
  "$PY" -m hermes_cli.main db connect --help >/dev/null 2>&1 && return 0
  return 1
}

verify_mongo_status() {
  "$HERMES_HOME_DIR/bin/hermes" mongo status --help >/dev/null 2>&1 && return 0
  "$HOME/.local/bin/hermes" mongo status --help >/dev/null 2>&1 && return 0
  hermes mongo status --help >/dev/null 2>&1 && return 0
  "$PY" -m hermes_cli.main mongo status --help >/dev/null 2>&1 && return 0
  return 1
}

if ! "$PY" -c "import hermes_cli.main, hermes_storage, hermes_cli.mongo_cmds" 2>/dev/null; then
  die "Mongo packages failed to import via $PY — pip install -e likely failed"
fi

if ! verify_db_connect; then
  echo ""
  echo "DEBUG:"
  echo "  which hermes = $(command -v hermes || echo none)"
  echo "  hermes --help (first lines):"
  hermes --help 2>&1 | head -20 || true
  die "hermes db connect still missing after install — this is a bug, installer aborting"
fi

if ! verify_mongo_status; then
  echo ""
  echo "DEBUG:"
  echo "  which hermes = $(command -v hermes || echo none)"
  hermes mongo --help 2>&1 | head -20 || true
  die "hermes mongo status still missing after install — this is a bug, installer aborting"
fi

say "Verified: hermes db connect + hermes mongo status"

ensure_systemd_linger() {
  command -v loginctl >/dev/null 2>&1 || return 0
  local user
  user="$(id -un)"
  if loginctl show-user "$user" --property=Linger --value 2>/dev/null | grep -qi '^yes$'; then
    say "Systemd linger already enabled for $user"
    return 0
  fi
  say "Enabling systemd linger for $user (keeps gateway alive after logout/reboot)…"
  if loginctl enable-linger "$user" 2>/dev/null; then
    say "Systemd linger enabled for $user"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo -n loginctl enable-linger "$user" 2>/dev/null; then
    say "Systemd linger enabled for $user (via sudo)"
    return 0
  fi
  if command -v sudo >/dev/null 2>&1 && sudo loginctl enable-linger "$user" 2>/dev/null; then
    say "Systemd linger enabled for $user (via sudo)"
    return 0
  fi
  warn "Could not enable systemd linger automatically."
  warn "Run: sudo loginctl enable-linger $user"
  warn "Without linger, hermes-gateway.service dies when the user session ends."
}

if [[ "$(uname -s)" == "Linux" ]] && command -v systemctl >/dev/null 2>&1; then
  ensure_systemd_linger
fi

# Stamp version/ref for fleet auto-update compare.
VER="$($PY -c "from hermes_cli import __version__; print(__version__)" 2>/dev/null || true)"
mkdir -p "$HERMES_HOME_DIR"
cat > "$HERMES_HOME_DIR/.fleet_install_stamp" <<STAMP
{
  "version": "${VER}",
  "ref": "${REF}",
  "repo": "${REPO}",
  "applied_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date)"
}
STAMP

echo "  which hermes: $(command -v hermes)"
echo "  try: hermes mongo status"
echo "  try: hermes db connect --help"

echo ""
echo "${GREEN}OK Agent installed (Mongo)${NC}"
echo "  Source:   $AGENT_DIR"
echo "  Launcher: $(command -v hermes)"
echo "  Status:   hermes mongo status"
echo ""

curl_enroll() {
  local host="$1" code="$2" name
  host="${host#http://}"; host="${host#https://}"
  name=$(hostname -s 2>/dev/null || hostname || echo agent)
  say "Redeeming code via enroll API (curl fallback)вЂ¦"
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

already_connected=0
if [[ -f "$HERMES_HOME_DIR/bootstrap.yaml" ]]; then
  already_connected=1
  echo ""
  echo "Existing DB connection found:"
  echo "  $HERMES_HOME_DIR/bootstrap.yaml"
  [[ -f "$HERMES_HOME_DIR/certs/agent.pem" ]] && echo "  $HERMES_HOME_DIR/certs/agent.pem"
  echo "It was not removed by the update. Choose n unless you want a new one-time code."
fi

if [[ "$already_connected" -eq 1 ]]; then
  ask ans "Connect again with a new code? [y/N] " "N"
else
  ask ans "Connect this PC to Hermes DB now? [Y/n] " "Y"
fi
if [[ "$ans" =~ ^[Yy] ]]; then
  echo "Enter values from the DB server (agent-add):"
  do_connect "" ""
else
  if [[ "$already_connected" -eq 1 ]]; then
    echo "Keeping existing DB connection."
  else
    echo "Later: hermes db connect"
    echo "  $HERMES_HOME_DIR/bin/hermes db connect"
  fi
fi
echo ""
