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
uv_pin=0.13.9
dev_python="$repo/.venv/bin/python"
uv="$repo/.venv/bin/uv"
release="$repo/.release-test"

python_matches() {
  [[ $("$1" -I -c 'import platform; print(platform.python_version())' 2>/dev/null) == "$python_pin" ]]
}

uv_matches() {
  local version
  version=$("$uv" --version) || return 1
  [[ $version == "uv $uv_pin" || $version == "uv $uv_pin "* ]]
}

if [[ $mode == --verify ]]; then
  run=$(cd "$release/current" 2>/dev/null && pwd -P) || fail 'Run bash scripts/setup-dev.sh first.'
  installed="$run/installed-hub/bin/python"
  [[ -x $installed && -x $run/installed-hub/bin/rah && -x $dev_python && -x $uv ]] ||
    fail 'The dev install is incomplete. Rerun bash scripts/setup-dev.sh.'
  python_matches "$dev_python" && python_matches "$installed" && uv_matches ||
    fail 'The installed toolchain does not match the pins. Rerun bash scripts/setup-dev.sh.'
  command -v node >/dev/null || fail 'Node is required for JavaScript syntax checks.'

  "$dev_python" -m pytest -q -m 'not live'
  node --check rah/static/app.js
  node --check rah/static/workspace.js
  node --check rah/static/native_handoff.js

  cd "$run"
  "$installed" -I -c '
from pathlib import Path
import sysconfig
import rah

if not Path(rah.__file__).resolve().is_relative_to(Path(sysconfig.get_path("purelib")).resolve()):
    raise SystemExit("RAH did not import from the installed wheel environment.")
'
  "$installed" -s "$repo/tests/smoke_setup.py" --uv "$uv" --report "$repo/reports/setup.json"
  "$installed" -s "$repo/tests/smoke_setup.py" --uv "$uv" --packaged --report "$repo/reports/upstreams.json"
  exit 0
fi

if [[ -e .venv || -L .venv ]]; then
  python_matches "$dev_python" || fail "Existing .venv does not use Python $python_pin. Move it aside and rerun setup."
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
  "$builder" -I -m venv .venv
fi

if ! "$dev_python" -I -m pip --version >/dev/null 2>&1; then
  "$dev_python" -I -m ensurepip
fi
if [[ ! -x $uv ]] || ! uv_matches; then
  "$dev_python" -I -m pip install --only-binary=:all: "uv==$uv_pin"
fi
"$uv" pip sync --python "$dev_python" --require-hashes --only-binary=:all: dev-deps.lock

mkdir -p "$release/bin"
run=$(mktemp -d "$release/dev.XXXXXXXX")
if ! "$dev_python" -I scripts/bundle_hub.py --system-python --uv "$uv" \
  --bundle "$run/bootstrap" --venv "$run/installed-hub"; then
  rm -rf -- "$run"
  fail 'Wheel build or install failed; the previous launcher target was not changed.'
fi

shim=$(mktemp "$release/bin/.rah.XXXXXXXX")
cat > "$shim" <<'LAUNCHER'
#!/usr/bin/env bash
set -euo pipefail
release=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if ! run=$(cd "$release/current" 2>/dev/null && pwd -P) || [[ ! -x $run/installed-hub/bin/python ]]; then
  printf 'Dev install missing. Run bash scripts/setup-dev.sh.\n' >&2
  exit 1
fi
unset PYTHONPATH PYTHONHOME
cd "$run"
# The existing setup command keeps current apps, retries incomplete setup, and opens the hub.
exec "$run/installed-hub/bin/python" -I -m rah.cli \
  --data-dir "$release/manual-data" setup --foreground --uv "$release/../.venv/bin/uv" --no-shortcut "$@"
LAUNCHER
chmod 700 "$shim"
"$dev_python" -I - "$run" "$shim" "$release" <<'PY'
from pathlib import Path
import sys

run, shim, release = map(Path, sys.argv[1:])
pending = run / "current-link"
# Relative to its final location; publish only once the wheel and shim are ready.
pending.symlink_to(run.name, target_is_directory=True)
shim.replace(release / "bin/rah")
pending.replace(release / "current")
PY
printf 'Installed dev wheel. Verify with bash scripts/setup-dev.sh --verify, or start with ./.release-test/bin/rah.\n'