# Architecture

The hub serves a loopback dashboard and keeps the three upstreams in independent final-path Python environments. The installed wheel contains the per-host route manifest, hashed locks and the small set of required built wheels and pinned source assets.

- `rah/installer.py` builds each installation, checks the entry point and promotes its pointer after the launch files are ready. `rah/setup.py` shares the core install and repair paths between CLI and hub.
- `rah/settings.py` keeps separate chat and embedding defaults and projects scoped values to native roles. A bad app override is reported on that app. Generated profiles are checked for manual edits.
- `rah/upstreams.py` and `rah/gptr_browser.py` start pinned native services and leave the upstream stacks in charge of research and output. Per-app launch scripts survive hub removal.
- `rah/projects.py` and `rah/runs.py` retain original files and store a chronological chat history. Request IDs reserve work before upload. Managed runs save their artifact and update the initiating chat in the same transaction. Single-use approvals bind the exact question, quote, file hashes and recipient profile.
- `rah/pqa_profile_worker.py` invokes native PaperQA2 analysis with the approved subset in a fresh per-run home and index, enforces the approved request tuples and records observable coverage. `rah/worker.py` now only runs the legacy managed GPT Researcher report.
- `rah/openrouter.py` loads separate role catalogues and current-key usage from fixed endpoints.

## Guided research pipelines

- `rah/pipelines.py` is the deterministic controller. The recipe table (version 1) is fixed in code; every request starts `survey → scope_gate`, then the purpose's fixed downstream positions. A later recipe version gets a new table entry and existing rows keep their stored version.
- One scheduler thread in the foreground hub wakes on commits, job completion and every second. It dispatches the earliest eligible attempt per resource (`gptr`, `scope_gate`, `pqa`, `oa`; LDR steps wait for the user) in `queued_at`, step-UUID order. Reservation is a compare-and-set inside `Jobs.start(reserve=…)`; no job slot is held while waiting for approval, native work or an app. Before any public dispatch the controller recomputes the route fingerprint and checks the 24-hour, same-process public approval; otherwise the step waits at `reconfirm_recipients`.
- `rah/gptr_pipeline.py` runs the pinned GPT Researcher CLI as an argument list in its own process group with a private working directory, empty `DOC_PATH`, private home directories, a shared private tokenizer cache, `PYTHONNOUSERSITE=1` and `PYTHON_DOTENV_DISABLED=1`. Survey deadline 3 minutes, Rapid and comparison 10 minutes, Deep 20 minutes. Cancellation or timeout terminates the group (POSIX `killpg`, Windows `taskkill /T /F`) and waits for exit. Operation and completion markers live under `hub/runs/guided/<step>/`.
- `rah/scope_gate.py` builds the single questionnaire request, validates the exact JSON shape, renders the fixed brief template and the deterministic fallback.
- `rah/research_profiles.py` holds the immutable six-model catalogue (version 1). `rah/pqa_profiles.py` holds the managed PaperQA2 route registry, eligibility checks and P6 identities; `rah/pqa_profile_worker.py` is the single worker for qualification and normal use.
- `rah/oa.py` implements identifier canonicalisation and the fixed PMC route with public-address DNS checks, direct dialing that keeps SNI and certificate validation on the original host, no redirects, bounded reads and a separate parser check.

## Workspace schema 3

SQLite remains the canonical registry (STRICT tables, parameterised SQL). On runs and artifacts `project_id` means the scope ID: a named project or a standalone chat.

| Table | Purpose |
| --- | --- |
| `pipeline_run` | One accepted guided request per brief revision. Exact question, purpose, quality, recipe version, immutable brief JSON, state, outcome and a compare-and-set `revision`. One nonterminal run per chat. |
| `pipeline_step` | One row per step attempt with its own chat entry. Retries add attempts; earlier attempts and outputs stay. Scope attempts store the round and questionnaire source. `detail_json` keeps source counts, observed Scope Gate usage, coverage and recovery data. |
| `pipeline_approval` | Durable public start, scope, re-confirmation, native launch and confirmation, open-access lookup, partial finish, rerun and PaperQA2 decision records with payload digests and credential-free route display. |
| `artifact` | Adds `step_id`, `role`, `media_type`, `output_key`, `title` and `origin_class`, plus `citations` and `questionnaire` kinds. Run outputs are unique per `(scope, run, output_key)`; legacy run results use `primary`. |
| `artifact_parent` | Actual dependencies only (survey → questionnaire, selected PDFs → answer), constrained to one scope. |
| `pipeline_paper`, `acquisition` | Run-owned Papers Needed copy with origin class and access status, and one provenance row per lookup attempt. The legacy `paper` table gains `pmcid` and `access_status`. |
| `pipeline_action` | Browser-generated request and action IDs with payload digests for idempotent replay. |
| `guided_default` | The guided composer default (Literature review, Standard), written identically on fresh installs and upgrades. |

Guided outputs are staged as private temporary files beside their destination, verified, and published under the workspace writer lock in one write transaction with their rows, parents, entry results and the step transition. Artifact IDs derive from `(step, output_key)`; an exact replay returns the original ID and any difference is a 409. An uncommitted publication marker lets restart remove only files this attempt placed and never registered. On restart, legacy completion recovery runs first; then running guided steps become `interrupted` (with **Reuse output** only for bytes bound to that attempt), waiting LDR steps pause for **Reopen LDR handoff**, and private PaperQA2 previews are discarded.

See [integrations.md](integrations.md) for native interfaces and [releases.md](releases.md) for release evidence.
