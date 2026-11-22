# Architecture

The hub serves a loopback dashboard and keeps the three upstreams in independent final-path Python environments. The installed wheel contains the per-host route manifest, hashed locks and the small set of required built wheels and pinned source assets.

- `rah/installer.py` builds each installation, checks the entry point and promotes its pointer after the launch files are ready. The dashboard groups these existing operations into one core install plan.
- `rah/settings.py` keeps separate chat and embedding defaults and projects scoped values to native roles. A bad app override is reported on that app. Generated profiles are checked for manual edits.
- `rah/upstreams.py` and `rah/gptr_browser.py` start pinned native services and leave the upstream stacks in charge of research and output. Per-app launch scripts survive hub removal.
- `rah/projects.py` and `rah/runs.py` retain original files and prepare a single-use approval bound to content hashes, question, configuration, verified recipients and the installed lock.
- `rah/worker.py` invokes native analysis with the approved subset in a fresh per-run home and index. It preserves the native result and bibliography.
- `rah/openrouter.py` loads separate role catalogues and current-key usage from fixed endpoints.

See [integrations.md](integrations.md) for native interfaces and [releases.md](releases.md) for release evidence.
