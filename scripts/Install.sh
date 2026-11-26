#!/bin/sh
set -eu
case "$#:$*" in
  0:|1:--bootstrap-only) ;;
  *) echo "Usage: $0 [--bootstrap-only]" >&2; exit 2 ;;
esac
cd "$(dirname "$0")"
case "$(uname -s):$(uname -m)" in
  Linux:x86_64)
    rah_host=ubuntu-x64
    . /etc/os-release
    [ "${ID:-}" = ubuntu ] || { echo "This installer targets Ubuntu."; exit 1; }
    rah_root="${XDG_DATA_HOME:-$HOME/.local/share}/research-agent-hub"
    sha256sum -c sha256.txt ;;
  Darwin:arm64)
    [ "$(sw_vers -productVersion | cut -d. -f1)" -ge 14 ] || { echo "macOS 14 or later is required."; exit 1; }
    rah_host=macos-arm64
    rah_root="$HOME/Library/Application Support/Research Agent Hub"
    shasum -a 256 -c sha256.txt ;;
  *) echo "This ZIP does not support this computer."; exit 1 ;;
esac
[ "$rah_host" = "$(cat host.txt)" ] || { echo "Download the ZIP for this computer."; exit 1; }
printf 'Install Research Agent Hub, Local Deep Research, GPT Researcher and PaperQA2 in %s? Internet access and space for three app environments are required. [y/N] ' "$rah_root"
read -r rah_reply
case "$rah_reply" in y|Y|yes|YES) ;; *) exit 0 ;; esac
rah_pin="$(cat python-version.txt)"
./uv --no-config python install "$rah_pin"
rah_python="$(./uv --no-config python find --managed-python "$rah_pin")"
exec "$rah_python" install_release.py "$rah_root" "$PWD/uv" "$@"
