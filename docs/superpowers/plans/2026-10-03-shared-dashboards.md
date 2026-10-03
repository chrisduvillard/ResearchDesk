# Shared contributor and fund dashboards

User-approved specification: reuse Dan Nathan's full interface for every contributor and DBMF's full interface for every fund. Equity funds adapt measurements to weights and stock prices. Keep no-data sources hidden. Preserve legacy routes, source evidence, data, and response contracts.

1. Add scoped contributor dashboard projections: status, grouped positions, history, timeline, strategy analysis, cached scorecards, source evidence, CSV and TradingView exports.
2. Add scoped fund projections matching the existing DBMF dashboard: accepted report comparisons, market/holding aggregation, history, exact revisions, evidence and exports. Use original DBMF projections for DBMF to preserve its richer archived history.
3. Generalize the existing frontend templates, navigation and saved state. Replace hardcoded identities and endpoints; adapt equity labels and measurements, retain all charts and controls.
4. Supply verified chart OHLC data through background collection, separately from immutable analytics batches; include fund assets. Preserve missing and stale data explicitly.
5. Test isolation, legacy compatibility, UI parity, empty/partial data, history/revisions, exports, prices, browser navigation and all visible entities. Review, deploy privately and verify Tailscale.

No schema migration planned: existing reports, event details, assets, price batches and settings support the projections. Use source-scoped opaque report IDs. Canonical checkout is explicitly user required. No new worktree.

## Verification

Implemented the shared pages and preserved the original routes and exports.
The final regression run passed 376 Python tests, 16 JavaScript tests and seven
optional real-browser journeys. The Docker image passed the same Python suite
with networking disabled. Live browser verification covered all three visible
contributors and nine visible funds, including charts, evidence downloads,
scorecards and narrow mobile layouts. Unverified prices and missing historical
reporting dates remain explicit; no observations were fabricated.
