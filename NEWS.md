# News

## Purpose pipeline refactor (unreleased)

Package metadata stays at 0.1.0. This work ships as a later minor whose number the owner assigns; Co-STORM/SearchClaw keep 0.2.0 and the optional apps keep 0.3.0.

- The workspace opens on a guided composer: choose **Literature review**, **Deep research**, **Rapid research**, **Independent corroboration** or **Analyse my papers**, and **Research quality** (Budget, Standard or Premium; Standard is the default on fresh and upgraded installs).
- Every guided request starts with an explicitly confirmed public survey in GPT Researcher, then a questionnaire-only Scope Gate. You answer the questions and approve the exact prompt before any downstream app runs.
- Recipes are fixed in code. LDR runs in its own browser tab with a prefilled, unsubmitted prompt and authenticated Markdown/RIS export to the waiting stage. GPT Researcher deep and rapid reports run in sealed per-run settings with process-group timeouts.
- Papers Needed gains a per-run list, request exports, folder import and optional automatic open-access download from the official PMC dataset for exact public DOIs and PMCIDs (off by default).
- Managed PaperQA2 uses new per-quality routes. The retired GPT-4.1 Mini profile no longer runs; historical answers stay readable. New routes start disabled until each passes live qualification on a platform.
- Workspace schema 3 adds runs, steps, approvals, artifact roles and lineage. The upgrade makes a verified backup first and refuses databases from newer releases.
- Classic apps, native launchers, the legacy composer and standalone operation remain available.

## 0.1.0 development refactor

- Initial setup installs RAH and all three core apps. App details provides scoped retries and native launch options.
- Projects group chats, files and Papers Needed. Native outputs stay with the originating conversation.
- Managed PaperQA2 accepts directly attached PDFs and one explicit approval for the reviewed OpenRouter/Azure profile. The native CLI remains available for other configurations.
- Settings applies saved credentials and roles in one action, with separate native provider and search settings under Advanced.
