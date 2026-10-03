"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const money = v => new Intl.NumberFormat("en-US", {style:"currency",currency:"USD",maximumFractionDigits:2}).format(v);
  const num = v => new Intl.NumberFormat("en-US", {maximumFractionDigits:2}).format(v);
  const labels = {bullish:"Bullish",bearish:"Bearish",large_move:"Benefits from large moves",range_bound:"Benefits from a price range",conditional:"Conditional exposure",unknown:"Needs more detail",mixed:"Mixed exposure",non_directional:"No simple direction"};
  const pill = d => '<span class="pill '+esc(d)+'">'+esc(labels[d])+'</span>';
  let request = 0, calculation = 0, company = "", strategies = [];
  const one = (kind, quantity, strike = null) => ({kind,quantity,strike,expiry:null,multiplier:kind==="stock"?1:100});
  const examples = {
    vertical:{legs:[one("call",1,100),one("call",-1,110)],net_cost:300},
    put_spread:{legs:[one("put",1,110),one("put",-1,100)],net_cost:300},
    ratio:{legs:[one("call",1,100),one("call",-2,110)],net_cost:0},
    straddle:{legs:[one("call",1,100),one("put",1,100)],net_cost:1000},
    condor:{legs:[one("put",1,80),one("put",-1,90),one("call",-1,110),one("call",1,120)],net_cost:-300},
    butterfly:{legs:[one("call",1,90),one("call",-2,100),one("call",1,110)],net_cost:200},
    collar:{legs:[one("stock",100),one("put",1,90),one("call",-1,110)],net_cost:10000},
    covered:{legs:[one("stock",100),one("call",-1,110)],net_cost:9800},
    custom:{legs:[one("call",1,100)],net_cost:null},
  };
  async function get(url) {
    const response = await fetch(window.DeskContext ? DeskContext.url(url) : url, {cache:"no-store"});
    if (!response.ok) throw new Error((await response.json()).detail || "Unable to load strategy analysis.");
    return response.json();
  }
  function renderAnalysis(index = 0) {
    const item = strategies[index];
    if (!item) {
      $("analysis-content").innerHTML = '<p class="empty">No current strategy is disclosed for this instrument. Earlier observations remain in History.</p>';
      return;
    }
    const a = item.analysis;
    const selector = strategies.length > 1
      ? '<label>Disclosed strategy<select class="analysis-select" id="analysis-select">'+strategies.map((p,i)=>'<option value="'+i+'" '+(i===index?"selected":"")+'>'+esc(p.wording)+'</option>').join("")+'</select></label>' : "";
    const table = a.legs.length
      ? '<div class="table-wrap"><table><thead><tr><th>Component</th><th>Quantity per unit</th><th>Strike</th><th>Expiration</th></tr></thead><tbody>'+
        a.legs.map(l=>'<tr><td>'+(l.quantity>0?"Buy / hold":"Sell / short")+' '+esc(l.kind==="stock"?"shares":l.kind)+'</td><td>'+num(Math.abs(l.quantity))+' '+(l.kind==="stock"?"shares":"contract(s)")+'</td><td>'+(l.kind==="stock"?"—":l.strike==null?"Not disclosed":money(l.strike))+'</td><td>'+(l.kind==="stock"?"—":esc(l.expiry || "Exact date unknown"))+'</td></tr>').join("")+'</tbody></table></div>' : "";
    const facts = [
      ["Interpretation",a.confidence==="explicit"?"From the disclosed structure":a.confidence==="reviewed"?"Sourced or manually reviewed":"Assumes the conventional named structure"],
      ["Volatility",a.volatility],["Passage of time",a.time_effect],
      ["Directional scorecard",a.score_eligible?"This structure can qualify after a new directional disclosure.":"This structure is excluded from the simple follow/oppose scorecard."]
    ];
    $("analysis-content").innerHTML =
      '<div class="analysis-layout"><div class="analysis-summary">'+selector+pill(a.direction)+
      '<h3>'+esc(a.title)+'</h3><p class="analysis-wording">Disclosed: '+esc(item.wording)+'</p><p>'+esc(a.explanation)+'</p>'+
      '<dl class="analysis-facts">'+facts.map(f=>'<div><dt>'+esc(f[0])+'</dt><dd>'+esc(f[1])+'</dd></div>').join("")+'</dl>'+
      '<p class="fine-print">'+esc(a.basis)+'</p>'+
      (a.detail_source?'<p class="fine-print">Clarification source: '+esc(a.detail_source)+'<br>'+esc(a.detail_reason)+'</p>':"")+
      (a.source?'<a class="text-link" href="'+esc(a.source)+'" target="_blank" rel="noopener">Strategy reference ↗</a>':"")+
      '</div><div class="analysis-details">'+
      (a.payoff_available?'<div id="disclosed-payoff"></div><button class="button secondary" id="explore-disclosed">Explore these legs as a scenario ↗</button>':
        '<div class="analysis-note"><strong>Payoff chart needs more detail</strong><br>'+(a.family==="calendar"?"Options with different expirations require market inputs and a valuation model.":"The disclosure does not establish enough option details to draw a numerical payoff.")+'</div>')+
      (a.missing.length?'<h3 class="missing-title" style="margin-top:20px">Details still missing</h3><ul>'+a.missing.map(x=>'<li>'+esc(x)+'</li>').join("")+'</ul>':"")+
      '<details '+(a.payoff_available?"":"open")+'><summary>Components and assumptions</summary>'+table+'<ul>'+a.assumptions.map(x=>'<li>'+esc(x)+'</li>').join("")+'</ul><p class="fine-print">Direction describes the disclosed structure. The contributor’s wider portfolio and actual position size are unknown.</p></details></div></div>';
    if (strategies.length > 1) $("analysis-select").addEventListener("change", e=>renderAnalysis(Number(e.target.value)));
    if (item.payoff) {
      renderPayoff($("disclosed-payoff"),item.payoff,"disclosed");
      $("explore-disclosed").addEventListener("click",()=>openExplorer(item.payoff.model));
    }
  }
  window.OptionsDesk = {
    async load(symbol,name) {
      const token = ++request;
      company=name;
      $("analysis-company").textContent=name;
      $("analysis-content").innerHTML='<p class="empty">Loading strategy details…</p>';
      try {
        const data=await get("/api/analysis/"+encodeURIComponent(symbol));
        if(token!==request)return;
        strategies=data.strategies;
        renderAnalysis();
      } catch(e) {
        if(token===request)$("analysis-content").innerHTML='<p class="empty">'+esc(e.message)+'</p>';
      }
    }
  };
  function addRow(l=one("call",1,100)) {
    const tr=document.createElement("tr");
    tr.innerHTML=
      '<td><select data-field="side" aria-label="Leg action"><option value="1">Buy</option><option value="-1">Sell</option></select></td>'+
      '<td><input data-field="quantity" aria-label="Leg quantity" type="number" min=".0001" max="10000" step="any" required value="'+esc(Math.abs(l.quantity))+'" /></td>'+
      '<td><select data-field="kind" aria-label="Leg instrument"><option value="call">Call</option><option value="put">Put</option><option value="stock">Shares</option></select></td>'+
      '<td><input data-field="strike" aria-label="Leg strike" type="number" min="0" max="10000000" step="any" value="'+(l.strike==null?"":esc(l.strike))+'" /></td>'+
      '<td><input data-field="expiry" aria-label="Leg expiration" type="date" value="'+esc(l.expiry||"")+'" /></td>'+
      '<td><input data-field="multiplier" aria-label="Leg multiplier" type="number" min=".0001" max="100000" step="any" required value="'+esc(l.multiplier)+'" /></td>'+
      '<td><button type="button" class="icon-button" aria-label="Remove leg">×</button></td>';
    tr.querySelector('[data-field="side"]').value=l.quantity>0?"1":"-1";
    tr.querySelector('[data-field="kind"]').value=l.kind;
    function kindChanged(reset=false) {
      const stock=tr.querySelector('[data-field="kind"]').value==="stock";
      const strike=tr.querySelector('[data-field="strike"]'), expiry=tr.querySelector('[data-field="expiry"]'), mult=tr.querySelector('[data-field="multiplier"]');
      strike.disabled=expiry.disabled=stock;
      strike.required=!stock;
      mult.readOnly=stock;
      if(reset){mult.value=stock?1:100;if(stock){strike.value="";expiry.value="";}}
    }
    tr.querySelector('[data-field="kind"]').addEventListener("change",()=>kindChanged(true));
    tr.querySelector("button").addEventListener("click",()=>{tr.remove();markStale();});
    $("payoff-legs").appendChild(tr);
    kindChanged();
  }
  function fillModel(m) {
    $("payoff-legs").innerHTML="";
    m.legs.forEach(addRow);
    $("payoff-cost").value=m.net_cost??"";
    $("payoff-same-expiry").checked=m.same_expiry??true;
    $("payoff-min").value="";
    $("payoff-max").value="";
  }
  function openExplorer(m) {
    $("payoff-company").textContent=company+" · example values, independent of current quotations";
    $("payoff-error").classList.add("hidden");
    $("payoff-example").value=m?"custom":"vertical";
    fillModel(m||examples.vertical);
    if(!$("payoff-dialog").open)$("payoff-dialog").showModal();
    runCalculation();
  }
  function markStale() {
    calculation++;
    $("payoff-result").classList.add("stale");
    $("payoff-result").setAttribute("aria-label","Previous calculation. Calculate again to apply edited inputs.");
    $("payoff-error").classList.add("hidden");
  }
  async function runCalculation() {
    if(!$("payoff-form").reportValidity())return;
    const token=++calculation;
    const legs=[...$("payoff-legs").children].map(tr=>{
      const v=key=>tr.querySelector('[data-field="'+key+'"]').value;
      const stock=v("kind")==="stock";
      return {kind:v("kind"),quantity:Number(v("quantity"))*Number(v("side")),strike:stock?null:Number(v("strike")),
        expiry:stock?null:v("expiry")||null,multiplier:Number(v("multiplier"))};
    });
    const m={legs,same_expiry:$("payoff-same-expiry").checked,net_cost:$("payoff-cost").value===""?null:Number($("payoff-cost").value)};
    if($("payoff-min").value!=="")m.range_min=Number($("payoff-min").value);
    if($("payoff-max").value!=="")m.range_max=Number($("payoff-max").value);
    try {
      const response=await fetch("/api/payoff",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(m)});
      const result=await response.json();
      if(token!==calculation)return;
      if(!response.ok)throw new Error(result.detail||"The payoff could not be calculated.");
      $("payoff-error").classList.add("hidden");
      $("payoff-result").classList.remove("stale");
      $("payoff-result").removeAttribute("aria-label");
      renderPayoff($("payoff-result"),result,"scenario");
    } catch(e) {
      if(token!==calculation)return;
      $("payoff-error").textContent=e.message;
      $("payoff-error").classList.remove("hidden");
      $("payoff-result").innerHTML="";
    }
  }
  function renderPayoff(target,r,id) {
    const pnl=r.mode==="profit_loss",pts=r.points;
    const hasOptions=r.model.legs.some(l=>l.kind!=="stock");
    const chartTitle=hasOptions?(pnl?"Expiration profit / loss":"Expiration value"):(pnl?"Stock profit / loss":"Stock value");
    const priceTitle=hasOptions?"Underlying price at expiration":"Underlying price";
    const W=820,H=310,L=76,R=26,T=26,B=48,xmin=r.model.range_min,xmax=r.model.range_max;
    let ymin=Math.min(0,...pts.map(p=>p.value)),ymax=Math.max(0,...pts.map(p=>p.value));
    const pad=(ymax-ymin||100)*.12;ymin-=pad;ymax+=pad;
    const rough=(ymax-ymin)/4,magnitude=10**Math.floor(Math.log10(rough));
    const step=[1,2,5,10].find(n=>n*magnitude>=rough)*magnitude;
    ymin=Math.floor(ymin/step)*step;ymax=Math.ceil(ymax/step)*step;
    const x=v=>L+(v-xmin)/(xmax-xmin)*(W-L-R), y=v=>H-B-(v-ymin)/(ymax-ymin)*(H-T-B);
    const path=pts.map((p,i)=>(i?"L":"M")+x(p.price).toFixed(2)+","+y(p.value).toFixed(2)).join(" ");
    const area=path+" L"+x(xmax)+","+y(0)+" L"+x(xmin)+","+y(0)+" Z";
    const roots=r.breakevens.map(money).concat(r.breakeven_ranges.map(f=>money(f.start)+"–"+(f.to==null?"∞":money(f.to))));
    const worst=r.minimum_unbounded?(pnl?"Unlimited loss":"Unbounded below"):money(r.minimum);
    const best=r.maximum_unbounded?(pnl?"Unlimited gain":"Unbounded above"):money(r.maximum);
    const svgLine=(x1,y1,x2,y2,color,dash="")=>'<line x1="'+x1+'" y1="'+y1+'" x2="'+x2+'" y2="'+y2+'" stroke="'+color+'" stroke-dasharray="'+dash+'"/>';
    const svgText=(xx,yy,text,anchor="end")=>'<text x="'+xx+'" y="'+yy+'" text-anchor="'+anchor+'">'+esc(text)+'</text>';
    const grid=Array.from({length:Math.round((ymax-ymin)/step)+1},(_,i)=>{
      const v=ymin+step*i;
      return svgLine(L,y(v),W-R,y(v),"#e9ede3")+svgText(L-9,y(v)+4,num(v));
    }).join("");
    const ticks=Array.from({length:6},(_,i)=>{
      const v=xmin+(xmax-xmin)*i/5;
      return svgText(x(v),H-B+22,num(v),"middle");
    }).join("");
    const strikes=[...new Set(r.model.legs.filter(l=>l.kind!=="stock").map(l=>l.strike))].filter(k=>k>=xmin&&k<=xmax);
    const metric=(title,value)=>'<div class="payoff-metric"><span>'+esc(title)+'</span><strong>'+esc(value)+'</strong></div>';
    target.innerHTML=
      '<div class="payoff-heading"><h3>'+chartTitle+'</h3>'+pill(r.direction)+'</div>'+
      '<p class="fine-print">'+esc(r.explanation)+' Amounts are per entered strategy unit.</p>'+
      '<div class="payoff-metrics">'+metric(pnl?"Lowest profit / loss":"Minimum value",worst)+metric(pnl?"Highest profit / loss":"Maximum value",best)+metric("Breakeven price(s)",pnl?roots.join(", ")||"None":"Entry cost needed")+'</div>'+
      '<svg class="payoff-svg" viewBox="0 0 '+W+' '+H+'" role="img" aria-label="'+chartTitle+' across underlying prices">'+
      '<defs><clipPath id="'+id+'-positive"><rect x="'+L+'" y="'+T+'" width="'+(W-L-R)+'" height="'+(y(0)-T)+'"/></clipPath><clipPath id="'+id+'-negative"><rect x="'+L+'" y="'+y(0)+'" width="'+(W-L-R)+'" height="'+(H-B-y(0))+'"/></clipPath></defs>'+
      '<g font-family="Inter,system-ui,sans-serif" font-size="11" fill="#7e8b73">'+grid+ticks+svgText(L,15,"USD per strategy unit","start")+svgText((W+L-R)/2,H-7,priceTitle+" · USD","middle")+'</g>'+
      '<path d="'+area+'" fill="#e4f0df" clip-path="url(#'+id+'-positive)"/><path d="'+area+'" fill="#f5e5df" clip-path="url(#'+id+'-negative)"/>'+
      svgLine(L,y(0),W-R,y(0),"#8e9a80","5 4")+strikes.map(k=>svgLine(x(k),T,x(k),H-B,"#cbd3bf","3 5")).join("")+
      '<path d="'+path+'" fill="none" stroke="#426f4c" stroke-width="2.8" stroke-linejoin="round"/>'+
      '<line class="payoff-cursor" y1="'+T+'" y2="'+(H-B)+'" stroke="#56774c" stroke-dasharray="3 3"/><circle class="payoff-point" r="4.5" fill="#375a3e" stroke="white" stroke-width="2"/></svg>'+
      '<label class="payoff-slider">Explore a price<input type="range" min="'+xmin+'" max="'+xmax+'" step="'+((xmax-xmin)/1000)+'" value="'+((xmin+xmax)/2)+'" aria-label="Underlying price at expiration" /></label>'+
      '<div class="payoff-readout"><span class="payoff-price"></span><strong class="payoff-value"></strong></div>'+
      '<div class="payoff-regions">'+r.regions.map(z=>'<span>'+money(z.start)+"–"+(z.to==null?"∞":money(z.to))+": "+(z.trend==="rising"?"higher prices help":z.trend==="falling"?"higher prices hurt":"value is flat")+'</span>').join("")+'</div>'+
      '<details><summary>Calculation assumptions</summary><ul class="payoff-assumptions">'+r.assumptions.map(t=>'<li>'+esc(t)+'</li>').join("")+'</ul></details>';
    const slider=target.querySelector('input[type="range"]');
    function move() {
      const price=Number(slider.value);
      let value=-Number(r.model.net_cost||0);
      for(const l of r.model.legs)value+=l.quantity*l.multiplier*(l.kind==="stock"?price:Math.max(l.kind==="call"?price-l.strike:l.strike-price,0));
      target.querySelector(".payoff-price").textContent="Underlying price: "+money(price);
      target.querySelector(".payoff-value").textContent=(pnl?"Profit / loss: ":hasOptions?"Expiration value: ":"Stock value: ")+money(value);
      const cursor=target.querySelector(".payoff-cursor"),point=target.querySelector(".payoff-point");
      cursor.setAttribute("x1",x(price));cursor.setAttribute("x2",x(price));
      point.setAttribute("cx",x(price));point.setAttribute("cy",y(value));
    }
    slider.addEventListener("input",move);move();
  }
  $("open-payoff").addEventListener("click",()=>openExplorer());
  $("close-payoff").addEventListener("click",()=>{$("payoff-dialog").close();calculation++;});
  $("payoff-dialog").addEventListener("cancel",()=>calculation++);
  $("payoff-example").addEventListener("change",()=>{fillModel(examples[$("payoff-example").value]);runCalculation();});
  $("payoff-form").addEventListener("input",markStale);
  $("payoff-form").addEventListener("submit",e=>{e.preventDefault();runCalculation();});
  $("add-payoff-leg").addEventListener("click",()=>{if($("payoff-legs").children.length<16){addRow();markStale();}});
})();
