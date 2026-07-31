#!/usr/bin/env bash
# Create a one-time enroll code. Reuses hermes-enroll on :8743 ? never double-binds.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

NAME="${1:-}"
TTL="${HERMES_ENROLL_TTL:-300}"
PORT="${HERMES_ENROLL_PORT:-8743}"
PROFILE="${HERMES_PROFILE:-default}"
LISTEN_BIND="${HERMES_LISTEN_BIND:-${HERMES_MONGO_BIND:-0.0.0.0}}"

guess_ip() {
  local ip=""
  if command -v ip >/dev/null 2>&1; then
    ip=$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}' || true)
  fi
  if [[ -z "$ip" ]]; then
    ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  fi
  echo "${ip:-127.0.0.1}"
}

ADVERTISE="${HERMES_ADVERTISE_HOST:-$(guess_ip)}"
HOSTS="${HERMES_MONGO_HOSTS:-${ADVERTISE}:27017}"

if grep -q '^HERMES_MONGO_HOSTS=' "$ROOT/.env" 2>/dev/null; then
  sed -i.bak "s|^HERMES_MONGO_HOSTS=.*|HERMES_MONGO_HOSTS=$HOSTS|" "$ROOT/.env" && rm -f "$ROOT/.env.bak"
else
  echo "HERMES_MONGO_HOSTS=$HOSTS" >> "$ROOT/.env"
fi

TEMP_PID=""
cleanup() {
  if [[ -n "${TEMP_PID}" ]]; then
    kill "$TEMP_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

port_busy() {
  python3 -c "import socket;s=socket.socket();s.settimeout(0.3);r=s.connect_ex(('127.0.0.1',int('${PORT}')));s.close();raise SystemExit(0 if r==0 else 1)" 2>/dev/null
}

ensure_enroll_listening() {
  if systemctl --user is-active --quiet hermes-enroll.service 2>/dev/null; then
    echo "-> hermes-enroll.service already active on :${PORT}"
    return 0
  fi
  if port_busy; then
    echo "-> port ${PORT} already in use ? reusing existing enroll listener"
    return 0
  fi
  if systemctl --user cat hermes-enroll.service >/dev/null 2>&1; then
    systemctl --user start hermes-enroll.service || true
    sleep 1
    if systemctl --user is-active --quiet hermes-enroll.service; then
      echo "-> started hermes-enroll.service"
      return 0
    fi
  fi
  echo "-> starting temporary enroll listener on ${LISTEN_BIND}:${PORT}"
  HERMES_CONTROL_DIR="$ROOT" HERMES_ENROLL_PORT="$PORT" HERMES_LISTEN_BIND="$LISTEN_BIND" \
    HERMES_MONGO_HOSTS="$HOSTS" \
    python3 "$ROOT/scripts/enroll_standalone.py" serve &
  TEMP_PID=$!
  for _ in $(seq 1 40); do
    port_busy && return 0
    sleep 0.25
  done
  echo "ERROR: enroll listener failed to bind :${PORT}" >&2
  exit 1
}

ARGS=(create-code --profile "$PROFILE" --ttl "$TTL")
[[ -n "$NAME" ]] && ARGS+=(--name "$NAME")
CODE=$(HERMES_CONTROL_DIR="$ROOT" python3 "$ROOT/scripts/enroll_standalone.py" "${ARGS[@]}")

ensure_enroll_listening

CONNECT_CMD="hermes db connect --host ${ADVERTISE}:${PORT} --code ${CODE}"

echo ""
echo "========================================================"
echo "  Hermes DB - connect an agent PC"
echo "========================================================"
echo "  Code     :  $CODE"
echo "  Address  :  ${ADVERTISE}:${PORT}"
echo "  Mode     :  ${HERMES_LISTEN_MODE:-unknown}  bind=${LISTEN_BIND}"
echo "  Valid    :  $((TTL/60)) min"
echo ""
echo "  On the agent PC run THIS command:"
echo ""
echo "    ${CONNECT_CMD}"
echo ""
echo "  If hermes db is missing, fix launcher first:"
echo "    curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh | bash"
echo "    then run the hermes db connect line above again."
echo ""
echo "  Or one-shot without hermes CLI:"
echo "    curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/db-connect.sh | bash -s -- --host ${ADVERTISE}:${PORT} --code ${CODE}"
echo "========================================================"
echo ""
echo "Waiting for PC to connect (Ctrl+C to stop waiting)..."

export HERMES_CONTROL_DIR="$ROOT"
WHO=$(python3 - "$CODE" "$TTL" "$ROOT/enroll_pending" <<'PY'
import json, re, sys, time
from pathlib import Path

code_raw, ttl, pending = sys.argv[1], int(sys.argv[2]), Path(sys.argv[3])
want = re.sub(r"[^A-Z0-9]", "", code_raw.upper())
deadline = time.time() + ttl + 30
while time.time() < deadline:
    for f in pending.glob("*.json"):
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        got = re.sub(r"[^A-Z0-9]", "", str(doc.get("code") or "").upper())
        if got != want:
            continue
        if doc.get("used"):
            print(doc.get("used_by") or "agent")
            raise SystemExit(0)
    time.sleep(1)
raise SystemExit(1)
PY
) && {
  echo ""
  echo "OK: agent connected as '${WHO}'"
  exit 0
}

echo ""
echo "Timed out ? code expired or unused. Run agent-add again."
exit 1
