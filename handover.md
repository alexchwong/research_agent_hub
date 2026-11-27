# RAH 0.1.0 release handover

Start with CI on the release candidate. Use the [release audit](docs/releases.md#remaining-release-acceptance) for acceptance details and [DEVEL.md](DEVEL.md) for build, smoke-test and diagnostic commands.

## Remaining work

- [ ] **Confirm CI on the release candidate.** Verify that **Cross-platform setup** and **Pinned upstream setup** both pass on the same commit on Ubuntu 26.04 x64, macOS 15 arm64 and Windows Server 2025 x64. Record the commit and run links. The earlier Windows diagnostic run does not establish the candidate’s status, and the fixture smoke does not replace the real pinned-upstream smoke.
- [ ] **Complete desktop acceptance.** Test the release ZIP on Ubuntu x64, Apple silicon macOS (including the documented macOS 14 minimum) and Windows 11 x64. Use a disposable account to test clean setup and setup with existing data, shortcut launch and shutdown, native app startup and exports, workspace migration and recovery, and standalone use after hub-only removal. Record the package hash and host for each run. Windows Server CI does not count as Windows 11 desktop acceptance.
- [ ] **Complete live upstream and Usage checks.** Use authorised test credentials to verify LDR academic and broad search, GPT Researcher Classic general and deep reports, and native role overrides. Compare Usage with OpenRouter’s current-key response and check missing, stale, BYOK and provider-error states. Use separate search credentials where required.
- [ ] **Run one end-to-end academic survey.** Start with a native LDR run, review RIS or manual intake, supply and approve the selected PDFs, and record a cited native PaperQA2 answer.
- [ ] **Finish redistribution review.** Check bundled wheels and source-built packages, upstream notices, and native first-run downloads. Review both the source-archive and wheel licence records for GPT Researcher before redistribution.

## Evidence already recorded

The [workspace audit](docs/releases.md#010-workspace-audit) records installed-wheel setup, native browser startup, failure isolation and workspace checks on Debian. It does not replace target desktop acceptance.

The [reviewed PaperQA2 evidence](docs/releases.md#reused-paperqa2-live-evidence) applies to the unchanged packaged route. Successful embedding responses did not identify the serving provider. Do not repeat qualification for the unchanged route; review it if the worker, endpoint, provider policy or compatible dependencies change. Each managed transfer still requires explicit approval.
# Purpose pipeline refactor handover (unreleased)

The refactor is **offline complete** against baseline `b3b5df45…` (see [releases.md](docs/releases.md#purpose-pipeline-refactor-unreleased)). It is not live qualified. Package metadata stays at 0.1.0; the release number is the owner's to assign. The 0.1.0 items above remain open and separate.

- [ ] **Run the P6 campaign** on each supported host with authorised credentials, then review `reports/rah-live.json`:

  ```bash
  export OPENROUTER_API_KEY=sk-or-v1-...
  ./.release-test/current/installed-hub/bin/python scripts/live_rah_refactor.py --profile all --output reports/rah-live.json
  ```

- [ ] **Qualify Standard PaperQA2 on every release platform.** Approve each provider tuple at the prompt. Enable a passing route only after owner approval by adding its report `enable_entry` to `rah/pqa_profiles.py`, then rerun the offline suite. Budget and Premium may stay unavailable where they lack evidence.
- [ ] **Run the packaged smoke with the real pins** on Ubuntu, macOS and Windows CI hosts.
- [ ] **Desktop checks**: the guided walkthrough on Windows 11 x64 (including process-tree termination), Apple silicon macOS 14+ and Ubuntu x64; the native LDR literature flow with authenticated RIS/Markdown export, **Reopen LDR handoff** after a restart and native history.
