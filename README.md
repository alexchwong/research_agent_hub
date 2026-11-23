# Research Agent Hub

RAH is a local dashboard for Local Deep Research, GPT Researcher and PaperQA2. It helps install and open the apps, save original results, track papers, and view shared-key OpenRouter usage.

## Install and open

1. Download the matching ZIP and verify its published SHA-256. [Release page](https://github.com/alexchwong/research_agent_hub/releases)
2. Extract the ZIP. On Ubuntu x64, open a terminal in the extracted folder and run `sh Install.sh`. On Windows x64, open **Install.cmd**. On Apple silicon macOS 14 or later, open **Install.command** if permitted by your Mac’s security settings.
3. Confirm once to install RAH, Local Deep Research, GPT Researcher and PaperQA2. Setup shows progress in the terminal and opens the workspace. The core apps require disk space for three independent environments and access to download locked packages. For later sessions use the desktop shortcut or `rah open --foreground`. If an app failed to install, use its **Retry** action in **App details**.

Keep the hub's console open while using the workspace. To stop the hub, press **Ctrl+C** in that console or close the console window. Closing the browser tab does not stop the hub. Stopping the console can interrupt active hub operations; independently launched native apps do not depend on it.

On Windows, use File Properties to unblock the verified ZIP before extraction if Windows marks it as downloaded. The installer uses PowerShell’s RemoteSigned policy. On macOS, after verifying the ZIP, follow your normal Gatekeeper approval process if macOS blocks the application.

Development ZIPs are candidates. See [release evidence](docs/releases.md) for verified platforms and [DEVEL.md](DEVEL.md) for a manual bootstrap.

## Daily use

- **New chat** creates a standalone chat and its folder immediately. **New chat in project** creates a chat in that project. Use **Delete chat** to move its private files to Trash and **Settings → Chat Trash** to restore it.
- Write one question and choose **Open in… → GPT Researcher Classic** or **Local Deep Research**. The native form opens with an editable draft; click the native start button when ready. If prefill is unavailable, copy and paste the draft. In LDR, use **Save report to chat/project** or **Save RIS to chat/project** on an authenticated result opened in the handoff tab; native download and RAH attachment remain available.
- **Run GPT Researcher report in RAH…** is separate from Classic. It runs the CLI after you confirm the public question and review its model and search services. Classic writes native outputs to the chat or project folder it opened for; stop and reopen it to switch folders.
- **Settings → Search sources** defaults GPT Researcher to PubMed Central and Local Deep Research to PubMed. Neither requires a separate search key. For general web search, configure and test a local SearXNG instance. Search still sends queries to the chosen service; model credentials are separate.
- **PaperQA2** accepts PDFs directly or from a project. Add a question and any explicit excerpt you want to use. Review the selected files and the OpenRouter → Azure route, then approve once. Reopen the chat to read the native answer and download its references. Account ZDR is optional and managed through OpenRouter. **Show standalone command** is available for native settings and modes.
- **Settings → OpenRouter** stores scoped settings in the private `hub/config/.env`. With a saved key, unconfigured GPT Researcher and LDR chat roles default to `deepseek/deepseek-v4.1-flash-1017`. GPT Researcher embeddings default separately to `openai/text-embedding-3-small` through OpenRouter. Type in either model field to autocomplete from the OpenRouter catalogue via modelSpell, or enter a model ID manually. Explicit overrides and the reviewed PaperQA2 profile are preserved. Stop and reopen running native apps after saving changes. **Usage** reports current-key costs and coverage, including activity outside RAH.

The four recipes and their human checkpoints are described in [docs/workflows.md](docs/workflows.md).

### Workspace folders

| Platform | Default RAH data root |
| --- | --- |
| Windows 11 | `%LOCALAPPDATA%\Research Agent Hub\` |
| macOS | `~/Library/Application Support/Research Agent Hub/` |
| Linux | `$XDG_DATA_HOME/research-agent-hub/` or `~/.local/share/research-agent-hub/` if unset |

Projects use `projects/<project-id>/`, project chats use `projects/<project-id>/chats/<chat-id>/`, and standalone chats use `chats/<chat-id>/`. **Files**, **Open folder**, and **Copy path** use the active chat, or the project when no chat is selected. **Open folder** uses Explorer, Finder, or the Linux desktop file manager.

`--data-dir` changes the base for an entire RAH instance. To relocate only research files, choose a private local folder in **Settings → Workspace storage** (or enter its absolute path) and explicitly confirm **Migrate workspace**. RAH copies projects, chats, and Trash before switching, and retains a backup at the old location. Credentials, the database, and installed apps stay in the original data root. Stop native apps and finish or discard active research before migrating. On upgrade, legacy Unfiled chats become standalone; shared legacy material stays in its original project folder.

## Use the apps with the hub closed

Run `apps/<app>/launch/native.py` using the Python executable in that app’s active `versions/<install-id>/venv`. Find `install-id` in the app’s `current.json`. LDR opens its local server with no arguments. GPT Researcher's standalone command accepts its native CLI arguments or `--browser` to start its Classic UI at `http://127.0.0.1:58722/`. PaperQA2 accepts its CLI arguments and can load a native settings file selected in RAH.

Run `rah remove-hub` from the installed hub, then remove its virtual environment from outside it. Native installations, guarded launch scripts, saved settings and projects remain in the data directory.
