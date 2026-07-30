#!/usr/bin/env bash
# ============================================================================
# Hermes DB — self-hosted installer (NO Docker)
# ============================================================================
# Native MongoDB + systemd user services for enroll (:8743) and
# mTLS orchestrator (:8744). Ubuntu/Debian.
#
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/installDB.sh | bash
#
# Env:
#   HERMES_DB_HOME          default ~/hermes-db
#   HERMES_MONGO_HOSTS      public host:27017 (single node)
#   HERMES_SKIP_CONNECT=1   skip agent connect prompt
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

auth_curl() {
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    curl -fsSL -H "Authorization: Bearer ${GH_TOKEN:-$GITHUB_TOKEN}" "$@"
  else
    curl -fsSL "$@"
  fi
}

guess_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)
  fi
  [[ -z "$ip" ]] && ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  echo "${ip:-127.0.0.1}"
}

echo ""
echo "${BOLD}⚕ Hermes DB installer (self-hosted, no Docker)${NC}"
echo ""

command -v openssl >/dev/null || die "openssl required"
command -v python3 >/dev/null || die "python3 required"
command -v curl >/dev/null || die "curl required"
command -v systemctl >/dev/null || die "systemd required"
command -v sudo >/dev/null || die "sudo required (to apt-install mongodb-org)"

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

LAN=$(guess_ip)
if [[ ! -f "$HERMES_DB_HOME/.env" ]]; then
  cat > "$HERMES_DB_HOME/.env" <<EOF
MONGO_ROOT_USER=hermesRoot
MONGO_ROOT_PASSWORD=$(openssl rand -hex 16)
HERMES_ENROLL_TOKEN=$(openssl rand -hex 24)
HERMES_MONGO_PORT=27017
HERMES_MONGO_HOSTS=${HERMES_MONGO_HOSTS:-${LAN}:27017}
HERMES_MONGO_RS_HOST=${LAN}:27017
HERMES_ORCHESTRATOR_URL=https://${LAN}:8744
HERMES_REPLICA_SET=rs0
EOF
  chmod 600 "$HERMES_DB_HOME/.env"
  say "Wrote $HERMES_DB_HOME/.env"
else
  say "Using existing $HERMES_DB_HOME/.env"
fi
set -a && source "$HERMES_DB_HOME/.env" && set +a

say "Generating CA / server certificates…"
export HERMES_CERT_EXTRA_SAN="IP:${LAN}"
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

# Open firewall hint
warn "If agents are remote, open ports 27017, 8743, 8744 (ufw allow …)."

echo ""
echo "${GREEN}✓ Hermes DB self-hosted${NC}  →  $HERMES_DB_HOME"
echo "  Mongo        : ${HERMES_MONGO_HOSTS}  (native mongod, no Docker)"
echo "  Enroll       : http://${LAN}:8743"
echo "  Orchestrator : https://${LAN}:8744  (mTLS)"
echo "  Status       : systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator"
echo ""

if [[ "$SKIP_CONNECT" == "1" ]] || [[ ! -t 0 ]]; then
  echo "Next: $HERMES_DB_HOME/agent-add"
  exit 0
fi

read -r -p "Connect an agent PC now (one-time code & wait)? [Y/n] " ans || ans=Y
ans=${ans:-Y}
if [[ "$ans" =~ ^[Yy] ]]; then
  read -r -p "Optional PC name (e.g. home-pc): " pcname || pcname=""
  # Stop persistent enroll service while interactive wait owns :8743
  systemctl --user stop hermes-enroll.service 2>/dev/null || true
  bash "$HERMES_DB_HOME/scripts/agent-add.sh" ${pcname:+"$pcname"} || true
  systemctl --user start hermes-enroll.service 2>/dev/null || true
  exit 0
fi

echo "Later: $HERMES_DB_HOME/agent-add"
echo ""
