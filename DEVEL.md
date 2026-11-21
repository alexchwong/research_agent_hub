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

Build the wheel, a hash-locked bootstrap bundle and a clean installed hub. Use empty absolute paths for the output directories.

```bash
.venv/bin/python scripts/bundle_hub.py --system-python \
  --uv "$PWD/.venv/bin/uv" --bundle /absolute/new-bootstrap --venv /absolute/new-installed-hub
```

Run the fixture and the real pinned upstream checks with the installed Python. Both are non-interactive and require no provider credentials. Add `--shortcut` only in a disposable desktop test account. The smoke removes RAH's shortcut on exit. See the [release evidence checklist](docs/releases.md) for the desktop and live checks that cannot be inferred from smoke results.

```bash
/absolute/new-installed-hub/bin/python tests/smoke_setup.py --uv "$PWD/.venv/bin/uv" --report reports/setup.json
/absolute/new-installed-hub/bin/python tests/smoke_setup.py --uv "$PWD/.venv/bin/uv" --packaged --report reports/upstreams.json
```

The real test uses the standard wheel path and three installed apps. It checks native browser readiness and GPT Researcher’s HTTP and WebSocket origins. It exercises the actual pinned PaperQA2 transport with synthetic data and the pinned LDR RIS emitter. It needs network access to install locked dependencies and uses no provider credentials. Use a disk-backed temporary directory with adequate free space. The standalone `tests/probe_paper_transport.py` also accepts `--report` when run with the pinned PaperQA2 interpreter and `--worker /absolute/path/to/rah/worker.py`. Reports omit exception messages and payloads; failed checks still return a nonzero exit code. See [handover.md](handover.md) for live and desktop steps.

`scripts/prepare_candidates.py` can generate a disposable fresh per-host resolution for investigation. Standard setup uses the reviewed lock files shipped in `rah/install_assets`. Treat lock regeneration and private route qualification as separate changes.
