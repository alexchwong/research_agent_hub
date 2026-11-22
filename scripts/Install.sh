#!/bin/sh
set -eu
cd "$(dirname "$0")"
case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    rah_host=ubuntu-x64
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] || { echo "This installer targets Ubuntu."; exit 1; }
    rah_root="${XDG_DATA_HOME:-$HOME/.local/share}/research-agent-hub"
    sha256sum -c sha256.txt ;;
  Darwin:arm64)
    rah_host=macos-arm64
    rah_root="$HOME/Library/Application Support/Research Agent Hub"
    shasum -a 256 -c sha256.txt ;;
  *) echo "This ZIP does not support this computer."; exit 1 ;;
esac
[ "$rah_host" = "$(cat host.txt)" ] || { echo "Download the ZIP for this computer."; exit 1; }
printf 'Install Research Agent Hub and its pinned Python runtime in %s? Python may be downloaded. [y/N] ' "$rah_root"
read -r rah_reply
case "$rah_reply" in y|Y|yes|YES) ;; *) exit 0 ;; esac
rah_pin="$(cat python-version.txt)"
./uv --no-config python install "$rah_pin"
rah_python="$(./uv --no-config python find --managed-python "$rah_pin")"
"$rah_python" install_release.py "$rah_root" "$PWD/uv"
