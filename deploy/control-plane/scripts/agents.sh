#!/usr/bin/env bash
# ============================================================================
# Hermes DB — list / prune agents (human-readable, no JSON dump)
# ============================================================================
# Installed as:  ~/hermes-db/agents
# Usage:
#   agents              list nodes + enrolled agents
#   agents list
#   agents activate <machine_id|node_id|hostname>
#   agents prune [--keep NAME] [--yes]
#   agents revoke <machine_id|cert_cn> [--yes]
# ============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a
[[ -f "$ROOT/certs/app-credentials.txt" ]] && set -a && source "$ROOT/certs/app-credentials.txt" && set +a

PORT="${HERMES_MONGO_PORT:-27017}"
ROOT_USER="${MONGO_ROOT_USER:-hermesRoot}"
ROOT_PASS="${MONGO_ROOT_PASSWORD:-}"
PROFILE_DB="hermes_profile_${HERMES_PROFILE:-default}"

die() { echo "ERROR: $*" >&2; exit 1; }
[[ -n "$ROOT_PASS" ]] || die "MONGO_ROOT_PASSWORD missing (source $ROOT/.env / certs/app-credentials.txt)"
command -v mongosh >/dev/null || die "mongosh required"

meval() {
  mongosh --quiet -u "$ROOT_USER" -p "$ROOT_PASS" \
    --authenticationDatabase admin --port "$PORT" --eval "$1"
}

cmd="${1:-list}"
shift || true

case "$cmd" in
  -h|--help|help)
    sed -n '2,14p' "$0"
    exit 0
    ;;

  list|ls|"")
    meval "
const s = db.getSiblingDB('hermes_shared');
const p = db.getSiblingDB('${PROFILE_DB}');
const now = new Date();
function ago(d) {
  if (!d) return '-';
  const t = (d instanceof Date) ? d : new Date(d);
  if (isNaN(t)) return String(d);
  const sec = Math.max(0, Math.floor((now - t) / 1000));
  if (sec < 60) return sec + 's ago';
  if (sec < 3600) return Math.floor(sec/60) + 'm ago';
  if (sec < 86400) return Math.floor(sec/3600) + 'h ago';
  return Math.floor(sec/86400) + 'd ago';
}
function pad(s, n) { s = String(s ?? ''); return s.length >= n ? s.slice(0,n) : s + ' '.repeat(n - s.length); }

print('');
print('Hermes DB agents');
print('================');
const st = s.cluster_state.findOne({_id:'default'}) || {};
print('Active node : ' + (st.active_node_id || '(none)'));
print('Messaging   : ' + (st.messaging_owner || '(none)'));
print('Handoff     : ' + (st.handoff_state || 'idle'));
print('');

print('Online presence (cluster_nodes)');
print(pad('NODE', 22) + pad('MACHINE', 16) + pad('HOST', 16) + pad('STATUS', 8) + pad('TASKS', 7) + 'LAST SEEN');
print('-'.repeat(78));
const nodes = s.cluster_nodes.find().sort({hostname:1}).toArray();
if (!nodes.length) print('(none)');
for (const n of nodes) {
  let hb = n.heartbeat_at;
  let online = false;
  if (hb) {
    const t = (hb instanceof Date) ? hb : new Date(hb);
    if (!isNaN(t)) online = (now - t) < 60000;
  }
  const status = online ? 'online' : 'offline';
  print(
    pad(n.node_id || '-', 22) +
    pad(n.machine_id || '-', 16) +
    pad(n.hostname || '-', 16) +
    pad(status, 8) +
    pad(n.active_turns || 0, 7) +
    ago(hb)
  );
}
print('');

print('Enrolled (agent_registry / certs)');
print(pad('MACHINE', 16) + pad('CERT CN', 20) + pad('PROFILE', 12) + 'CREATED');
print('-'.repeat(66));
const regs = s.agent_registry.find().sort({machine_id:1}).toArray();
if (!regs.length) print('(none)');
for (const r of regs) {
  print(
    pad(r.machine_id || '-', 16) +
    pad(r.cert_cn || r.machine_id || '-', 20) +
    pad(r.profile || 'default', 12) +
    ago(r.created_at || r.enrolled_at || r.updated_at)
  );
}
print('');

print('Machine overlays (${PROFILE_DB}.machines)');
print(pad('MACHINE', 16) + pad('HOST', 16) + pad('NODE', 22) + 'UPDATED');
print('-'.repeat(70));
const macs = p.machines.find().sort({machine_id:1}).toArray();
if (!macs.length) print('(none)');
for (const m of macs) {
  print(
    pad(m.machine_id || '-', 16) +
    pad(m.hostname || '-', 16) +
    pad(m.node_id || '-', 22) +
    ago(m.updated_at)
  );
}
print('');
"
    ;;

  activate)
    TARGET="${1:-}"
    [[ -n "$TARGET" ]] || die "usage: agents activate <machine_id|node_id|hostname>"
    meval "
const s = db.getSiblingDB('hermes_shared');
const needle = '${TARGET}'.toLowerCase();
const nodes = s.cluster_nodes.find().toArray();
const target = nodes.find(n => [n.node_id, n.machine_id, n.hostname]
  .filter(Boolean).some(v => String(v).toLowerCase() === needle || String(v).toLowerCase().startsWith(needle)));
if (!target) { print('ERROR: no online node matched ${TARGET}'); quit(2); }
const st = s.cluster_state.findOne({_id:'default'}) || {};
const owner = st.messaging_owner || st.active_node_id;
const source = owner ? s.cluster_nodes.findOne({node_id: owner}) : null;
if (source && Number(source.active_turns || 0) > 0) {
  print('ERROR: active agent is busy; wait for the task to finish or send /stop before switching');
  quit(3);
}
if (owner === target.node_id) { print('Already active: ' + target.node_id); quit(0); }
s.cluster_state.updateOne({_id:'default'}, {\$set: {
  active_node_id: target.node_id,
  pending_active_node_id: target.node_id,
  handoff_state: 'releasing',
  handoff_from: owner || null,
  handoff_to: target.node_id,
  handoff_error: null,
  handoff_session_keys: (source && source.active_session_keys) || [],
  updated_at: new Date()
}}, {upsert:true});
print('Switch requested: ' + (owner || '(none)') + ' -> ' + target.node_id);
print('The old gateway will release after its active task is idle; the target will acquire the same Mongo session routing.');
"
    ;;

  prune)
    KEEP=""
    YES=0
    while [[ $# -gt 0 ]]; do
      case "$1" in
        --keep) KEEP="$2"; shift 2 ;;
        --yes|-y) YES=1; shift ;;
        *) die "unknown arg: $1" ;;
      esac
    done
    if [[ -z "$KEEP" ]]; then
      # default: keep the only online node, or ask
      KEEP=$(meval "
const s = db.getSiblingDB('hermes_shared');
const now = new Date();
let keep = '';
for (const n of s.cluster_nodes.find()) {
  const t = n.heartbeat_at ? ((n.heartbeat_at instanceof Date) ? n.heartbeat_at : new Date(n.heartbeat_at)) : null;
  if (t && !isNaN(t) && (now - t) < 60000) { keep = n.node_id; break; }
}
print(keep);
" | tr -d '\r')
    fi
    [[ -n "$KEEP" ]] || die "No online node to keep. Pass --keep <node_id>"
    echo "Will delete cluster_nodes except: $KEEP"
    if [[ "$YES" != "1" ]]; then
      read -r -p "Type YES: " ans || true
      [[ "$ans" == "YES" ]] || die "aborted"
    fi
    meval "
const s = db.getSiblingDB('hermes_shared');
const r = s.cluster_nodes.deleteMany({ node_id: { \$ne: '${KEEP}' } });
print('Deleted ' + r.deletedCount + ' ghost node(s). Kept: ${KEEP}');
"
    ;;

  revoke)
    TARGET="${1:-}"
    shift || true
    YES=0
    [[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && YES=1
    [[ -n "$TARGET" ]] || die "usage: agents revoke <machine_id|cert_cn> [--yes]"
    echo "Will revoke enrollment + machine overlay for: $TARGET"
    if [[ "$YES" != "1" ]]; then
      read -r -p "Type YES: " ans || true
      [[ "$ans" == "YES" ]] || die "aborted"
    fi
    meval "
const s = db.getSiblingDB('hermes_shared');
const p = db.getSiblingDB('${PROFILE_DB}');
const q = { \$or: [ { machine_id: '${TARGET}' }, { cert_cn: '${TARGET}' } ] };
const a = s.agent_registry.deleteMany(q);
const m = p.machines.deleteMany({ machine_id: '${TARGET}' });
const n = s.cluster_nodes.deleteMany({ \$or: [ { machine_id: '${TARGET}' }, { node_id: '${TARGET}' } ] });
print('Revoked registry=' + a.deletedCount + ' machines=' + m.deletedCount + ' nodes=' + n.deletedCount);
"
    ;;

  *)
    die "unknown command: $cmd (try: list | prune | revoke)"
    ;;
esac
