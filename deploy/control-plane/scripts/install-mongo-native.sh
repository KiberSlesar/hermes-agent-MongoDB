#!/usr/bin/env bash
# Install MongoDB Community natively (Ubuntu/Debian) — no Docker.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

say() { echo "→ $*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

command -v openssl >/dev/null || die "openssl required"
[[ -f "$ROOT/certs/mongo-server.pem" ]] || die "run gen-ca.sh first"
[[ -f "$ROOT/certs/ca.crt" ]] || die "run gen-ca.sh first"
[[ -f "$ROOT/certs/mongo-keyfile" ]] || die "run gen-ca.sh first"

# --- Install mongodb-org if missing ---
if ! command -v mongod >/dev/null 2>&1; then
  say "Installing MongoDB Community (apt)…"
  . /etc/os-release
  [[ "${ID:-}" == "ubuntu" || "${ID:-}" == "debian" ]] || die "Native installer supports Ubuntu/Debian. Found: ${ID:-unknown}"
  sudo apt-get update -qq
  sudo apt-get install -y -qq gnupg curl ca-certificates
  curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc |
    sudo gpg -o /usr/share/keyrings/mongodb-server-7.0.gpg --dearmor
  CODENAME="${VERSION_CODENAME:-jammy}"
  echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/${ID} ${CODENAME}/mongodb-org/7.0 multiverse" |
    sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq mongodb-org mongodb-mongosh || \
    sudo apt-get install -y -qq mongodb-org
fi

command -v mongod >/dev/null || die "mongod not found after install"
command -v mongosh >/dev/null || warn_mongosh() { echo "WARNING: mongosh missing — install mongodb-mongosh"; }

# Data dirs under control-plane home (not /var — easier perms for hermes user)
DATA="$ROOT/data/mongo"
LOG="$ROOT/logs"
mkdir -p "$DATA" "$LOG"
chmod 700 "$DATA"
chmod 400 "$ROOT/certs/mongo-keyfile" || true
chmod 600 "$ROOT/certs/mongo-server.pem" || true
chmod 644 "$ROOT/certs/ca.crt" || true

# Bind IP
BIND="${HERMES_MONGO_BIND:-0.0.0.0}"
PORT="${HERMES_MONGO_PORT:-27017}"

CONF="$ROOT/mongod.conf"
cat > "$CONF" <<EOF
# Hermes self-hosted MongoDB (generated — do not edit by hand unless you know why)
storage:
  dbPath: ${DATA}
systemLog:
  destination: file
  path: ${LOG}/mongod.log
  logAppend: true
net:
  port: ${PORT}
  bindIp: ${BIND}
  tls:
    mode: preferTLS
    certificateKeyFile: ${ROOT}/certs/mongo-server.pem
    CAFile: ${ROOT}/certs/ca.crt
    allowConnectionsWithoutCertificates: true
replication:
  replSetName: rs0
security:
  authorization: enabled
  keyFile: ${ROOT}/certs/mongo-keyfile
processManagement:
  fork: false
EOF

# First boot without auth to init RS — if no admin yet, start briefly with auth disabled
NEEDS_BOOTSTRAP=0
if [[ ! -f "$ROOT/certs/app-credentials.txt" ]]; then
  NEEDS_BOOTSTRAP=1
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT="$UNIT_DIR/hermes-mongod.service"

cat > "$UNIT" <<EOF
[Unit]
Description=Hermes MongoDB (self-hosted)
After=network.target

[Service]
Type=simple
WorkingDirectory=${ROOT}
ExecStart=$(command -v mongod) --config ${CONF}
Restart=on-failure
RestartSec=3
LimitNOFILE=64000

[Install]
WantedBy=default.target
EOF

# Bootstrap path: temporary conf without auth to create users
if [[ "$NEEDS_BOOTSTRAP" -eq 1 ]]; then
  say "First-time bootstrap (create users)…"
  BOOT_CONF="$ROOT/mongod.bootstrap.conf"
  cat > "$BOOT_CONF" <<EOF
storage:
  dbPath: ${DATA}
systemLog:
  destination: file
  path: ${LOG}/mongod-bootstrap.log
  logAppend: true
net:
  port: ${PORT}
  bindIp: 127.0.0.1
replication:
  replSetName: rs0
processManagement:
  fork: false
EOF
  # Stop any previous
  systemctl --user stop hermes-mongod.service 2>/dev/null || true
  pkill -f "mongod --config ${CONF}" 2>/dev/null || true
  pkill -f "mongod --config ${BOOT_CONF}" 2>/dev/null || true
  sleep 1
  mongod --config "$BOOT_CONF" &
  BOOT_PID=$!
  for i in $(seq 1 60); do
    if mongosh --quiet --port "$PORT" --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  bash "$ROOT/scripts/init-replica-native.sh" --bootstrap
  kill "$BOOT_PID" 2>/dev/null || true
  wait "$BOOT_PID" 2>/dev/null || true
  sleep 1
fi

systemctl --user daemon-reload
systemctl --user enable hermes-mongod.service
systemctl --user restart hermes-mongod.service

# linger so services survive logout
loginctl enable-linger "$(id -un)" 2>/dev/null || true

say "Waiting for mongod…"
for i in $(seq 1 60); do
  if mongosh --quiet --port "$PORT" --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1 \
    || mongosh --quiet "mongodb://127.0.0.1:${PORT}/?directConnection=true" --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1; then
    break
  fi
  # try with creds
  if [[ -f "$ROOT/certs/app-credentials.txt" ]]; then
    # shellcheck disable=SC1091
    set -a && source "$ROOT/certs/app-credentials.txt" && set +a
    if mongosh --quiet -u "$MONGO_ROOT_USER" -p "$MONGO_ROOT_PASSWORD" --authenticationDatabase admin \
      --port "$PORT" --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1; then
      break
    fi
  fi
  sleep 1
  if [[ $i -eq 60 ]]; then
    die "mongod did not become ready — see $LOG/mongod.log"
  fi
done

# Ensure RS + users exist even if credentials already present
bash "$ROOT/scripts/init-replica-native.sh" || true

echo "✓ Native MongoDB listening on ${BIND}:${PORT}"
echo "  conf: $CONF"
echo "  unit: hermes-mongod.service (systemctl --user status hermes-mongod)"
