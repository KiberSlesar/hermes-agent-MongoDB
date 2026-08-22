#!/usr/bin/env bash
# Install MongoDB Community via apt (Ubuntu/Debian).
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

  # MongoDB apt only ships a few distro codenames. Newer Ubuntu (e.g. resolute)
  # has no Release file yet — fall back to the newest supported one.
  pick_mongo_codename() {
    local distro="$1" want="$2" c
    local -a candidates=()
    [[ -n "$want" ]] && candidates+=("$want")
    if [[ "$distro" == "ubuntu" ]]; then
      candidates+=(noble jammy focal)
    else
      candidates+=(bookworm bullseye)
    fi
    for c in "${candidates[@]}"; do
      if curl -fsI "https://repo.mongodb.org/apt/${distro}/dists/${c}/mongodb-org/7.0/Release" >/dev/null 2>&1; then
        echo "$c"
        return 0
      fi
    done
    return 1
  }

  CODENAME="$(pick_mongo_codename "${ID}" "${VERSION_CODENAME:-}")" \
    || die "No MongoDB 7.0 apt repo for ${ID} (tried ${VERSION_CODENAME:-?} + fallbacks)"
  if [[ "${CODENAME}" != "${VERSION_CODENAME:-}" ]]; then
    say "MongoDB has no packages for '${VERSION_CODENAME:-unknown}' — using '${CODENAME}' packages"
  fi

  echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/${ID} ${CODENAME}/mongodb-org/7.0 multiverse" |
    sudo tee /etc/apt/sources.list.d/mongodb-org-7.0.list >/dev/null
  sudo apt-get update -qq
  sudo apt-get install -y -qq mongodb-org mongodb-mongosh || \
    sudo apt-get install -y -qq mongodb-org
fi

command -v mongod >/dev/null || die "mongod not found after install"
command -v mongosh >/dev/null || echo "WARNING: mongosh missing — install mongodb-mongosh"

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
# Hermes MongoDB (generated — do not edit by hand unless you know why)
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
else
  # Credentials from a failed prior run → refuse to pretend we're fine
  # shellcheck disable=SC1091
  set -a && source "$ROOT/certs/app-credentials.txt" && set +a
  if ! mongosh --quiet -u "${MONGO_ROOT_USER}" -p "${MONGO_ROOT_PASSWORD}" \
      --authenticationDatabase admin --port "${HERMES_MONGO_PORT:-27017}" \
      --eval 'db.runCommand({ping:1}).ok' >/dev/null 2>&1; then
    die "Broken previous bootstrap (auth failed). Reset then re-run:
  systemctl --user stop hermes-mongod
  rm -rf \"$DATA\" \"$ROOT/certs/app-credentials.txt\"
  # keep .env and CA certs, then: bash $ROOT/scripts/install-mongo-native.sh"
  fi
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"
UNIT="$UNIT_DIR/hermes-mongod.service"

cat > "$UNIT" <<EOF
[Unit]
Description=Hermes MongoDB
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
  # Bind all interfaces so rs.reconfig(LAN IP) maps to this node.
  cat > "$BOOT_CONF" <<EOF
storage:
  dbPath: ${DATA}
systemLog:
  destination: file
  path: ${LOG}/mongod-bootstrap.log
  logAppend: true
net:
  port: ${PORT}
  bindIp: ${BIND}
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
  if ! bash "$ROOT/scripts/init-replica-native.sh" --bootstrap; then
    kill "$BOOT_PID" 2>/dev/null || true
    wait "$BOOT_PID" 2>/dev/null || true
    die "bootstrap failed — wipe $DATA and certs/app-credentials.txt then re-run"
  fi
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

# Re-run init only if credentials exist (auth path); fail loud
if [[ -f "$ROOT/certs/app-credentials.txt" ]]; then
  bash "$ROOT/scripts/init-replica-native.sh"
fi

echo "✓ Native MongoDB listening on ${BIND}:${PORT}"
echo "  conf: $CONF"
echo "  unit: hermes-mongod.service (systemctl --user status hermes-mongod)"
