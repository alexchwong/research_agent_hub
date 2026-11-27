# Mandatory rule assessment

This assessment applies the product plan’s B1–B8 requirements to the final workspace v2 implementation. [releases.md](releases.md) records the remaining acceptance checks.

| Rule | How the implementation follows it |
|---|---|
| **B1 · Thin wrapper** | RAH coordinates explicit setup, settings, storage and consent actions. The upstream browser/CLI entry points do the research. The managed worker calls PaperQA2’s public `Settings`/`ask` API. Report comparison displays two originals. |
| **B2 · Upstream independence** | Each app has a separate final-path venv, stable native data directory and standalone launcher. The hub-only removal command keeps those files. GPT Researcher’s Classic interface runs from the upstream source archive through its native ASGI app. |
| **B3 · Native choice** | Compatible roles accept a separate chat or embedding default and an explicit override. The `native` choice relinquishes that role. Upstream advanced settings and CLI choices remain available. Active cloud behavior requires native verification. |
| **B4 · Small core** | The installer plan enumerates exactly three pinned applications, prerequisites and dependencies. Confirmation or an explicit CLI setup authorises those installs. Setup works one app at a time and promotes it after preparing its standalone entry. Optional upstream services or later apps require separate decisions. |
| **B5 · Shared configuration** | The protected shared `.env` holds one OpenRouter credential. Each managed app receives its required scoped values. Native search/alternate provider settings are separate. Profile edits are checked and invalid app projections are reported independently. |
| **B6 · Monitoring only** | The dashboard shows current-key figures in USD and labels BYOK and stale or missing amounts. Key-level totals can include outside use. Usage is independent of research, setup and launches. |
| **B7 · Human-controlled handoff** | The user finds and supplies full texts, then selects files. A verified profile must identify its provider and model per role before a consent prompt can be offered. Approval binds the selection, hashes, settings and lock and is consumed once. The worker stages the approved subset after consent. The trusted manifest records the reviewed profile and the limits of its provider evidence. |
| **B8 · Fresh repository and release control** | The refactor preserves the fresh RAH implementation and records its dependency and approval evidence. The workflows use read-only repository permissions. The deliverable is an uncommitted changed-path ZIP. Live release evidence and dependency review are tracked explicitly. |

## 0.1.0 workspace application

The installer’s affirmative confirmation or explicit CLI setup is the installation decision for the three mandatory apps. Optional software requires a separate explicit decision. The workspace stores app actions and native artifacts locally. Each managed PDF handoff requires approval for the specific inputs and verified recipients. The implementer records boundary compliance in the [release audit](releases.md).

## Purpose pipeline refactor (B1–B8 trace)

| Rule | Amendment and implementation trace |
|---|---|
| **B1 · No agentic harness** | One prompted survey in sealed GPT Researcher mode per guided run, a questionnaire-only Scope Gate with at most one user-requested second round, deterministic continuation of five user-selected fixed recipes (`rah/pipelines.py`), and exact-identifier open-access transport (`rah/oa.py`). The Gate cannot plan, browse, use tools, pick apps, choose answers or decide next actions; RAH validates its output shape and the user answers. Planning, relevance, synthesis and tool choice stay upstream. |
| **B2 · Independent upstreams** | Native LDR handoff with prefill and authenticated export; the SDK route was not adopted after P0. The pinned downloader failed its contract, so a narrow identifier-only PMC adapter is used. No fork, patched package or reproduced research library. Native history and standalone launch are unchanged. |
| **B3 · Native choice** | Exactly six curated models across three guided qualities (`rah/research_profiles.py`). Classic apps, advanced settings and native modes stay separate and are never overwritten by guided runs. |
| **B4 · Three core apps** | The same three installs, pins and locks. No new dependency. |
| **B5 · Honest settings** | The central private `.env` adds only the open-access opt-in, PMC contact and OpenURL base. Standard is the guided default on fresh and upgraded installs. Per-run projections and route fingerprints bind each approval to its scope; native processes keep their own settings and LDR overrides are shown as unobserved and attested by the user. |
| **B6 · Monitor costs** | Advertised OpenRouter prices from the cached catalogue, native cost estimates and directly observed Scope Gate usage where returned, plus shared-key Usage. No ceilings or billing claims; query counts are targets. |
| **B7 · Private data control** | Open-access lookups send only exact public identifiers when opted in; private-origin identifiers need per-destination approval. Every PaperQA2 run reviews the actual files, question and per-role providers. Manual acquisition and explicit subset selection remain. |
| **B8 · Release discipline** | Transactional schema upgrade with a verified backup, shipped worker digest and disabled-candidate gate, separated P0–P5 and P6 evidence, a changed-path ZIP and no commit, push or release. |
