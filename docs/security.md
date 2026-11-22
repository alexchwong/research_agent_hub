# Security and data handling

The CLI binds the hub to 127.0.0.1. A private instance record lets the launcher challenge the service and pass a short-lived-in-memory access token to the browser. API mutation requests require an exact Host, Origin and bearer token. The HTML shell has no private data. A hostile process already running as the same OS user or an administrator is outside the application boundary.

Protect native launch directories and local data as well as the hub configuration. Per-app settings are projected through an allowlist. Keep RAH's environment file outside all native source roots. User-owned native app settings remain separate. Upload contents are local and downloaded as attachments.

Private PDF approval binds the selected project rows, file bytes, question and the verified provider/lock identity. Native entry points remain available for user-controlled use. See the qualification checks in docs/releases.md before approving any cloud handoff.

GPT Researcher’s Classic UI runs on its own loopback origin. The deployment wrapper validates Host, requires its native Origin for WebSocket and HTTP mutations, rejects foreign browser subresources, and disables forwarded proxy headers. Its readiness route checks a per-launch token. Standalone use is local and retains the upstream browser and native credential choices.

The PaperQA2 worker receives only the scoped OpenRouter credential for the verified profile. The candidate uses one provider per role and disables provider fallbacks at the request level. The current evidence exercises offline transport; private transfer requires the live acceptance evidence in docs/releases.md.
