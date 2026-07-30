#!/usr/bin/env bash
# Init single-node replica set + admin/app users (native mongosh, no Docker).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

BOOTSTRAP=0
[[ "${1:-}" == "--bootstrap" ]] && BOOTSTRAP=1

PORT="${HERMES_MONGO_PORT:-27017}"
ROOT_USER="${MONGO_ROOT_USER:-hermesRoot}"
ROOT_PASS="${MONGO_ROOT_PASSWORD:-changeMeNow}"
APP_USER="${HERMES_APP_USER:-hermesApp}"
APP_PASS="${HERMES_APP_PASSWORD:-}"
if [[ -z "$APP_PASS" ]]; then
  APP_PASS=$(openssl rand -hex 16)
fi

HOST_FOR_RS="${HERMES_MONGO_RS_HOST:-127.0.0.1:${PORT}}"

mongosh_eval() {
  if [[ "$BOOTSTRAP" -eq 1 ]]; then
    mongosh --quiet --port "$PORT" --eval "$1"
  else
    mongosh --quiet -u "$ROOT_USER" -p "$ROOT_PASS" --authenticationDatabase admin \
      --port "$PORT" --eval "$1"
  fi
}

echo "→ Initiating replica set rs0 (single node: $HOST_FOR_RS)…"
if [[ "$BOOTSTRAP" -eq 1 ]]; then
  mongosh --quiet --port "$PORT" --eval "
try {
  rs.status();
  print('replica set already initiated');
} catch (e) {
  rs.initiate({ _id: 'rs0', members: [ { _id: 0, host: '$HOST_FOR_RS' } ] });
  print('replica set initiated');
}
" || true
else
  mongosh_eval "
try { rs.status(); print('ok'); } catch (e) {
  rs.initiate({ _id: 'rs0', members: [ { _id: 0, host: '$HOST_FOR_RS' } ] });
}
" || true
fi

echo "→ Waiting for PRIMARY…"
for i in $(seq 1 60); do
  STATE=$(mongosh --quiet --port "$PORT" --eval 'try{rs.isMaster().ismaster}catch(e){false}' 2>/dev/null || echo false)
  if [[ "$BOOTSTRAP" -ne 1 ]]; then
    STATE=$(mongosh --quiet -u "$ROOT_USER" -p "$ROOT_PASS" --authenticationDatabase admin --port "$PORT" \
      --eval 'try{rs.isMaster().ismaster}catch(e){false}' 2>/dev/null || echo false)
  fi
  [[ "$STATE" == "true" ]] && break
  sleep 1
done

if [[ "$BOOTSTRAP" -eq 1 ]]; then
  echo "→ Creating root + app users…"
  mongosh --quiet --port "$PORT" --eval "
const admin = db.getSiblingDB('admin');
try {
  admin.createUser({ user: '$ROOT_USER', pwd: '$ROOT_PASS', roles: [ { role: 'root', db: 'admin' } ] });
  print('created root');
} catch (e) { print('root: ' + e.message); }
" || true
fi

# App user + shared DB seed (authenticated)
AUTH_ARGS=()
if [[ "$BOOTSTRAP" -eq 1 ]]; then
  # After creating root, use it
  sleep 1
fi
mongosh --quiet -u "$ROOT_USER" -p "$ROOT_PASS" --authenticationDatabase admin --port "$PORT" --eval "
const admin = db.getSiblingDB('admin');
try {
  admin.createUser({
    user: '$APP_USER',
    pwd: '$APP_PASS',
    roles: [
      { role: 'readWrite', db: 'hermes_shared' },
      { role: 'dbAdmin', db: 'hermes_shared' },
      { role: 'readWrite', db: 'hermes_profile_default' },
      { role: 'dbAdmin', db: 'hermes_profile_default' },
      { role: 'readWriteAnyDatabase', db: 'admin' },
      { role: 'dbAdminAnyDatabase', db: 'admin' }
    ]
  });
  print('created app user');
} catch (e) { print('app: ' + e.message); }
db.getSiblingDB('hermes_shared').cluster_state.updateOne(
  { _id: 'default' },
  { \$setOnInsert: {
      _id: 'default',
      active_node_id: null,
      messaging_owner: null,
      handoff_state: 'idle',
      failover: 'auto',
      history: []
  }},
  { upsert: true }
);
print('hermes_shared ready');
" || true

umask 077
cat > "$ROOT/certs/app-credentials.txt" <<EOF
HERMES_APP_USER=$APP_USER
HERMES_APP_PASSWORD=$APP_PASS
MONGO_ROOT_USER=$ROOT_USER
MONGO_ROOT_PASSWORD=$ROOT_PASS
EOF
chmod 600 "$ROOT/certs/app-credentials.txt"

echo "✓ Replica set / users ready (native)."
