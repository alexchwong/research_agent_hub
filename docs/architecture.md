# Architecture

The hub serves a loopback dashboard and keeps the three upstreams in independent final-path Python environments. The installed wheel contains the per-host route manifest, hashed locks and the small set of required built wheels and pinned source assets.

- `rah/installer.py` builds each installation, checks the entry point and promotes its pointer after the launch files are ready. `rah/setup.py` shares the core install and repair paths between CLI and hub.
- `rah/settings.py` keeps separate chat and embedding defaults and projects scoped values to native roles. A bad app override is reported on that app. Generated profiles are checked for manual edits.
- `rah/upstreams.py` and `rah/gptr_browser.py` start pinned native services and leave the upstream stacks in charge of research and output. Per-app launch scripts survive hub removal.
- `rah/projects.py` and `rah/runs.py` retain original files and store a chronological chat history. Request IDs reserve work before upload. Managed runs save their artifact and update the initiating chat in the same transaction. Single-use approvals bind the exact question, quote, file hashes and recipient profile.
- `rah/worker.py` invokes native analysis with the approved subset in a fresh per-run home and index. It preserves the native result and bibliography.
- `rah/openrouter.py` loads separate role catalogues and current-key usage from fixed endpoints.

See [integrations.md](integrations.md) for native interfaces and [releases.md](releases.md) for release evidence.
