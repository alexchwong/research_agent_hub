# Mandatory rule assessment

This argument is based on the product plan’s B1–B8 requirements and the technical v3 implementation. [releases.md](releases.md) records the remaining acceptance checks.

| Rule | How the implementation follows it |
|---|---|
| **B1 · Thin wrapper** | RAH coordinates explicit setup, settings, storage and consent actions. The upstream browser/CLI entry points do the research. The managed worker calls PaperQA2’s public `Settings`/`ask` API. Report comparison displays two originals. |
| **B2 · Upstream independence** | Each app has a separate final-path venv, stable native data directory and standalone launcher. The hub-only removal command keeps those files. GPT Researcher’s Classic interface runs from the upstream source archive through its native ASGI app. |
| **B3 · Native choice** | Compatible roles accept a separate chat or embedding default and an explicit override. The `native` choice relinquishes that role. Upstream advanced settings and CLI choices remain available. Active cloud behavior requires native verification. |
| **B4 · Small core** | The reviewed core plan enumerates exactly three pinned applications, prerequisites and dependencies. Its digest binds the approval to the plan. Setup works one app at a time and promotes it after preparing its standalone entry. Optional upstream services or later apps require separate decisions. |
| **B5 · Shared configuration** | The protected shared `.env` holds one OpenRouter credential. Each managed app receives its required scoped values. Native search/alternate provider settings are separate. Profile edits are checked and invalid app projections are reported independently. |
| **B6 · Monitoring only** | The dashboard shows current-key figures in USD and labels BYOK and stale or missing amounts. Key-level totals can include outside use. Usage is independent of research, setup and launches. |
| **B7 · Human-controlled handoff** | The user finds and supplies full texts, then selects files. A verified profile must identify its provider and model per role before a consent prompt can be offered. Approval binds the selection, hashes, settings and lock and is consumed once. The worker stages the approved subset after consent. The shipped manifests carry no private egress attestation. |
| **B8 · Fresh repository and release control** | The refactor preserves the fresh RAH implementation and records its dependency and approval evidence. The workflows use read-only repository permissions. The deliverable is an uncommitted repository ZIP. Live release evidence and dependency review are tracked explicitly. |
