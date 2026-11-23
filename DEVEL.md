# Development

Keep research inside the native upstreams. Preserve each app’s documented entry points, settings and original outputs.

## Quick start on Ubuntu and macOS

From the repository root, with Python 3.12.14 available and Node installed for verification:

```bash
bash scripts/setup-dev.sh
bash scripts/setup-dev.sh --verify
./.release-test/bin/rah
```

1. **Set up:** Prepare `.venv` with uv 0.13.9 and the locked dev tools, then build and install the wheel in a separate environment at `.release-test/current/installed-hub`. If the matching interpreter is not on `PATH`, set `RAH_DEV_PYTHON` to its absolute executable path before a fresh setup. An existing `.venv` must use Python 3.12.14. Rerun setup after changing package code to refresh the installed wheel; quit the dev hub first if it is running. Old builds are retained and can be removed when no longer in use. Setup does not start the hub or install the upstream apps.
2. **Verify:** Run the offline pytest suite, JavaScript syntax checks, and the fixture and packaged-upstream smokes against the previously installed wheel. Verification does not rebuild the wheel, modify the manual workspace, or create shortcuts. It needs no provider credentials; the packaged smoke may need network access for uncached locked dependencies and needs a temporary directory with adequate free space (set `TMPDIR` if needed). See [Installed-package verification](#installed-package-verification) for report details.
3. **Start:** Run the installed hub in the current terminal with data under `.release-test/manual-data`, separate from your normal RAH workspace. First launch provisions the three pinned apps and may need network access. Later launches keep current apps. To stop the hub, press **Ctrl+C** in the terminal that started it or close that console window. This can interrupt active operations. If provisioning is incomplete, retry in **App details**, or stop the dev hub and rerun the launcher. No desktop shortcut is created. Pass `--no-browser` for a headless launch.

Wheel builds stage the current source, including local edits, without reusing the checkout's `build/` or egg-info directories. The builder checks that the wheel's `rah` files match the staged files before installing it. If a pre-foreground build left a detached dev hub running, use its **Quit** action before launching the new build. Do not delete `instance.lock` to get past a running hub.

## Windows PowerShell

Use a Python 3.12.14 interpreter and run from the repository root:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: uv==0.13.9
.\.venv\Scripts\uv.exe pip sync --python .venv\Scripts\python.exe --require-hashes --only-binary=:all: dev-deps.lock
.\.venv\Scripts\python.exe -m pytest -q -m 'not live'
node --check rah/static/app.js
node --check rah/static/workspace.js
node --check rah/static/native_handoff.js
```

The offline suite needs no credentials and blocks non-loopback networking in the test process. To regenerate the dev lock without changing the runtime pins:

```bash
.venv/bin/uv pip compile --universal --python-version 3.12.14 --generate-hashes --only-binary :all: --extra dev -c hub-deps.lock pyproject.toml dev-tools.in -o dev-deps.lock
```

## Opt-in live OpenRouter checks

Provide an authorized `OPENROUTER_API_KEY` in the environment without putting it in shell history. This command uses the application's Usage client to read current-key metadata; it does not run inference:

```bash
.venv/bin/python -m pytest -q -m live --live-openrouter -k current_key_usage
```

The live inference tests use the following test-only defaults. The exports are optional; use these variables to override the models:

```bash
export RAH_LIVE_CHAT_MODEL='openai/gpt-4.1-nano'
export RAH_LIVE_EMBEDDING_MODEL='openai/text-embedding-3-small'
```

To authorize two potentially billable requests, run:

```bash
.venv/bin/python -m pytest -q -m live --live-openrouter --live-inference -k direct_inference
```

This sends one synthetic chat prompt (16 output tokens max) and one synthetic embedding input to OpenRouter and its routed providers. It sends no project content. These checks do not qualify upstream app routing or the managed PDF handoff.

## Installed-package verification

On Ubuntu and macOS, run `bash scripts/setup-dev.sh --verify` after setup. On Windows PowerShell, build and install the wheel from the repository root using the following commands. The bootstrap path must be new; for a rebuild, use fresh bootstrap and venv paths or move the previous ones aside.

```powershell
.\.venv\Scripts\python.exe scripts/bundle_hub.py --system-python `
  --uv "$PWD/.venv/Scripts/uv.exe" `
  --bundle "$PWD/.release-test/bootstrap" `
  --venv "$PWD/.release-test/installed-hub"
```

Then run the fixture and real pinned-upstream checks with the installed Python:

```powershell
.\.release-test\installed-hub\Scripts\python.exe tests/smoke_setup.py --uv "$PWD/.venv/Scripts/uv.exe" --report reports/setup.json
.\.release-test\installed-hub\Scripts\python.exe tests/smoke_setup.py --uv "$PWD/.venv/Scripts/uv.exe" --packaged --report reports/upstreams.json
```

On all platforms, the fixture smoke writes `reports/setup.json`, and the packaged smoke writes `reports/upstreams.json`. The packaged smoke verifies native browser readiness and GPT Researcher’s HTTP and WebSocket origins. It also exercises the pinned PaperQA2 transport with synthetic data and the pinned LDR RIS emitter. Reports omit exception messages and payloads; failed checks return a nonzero exit code. Add `--shortcut` to a manual smoke run only in a disposable desktop test account: the smoke removes RAH's shortcut on exit. See the [release checklist](docs/releases.md) for live and desktop steps.

`scripts/prepare_candidates.py` can generate a disposable fresh per-host resolution for investigation. Standard setup uses the reviewed lock files shipped in `rah/install_assets`. Treat lock regeneration and private route qualification as separate changes.

## Build a release-form ZIP

With Python 3.12.14 and uv 0.13.9, run from the repository root on each target. This build uses a separate bootstrap directory and writes the ZIP and checksum to `.release-test/release-assets`. If `.release-test/release-bootstrap` already exists, move it aside before rebuilding.

```bash
.venv/bin/python scripts/bundle_hub.py --uv "$PWD/.venv/bin/uv" \
  --bundle "$PWD/.release-test/release-bootstrap" \
  --release-dir "$PWD/.release-test/release-assets"
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts/bundle_hub.py `
  --uv "$PWD/.venv/Scripts/uv.exe" `
  --bundle "$PWD/.release-test/release-bootstrap" `
  --release-dir "$PWD/.release-test/release-assets"
```

Use the ZIP and detached checksum as candidate artifacts. Verify the normal desktop entry on each target. One confirmation provisions the hub and all three apps. Check first launch, shortcut, Ctrl+C and console-close shutdown, restart and reinstallation with existing data.

`rah setup` provisions core installations and can reuse a recorded absolute uv path. The workspace exposes targeted retries in **App details**. `rah serve` starts the local service and `rah open` opens an existing workspace. Back up the private data root and any separate workspace before exercising an in-place upgrade. The base schema and initial workspace migration are followed by `rah/workspace_v3.sql` (SQLite `user_version=2`). Normal project paths and paper edit revisions are preserved. Unfiled chat folder relocation resumes on startup if interrupted.

## Optional maintainer diagnostic for a changed PaperQA2 route

This diagnostic is for maintainers investigating a materially changed route. Normal setup and use rely on the packaged profile and its existing live evidence. Use the installed hub from the verification section above, Python 3.12.14 and an authorised OpenRouter test key. Each setup command below creates a fresh RAH data directory under `.release-test`. The script writes test model settings and creates a synthetic project there. Use a test key without prompt logging or external trace export. The script sends a small synthetic corpus and probes to OpenRouter and may incur charges. It never uses your research files.

1. From the repository root, create the disposable data directory and provision the core apps in one setup command. Omit `--no-shortcut` for a separate desktop-shortcut acceptance exercise.

   ```bash
   paperqa_data="$(mktemp -d "$PWD/.release-test/paperqa2-live.XXXXXXXX")"
   ./.release-test/current/installed-hub/bin/python -I -m rah.cli --data-dir "$paperqa_data" \
     setup --uv "$PWD/.venv/bin/uv" --no-shortcut --no-browser
   ```

   On Windows PowerShell:

   ```powershell
   $paperqaData = Join-Path "$PWD/.release-test" ("paperqa2-live-" + [guid]::NewGuid().ToString("N"))
   .\.release-test\installed-hub\Scripts\python.exe -m rah.cli --data-dir "$paperqaData" `
     setup --uv "$PWD/.venv/Scripts/uv.exe" --no-shortcut --no-browser
   ```

2. Once the apps are installed, stop the disposable hub with its Quit command, or run `rah remove-hub --no-shortcut` with that same data directory. The latter keeps the native apps and settings. Set `OPENROUTER_API_KEY` in your environment using your normal secret entry method. Run the live check from the same shell so it uses the data directory created in step 1.

   ```bash
   ./.release-test/current/installed-hub/bin/python scripts/live_paperqa2_dataflow.py \
     --live --require-hub --data-dir "$paperqa_data" --report reports/paperqa2-live.json
   ```

   On Windows PowerShell:

   ```powershell
   .\.release-test\installed-hub\Scripts\python.exe scripts/live_paperqa2_dataflow.py `
     --live --require-hub --data-dir "$paperqaData" --report reports/paperqa2-live.json
   ```

3. Inspect only `reports/paperqa2-live.json` for review. Exit `0` means PASS, `1` means FAIL, and `2` means INCONCLUSIVE for the observed checks. To repeat the check, create a fresh data directory. The installed-hub diagnostic uses the actual packaged profile and records the API error message on a failed hub request.

Without `--require-hub`, the command diagnoses the native transport and may use fallback test settings. That result does not qualify the installed hub. The single report covers Python-process observation and limits. Release review separately records any independent host capture and the desktop workflow evidence in [docs/releases.md](docs/releases.md).
