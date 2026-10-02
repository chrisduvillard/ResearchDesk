# Historical source follow-up

Checked on **2 October 2026**. This follow-up supersedes the earlier review's statement that all older direct SEC downloads remain blocked. Seven full official schedules were retrieved successfully with HTTP 200 using a descriptive research User-Agent. Some requests using the default Python User-Agent still returned HTTP 403, so access remains request-dependent.

**These are verified source discoveries, not imported holdings.** No production records, historical catalog entries or price mappings changed in this investigation. The accepted history still begins on 31 December 2021 and still lacks June 2025.

## Full official schedules recovered

| Reporting date | Official filing | Consolidated net assets | Futures rows | Treasury-bill rows |
| :--- | :--- | ---: | ---: | ---: |
| 30 June 2019 | [N-CSRS, pages 4–5](https://www.sec.gov/Archives/edgar/data/1359057/000089853119000476/imdbimfsetf-ncsrs.htm) | $15,390,509 | 15 | 25 |
| 30 September 2019 | [N-Q, Item 1](https://www.sec.gov/Archives/edgar/data/1359057/000089418919008001/mdp-imdbi_nq.htm) | $19,364,781 | 15 | 29 |
| 31 December 2019 | [N-CSR, pages 10–11](https://www.sec.gov/Archives/edgar/data/1359057/000089853120000163/imdbietf-ncsra.htm) | $18,369,017 | 13 | 18 |
| 30 June 2020 | [N-CSRS, pages 4–5](https://www.sec.gov/Archives/edgar/data/1359057/000089853120000413/imdbietf-ncsrs.htm) | $19,573,553 | 14 | 19 |
| 31 December 2020 | [N-CSR, pages 9–10](https://www.sec.gov/Archives/edgar/data/1359057/000089853121000180/imdbietf-ncsra.htm) | $36,453,757 | 14 | 24 |
| 30 June 2021 | [N-CSRS, pages 4–5](https://www.sec.gov/Archives/edgar/data/1359057/000089853121000427/imdbietf-ncsrs.htm) | $53,879,926 | 14 | 24 |
| 30 June 2025 | [N-CSRS, pages 49–50](https://www.sec.gov/Archives/edgar/data/1020425/000119312525196797/d80914dncsrs.htm) | $1,172,488,395 | 10 | 12 |

Accession numbers and filenames were located through the SEC's [current trust submissions](https://data.sec.gov/submissions/CIK0001020425.json), [predecessor trust submissions](https://data.sec.gov/submissions/CIK0001359057.json) and [older predecessor submissions](https://data.sec.gov/submissions/CIK0001359057-submissions-001.json). The predecessor trust contains other funds; its unrelated reports must not be assigned to DBMF. The June 2025 filing directory contains no PDF attachment.

For all six 2019–2021 schedules, decimal checks confirmed that each futures row's signed current value minus original amount equals its reported unrealized gain/loss. Row gains/losses sum to the published total, each bill's fair value sums to total investments, and investments plus other assets less liabilities equal net assets. These checks establish source consistency; they do not independently verify exchange quotations or the issuer's contract counts.

The June 2020 gold row illustrates why bulk N-PORT original amounts cannot be imported as exposure: the full schedule reports original amount **$3,148,703**, current value **$3,240,900** and unrealized appreciation **$92,197**. The previously inspected bulk original amount was $3,148,703.2602. The full schedule resolves the column meaning but does not supply an implemented adapter.

## What still prevents safe import

The existing historical parser expects an issuer PDF layout. The older SEC schedules use the former fund name, different headings, wrapped HTML table rows and unlabeled futures subtotals. They provide **settlement month**, such as `Sep-20`, rather than an exact expiration date. A dedicated adapter must preserve that precision and leave the exact date unknown. Early reports also contain instrument spelling variants requiring explicit mapping review; separate Treasury-bill rows and opposing contracts must remain separate evidence.

June 2025 adds **$199,004,490** invested in the State Street Navigator Securities Lending Government Money Market Portfolio. The balance sheet records an equal securities-lending collateral payable. The schedule also has **$934,415,327** in Treasury bills and **$17,552,000** in repurchase agreements. Their combined **$1,150,971,817**, plus **$21,516,578** other assets less liabilities, matches net assets. The lending investment needs its own reviewed representation and explanation of the matching liability; assigning it to ordinary cash or omitting it would misrepresent the source. Futures have distinct original and current notional columns and total unrealized appreciation of **$10,625,775**.

A future implementation should retain the original HTML, validate fund/date and every table row, reconcile subtotals and net assets, reject unfamiliar investments, test wrong-fund and truncated-table cases, and receive independent arithmetic review before import. This release does not introduce that accounting adapter.

## SOFR and Eurodollar prices

The [Yahoo SR3 daily chart endpoint](https://query1.finance.yahoo.com/v8/finance/chart/SR3%3DF?range=max&interval=1d) still returned a single current timestamp, with no completed daily history. The [GE endpoint](https://query1.finance.yahoo.com/v8/finance/chart/GE%3DF?range=max&interval=1d) returned HTTP 404, “No data found.” Neither response supplies usable historical candles.

[CME identifies SR3](https://www.cmegroup.com/markets/interest-rates/stirs/three-month-sofr.html) as three-month SOFR futures quoted as 100 minus the contract reference-quarter rate. The [New York Fed's overnight SOFR observations](https://www.newyorkfed.org/markets/reference-rates/sofr) are a different series and cannot replace futures OHLC data. CME's [Eurodollar conversion announcement](https://www.cmegroup.com/media-room/press-releases/2023/4/24/cme_group_completeskeymilestonesinconversionofeurodollarfutureso.html) confirms the April 2023 conversion and subsequent expiry of remaining May/June contracts; Eurodollar history must retain its identity.

[CME DataMine](https://www.cmegroup.com/datamine.html) is an official route to historical futures data, but requires an account, an order and data licensing. No licensed dataset was obtained or integrated. This investigation found no additional verified feed ready for the application's existing historical-price adapter; the explicit unavailable-history labels remain appropriate.
