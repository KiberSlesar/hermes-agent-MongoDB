#!/usr/bin/env bash
# ============================================================================
# Full uninstall — Hermes DB control plane (native Mongo + systemd)
# ============================================================================
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/uninstall-db.sh | bash
#
# Env:
#   HERMES_DB_HOME=~/hermes-db
#   HERMES_PURGE_MONGODB=1   also apt-remove mongodb-org* (dangerous)
#   HERMES_YES=1             skip confirmation
# ============================================================================
set -euo pipefail

HERMES_DB_HOME="${HERMES_DB_HOME:-$HOME/hermes-db}"
YES="${HERMES_YES:-0}"
PURGE_MONGODB="${HERMES_PURGE_MONGODB:-0}"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[0;33m'; BOLD=$'\033[1m'; NC=$'\033[0m'
say() { echo "${GREEN}→${NC} $*"; }
warn() { echo "${YELLOW}!${NC} $*"; }

ask_tty() {
  local ans=""
  if [[ -r /dev/tty ]]; then
    printf "%s" "$1" > /dev/tty
    read -r ans < /dev/tty || true
  fi
  echo "$ans"
}

echo ""
echo "${BOLD}Hermes DB uninstall${NC}"
echo "  Will stop/disable:"
echo "    systemctl --user hermes-mongod hermes-enroll hermes-orchestrator"
echo "  Will remove:"
echo "    - $HERMES_DB_HOME  (data, certs, .env, scripts)"
echo "    - ~/.config/systemd/user/hermes-*.service"
if [[ "$PURGE_MONGODB" == "1" ]]; then
  echo "    - mongodb-org packages (HERMES_PURGE_MONGODB=1)"
fi
echo ""

if [[ "$YES" != "1" ]]; then
  ans=$(ask_tty "Type YES to wipe Hermes DB on this machine: ")
  [[ "$ans" == "YES" ]] || { echo "Aborted."; exit 1; }
fi

say "Stopping services…"
systemctl --user stop hermes-enroll.service hermes-orchestrator.service hermes-mongod.service 2>/dev/null || true
systemctl --user disable hermes-enroll.service hermes-orchestrator.service hermes-mongod.service 2>/dev/null || true

# Kill stray mongod from bootstrap
pkill -f "mongod --config ${HERMES_DB_HOME}" 2>/dev/null || true
pkill -f "enroll_standalone.py" 2>/dev/null || true
pkill -f "orchestrator_standalone.py" 2>/dev/null || true
sleep 1

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rm -f "$UNIT_DIR/hermes-mongod.service" \
      "$UNIT_DIR/hermes-enroll.service" \
      "$UNIT_DIR/hermes-orchestrator.service" \
      "$UNIT_DIR/default.target.wants/hermes-mongod.service" \
      "$UNIT_DIR/default.target.wants/hermes-enroll.service" \
      "$UNIT_DIR/default.target.wants/hermes-orchestrator.service" 2>/dev/null || true
systemctl --user daemon-reload 2>/dev/null || true

if [[ -d "$HERMES_DB_HOME" ]]; then
  say "Removing $HERMES_DB_HOME"
  rm -rf "$HERMES_DB_HOME"
fi

# Broken apt list from failed installs
if [[ -f /etc/apt/sources.list.d/mongodb-org-7.0.list ]]; then
  if [[ "$PURGE_MONGODB" == "1" ]]; then
    say "Removing MongoDB apt source + packages…"
    sudo rm -f /etc/apt/sources.list.d/mongodb-org-7.0.list
    sudo apt-get remove -y mongodb-org mongodb-org-* mongodb-mongosh 2>/dev/null || true
    sudo apt-get purge -y mongodb-org mongodb-org-* 2>/dev/null || true
  else
    warn "Left MongoDB packages installed. To purge:"
    echo "  HERMES_PURGE_MONGODB=1 HERMES_YES=1 bash uninstall-db.sh"
  fi
fi

echo ""
echo "${GREEN}OK DB wiped${NC}"
echo ""
echo "Reinstall:"
echo "  curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash"
echo ""
