"use strict";
const $ = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const dateOptions = {
  day: "2-digit",
  month: "short",
  year: "numeric",
  timeZone: "America/New_York",
};
function date(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("en-GB", dateOptions).format(
    new Date(/^\d{4}-\d{2}-\d{2}$/.test(value) ? value + "T12:00:00Z" : value),
  );
}
function time(value) {
  return value
    ? new Intl.DateTimeFormat("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
        timeZone: "America/New_York",
      }).format(new Date(value)) + " ET"
    : "—";
}
function datetime(value) {
  return value ? date(value) + " · " + time(value) : "—";
}
const directions = {
  bullish: "Bullish",
  bearish: "Bearish",
  mixed: "Mixed",
  unknown: "Unknown",
  non_directional: "No simple direction",
  large_move: "Large moves",
  range_bound: "Price range",
  conditional: "Conditional",
};
const icons = {
  bullish: "↗",
  bearish: "↘",
  mixed: "↔",
  unknown: "·",
  non_directional: "↔",
  large_move: "↕",
  range_bound: "↔",
  conditional: "◇",
};
const eventNames = {
  baseline: "Tracking baseline",
  added: "Newly disclosed",
  removed: "No longer disclosed",
  direction_changed: "Direction changed",
  strategy_changed: "Strategy updated",
  correction: "Interpretation corrected",
};
const colors = {
  bullish: "#26765a",
  bearish: "#b85b57",
  mixed: "#8b927d",
  unknown: "#9aa290",
  non_directional: "#8b927d",
  large_move: "#727da8",
  range_bound: "#a18656",
  conditional: "#7b7890",
};
function badge(direction) {
  return `<span class="pill ${esc(direction)}">${icons[direction] || "·"} ${esc(directions[direction] || direction)}</span>`;
}
function percent(value) {
  return value == null
    ? "—"
    : (value > 0 ? "+" : "") + (value * 100).toFixed(2) + "%";
}
function win(value) {
  return value == null ? "—" : (value * 100).toFixed(0) + "%";
}
function strategyName(p) {
  if (p.strategy === "shares")
    return p.side === "long" ? "Long shares" : "Short shares";
  return p.strategy
    .replace(/\bput\b/gi, "put")
    .replace(/\bcall\b/gi, "call")
    .replace(/\bspread\b/gi, "spread");
}
function error(message) {
  $("error-banner").textContent = message;
  $("error-banner").classList.remove("hidden");
}
function toast(message) {
  (document.querySelector("dialog[open]") || document.body).appendChild($("toast"));
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  clearTimeout(state.toast);
  state.toast = setTimeout(() => $("toast").classList.add("hidden"), 3200);
}
async function get(url, asText = false) {
  const response = await fetch(window.DeskContext ? DeskContext.url(url) : url, { cache: "no-store", signal: AbortSignal.timeout(15000) });
  if (!response.ok) {
    let message;
    try {
      message = (await response.json()).detail;
    } catch {
      message = "The server could not complete this request.";
    }
    throw new Error(message);
  }
  return asText ? response.text() : response.json();
}
const state = {
  positions: [],
  instruments: [],
  status: null,
  selected: null,
  page: "overview",
  range: "6",
  chart: null,
  candles: null,
  shade: null,
  markers: null,
  bars: [],
  timeline: [],
  history: [],
  hasMore: false,
  scores: null,
  horizon: 20,
  chartRequest: 0,
  chartNeedsRefresh: true,
  historyRequest: 0,
  scoreRequest: 0,
};
const initialView = Desk.loadView(DeskContext.scope);
function applyView(view) {
  state.page=view.page;state.selected=view.symbol||null;state.range=view.range;state.horizon=Number(view.horizon);
  state.historyFilter=view.history;state.scoreFilter=view.score;
  $("shade-toggle").checked=view.shade==="1";$("changes-period").value=view.changes;
  $("history-filter").value=view.history;$("score-filter").value=view.score;
  document.querySelectorAll('[data-range]').forEach(b=>{const active=b.dataset.range===state.range;b.classList.toggle("active",active);b.setAttribute("aria-pressed",String(active));});
  document.querySelectorAll('[data-horizon]').forEach(b=>{const active=Number(b.dataset.horizon)===state.horizon;b.classList.toggle("active",active);b.setAttribute("aria-pressed",String(active));});
  state.shade?.applyOptions({visible:$("shade-toggle").checked});
}
function remember(push=false) {
  Desk.saveView(DeskContext.scope,{page:state.page,symbol:state.selected||"",range:state.range,horizon:String(state.horizon),
    history:state.historyFilter||"",score:state.scoreFilter||"",shade:$("shade-toggle").checked?"1":"0",changes:$("changes-period").value},push);
}
function activity() {return Desk.activity(DeskContext.scope,$("changes-period").value,null,e=>showEvidence(e.id,e));}
function setPage(page, push=true) {
  if (!["overview", "history", "scorecard"].includes(page)) page = "overview";
  state.page = page;
  document
    .querySelectorAll(".page")
    .forEach((p) => p.classList.toggle("active", p.id === "page-" + page));
  document
    .querySelectorAll(".nav")
    .forEach((b) => {
      const active = b.dataset.page === page;
      b.classList.toggle("active", active);
      b.setAttribute("aria-current", active ? "page" : "false");
    });
  if (page === "history") loadHistory();
  if (page === "scorecard") loadScores();
  if (page === "overview" && state.chart)
    requestAnimationFrame(() =>
      state.chart.applyOptions({ width: $("chart").clientWidth }),
    );
  remember(push);
}
function renderStatus(s) {
  state.status = s;
  DeskContext.apply(s);
  $("active-count").textContent = s.active_instruments;
  $("bull-count").textContent = s.bullish;
  $("bear-count").textContent = s.bearish;
  $("signal-count").textContent = s.eligible_signals;
  $("nav-count").textContent = s.event_count;
  $("source-date").textContent = date(s.source_as_of);
  $("source-time").textContent = time(s.source_as_of) + " · New York";
  $("last-observed").textContent = datetime(s.last_observed);
  $("next-check").textContent = datetime(s.next_scheduled_run);
  $("tracking-start").textContent = date(s.tracking_started);
  $("last-backup").textContent = s.backup_error
    ? "Needs attention"
    : datetime(s.last_backup);
  $("source-text").textContent =
    s.disclosure ||
    "The first successful disclosure has not been recorded yet.";
  const running = s.last_run?.status === "running";
  const problem =
    s.stale ||
    s.source_outdated ||
    s.backup_error ||
    (!s.worker_healthy && !running) ||
    ["error", "partial", "interrupted"].includes(s.last_run?.status);
  $("status-dot").classList.toggle("warning", !!problem);
  $("monitor-state").textContent = running
    ? "Collecting data"
    : problem
      ? "Monitoring · check status"
      : "Monitoring sources";
  $("health-pill").textContent = running
    ? "Collecting"
    : problem
      ? "Needs attention"
      : "Up to date";
  $("health-pill").className = "pill " + (problem ? "unknown" : "bullish");
  if (s.other)
    $("exposure-note").textContent =
      `${s.other} mixed or unresolved · position sizes unknown`;
  else
    $("exposure-note").textContent = "Sizing and portfolio hedges are unknown";
  const warnings = [];
  if (s.stale)
    warnings.push(
      "The latest successful observation is more than 36 hours old.",
    );
  if (s.source_outdated)
    warnings.push("CNBC’s disclosure timestamp is more than a week old.");
  if (s.last_run?.error)
    warnings.push(
      "The last collection could not establish a fresh disclosure. The last valid positions are preserved.",
    );
  if (Object.keys(s.last_run?.price_errors || {}).length)
    warnings.push(
      "Some daily prices could not be refreshed; cached prices are retained.",
    );
  if (!s.worker_healthy && !running)
    warnings.push("The daily collector has not reported a recent heartbeat.");
  if (s.backup_error) warnings.push("The latest backup needs attention.");
  if (warnings.length) error(warnings.join(" "));
  else $("error-banner").classList.add("hidden");
}
function renderPositions() {
  const list = $("positions-list");
  list.innerHTML = state.positions.length
    ? state.positions
        .map(
          (p) =>
            `<button class="position ${p.symbol === state.selected ? "selected" : ""}" data-symbol="${esc(p.symbol)}" aria-pressed="${p.symbol === state.selected}"><span class="position-heading"><span class="position-name">${esc(p.name)}</span><span class="position-arrow">${p.symbol === state.selected ? "↗" : ""}</span></span><span class="strategy">${esc(p.strategies.map(strategyName).join(" · "))}</span><span class="position-meta">${badge(p.direction)}<span class="confidence">${p.confidence === "inferred" ? "Inferred from strategy" : p.confidence === "explicit" ? "Disclosed holding" : p.confidence === "reviewed" ? "Reviewed" : "Needs review"}</span></span></button>`,
        )
        .join("")
    : '<div class="empty">No positions in the latest valid disclosure.</div>';
  list
    .querySelectorAll("[data-symbol]")
    .forEach((b) =>
      b.addEventListener("click", () => selectInstrument(b.dataset.symbol,{navigate:true,save:true})),
    );
  const equities = state.positions.filter((p) => p.asset_class !== "Bonds");
  const eqDirections = new Set(equities.map((p) => p.direction));
  $("interpretation").textContent =
    (eqDirections.has("bullish") && eqDirections.has("bearish")
      ? "The disclosed equity exposures point in both directions. "
      : "Each direction describes the disclosed position in its underlying. ") +
    "Labels describe the disclosed positions. Entry prices, position sizes, portfolio hedges and exact trade dates are undisclosed.";
}
function populateFilters() {
  for (const id of ["history-filter", "score-filter"]) {
    const select = $(id),
      previous = id === "history-filter" ? state.historyFilter : state.scoreFilter;
    select.innerHTML =
      '<option value="">All companies and funds</option>' +
      state.instruments
        .map(
          (p) =>
            `<option value="${esc(p.symbol)}">${esc(p.name)}${p.active ? "" : " · historical"}</option>`,
        )
        .join("");
    select.value = state.instruments.some(p=>p.symbol===previous) ? previous : "";
    if(id === "history-filter")state.historyFilter=select.value;else state.scoreFilter=select.value;
  }
}
function initChart() {
  if (state.chart) return;
  const L = window.LightweightCharts;
  if (!L)
    throw new Error("The chart library could not load. Refresh the page.");
  state.chart = L.createChart($("chart"), {
    autoSize: true,
    layout: {
      background: { type: "solid", color: "#ffffff" },
      textColor: "#59654f",
      fontFamily: "Inter, -apple-system, sans-serif",
      fontSize: 12,
      attributionLogo: true,
    },
    grid: { vertLines: { color: "#f4f5ef" }, horzLines: { color: "#eff2e9" } },
    rightPriceScale: {
      borderVisible: false,
      scaleMargins: { top: 0.15, bottom: 0.12 },
    },
    timeScale: {
      borderVisible: false,
      timeVisible: false,
      rightOffset: 5,
      barSpacing: 6,
    },
    crosshair: {
      mode: L.CrosshairMode.Normal,
      vertLine: { color: "#aebaa0", labelBackgroundColor: "#53674a" },
      horzLine: { color: "#aebaa0", labelBackgroundColor: "#53674a" },
    },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
    localization: { locale: "en-GB" },
  });
  state.shade = state.chart.addSeries(L.HistogramSeries, {
    priceScaleId: "exposure",
    priceLineVisible: false,
    lastValueVisible: false,
    base: 0,
    priceFormat: { type: "volume" },
    autoscaleInfoProvider: () => ({ priceRange: { minValue: 0, maxValue: 1 } }),
  });
  state.chart
    .priceScale("exposure")
    .applyOptions({ visible: false, scaleMargins: { top: 0, bottom: 0 } });
  state.candles = state.chart.addSeries(L.CandlestickSeries, {
    upColor: "#739979",
    downColor: "#c18578",
    wickUpColor: "#739979",
    wickDownColor: "#c18578",
    borderVisible: false,
    priceLineColor: "#738768",
    lastValueVisible: true,
    priceLineStyle: 2,
  });
  state.markers = L.createSeriesMarkers(state.candles, []);
  state.chart.subscribeCrosshairMove((param) => {
    const tip = $("chart-tooltip");
    if (!param.time || !param.point || param.point.x < 0 || param.point.y < 0) {
      tip.classList.add("hidden");
      return;
    }
    const key =
      typeof param.time === "string"
        ? param.time
        : `${param.time.year}-${String(param.time.month).padStart(2, "0")}-${String(param.time.day).padStart(2, "0")}`;
    const events = state.timeline.filter((e) => e.chart_date === key);
    if (!events.length) {
      tip.classList.add("hidden");
      return;
    }
    tip.innerHTML = events
      .map(
        (e) =>
          `<b>${esc(e.name)}</b><br>${esc(eventNames[e.kind])} · ${esc(directions[e.direction])}<br>Observed ${esc(datetime(e.observed_at))}<br>Source ${esc(datetime(e.source_as_of))}`,
      )
      .join("<br><br>");
    tip.classList.remove("hidden");
  });
}
function applyRange() {
  if (!state.bars.length) return;
  const end = state.bars.at(-1).time;
  if (state.range === "all") state.chart.timeScale().fitContent();
  else {
    const from = new Date(end + "T12:00:00Z");
    from.setUTCMonth(from.getUTCMonth() - Number(state.range));
    const start = from.toISOString().slice(0, 10);
    state.chart
      .timeScale()
      .setVisibleRange({
        from: start < state.bars[0].time ? state.bars[0].time : start,
        to: end,
      });
  }
}
async function selectInstrument(symbol,{navigate=false,save=false}={}) {
  const request = ++state.chartRequest;
  state.chartNeedsRefresh = true;
  // Clear the old instrument before relabelling the panel. A failed request
  // must never leave another instrument's candles under this one's name.
  if (state.chartSymbol !== symbol) {
    state.chartSymbol = symbol;
    state.bars = []; state.timeline = [];
    state.candles?.setData([]); state.shade?.setData([]); state.markers?.setMarkers([]);
    $("chart-tooltip").classList.add("hidden");
    $("price-note").textContent = ""; $("observation-note").textContent = "";
  }
  state.selected = symbol;
  DeskContext.selected=symbol;
  window.DeskOwner?.selection();
  if(save)remember(true);
  if(navigate)Desk.reveal("instrument-detail");
  renderPositions();
  const current = state.positions.find((p) => p.symbol === symbol),
    instrument = current || state.instruments.find((p) => p.symbol === symbol);
  if (!instrument) return;
  DeskContext.direction=current?.direction;DeskContext.reviewable=current?.strategies.length===1;window.DeskOwner?.selection();
  $("chart-title").textContent = instrument.name;
  window.OptionsDesk.load(symbol, instrument.name);
  $("chart-category").textContent = (
    instrument.asset_class + " · DAILY PRICE CHART"
  ).toUpperCase();
  $("chart-description").textContent =
    instrument.description || "Recorded public disclosure";
  $("chart-strategy").textContent = current
    ? current.strategies.map(strategyName).join(" · ")
    : "No longer in the current disclosure";
  $("chart-direction").innerHTML = badge(current?.direction || "unknown");
  $("tradingview-button").disabled = !instrument.tv_symbol;
  $("tradingview-button").title = instrument.tv_symbol
    ? "Export this instrument’s history"
    : "TradingView symbol mapping needs verification";
  $("chart-message").textContent = "Loading daily prices…";
  $("chart-message").classList.remove("hidden");
  try {
    const [prices, events] = await Promise.all([
      get("/api/prices/" + encodeURIComponent(symbol)),
      get("/api/timeline/" + encodeURIComponent(symbol)),
    ]);
    if (request !== state.chartRequest) return;
    initChart();
    state.bars = prices.bars;
    state.timeline = events;
    state.candles.setData(
      prices.bars.map(({ time, open, high, low, close }) => ({
        time,
        open,
        high,
        low,
        close,
      })),
    );
    const byDay = new Set(prices.bars.map((b) => b.time));
    state.markers.setMarkers(
      events
        .filter((e) => byDay.has(e.chart_date))
        .map((e) => ({
          time: e.chart_date,
          position: e.direction === "bearish" ? "aboveBar" : "belowBar",
          color:
            e.kind === "baseline" || e.kind === "removed"
              ? "#939d87"
              : colors[e.direction],
          shape: "circle",
          text: (eventNames[e.kind] || "Update") + " · " + instrument.name,
          size: 1,
        })),
    );
    let index = 0,
      direction = "unknown";
    state.shade.setData(
      prices.bars.map((b) => {
        while (index < events.length && events[index].chart_date <= b.time) {
          direction = events[index].direction;
          index++;
        }
        return {
          time: b.time,
          value: 1,
          color:
            direction === "bullish"
              ? "rgba(142,186,133,.13)"
              : direction === "bearish"
                ? "rgba(209,148,131,.12)"
                : ({
                    mixed: "rgba(166,178,147,.10)",
                    large_move: "rgba(112,123,172,.11)",
                    range_bound: "rgba(165,132,78,.11)",
                    conditional: "rgba(123,120,144,.10)",
                  }[direction] || "rgba(255,255,255,0)"),
        };
      }),
    );
    state.shade.applyOptions({ visible: $("shade-toggle").checked });
    applyRange();
    $("chart-message").classList.toggle("hidden", prices.bars.length > 0);
    if (!prices.bars.length)
      $("chart-message").textContent = prices.error
        ? "Daily prices are unavailable. The disclosure history is preserved."
        : "Waiting for the first completed daily price bar.";
    $("price-note").textContent = prices.bars.length
      ? "Through " +
        date(prices.bars.at(-1).time) +
        " · Adjusted daily prices" +
        (prices.error ? " · Refresh pending" : "")
      : "Daily prices, adjusted for splits and dividends";
    const last = events.at(-1),
      unplotted = events.filter((e) => !byDay.has(e.chart_date));
    $("observation-note").innerHTML = last
      ? `<strong>${esc(eventNames[last.kind])}</strong> · ${esc(date(last.observed_at))}${last.kind === "baseline" ? " · Already disclosed when tracking began. Entry date unknown." : ""}${unplotted.length ? " · A marker appears when its completed daily bar is available." : ""}`
      : "No recorded observations.";
    state.chartNeedsRefresh = false;
  } catch (e) {
    if (request === state.chartRequest) {
      $("chart-message").textContent = e.message;
      $("chart-message").classList.remove("hidden");
    }
  }
}
async function loadHistory(append = false) {
  if (append && (state.historyLoading || !state.hasMore)) return;
  const request = ++state.historyRequest;
  state.historyLoading = true;
  $("load-more").disabled = true;
  if (!append) {
    state.history = [];
    state.hasMore = false;
    $("history-body").innerHTML = "";
    $("load-more").classList.add("hidden");
    $("history-empty").textContent = "Loading disclosure history…";
    $("history-empty").classList.remove("hidden");
  }
  const filter = state.historyFilter;
  const params = new URLSearchParams({ limit: "100" });
  if (filter) params.set("symbol", filter);
  if (append && state.history.length)
    params.set("before_id", state.history.at(-1).id);
  try {
    const data = await get("/api/events?" + params);
    if (request !== state.historyRequest) return;
    state.history = append ? state.history.concat(data.items) : data.items;
    state.hasMore = data.has_more;
    $("history-body").innerHTML = state.history
      .map(
        (e) =>
          `<tr><td class="event-time">${esc(date(e.observed_at))}<small>${esc(time(e.observed_at))}</small></td><td class="name-cell">${esc(e.name)}</td><td>${esc(eventNames[e.kind] || e.kind)}</td><td>${badge(e.direction)}</td><td><button class="evidence-button" data-event="${e.id}" aria-label="View evidence for ${esc(e.name)}">↗</button></td></tr>`,
      )
      .join("");
    $("history-empty").textContent = "No recorded events for this selection.";
    $("history-empty").classList.toggle("hidden", state.history.length > 0);
    $("load-more").classList.toggle("hidden", !state.hasMore);
    document
      .querySelectorAll("[data-event]")
      .forEach((b) =>
        b.addEventListener("click", () =>
          showEvidence(Number(b.dataset.event)),
        ),
      );
  } catch (e) {
    if (request !== state.historyRequest) return;
    if (!append) $("history-empty").textContent = "History unavailable. Refresh this page to retry.";
    error(e.message);
  } finally {
    if (request === state.historyRequest) {
      state.historyLoading = false;
      $("load-more").disabled = false;
    }
  }
}
function showEvidence(id, supplied=null) {
  const e = supplied || state.history.find((x) => x.id === id);
  if (!e) return;
  $("evidence-title").textContent = e.name;
  $("evidence-metadata").innerHTML = [
    ["Event", eventNames[e.kind]],
    ["First observed", datetime(e.observed_at)],
    ["Source as of", datetime(e.source_as_of)],
    ["Chart date", date(e.chart_date)],
    ["Direction", directions[e.direction]],
    ["Interpretation", e.confidence],
    ["Score eligible", e.eligible ? "Yes" : "No"],
  ]
    .map(
      ([key, value]) => `<div><dt>${esc(key)}</dt><dd>${esc(value)}</dd></div>`,
    )
    .join("");
  $("evidence-quote").textContent = e.raw_text;
  $("evidence-strategies").innerHTML =
    (e.after.length ? e.after : e.before)
      .map(
        (p) =>
          `<p><strong>${esc(strategyName(p))}</strong><br>${esc(p.explanation)}</p>`,
      )
      .join("") + (e.reason ? `<p>Review: ${esc(e.reason)}</p>` : "");
  $("download-source").href = e.source_href || "/api/snapshots/" + e.snapshot_id + "/source";
  $("evidence-dialog").showModal();
}
async function loadScores() {
  const request = ++state.scoreRequest;
  state.scores = null;
  for (const id of ["score-cards", "score-summary", "signals-list"])
    $(id).innerHTML = "";
  $("score-empty").classList.add("hidden");
  $("sample-note").textContent = "Loading scorecard…";
  try {
    const data = await get(
      "/api/scorecard" +
        (state.scoreFilter
          ? "?symbol=" + encodeURIComponent(state.scoreFilter)
          : ""),
    );
    if (request !== state.scoreRequest) return;
    state.scores = data;
    const prior=$('score-class').value;
    const classes=[...new Set(data.summary.map(r=>r.asset_class||'All'))];
    $('score-class').innerHTML=classes.map(c=>`<option value="${esc(c)}">${esc(c)}</option>`).join('');
    $('score-class').value=classes.includes(prior)?prior:classes[0];
    renderScores();
  } catch (e) {
    if (request !== state.scoreRequest) return;
    $("sample-note").textContent = "Scorecard unavailable. Refresh this page to retry.";
    error(e.message);
  }
}
function renderScores() {
  if (!state.scores) return;
  const data = state.scores;
  const rows=data.summary.filter(r=>!r.asset_class||r.asset_class===$('score-class').value);
  const row = rows.find((r) => r.horizon === state.horizon);
  if(!row)return;
  const suffix=$('score-cost').value==='net'?'_net':'';
  const modes = [
    ["follow", "Follow direction", "Directional return"],
    ["oppose", "Fade direction", "Opposite directional return"],
    ["always_long", "Always long", "Same instrument and dates"],
  ];
  $("score-cards").innerHTML = modes
    .map(([key, title, subtitle]) => {
      const m = row[key+suffix]||row[key];
      return `<article class="score-card ${key}"><div class="score-label"><span>${esc(title)}</span><span>${key === "follow" ? "↗" : key === "oppose" ? "↘" : "→"}</span></div><div class="score-big ${m.mean == null ? "" : m.mean >= 0 ? "bullish" : "bearish"}">${percent(m.mean)}</div><p class="fine-print">${esc(subtitle)} · ${state.horizon} session${state.horizon === 1 ? "" : "s"}</p><div class="score-meta"><span>Win rate <strong>${win(m.win_rate)}</strong></span><span>Median <strong>${percent(m.median)}</strong></span></div></article>`;
    })
    .join("");
  $("sample-note").textContent =
    `${row.follow.n} completed signals · ${row.pending} pending · ${row.overlaps||0} overlaps excluded`;
  $("score-empty").classList.toggle("hidden", row.follow.n > 0);
  $("pending-note").textContent = row.missing
    ? `${row.missing} signals awaiting complete price data`
    : row.pending
      ? `${row.pending} measurement periods still in progress`
      : "Prospective tracking · no invented historical trades";
  $("score-summary").innerHTML = rows
    .map(
      (r) =>
        `<tr><td>${r.horizon} session${r.horizon === 1 ? "" : "s"}</td><td>${percent((r["follow"+suffix]||r.follow).mean)}</td><td>${percent((r["oppose"+suffix]||r.oppose).mean)}</td><td>${percent((r["always_long"+suffix]||r.always_long).mean)}</td><td>${r.follow.n}</td><td>${r.pending} / ${r.missing}</td></tr>`,
    )
    .join("");
  $("methodology-text").textContent = data.methodology + (data.assumptions ? ` Trading cost: ${data.assumptions.cost_bps} bps per side. Annual short borrowing: ${data.assumptions.borrow_rate*100}%.` : '') + (data.as_of ? ' Calculated '+datetime(data.as_of)+'.' : '');
  let saved=$('saved-score-run');
  if(!saved){saved=document.createElement('p');saved.id='saved-score-run';$('methodology-text').after(saved);}
  saved.innerHTML=data.run_id?`<a href="/research?view=analytics&run=${encodeURIComponent(data.run_id)}">Saved inputs, benchmark comparisons and uncertainty →</a>`:'';
  $("signals-list").innerHTML = data.signals.length
    ? data.signals
        .filter(s=>!s.asset_class||s.asset_class===$("score-class").value)
        .slice()
        .reverse()
        .map(
          (s) =>
            `<div class="signal-item"><div><strong>${esc(s.name)}</strong>${badge(s.direction)} · Observed ${esc(date(s.observed_at))}</div><div>Entry ${esc(date(s.entry_date))} · ${s.results[String(state.horizon)].status === "complete" ? percent(s.results[String(state.horizon)]["follow"+suffix]) : esc(s.results[String(state.horizon)].status.replaceAll("_", " "))}</div></div>`,
        )
        .join("")
    : '<div class="empty">New directional disclosures will appear here.</div>';
}
async function openExport() {
  const instrument = state.instruments.find((p) => p.symbol === state.selected);
  if (!instrument) return;
  try {
    const text = await get(
      "/api/export/pine/" + encodeURIComponent(state.selected),
      true,
    );
    $("export-instrument").textContent = instrument.name;
    $("export-text").value = text;
    const header = text.split("\n")[0].split("|");
    $("export-coverage").textContent =
      `${header[6]} of ${header[5]} recorded events · export generated ${datetime(header[3])}${header[5] !== header[6] ? " · Older events remain in the dashboard." : ""}`;
    $("export-dialog").showModal();
  } catch (e) {
    toast(e.message);
  }
}
async function copyExport() {
  try {
    await navigator.clipboard.writeText($("export-text").value);
    toast("Export copied. Paste it into the indicator’s settings.");
  } catch {
    $("export-text").focus();
    $("export-text").select();
    toast("Select all export text and copy it to TradingView.");
  }
}
async function refresh() {
  try {
    const [status, positions, instruments] = await Promise.all([
      get("/api/status"),
      get("/api/positions"),
      get("/api/instruments"),
    ]);
    const changed =
      !state.status ||
      state.status.last_run?.finished_at !== status.last_run?.finished_at ||
      state.status.event_count !== status.event_count || state.status.price_version !== status.price_version;
    state.positions = positions;
    state.instruments = instruments;
    renderStatus(status);
    populateFilters();
    if (!state.selected || !instruments.some(p=>p.symbol===state.selected))
      state.selected =
        positions.find((p) => p.symbol === "MSFT")?.symbol ||
        positions[0]?.symbol ||
        instruments[0]?.symbol;
    renderPositions();
    if(!state.selected){
      $('chart-title').textContent='No disclosed instruments';
      $('chart-message').textContent=status.disclosure?'The accepted disclosure contains no positions.':'No accepted disclosure is available yet.';
      $('chart-message').classList.remove('hidden');
      $('analysis-content').textContent='No current disclosed strategies. Reviewed calls are available separately.';
    }
    if ((changed || state.chartNeedsRefresh) && state.selected) await selectInstrument(state.selected);
    if (changed && state.page === "history") loadHistory();
    if (changed && state.page === "scorecard") loadScores();
    remember();
    if(changed)await activity();
  } catch (e) {
    error(
      "Cannot reach the dashboard server. Check your connection and refresh. " +
        e.message,
    );
    $("monitor-state").textContent = "Connection interrupted";
    $("status-dot").classList.add("warning");
  }
}
document
  .querySelectorAll("[data-page]")
  .forEach((b) => b.addEventListener("click", () => setPage(b.dataset.page)));
document.querySelector(".brand").addEventListener("click", (e) => {
  e.preventDefault();
  setPage("overview");
});
document.querySelectorAll("[data-range]").forEach((b) =>
  b.addEventListener("click", () => {
    state.range = b.dataset.range;
    document
      .querySelectorAll("[data-range]")
      .forEach((x) => {x.classList.toggle("active", x === b);x.setAttribute("aria-pressed",String(x===b));});
    remember(true);
    applyRange();
  }),
);
document.querySelectorAll("[data-horizon]").forEach((b) =>
  b.addEventListener("click", () => {
    state.horizon = Number(b.dataset.horizon);
    document
      .querySelectorAll("[data-horizon]")
      .forEach((x) => {x.classList.toggle("active", x === b);x.setAttribute("aria-pressed",String(x===b));});
    remember(true);
    renderScores();
  }),
);
$("shade-toggle").addEventListener("change", () => {
  remember(true);state.shade?.applyOptions({ visible: $("shade-toggle").checked });
});
$("tradingview-button").addEventListener("click", openExport);
$("copy-export").addEventListener("click", copyExport);
$("close-export").addEventListener("click", () => $("export-dialog").close());
$("close-evidence").addEventListener("click", () =>
  $("evidence-dialog").close(),
);
$("history-filter").addEventListener("change", () => {state.historyFilter=$("history-filter").value;remember(true);loadHistory();});
$("score-filter").addEventListener("change", () => {state.scoreFilter=$("score-filter").value;remember(true);loadScores();});
$("changes-period").addEventListener("change",()=>{remember(true);activity();});
$("load-more").addEventListener("click", () => loadHistory(true));
for (const id of ["export-dialog", "evidence-dialog"])
  $(id).addEventListener("click", (e) => {
    if (e.target === $(id)) {
      const r = $(id).getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        $(id).close();
    }
  });
applyView(initialView);
setPage(state.page,false);
window.addEventListener("popstate",()=>{applyView(Desk.loadView(DeskContext.scope,false));setPage(state.page,false);if(state.selected)selectInstrument(state.selected);activity();});
refresh();
setInterval(refresh, 60000);

$("copy-script").addEventListener("click", async () => {
  try {
    const script = await get("/downloads/research-desk.pine", true);
    await navigator.clipboard.writeText(script);
    toast("Indicator copied. Paste it into a new Pine Editor script.");
  } catch (e) {
    toast("Use Download indicator to get the script.");
  }
});

$("score-class").addEventListener("change",renderScores);
$("score-cost").addEventListener("change",renderScores);

window.addEventListener("contributor-review-saved",()=>{state.chartNeedsRefresh=true;refresh();});
