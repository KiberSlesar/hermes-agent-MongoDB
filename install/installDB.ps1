# Hermes DB installer for Windows is not supported natively.
# Use Ubuntu (recommended) or WSL2:
#
#   wsl -d Ubuntu
#   curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash
#

Write-Host @"

Hermes DB runs on Ubuntu / Debian (or WSL2).

  curl -fsSL https://raw.githubusercontent.com/KiberSlesar/hermes-agent-MongoDB/main/install/installDB.sh | bash

Windows Server: install MongoDB Community yourself, or run the Ubuntu script in WSL2.

"@
