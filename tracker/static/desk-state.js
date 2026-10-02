/* Pure state helpers shared by both dashboards and exercised with node:test. */
(function (root) {
  "use strict";
  const choice = (values) => (v) => values.includes(String(v));
  const day = (v) => /^\d{4}-\d{2}-\d{2}$/.test(v) && !Number.isNaN(Date.parse(v)) && new Date(v).toISOString().slice(0, 10) === v;
  const id = (v) => /^\d+$/.test(v) && Number.isSafeInteger(Number(v)) && Number(v) > 0;
  const word = (v) => /^[a-zA-Z0-9_.^-]{1,40}$/.test(v);
  const optional = (test) => (v) => v === "" || test(v);
  const common = { changes: ["previous", choice(["previous", "visit"])] };
  const schemas = {
    dbmf: { ...common, market: ["us2y", word], range: ["all", choice(["1", "3", "6", "12", "all"])],
      compare: ["previous", choice(["previous", "week", "month", "date"])], date: ["", optional(day)],
      report: ["", optional(id)], revision: ["", optional(id)], compare_report: ["", optional(id)], former: ["0", choice(["0", "1"])] },
    dan: { ...common, page: ["overview", choice(["overview", "history", "scorecard"])], symbol: ["", optional(word)],
      range: ["6", choice(["6", "12", "24", "all"])], horizon: ["20", choice(["1", "5", "20", "60"])],
      history: ["", optional(word)], score: ["", optional(word)], shade: ["1", choice(["0", "1"])] },
  };
  function normalize(scope, raw = {}) {
    const view = {};
    for (const [key, [fallback, valid]] of Object.entries(schemas[scope])) {
      const value = String(raw?.[key] ?? fallback);
      view[key] = valid(value) ? value : fallback;
    }
    if (scope === "dbmf") {
      if (view.compare !== "date") { view.date = ""; view.compare_report = ""; }
      if (view.compare === "date" && !view.date && !view.compare_report) view.compare = "previous";
    }
    return view;
  }
  function fromURL(scope, url, saved) {
    const raw = Object.fromEntries(url.searchParams);
    if (scope === "dan" && ["overview", "history", "scorecard"].includes(url.hash.slice(1))) raw.page = url.hash.slice(1);
    return normalize(scope, Object.keys(raw).some(k => k in schemas[scope]) ? raw : saved);
  }
  function viewURL(scope, raw) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(normalize(scope, raw))) if (value !== "") params.set(key, value);
    return (scope === "dbmf" ? "/dbmf" : "/dan") + "?" + params;
  }
  function transitions(previous, issues) {
    const active = Object.fromEntries(issues.filter(i => i.notify || previous[i.key]).map(i => [i.key, {title: i.title, link: i.link}]));
    return {active, opened: issues.filter(i => i.notify && !previous[i.key]),
      resolved: Object.entries(previous).filter(([key]) => !issues.some(i => i.key === key)).map(([key, value]) => ({key, ...value}))};
  }
  const api = {normalize, fromURL, viewURL, transitions, day};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DeskState = api;
})(globalThis);
