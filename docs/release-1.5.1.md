# Version 1.5.1 · Release follow-up

This release follows the [implementation review](release-review-2026-10-02.md) and addresses the approved sharing-readiness work.

## Included

- Correctness fixes for interpretation precedence, conflicting option premiums, invalid price metadata, DBMF acquisitions/replays/revisions, and interrupted browser requests.
- A shorter illustrated README with ZIP-based installation, separate Windows and macOS/Linux setup, explicit start/stop/update instructions, and detailed linked guides.
- Checksummed backup export scripts for Bash and Windows PowerShell, plus an isolated backup verifier and copyable recovery instructions. An external storage account or unattended backup schedule is not automatically configured.
- Warning-free Starlette tests using `httpx2`, with all unrelated dependency pins preserved.
- GitHub Actions for Linux Docker tests, macOS native tests, and Windows PowerShell setup/Compose configuration/JavaScript/backup-script checks. The Windows job does not run Docker Desktop or WSL 2.
- Fresh dashboard screenshots and seven newly accessible official historical sources documented in the [source follow-up](data-follow-up-release.md).

## Verification and release status

Local Python tests: **316 passed**, with no test-client deprecation warning. JavaScript tests: **13 passed**. Backup tests cover successful exports, integrity failures, repeated copies, missing destinations, unsafe archive members, and legacy databases. The PowerShell script also passes locally under PowerShell 7; hosted Windows checks exercise Windows PowerShell 5.1.

The independent repository is [chrisduvillard/ResearchDesk](https://github.com/chrisduvillard/ResearchDesk). It starts with a fresh source snapshot and contains none of the old repository’s Git objects. The original DanNathan repository stays private because its previously documented cached-object privacy issue is not proven removed.

Hosted CI, final deployment, and publication results will be recorded here after verification.

## Remaining follow-ups

- Test the full Docker Desktop install on personal Windows and macOS computers; hosted checks cover native macOS execution and Windows setup scripts, not that complete experience.
- Have a real nontechnical person follow the README. An independent agent walkthrough found and corrected documentation gaps; it is not human usability testing. Use the [setup feedback form](https://github.com/chrisduvillard/ResearchDesk/issues/new?template=setup-feedback.yml) to record the result.
- Add a separately reviewed SEC HTML importer. The recovered schedules use older layouts, month-only settlements, and, in June 2025, securities-lending collateral matched by a liability. They are not imported or counted as coverage yet.
- Obtain reliable, verified history for unavailable price references. Existing rejected candles remain explicit gaps.
