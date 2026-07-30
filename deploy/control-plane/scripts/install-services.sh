#!/usr/bin/env bash
# Install systemd --user units for enroll (:8743) and orchestrator (:8744).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a
[[ -f "$ROOT/certs/app-credentials.txt" ]] && set -a && source "$ROOT/certs/app-credentials.txt" && set +a

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

# pymongo for orchestrator
if ! python3 -c "import pymongo" 2>/dev/null; then
  echo "? pip install pymongo"
  python3 -m pip install --user -q pymongo || sudo apt-get install -y -qq python3-pymongo || true
fi

MONGO_HOST="${HERMES_MONGO_HOSTS%%,*}"
MONGO_HOST="${MONGO_HOST:-127.0.0.1:27017}"
LISTEN_BIND="${HERMES_LISTEN_BIND:-${HERMES_MONGO_BIND:-0.0.0.0}}"

cat > "$UNIT_DIR/hermes-enroll.service" <<EOF
[Unit]
Description=Hermes enroll API (:8743)
After=hermes-mongod.service network.target
Requires=hermes-mongod.service

[Service]
Type=simple
WorkingDirectory=${ROOT}
Environment=HERMES_CONTROL_DIR=${ROOT}
Environment=HERMES_ENROLL_PORT=8743
Environment=HERMES_LISTEN_BIND=${LISTEN_BIND}
Environment=HERMES_MONGO_HOSTS=${HERMES_MONGO_HOSTS}
Environment=HERMES_REPLICA_SET=rs0
Environment=HERMES_ORCHESTRATOR_URL=${HERMES_ORCHESTRATOR_URL}
ExecStart=$(command -v python3) ${ROOT}/scripts/enroll_standalone.py serve
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/hermes-orchestrator.service" <<EOF
[Unit]
Description=Hermes orchestrator mTLS (:8744)
After=hermes-mongod.service network.target
Requires=hermes-mongod.service

[Service]
Type=simple
WorkingDirectory=${ROOT}
Environment=HERMES_CONTROL_DIR=${ROOT}
Environment=HERMES_ORCHESTRATOR_PORT=8744
Environment=HERMES_LISTEN_BIND=${LISTEN_BIND}
Environment=HERMES_MONGO_HOSTS=${MONGO_HOST}
Environment=HERMES_REPLICA_SET=rs0
Environment=HERMES_APP_USER=${HERMES_APP_USER:-hermesApp}
Environment=HERMES_APP_PASSWORD=${HERMES_APP_PASSWORD:-}
Environment=HERMES_SHARED_DB=hermes_shared
ExecStart=$(command -v python3) ${ROOT}/scripts/orchestrator_standalone.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable hermes-enroll.service hermes-orchestrator.service
systemctl --user restart hermes-enroll.service hermes-orchestrator.service
loginctl enable-linger "$(id -un)" 2>/dev/null || true

echo "? Services (bind ${LISTEN_BIND}):"
echo "  systemctl --user status hermes-mongod hermes-enroll hermes-orchestrator"
