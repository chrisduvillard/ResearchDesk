# Research Desk · User guide

[← Installation and overview](../README.md) · [Technical reference](reference.md)

## Daily workflow

### 1. Start with “What changed?”

- **Dan Nathan:** see newly disclosed or removed instruments, direction and strategy updates, and audited interpretation corrections. Open a card to inspect the original evidence. “Since previous check” compares accepted observations; repeated or rejected downloads do not manufacture changes.
- **DBMF:** see the five largest net exposure changes, newly present or absent markets, and collateral changes. The comparison shows both actual reporting dates, even when they are months apart. These are changes in percentage points, not inferred transactions.
- **Since my last visit:** compare with the last view recorded by this browser. DBMF retains the exact previously viewed report, including its revision. Newly saved historical reports and revisions are counted separately from changes to the latest portfolio.

The visit baseline stays fixed while a tab is open. Refreshing or returning starts a new visit. A first visit falls back to the previous report comparison. Visit markers and preferences live in browser storage; they are specific to the browser and address used to access the app. Clearing site data resets them.

### 2. Select a market or instrument

Click a position to bring its chart into view. DBMF change cards also open the market detail; Dan Nathan change cards open the original evidence. DBMF’s market detail sits immediately below current positioning, with controls to return to positioning or open exposure history. Both chart range controls stay synchronized.

Selections survive refreshes and dashboard switches. URLs preserve the selected instrument or market, range, comparison date, evidence report and source revision. Dan Nathan URLs also preserve the active page, history/scorecard filters, horizon and exposure shading. Browser **Back** and **Forward** restore those views. Explicit bookmark parameters take precedence over remembered preferences.

For DBMF, a requested comparison date uses the latest accepted report **on or before** that date. The page displays the actual date prominently. It never invents a missing observation.

### 3. Check freshness

Expand **Data health** on either dashboard to inspect both collectors:

| Field | What it means |
| :--- | :--- |
| Source reporting date | The date the publisher assigns to its disclosure or holdings |
| Last successful source check | When the app last obtained an accepted source, including an unchanged report |
| Latest completed prices | The dates of cached daily bars, with details for each market |
| Last successful price refresh | When the price cache last refreshed successfully |
| Collector | Whether the independent worker is running and reporting a heartbeat |

A successful download does not make an old source current. Cached holdings and prices remain visible during failures.

**Optional browser alerts:** expand Data health and choose **Enable browser alerts**. The browser asks for notification permission. Alerts cover repeated failed/incomplete collections, stale data, missing worker heartbeats and unfamiliar DBMF instruments. They notify once per ongoing problem, plus recovery; repeated checks and other open tabs share the same incident state where browser locking is supported.

Alerts work while a dashboard tab is open and able to run, including in the background subject to browser throttling. They do not run after every tab is closed. Nothing is sent to an external messaging service. Unsupported or blocked notifications leave the in-app status panel available. Use **Disable browser alerts** to turn them off.

### 4. Inspect corrections

Expand **Source revisions** on DBMF. Choose a preserved revision to compare:

- reporting date, collection timestamps, matching net assets and source downloads;
- exact before/after contract values, quantities, identifiers, weights and exposure percentages;
- added or removed rows and the resulting market exposure changes;
- whether the source, processing rules, or both changed.

Comparisons require the **same reporting date and source type**. Daily holdings and consolidated historical schedules are not presented as corrections to one another. Acquisition timestamps establish revision order, including recovered imports. Rows use exact identities; ambiguous matches appear as removed and added. A source revision is not a trade signal.

![Dan Nathan disclosure dashboard](images/dan-overview.jpg)

## How the data works

### Sources and schedules

| Data | Source | Collection / coverage |
| :--- | :--- | :--- |
| Dan Nathan disclosures | [CNBC disclosures](https://www.cnbc.com/dan-nathan/) | Daily at **22:15 New York time** |
| Current DBMF holdings | [Official iMGP holdings table](https://www.imgp.com/us/fund/US53700T8273-imgp-dbi-managed-futures-strategy-etf/) | Daily at **10:00 and 22:30 New York time** |
| Historical DBMF holdings | Verified official consolidated reports | 18 reporting dates from December 2021 through June 2026 in the current catalog |
| Daily prices | Yahoo Finance through yfinance | Refreshed independently of successful holdings parsing; completed bars only |

Schedules follow New York daylight saving time. Each slot allows three attempts, with retries after 5 minutes and then another 30 minutes. Startup catches up with the latest due check. Collection does not invent reports for missed days.

Historical coverage is incomplete from DBMF’s May 2019 inception. Confirmed sources include [September 30, 2025](https://www.imgp.com/wp-content/uploads/imgp-us-q32025.pdf) and [March 31, 2026](https://www.imgp.com/wp-content/uploads/2026/06/Litman-Gregory-NPORT-F-3.31.26-19357A-bannerless.pdf). Seven additional SEC schedules were recovered in the [release source follow-up](data-follow-up-release.md); importing their older layouts and collateral accounting remains outstanding. Access results and coverage gaps remain visible. The catalog includes 18 verified historical reports. Three additional futures references cover former markets, and excluded candles include inspectable provider evidence. See the [source and historical coverage reference](reference.md#dbmf-collection-and-historical-coverage) and [data availability follow-up](review-2026-10-02.md#data-availability-follow-up--version-150).

### DBMF arithmetic

```text
Exposure (%) = signed current notional value / matching fund net assets × 100
Gross exposure = long exposure + absolute short exposure, before netting
```

Dollar amounts remain decimal strings; calculations use 50-digit decimal precision. Rounded source weights are validation checks. Expirations aggregate into their underlying market, while Treasury maturities and ultra contracts remain separate. Treasury bills, identified cash and repurchase agreements appear under Collateral.

Exposures can exceed 100% and do not measure risk contribution. Bond direction refers to prices; currency direction refers to the named currency against the dollar. Historical calculations use current **Notional Value**, not original contract values, accounting gains/losses or volatility-adjusted allocations. Consolidated schedules prevent counting subsidiary holdings twice.

| Exposure | Price reference |
| :--- | :--- |
| US 2-year / 10-year / long Treasury bonds | `ZT=F` / `ZN=F` / `ZB=F` futures |
| US large-cap equities | `ES=F` futures |
| Gold / WTI crude oil | `GC=F` / `CL=F` futures |
| Euro versus US dollar | `EURUSD=X` |
| Japanese yen versus US dollar | `JPY=X`, inverted to USD per yen |
| Developed equities outside North America | `EFA` ETF proxy |
| Emerging-market equities | `EEM` ETF proxy |

Futures references can use different expirations and contain roll-related changes. DBMF reference prices are unadjusted; ETF distributions are not reinvested. Negative futures prices are supported. Markets without a verified reference retain their exposure history without a price chart.

Equity completion respects exchange holidays and early closes. Futures wait until 18:00 New York on their trade date; currencies wait until 01:00 UTC after the provider date. Freshness warnings allow scheduled collection/retries before expecting a new equity bar; futures and currency caches warn after four calendar days without a bar. [Full price rules](reference.md#dbmf-price-references).

### Safe handling of source changes

Parsing, instrument naming, aggregation, summaries and strategy calculations use deterministic code. **There are no LLM calls, model credentials or model costs.**

Complete raw responses are archived, including rejected sources. The parser tolerates known heading aliases, reordered columns and routine contract-name/expiry variations. Missing totals, inconsistent arithmetic, ambiguous columns and unfamiliar instruments prevent publication. The last accepted snapshot remains current.

A new market appears under **Holdings needing review**. An operator verifies its identity and value basis, adds a sourced mapping through the CLI, and replays the original archived report. Mappings are audited and loaded without restarting services. A new security type with different exposure arithmetic requires a reviewed adapter. [New-instrument workflow and examples](reference.md#new-instruments-and-source-changes).

### Dan Nathan interpretation and scoring

Initial holdings form a baseline. Later valid observations distinguish additions, removals, direction changes and strategy updates. Unknown or ambiguous structures stay explicit; a month alone never establishes an option expiry day or year.

The scorecard enters an eligible directional episode at the first regular market open strictly after observation and measures 1, 5, 20 and 60 trading sessions. It compares follow, oppose and always-long underlying returns. Baselines, ambiguous exposures, complex structures and retrospective corrections are excluded. It is not actual options P&L or a portfolio simulation. [Methodology](reference.md#scorecard) · [Options analysis](reference.md#strategy-analysis-and-payoff-explorer) · [TradingView setup](reference.md#tradingview).

## API

Interactive documentation: **`/docs`**, served entirely from local assets. JSON data endpoints return `Cache-Control: no-store`.

| Area | Routes |
| :--- | :--- |
| Shared health | `GET /api/desk/health` |
| Disclosure summaries | `GET /api/changes?after_id=…` |
| Disclosures | `/api/status`, `/api/positions`, `/api/instruments`, `/api/events`, `/api/timeline/{symbol}` |
| Charts and analysis | `/api/prices/{symbol}`, `/api/scorecard`, `/api/analysis/{symbol}` |
| Hypothetical calculator | `POST /api/payoff` — calculation only |
| DBMF | `/api/dbmf/status`, `/exposures`, `/history`, `/prices/{market_id}` |
| DBMF summaries | `/api/dbmf/changes?baseline_id=…&since_id=…` |
| DBMF corrections | `/api/dbmf/revisions`, `/revisions/{id}?against_id=…` |
| Evidence | `/api/snapshots/{id}` and `/source`; `/api/dbmf/reports/{id}` and `/source` |
| Review audit | `/api/dbmf/unmapped`, `/api/dbmf/mappings` |
| Downloads | `/api/export/history.csv`, `/api/export/pine/{symbol}`, `/api/dbmf/export/history.csv` |

Routes abbreviated in the DBMF rows share the `/api/dbmf` prefix. Revision listing supports `limit` and `before_id` pagination. Exposure history CSV includes all accepted revisions at full precision. The dashboard shows the canonical accepted observation for each date.

## Troubleshooting

| Symptom | Check |
| :--- | :--- |
| Dashboard cannot connect | Check `docker compose ps`, your configured port, and the SSH tunnel when accessing a remote server. |
| Data directory missing or permission denied | Create `HOST_DATA_DIR` and check that its owner matches `APP_UID`/`APP_GID`. |
| Successful fetch but old holdings date | The issuer may still publish the previous report. Compare source date and collection time separately. |
| Unknown DBMF contract | Open Holdings needing review; follow the sourced mapping workflow before replaying. |
| Price chart unavailable | Inspect per-market freshness. A provider failure preserves the cache; a new market may have no verified reference. |
| Comparison uses an earlier date | No accepted report exists on the requested date. The actual earlier date is displayed. |
| No browser notification | Enable alerts in Data health, allow notifications in browser settings, and keep a tab open. Mobile/browser support varies. |
| Saved view looks different on another device | Preferences and visit markers are browser-local. Use the bookmarked URL to transfer a view. |
| No source revisions listed | No accepted version of that reporting date/source has changed. Repeated downloads are deduplicated. |
