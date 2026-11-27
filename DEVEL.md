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
node --check rah/static/guided.js
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

GitHub Actions uses uv 0.13.9 to provision CPython 3.12.14 on all three runner platforms. Both workflows build a release ZIP, verify its checksum, and run the shipped launcher in a disposable data root. The launcher runs its normal checks and installs the hub, but stops before starting a foreground desktop session. The smoke checks then use the installed hub and retained uv to run `rah setup`: the fixture check injects an intentional lock failure, and the packaged check uses the same three app routes as a normal install. The developer wheel route shares the release installer's locked wheelhouse sync; it keeps its separate development layout.

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

On all platforms, the fixture smoke writes `reports/setup.json`, and the packaged smoke writes `reports/upstreams.json`. The packaged smoke verifies native browser readiness and GPT Researcher’s HTTP and WebSocket origins. It also exercises the pinned PaperQA2 transport with synthetic data and the pinned LDR RIS emitter. Reports omit exception messages and payloads; failed checks return a nonzero exit code. The packaged smoke runs the managed PaperQA2 transport probe against `rah/pqa_profile_worker.py`. Add `--shortcut` to a manual smoke run only in a disposable desktop test account: the smoke removes RAH's shortcut on exit. See the [release checklist](docs/releases.md) for live and desktop steps.

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

`rah setup` provisions core installations and can reuse a recorded absolute uv path. The workspace exposes targeted retries in **App details**. `rah serve` starts the local service and `rah open` opens an existing workspace. Back up the private data root and any separate workspace before exercising an in-place upgrade. The base schema and initial workspace migration are followed by `rah/workspace_v3.sql` (SQLite `user_version=2`) and `rah/workspace_schema_3.sql` (`user_version=3`). Normal project paths and paper edit revisions are preserved. See [Workspace schema 3 upgrade and downgrade](#workspace-schema-3-upgrade-and-downgrade). Unfiled chat folder relocation resumes on startup if interrupted.

## Workspace schema 3 upgrade and downgrade

Startup checks the stored SQLite `user_version` before touching workspace files. A newer version is refused. The chain runs `schema.sql`, `workspace.sql`, `workspace_v3.sql` (`user_version=2`) and then `rah/workspace_schema_3.sql` (`user_version=3`). Before schema 3, RAH holds a reserved write lock, writes a backup through the SQLite backup API to `hub/backups/state-pre-v3-<timestamp>.sqlite3`, runs `integrity_check` on it and records its version and SHA-256 beside it. The rebuild runs with foreign keys off inside one `BEGIN IMMEDIATE` transaction and copies rows by column. It checks row counts, preserved IDs, `foreign_key_check` and `integrity_check` before committing, then checks again on reopen. Any mismatch rolls back and stops startup with the backup location. No file or folder moves.

To downgrade: stop the hub, keep a copy of the current data root (schema 3 changes made after the backup cannot be represented in schema 2), then replace `hub/state.sqlite3` with the pre-v3 backup and restore the workspace files from the same point in time. Do not run older code against an upgraded database.

## Purpose pipeline verification

### P0 probes against installed pins

These run offline with the installed native environments and write JSON reports. They intercept traffic at the installed package transports and block real sockets.

```bash
# GPT Researcher guided survey route: effective settings and observed request bodies.
.venv/bin/python -c "import json; from rah import gptr_pipeline; print(json.dumps(gptr_pipeline.guided_values({'RETRIEVER': 'pubmed_central'}, 'synthetic-key', 'survey', 'standard') | {'PATH': '/usr/bin:/bin', 'LITELLM_LOCAL_MODEL_COST_MAP': 'True'}))" > /tmp/survey-env.json
"$GPTR_VENV/bin/python" tests/probe_gptr_survey.py --source "$GPTR_INSTALL/source" --env /tmp/survey-env.json --report reports/p0-gptr.json
# Managed PaperQA2 candidate routes through the installed pinned clients.
(cd tests && "$PQA_VENV/bin/python" probe_paper_transport.py --worker ../rah/pqa_profile_worker.py --route pqa-standard-v1)
# LDR RIS emitter, open-access downloader contract and programmatic quick_summary feasibility.
(cd tests && "$LDR_VENV/bin/python" probe_ldr_feasibility.py --report ../reports/p0-ldr.json)
```

`$GPTR_INSTALL`, `$GPTR_VENV`, `$PQA_VENV` and `$LDR_VENV` are the active `apps/<app>/versions/<install-id>` directory and its `venv` in a disposable data root. The offline pytest suite covers P1–P5 with fake transports and a fake GPT Researcher CLI.

### P6 live qualification

P6 is one owner-authorised campaign. Run it with the installed hub's Python after the offline gates pass. Put the key in your shell with your normal secret entry method:

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
./.release-test/current/installed-hub/bin/python scripts/live_rah_refactor.py --profile all --output reports/rah-live.json
```

On Windows use `.\.release-test\installed-hub\Scripts\python.exe` and `$env:OPENROUTER_API_KEY`. The runner provisions a temporary data root with the retained `uv` (pass `--uv` if it is not on `PATH`) and never reads your real data root. It prompts once before public calls, then for each PaperQA2 route's exact provider tuple and synthetic corpus, including a decline probe. Optional inputs: `RAH_LIVE_GPTR_RETRIEVER`, `RAH_LIVE_TAVILY_API_KEY` or `RAH_LIVE_SEARX_URL` for search; `RAH_OA_PMC_CONTACT_EMAIL` for DOI conversion; `RAH_LIVE_OA_PMCID` for a known open-access PMCID; and `RAH_LIVE_PQA_<PROFILE>_<ROLE>_PROVIDER` to propose a provider instead of the first catalogued endpoint. Use `--profile budget|standard|premium` for a targeted repeat and `--keep` to inspect the disposable root.

The report (`schema_version` 1) lists every check with status, model and provider where known, a redacted diagnostic and remediation. Exit `0` means every requested mandatory managed check passed, `1` means an automated check failed and `2` means a mandatory check was skipped or not approved. Outstanding manual checks (LDR native discovery on a desktop and the three-host matrix) are always printed.

A passing `pqa_route` check includes an `enable_entry`. A maintainer enables that route only after owner approval by adding the entry to the route's `enabled` tuple in `rah/pqa_profiles.py`. The offline suite then proves the shipped entry matches the packaged worker digest, lock and Python identity; any change to those requires fresh qualification.
