#!/usr/bin/env bash
# ============================================================================
# Full uninstall — Hermes AGENT (Mongo fork + upstream leftovers)
# ============================================================================
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/uninstall-agent.sh | bash
#
# Env:
#   HERMES_HOME           default ~/.hermes
#   HERMES_PURGE_MONGO=0  (unused here — agent only)
#   HERMES_YES=1          skip confirmation
# ============================================================================
set -euo pipefail

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
YES="${HERMES_YES:-0}"

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
echo "${BOLD}Hermes AGENT uninstall${NC}"
echo "  Will remove:"
echo "    - $HERMES_HOME_DIR  (config, certs, bootstrap, hermes-agent checkout, skills, cache)"
echo "    - ~/.local/bin/hermes  (and hermes-acp if ours)"
echo "    - /usr/local/bin/hermes  (restore .upstream if present, else delete Mongo launcher)"
echo "    - optional: /usr/local/lib/hermes-agent  (upstream sealed install)"
echo ""

if [[ "$YES" != "1" ]]; then
  ans=$(ask_tty "Type YES to wipe agent install: ")
  [[ "$ans" == "YES" ]] || { echo "Aborted."; exit 1; }
fi

# Stop common services if any
systemctl --user stop hermes-gateway.service 2>/dev/null || true
systemctl --user disable hermes-gateway.service 2>/dev/null || true

# Launchers
rm -f "$HOME/.local/bin/hermes" "$HOME/.local/bin/hermes-acp" 2>/dev/null || true
rm -f "$HERMES_HOME_DIR/bin/hermes" 2>/dev/null || true

restore_or_rm() {
  local p="$1"
  [[ -e "$p" || -L "$p" ]] || return 0
  if [[ -e "${p}.upstream" ]]; then
    say "Restoring upstream $p from ${p}.upstream"
    if [[ -w "$(dirname "$p")" ]]; then
      mv -f "${p}.upstream" "$p"
    else
      sudo mv -f "${p}.upstream" "$p"
    fi
  else
    # Only remove if it looks like our Mongo wrapper
    if grep -q 'Hermes Mongo fork launcher\|hermes_cli.main' "$p" 2>/dev/null; then
      say "Removing Mongo launcher $p"
      if [[ -w "$(dirname "$p")" ]]; then
        rm -f "$p"
      else
        sudo rm -f "$p"
      fi
    else
      warn "Leaving $p (not recognized as Mongo launcher). Remove manually if needed."
    fi
  fi
}

restore_or_rm /usr/local/bin/hermes
restore_or_rm /usr/local/bin/hermes-acp

# Upstream sealed tree (optional — ask)
PURGE_UPSTREAM="${HERMES_PURGE_UPSTREAM:-}"
if [[ -d /usr/local/lib/hermes-agent ]]; then
  if [[ "$PURGE_UPSTREAM" == "1" ]] || [[ "$YES" == "1" ]]; then
    say "Removing /usr/local/lib/hermes-agent"
    if [[ -w /usr/local/lib ]]; then
      rm -rf /usr/local/lib/hermes-agent
    else
      sudo rm -rf /usr/local/lib/hermes-agent
    fi
  else
    ans=$(ask_tty "Also delete upstream /usr/local/lib/hermes-agent? [y/N] ")
    if [[ "$ans" =~ ^[Yy]$ ]]; then
      say "Removing /usr/local/lib/hermes-agent"
      sudo rm -rf /usr/local/lib/hermes-agent
    else
      warn "Kept /usr/local/lib/hermes-agent"
    fi
  fi
fi

# Home
if [[ -d "$HERMES_HOME_DIR" ]]; then
  say "Removing $HERMES_HOME_DIR"
  rm -rf "$HERMES_HOME_DIR"
fi

# Pip editable leftovers (best-effort)
python3 -m pip uninstall -y hermes-agent 2>/dev/null || true
"$HOME/.local/lib/hermes-agent/venv/bin/pip" uninstall -y hermes-agent 2>/dev/null || true

hash -r 2>/dev/null || true

echo ""
echo "${GREEN}OK agent wiped${NC}"
echo "  which hermes: $(command -v hermes || echo '(none)')"
echo ""
echo "Reinstall:"
echo "  curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/install-agent.sh | bash"
echo ""
