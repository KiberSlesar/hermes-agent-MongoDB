#!/usr/bin/env bash
# Standalone DB enroll — works even when upstream `hermes` has no `db connect`.
#   curl -fsSL .../install/db-connect.sh | bash -s -- --host IP:8743 --code ABCD-EFGH
set -euo pipefail

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
HOST=""
CODE=""
NAME=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --hermes-home) HERMES_HOME_DIR="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: db-connect.sh --host IP:8743 --code ABCD-EFGH [--name pc-name]"
      exit 0 ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

ask() {
  local __var="$1" __prompt="$2" __def="${3:-}" __ans=""
  if [[ -r /dev/tty ]]; then
    printf "%s" "$__prompt" > /dev/tty
    read -r __ans < /dev/tty || true
    [[ -n "$__def" ]] && __ans=${__ans:-$__def}
  fi
  printf -v "$__var" '%s' "$__ans"
}

[[ -n "$HOST" ]] || ask HOST "Control-plane address (IP:8743): " "127.0.0.1:8743"
[[ -n "$CODE" ]] || ask CODE "One-time code: "
[[ -n "$HOST" && -n "$CODE" ]] || { echo "Need --host and --code"; exit 1; }
[[ -n "$NAME" ]] || NAME=$(hostname -s 2>/dev/null || hostname || echo agent)

HOST="${HOST#http://}"; HOST="${HOST#https://}"
mkdir -p "$HERMES_HOME_DIR/certs"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "Connecting to http://${HOST}/enroll …"
curl -fsSL -X POST "http://${HOST}/enroll" \
  -H "Content-Type: application/json" \
  -d "{\"code\":\"${CODE}\",\"name\":\"${NAME}\"}" \
  -o "$TMP/bundle.tar.gz"

tar -xzf "$TMP/bundle.tar.gz" -C "$TMP"
BDIR=$(find "$TMP" -mindepth 1 -maxdepth 2 -type d -name 'certs' -printf '%h\n' 2>/dev/null | head -1)
[[ -z "$BDIR" ]] && BDIR=$(find "$TMP" -name bootstrap.yaml -printf '%h\n' 2>/dev/null | head -1)
[[ -n "$BDIR" ]] || { echo "Bad bundle"; exit 1; }

cp "$BDIR/bootstrap.yaml" "$HERMES_HOME_DIR/bootstrap.yaml"
cp -f "$BDIR/certs/"* "$HERMES_HOME_DIR/certs/" 2>/dev/null || true
chmod 600 "$HERMES_HOME_DIR/bootstrap.yaml" "$HERMES_HOME_DIR/certs/"* 2>/dev/null || true

echo "OK: wrote $HERMES_HOME_DIR/bootstrap.yaml + certs"
if command -v hermes >/dev/null 2>&1; then
  hermes storage status 2>/dev/null || true
  hermes storage seed 2>/dev/null || hermes storage migrate 2>/dev/null || true
fi
echo "Done. If hermes db is missing, re-run install-agent.sh to force Mongo launcher."
