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
- LDR uses its complete native workflow with both result-return paths (authenticated export and user-imported file). The programmatic SDK route failed P0 (no typed citations) and is not shipped.
- The pinned LDR downloader failed the P0 contract. Automatic open-access download uses the official PMC ID Converter and PMC Cloud Service for exact identifiers, off by default; other articles stay in Papers Needed for manual acquisition.
- Managed PaperQA2 retires GPT-4.1 Mini and `openrouter-text-v1`. The new per-quality routes ship as disabled candidates; Standard must qualify on every release platform, Budget and Premium are enabled independently.
- Workspace schema 3 upgrades in place after a verified backup; downgrade is a restore of that backup plus matching files.
