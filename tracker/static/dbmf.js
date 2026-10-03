"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const pct = (value, digits=2) => value == null ? "—" : `${Number(value)>0?"+":""}${Number(value).toFixed(digits)}%`;
  const pp = value => value == null ? "—" : Desk.pp(value);
  const fmtDate = value => value ? new Intl.DateTimeFormat("en-GB", {day:"2-digit",month:"short",year:"numeric",timeZone:"UTC"}).format(new Date(value.slice(0,10)+"T12:00:00Z")) : "—";
  const stamp = value => value ? new Intl.DateTimeFormat("en-GB", {day:"2-digit",month:"short",hour:"2-digit",minute:"2-digit",timeZone:"America/New_York"}).format(new Date(value))+" ET" : "—";
  const money = value => value == null ? "Not reported" : new Intl.NumberFormat("en-US", {style:"currency",currency:"USD",maximumFractionDigits:2}).format(Number(value));
  const dayAfter = (day, offset=1) => { const d=new Date(day+"T12:00:00Z");d.setUTCDate(d.getUTCDate()+offset);return d.toISOString().slice(0,10); };
  const missedWeekday = (from,to) => {for(let d=dayAfter(from);d<to;d=dayAfter(d)){const weekday=new Date(d+"T12:00:00Z").getUTCDay();if(weekday!==0&&weekday!==6)return true;}return false;};
  const initialView = Desk.loadView(DeskContext.scope);
  const state = {status:null,exposures:null,history:null,market:"us2y",range:"all",report:null,compare:"previous",compareDate:null,compareId:null,prices:null,chart:null,request:0,comparisonRequest:0,evidenceRequest:0,reports:new Map()};
  let revisionItems = [], revisionRequest = 0, revisionsMore = false;
  let refreshVersion = null, refreshBusy = false;
  function applyView(view) {
    state.market=view.market;state.range=view.range;state.compare=view.compare;state.compareDate=view.date||null;
    state.compareId=DeskContext.reportId(view.compare_report);state.report=DeskContext.reportId(view.report);state.revision=DeskContext.reportId(view.revision);
    $("former-markets").checked=view.former==="1";$("changes-period").value=view.changes;
    $("comparison").value=state.compare;$("comparison-date").value=state.compareDate||"";
    $("comparison-date").classList.toggle("hidden",state.compare!=="date");
    rangeButtons();
  }
  function remember(push=false) {
    Desk.saveView(DeskContext.scope,{market:state.market,range:state.range,compare:state.compare,date:state.compareDate||"",
      compare_report:state.compareId||"",report:state.report||"",revision:state.revision||"",
      former:$("former-markets").checked?"1":"0",changes:$("changes-period").value},push);
  }
  function rangeButtons() {
    document.querySelectorAll('[data-range]').forEach(b=>{const active=b.dataset.range===state.range;b.classList.toggle("active",active);b.setAttribute("aria-pressed",String(active));});
  }
  function activity() { return Desk.activity(DeskContext.scope,$("changes-period").value,id=>selectMarket(id,{navigate:true,save:true}).catch(fail)); }
  async function get(url) {
    const res=await fetch(window.DeskContext ? DeskContext.url(url) : url,{cache:"no-store",signal:AbortSignal.timeout(15000)});
    if (!res.ok) { let detail;try {detail=(await res.json()).detail;} catch {} const error=new Error(typeof detail==="string"?detail:`Request failed (${res.status})`);error.status=res.status;throw error; }
    return res.json();
  }
  function fail(error) { $("dbmf-error").textContent=error.message||String(error);$("dbmf-error").classList.remove("hidden"); }
  function currentMarket() {return state.exposures?.current?.markets.find(m=>m.id===state.market);}
  function rangeStart() {
    if (state.range==="all") return DeskContext.legacyFund ? state.status?.coverage.inception||"2019-05-07" : state.prices?.bars?.[0]?.time || state.history?.observations?.[0]?.date || state.status?.server_time?.slice(0,10) || new Date().toISOString().slice(0,10);
    const latest=state.status?.server_time?.slice(0,10)||new Date().toISOString().slice(0,10);
    const d=new Date(latest+"T12:00:00Z"), day=d.getUTCDate();d.setUTCDate(1);d.setUTCMonth(d.getUTCMonth()-Number(state.range));
    const max=new Date(Date.UTC(d.getUTCFullYear(),d.getUTCMonth()+1,0)).getUTCDate();d.setUTCDate(Math.min(day,max));return d.toISOString().slice(0,10);
  }
  function visibleReports() {return (state.history?.observations||[]).filter(r=>r.date>=rangeStart());}
  function applyFundLabels() {
    if(!DeskContext.equity)return;
    document.querySelector('.page-heading .eyebrow').textContent='A VIEW INSIDE THE PORTFOLIO';
    const cards=document.querySelectorAll('.dbmf-stats article');
    cards[2].querySelector('.stat-label').textContent='Current holdings';
    cards[2].querySelector('.stat-note').textContent='Accepted issuer report';
    cards[2].querySelector('.dbmf-longshort > span').hidden=true;
    cards[3].querySelector('.stat-label').textContent='Top five holdings';
    cards[3].querySelector('.stat-note').textContent='Combined portfolio weight';
    document.querySelector('#position-panel .eyebrow').textContent='REPORTED PORTFOLIO WEIGHT';
    document.querySelector('.position-column-head > span').textContent='Holding';
    document.querySelector('.bar-axis').innerHTML='<span>0</span><span>Portfolio weight →</span>';
    document.querySelector('.collateral-section').hidden=true;
    document.querySelector('#contract-evidence > summary').textContent='Holding details and source evidence';
    document.querySelector('.dbmf-chart-label').innerHTML='Stock price · upper panel <span>Observed holding weight (%) · lower panel</span>';
    $('history-heading').textContent='Holdings history';
    document.querySelector('#market-detail .eyebrow').textContent='HOLDING DETAIL';
    document.querySelector('.market-selector').firstChild.textContent='Holding';
    document.querySelector('[data-jump="history-panel"]').textContent='Holdings history ↓';
    document.querySelector('.dbmf-chart-label + #dbmf-chart + p').textContent='Holding weights appear only on reporting dates. Lines are not drawn between unknown positions. Scroll or pinch to zoom both panels together.';
    document.querySelector('.heatmap-legend').innerHTML='<span>Smaller weight</span><i></i><span>Larger weight</span><span>Each cell is an observed report.</span>';
    document.querySelector('.dbmf-method').innerHTML='<h3>How to read the holdings</h3><p>Weights are the percentages published by the issuer. Changes in weight may reflect prices, fund flows or transactions; they do not establish buys or sells. Quantities and market values remain available in source evidence.</p><p>Only accepted reporting dates are shown. Holdings between reports are unknown. Stock charts use split- and dividend-adjusted prices; no fund portfolio return is estimated.</p>';
    document.querySelector('.dbmf-footer a').textContent='Download holdings history CSV';
  }
  function renderStatus(s) {
    state.status=s;
    DeskContext.apply(s);
    applyFundLabels();
    const issues=[];
    if (s.last_run?.error && !s.last_run.recovered_by_replay) issues.push(`Last collection: ${s.last_run.error} The last valid report is retained.`);
    if (s.collection_stale) issues.push("Holdings have not been successfully checked in the last 26 hours.");
    if (s.holdings_stale) issues.push("The latest holdings date is more than four days old.");
    if (!s.worker_healthy) issues.push("The fund collector has no recent heartbeat.");
    if (Object.keys(s.last_run?.price_errors||{}).length) issues.push("Some price references could not refresh; cached prices are retained.");
    if (s.backup_error) issues.push("The latest backup needs attention.");
    $("dbmf-error").textContent=issues.join(" ");$("dbmf-error").classList.toggle("hidden",!issues.length);
    $("health-dot").classList.toggle("warning",Boolean(issues.length));
    $("health-text").textContent=s.last_run?.status==="running"?"Collecting data":issues.length?"Monitoring · check status":"Monitoring sources";
    $("schedule-note").textContent=`Checks ${s.schedule}. Next: ${stamp(s.next_scheduled_run)}. Backup: ${stamp(s.last_backup)}.`;
    $("collection-time").textContent=`Last checked ${stamp(s.last_collected)}`;
    const review=s.review||{items:[],pending_reports:0};
    $("mapping-review").classList.toggle("hidden",!review.items.length);
    $("mapping-count").textContent=`${review.unmapped_names||0} unfamiliar name${review.unmapped_names===1?"":"s"} · ${review.pending_reports} saved report${review.pending_reports===1?"":"s"}`;
    $("mapping-items").innerHTML=review.items.length?`<table class="evidence-table"><thead><tr><th>Original contract</th><th>Latest reporting date</th><th>Reported value (USD)</th><th>Value / net assets</th><th>Source</th></tr></thead><tbody>${review.items.map(x=>`<tr><td>${esc(x.original_name)}<br><small>${esc(x.ticker||x.identifier||"")}</small></td><td>${fmtDate(x.reporting_date)}</td><td>${money(x.reported_value)}</td><td>${pct(x.value_pct,4)}</td><td><a href="${DeskContext.api}/reports/${x.report_id}/source">Download report</a></td></tr>`).join("")}</tbody></table>`:"";
    const c=s.coverage;
    $("coverage-caption").innerHTML=`<b>${c.observations} observed reporting dates</b> · ${c.historical_reports} official historical reports${c.first_report?` · ${fmtDate(c.first_report)} to ${fmtDate(c.latest_report)}`:""}. ${esc(c.note)}${c.sec_access.length?`<details><summary>Historical coverage and source access</summary><p>Fund inception: ${fmtDate(c.inception)}. Historical search checked ${stamp(c.checked_at)}.</p>${c.sec_access.map(x=>`<p>${fmtDate(x.date)} · <a href="${esc(x.url)}" target="_blank" rel="noopener">SEC filing ↗</a> · ${esc(x.status)}</p>`).join("")}</details>`:""}${s.historical_errors.length?`<p>${s.historical_errors.length} historical source imports need attention.</p>`:""}`;
  }
  function renderExposures() {
    const r=state.exposures, now=r.current;
    if (!now) { $("market-bars").innerHTML='<p class="empty">No accepted holdings report yet. Collection status appears above.</p>';return; }
    $("holdings-date").textContent=fmtDate(now.source_date);
    $("net-assets").textContent=now.net_assets == null ? "Not reported" : new Intl.NumberFormat("en-US",{style:"currency",currency:"USD",notation:"compact",maximumFractionDigits:2}).format(Number(now.net_assets));
    $("net-assets").title=money(now.net_assets);
    $("gross-long").textContent=pct(now.summary.long_pct,1);$("gross-short").textContent=pct(now.summary.short_pct,1);
    $("collateral-total").textContent=now.summary.collateral_pct == null ? "Not reported" : Number(now.summary.collateral_pct).toFixed(1)+"%";
    if(DeskContext.equity){
      $("gross-long").textContent=now.summary.holdings;
      $("gross-short").textContent="";
      $("collateral-total").textContent=pct(now.summary.top_five_pct,1);
    }
    const comparison=r.comparison;
    $("comparison-caption").innerHTML=comparison?`Current: <strong>${fmtDate(now.source_date)}</strong> · Comparison actually used: <strong>${fmtDate(comparison.source_date)}</strong>${r.requested_date?` (requested ${fmtDate(r.requested_date)}${r.requested_date!==comparison.source_date?"; latest available earlier report":""})`:""}${comparison.source_kind==="historical"?" · ◇ Historical report":""}`:`Current: <strong>${fmtDate(now.source_date)}</strong> · ${r.requested_date?`No report exists on or before ${fmtDate(r.requested_date)}.`:"No previous report is available."} Changes are unavailable.`;
    const markets=now.markets.filter(m=>m.category!=="Collateral" && ($("former-markets").checked || m.provider_symbol || Number(m.net_pct)!==0 || Number(m.comparison_pct)!==0));
    const max=Math.max(25,...markets.flatMap(m=>[Math.abs(Number(m.net_pct)),Math.abs(Number(m.comparison_pct||0))]));
    const scale=Math.ceil(max/25)*25;
    const bar=(v,css)=>{const n=Number(v);if(DeskContext.equity)return `<i class="${css}" style="left:0;width:${Math.abs(n)/scale*100}%"></i>`;const width=Math.abs(n)/scale*50;return `<i class="${css}${n<0?" short":""}" style="left:${n<0?50-width:50}%;width:${width}%"></i>`;};
    $("market-bars").innerHTML=markets.map(m=>`<button class="market-bar-row" data-market="${esc(m.id)}" aria-pressed="${m.id===state.market}" title="${esc(m.name)}: net ${pct(m.net_pct,4)}; long ${pct(m.long_pct,4)}, short ${pct(m.short_pct,4)}, gross ${pct(m.gross_pct,4)}; change ${pp(m.change_pp)}"><span class="market-bar-name">${esc(m.name)}${m.contract_roll?" · contract roll":""}${!m.provider_symbol?'<small>Price reference unavailable</small>':""}</span><span class="bar-track" aria-hidden="true">${m.comparison_pct!==null?bar(m.comparison_pct,"bar-outline"):""}${bar(m.net_pct,"bar-fill")}</span><span class="bar-net ${Number(m.net_pct)<0?"bearish":"bullish"}">${pct(m.net_pct)}</span><span class="bar-change">${pp(m.change_pp)}</span></button>`).join("");
    $("market-bars").querySelectorAll("button").forEach(b=>b.addEventListener("click",()=>selectMarket(b.dataset.market,{navigate:true,save:true}).catch(fail)));
    $("bar-scale").textContent=DeskContext.equity?`Axis: 0% to ${scale}%`:`Axis: −${scale}% to +${scale}%`;
    $("collateral-items").innerHTML=now.markets.filter(m=>m.category==="Collateral").map(m=>`<span>${esc(m.name)} <b>${m.absent?"Not separately reported":pct(m.net_pct)}</b></span>`).join("");
    $("collateral-note").textContent="Only separately identified collateral is included. Other assets less liabilities are not assumed to be cash.";
    $("market-select").innerHTML=now.markets.filter(m=>m.category!=="Collateral").map(m=>`<option value="${esc(m.id)}"${m.id===state.market?" selected":""}>${esc(m.name)}</option>`).join("");
    renderMetrics();
  }
  function heatColour(value) {const n=Number(value),a=Math.min(Math.abs(n),100)/100;return n<0?`rgba(190,125,107,${.08+a*.53})`:`rgba(114,151,110,${.06+a*.5})`;}
  function renderHeatmap() {
    const reports=visibleReports(), markets=(state.history?.markets||[]).filter(m=>m.category!=="Collateral" && (m.provider_symbol||reports.some(r=>Number(r.exposures[m.id].net_pct)!==0)));
    if (!reports.length) {$("heatmap").innerHTML='<p class="empty">No holdings reports in this range. Choose a longer range to see earlier observations.</p>';return;}
    const columns=[];let previous=DeskContext.legacyFund?rangeStart():reports[0].date;
    reports.forEach(r=>{const gap=Math.round((new Date(r.date)-new Date(previous))/86400000);if(missedWeekday(previous,r.date))columns.push({gap,from:previous,to:r.date});columns.push(r);previous=r.date;});
    const today=state.status.server_time.slice(0,10), trailing=Math.round((new Date(today)-new Date(previous))/86400000);if(missedWeekday(previous,today))columns.push({gap:trailing,from:previous,to:today});
    const gapTip=c=>`${c.gap} calendar days between ${fmtDate(c.from)} and ${fmtDate(c.to)}; intervening positions are unknown`;
    $("heatmap").innerHTML=`<table class="heatmap-table"><thead><tr><th scope="col">Market / reporting date</th>${columns.map(c=>c.gap?`<th class="heatmap-gap" title="${gapTip(c)}">${c.gap}d<br>gap</th>`:`<th scope="col">${c.source_kind==="historical"?"◇":"●"} ${fmtDate(c.date).replace(/ (\d{4})$/,"<br>$1")}<br>${c.revision>1?`rev. ${c.revision}`:""}</th>`).join("")}</tr></thead><tbody>${markets.map(m=>`<tr><th scope="row">${esc(m.name)}</th>${columns.map(c=>c.gap?`<td class="heatmap-gap" title="${gapTip(c)}"><span aria-hidden="true">·</span></td>`:`<td><button class="heatmap-cell${state.compareId===c.id&&state.market===m.id?" selected":""}" data-report="${c.id}" data-market="${esc(m.id)}" style="background:${heatColour(c.exposures[m.id].net_pct)}" aria-label="${esc(m.name)}, ${fmtDate(c.date)}, ${pct(c.exposures[m.id].net_pct)}, ${c.source_kind} report" title="${fmtDate(c.date)} · ${esc(m.name)}: ${pct(c.exposures[m.id].net_pct,4)}${c.exposures[m.id].absent?" · absent from complete report":""}">${pct(c.exposures[m.id].net_pct,1)}</button></td>`).join("")}</tr>`).join("")}</tbody></table>`;
    $("heatmap").querySelectorAll("button").forEach(b=>b.addEventListener("click",async()=>{
      state.compareId=DeskContext.reportId(b.dataset.report);state.compare="date";state.compareDate=state.history.observations.find(r=>r.id===state.compareId).date;state.report=state.compareId;
      $("comparison").value="date";$("comparison-date").classList.remove("hidden");$("comparison-date").value=state.compareDate;
      try {await Promise.all([loadComparison(),selectMarket(b.dataset.market,{navigate:true})]);remember(true);} catch(e){fail(e);}
    }));
  }
  function renderMetrics() {
    const m=currentMarket();if(!m)return;
    $("detail-heading").textContent=m.name;
    const values=visibleReports().map(r=>Number(r.exposures[m.id].net_pct));
    $("detail-metrics").innerHTML=(DeskContext.equity ? [['Current weight',pct(m.net_pct)],['Change',pp(m.change_pp)],['Shares',m.holdings.some(h=>h.quantity==null)?'Not reported':m.holdings.reduce((n,h)=>n+Number(h.quantity),0).toLocaleString('en-US')],['Market value',money(m.net_notional)],['Observed weight range',values.length?`${pct(Math.min(...values),1)} to ${pct(Math.max(...values),1)}`:'No reports']] : [['Current net',pct(m.net_pct)],['Current long',pct(m.long_pct)],['Current short',pct(m.short_pct)],['Current gross',pct(m.gross_pct)],['Observed range · selected period',values.length?`${pct(Math.min(...values),1)} to ${pct(Math.max(...values),1)}`:'No reports']]).map(([label,value],i)=>`<div><span>${label}</span><b class="${i===4?"range-metric":""}">${value}</b></div>`).join("");
  }
  function initChart() {
    const L=window.LightweightCharts;if(!L)throw new Error("The chart library could not load. Refresh the page.");
    state.chart=L.createChart($("dbmf-chart"),{autoSize:true,layout:{background:{type:"solid",color:"#fff"},textColor:"#59654f",fontSize:12,attributionLogo:true,panes:{separatorColor:"#e5e8df",separatorHoverColor:"#d3dfc5",enableResize:true}},grid:{vertLines:{color:"#f7f8f4"},horzLines:{color:"#f0f3e9"}},rightPriceScale:{borderVisible:false,minimumWidth:75},timeScale:{borderVisible:false,rightOffset:3},crosshair:{mode:L.CrosshairMode.Normal,vertLine:{color:"#96aa88",labelBackgroundColor:"#53674a"},horzLine:{color:"#96aa88",labelBackgroundColor:"#53674a"}},localization:{locale:"en-GB"}});
    state.candles=state.chart.addSeries(L.CandlestickSeries,{upColor:"#739979",downColor:"#c18578",wickUpColor:"#739979",wickDownColor:"#c18578",borderVisible:false,priceLineVisible:false});
    state.exposureSeries=state.chart.addSeries(L.LineSeries,{color:"#537c57",lineVisible:false,pointMarkersVisible:true,pointMarkersRadius:4,priceLineVisible:false,lastValueVisible:false,priceFormat:{type:"custom",formatter:v=>v.toFixed(1)+"%",minMove:.01}},1);
    state.exposureSeries.createPriceLine({price:0,color:"#c7d4bd",lineWidth:1,lineStyle:2,axisLabelVisible:false,title:""});
    state.markers=L.createSeriesMarkers(state.exposureSeries,[]);
    state.chart.panes()[0].setStretchFactor(2);state.chart.panes()[1].setStretchFactor(1);
    state.chart.subscribeCrosshairMove(param=>{
      if(!param.time)return;
      const day=typeof param.time==="string"?param.time:`${param.time.year}-${String(param.time.month).padStart(2,"0")}-${String(param.time.day).padStart(2,"0")}`;
      const price=param.seriesData.get(state.candles),report=state.history.observations.find(r=>r.date===day);
      $("detail-readout").textContent=`${fmtDate(day)} · Price close: ${price?.close!=null?Number(price.close).toLocaleString("en-US",{maximumFractionDigits:state.prices?.market.invert?7:4}):"no completed bar"} · ${DeskContext.equity?"Weight":"Exposure"}: ${report?pct(report.exposures[state.market].net_pct,4)+(report.source_kind==="historical"?" ◇ Historical report":" ● Daily report"):"no report on this date"}`;
    });
    state.chart.subscribeClick(param=>{if(!param.time)return;const key=typeof param.time==="string"?param.time:`${param.time.year}-${String(param.time.month).padStart(2,"0")}-${String(param.time.day).padStart(2,"0")}`;const r=state.history.observations.find(r=>r.date===key);if(r){state.report=r.id;remember(true);renderEvidence().catch(fail);}});
  }
  function drawChart() {
    if(!state.prices||!state.history)return;
    if(!state.chart)initChart();
    const {market:m,bars}=state.prices;
    state.candles.applyOptions({priceFormat:{type:"price",precision:m.invert?6:m.price_kind==="currency"?4:2,minMove:m.invert?.000001:m.price_kind==="currency"?.0001:.01}});
    const missing=state.prices.omitted_bars||[];
    state.candles.setData([...bars,...missing.map(b=>({time:b.date}))].sort((a,b)=>a.time.localeCompare(b.time)));
    const points=new Map(state.history.observations.map(r=>[r.date,Number(r.exposures[state.market].net_pct)]));
    const dates=[...new Set([...bars.map(b=>b.time),...points.keys(),rangeStart(),state.status.server_time.slice(0,10)])].sort();
    state.exposureSeries.setData(dates.map(time=>points.has(time)?{time,value:points.get(time)}:{time}));
    state.markers.setMarkers(state.history.observations.map(r=>({time:r.date,position:"inBar",shape:r.source_kind==="historical"?"square":"circle",color:r.id===state.report?"#263e2c":"#829f70",size:r.id===state.report?1.4:.8,text:r.id===state.report?"Selected":""})));
    const end=dates.at(-1),start=rangeStart();
    state.chart.timeScale().setVisibleRange({from:start,to:end<=start?dayAfter(start):end});
    $("price-label").textContent=m.provider_symbol?`${m.price_reference} · ${m.provider_symbol} · ${m.price_kind}`:m.price_reference;
    $("price-date").textContent=`Latest completed bar: ${fmtDate(state.prices.latest_price_date)}`;
    $("price-note").textContent=[state.prices.warning,state.prices.adjustment,m.invert?(m.id==="jpy"?"USD per yen: rising prices mean a stronger yen.":"The currency quote is inverted to match the exposure’s direction."):m.category==="Bonds"?"Rising prices mean stronger bonds, not higher yields.":m.category==="Short-term rates"?"Rising futures prices correspond to lower implied short-term interest rates.":""].filter(Boolean).join(" ");
    const stale=state.prices.latest_price_date && Math.round((new Date(state.status.server_time.slice(0,10))-new Date(state.prices.latest_price_date))/86400000)>4;
    const error=m.price_error?`Price refresh failed: ${m.price_error} Cached data remains visible.`:!bars.length?m.provider_symbol?"No completed price data is available yet. Exposure observations remain available.":"This market has no verified price reference. Exposure observations remain available.":stale?"The latest cached price is more than four days old.":missing.length?`${missing.length} inconsistent provider bars are omitted across the cached history; those dates remain gaps.`:"";
    $("price-error").textContent=error;$("price-error").classList.toggle("hidden",!error);
    $("price-quality").classList.toggle("hidden",!missing.length);
    $("price-quality-summary").textContent=`Inspect ${missing.length} excluded price ${missing.length===1?"bar":"bars"} · full history`;
    $("price-quality-content").innerHTML=missing.length?`<p>${esc(state.prices.quality_note||"Original provider values are shown before currency inversion.")}</p><p>Provider symbol: <strong>${esc(m.provider_symbol)}</strong>. Last successful price refresh: ${stamp(m.price_checked_at)}.</p><div class="evidence-table-wrap price-quality-table" tabindex="0" role="region" aria-label="Excluded price bars, scroll for all dates"><table class="evidence-table"><thead><tr><th scope="col">Date</th><th scope="col">Open</th><th scope="col">High</th><th scope="col">Low</th><th scope="col">Close</th><th scope="col">Why excluded</th></tr></thead><tbody>${missing.slice().reverse().map(b=>`<tr><td>${fmtDate(b.date)}</td>${["open","high","low","close"].map(k=>`<td>${esc(b.raw_ohlc?.[k]??"Not saved")}</td>`).join("")}<td>${esc((b.issues||[b.reason]).join("; "))}</td></tr>`).join("")}</tbody></table></div><p><a href="${DeskContext.api}/prices/${encodeURIComponent(m.id)}">Price data and validation evidence (JSON)</a></p>`:"";
    $("detail-readout").textContent=`${m.name} · Move over either panel for the same date. ${DeskContext.equity?"Holding weight":"Exposure"} is available only on observed reporting dates.`;
  }
  async function renderEvidence() {
    if(!state.exposures?.current)return;
    const request=++state.evidenceRequest,id=state.report||state.exposures.current.id,market=state.market;
    $("evidence-report").innerHTML=state.history.observations.slice().reverse().map(r=>`<option value="${r.id}"${r.id===id?" selected":""}>${fmtDate(r.date)} · ${r.source_kind==="historical"?"historical":"daily"} report${r.revision>1?` · revision ${r.revision}`:""}</option>`).join("");
    if(!state.reports.has(id)) {
      try {state.reports.set(id,await get(`/api/dbmf/reports/${id}`));}
      catch(error) {if(error.status===404&&state.report){state.report=null;remember();return renderEvidence();}throw error;}
    }
    if(request!==state.evidenceRequest||market!==state.market)return;
    const report=state.reports.get(id);
    if(!report.markets){state.report=null;remember();return renderEvidence();}
    if(!state.history.observations.some(r=>r.id===id))$("evidence-report").add(new Option(`${fmtDate(report.source_date)} · preserved revision ${report.revision}`,String(id),true,true));
    const m=report.markets.find(m=>m.id===market);
    $("evidence-content").innerHTML=`<p><strong>${esc(m.name)} · ${fmtDate(report.source_date)}</strong> · Revision ${report.revision}. Net assets: ${money(report.net_assets)}. Collected ${stamp(report.collected_at)}.</p><p>${esc(report.metadata.completeness||"Accepted complete issuer report")}. Parser: ${esc(report.parser_version)}.</p>${m.holdings.length?`<div class="evidence-table-wrap"><table class="evidence-table"><thead><tr><th>${DeskContext.equity?"Holding":"Original contract"}</th><th>${DeskContext.equity?"Security identifier":"Expiry / identifier"}</th><th>${DeskContext.equity?"Market value (USD)":"Signed notional (USD)"}</th><th>${DeskContext.equity?"Weight":"Exposure"}</th><th>${DeskContext.equity?"Currency":"Published weight"}</th></tr></thead><tbody>${m.holdings.map(h=>`<tr><td>${esc(h.original_name)}<br><small>Source quantity: ${esc(h.quantity||"not shown")}</small></td><td>${esc(h.expiry||"—")}<br>${esc(h.identifier||h.ticker||"—")}</td><td>${money(h.notional)}</td><td title="${esc(h.exposure_pct)}%">${pct(h.exposure_pct,6)}</td><td>${DeskContext.equity?esc(h.currency||"Not reported"):h.weight!=null?pct(Number(h.weight)*100)+" (rounded)":"Not supplied"}</td></tr>`).join("")}</tbody></table></div><details><summary>Original source rows</summary><pre class="source-row">${esc(m.holdings.map(h=>h.evidence).join("\n\n"))}</pre></details>`:'<p>This market is absent from the accepted complete report. Its exposure is recorded as zero.</p>'}<div class="evidence-links"><a href="${DeskContext.api}/reports/${id}/source">Download archived source</a><a href="${esc(report.source_url)}" target="_blank" rel="noopener">Open issuer report ↗</a><a href="${DeskContext.api}/reports/${id}">Full report and precision</a></div>`;
    const risk=(report.risk_measures||[]).filter(h=>h.asset_id===market);
    if(risk.length)$('evidence-content').insertAdjacentHTML('beforeend',`<h3>Issuer risk measures</h3><p>${esc(report.metadata.risk_basis||'Issuer methodology; separate from ordinary notional exposure.')}</p><table class="evidence-table"><thead><tr><th>Measure</th><th>Value</th><th>Unit</th></tr></thead><tbody>${risk.map(h=>`<tr><td>${esc(h.measure)}</td><td>${esc(h.value)}</td><td>${esc(h.unit)}</td></tr>`).join('')}</tbody></table>`);
    if(state.markers)state.markers.setMarkers(state.history.observations.map(r=>({time:r.date,position:"inBar",shape:r.source_kind==="historical"?"square":"circle",color:r.id===id?"#263e2c":"#829f70",size:r.id===id?1.4:.8,text:r.id===id?"Selected":""})));
  }
  function availableMarket(id) {
    const markets=state.history?.markets||[];
    if(!markets.some(m=>m.id===id&&m.category!=="Collateral"))id=markets.find(m=>m.id==="us2y")?.id||markets.find(m=>m.category!=="Collateral")?.id;
    return id;
  }
  async function selectMarket(id,{navigate=false,save=false}={}) {
    id=availableMarket(id);
    if(!id)return;
    const request=++state.request;state.market=id;
    if(save)remember(true);
    if(navigate)Desk.reveal("market-detail");
    if(state.prices?.market.id!==id){state.prices=null;if(state.candles){state.candles.setData([]);state.exposureSeries.setData([]);state.markers.setMarkers([]);}$("price-label").textContent="Loading price reference…";$("price-date").textContent="";$("price-note").textContent="";$("price-error").classList.add("hidden");$("price-quality").classList.add("hidden");$("price-quality-content").textContent="";$("evidence-content").textContent="Loading source evidence…";$("detail-readout").textContent="Loading market prices…";}
    renderExposures();renderHeatmap();
    const response=await get(`/api/dbmf/prices/${encodeURIComponent(id)}`);
    if(request!==state.request)return;
    state.prices=response;drawChart();await renderEvidence();
  }
  async function loadComparison() {
    const request=++state.comparisonRequest;
    const query=state.compareId?`report_id=${state.compareId}`:`compare=${state.compare}${state.compareDate&&state.compare==="date"?`&compare_date=${state.compareDate}`:""}`;
    let result;
    try {result=await get('/api/dbmf/exposures?'+query);}
    catch(error) {if(request!==state.comparisonRequest)return;if(error.status===404&&state.compareId){state.compareId=null;state.compare="previous";$("comparison").value="previous";$("comparison-date").classList.add("hidden");remember();return loadComparison();}throw error;}
    if(request!==state.comparisonRequest)return;
    state.exposures=result;state.market=availableMarket(state.market);
    renderExposures();renderHeatmap();await renderEvidence();
  }
  async function refresh(initial=false) {
    if(refreshBusy)return;
    refreshBusy=true;
    try {
      const s=await get('/api/dbmf/status');renderStatus(s);
      const version=JSON.stringify([s.last_run?.id,s.last_run?.status,s.latest_report_id,s.catalog_version,s.current?.id,s.price_version]);
      if(initial||refreshVersion!==version){
        state.reports.clear();
        state.history=await get('/api/dbmf/history');await loadComparison();
        await selectMarket(state.market);remember();
        await Promise.all([activity(),loadRevisions()]);
        // Commit the version only after all panels have loaded successfully.
        refreshVersion=version;
      }
    } finally {refreshBusy=false;}
  }
  async function loadRevisions(older=false) {
    const response=await get("/api/dbmf/revisions"+(older&&revisionItems.length?`?before_id=${revisionItems.at(-1).id}`:""));
    revisionItems=older?[...revisionItems,...response.items]:response.items;revisionsMore=response.has_more;
    $("revision-count").textContent=revisionItems.length?`${revisionItems.length}${revisionsMore?"+":""} preserved comparisons`:"No revisions recorded";
    $("older-revisions").classList.toggle("hidden",!revisionsMore);
    $("revision-select").innerHTML=revisionItems.length?revisionItems.map(r=>`<option value="${r.id}">${fmtDate(r.source_date)} · ${r.source_kind} · revision ${r.revision} · saved ${stamp(r.collected_at)}</option>`).join(""):'<option value="">No revised reports yet</option>';
    $("revision-select").disabled=!revisionItems.length;
    if(!revisionItems.length&&!state.revision){$("revision-content").innerHTML='<p class="change-empty">No accepted report has been revised. When a source changes for an existing reporting date, its original and updated values will appear here.</p>';return;}
    await renderRevision(state.revision||revisionItems[0]?.id);
  }
  async function renderRevision(id) {
    if(!id)return;
    const request=++revisionRequest;
    $("revision-content").innerHTML='<p class="change-empty">Comparing preserved source rows…</p>';
    let data;
    try {data=await get(`/api/dbmf/revisions/${id}`);}
    catch(error){if(request!==revisionRequest)return;$("revision-content").innerHTML=`<p class="change-empty">${esc(error.message)}</p>`;return;}
    if(request!==revisionRequest)return;
    if(!revisionItems.some(r=>r.id===id))$("revision-select").add(new Option(`${fmtDate(data.current.source_date)} · revision ${data.current.revision}`,String(id)));
    $("revision-select").value=String(id);
    if(!data.previous){$("revision-content").innerHTML='<p class="change-empty">This is the first accepted version of this reporting date and source type.</p>';return;}
    const origin={source_changed:"The source changed for this reporting date. These differences describe a report revision, not trading activity.",reprocessed:"The source bytes are identical. These differences come from reprocessing the archived report.",source_and_processing:"Both the source and its parser or mapping changed. The differences cannot be attributed solely to an issuer correction."};
    const meta=(r,label)=>`<article><h3>${label} · revision ${r.revision}</h3><div>Reporting date: <strong>${fmtDate(r.source_date)}</strong></div><div>Fund net assets: <strong>${money(r.net_assets)}</strong></div><div>Collected: ${stamp(r.collected_at)}</div><div>Parser ${esc(r.parser_version)} · mapping ${esc(r.mapping_version)}</div><a href="${DeskContext.api}/reports/${r.id}/source">Download ${label.toLowerCase()} source</a></article>`;
    const fields={measure:'Measure',unit:'Unit',market_id:"Market mapping",original_name:"Original contract name",identifier:"Identifier",ticker:"Ticker",expiry:"Expiry",quantity:"Source quantity",notional:"Reported value · USD",weight:"Published weight · fraction",exposure_pct:"Exposure · %"};
    const rows=data.rows.map(row=>`<tbody><tr class="revision-row-heading"><th colspan="3">${esc((row.after||row.before).original_name)} · ${esc(row.kind)}</th></tr>${row.fields.map(field=>`<tr><th scope="row">${fields[field]}</th><td>${esc(row.before?.[field]??"Not present")}</td><td class="changed-value">${esc(row.after?.[field]??"Not present")}</td></tr>`).join("")}</tbody>`).join("");
    $("revision-content").innerHTML=`<p class="change-caption">${origin[data.origin]} ${data.changed_rows} changed, added or removed contract rows.${data.net_assets_changed?" Fund net assets changed, affecting the exposure denominator.":""}</p><div class="revision-metadata">${meta(data.previous,"Before")}${meta(data.current,"After")}</div>${data.markets.length?`<p class="change-footnote">Net exposure effects: ${data.markets.map(m=>`${esc(m.name)} ${pp(m.change_pp)}`).join(" · ")}</p>`:""}${rows?`<div class="table-wrap revision-values" tabindex="0" role="region" aria-label="Exact revision values"><table><thead><tr><th>Contract / changed field</th><th>Before</th><th>After</th></tr></thead>${rows}</table></div>`:'<p class="change-empty">No contract values changed. Compare report metadata and the archived sources above.</p>'}<p class="change-footnote">Values retain their stored precision. Contract rows are matched by market, expiry and exact identifier (or ticker/name when no identifier is supplied). Ambiguous rows appear as removed and added.</p>`;
  }
  $("comparison").addEventListener("change",()=>{state.compare=$("comparison").value;state.compareId=null;$("comparison-date").classList.toggle("hidden",state.compare!=="date");if(state.compare==="date"){state.compareDate=$("comparison-date").value||state.exposures?.current?.source_date||new Date().toISOString().slice(0,10);$("comparison-date").value=state.compareDate;}remember(true);loadComparison().catch(fail);});
  $("comparison-date").addEventListener("change",()=>{if(!DeskState.day($("comparison-date").value))return;state.compareDate=$("comparison-date").value;state.compareId=null;remember(true);loadComparison().catch(fail);});
  $("former-markets").addEventListener("change",()=>{remember(true);renderExposures();});
  $("market-select").addEventListener("change",()=>selectMarket($("market-select").value,{save:true}).catch(fail));
  $("evidence-report").addEventListener("change",()=>{state.report=DeskContext.reportId($("evidence-report").value);remember(true);renderEvidence().catch(fail);});
  $("changes-period").addEventListener("change",()=>{remember(true);activity();});
  $("revision-select").addEventListener("change",()=>{state.revision=DeskContext.reportId($("revision-select").value);remember(true);renderRevision(state.revision).catch(fail);});
  $("older-revisions").addEventListener("click",()=>loadRevisions(true).catch(fail));
  document.querySelectorAll('[data-range]').forEach(b=>b.addEventListener("click",()=>{state.range=b.dataset.range;rangeButtons();remember(true);renderHeatmap();renderMetrics();drawChart();}));
  document.querySelectorAll('[data-jump]').forEach(b=>b.addEventListener("click",()=>Desk.reveal(b.dataset.jump)));
  window.addEventListener("popstate",async()=>{applyView(Desk.loadView(DeskContext.scope,false));try{await Promise.all([loadComparison(),selectMarket(state.market),activity(),renderRevision(state.revision||revisionItems[0]?.id)]);}catch(error){fail(error);}});
  applyView(initialView);
  if(state.revision)$("revision-panel").open=true;
  refresh(true).catch(fail);setInterval(()=>refresh().catch(fail),60000);
})();
