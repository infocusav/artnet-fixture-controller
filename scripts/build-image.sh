#!/usr/bin/env bash
# Adds the controller to an unpacked Raspberry Pi OS Lite image.
# Run as root. Usage: build-image.sh <image.img>
set -euo pipefail

image="${1:?Give the path to an unpacked .img file}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mount_point="$(mktemp -d)"
loop_device=""

cleanup() {
  if mountpoint -q "$mount_point"; then umount "$mount_point"; fi
  if [[ -n "$loop_device" ]]; then losetup -d "$loop_device"; fi
  rmdir "$mount_point"
}
trap cleanup EXIT

# The system is the second partition. Its first sector is stored in the partition table.
first_sector="$(od -An -tu4 -j 470 -N 4 "$image" | tr -d ' ')"
if [[ -z "$first_sector" || "$first_sector" -eq 0 ]]; then
  echo "Could not find the system partition in $image" >&2
  exit 1
fi

loop_device="$(losetup --find --show --offset "$((first_sector * 512))" "$image")"
if ! mount "$loop_device" "$mount_point"; then
  echo "Could not open the system partition in $image" >&2
  exit 1
fi
if [[ ! -f "$mount_point/etc/os-release" ]]; then
  echo "$image does not look like Raspberry Pi OS" >&2
  exit 1
fi

"$script_dir/install-files.sh" "$mount_point" --network
sync
echo "Added the controller to $image"
