/* The same research switchers on the briefing and both detailed desks. */
"use strict";
(() => {
  const root = document.querySelector("#desk-navigation");
  if (!root) return;
  root.innerHTML = `
    <div class="desk-nav-primary">
      <a class="desk-home" href="/?view=today" data-desk-view="today">Today</a>
      <div class="desk-selector"><label for="desk-person">CNBC contributor</label>
        <select id="desk-person" disabled aria-busy="true"><option value="">Loading people…</option></select>
      </div>
      <div class="desk-selector"><label for="desk-fund">Fund</label>
        <select id="desk-fund" disabled aria-busy="true"><option value="">Loading funds…</option></select>
      </div>
    </div>
    <div class="desk-nav-tools">
      <a href="/research?view=funds" data-desk-view="funds">Compare funds</a>
      <a href="/research?view=calls" data-desk-view="calls">Calls</a>
      <a href="/research?view=analytics" data-desk-view="analytics">Follow / fade</a>
      <a href="/research?view=sources" data-desk-view="sources">Sources & alerts</a>
    </div>`;
  const person = root.querySelector("#desk-person");
  const fund = root.querySelector("#desk-fund");
  person.addEventListener("change", () => {
    if (person.value) location.assign(person.value === "dan-nathan" ? "/dan" :
      "/contributors/" + encodeURIComponent(person.value));
  });
  fund.addEventListener("change", () => {
    if (fund.value) location.assign(fund.value === "DBMF" ? "/dbmf" :
      "/funds/" + encodeURIComponent(fund.value));
  });
  function sync() {
    const params = new URLSearchParams(location.search);
    const view = params.get("view") || "today";
    const research = ["/", "/research"].includes(location.pathname);
    person.value = location.pathname.startsWith("/contributors/") ? decodeURIComponent(location.pathname.split("/")[2]) : location.pathname === "/dan" ? "dan-nathan" : research ?
      (view === "contributors" ? params.get("id") : params.get("contributor")) || "" : "";
    fund.value = location.pathname.startsWith("/funds/") ? decodeURIComponent(location.pathname.split("/")[2]) : location.pathname === "/dbmf" ? "DBMF" : research && view === "funds" ? params.get("fund") || "" : "";
    for (const a of root.querySelectorAll("[data-desk-view]")) {
      if (research && a.dataset.deskView === view && !(view === "funds" && fund.value))
        a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  }
  async function loadChoices(select, path, hasData, prompt) {
    try {
      const response = await fetch("/api/v2/" + path, { signal: AbortSignal.timeout(15000) });
      if (!response.ok) throw Error("Could not load choices");
      const order = path === "funds"
        ? ["DBMF", "KMLM", "CTA", "WTMF"]
        : ["dan-nathan", "karen-finerman", "guy-adami", "josh-brown", "steve-weiss", "tim-seymour"];
      const rank = id => order.includes(id) ? order.indexOf(id) : order.length;
      const rows = (await response.json()).filter(hasData).sort((a, b) => rank(a.id) - rank(b.id));
      select.replaceChildren(new Option(rows.length ? prompt : "No data yet", ""));
      for (const row of rows) select.add(new Option(path === "funds" ? row.id : row.name, row.id));
      select.disabled = rows.length === 0;
      sync();
    } catch {
      select.replaceChildren(new Option("Could not load — refresh", ""));
      select.disabled = true;
    } finally {
      select.removeAttribute("aria-busy");
    }
  }
  function refresh() {
    // A source outage must not discard previously accepted data. Coverage and
    // qualification describe reliability; stored evidence controls visibility.
    return Promise.allSettled([
      loadChoices(person, "contributors", row => row.has_data, "Choose a person"),
      loadChoices(fund, "funds", row => row.latest != null, "Choose a fund"),
    ]);
  }
  window.DeskNavigation = { sync, refresh };
  window.addEventListener("pageshow", event => { sync(); if (event.persisted) refresh(); });
  window.addEventListener("popstate", sync);
  sync();
  refresh();
})();
