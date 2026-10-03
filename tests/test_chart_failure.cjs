const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');

test('A failed instrument switch cannot display the previous instrument’s prices', async () => {
  const elements = new Map();
  const element = id => {
    if (!elements.has(id)) elements.set(id, {
      textContent: '', value: '', checked: false,
      classList: {add() {}, remove() {}, toggle() {}},
      addEventListener() {}, querySelectorAll() { return []; },
    });
    return elements.get(id);
  };
  const view = {page:'overview',symbol:'MSFT',range:'6',horizon:'20',shade:'1',changes:'previous'};
  const context = vm.createContext({
    DeskContext:{scope:"dan",url:u=>u,apply(){},reportId:v=>Number(v)||null,legacyFund:true,api:"/api/dbmf"},
    document: {getElementById:element, querySelector:element, querySelectorAll:() => []},
    window: {OptionsDesk:{load() {}}, addEventListener() {}},
    Desk: {loadView:() => view, saveView() {}},
    fetch:async () => { throw new Error('Test provider unavailable'); },
    AbortSignal, Intl, URLSearchParams, setInterval() {}, setTimeout() {}, clearTimeout() {},
  });
  vm.runInContext(fs.readFileSync('tracker/static/app.js','utf8'),context);
  await vm.runInContext(`(async () => {
    const series = () => ({data:[{time:'2026-10-01',close:500}],setData(value){this.data=value;}});
    state.instruments = [{symbol:'TLT',name:'Treasury bond ETF',asset_class:'Bonds'}];
    state.chartSymbol='MSFT';state.selected='MSFT';
    state.candles=series();state.markers=series();state.shade=series();
    state.markers.setMarkers=state.markers.setData;
    state.bars=[{time:'2026-10-01',close:500}];state.timeline=[{symbol:'MSFT'}];
    await selectInstrument('TLT');
  })()`,context);
  for (const field of ['candles.data','markers.data','shade.data','bars','timeline']) {
    assert.equal(vm.runInContext(`state.${field}.length`,context),0);
  }
  assert.equal(element('chart-title').textContent,'Treasury bond ETF');
  assert.equal(element('chart-message').textContent,'Test provider unavailable');
});
