#!/usr/bin/env bash
# ============================================================================
# Hermes DB — self-hosted installer (NO Docker)
# ============================================================================
# Native MongoDB + systemd user services for enroll (:8743) and
# mTLS orchestrator (:8744). Ubuntu/Debian.
#
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/installDB.sh | bash
#
# Env (non-interactive):
#   HERMES_LISTEN_MODE=lo|lan|wan     where to listen / advertise
#   HERMES_ADVERTISE_HOST=1.2.3.4     override advertised IP/DNS
#   HERMES_DB_HOME=~/hermes-db
#   HERMES_SKIP_CONNECT=1
# ============================================================================
set -euo pipefail

HERMES_DB_HOME="${HERMES_DB_HOME:-$HOME/hermes-db}"
SKIP_CONNECT="${HERMES_SKIP_CONNECT:-0}"
REPO="${HERMES_MONGO_REPO:-KiberSlesar/hermes-agent-MongoDB-private}"
REF="${HERMES_MONGO_REF:-main}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say() { echo "${GREEN}→${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }
die() { echo "${RED}ERROR:${NC} $*" >&2; exit 1; }

# curl|bash leaves stdin as the pipe — prompts must use the real terminal.
can_prompt() {
  [[ "$SKIP_CONNECT" != "1" ]] && [[ -r /dev/tty ]]
}
ask() {
  # usage: ask VAR "prompt" [default]
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

guess_lan_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)
  fi
  [[ -z "$ip" ]] && ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  echo "${ip:-127.0.0.1}"
}

guess_wan_ip() {
  local ip=""
  ip=$(curl -4 -fsS --max-time 4 https://ifconfig.me/ip 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -4 -fsS --max-time 4 https://api.ipify.org 2>/dev/null || true)
  [[ -z "$ip" ]] && ip=$(curl -4 -fsS --max-time 4 https://icanhazip.com 2>/dev/null | tr -d '[:space:]' || true)
  echo "${ip}"
}

# Sets: MODE, BIND_IP, ADVERTISE_HOST, LISTEN_HINT
resolve_listen_mode() {
  local lan wan mode ans custom
  lan=$(guess_lan_ip)
  wan=$(guess_wan_ip)

  mode="${HERMES_LISTEN_MODE:-}"
  mode=$(echo "$mode" | tr '[:upper:]' '[:lower:]')

  if [[ -z "$mode" ]]; then
    if can_prompt; then
      echo ""
      echo "${BOLD}Where should Mongo / enroll / orchestrator listen?${NC}"
      echo "  1) LO   - only this machine (127.0.0.1)"
      echo "  2) LAN  - local network (${lan})"
      if [[ -n "$wan" ]]; then
        echo "  3) WAN  - internet / public IP (${wan})"
      else
        echo "  3) WAN  - internet / public IP (enter manually)"
      fi
      echo "  4) Custom host/IP"
      ask ans "Choice [1/2/3/4] (default 2=LAN): " "2"
      case "$ans" in
        1|lo|LO) mode=lo ;;
        3|wan|WAN) mode=wan ;;
        4|custom|CUSTOM)
          ask custom "Advertise host/IP: "
          [[ -n "$custom" ]] || die "empty custom host"
          HERMES_ADVERTISE_HOST="$custom"
          mode=custom
          ;;
        *) mode=lan ;;
      esac
    else
      mode=lan
      warn "No TTY — default listen mode: LAN (${lan}). Set HERMES_LISTEN_MODE=lo|lan|wan"
    fi
  fi

  case "$mode" in
    lo|localhost|local)
      MODE=lo
      BIND_IP=127.0.0.1
      ADVERTISE_HOST="${HERMES_ADVERTISE_HOST:-127.0.0.1}"
      LISTEN_HINT="localhost only"
      ;;
    lan|localnet)
      MODE=lan
      BIND_IP=0.0.0.0
      ADVERTISE_HOST="${HERMES_ADVERTISE_HOST:-$lan}"
      LISTEN_HINT="LAN (${ADVERTISE_HOST})"
      ;;
    wan|public|internet)
      MODE=wan
      BIND_IP=0.0.0.0
      if [[ -n "${HERMES_ADVERTISE_HOST:-}" ]]; then
        ADVERTISE_HOST="$HERMES_ADVERTISE_HOST"
      elif [[ -n "$wan" ]]; then
        ADVERTISE_HOST="$wan"
        if can_prompt; then
          ask custom "Public IP to advertise [${wan}]: " "$wan"
          ADVERTISE_HOST="$custom"
        fi
      else
        can_prompt || die "WAN mode needs HERMES_ADVERTISE_HOST=..."
        ask ADVERTISE_HOST "Public IP / DNS to advertise: "
        [[ -n "$ADVERTISE_HOST" ]] || die "empty WAN host"
      fi
      LISTEN_HINT="WAN (${ADVERTISE_HOST}) - open firewall 27017,8743,8744"
      ;;
    custom)
      MODE=custom
      BIND_IP=0.0.0.0
      ADVERTISE_HOST="${HERMES_ADVERTISE_HOST}"
      LISTEN_HINT="custom (${ADVERTISE_HOST})"
      ;;
    *)
      die "Unknown HERMES_LISTEN_MODE='$mode' (use lo|lan|wan)"
      ;;
  esac

  say "Listen mode: ${MODE} — bind ${BIND_IP}, advertise ${ADVERTISE_HOST}"
}

echo ""
echo "${BOLD}⚕ Hermes DB installer (self-hosted, no Docker)${NC}"
echo ""

command -v openssl >/dev/null || die "openssl required"
command -v python3 >/dev/null || die "python3 required"
command -v curl >/dev/null || die "curl required"
command -v systemctl >/dev/null || die "systemd required"
command -v sudo >/dev/null || die "sudo required (to apt-install mongodb-org)"

resolve_listen_mode

# --- Fetch control-plane scripts into HERMES_DB_HOME ---
SRC=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
  HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [[ -d "$HERE/../deploy/control-plane" ]] && SRC="$(cd "$HERE/../deploy/control-plane" && pwd)"
fi

mkdir -p "$HERMES_DB_HOME"
if [[ -n "$SRC" ]]; then
  say "Using local control-plane: $SRC"
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --exclude '.env' --exclude 'data' --exclude 'logs' \
      --exclude 'certs/*' --exclude 'bundles/*' --exclude 'enroll_pending/*' \
      "$SRC/" "$HERMES_DB_HOME/"
  else
    tar -C "$SRC" --exclude='.env' --exclude='data' --exclude='logs' \
      --exclude='certs' --exclude='bundles' --exclude='enroll_pending' \
      -cf - . | tar -C "$HERMES_DB_HOME" -xf -
  fi
else
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  say "Downloading control-plane from GitHub…"
  auth_curl -L "https://api.github.com/repos/${REPO}/tarball/${REF}" -o "$TMP/pack.tgz" \
    || auth_curl -L "https://codeload.github.com/${REPO}/tar.gz/${REF}" -o "$TMP/pack.tgz"
  mkdir -p "$TMP/out"
  tar -xzf "$TMP/pack.tgz" -C "$TMP/out"
  CP=$(find "$TMP/out" -type d -path '*/deploy/control-plane' | head -1)
  [[ -n "$CP" ]] || die "deploy/control-plane missing in archive"
  tar -C "$CP" -cf - . | tar -C "$HERMES_DB_HOME" -xf -
fi
mkdir -p "$HERMES_DB_HOME/certs" "$HERMES_DB_HOME/bundles" "$HERMES_DB_HOME/enroll_pending" \
  "$HERMES_DB_HOME/data" "$HERMES_DB_HOME/logs"
chmod +x "$HERMES_DB_HOME/scripts/"*.sh 2>/dev/null || true

upsert_env() {
  local f="$1" k="$2" v="$3"
  if grep -q "^${k}=" "$f" 2>/dev/null; then
    sed -i.bak "s|^${k}=.*|${k}=${v}|" "$f"
  else
    echo "${k}=${v}" >> "$f"
  fi
  rm -f "${f}.bak"
}

write_env_listen_vars() {
  local f="$HERMES_DB_HOME/.env"
  upsert_env "$f" HERMES_LISTEN_MODE "$MODE"
  upsert_env "$f" HERMES_MONGO_BIND "$BIND_IP"
  upsert_env "$f" HERMES_LISTEN_BIND "$BIND_IP"
  upsert_env "$f" HERMES_ADVERTISE_HOST "$ADVERTISE_HOST"
  upsert_env "$f" HERMES_MONGO_HOSTS "${ADVERTISE_HOST}:27017"
  upsert_env "$f" HERMES_MONGO_RS_HOST "${ADVERTISE_HOST}:27017"
  upsert_env "$f" HERMES_ORCHESTRATOR_URL "https://${ADVERTISE_HOST}:8744"
  upsert_env "$f" HERMES_MONGO_PORT "27017"
}

if [[ ! -f "$HERMES_DB_HOME/.env" ]]; then
  cat > "$HERMES_DB_HOME/.env" <<EOF
MONGO_ROOT_USER=hermesRoot
MONGO_ROOT_PASSWORD=$(openssl rand -hex 16)
HERMES_ENROLL_TOKEN=$(openssl rand -hex 24)
HERMES_REPLICA_SET=rs0
EOF
  chmod 600 "$HERMES_DB_HOME/.env"
  say "Wrote $HERMES_DB_HOME/.env"
else
  say "Using existing $HERMES_DB_HOME/.env (updating listen/advertise)"
fi
write_env_listen_vars
set -a && source "$HERMES_DB_HOME/.env" && set +a

say "Generating CA / server certificates…"
export HERMES_CERT_EXTRA_SAN="IP:${ADVERTISE_HOST}"
# Also include LAN if advertise is WAN
LAN_IP=$(guess_lan_ip)
if [[ "$ADVERTISE_HOST" != "$LAN_IP" && "$ADVERTISE_HOST" != "127.0.0.1" ]]; then
  export HERMES_CERT_EXTRA_SAN="IP:${ADVERTISE_HOST},IP:${LAN_IP}"
fi
bash "$HERMES_DB_HOME/scripts/gen-ca.sh"

say "Installing native MongoDB…"
bash "$HERMES_DB_HOME/scripts/install-mongo-native.sh"

say "Starting enroll + orchestrator services…"
bash "$HERMES_DB_HOME/scripts/install-services.sh"

cat > "$HERMES_DB_HOME/agent-add" <<'EOF'
#!/usr/bin/env bash
exec "$(cd "$(dirname "$0")" && pwd)/scripts/agent-add.sh" "$@"
EOF
chmod +x "$HERMES_DB_HOME/agent-add"

if [[ "$MODE" != "lo" ]]; then
  warn "If agents are remote, open ports 27017, 8743, 8744 (ufw allow …)."
fi

echo ""
echo "${GREEN}✓ Hermes DB self-hosted${NC}  →  $HERMES_DB_HOME"
echo "  Mode         : ${MODE} — ${LISTEN_HINT}"
echo "  Bind         : ${BIND_IP}"
echo "  Advertise    : ${ADVERTISE_HOST}"
echo "  Mongo        : ${HERMES_MONGO_HOSTS}"
echo "  Enroll       : http://${ADVERTISE_HOST}:8743"
echo "  Orchestrator : https://${ADVERTISE_HOST}:8744  (mTLS)"
echo "  Status       : systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator"
echo ""

if [[ "$SKIP_CONNECT" == "1" ]]; then
  echo "Next: $HERMES_DB_HOME/agent-add"
  exit 0
fi

if ! can_prompt; then
  warn "No TTY for prompts — run: $HERMES_DB_HOME/agent-add"
  exit 0
fi

ask ans "Connect an agent PC now (one-time code & wait)? [Y/n] " "Y"
if [[ "$ans" =~ ^[Yy] ]]; then
  ask pcname "Optional PC name (e.g. home-pc): "
  bash "$HERMES_DB_HOME/scripts/agent-add.sh" ${pcname:+"$pcname"} || true
  exit 0
fi

echo "Later: $HERMES_DB_HOME/agent-add"
echo ""
