#!/usr/bin/env bash
set -euo pipefail

fail() { printf 'setup-dev: %s\n' "$*" >&2; exit 1; }
usage() { printf 'Usage: bash scripts/setup-dev.sh [--verify]\n'; }

mode=${1:-}
if [[ $# -gt 1 ]]; then usage >&2; exit 2; fi
case "$mode" in
  '') ;;
  --verify) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

repo=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd "$repo"
unset PYTHONPATH PYTHONHOME
umask 077

python_pin=3.12.14
dev_python="$repo/.venv/bin/python"

python_matches() {
  [[ $("$1" -I -c 'import platform; print(platform.python_version())' 2>/dev/null) == "$python_pin" ]]
}

if [[ $mode == --verify ]]; then
  [[ -x $dev_python ]] || fail 'Run bash scripts/setup-dev.sh first.'
  exec "$dev_python" -I "$repo/scripts/setup_dev.py" verify
fi

if [[ -e .venv || -L .venv ]]; then
  python_matches "$dev_python" || fail "Existing .venv does not use Python $python_pin. Move it aside and rerun setup."
  builder=$dev_python
else
  builder=${RAH_DEV_PYTHON:-}
  if [[ -n $builder ]]; then
    python_matches "$builder" || fail "RAH_DEV_PYTHON must select Python $python_pin."
  else
    for candidate in python3.12 python3; do
      if python_matches "$candidate"; then builder=$candidate; break; fi
    done
    [[ -n $builder ]] || fail "Python $python_pin is required. Set RAH_DEV_PYTHON to its executable."
  fi
fi
exec "$builder" -I "$repo/scripts/setup_dev.py" install
