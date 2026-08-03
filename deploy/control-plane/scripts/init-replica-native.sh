#!/usr/bin/env bash
# Init single-node replica set + admin/app users (mongosh).
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

# Advertise this to agents (LAN). Bootstrap always initiates on loopback first,
# then reconfigs — avoids "No host … maps to this node" when mongod was local-only.
PUBLIC_HOST="${HERMES_MONGO_RS_HOST:-127.0.0.1:${PORT}}"
LOCAL_HOST="127.0.0.1:${PORT}"

die() { echo "ERROR: $*" >&2; exit 1; }

mongosh_boot() {
  mongosh --quiet --port "$PORT" --eval "$1"
}

mongosh_auth() {
  mongosh --quiet -u "$ROOT_USER" -p "$ROOT_PASS" --authenticationDatabase admin \
    --port "$PORT" --eval "$1"
}

wait_primary() {
  local i state
  echo "→ Waiting for PRIMARY…"
  for i in $(seq 1 90); do
    if [[ "$BOOTSTRAP" -eq 1 ]]; then
      state=$(mongosh_boot 'try{rs.isMaster().ismaster}catch(e){false}' 2>/dev/null || echo false)
    else
      state=$(mongosh_auth 'try{rs.isMaster().ismaster}catch(e){false}' 2>/dev/null || echo false)
    fi
    [[ "$state" == "true" ]] && return 0
    sleep 1
  done
  return 1
}

echo "→ Initiating replica set rs0 (advertise: $PUBLIC_HOST)…"

if [[ "$BOOTSTRAP" -eq 1 ]]; then
  # 1) Initiate on loopback so this process always maps to the member host
  mongosh_boot "
try {
  const s = rs.status();
  print('replica set already initiated: ' + s.set);
} catch (e) {
  rs.initiate({ _id: 'rs0', members: [ { _id: 0, host: '${LOCAL_HOST}' } ] });
  print('replica set initiated on ${LOCAL_HOST}');
}
" || die "rs.initiate failed"
else
  mongosh_auth "
try { rs.status(); print('replica set already initiated'); }
catch (e) {
  rs.initiate({ _id: 'rs0', members: [ { _id: 0, host: '${LOCAL_HOST}' } ] });
  print('replica set initiated on ${LOCAL_HOST}');
}
" || die "rs.initiate failed (auth)"
fi

wait_primary || die "replica set did not become PRIMARY"

# 2) Reconfig to LAN / public host if different (needed for remote agents)
if [[ "$PUBLIC_HOST" != "$LOCAL_HOST" ]]; then
  echo "→ Reconfig member host → $PUBLIC_HOST"
  RECONFIG="
cfg = rs.conf();
cfg.members[0].host = '${PUBLIC_HOST}';
try { rs.reconfig(cfg); print('reconfig ok'); }
catch (e) { rs.reconfig(cfg, { force: true }); print('reconfig force ok'); }
"
  if [[ "$BOOTSTRAP" -eq 1 ]]; then
    mongosh_boot "$RECONFIG" || die "rs.reconfig failed"
  else
    mongosh_auth "$RECONFIG" || die "rs.reconfig failed"
  fi
  wait_primary || die "not PRIMARY after reconfig"
fi

if [[ "$BOOTSTRAP" -eq 1 ]]; then
  echo "→ Creating root + app users…"
  mongosh_boot "
const admin = db.getSiblingDB('admin');
if (admin.getUser('${ROOT_USER}')) {
  print('root already exists');
} else {
  admin.createUser({ user: '${ROOT_USER}', pwd: '${ROOT_PASS}', roles: [ { role: 'root', db: 'admin' } ] });
  print('created root');
}
" || die "create root user failed"
fi

mongosh_auth "
const admin = db.getSiblingDB('admin');
if (admin.getUser('${APP_USER}')) {
  print('app user already exists');
} else {
  admin.createUser({
    user: '${APP_USER}',
    pwd: '${APP_PASS}',
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
}
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
" || die "app user / seed failed"

umask 077
cat > "$ROOT/certs/app-credentials.txt" <<EOF
HERMES_APP_USER=$APP_USER
HERMES_APP_PASSWORD=$APP_PASS
MONGO_ROOT_USER=$ROOT_USER
MONGO_ROOT_PASSWORD=$ROOT_PASS
EOF
chmod 600 "$ROOT/certs/app-credentials.txt"

echo "✓ Replica set / users ready (native). Advertise: $PUBLIC_HOST"
