# Research Agent Hub

RAH is a local dashboard for Local Deep Research, GPT Researcher and PaperQA2. It helps install and open the apps, save original results, track papers, and view shared-key OpenRouter usage.

## Install and open

1. Download the matching ZIP and verify its published SHA-256. [Release page](https://github.com/alexchwong/research_agent_hub/releases)
2. Extract the ZIP. On Ubuntu x64, open a terminal in the extracted folder and run `sh Install.sh`. On Windows x64, open **Install.cmd**. On Apple silicon macOS 14 or later, open **Install.command** if permitted by your Mac’s security settings.
3. Confirm once to install RAH, Local Deep Research, GPT Researcher and PaperQA2. Setup shows progress in the terminal and opens the workspace. The core apps require disk space for three independent environments and access to download locked packages. For later sessions use the desktop shortcut or `rah open`. If an app failed to install, use its **Retry** action in **App details**.

On Windows, use File Properties to unblock the verified ZIP before extraction if Windows marks it as downloaded. The installer uses PowerShell’s RemoteSigned policy. On macOS, after verifying the ZIP, follow your normal Gatekeeper approval process if macOS blocks the application.

Development ZIPs are candidates. See [release evidence](docs/releases.md) for verified platforms and [DEVEL.md](DEVEL.md) for a manual bootstrap.

## Daily use

- **New chat** opens a blank composer. Choose an app and submit a request to start the chat. Projects group chats and files. New requests outside a named project go to **Unfiled**.
- **Local Deep Research** opens its native web interface. Enter the task there, then attach its exported report or RIS to the originating chat. **Papers Needed** previews RIS, accepts pasted and manual citations, and tracks which PDFs you have supplied.
- **GPT Researcher** accepts a public question and report mode. The managed action discloses its model and search services. Native modes remain available from **Open native app** and **App details**.
- **PaperQA2** accepts PDFs directly or from a project. Add a question and any explicit excerpt you want to use. Review the selected files and the OpenRouter → Azure route, then approve once. Reopen the chat to read the native answer and download its references. Account ZDR is optional and managed through OpenRouter. **Show standalone command** is available for native settings and modes.
- **Settings** stores scoped settings in the private `hub/config/.env`. Add your OpenRouter key and save. Apply the recommended PaperQA2 models if needed. **Advanced** exposes shared model defaults, app overrides and separate provider/search credentials. **Usage** reports current-key OpenRouter costs and coverage, including activity outside RAH.

The four recipes and their human checkpoints are described in [docs/workflows.md](docs/workflows.md).

## Use the apps with the hub closed

Run `apps/<app>/launch/native.py` using the Python executable in that app’s active `versions/<install-id>/venv`. Find `install-id` in the app’s `current.json`. LDR opens its local server with no arguments. GPT Researcher's standalone command accepts its native CLI arguments or `--browser` to start its Classic UI at `http://127.0.0.1:58722/`. PaperQA2 accepts its CLI arguments and can load a native settings file selected in RAH.

Run `rah remove-hub` from the installed hub, then remove its virtual environment from outside it. Native installations, guarded launch scripts, saved settings and projects remain in the data directory.
