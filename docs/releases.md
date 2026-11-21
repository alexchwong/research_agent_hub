# Release verification

The fixture setup workflow exercises the installed wheel, selected platform shortcut, a failed core-app installation and a scoped retry. The pinned upstream workflow exercises the standard packaged manifests, all three real installs, native browser readiness, GPT Researcher’s origin protections, PaperQA2’s synthetic request serialization and native LDR RIS intake.

Ubuntu 26.04 x64, Apple silicon macOS 15 and Windows Server 2025 x64 are the GitHub Actions targets. Complete the desktop and live checks in [handover.md](../handover.md) before publishing 0.1.0.

Package from a clean source checkout or export. Exclude `.venv/`, `build/`, `dist/`, `.release-test/`, `reports/`, caches and populated `.env` files from source deliverables. CI retains the fixture and packaged smoke JSON reports separately from the bootstrap bundle. A credential-free report does not establish live-provider or desktop qualification.

## v0.1.0 evidence checklist

For each advertised OS version and architecture, record the wheel hash, app pins, manifest and lock hashes, Python/uv versions, and the report or checklist reference. Mark each check **passed**, **failed**, or **not run**. Do not carry results forward to a changed pin or artifact route without rerunning the affected checks.

| Evidence | Run without user input? | What counts as a pass |
|---|---|---|
| Offline pytest and fixture installed-wheel smoke | Yes | Current wrapper contract, failure isolation, scoped retry and retained fixture launchers. |
| Packaged upstream smoke | Yes; needs dependency downloads | All three pinned apps install and the automated native probes pass. Record CI runners separately from desktop hosts. |
| Windows 11 x64, Apple silicon macOS and Ubuntu x64 desktop acceptance | Manual only for desktop observations not covered by automation | Launch from the shortcut, verify app coexistence, and actually use retained browser and CLI entry points after hub-only removal. Neither Windows Server nor a different macOS version qualifies the advertised target. |
| OpenRouter usage and native app mappings | Explicit live opt-in and authorized credentials; no interactive test prompts | Compare Usage with the current-key response. Confirm role overrides and native control in actual pinned upstream runs; the direct API smoke is not enough. |
| PaperQA2 qualification and academic survey | Synthetic checks automated where possible; explicit approval for the real handoff | Verify onward destinations and excluded PDFs, including an existing index; keep the shipped gate closed until evidence is reviewed. Complete the cited LDR-to-PaperQA2 survey through the installed UI. |

Capture only sanitized evidence: no API keys, raw provider payloads, private paths, or PDF contents. Escalate a blocked host or route before narrowing release scope. See [handover.md](../handover.md) for the detailed live and desktop steps.
