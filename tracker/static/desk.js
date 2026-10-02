"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const read = (key, fallback = null) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
  const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); return true; } catch { return false; } };
  const date = v => v ? new Intl.DateTimeFormat("en-GB", {day:"2-digit", month:"short", year:"numeric", timeZone:"America/New_York"}).format(new Date(v.length === 10 ? v + "T12:00:00Z" : v)) : "Not available";
  const stamp = v => v ? date(v) + " · " + new Intl.DateTimeFormat("en-GB", {hour:"2-digit", minute:"2-digit", timeZone:"America/New_York"}).format(new Date(v)) + " ET" : "Not recorded";
  const pp = v => Number(v) !== 0 && Math.abs(Number(v)) < .005 ? (Number(v)>0 ? "Increase" : "Decrease") + " <0.01 pp" : (Number(v) > 0 ? "+" : "") + Number(v).toFixed(2) + " pp";
  async function get(url) {
    const response = await fetch(url, {cache:"no-store", signal:AbortSignal.timeout(15000)});
    if (!response.ok) { const body = await response.json().catch(() => ({})); const error = new Error(body.detail || `Request failed (${response.status})`); error.status=response.status; throw error; }
    return response.json();
  }
  function loadView(scope, restore = true) { return DeskState.fromURL(scope, new URL(location.href), restore ? read(`desk.view.${scope}`, {}) : {}); }
  function saveView(scope, view, push = false) {
    const clean = DeskState.normalize(scope, view), url = DeskState.viewURL(scope, clean);
    write(`desk.view.${scope}`, clean);
    if (location.pathname + location.search !== url) history[push ? "pushState" : "replaceState"](null, "", url);
    updateLinks();
  }
  function updateLinks() {
    document.querySelectorAll(".desk-switch a").forEach(a => {
      const scope = a.textContent.trim() === "DBMF" ? "dbmf" : "dan";
      const saved = read(`desk.view.${scope}`);
      a.href = DeskState.viewURL(scope, saved || {});
    });
  }
  function reveal(id) {
    const target = $(id); if (!target) return;
    target.focus({preventScroll:true});
    target.scrollIntoView({behavior:matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth", block:"start"});
  }
  const visitCache = new Map();
  function visit(scope) {
    if (!visitCache.has(scope)) {
      const saved = read(`desk.visit.${scope}`);
      visitCache.set(scope, saved && Number.isSafeInteger(saved.after_id) && saved.after_id >= 0 ? saved : null);
    }
    return visitCache.get(scope);
  }
  function markVisit(scope, cursor, at) {
    // Capture the previous visit once; polling never moves this tab's baseline.
    visit(scope);
    if (document.visibilityState === "visible") write(`desk.visit.${scope}`, {...cursor, at});
  }

  let activityRequest = 0;
  async function activity(scope, mode, onMarket, onEvidence) {
    const request = ++activityRequest, prior = visit(scope), params = new URLSearchParams();
    if (mode === "visit" && prior) {
      if (scope === "dbmf") { params.set("since_id", prior.after_id); if (prior.report_id) params.set("baseline_id", prior.report_id); }
      else params.set("after_id", prior.after_id);
    }
    const root = $("changes-content"), caption = $("changes-caption");
    try {
      const data = await get((scope === "dbmf" ? "/api/dbmf/changes" : "/api/changes") + "?" + params);
      if (request !== activityRequest) return;
      const firstVisit = mode === "visit" && !prior;
      const visitLabel = mode === "visit" && prior ? `Since your last visit: ${stamp(prior.at)}. ` : firstVisit ? "First visit in this browser. Showing the previous report comparison. " : "";
      if (scope === "dbmf") {
        caption.textContent = visitLabel + (data.current && data.comparison ? `Reporting dates: ${date(data.comparison.source_date)} → ${date(data.current.source_date)}. Exposure changes in percentage points.` : "A second accepted report is needed to calculate exposure changes.");
        const marketItems = data.items.filter(m => m.category !== "Collateral");
        root.innerHTML = marketItems.length ? `<div class="change-grid">${marketItems.slice(0,5).map(m => `<button class="change-item" data-change-market="${esc(m.id)}"><span>${esc(m.name)}</span><strong>${pp(m.change_pp)}</strong><small>${Number(m.before_pct).toFixed(2)}% → ${Number(m.after_pct).toFixed(2)}%${m.kind === "new" ? " · Newly present" : m.kind === "absent" ? " · Now absent" : ""}</small></button>`).join("")}</div>` : `<p class="change-empty">${data.comparison ? "No market exposure changes in this comparison." : "The first report establishes a baseline."}</p>`;
        const collateral = data.items.filter(m => m.category === "Collateral");
        if (collateral.length) root.innerHTML += `<p class="change-footnote">Collateral: ${collateral.map(m => `${esc(m.name)} ${pp(m.change_pp)}`).join(" · ")}</p>`;
        if (data.revisions.length) root.innerHTML += `<p class="change-footnote">${data.revisions.length}${data.revisions_more ? "+" : ""} report revision${data.revisions.length === 1 ? "" : "s"} recorded. <button class="inline-action" id="changes-revisions">Compare preserved versions →</button></p>`;
        if (mode === "visit" && prior && data.new_reports) root.innerHTML += `<p class="change-footnote">${data.new_reports} accepted report${data.new_reports === 1 ? "" : "s"} saved since your visit, including historical imports and revisions. The cards compare the latest portfolio with the one you last viewed.</p>`;
        root.querySelectorAll("[data-change-market]").forEach(b => b.addEventListener("click", () => onMarket(b.dataset.changeMarket)));
        $("changes-revisions")?.addEventListener("click", () => { $("revision-panel").open = true; reveal("revision-panel"); });
      } else {
        const labels = {baseline:"Tracking baseline", added:"Newly disclosed", removed:"No longer disclosed", direction_changed:"Direction changed", strategy_changed:"Strategy updated", correction:"Interpretation corrected"};
        caption.textContent = visitLabel + (data.current ? `Latest source: ${stamp(data.current.source_as_of)}. ${mode !== "visit" && data.previous ? `Since the check on ${stamp(data.previous.fetched_at)}.` : ""}` : "Waiting for the first accepted disclosure.");
        root.innerHTML = data.items.length ? `<div class="change-grid">${data.items.slice(0,5).map(e => `<button class="change-item" data-change-event="${e.id}"><span>${esc(e.name)}</span><strong>${esc(labels[e.kind] || e.kind)}</strong><small>Observed ${stamp(e.observed_at)} · View evidence →</small></button>`).join("")}</div>` : '<p class="change-empty">No new disclosure changes in this period.</p>';
        if (data.total > 5) root.innerHTML += `<p class="change-footnote">Showing the latest 5 of ${data.total} changes. <a href="/?page=history">Open the full history →</a></p>`;
        root.querySelectorAll("[data-change-event]").forEach(b => b.addEventListener("click", () => onEvidence(data.items.find(e => e.id === Number(b.dataset.changeEvent)))));
        if(data.cursor_reset)caption.textContent="The saved visit belongs to a newer database. Showing changes since the previous accepted check. Latest source: " + stamp(data.current?.source_as_of);
      }
      markVisit(scope, data.cursor, new Date().toISOString());
    } catch (error) {
      if (request !== activityRequest) return;
      if(error.status===404 && mode==="visit" && prior) {
        visitCache.set(scope,null);
        await activity(scope,mode,onMarket,onEvidence);
        caption.textContent="Your saved visit report is no longer available. " + caption.textContent;
        return;
      }
      caption.textContent = "Change summary unavailable. " + error.message;
      root.innerHTML = '<p class="change-empty">Refresh to retry. Saved selections and source history are preserved.</p>';
    }
  }

  let latestHealth = null, healthBusy = false, offlineCount = 0, alertProblem = "", alertMemory = {};
  const alertKey = "desk.alerts.v1";
  function alertSupport() { return "Notification" in window && window.isSecureContext; }
  function alertButton() {
    const button = $("browser-alerts"), enabled = read(alertKey, {}).enabled && alertSupport() && Notification.permission === "granted";
    button.textContent = enabled ? "Disable browser alerts" : "Enable browser alerts";
    button.setAttribute("aria-pressed", String(Boolean(enabled)));
    button.disabled = !alertSupport();
    $("alert-note").textContent = alertProblem || (!alertSupport() ? "Browser alerts are unavailable here. The status panel still works." : Notification.permission === "denied" ? "Notifications are blocked in browser settings. The status panel still works." : enabled ? "Alerts are on for this browser while a dashboard tab is open. One notification per problem, plus recovery." : "Optional alerts while a dashboard tab is open. Nothing is sent to an external service.");
  }
  async function notifyIssues(issues) {
    const run = () => {
      const settings = read(alertKey, {});
      if (!settings.enabled || !alertSupport() || Notification.permission !== "granted") return;
      const prior = settings.active || alertMemory;
      const next = DeskState.transitions(prior, issues);
      for (const [resolved, items] of [[false, next.opened], [true, next.resolved]]) {
        if (!items.length) continue;
        try {
          const note = new Notification(resolved ? "Research Desk · recovered" : "Research Desk · needs attention", {
            body: items.map(i => resolved ? i.title + " — resolved" : i.title).join("\n"),
            tag: resolved ? "desk-recovered" : "desk-attention",
          });
          note.onclick = () => { window.focus(); reveal("data-health"); note.close(); };
          note.onerror = () => { alertProblem = "This browser could not display an alert. Check its notification settings; the status panel remains available."; alertButton(); };
        } catch {
          alertProblem = "System alerts are unavailable in this browser. Use the status panel here.";
          write(alertKey, {...settings, enabled:false}); alertButton(); return;
        }
      }
      alertMemory = next.active;
      write(alertKey, {...settings, active:next.active});
    };
    if (navigator.locks) await navigator.locks.request("research-desk-alerts", run);
    else run();
  }
  function renderHealth(data) {
    const openPrices = [...$("health-content").querySelectorAll('details[open]')].map(d=>d.id);
    const issueCount = data.issues.length;
    $("health-summary").textContent = issueCount ? `${issueCount} item${issueCount === 1 ? "" : "s"} need attention` : "Both dashboards are up to date";
    $("health-summary").classList.toggle("health-warning", Boolean(issueCount));
    $("health-checked").textContent = "Checked " + stamp(data.server_time);
    const content = `<div class="health-grid">${data.desks.map(d => {
      const dates = d.prices.map(p => p.latest_date).filter(Boolean).sort();
      const summary = !dates.length ? "No completed bars" : dates[0] === dates.at(-1) ? date(dates[0]) : `${date(dates[0])} to ${date(dates.at(-1))}`;
      return `<article><h3><a href="${d.link}">${d.name}</a></h3><dl><div><dt>Source reporting date</dt><dd>${d.id === "dan" ? stamp(d.source_date) : date(d.source_date)}${d.source_stale ? " · Old source" : ""}</dd></div><div><dt>Last successful source check</dt><dd>${stamp(d.last_collected)}${d.collection_stale ? " · Overdue" : ""}</dd></div><div><dt>Next scheduled check</dt><dd>${stamp(d.next_check)}</dd></div><div><dt>Latest completed prices</dt><dd>${summary}</dd></div><div><dt>Collector</dt><dd>${d.running ? "Collecting" : d.worker_healthy ? "Running" : "Heartbeat missing"}</dd></div></dl><details id="health-prices-${d.id}"><summary>Price freshness by market</summary><div class="table-wrap"><table><thead><tr><th>Market</th><th>Latest bar</th><th>Last successful refresh</th><th>Status</th></tr></thead><tbody>${d.prices.map(p => `<tr><th scope="row">${esc(p.name)}</th><td>${date(p.latest_date)}</td><td>${stamp(p.price_checked_at)}</td><td>${p.price_error ? "Refresh failed" : p.stale ? "Waiting for newer prices" : "Current"}</td></tr>`).join("")}</tbody></table></div></details></article>`;
    }).join("")}</div><ul class="health-issues">${data.issues.map(i => `<li><strong>${esc(i.title)}</strong><span>${esc(i.detail)}</span></li>`).join("")}</ul><p class="change-footnote">${esc(data.price_rule)} Last backup: ${stamp(data.last_backup)}.</p>`;
    // Avoid replacing focused controls on every heartbeat-only update.
    if($("health-content").dataset.rendered!==content) {
      $("health-content").innerHTML=content;
      $("health-content").dataset.rendered=content;
      openPrices.forEach(id=>{if($(id))$(id).open=true;});
    }
  }
  async function refreshHealth() {
    if (healthBusy) return; healthBusy = true;
    try {
      const data = await get('/api/desk/health');
      latestHealth = data; offlineCount = 0;
      renderHealth(data); await notifyIssues(data.issues);
    } catch {
      offlineCount++;
      $("health-summary").textContent = "Connection interrupted · status may be out of date";
      $("health-summary").classList.add("health-warning");
      if (offlineCount >= 3) await notifyIssues([...(latestHealth?.issues || []), {key:"connection", title:"Cannot reach the dashboard server", notify:true, link:"/"}]);
    } finally { healthBusy = false; alertButton(); }
  }
  function initHealth() {
    $("browser-alerts").addEventListener("click", async () => {
      const settings = read(alertKey, {}); alertProblem = "";
      if (settings.enabled) write(alertKey, {...settings, enabled:false});
      else {
        try {
          const permission = await Notification.requestPermission();
          if (permission === "granted") {
            if (!write(alertKey, {enabled:true, active:{}})) alertProblem = "Browser storage is unavailable, so alerts cannot be enabled. The status panel still works.";
            else await refreshHealth();
          }
        } catch { alertProblem = "This browser could not enable notifications. The status panel still works."; }
      }
      alertButton();
    });
    window.addEventListener("storage", event => { if (event.key === alertKey) alertButton(); updateLinks(); });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshHealth(); });
    alertButton(); refreshHealth(); setInterval(refreshHealth, 60000);
  }
  window.Desk = {esc, date, stamp, pp, get, read, write, loadView, saveView, reveal, activity, visit, refreshHealth};
  updateLinks(); initHealth();
})();
