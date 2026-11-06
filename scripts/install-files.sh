#!/usr/bin/env bash
# Copies the controller into a root filesystem. Used by install.sh and build-image.sh.
# Usage: install-files.sh <root> [--network]
set -euo pipefail

root="${1:?Give the root folder, such as /}"
network="${2:-}"
source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -D -m 755 "$source_dir/fixture_control.py" "$root/opt/fixture-control/fixture_control.py"
install -D -m 755 "$source_dir/make_chase.py" "$root/opt/fixture-control/make_chase.py"
install -D -m 644 "$source_dir/system/fixture-control.service" "$root/etc/systemd/system/fixture-control.service"

mkdir -p "$root/etc/systemd/system/multi-user.target.wants"
ln -sf ../fixture-control.service "$root/etc/systemd/system/multi-user.target.wants/fixture-control.service"

if [[ "$network" == "--network" ]]; then
  install -D -m 600 "$source_dir/system/artnet-output.nmconnection" \
    "$root/etc/NetworkManager/system-connections/artnet-output.nmconnection"
  install -D -m 600 "$source_dir/system/control.nmconnection" \
    "$root/etc/NetworkManager/system-connections/control.nmconnection"
  install -D -m 644 "$source_dir/system/99-fixture-control-network.cfg" \
    "$root/etc/cloud/cloud.cfg.d/99-fixture-control-network.cfg"
fi
