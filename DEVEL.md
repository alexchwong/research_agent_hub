# Development

Keep research inside the native upstreams. Preserve each app’s documented entry points, settings and original outputs.

Use Python 3.12.14, uv 0.13.9 and Node for JavaScript syntax checking. From the repository root on Ubuntu or macOS:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --only-binary=:all: uv==0.13.9
.venv/bin/uv pip sync --python .venv/bin/python --require-hashes --only-binary=:all: dev-deps.lock
.venv/bin/python -m pytest -q -m 'not live'
node --check rah/static/app.js
```

On Windows PowerShell, with a 3.12.14 interpreter selected:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --only-binary=:all: uv==0.13.9
.\.venv\Scripts\uv.exe pip sync --python .venv\Scripts\python.exe --require-hashes --only-binary=:all: dev-deps.lock
.\.venv\Scripts\python.exe -m pytest -q -m 'not live'
node --check rah/static/app.js
```

Verify the interpreter is 3.12.14 before creating a fresh venv. These commands assume the working directory is the checkout root. The offline suite needs no credentials and blocks non-loopback networking in the test process. To regenerate the dev lock without changing the runtime pins:

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

Run from the repository root. The script creates the wheel and bootstrap files in `.release-test/bootstrap` and installs RAH into the separate test environment `.release-test/installed-hub`. The bootstrap directory must not already exist. To rebuild, move the previous `.release-test` directory aside first.

```bash
.venv/bin/python scripts/bundle_hub.py --system-python \
  --uv "$PWD/.venv/bin/uv" \
  --bundle "$PWD/.release-test/bootstrap" \
  --venv "$PWD/.release-test/installed-hub"
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\python.exe scripts/bundle_hub.py --system-python `
  --uv "$PWD/.venv/Scripts/uv.exe" `
  --bundle "$PWD/.release-test/bootstrap" `
  --venv "$PWD/.release-test/installed-hub"
```

Run the fixture and the real pinned upstream checks with the installed Python. Both are non-interactive and require no provider credentials. Add `--shortcut` only in a disposable desktop test account. The smoke removes RAH's shortcut on exit. See the [release evidence checklist](docs/releases.md) for the desktop and live checks that cannot be inferred from smoke results.

```bash
./.release-test/installed-hub/bin/python tests/smoke_setup.py --uv "$PWD/.venv/bin/uv" --report reports/setup.json
./.release-test/installed-hub/bin/python tests/smoke_setup.py --uv "$PWD/.venv/bin/uv" --packaged --report reports/upstreams.json
```

On Windows PowerShell:

```powershell
.\.release-test\installed-hub\Scripts\python.exe tests/smoke_setup.py --uv "$PWD/.venv/Scripts/uv.exe" --report reports/setup.json
.\.release-test\installed-hub\Scripts\python.exe tests/smoke_setup.py --uv "$PWD/.venv/Scripts/uv.exe" --packaged --report reports/upstreams.json
```

The real test uses the standard wheel path and three installed apps. It checks native browser readiness and GPT Researcher’s HTTP and WebSocket origins. It exercises the actual pinned PaperQA2 transport with synthetic data and the pinned LDR RIS emitter. It needs network access to install locked dependencies and uses no provider credentials. Use a disk-backed temporary directory with adequate free space. The packaged smoke already runs `tests/probe_paper_transport.py` with the installed PaperQA2 interpreter. Its result is included in `reports/upstreams.json`. Reports omit exception messages and payloads; failed checks still return a nonzero exit code. See [release checklist](docs/releases.md) for live and desktop steps.

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

Use the ZIP and detached checksum as candidate artifacts. Verify the normal desktop entry, then install the three apps from the dashboard on the actual target OS. Check first run, shortcut, restart, and reinstallation with existing data.

## PaperQA2 live verification — one command, one JSON

Use the installed hub from the verification section above, Python 3.12.14 and an authorised OpenRouter test key. Each setup command below creates a fresh RAH data directory under `.release-test`. The script writes test model settings and creates a synthetic project there. Use a test key without prompt logging or external trace export. The script sends a small synthetic corpus and probes to OpenRouter and may incur charges. It never uses your research files.

1. From the repository root, create the disposable data directory and start RAH. Then install the core apps in the dashboard. Omit `--no-shortcut` for a separate desktop-shortcut acceptance exercise.

   ```bash
   paperqa_data="$(mktemp -d "$PWD/.release-test/paperqa2-live.XXXXXXXX")"
   ./.release-test/installed-hub/bin/python -m rah.cli --data-dir "$paperqa_data" \
     setup --uv "$PWD/.venv/bin/uv" --no-shortcut
   ```

   On Windows PowerShell:

   ```powershell
   $paperqaData = Join-Path "$PWD/.release-test" ("paperqa2-live-" + [guid]::NewGuid().ToString("N"))
   .\.release-test\installed-hub\Scripts\python.exe -m rah.cli --data-dir "$paperqaData" `
     setup --uv "$PWD/.venv/Scripts/uv.exe" --no-shortcut
   ```

2. Once the apps are installed, quit the dashboard for that data directory. Set `OPENROUTER_API_KEY` in your environment using your normal secret entry method. Run the live check from the same shell so it uses the data directory created in step 1.

   ```bash
   ./.release-test/installed-hub/bin/python scripts/live_paperqa2_dataflow.py \
     --live --require-hub --data-dir "$paperqa_data" --report reports/paperqa2-live.json
   ```

   On Windows PowerShell:

   ```powershell
   .\.release-test\installed-hub\Scripts\python.exe scripts/live_paperqa2_dataflow.py `
     --live --require-hub --data-dir "$paperqaData" --report reports/paperqa2-live.json
   ```

3. Return **only `reports/paperqa2-live.json`** for review. Exit `0` means PASS for the script's observations, `1` means FAIL and `2` means INCONCLUSIVE. To repeat the full check, start at step 1 to create a fresh data directory. In release-candidate mode the script uses a temporary in-memory route record while the shipping gate remains closed. It exercises the normal APIs and the installed native worker. The report states whether it exercised a final packaged verified row or the in-memory candidate.

Without `--require-hub`, the command diagnoses the native transport and may use fallback test settings. That result does not qualify the installed hub. The single report covers Python-process observation and limits. Release review separately records any independent host capture and the desktop workflow evidence in [docs/releases.md](docs/releases.md).
