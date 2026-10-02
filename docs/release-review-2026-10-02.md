# Sharing-readiness review · 1.5.1

Reviewed on **2 October 2026**. This is a code, packaging, usability, and operational follow-up to the [earlier data review](review-2026-10-02.md). This report records the initial review before publication. See the [1.5.1 release follow-up](release-1.5.1.md) for the subsequent approved changes, publication, and deployment status.

## Findings addressed

| Area | Finding | Change |
| :--- | :--- | :--- |
| Interpretation reviews | Details and a later manual review within the same second could leave the earlier interpretation active. | New review timestamps retain microseconds. Ambiguous legacy ties favor the manual review and suppress an uncertain payoff. |
| Option premiums | Conflicting debit/credit wording could produce a score-eligible interpretation and an impossible inferred payoff. | Validate payment qualifiers consistently; reject contradictions and recognize stated premiums for supported iron structures. Interpretation version is now 2.0.2. |
| Price cache | Infinite volume could pass validation and make the JSON price endpoint fail. | Reject negative or nonfinite volume, dividends, and splits before replacing cached data. |
| DBMF recovery | Replaying an older rejected report could supersede a later repeated valid observation, including after an upgrade. | Preserve acquisition evidence independently of immutable report revisions; recover verifiable legacy duplicate checks with explicit timing bounds. |
| DBMF revision ordering | A replay could reverse the order of revisions acquired within the same second. | Compare original acquisition order independently of replay IDs and later duplicate checks. |
| History and scores | A failed filter request could leave another instrument’s results visible; overlapping pagination could mix instruments. | Clear obsolete filtered results, guard pagination during loading, and ignore superseded errors. |
| DBMF bookmarks | An unknown market in a URL could break evidence rendering. | Resolve an available market before rendering dependent panels. |
| Refresh recovery | Failed detail/price loads could remain unretried while the server’s report stayed unchanged. | Retry incomplete loads on subsequent polls on both dashboards. |
| Accessibility | Dialogs lacked accessible names and navigation selection was only visual. | Name dialogs and expose the active navigation state. |
| Installation | The README mixed first-time setup with extensive operational and financial methodology. | Short illustrated README, ZIP download, separate PowerShell and Unix commands, start/stop help, and linked user/reference guides. |
| Maintenance | No automated repository checks. | Add GitHub Actions for browser tests, Compose validation, Docker build, offline Python regressions, and fresh-service smoke checks. |

## Validation

- **295 Python tests passed** locally and in the Python 3.12 Docker image with network access disabled. **13 JavaScript tests passed** with Node’s test runner. New behavioral regressions were observed failing before their corresponding fixes.
- Built the application image, installed an isolated three-service Compose stack using a new writable data directory, and exercised real CNBC/DBMF collection and price downloads. All three services became healthy; both sources were accepted with no reported price errors.
- Checked the existing production stack read-only: all three services were healthy and shared data health reported no issues at review time.
- Browser checks covered both dashboards, an invalid DBMF market bookmark, history, scorecard, the payoff calculator, and local Swagger documentation. The calculator’s example returned a $300 maximum loss, $700 maximum gain, and $103 breakeven. The API reference rendered 33 operations.
- Checked a desktop browser and 390 × 844 mobile viewport. Both dashboards rendered without horizontal page overflow or application JavaScript errors. Dialog names resolve to existing headings.
- Seventeen page/API/vendor routes returned HTTP 200; JSON API responses included `Cache-Control: no-store`.
- Created a backup of the isolated installation, restored it into a separate directory, and verified SQLite integrity, foreign keys, and the hashes of every referenced CNBC/DBMF source archive. Also started the final image against an isolated restored database with the legacy schema: all three services became healthy and the migrated database passed integrity checks.
- `npm audit` and `pip-audit` reported no known vulnerabilities in the pinned dependencies. `uv pip check` found no installed dependency conflicts. Gitleaks found no secrets in the ten reachable Git commits; reachable author/committer addresses use GitHub’s noreply address. A separate Gitleaks scan of the clean source snapshot also found no secrets; that snapshot builds without Git metadata, local dependencies, or runtime data.
- Checked local documentation links, release-version consistency, Compose configuration, workflow syntax with actionlint, and whitespace errors. GitHub Actions dependencies are pinned to verified release commit hashes.

## Publication and practical limits

**Keep the existing GitHub repository private until its documented history issue is resolved, or publish from a fresh repository.** The earlier audit records a GitHub-cached unreachable commit containing a personal author/committer email. This review confirms the repository is still private and its reachable history uses the noreply address; it does not establish removal of that cached object. Changing visibility was outside this review.

The Docker installation was executed on Linux. Windows PowerShell instructions and macOS instructions were reviewed against their setup conventions and Docker’s installation documentation, but were not run on those operating systems. The GitHub workflow’s constituent build/test commands were exercised locally; a hosted Actions run requires pushing the workflow.

Legacy DBMF duplicate acquisitions can only be recovered where a completed run and a valid source archive survive. Their collection-run start/end times are retained as bounds, not claimed as exact fetch times; overlapping intervals cannot prove a later observation. Missing or corrupt evidence is not invented. New acquisitions retain explicit ordering.

At the initial review, one Starlette warning remained (resolved in the release follow-up): its test client deprecates the current `httpx` integration in favor of `httpx2`. It does not fail the suite. Dependency audits identify known advisories, not every possible vulnerability.

Data still depends on publishers and Yahoo Finance. Historical holdings coverage is incomplete, rejected price candles remain gaps, and observations do not establish real trades or portfolio returns. This review does not repeat the earlier audit’s full historical production-data reconciliation, certify every possible behavior, or establish redistribution rights for third-party material.
