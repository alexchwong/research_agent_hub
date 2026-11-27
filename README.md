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

### Guided research

The workspace opens on the guided composer. Choose a **Purpose** and a **Research quality**, type a **Public research question** (or a **Public topic for survey** for Analyse my papers) and select **Start public research**.

1. **Public start.** The line under the question, built from your current settings, says where it will go (survey models and search, then the Scope Gate); **Recipients and prices** has the details. One click on **Start public research** starts the survey. RAH does not detect private text; rewrite anything private first.
2. **Survey.** GPT Researcher runs one short report (DeepSeek for every survey role, one generated query as a target, three results per query, a 2,048-token cap per request and a 3-minute deadline). A survey with zero sources is kept and labelled **No sources retrieved**; nothing runs after it until you retry.
3. **Scope questions.** One Haiku request asks 2–4 scope questions about the topic. If it fails, editable generic questions appear. Pick options, add custom text that replaces or adds to your choices, or tick **Use broad scope** to send your question unchanged. **Ask more questions** is available once. **Preview scope** shows the exact prompt, constraints and recipients; **Approve scope** approves exactly that.
4. **Downstream stages** follow the fixed recipe:

| Purpose | Stages after scope | Partial finish |
| --- | --- | --- |
| Literature review | LDR discovery (in RAH) → Papers Needed → PaperQA2 | **Finish with discovery only** after RIS review |
| Deep research | GPT Researcher deep report → Papers Needed → PaperQA2 | **Finish with report** |
| Rapid research | GPT Researcher research report | **Use survey as result** on the scope screen |
| Independent corroboration | LDR report, then GPT Researcher report, side by side | — |
| Analyse my papers | Choose PDFs → private question → PaperQA2 | — |

- **LDR stages** run inside RAH with LDR's Python API: no browser tab opens. The card shows LDR's own progress messages (and a percentage when LDR gives one), elapsed time against the 60-minute limit, then the LDR report inline with its RIS citations. LDR uses its defaults plus your RAH settings for LDR; settings saved in LDR's own interface don't apply, and runs aren't saved to LDR's history. If LDR returns no usable citations the stage fails with **Rerun this stage…**. Open LDR natively from **Classic apps** at any time.
- **Papers Needed** suggests rows from the confirmed RIS (literature) or the report's explicit references section (deep research). Save the rows you accept. Identifiers you type yourself stay private. With **Settings → Guided research → Automatically obtain available open-access PDFs** on, RAH sends only exact public DOIs and PMCIDs to the PMC ID Converter and PMC Cloud Service and saves eligible open-access PDFs; private identifiers are listed in one block and looked up with one click on **Look up N private identifiers at PMC**. Use the DOI, PubMed, PMC or library links, **Export requests** and **Import PDFs from folder…** for everything else.
- **PDF analysis** uses the exact PDFs you pick and a private question entered at that stage (**Use approved public question** copies the public prompt locally). RAH checks the transfer first and shows every file, role, model, provider and endpoint; **Send N PDFs to PaperQA2** then sends exactly that in one click. Managed PDF analysis needs a qualified route for the selected quality on your platform; otherwise open native PaperQA2 or use the recipe's partial finish.
- Stage cards show state, progress, models, approved recipients, source counts, the OpenRouter usage observed for that stage, GPT Researcher's own estimate and original outputs. Every button shows what it is doing as soon as you press it, and errors appear beside it with a fix where there is one. **Download named** saves an output as `NN_app_role_id.ext`. **Pause after this step**, **Cancel research**, **Rerun this stage…** and **Reuse verified output** are offered where safe.
- After a run, **Reuse corpus**, **Seek new literature** and **Revise scope** open a new editable draft. Each accepted request gets a new revision with a fresh public confirmation and survey.

Approvals don't expire and survive restarts. A changed model, search service, credential, app install, prompt or RAH runner pauses the next public stage at **Awaiting recipient re-confirmation**, naming what changed.

### Classic apps

Select **Classic apps** in the composer for the original app-by-app workflow.

- **New chat** creates a standalone chat and its folder immediately. **New chat in project** creates a chat in that project. Use **Delete chat** to move its private files to Trash and **Settings → Chat Trash** to restore it.
- Write one question and choose **Open in… → GPT Researcher Classic** or **Local Deep Research**. The native form opens with an editable draft; click the native start button when ready. If prefill is unavailable, copy and paste the draft. In LDR, use **Save report to chat/project** or **Save RIS to chat/project** on an authenticated result opened in the handoff tab; native download and RAH attachment remain available.
- **Run public GPT Researcher report** is separate from Classic. The line above it names the model and search services the question goes to; one click runs the CLI, and RAH refuses the run if those recipients changed. Classic writes native outputs to the chat or project folder it opened for; stop and reopen it to switch folders.
- **Settings → Search sources** defaults GPT Researcher to PubMed Central and Local Deep Research to PubMed. Neither requires a separate search key. For general web search, configure and test a local SearXNG instance. Search still sends queries to the chosen service; model credentials are separate.
- **PaperQA2** accepts PDFs directly or from a project. Add a question and any explicit excerpt you want to use. Attached PDFs are saved to the chat (local) first. RAH then checks the transfer and lists the files and the qualified Standard route (models, providers and endpoints per role); **Send N PDFs to PaperQA2** sends exactly that in one click. Reopen the chat to read the native answer and download its references. Account ZDR is optional and managed through OpenRouter. **Show standalone command** is available for native settings and modes.
- **Settings → OpenRouter** stores scoped settings in the private `hub/config/.env`. With a saved key, unconfigured GPT Researcher and LDR chat roles default to `deepseek/deepseek-v4.1-flash-1017`. GPT Researcher embeddings default separately to `openai/text-embedding-3-small` through OpenRouter. Type in either model field to autocomplete from the OpenRouter catalogue via modelSpell, or enter a model ID manually. Explicit overrides are preserved. PaperQA2 role settings apply to standalone PaperQA2 only. **Settings → Research models** shows the fixed guided models per quality and refreshes advertised prices. Stop and reopen running native apps after saving changes. **Usage** reports current-key costs and coverage, including activity outside RAH. Each managed chat entry also shows the OpenRouter usage RAH observed for it; native launches show "Not observed".

The guided recipes, classic recipes and their human checkpoints are described in [docs/workflows.md](docs/workflows.md).

### Workspace folders

| Platform | Default RAH data root |
| --- | --- |
| Windows 11 | `%LOCALAPPDATA%\Research Agent Hub\` |
| macOS | `~/Library/Application Support/Research Agent Hub/` |
| Linux | `$XDG_DATA_HOME/research-agent-hub/` or `~/.local/share/research-agent-hub/` if unset |

Projects use `projects/<project-id>/`, project chats use `projects/<project-id>/chats/<chat-id>/`, and standalone chats use `chats/<chat-id>/`. **Files**, **Open folder**, and **Copy path** use the active chat, or the project when no chat is selected. **Open folder** uses Explorer, Finder, or the Linux desktop file manager.

`--data-dir` changes the base for an entire RAH instance. To relocate only research files, choose a private local folder in **Settings → Workspace storage** (or enter its absolute path) and explicitly confirm **Migrate workspace**. RAH copies projects, chats, and Trash before switching, and retains a backup at the old location. Credentials, the database, and installed apps stay in the original data root. Stop native apps and finish or discard active research before migrating. On upgrade, legacy Unfiled chats become standalone; shared legacy material stays in its original project folder.

### Upgrades

The first start of this release upgrades the workspace database to schema 3. RAH first writes a verified copy to `hub/backups/state-pre-v3-<timestamp>.sqlite3` (with a `.json` record) and leaves files and folders where they are. If the upgrade cannot be verified, RAH leaves the database unchanged and names the backup. An older RAH cannot open an upgraded workspace; see [DEVEL.md](DEVEL.md#workspace-schema-3-upgrade-and-downgrade) to roll back.

## Use the apps with the hub closed

Run `apps/<app>/launch/native.py` using the Python executable in that app’s active `versions/<install-id>/venv`. Find `install-id` in the app’s `current.json`. LDR opens its local server with no arguments. GPT Researcher's standalone command accepts its native CLI arguments or `--browser` to start its Classic UI at `http://127.0.0.1:58722/`. PaperQA2 accepts its CLI arguments and can load a native settings file selected in RAH.

Run `rah remove-hub` from the installed hub, then remove its virtual environment from outside it. Native installations, guarded launch scripts, saved settings and projects remain in the data directory.
