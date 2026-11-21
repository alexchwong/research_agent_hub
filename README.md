# Research Agent Hub

RAH is a local dashboard for Local Deep Research, GPT Researcher and PaperQA2. It helps install and open the apps, save original results, track papers, and view shared-key OpenRouter usage.

**Development build.** The wheel includes candidate install routes for Ubuntu x64, Windows 11 x64 and Apple silicon macOS 14 or later. A packaged install and native browser startup are recorded on Debian x64; the target desktop and live-provider acceptance checks remain open. See [handover.md](handover.md) for verification status and native troubleshooting.

## Bootstrap from a built host bundle

Build the host bootstrap wheelhouse from this repository with the commands in [DEVEL.md](DEVEL.md), or use the matching GitHub CI artifact. Verify its published hashes and use the pinned uv binary. Replace the example paths with absolute paths. Keep the installed Python interpreter for retained native apps.

On Ubuntu or macOS:

```bash
uv_bin=/absolute/path/to/uv
bundle=/absolute/path/to/host-bootstrap
rah_venv=/absolute/private/path/to/rah-venv
python_pin="$(cat "$bundle/python-version.txt")"
"$uv_bin" python install "$python_pin"
"$uv_bin" venv --managed-python --python "$python_pin" "$rah_venv"
"$uv_bin" pip sync --python "$rah_venv/bin/python" --require-hashes --only-binary :all: \
  --no-index --find-links "$bundle/wheelhouse" "$bundle/hub.lock"
"$rah_venv/bin/rah" setup --uv "$uv_bin"
```

On Windows PowerShell:

```powershell
$uvBin = "C:\Path\To\uv.exe"
$bundle = "C:\Path\To\host-bootstrap"
$rahVenv = "$env:LOCALAPPDATA\Research Agent Hub\hub\venv"
$pin = (Get-Content "$bundle\python-version.txt" -Raw).Trim()
& $uvBin python install $pin
& $uvBin venv --managed-python --python $pin $rahVenv
& $uvBin pip sync --python "$rahVenv\Scripts\python.exe" --require-hashes --only-binary :all: `
  --no-index --find-links "$bundle\wheelhouse" "$bundle\hub.lock"
& "$rahVenv\Scripts\rah.exe" setup --uv $uvBin
```

Open **Apps → Install core apps**, review the plan and confirm. An incomplete setup offers **Retry incomplete apps**. Only those apps are retried. The install fetches hash-locked wheels and uses a separate environment for each app. Choose a private data location with sufficient disk space.

## Daily use

- **Apps** opens native LDR and GPT Researcher in the browser once they are ready. Their loopback ports are 58721 and 58722. PaperQA2 shows its standalone terminal command. Add `ask "question"` to run it with your own corpus and native settings.
- **Settings** uses a private `hub/config/.env` under the RAH data directory. Set the OpenRouter key once, then choose separate chat and embedding defaults. An explicit `openrouter:model-id` overrides the relevant default. Enter `native` to release that role to upstream settings. Stop and reopen a browser app to apply saved launch settings. A legacy `RAH_PQA_OPENAI_API_KEY` entry is ignored and preserved in the file. Native provider and search credentials remain separate.
- **Workflows → Projects** retains original reports, answers and PDFs. Export RIS from native LDR, attach it and review the entries in **Papers Needed**. Paste and manual entry are also available. Obtain each PDF through your own access. Associate the file with its paper and select the files for analysis.
- **Managed PDF transfer** uses the shared OpenRouter key. After route qualification, the confirmation names OpenRouter and the onward provider for every role. The Zero Data Retention prompt opens OpenRouter Privacy Settings. The user decides whether to enable it. This build keeps the handoff unavailable pending live data-flow and routing evidence. Standalone PaperQA2 is available from Apps.
- **Usage** reports shared-key USD figures and OpenRouter-reported BYOK fields. The total can include activity outside RAH. Direct provider and search-service charges are outside this view.

The four recipes and their human checkpoints are described in [docs/workflows.md](docs/workflows.md).

## Use the apps with the hub closed

Run `apps/<app>/launch/native.py` using the Python executable in that app’s active `versions/<install-id>/venv`. Find `install-id` in the app’s `current.json`. LDR opens its local server with no arguments. GPT Researcher's standalone command accepts its native CLI arguments or `--browser` to start its Classic UI at `http://127.0.0.1:58722/`. PaperQA2 accepts its CLI arguments and can load a native settings file selected in RAH.

Run `rah remove-hub` from the installed hub, then remove its virtual environment from outside it. Native installations, guarded launch scripts, saved settings and projects remain in the data directory.
