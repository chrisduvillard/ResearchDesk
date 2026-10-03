/* The same research switchers on the briefing and both detailed desks. */
"use strict";
(() => {
  const root = document.querySelector("#desk-navigation");
  if (!root) return;
  const contributors = [
    ["dan-nathan", "Dan Nathan"],
    ["karen-finerman", "Karen Finerman"],
    ["guy-adami", "Guy Adami"],
    ["josh-brown", "Josh Brown"],
    ["steve-weiss", "Steve Weiss"],
    ["tim-seymour", "Tim Seymour"],
  ];
  const funds = ["DBMF", "KMLM", "CTA", "WTMF", "ARKK", "ARKQ", "ARKW", "ARKG", "ARKF", "ARKX"];
  root.innerHTML = `
    <div class="desk-nav-primary">
      <a class="desk-home" href="/?view=today" data-desk-view="today">Today</a>
      <div class="desk-selector"><label for="desk-person">CNBC contributor</label>
        <select id="desk-person"><option value="">Choose a person</option>${contributors.map(([id, name]) => `<option value="${id}">${name}</option>`).join("")}</select>
      </div>
      <div class="desk-selector"><label for="desk-fund">Fund</label>
        <select id="desk-fund"><option value="">Choose a fund</option>${funds.map(id => `<option value="${id}">${id}</option>`).join("")}</select>
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
      "/research?view=contributors&id=" + encodeURIComponent(person.value));
  });
  fund.addEventListener("change", () => {
    if (fund.value) location.assign(fund.value === "DBMF" ? "/dbmf" :
      "/research?view=funds&fund=" + encodeURIComponent(fund.value));
  });
  function sync() {
    const params = new URLSearchParams(location.search);
    const view = params.get("view") || "today";
    const research = ["/", "/research"].includes(location.pathname);
    person.value = location.pathname === "/dan" ? "dan-nathan" : research ?
      (view === "contributors" ? params.get("id") : params.get("contributor")) || "" : "";
    fund.value = location.pathname === "/dbmf" ? "DBMF" : research && view === "funds" ? params.get("fund") || "" : "";
    for (const a of root.querySelectorAll("[data-desk-view]")) {
      if (research && a.dataset.deskView === view && !(view === "funds" && fund.value))
        a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    }
  }
  window.DeskNavigation = { sync };
  window.addEventListener("pageshow", sync);
  window.addEventListener("popstate", sync);
  sync();
})();
