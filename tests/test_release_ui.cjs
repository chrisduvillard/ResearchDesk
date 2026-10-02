const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

// Browser and network boundaries only; the production request/render code runs unchanged.
function browser() {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, {
        textContent:'', innerHTML:'', value:'', checked:false, disabled:false,
        classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),
          contains:c=>classes.has(c),toggle(c,on){if(on)classes.add(c);else classes.delete(c);}},
        addEventListener(){},setAttribute(){},querySelectorAll:()=>[],add(){},
      });
    }
    return elements.get(id);
  };
  const context = vm.createContext({
    document:{getElementById:element,querySelector:element,querySelectorAll:()=>[]},
    window:{addEventListener(){},OptionsDesk:{load(){}}},
    Desk:{loadView:()=>({page:'overview',symbol:'MSFT',range:'6',horizon:'20',shade:'1',changes:'previous'}),saveView(){},activity:async()=>{},pp:()=> '—'},
    fetch:async()=>{throw Error('offline');},AbortSignal,Intl,URLSearchParams,
    setInterval(){},setTimeout(){},clearTimeout(){},Option:function(){},
  });
  return {context,element,run:code=>vm.runInContext(code,context)};
}

async function dan() {
  const b=browser();
  b.run(fs.readFileSync('tracker/static/app.js','utf8'));
  await new Promise(resolve=>setImmediate(resolve));
  return b;
}

const event = (id,symbol,name) => ({id,symbol,name,kind:'added',direction:'bullish',observed_at:'2026-10-01T12:00:00Z'});

test('Changing history filter while paginating cannot mix instruments or skip the newest results',async()=>{
  const b=await dan();
  b.context.oldEvent=event(100,'MSFT','Microsoft');
  b.context.newEvent=event(200,'TLT','Treasury ETF');
  await b.run(`(async()=>{
    state.history=[oldEvent];state.historyFilter='TLT';state.hasMore=true;
    let resolveFirst;let calls=0;
    get=async()=>{if(++calls===1)return new Promise(resolve=>resolveFirst=resolve);return {items:[],has_more:false};};
    const first=loadHistory();await loadHistory(true);
    resolveFirst({items:[newEvent],has_more:false});await first;
  })()`);
  assert.equal(b.run('JSON.stringify(state.history.map(e=>e.symbol+":"+e.id))'),'["TLT:200"]');
  assert.match(b.element('history-body').innerHTML,/Treasury ETF/);
  assert.doesNotMatch(b.element('history-body').innerHTML,/Microsoft/);
});

test('A failed history filter request removes the previous instrument rows',async()=>{
  const b=await dan();
  b.element('history-body').innerHTML='<tr><td>Microsoft</td></tr>';
  b.context.oldEvent=event(100,'MSFT','Microsoft');
  await b.run(`state.history=[oldEvent];state.historyFilter='TLT';get=async()=>{throw Error('offline')};loadHistory()`);
  assert.equal(b.element('history-body').innerHTML,'');
  assert.equal(b.run('state.history.length'),0);
  assert.equal(b.element('load-more').classList.contains('hidden'),true);
});

test('A failed score filter request cannot leave another instrument’s returns visible',async()=>{
  const b=await dan();
  for(const id of ['score-cards','score-summary','signals-list'])b.element(id).innerHTML='Microsoft +25%';
  await b.run(`state.scores={old:true};state.scoreFilter='TLT';get=async()=>{throw Error('offline')};loadScores()`);
  for(const id of ['score-cards','score-summary','signals-list'])assert.equal(b.element(id).innerHTML,'');
  assert.equal(b.run('state.scores'),null);
});

test('Dan polling retries interrupted prices without requiring a new collection run',async()=>{
  const b=await dan();
  b.run(`state.chart={timeScale:()=>({setVisibleRange(){}})};
    state.candles={setData(){}};state.shade={setData(){},applyOptions(){}};
    state.markers={setMarkers(){}};`);
  let offline=true;
  b.context.fetch=async url=>{
    let data;
    if(url==='/api/status')data={last_run:{finished_at:'2026-10-01T12:00:00Z'},event_count:0,worker_healthy:true};
    else if(url==='/api/positions'||url==='/api/timeline/MSFT')data=[];
    else if(url==='/api/instruments')data=[{symbol:'MSFT',name:'Microsoft',asset_class:'Equity'}];
    else if(url==='/api/prices/MSFT'){
      if(offline)throw Error('temporary price outage');
      data={bars:[{time:'2026-10-01',open:100,high:102,low:99,close:101}]};
    }else throw Error('Unexpected URL '+url);
    return {ok:true,json:async()=>data};
  };
  await b.run('refresh()');
  assert.match(b.element('chart-message').textContent,/temporary price outage/);
  offline=false;
  await b.run('refresh()');
  assert.equal(b.run('state.bars[0]?.close'),101);
  assert.equal(b.element('chart-message').classList.contains('hidden'),true);
});

function dbmf(market='us2y') {
  const b=browser();
  const item={id:'us2y',name:'US 2-year Treasury',category:'Bonds',provider_symbol:'ZT=F',price_kind:'futures',price_reference:'Treasury futures',net_pct:10,long_pct:10,short_pct:0,gross_pct:10,comparison_pct:null,change_pp:null,holdings:[]};
  const report={id:1,source_date:'2026-10-01',net_assets:1000,summary:{long_pct:10,short_pct:0,collateral_pct:0},markets:[item],metadata:{completeness:'complete'},revision:1,collected_at:'2026-10-01T12:00:00Z',source_kind:'live',source_url:'https://example.com/report',parser_version:'1'};
  const status={server_time:'2026-10-02T12:00:00Z',last_run:{id:1,status:'success',price_errors:{}},latest_report_id:1,catalog_version:'1',current:{id:1},worker_healthy:true,review:{items:[]},coverage:{inception:'2019-05-07',observations:1,historical_reports:0,sec_access:[],note:''},historical_errors:[]};
  const history={markets:[item],observations:[{id:1,date:'2026-10-01',source_kind:'live',exposures:{us2y:{net_pct:10}}}]};
  const prices={market:item,bars:[{time:'2026-10-01',open:100,high:102,low:99,close:101}],latest_price_date:'2026-10-01'};
  b.context.Desk.loadView=()=>({market,range:'all',compare:'previous',former:'0',changes:'previous'});
  const series=()=>({data:[],setData(data){this.data=data;},applyOptions(){},createPriceLine(){}});
  b.context.window.LightweightCharts={CrosshairMode:{Normal:0},createSeriesMarkers:()=>({setMarkers(){}}),createChart:()=>({addSeries:series,panes:()=>[{setStretchFactor(){}},{setStretchFactor(){}}],subscribeCrosshairMove(){},subscribeClick(){},timeScale:()=>({setVisibleRange(){}})})};
  b.respond=async url=>{
    if(url.endsWith('/status'))return status;
    if(url.endsWith('/history'))return history;
    if(url.includes('/exposures?'))return {current:report,comparison:null};
    if(url.endsWith('/reports/1'))return report;
    if(url.endsWith('/prices/us2y'))return prices;
    if(url.endsWith('/revisions'))return {items:[],has_more:false};
    throw Error('Unexpected URL '+url);
  };
  b.context.fetch=async url=>({ok:true,json:()=>b.respond(url)});
  // Expose closure functions in the test only and keep startup under test control.
  b.run(fs.readFileSync('tracker/static/dbmf.js','utf8').replace(
    'refresh(true).catch(fail);setInterval(()=>refresh().catch(fail),60000);',
    'window.review={state,refresh};'));
  b.app=b.context.window.review;
  return b;
}

test('An unknown DBMF bookmark falls back to an available market before rendering evidence',async()=>{
  const b=dbmf('unknown-market');
  await b.app.refresh(true);
  assert.equal(b.app.state.market,'us2y');
  assert.match(b.element('evidence-content').innerHTML,/US 2-year Treasury/);
  assert.equal(b.app.state.candles.data[0].close,101);
});

test('DBMF polling retries a failed detail load even when the server report is unchanged',async()=>{
  const b=dbmf(),respond=b.respond;
  let offline=true;
  b.respond=async url=>{if(offline&&url.includes('/prices/'))throw Error('temporary outage');return respond(url);};
  await assert.rejects(b.app.refresh(true),/temporary outage/);
  offline=false;
  await b.app.refresh();
  assert.equal(b.app.state.prices?.bars[0].close,101);
  assert.match(b.element('price-label').textContent,/Treasury futures/);
});
