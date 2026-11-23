# Security and data handling

The CLI binds the hub to 127.0.0.1. A private instance record lets the launcher challenge the service and pass a hub-session access token to the browser. API mutation requests require an exact Host, Origin and bearer token. The HTML shell has no private data. The tab stores the bearer in sessionStorage when available and immediately removes the launcher fragment. Reload restores the tab within the running hub. Navigation URLs contain only project and chat IDs. Authentication failure and Quit clear the bearer. After a restart, open the launcher and restore saved chats from the sidebar. A hostile process already running as the same OS user or an administrator is outside the application boundary.

Protect native launch directories and local data as well as the hub configuration. Per-app settings are projected through an allowlist. Keep RAH's environment file outside all native source roots. User-owned native app settings remain separate. Upload contents are local and downloaded as attachments.

Private PDF approval binds the originating project and request, ordered file IDs/names/hashes, question, explicit excerpt, effective key, compatible install and reviewed provider/lock identity. Declining or revisiting a chat starts no worker. Native entry points remain available for user-controlled use. See [release evidence](releases.md) for the reviewed profile and platform status.

GPT Researcher’s Classic UI runs on its own loopback origin. The deployment wrapper validates Host, requires its native Origin for WebSocket and HTTP mutations, rejects foreign browser subresources, and disables forwarded proxy headers. Its readiness route checks a per-launch token. Standalone use is local and retains the upstream browser and native credential choices.

The PaperQA2 worker receives only the scoped OpenRouter credential for the verified profile. The reviewed profile uses one eligible provider per role and disables provider fallbacks. The report observed Azure for chat and established constrained routing for embeddings. The serving provider was unobservable in embedding responses.
