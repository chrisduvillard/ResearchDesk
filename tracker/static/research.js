"use strict";
const content = document.querySelector("#content");
let auth = { authenticated: false },
  generation = 0,
  controller,
  poll;
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const safe = (v) => {
  try {
    const u = new URL(v, location.origin);
    return ["http:", "https:"].includes(u.protocol) &&
      !u.username &&
      !u.password
      ? u.href
      : "#";
  } catch {
    return "#";
  }
};
const link = (url, text) => `<a href="${esc(safe(url))}">${esc(text)}</a>`;
const fmt = (v, d = 2) =>
  v == null
    ? "Unavailable"
    : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const pct = (v) => (v == null ? "Unavailable" : `${fmt(v * 100)}%`);
const stamp = (v) => (v ? esc(v.replace("T", " ")) : "Unknown");
const assetLink = (id, label) =>
  link("/research?view=asset&id=" + encodeURIComponent(id), label);
const empty = (text) => `<p class="empty">${esc(text)}</p>`;
const badge = (text, warn = false) =>
  `<span class="badge${warn ? " warning" : ""}">${esc(text)}</span>`;
const options = (rows, current) =>
  rows
    .map(
      ([v, label]) =>
        `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(label)}</option>`,
    )
    .join("");
const table = (heads, rows) =>
  `<div class="table-wrap"><table><thead><tr>${heads.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
const tr = (cells) => `<tr>${cells.map((c) => `<td>${c}</td>`).join("")}</tr>`;
const title = (eyebrow, heading, description) =>
  `<div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(heading)}</h1><p class="intro">${esc(description)}</p>`;
const toast = (text) => {
  const n = document.querySelector("#toast");
  n.textContent = text;
  n.style.display = "block";
  setTimeout(() => (n.style.display = "none"), 6000);
};
async function api(path, method = "GET", body, signal) {
  const response = await fetch("/api/v2" + path, {
    method,
    signal,
    headers: {
      "Content-Type": "application/json",
      ...(auth.csrf_token ? { "X-CSRF-Token": auth.csrf_token } : {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok)
    throw Error(
      typeof data.detail === "string"
        ? data.detail
        : JSON.stringify(data.detail),
    );
  if (Array.isArray(data))
    data.nextCursor = response.headers.get("X-Next-Cursor");
  return data;
}
function navigate(url) {
  history.pushState({}, "", url);
  render();
}
function eventList(items) {
  return items.length
    ? items
        .map(
          (i) =>
            `<article class="activity"><strong>${link(i.asset_id ? "/research?view=asset&id=" + encodeURIComponent(i.asset_id) : i.evidence_url, i.title)}</strong> ${badge(i.kind, i.kind.includes("failure") || i.kind === "correction")}<small>Source ${esc(i.source_id)} · Public ${stamp(i.public_at)} · Usable ${stamp(i.available_at)}</small>${link(i.evidence_url, "Evidence ↗")}</article>`,
        )
        .join("")
    : empty("No activity in this window.");
}
function chart(points, label) {
  const valid = points.filter(
    (p) => p.value != null && Number.isFinite(p.value),
  );
  if (!valid.length) return empty("No complete values to chart.");
  const low = Math.min(...valid.map((p) => p.value)),
    high = Math.max(...valid.map((p) => p.value)),
    span = high - low || 1;
  const path = points
    .map((p, i) =>
      p.value == null
        ? ""
        : `${i === 0 || points[i - 1].value == null ? "M" : "L"}${55 + (i * 700) / Math.max(1, points.length - 1)},${190 - ((p.value - low) / span) * 155}`,
    )
    .join(" ");
  return `<figure><svg class="chart" viewBox="0 0 790 235" role="img" aria-label="${esc(label)}"><title>${esc(label)}</title><path d="M55 20V200H755" fill="none" stroke="#c8d7cd"/><path d="${path}" fill="none" stroke="#176452" stroke-width="2.5"/><text x="0" y="30">${fmt(high, 0)}</text><text x="0" y="195">${fmt(low, 0)}</text><text x="55" y="225">${esc(points[0].date)}</text><text x="670" y="225">${esc(points.at(-1).date)}</text></svg><figcaption class="muted">${esc(label)}</figcaption></figure>`;
}
async function today(signal) {
  const [brief, sources] = await Promise.all([
    api("/briefings", "GET", undefined, signal),
    api("/sources/status", "GET", undefined, signal),
  ]);
  return (
    title(
      "Daily research",
      "Today",
      "What changed, what disagrees, and what needs a closer look. Every observation leads back to its evidence.",
    ) +
    `<div class="grid research-shortcuts"><a class="card" href="/dbmf"><h2>DBMF holdings & exposure →</h2><p>Current positioning, report comparisons, charts, and exposure history.</p></a><a class="card" href="/dan"><h2>Dan Nathan’s positions →</h2><p>Disclosed positions, recent changes, price charts, and scorecard.</p></a></div><div class="grid"><div class="card">Briefing cutoff<strong class="metric">07:00 Zurich</strong><small>${brief.briefing ? stamp(brief.briefing.cutoff) : "Awaiting the analytics worker"}</small></div><div class="card">Available sources<strong class="metric">${sources.filter((s) => ["available", "qualified"].includes(s.coverage)).length} / ${sources.length}</strong><small>Observation sources are still being qualified.</small></div><div class="card">Collection needs attention<strong class="metric">${sources.filter((s) => s.enabled && (s.stale || s.last_run?.status === "error")).length}</strong>${link("/research?view=sources", "Inspect source health →")}</div></div><div class="two"><section class="panel"><h2>Morning briefing</h2>${eventList(brief.items)}</section><section class="panel"><h2>Since the cutoff</h2>${eventList(brief.live)}</section></div>`
  );
}
async function contributors(params, signal) {
  const profiles = await api("/contributors", "GET", undefined, signal),
    id = params.get("id");
  if (!id)
    return (
      title(
        "People & evidence",
        "Contributors",
        "Disclosure observations and reviewed on-air calls remain separate research streams.",
      ) +
      `<div class="grid">${profiles.filter((p) => p.has_data).map((p) => `<article class="card"><h2>${link(p.id === "dan-nathan" ? "/dan" : "/research?view=contributors&id=" + p.id, p.name)}</h2>${badge(p.coverage, p.coverage === "unavailable")}<p class="muted">${esc(p.reason || "Automatic disclosure observation")}</p>${link(p.source_url, "Official source ↗")} · ${link("/research?view=calls&contributor=" + p.id, "Reviewed calls")}</article>`).join("")}</div>`
    );
  const profile = profiles.find((p) => p.id === id);
  if (!profile) throw Error("Contributor not found");
  const [positions, events] = await Promise.all([
    api("/contributors/" + id + "/positions", "GET", undefined, signal),
    api(
      "/contributors/" +
        id +
        "/events" +
        (params.get("cursor")
          ? "?cursor=" + encodeURIComponent(params.get("cursor"))
          : ""),
      "GET",
      undefined,
      signal,
    ),
  ]);
  return (
    title(
      "Contributor research",
      profile.name,
      profile.reason ||
        "Latest accepted disclosure. Observed changes are not verified trades.",
    ) +
    `<div class="actions">${badge(profile.coverage)}${link(profile.source_url, "Official source ↗")}${link("/research?view=calls&contributor=" + id, "Calls")}${auth.authenticated ? link("/research?view=record&contributor=" + id, "Record a call") : ""}</div><section class="panel"><h2>Disclosure positions</h2>${
      positions.length
        ? table(
            ["Instrument", "Direction", "Strategy", "Evidence"],
            positions.map((p) =>
              tr([
                assetLink("legacy:" + p.symbol, p.symbol),
                badge(p.direction),
                esc(p.strategy),
                esc(p.raw_text) +
                  (auth.authenticated
                    ? `<p><button class="secondary" data-review="${esc(p.symbol)}" data-contributor="${esc(id)}">Review direction</button></p>`
                    : ""),
              ]),
            ),
          )
        : empty(
            "No accepted positions. Check source coverage before interpreting an empty view.",
          )
    }</section><section class="panel"><h2>Disclosure history</h2>${
      events.length
        ? table(
            ["Instrument", "Change", "Public date", "Available", "Source"],
            events.map((e) =>
              tr([
                assetLink(e.asset_id, e.symbol),
                badge(e.kind),
                stamp(e.public_at),
                stamp(e.available_at),
                link(e.source_url, "Source ↗"),
              ]),
            ),
          )
        : empty("No disclosure events yet.")
    }${events.nextCursor ? link("/research?view=contributors&id=" + id + "&cursor=" + encodeURIComponent(events.nextCursor), "Older events →") : ""}</section>`
  );
}
async function calls(params, signal) {
  const contributor = params.get("contributor");
  const items = await api(
    "/calls" +
      "?" +
      new URLSearchParams({
        ...(contributor ? { contributor_id: contributor } : {}),
        ...(params.get("cursor") ? { cursor: params.get("cursor") } : {}),
      }),
    "GET",
    undefined,
    signal,
  );
  return (
    title(
      "Owner-reviewed evidence",
      "On-air calls",
      "Approval determines when a call becomes usable. A historical spoken date never backdates a live signal.",
    ) +
    `<div class="actions">${auth.authenticated ? link("/research?view=record" + (contributor ? "&contributor=" + contributor : ""), "Record a call →") : "Log in as owner to record, edit, approve, or retract a call."}</div>` +
    items
      .map(
        (c) =>
          `<article class="card"><h2>${assetLink(c.document.asset_id, c.document.asset_id)} · ${esc(c.document.action)}</h2>${badge(c.status, c.status !== "approved")} <small>${esc(c.contributor_id)} · Revision ${c.revision}</small><p>${esc(c.document.excerpt)}</p><p class="muted">Spoken ${stamp(c.document.spoken_at)} (${esc(c.document.timezone)})<br>Approved ${stamp(c.approved_at)} · Horizon ${esc(c.document.horizon)}${c.document.conditions ? " · Conditional: " + esc(c.document.conditions) : ""}</p>${link(c.document.source_url, "Original source ↗")} · ${link("/api/v2/calls/" + c.id + "/revisions", "Revision history")}<div class="actions">${auth.authenticated ? `${link("/research?view=record&call=" + c.id, "Edit")}${c.status === "draft" ? `<button data-call="${c.id}" data-revision="${c.revision}" data-action="approve">Approve this revision</button>` : ""}${c.status !== "retracted" ? `<button class="secondary" data-call="${c.id}" data-revision="${c.revision}" data-action="retract">Retract</button>` : ""}` : ""}</div></article>`,
      )
      .join("") +
    (items.length ? "" : empty("No recorded calls yet.")) +
    (items.nextCursor
      ? link(
          "/research?view=calls&" +
            new URLSearchParams({
              ...(contributor ? { contributor } : {}),
              cursor: items.nextCursor,
            }),
          "Older calls →",
        )
      : "")
  );
}
async function record(params, signal) {
  if (!auth.authenticated)
    return title(
      "Owner access",
      "Record a call",
      "Log in using the button above to record a sourced call.",
    );
  const [profiles, assets, existing] = await Promise.all([
    api("/contributors", "GET", undefined, signal),
    api("/assets?limit=100", "GET", undefined, signal),
    params.get("call")
      ? api(
          "/calls/" + encodeURIComponent(params.get("call")),
          "GET",
          undefined,
          signal,
        )
      : null,
  ]);
  const d = existing?.document || {};
  if (d.asset_id && !assets.some((a) => a.id === d.asset_id))
    assets.push(
      await api(
        "/assets/" + encodeURIComponent(d.asset_id),
        "GET",
        undefined,
        signal,
      ),
    );
  return (
    title(
      "Owner workspace",
      existing ? "Revise a call" : "Record a call",
      "Save a draft, check its source, then approve it. Conditional or ambiguous calls stay visible but are excluded from automated scoring.",
    ) +
    `<section class="panel"><form id="call-form" data-id="${existing?.id || ""}" data-revision="${existing?.revision || ""}"><div class="form-grid"><label>Contributor<select name="contributor_id">${options(
      profiles.map((p) => [p.id, p.name]),
      d.contributor_id || params.get("contributor") || "dan-nathan",
    )}</select></label><label>Find instrument<input id="call-asset-search" type="search" placeholder="Search symbol or name"></label><label>Instrument<select name="asset_id" required>${options(
      assets.map((a) => [a.id, a.symbol + " — " + a.name]),
      d.asset_id,
    )}</select></label><label>Direction / action<select name="action">${options(
      [
        ["long", "Long / buy"],
        ["short", "Short / bearish"],
        ["close", "Close"],
        ["ambiguous", "Ambiguous"],
      ],
      d.action || "long",
    )}</select></label><label>Instrument type<select name="instrument_type">${options(
      [
        ["underlying", "Shares / underlying"],
        ["option", "Option (research only)"],
      ],
      d.instrument_type || "underlying",
    )}</select></label><label>Spoken timestamp, including UTC offset<input name="spoken_at" required placeholder="2026-10-02T16:30:00-04:00" value="${esc(d.spoken_at || new Date().toISOString())}"></label><label>Timezone<input name="timezone" required value="${esc(d.timezone || "UTC")}" placeholder="America/New_York"></label><label>Source link<input name="source_url" type="url" required value="${esc(d.source_url)}"></label><label>Stated horizon<input name="horizon" required maxlength="300" value="${esc(d.horizon)}" placeholder="e.g. next several weeks"></label></div><label>Short supporting excerpt<textarea name="excerpt" required maxlength="2000">${esc(d.excerpt)}</textarea></label><div class="form-grid"><label>Optional price target<input name="target" type="number" step="any" min="0.01" value="${esc(d.target)}"></label><label>Conditions (leave blank for an unconditional call)<input name="conditions" maxlength="1000" value="${esc(d.conditions)}"></label></div><button>Save draft</button><p id="form-error" role="alert" class="error"></p></form></section>`
  );
}
async function funds(params, signal) {
  const f = params.get("fund");
  const funds = f ? [] : (await api("/funds", "GET", undefined, signal)).filter(fund => fund.latest != null);
  const comparison = funds.length ? await api(
    "/fund-comparisons?" + new URLSearchParams({
      funds: funds.map(fund => fund.id).join(","),
      measure: params.get("measure") || "notional_pct_nav",
      mode: params.get("mode") || "latest",
    }), "GET", undefined, signal,
  ) : { columns: [], rows: [] };
  let detail = "";
  if (f) {
    const result = await api(
      "/funds/" + encodeURIComponent(f) + "/exposures",
      "GET",
      undefined,
      signal,
    );
    detail = `<section class="panel"><h2>${esc(f)} holdings</h2><p>Source date ${stamp(result.report?.source_date)} · Acquired ${stamp(result.report?.acquired_at)} · ${badge(result.coverage)}</p>${result.equity_summary ? `<p>${result.equity_summary.holdings} holdings · Top five ${fmt(result.equity_summary.top_five_weight_pct)}% of NAV. ${esc(result.equity_summary.sector_coverage)}</p>` : ""}${result.report ? link("/api/v2/fund-reports/" + result.report.id + "/source", "Original report ↗") : ""}<details><summary>Changes since the previous report</summary>${table(
      [
        "Holding",
        "Measure",
        "Previous",
        "Current",
        "Change (pp)",
        "Interpretation",
      ],
      (result.changes || []).map((c) =>
        tr([
          c.asset_id ? assetLink(c.asset_id, c.name) : esc(c.name),
          esc(c.measure),
          fmt(c.old),
          fmt(c.new),
          fmt(c.change_pp),
          esc(c.kind) + (c.contract_roll ? " · contract roll" : ""),
        ]),
      ),
    )}</details>${
      result.holdings.length
        ? table(
            [
              "Instrument",
              "Measure",
              "Value",
              "Quantity",
              "Expiry",
              "Evidence",
            ],
            result.holdings.map((h) =>
              tr([
                h.asset_id ? assetLink(h.asset_id, h.name) : esc(h.name),
                esc(h.measure),
                fmt(h.value) + " " + esc(h.unit),
                fmt(h.quantity),
                esc(h.expiry),
                `<details><summary>Source row</summary><pre>${esc(h.evidence)}</pre></details>`,
              ]),
            ),
          )
        : empty("Automatic holdings are unavailable for this source.")
    }</section>`;
  }
  if (f) return title("Fund research", f + " positioning", "Holdings, changes, and source evidence.") +
    `<div class="actions">${link("/research?view=funds", "Compare funds →")}</div>` + detail;
  if (!funds.length) return title("ETF research", "Fund positioning", "No fund holdings have been collected yet.");
  return (
    title(
      "ETF research",
      "Fund positioning",
      "Compare equivalent measures. Holdings changes are observations, not inferred buys and sells. Blank cells are missing data.",
    ) +
    `<form class="filters" id="fund-filters"><label>Measure<select name="measure">${options(
      [
        ["notional_pct_nav", "Signed notional / NAV"],
        ["equity_weight_pct", "Equity portfolio weight"],
        ["collateral_pct_nav", "Collateral / NAV"],
        ["issuer_risk_weight_pct", "Issuer risk weight"],
        ["volatility_contribution_pct", "Volatility contribution"],
        ["issuer_portfolio_weight_pct", "Issuer reported weight"],
      ],
      params.get("measure") || "notional_pct_nav",
    )}</select></label><label>Date alignment<select name="mode">${options(
      [
        ["latest", "Latest available"],
        ["aligned", "Aligned reporting date"],
      ],
      params.get("mode") || "latest",
    )}</select></label><button>Compare</button></form><section class="panel"><div class="table-wrap"><table><thead><tr><th>Fund</th><th>Source date</th>${comparison.columns.map((c) => `<th>${assetLink(c, c.replace("market:", ""))}</th>`).join("")}</tr></thead><tbody>${comparison.rows
      .map(
        (r) =>
          `<tr><th>${link(r.fund_id === "DBMF" ? "/dbmf" : "/research?view=funds&fund=" + r.fund_id, r.fund_id)}</th><td>${stamp(r.source_date)} ${r.stale ? badge("stale / missing", true) : ""}</td>${comparison.columns
            .map((k) => {
              const c = r.cells[k];
              return `<td class="number ${c.value == null ? "heat-missing" : c.value >= 0 ? "heat-pos" : "heat-neg"}">${c.value == null ? "—" : fmt(c.value) + "%"}<small><br>${c.change_pp == null ? "No prior comparison" : (c.change_pp > 0 ? "+" : "") + fmt(c.change_pp) + " pp"}</small></td>`;
            })
            .join("")}</tr>`,
      )
      .join(
        "",
      )}</tbody></table></div></section>${detail}<div class="grid">${funds.map((f) => `<article class="card"><h3>${link(f.id === "DBMF" ? "/dbmf" : "/research?view=funds&fund=" + f.id, f.id)}</h3>${badge(f.coverage, !f.qualified)}<p>${esc(f.reason || "Qualified automatic collection")}</p><small>Latest ${stamp(f.latest?.source_date)}</small></article>`).join("")}</div>`
  );
}
async function asset(params, signal) {
  const a = await api(
    "/assets/" + encodeURIComponent(params.get("id")),
    "GET",
    undefined,
    signal,
  );
  const timeline = await api(
    "/assets/" +
      encodeURIComponent(a.id) +
      "/timeline" +
      (params.get("cursor")
        ? "?cursor=" + encodeURIComponent(params.get("cursor"))
        : ""),
    "GET",
    undefined,
    signal,
  );
  return (
    title(
      a.kind,
      a.symbol + " · " + a.name,
      "Publication and spoken dates remain separate from acquisition and approval. Agreement counts exclude stale or ambiguous observations.",
    ) +
    `<div class="grid">${Object.entries(a.agreement)
      .map(
        ([kind, v]) =>
          `<div class="card"><h3>${esc(kind)} agreement</h3><strong class="metric">${v.bullish} bullish · ${v.bearish} bearish</strong><small>${v.denominator} usable contributors / ${v.tracked_contributors} tracked</small></div>`,
      )
      .join(
        "",
      )}<div class="card"><h3>Identity</h3>${badge(a.verified ? "Verified source identity" : "Unverified", !a.verified)}<p>${esc(a.provenance)}</p><small>${esc(a.id)}</small></div></div><section class="panel"><h2>Price history</h2>${chart(
      a.prices.map((p) => ({ date: p.date, value: p.close })),
      "Adjusted daily close",
    )}<p class="muted">Last acquisition ${stamp(a.prices.at(-1)?.fetched_at)}</p></section><section class="panel"><h2>Contributor evidence</h2>${
      a.events.length
        ? table(
            [
              "Contributor",
              "Type / change",
              "Direction",
              "Public",
              "Usable",
              "Evidence",
            ],
            a.events.map((e) =>
              tr([
                link(
                  "/research?view=contributors&id=" + e.contributor_id,
                  e.contributor_id,
                ),
                esc(e.evidence_type) + " · " + esc(e.kind),
                esc(e.direction),
                stamp(e.public_at),
                stamp(e.available_at),
                link(e.source_url, "Source ↗") +
                  "<p>" +
                  esc(e.excerpt) +
                  "</p>",
              ]),
            ),
          )
        : empty("No contributor evidence for this identity.")
    }</section><section class="panel"><h2>Tracked fund holdings</h2>${
      a.holdings.length
        ? table(
            ["Fund", "Source date", "Measure", "Value"],
            a.holdings.map((h) =>
              tr([
                link("/research?view=funds&fund=" + h.fund_id, h.fund_id),
                stamp(h.source_date),
                esc(h.measure),
                fmt(h.value) + " " + esc(h.unit),
              ]),
            ),
          )
        : empty("No fund holdings mapped to this identity.")
    }</section><section class="panel"><h2>Dated activity</h2>${eventList(timeline.items)}${timeline.has_more ? link("/research?view=asset&id=" + encodeURIComponent(a.id) + "&cursor=" + encodeURIComponent(timeline.cursor), "Older activity →") : ""}</section>${auth.authenticated ? `<section class="panel"><h2>Owner identity and benchmark configuration</h2><p>Map distinct provider IDs only after checking that they represent the same security or listing. Saved analysis inputs remain unchanged.</p><form id="asset-config" data-asset="${esc(a.id)}"><label>Canonical verified asset ID<input name="canonical_id" placeholder="legacy:MSFT"></label><label>Evidence URL for identity mapping<input name="source_url" type="url"></label><label>Benchmark asset ID (blank removes mapping)<input name="benchmark_id" value="${esc(a.benchmark_id || "")}"></label><label>Reason<textarea name="reason" required></textarea></label><div class="actions"><button value="mapping">Save identity mapping</button><button value="benchmark">Save benchmark</button></div><p id="form-error" role="alert"></p></form></section>` : ""}`
  );
}
function runResults(run) {
  if (!run.result)
    return `<section class="panel"><h2>Run ${esc(run.status)}</h2><p>${esc(run.error || "The analytics worker will process this immutable input set.")}</p></section>`;
  if (run.kind === "scorecard")
    return `<section class="panel"><h2>Directional scorecard</h2><p>${esc(run.result.methodology)}</p>${table(
      [
        "Asset class",
        "Horizon",
        "Sample",
        "Follow gross",
        "Follow net",
        "Fade net",
        "Always-long net",
        "Median net",
        "Win rate",
        "Benchmark / excess",
        "Missing / overlaps",
        "95% interval",
      ],
      run.result.summary.map((s) =>
        tr([
          esc(s.asset_class),
          s.horizon + " sessions",
          fmt(s.n),
          pct(s.follow.mean),
          pct(s.follow_net.mean),
          pct(s.fade_net.mean),
          pct(s.always_long_net.mean),
          pct(s.follow_net.median),
          pct(s.follow_net.win_rate),
          pct(s.benchmark?.mean) +
            " / " +
            pct(s.excess?.mean) +
            " (missing " +
            s.missing_benchmarks +
            ")",
          `${s.missing} / ${s.overlaps}`,
          s.interval
            ? pct(s.interval.lower) + " to " + pct(s.interval.upper)
            : "Suppressed: insufficient independent history",
        ]),
      ),
    )}<details><summary>Inspect signals and exclusions</summary><pre>${esc(JSON.stringify(run.result.signals, null, 2))}</pre></details></section>`;
  return `<div class="split">${["follow", "fade"]
    .map((mode) => {
      const r = run.result[mode];
      return `<section class="panel"><h2>Hypothetical ${mode}</h2>${badge(r.status, r.status !== "complete")}<p>Buy-and-hold ${esc(r.benchmark?.asset_id || "unmapped")}: ${pct(r.benchmark?.total_return)} · ${esc(r.benchmark?.status)}. ${esc(r.benchmark?.reason || r.benchmark?.assumptions || "")}</p><p>Coverage: ${r.coverage?.eligible_signals ?? 0} eligible signals · ${r.coverage?.missing_entries ?? 0} missing entry prices · ${r.coverage?.open_positions ?? 0} open positions.</p><div class="grid"><p>Return<strong class="metric">${pct(r.total_return)}</strong></p><p>Max drawdown<strong class="metric">${pct(r.max_drawdown)}</strong></p></div>${chart(
        r.equity_curve.map((p) => ({ date: p.date, value: p.equity })),
        "Portfolio equity · USD",
      )}<p>Costs $${fmt(r.costs)} · Borrow $${fmt(r.borrowing)} · Dividends $${fmt(r.dividends)} · Turnover ${fmt(r.turnover)}×</p><details><summary>Trades (${r.trades.length})</summary>${table(
        ["Date", "Asset", "Action", "Shares", "Price"],
        r.trades.map((t) =>
          tr([
            t.date,
            assetLink(t.asset_id, t.asset_id),
            esc(t.action),
            fmt(t.quantity, 4),
            fmt(t.price),
          ]),
        ),
      )}</details><details><summary>Skipped signals (${r.skipped.length})</summary><pre>${esc(JSON.stringify(r.skipped, null, 2))}</pre></details><details><summary>Valuations and open positions</summary><pre>${esc(JSON.stringify({ curve: r.equity_curve, positions: r.positions }, null, 2))}</pre></details></section>`;
    })
    .join("")}</div>`;
}
async function analytics(params, signal) {
  const profiles = await api("/contributors", "GET", undefined, signal);
  const runId = params.get("run");
  let result = "";
  if (runId) {
    const run = await api(
      "/simulation-runs/" + encodeURIComponent(runId),
      "GET",
      undefined,
      signal,
    );
    result =
      `<p>${badge(run.inputs.forward_start ? "Prospective from " + run.inputs.forward_start : "Exploratory historical")}</p>` +
      runResults(run) +
      `<details><summary>Saved assumptions, evidence and price inputs</summary><pre>${esc(JSON.stringify(run.inputs, null, 2))}</pre></details>`;
    if (["queued", "running"].includes(run.status))
      poll = setTimeout(render, 2500);
  }
  const [runs, definitions] = await Promise.all([
    api("/scorecards", "GET", undefined, signal),
    api("/simulations", "GET", undefined, signal),
  ]);
  return (
    title(
      "Signal research",
      "Follow / fade",
      "Underlying directional scores and hypothetical portfolios. Costs and borrowing are editable assumptions. ETF holdings are context and never portfolio signals.",
    ) +
    `<section class="panel"><form id="model-form"><div class="form-grid"><label>Contributor<select name="contributor_id">${options(
      profiles.map((p) => [p.id, p.name]),
      "dan-nathan",
    )}</select></label><label>Evidence stream<select name="evidence_type"><option value="disclosure">Disclosures</option><option value="call">Reviewed calls</option></select></label><label>Exit rule<select name="exit"><option value="fixed">Fixed holding period</option><option value="signal">Opposing signal / close (maximum 60 sessions)</option></select></label><label>Holding period<select name="horizon">${options(
      [
        ["1", "1 session"],
        ["5", "5 sessions"],
        ["20", "20 sessions"],
        ["60", "60 sessions"],
      ],
      "20",
    )}</select></label><label>Trading cost per side (basis points)<input name="cost_bps" type="number" min="0" max="1000" step="any" value="5"></label><label>Annual short borrow (%)<input name="borrow_percent" type="number" min="0" max="500" step="any" value="5"></label><label>Previous definition ID (optional new version)<input name="previous_id" type="number" min="1"></label><label>Run type<select name="mode"><option value="historical">Exploratory historical</option><option value="prospective">Prospective, starting now</option></select></label></div><p class="muted">Portfolio: $100,000, 10% target per entry, at most 10 positions, 100% maximum gross exposure. No pyramiding; short proceeds remain restricted.</p><div class="actions"><button name="kind" value="simulation"${auth.authenticated ? "" : " disabled"}>Run paired portfolios</button><button name="kind" value="scorecard" class="secondary"${auth.authenticated ? "" : " disabled"}>Run scorecard</button></div>${auth.authenticated ? "" : "<small>Owner login is required to save a model.</small>"}<p id="form-error" class="error" role="alert"></p></form></section>${result}<section class="panel"><h2>Saved portfolio definitions</h2>${table(
      ["Version", "Cohort", "Mode / start", "Result"],
      definitions.map((d) =>
        tr([
          esc(d.id) + (d.previous_id ? " after " + esc(d.previous_id) : ""),
          esc(d.contributor_id) + " · " + esc(d.evidence_type),
          esc(d.mode) + " · " + stamp(d.forward_start),
          d.latest_run
            ? link("/research?view=analytics&run=" + d.latest_run, "Latest run")
            : "Pending",
        ]),
      ),
    )}</section><section class="panel"><h2>Saved scorecards</h2>${
      runs.length
        ? table(
            ["Created", "Status", "Run"],
            runs.map((r) =>
              tr([
                stamp(r.created_at),
                esc(r.status),
                link("/research?view=analytics&run=" + r.id, "Open result"),
              ]),
            ),
          )
        : empty(
            "The worker will create scorecards as usable evidence and prices become available.",
          )
    }</section>`
  );
}
async function sources(params, signal) {
  const [sources, rules] = await Promise.all([
    api("/sources/status", "GET", undefined, signal),
    api("/alert-rules", "GET", undefined, signal),
  ]);
  const rule = rules[0]?.config || {};
  return (
    title(
      "Coverage & delivery",
      "Sources and alerts",
      "A failed source retains its last accepted state. Fund automation remains in observation mode until five scheduled successes cover two reporting dates.",
    ) +
    `<section class="panel"><h2>Browser notifications</h2><p>Notifications work while a dashboard tab is open. Delivery and permission belong to this browser. Enabling starts with current activity.</p><button id="notifications">${localStorage.getItem("rd-notifications") === "1" ? "Disable" : "Enable"} browser notifications</button><p id="notification-status" role="status"></p></section><section class="panel">${table(
      ["Source", "Coverage", "Last success", "Latest check", "Configuration"],
      sources.map((s) =>
        tr([
          link(s.url, s.id),
          badge(
            s.coverage,
            s.coverage !== "qualified" && s.coverage !== "available",
          ) +
            "<p>" +
            esc(s.reason) +
            "</p>",
          stamp(s.last_success) + (s.stale ? " " + badge("stale", true) : ""),
          esc(s.last_run?.status || "Not collected") +
            "<p>" +
            esc(s.last_run?.error) +
            "</p>",
          auth.authenticated && s.adapter
            ? `<button data-source="${esc(s.id)}" data-enabled="${s.enabled ? "0" : "1"}" class="secondary">${s.enabled ? "Pause" : "Enable"}</button>`
            : s.enabled
              ? "Enabled"
              : "Unavailable",
        ]),
      ),
    )}</section><section class="panel"><h2>Alert rules</h2><form id="alert-form"><div class="form-grid"><label>Source (blank means all)<input name="source_id" value="" placeholder="fund:CTA"></label><label>Watched asset ID (blank means all)<input name="asset_id" value="" placeholder="legacy:MSFT"></label><label>Futures change, percentage points<input name="futures_pp" type="number" min="0" step="any" value="${rule.futures_pp ?? 5}"></label><label>Equity weight change, percentage points<input name="equity_pp" type="number" min="0" step="any" value="${rule.equity_pp ?? 1}"></label><label>Minimum magnitude for both sides of a flip (%)<input name="flip_min" type="number" min="0" step="any" value="${rule.flip_min ?? 1}"></label><label>Rule ID (1 is the global default)<input name="id" type="number" min="1" value="1"></label></div><label><input name="enabled" type="checkbox" checked> Enable rule</label><label><input name="contributor_changes" type="checkbox" checked> Contributor changes and calls</label><label><input name="source_health" type="checkbox" checked> Source failures, overdue checks and recovery</label><div class="actions"><button${auth.authenticated ? "" : " disabled"}>Save rule</button></div><p id="form-error" role="alert"></p></form><details><summary>Current rules</summary><pre>${esc(JSON.stringify(rules, null, 2))}</pre></details></section>`
  );
}
async function render() {
  clearTimeout(poll);
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal,
    version = ++generation;
  const params = new URLSearchParams(location.search),
    view = params.get("view") || "today";
  if ((view === "funds" && params.get("fund")) || (view === "contributors" && params.get("id"))) {
    const fund=view==='funds', id=params.get(fund?'fund':'id');
    const target=fund?(id==='DBMF'?'/dbmf':'/funds/'+encodeURIComponent(id)):(id==='dan-nathan'?'/dan':'/contributors/'+encodeURIComponent(id));
    const filters=new URLSearchParams(params);filters.delete('view');filters.delete(fund?'fund':'id');
    location.replace(target+(filters.size?'?'+filters:'')+location.hash);
    return;
  }
  window.DeskNavigation?.sync();
  content.setAttribute("aria-busy", "true");
  try {
    let html;
    if (view === "search") {
      const assets = await api(
        "/assets?q=" + encodeURIComponent(params.get("q") || ""),
        "GET",
        undefined,
        signal,
      );
      html =
        title(
          "Global search",
          "Securities & markets",
          "Distinct listings and contracts keep their own identities.",
        ) +
        table(
          ["Instrument", "Name", "Identity"],
          assets.map((a) =>
            tr([
              assetLink(a.id, a.symbol),
              esc(a.name),
              badge(a.verified ? "Verified" : "Unverified", !a.verified),
            ]),
          ),
        );
    } else {
      const handlers = {
        today,
        contributors,
        calls,
        record,
        funds,
        asset,
        analytics,
        sources,
      };
      html = await (view === "today"
        ? today(signal)
        : (handlers[view] || contributors)(params, signal));
    }
    if (version !== generation) return;
    content.innerHTML = html;
    window.DeskNavigation?.sync();
    bind();
  } catch (error) {
    if (error.name !== "AbortError" && version === generation)
      content.innerHTML =
        title("Research Desk", "Could not load this view", error.message) +
        `<button id="retry">Try again</button>`;
  } finally {
    if (version === generation) {
      content.removeAttribute("aria-busy");
      document.querySelector("#retry")?.addEventListener("click", render);
    }
  }
}
function formHandler(id, fn) {
  const form = document.querySelector(id);
  form?.addEventListener("submit", async (e) => {
    e.preventDefault();
    const button = e.submitter;
    button.disabled = true;
    try {
      await fn(Object.fromEntries(new FormData(form)), form, e);
    } catch (error) {
      document.querySelector("#form-error").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  });
}
function bind() {
  let assetSearchVersion = 0;
  document
    .querySelector("#call-asset-search")
    ?.addEventListener("input", async (e) => {
      const version = ++assetSearchVersion;
      try {
        const rows = await api(
          "/assets?q=" + encodeURIComponent(e.target.value) + "&limit=100",
        );
        if (version === assetSearchVersion)
          document.querySelector("select[name=asset_id]").innerHTML = options(
            rows.map((a) => [a.id, a.symbol + " — " + a.name]),
          );
      } catch (error) {
        toast(error.message);
      }
    });
  document.querySelectorAll("[data-review]").forEach(
    (b) =>
      (b.onclick = async () => {
        const direction = prompt(
          "Reviewed direction: bullish, bearish, non_directional, mixed, or unknown",
        );
        if (!direction) return;
        const reason = prompt(
          "Evidence and reason for this interpretation correction:",
        );
        if (!reason) return;
        try {
          await api(
            "/contributors/" + b.dataset.contributor + "/reviews",
            "POST",
            { symbol: b.dataset.review, direction, reason },
          );
          render();
        } catch (error) {
          toast(error.message);
        }
      }),
  );
  formHandler("#asset-config", async (d, f, e) => {
    const kind = e.submitter.value;
    await api(
      "/assets/" + encodeURIComponent(f.dataset.asset) + "/" + kind,
      "PUT",
      kind === "mapping"
        ? {
            canonical_id: d.canonical_id,
            source_url: d.source_url,
            reason: d.reason,
          }
        : { benchmark_id: d.benchmark_id || null, reason: d.reason },
    );
    toast("Configuration saved");
    render();
  });
  formHandler("#call-form", async (d, f) => {
    d.target = d.target ? Number(d.target) : null;
    if (f.dataset.id) d.revision = Number(f.dataset.revision);
    await api(
      "/calls" + (f.dataset.id ? "/" + f.dataset.id : ""),
      f.dataset.id ? "PUT" : "POST",
      d,
    );
    navigate("/research?view=calls&contributor=" + d.contributor_id);
  });
  document.querySelectorAll("[data-call]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          const reason =
            b.dataset.action === "retract"
              ? prompt("Reason for retracting this call:")
              : "";
          if (reason === null) return;
          await api(`/calls/${b.dataset.call}/${b.dataset.action}`, "POST", {
            revision: Number(b.dataset.revision),
            reason,
          });
          window.DeskNavigation?.refresh();
          render();
        } catch (e) {
          toast(e.message);
        }
      }),
  );
  document.querySelector("#fund-filters")?.addEventListener("submit", (e) => {
    e.preventDefault();
    navigate(
      "/research?view=funds&" + new URLSearchParams(new FormData(e.target)),
    );
  });
  formHandler("#model-form", async (d, f, e) => {
    const payload = {
      contributor_id: d.contributor_id,
      evidence_type: d.evidence_type,
      mode: d.mode,
      previous_id: d.previous_id ? Number(d.previous_id) : null,
      config: {
        exit: d.exit,
        horizon: Number(d.horizon),
        cost_bps: Number(d.cost_bps),
        borrow_rate: Number(d.borrow_percent) / 100,
      },
    };
    const job = await api(
      e.submitter.value === "simulation" ? "/simulations" : "/scorecards",
      "POST",
      payload,
    );
    navigate("/research?view=analytics&run=" + job.job_id);
  });
  document.querySelectorAll("[data-source]").forEach(
    (b) =>
      (b.onclick = async () => {
        try {
          await api("/sources/" + encodeURIComponent(b.dataset.source), "PUT", {
            enabled: b.dataset.enabled === "1",
          });
          window.DeskNavigation?.refresh();
          render();
        } catch (e) {
          toast(e.message);
        }
      }),
  );
  formHandler("#alert-form", async (d) => {
    await api("/alert-rules/" + d.id, "PUT", {
      source_id: d.source_id || null,
      asset_id: d.asset_id || null,
      enabled: d.enabled === "on",
      contributor_changes: d.contributor_changes === "on",
      source_health: d.source_health === "on",
      futures_pp: Number(d.futures_pp),
      equity_pp: Number(d.equity_pp),
      flip_min: Number(d.flip_min),
    });
    toast("Alert rule saved");
    render();
  });
  document
    .querySelector("#notifications")
    ?.addEventListener("click", toggleNotifications);
}
// IndexedDB transactions expose committed cursor updates across browser processes.
// A localStorage cache can lag across tabs even while Web Locks serialize work.
async function notificationCursor(value) {
  const database = await new Promise((resolve, reject) => {
    const request = indexedDB.open("research-desk-delivery", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("state");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  try {
    return await new Promise((resolve, reject) => {
      const tx = database.transaction(
        "state",
        value === undefined ? "readonly" : "readwrite",
      );
      const store = tx.objectStore("state");
      const request =
        value === undefined ? store.get("cursor") : store.put(value, "cursor");
      tx.oncomplete = () => resolve(request.result);
      tx.onerror = () => reject(tx.error);
      tx.onabort = () =>
        reject(tx.error || Error("Cursor transaction interrupted"));
    });
  } finally {
    database.close();
  }
}
async function toggleNotifications() {
  try {
    if (localStorage.getItem("rd-notifications") === "1") {
      localStorage.setItem("rd-notifications", "0");
      render();
      return;
    }
    if (!("Notification" in window) || !navigator.locks)
      throw Error(
        "This browser needs HTTPS and cross-tab locks for deduplicated notifications.",
      );
    if ((await Notification.requestPermission()) !== "granted")
      throw Error("Browser notification permission was not granted.");
    await navigator.locks.request("research-desk-notifications", async () => {
      const page = await api("/activity?current=true");
      await notificationCursor(page.cursor);
      localStorage.setItem("rd-activity-cursor", page.cursor);
      localStorage.setItem("rd-notifications", "1");
    });
    render();
  } catch (e) {
    toast(e.message);
  }
}
async function notify() {
  if (
    localStorage.getItem("rd-notifications") !== "1" ||
    !navigator.locks ||
    Notification.permission !== "granted"
  )
    return;
  try {
    await navigator.locks.request(
      "research-desk-notifications",
      { ifAvailable: true },
      async (lock) => {
        if (!lock) return;
        const cursor = await notificationCursor();
        const page = await api(
          "/activity?" +
            (cursor ? "cursor=" + encodeURIComponent(cursor) : "current=true"),
        );
        await notificationCursor(page.cursor);
        localStorage.setItem("rd-activity-cursor", page.cursor);
        if (!cursor || page.cursor_reset) return;
        for (const item of page.alerts) {
          const n = new Notification(item.title, {
            body: item.source_id + " · " + item.available_at,
            tag: "research-" + item.id,
          });
          n.onclick = () => {
            window.focus();
            navigate(
              item.asset_id
                ? "/research?view=asset&id=" + encodeURIComponent(item.asset_id)
                : "/",
            );
            n.close();
          };
        }
      },
    );
  } catch {
    /* Retry at next poll; briefing remains readable. */
  }
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a");
  if (!a || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0)
    return;
  const u = new URL(a.href);
  if (
    u.origin === location.origin &&
    ["/", "/research"].includes(u.pathname) &&
    !u.hash
  ) {
    e.preventDefault();
    navigate(u.pathname + u.search);
  }
});
document.querySelector("#search").onsubmit = (e) => {
  e.preventDefault();
  navigate(
    "/research?view=search&q=" +
      encodeURIComponent(document.querySelector("#query").value),
  );
};
document.querySelector("#close-login").onclick = () =>
  document.querySelector("#owner-dialog").close();
document.querySelector("#owner-button").onclick = async () => {
  if (auth.authenticated) {
    try {
      await api("/auth/logout", "POST");
      auth = { authenticated: false };
      document.querySelector("#owner-button").textContent = "Owner login";
      render();
    } catch (e) {
      toast(e.message);
    }
  } else document.querySelector("#owner-dialog").showModal();
};
document.querySelector("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    auth = await api("/auth/login", "POST", {
      password: document.querySelector("#password").value,
    });
    document.querySelector("#password").value = "";
    document.querySelector("#owner-dialog").close();
    document.querySelector("#owner-button").textContent = "Log out";
    render();
  } catch (error) {
    document.querySelector("#login-error").textContent = error.message;
  }
};
window.addEventListener("popstate", render);
(async () => {
  if (
    location.pathname === "/" &&
    (location.hash ||
      (location.search && !new URLSearchParams(location.search).has("view")))
  ) {
    location.replace("/dan" + location.search + location.hash);
    return;
  }
  try {
    auth = await api("/auth");
    document.querySelector("#owner-button").textContent = auth.authenticated
      ? "Log out"
      : "Owner login";
    if (!auth.configured)
      document.querySelector("#owner-help").textContent =
        "The owner account has not been set up yet. Follow Owner setup in the user guide on the host computer.";
  } catch {}
  await render();
  setInterval(notify, 30000);
})();
