# Release verification

Package from a clean checkout or export. Exclude local environments, caches, reports and populated `.env` files. Retain sanitised evidence separately. The release workflows target Ubuntu 26.04 x64, macOS 15 arm64 and Windows Server 2025 x64. A Windows Server run checks the package and native startup. Windows 11 desktop acceptance is a separate observation.

## 0.1.0 workspace audit

Baseline `fb25fd96e1174c4af745a926709745ea93c663f6`. Implementer environment Debian 13.7 x64, Python 3.12.14 and uv 0.13.9. The UI walkthrough used Chromium at 1440 and 390 pixels wide. Managed execution in the walkthrough used controlled native responses and no live credentials. Prior PaperQA2 provider evidence appears below.

| Audit package | SHA-256 |
|---|---|
| RAH 0.1.0 installed wheel | `a48651ea4bfad3c9b98e7a929e5e700107c927d86b59b287169178e44f9d3548` |
| Linux x64 release-form candidate | `bea7a5b29a2a28d93870e67b6ed278840e2c7692493789f209950783f4130ef8` |
| Packaged route manifest | `8f4c55fa7d9b7c5a414896967f28a0de69f7d1adbe7870f89143889ca8affd8a` |
| Linux x64 LDR lock | `cce054be3f65d79aa739f406caf63740f80efe6f7234358a18d3d1eb29e86d4e` |
| Linux x64 GPT Researcher lock | `9dca24204542f67d49f8719fe0a387ca27f460220f95970f85e2d09275f61725` |

The offline suite passed 18 checks and skipped its three opt-in live checks. Both installed-wheel smokes passed.

| Spec check | Outcome and evidence |
|---|---|
| A1 · Setup and repair | The installed wheel provisioned all three real pins in a fresh Linux data directory. A separate fixture check forced a PaperQA2 lock failure during CLI setup, observed a nonzero exit, retried just that app and verified both successful pointers were unchanged. Matching setup reused the authenticated hub and the stored uv path. The desktop launchers are pending platform acceptance. |
| A2 · UI and settings | The fresh workspace opened on the composer with no install screen or app versions. Chromium showed a draft surviving the Settings round trip, the recommended setting preview, one Save, and the standalone command with no managed key. Checked desktop and narrow navigation, including the collapsed sidebar and keyboard-accessible form names. |
| A3 · Chats and persistence | Walked the Unfiled native command, named-project chat, follow-ups, app switching, rename, reload and Back/Forward. Opened the saved chat at a new launcher origin after restart. Inspected project/chat storage. A disposable v1 database kept its legacy artifact path and papers revision after the additive migration. Duplicate acceptance returned the original chat and changing its owner or intent was rejected. |
| A4 · Cross-app flow | Started the pinned LDR native service, attached a RIS fixture in its originating chat, and previewed and saved it in Papers Needed. The packaged smoke also imported a RIS emitted by the pinned LDR exporter. Submitted public GPT Researcher questions with controlled responses, then selected a quote and private PDF for PaperQA2. Switching back to GPT Researcher preserved the private draft locally and the public dispatch received only the new public question. |
| A5 · Consent and resilience | Direct attachment needed no paper record. Inspected the compact approval, declined a transfer and approved a new attempt after a sibling file upload. Separate checks exercised failed and repeated uploads, absent acknowledgement, cross-project PDF refusal, changed PDF bytes, consumed approval replay, review expiry and idempotent results recovery by run ID. |
| A6 · Packaged PaperQA2 | Installed the pinned runtime and ran its offline native transport probe. Walked the packaged profile with a synthetic file and controlled answer through approval, scoped staging, saved answer and BibTeX. Fresh provider output was outside this walkthrough. The reviewed live result below exercised the identical worker and Linux dependency lock. |
| A7 · Provider evidence and guards | Verified all shipped row/lock checksums, unchanged worker bytes, role/model/provider policy and custom-route exclusion. Reviewed the 35-check report and the separate process trace. The embedding serving identity remains unobserved. |
| A8 · Native independence and usage | The installed smoke verified all three pinned standalone entry points after hub-only removal and both native browsers. The GPT Researcher smoke checked same-origin WebSocket access and rejected foreign HTTP and WebSocket origins. The browser walkthrough checked Usage without a key and cleared the tab bearer on Quit. Live LDR, GPT Researcher and Usage with authorised service keys are pending. |
| A9 · Delivery | Boundary assessment in [mandatory-rules.md](mandatory-rules.md). The changed-path ZIP is based on current devel. Obsolete `handover.md` is superseded by this page and [DEVEL.md](../DEVEL.md). Repository publication was outside the task. |

The fresh install initially failed on the two native apps with source-built dependencies. Current devel tracked their locks but omitted the bundled wheel files. The audit restored seven previously shipped wheels from the earlier v3 archive and verified each against its earlier release lock. The LDR and GPT Researcher platform locks now reference those exact bytes. All application and dependency versions and PaperQA2 locks are unchanged. The three Linux app environments used about 4.3 GiB on the audit host. Allow additional downloads and uv cache space. Use a disk-backed temporary directory for the combined installed smoke.

## Reused PaperQA2 live evidence

The authorised Ubuntu x64 WSL2 report from 22 November 2026 passed 35 checks with the installed hub and unchanged worker. It covered approved-subset staging, prior index isolation, decline and replay, native PDF answers and citations, onward request destinations and refusal to fall back for chat and embeddings. Ten chat generations reported Azure. Embedding requests were eligible for Azure alone and the paired invalid-provider check was rejected. The successful embedding responses did not identify the serving provider. The separate process trace showed DNS and HTTPS traffic consistent with the OpenRouter run. Its scope is the recorded process capture.

| Evidence | SHA-256 |
|---|---|
| Installed live report | `2d4f0c9b389f518da43620ee2ad4e120fe4a8a66e52cc1f04dd449d403fdc184` |
| Independent process trace | `de0e5521c47b4e337039f892971a59879c83fa140886583a80b6da1e83727134` |
| Packaged `rah/worker.py` | `3f90ded02a30017649cec86f534680697f0223ed523262c65147c5df52b7e9c1` |
| Ubuntu and macOS PaperQA2 lock | `03fde5bf1872f2f7100b3c62238d477875ae022e80f5137de4653af6789eabe8` |
| Windows PaperQA2 lock | `73c5159aa9fda38cda3d80de378268e46b7a8319ddd5c499c698e7189ad85308` |

The packaged manifest references this shared profile on the three compatible install rows. Changes to the worker, endpoint, provider policy or compatible dependencies require review of the changed route. Ordinary setup and use perform compatibility checks automatically. Account ZDR is optional, and each transfer requires explicit approval.

## Remaining release acceptance

| Target | Status |
|---|---|
| Ubuntu x64 | Pending desktop acceptance. The current Linux package and workspace were exercised on compatible Debian. The prior WSL2 run establishes the reviewed live profile. |
| Apple silicon macOS 14+ | Pending package and desktop acceptance. Check Gatekeeper and the shortcut on target hardware, including the documented minimum. |
| Windows 11 x64 | Pending package and desktop acceptance. Check RemoteSigned/unblocking, the shortcut, data-root ACL inheritance and long-path handling. |

The release implementer checks each desktop entry in a disposable user account, starting with a clean install and then repeating setup with existing data. Exercise native coexistence, port conflicts, restart to apply changes, and standalone use after hub-only removal. Confirm LDR SQLCipher and native export prerequisites on the target, including any system libraries required for native PDF rendering. Review upstream notices and first-run downloads before redistribution. Record the exact package hash and host with the observation.

Using independently authorised test credentials, check the pinned LDR academic and broad-search routes, GPT Researcher Classic general and deep reports, native role overrides, and Usage against the current-key response. Record missing, stale, BYOK and provider-error behaviour. Complete a native LDR literature survey through RIS/manual review, supplied PDFs and a cited native answer. These maintainer checks complete release claims. The existing PaperQA2 provider evidence is reused for the unchanged reviewed path. See [DEVEL.md](../DEVEL.md) for build and diagnostic commands.
