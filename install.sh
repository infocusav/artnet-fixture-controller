#!/usr/bin/env bash
# Installs the controller on a Pi that is already running Raspberry Pi OS.
#   sudo ./install.sh             installs the program and starts it at boot
#   sudo ./install.sh --network   also sets eth0 to 2.0.0.100/8 and eth1 to 192.168.1.100/24
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run this with sudo: sudo ./install.sh" >&2
  exit 1
fi
if ! command -v python3 > /dev/null; then
  echo "Python 3 is missing. Install it with: sudo apt install python3" >&2
  exit 1
fi
network="${1:-}"
if [[ -n "$network" && "$network" != "--network" ]]; then
  echo "The only option is --network." >&2
  exit 1
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$script_dir/scripts/install-files.sh" / "$network"
systemctl daemon-reload
systemctl enable fixture-control
systemctl restart fixture-control

if [[ "$network" == "--network" ]]; then
  echo "Applying network settings. An SSH session may drop now; reconnect on the new address."
  nmcli connection reload
  nmcli connection up artnet-output || true
  nmcli connection up control || echo "The control port will start when eth1 has a live cable."
fi

echo "Installed. The control page is on port 8080 of this Pi's addresses:"
hostname -I
