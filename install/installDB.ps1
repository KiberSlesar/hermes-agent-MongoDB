# Hermes DB self-hosted installer for Windows is not supported natively.
# Use Ubuntu (recommended) or WSL2:
#
#   wsl -d Ubuntu
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/installDB.sh | bash
#
# Docker is intentionally NOT used.

Write-Host @"

Hermes DB = native MongoDB on Linux (no Docker).

On Ubuntu / Debian / WSL2:

  curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB-private/main/install/installDB.sh | bash

Windows Server: install MongoDB Community yourself, or run the Ubuntu script in WSL2.

"@
