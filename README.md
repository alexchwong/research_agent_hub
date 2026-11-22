# Research Agent Hub

RAH is a local dashboard for Local Deep Research, GPT Researcher and PaperQA2. It helps install and open the apps, save original results, track papers, and view shared-key OpenRouter usage.

## Install and open

1. Download the matching ZIP from the official [release page](https://github.com/alexchwong/research_agent_hub/releases) and check its published SHA-256.
2. Extract it and run **Install.cmd** on Windows x64, **Install.command** on Apple silicon macOS, or **Install.sh** on Ubuntu x64. On Ubuntu, use the file manager's **Run as Program** action or run the script from the extracted folder.
3. Approve hub setup. It may download its pinned Python. Use the Research Agent Hub desktop shortcut for later sessions.
4. In the dashboard, select **Install three research apps**, review dependencies and approve.
5. Open LDR or GPT Researcher. For OpenRouter, enter the shared key once in **Settings**, then save and apply the model settings. Native settings are available for each app.
6. To analyse papers, choose **Analyse papers**, select a project and supplied PDFs, enter a question and review the recipients before approval. If managed analysis is unavailable, use the standalone command under PaperQA2’s **Advanced / Troubleshooting**.

Windows may require unblocking the verified ZIP in its File Properties before extracting it. If Ubuntu extraction drops the executable bit, enable it in the file properties.

Development ZIPs are candidate builds. See the [release checklist](docs/releases.md) for supported desktop and data-flow evidence. Manual bootstrap is in [DEVEL.md](DEVEL.md).

## Daily use

- **Apps** opens native LDR and GPT Researcher in the browser once they are ready. Their loopback ports are 58721 and 58722. PaperQA2 offers a browser analysis flow when qualified and shows its standalone command under Advanced. Add `ask "question"` to run it with your own corpus and native settings.
- **Settings** uses a private `hub/config/.env` under the RAH data directory. Set the OpenRouter key once, then choose separate chat and embedding defaults. An explicit `openrouter:model-id` overrides the relevant default. Enter `native` to release that role to upstream settings. Stop and reopen a browser app to apply saved launch settings. A legacy `RAH_PQA_OPENAI_API_KEY` entry is ignored and preserved in the file. Native provider and search credentials remain separate.
- **Workflows → Projects** retains original reports, answers and PDFs. Export RIS from native LDR, attach it and review the entries in **Papers Needed**. Paste and manual entry are also available. Obtain each PDF through your own access. Associate the file with its paper and select the files for analysis.
- **Managed PDF transfer** uses the shared OpenRouter key. After route qualification, the confirmation names OpenRouter and the onward provider for every role. The Zero Data Retention prompt opens OpenRouter Privacy Settings. The user decides whether to enable it. The handoff is available only for a reviewed installed route. Account ZDR status remains unverified by RAH. Standalone PaperQA2 is available from Apps.
- **Usage** reports shared-key USD figures and OpenRouter-reported BYOK fields. The total can include activity outside RAH. Direct provider and search-service charges are outside this view.

The four recipes and their human checkpoints are described in [docs/workflows.md](docs/workflows.md).

## Use the apps with the hub closed

Run `apps/<app>/launch/native.py` using the Python executable in that app’s active `versions/<install-id>/venv`. Find `install-id` in the app’s `current.json`. LDR opens its local server with no arguments. GPT Researcher's standalone command accepts its native CLI arguments or `--browser` to start its Classic UI at `http://127.0.0.1:58722/`. PaperQA2 accepts its CLI arguments and can load a native settings file selected in RAH.

Run `rah remove-hub` from the installed hub, then remove its virtual environment from outside it. Native installations, guarded launch scripts, saved settings and projects remain in the data directory.
