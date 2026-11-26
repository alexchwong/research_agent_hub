# Windows CI handover — November 26, 2026

## Where work stopped

The fixes are on the remote branch `ci/windows-acl-diagnostic` at `99a49bb`. They have **not** been merged into `devel`; `origin/devel` was last confirmed at `d599e4e`. The local `devel` working tree already contains the same fixes as uncommitted changes, including the untracked `.gitattributes`. Do not reset that working tree or reapply the branch over it without reconciling the edits. The detached worktree used to make the CI commits is `/tmp/rah-windows-acl-ci.qyEIay`. No GitHub jobs were canceled.

## Changes and evidence

- **ACL verification:** The diagnostic runner measured a 15.02-second `Get-Acl` timeout; the .NET ACL read, current-user SID lookup, owner lookup, and access-rule enumeration each took under 0.2 seconds. The verifier now reads the owner and access rules via .NET and recognizes `Owner Rights` only after verifying that the directory owner is trusted. The verifier still fails closed. The Windows regression test passed: a private directory was accepted, and direct and inherited grants to `Everyone` were rejected.
- **Packaged lock checksums:** Git now preserves LF endings for packaged locks. A checkout test with `core.autocrlf=true` confirmed that all three Windows lock hashes were preserved. Lock contents and expected hashes were not changed. The fixture smoke test intentionally corrupts the PaperQA2 lock; that one checksum error is expected if the retry and isolation checks pass.
- **Windows shortcut:** Shortcut creation now uses direct COM activation from PowerShell, stops on errors, and checks that the `.lnk` file was saved. The Windows fixture release smoke test passed, including shortcut creation and retrying PaperQA2 without replacing the other installs.
- **Local validation:** 53 tests passed, one Windows-only test was skipped, and three live tests were deselected. JavaScript syntax checks and `git diff --check` passed.

## Last verified GitHub Actions status for `99a49bb`

| Workflow | Run ID | Ubuntu | macOS | Windows |
| --- | --- | --- | --- | --- |
| Cross-platform setup | `45764010732` | Passed | Passed | Passed: ACL regression, offline pytest, and fixture ZIP smoke |
| Pinned upstream setup | `45764010651` | Passed | Passed | Still running the installed ZIP smoke test for all three pinned apps and both native browser checks (job `139720441086`) |

## Resume here

1. From `/home/alex-wong/vscode_projects/research_agent_hub`, check the pending run with `gh run view 45764010651`. If the Windows job failed, read the failing step with `gh run view 45764010651 --job 139720441086 --log-failed` and inspect the `rah-upstreams-report-windows-2025` artifact if available. The fixture workflow passing does not prove that the packaged upstreams or native browser checks passed on Windows.
2. If the packaged Windows run failed, identify the first failing operation before changing anything. Keep ACL verification fail-closed and keep the packaged lock checksums enforced. Re-run the affected workflow on the revised commit.
3. Once both workflows pass on the same commit, integrate the diagnostic branch into `devel` through the normal review process and reconcile the existing local edits. Only then clean up the temporary worktree and branch if no longer needed.