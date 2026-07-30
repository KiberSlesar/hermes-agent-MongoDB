#!/usr/bin/env bash
# Create a one-time enroll code and wait for an agent PC (standalone control plane).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck disable=SC1091
[[ -f "$ROOT/.env" ]] && set -a && source "$ROOT/.env" && set +a

NAME="${1:-}"
TTL="${HERMES_ENROLL_TTL:-300}"
PORT="${HERMES_ENROLL_PORT:-8743}"
PROFILE="${HERMES_PROFILE:-default}"

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

LAN=$(guess_ip)
HOSTS="${HERMES_MONGO_HOSTS:-${LAN}:27017,${LAN}:27018,${LAN}:27019}"

# Persist hosts for agents
if grep -q '^HERMES_MONGO_HOSTS=' "$ROOT/.env" 2>/dev/null; then
  sed -i.bak "s|^HERMES_MONGO_HOSTS=.*|HERMES_MONGO_HOSTS=$HOSTS|" "$ROOT/.env" && rm -f "$ROOT/.env.bak"
else
  echo "HERMES_MONGO_HOSTS=$HOSTS" >> "$ROOT/.env"
fi

ARGS=(create-code --profile "$PROFILE" --ttl "$TTL")
[[ -n "$NAME" ]] && ARGS+=(--name "$NAME")
CODE=$(HERMES_CONTROL_DIR="$ROOT" python3 "$ROOT/scripts/enroll_standalone.py" "${ARGS[@]}")

echo ""
echo "========================================================"
echo "  Hermes DB — connect an agent PC"
echo "========================================================"
echo "  Code     :  $CODE"
echo "  Address  :  ${LAN}:${PORT}"
echo "  Valid    :  $((TTL/60)) min"
echo ""
echo "  On the agent PC run:"
echo "    curl -fsSL \$INSTALL_AGENT_URL | bash"
echo "    # then answer Yes to connect, or: hermes db connect"
echo "    Address: ${LAN}:${PORT}"
echo "    Code:    $CODE"
echo "========================================================"
echo ""
echo "Waiting for PC to connect (Ctrl+C to stop listener)…"

export HERMES_CONTROL_DIR="$ROOT"
export HERMES_ENROLL_PORT="$PORT"
export HERMES_MONGO_HOSTS="$HOSTS"
exec python3 "$ROOT/scripts/enroll_standalone.py" serve
