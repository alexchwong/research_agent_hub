# Decisions

- Technical spec v3 governs the RAH 0.1.0 refactor. OpenRouter is the managed route for all demonstrated compatible model roles. Chat and embeddings have separate defaults. Users can select upstream-native configuration for standalone use.
- The initial managed PaperQA2 profile uses one verified onward provider per role, OpenRouter as the common gateway and local text-only PDF parsing. Every run requires its own confirmation. Zero Data Retention is an optional OpenRouter account setting linked from the confirmation.
- GPT Researcher's Classic browser UI ships through the same pin and environment as its CLI. One guarded loopback listener serves the native browser stack.
- The installer confirmation or explicit `rah setup` covers LDR, GPT Researcher and PaperQA2. The normal workspace opens directly. Final-path environments allow installs and native data to outlive the hub.
- Windows 11 x64, Apple silicon macOS and Ubuntu x64 are the desktop targets. The Windows Server CI runner covers installer and native startup behaviour. The pinned FAISS wheel establishes macOS 14 as the minimum candidate version.
- RAH-managed local models are deferred to consideration in 0.4.0 or later.

- Chat entries record explicit app actions within projects. Directly attached PDFs need no Papers Needed association. Each managed PDF transfer receives one approval for its ordered, explicitly selected corpus and question.

## Purpose pipeline refactor

- Technical specification v2.0 (27 November 2026) and the owner decisions after review of the Purpose-Oriented Research Refactor Proposal v2.0 govern the refactor. Package metadata stays at 0.1.0; the release number is reserved for owner scheduling after 0.1.0, without changing the 0.2.0 and 0.3.0 optional-app scope.
- Guided scope changes start a fresh survey in a new revision. Fresh and upgraded installs default to the guided composer with Literature review and Standard; projects are optional.
- RAH's only model use is the questionnaire-only Scope Gate (Haiku). Survey roles all use DeepSeek; downstream roles use the six-model catalogue. RAH adds no model fallback and sets no reasoning effort.
- ~~LDR uses its complete native workflow with both result-return paths~~ Superseded by the chat-first decisions below: guided LDR stages run headlessly.
- The pinned LDR downloader failed the P0 contract. Automatic open-access download uses the official PMC ID Converter and PMC Cloud Service for exact identifiers, off by default; other articles stay in Papers Needed for manual acquisition.
- Managed PaperQA2 retires GPT-4.1 Mini and `openrouter-text-v1`. The new per-quality routes ship as disabled candidates; Standard must qualify on every release platform, Budget and Premium are enabled independently.
- Workspace schema 3 upgrades in place after a verified backup; downgrade is a restore of that backup plus matching files.

## Chat-first guided research (technical specification v2, 27 November 2026)

- Guided LDR stages run headlessly in RAH through LDR's documented `local_deep_research.api.quick_summary`, and its own `RISExporter` converts the returned `formatted_findings`. This supersedes "SDK route not shipped" and "no `rah/ldr_pipeline.py`": the P0 objection (no typed RIS) is met because the native export runs the same exporter over the same Markdown. The installed probe (`tests/probe_ldr_headless.py`) passed before the guided native handoff was deleted.
- Guided workflows have no native handoff and never open a browser. Native LDR and GPT Researcher stay available from **Classic apps → Open in…** as a separate pipeline. Settings saved inside LDR's own interface do not apply to headless runs (LDR defaults plus RAH's LDR settings; default strategy `langgraph-agent`). The headless deadline is 60 minutes.
- `ldr_discovery` succeeds only with a non-zero source count and an RIS with at least one titled row that has a valid URL or DOI.
- Re-confirmation happens only on a material route change (install, lock, models, search, credentials, prompt, runner/observer). Elapsed time and hub restarts no longer require it. The reason names the changed dimension.
- One-click public start, one server-preflighted PDF send and one action per batch of private identifier lookups replace the earlier dialogs and checkboxes; every server binding is kept.
- Every model-driven entry records OpenRouter usage observed in its own process. A figure is "complete" only on a route listed in `usage.COVERAGE`, which ships empty and gains entries only from passing installed probes with owner approval. Nothing is derived from shared-key totals.
- Workspace schema 4 adds `chat_entry.usage_json` and closes in-flight runs, steps and entries. No backup or downgrade path and no backward compatibility (owner decision).
