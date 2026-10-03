const test = require('node:test');
const assert = require('node:assert/strict');
const desk = require('../tracker/static/desk-state.js');

test('DBMF bookmark round trip includes market, range, exact comparison and evidence revision', () => {
  const view = desk.normalize('dbmf', {market:'jpy',range:'3',compare:'date',date:'2026-09-15',compare_report:'12',report:'7',revision:'14',former:'1',changes:'visit'});
  assert.deepEqual(desk.fromURL('dbmf', new URL(desk.viewURL('dbmf', view), 'http://localhost:8765'), {}), view);
});
test('An explicit bookmark takes precedence over remembered values', () => {
  const view = desk.fromURL('dbmf', new URL('http://localhost:8765/dbmf?market=gold'), {market:'jpy',range:'3',compare:'month'});
  assert.equal(view.market,'gold'); assert.equal(view.range,'all'); assert.equal(view.compare,'previous');
});
test('Plain navigation restores the previous view, including Dan history filters', () => {
  const saved = {page:'history',symbol:'TLT',range:'12',history:'MSFT',horizon:'60',shade:'0'};
  assert.equal(desk.fromURL('dan', new URL('http://localhost:8765/'), saved).history,'MSFT');
  assert.equal(desk.fromURL('dan', new URL('http://localhost:8765/#scorecard'), saved).page,'scorecard');
});
test('Corrupt or obsolete state falls back safely and invalid calendar dates are rejected', () => {
  const view=desk.normalize('dbmf',{market:'<script>',range:'nan',date:'2026-02-31',compare:'date',compare_report:'9007199254740993',report:'-1'});
  assert.equal(view.market,'us2y'); assert.equal(view.range,'all'); assert.equal(view.date,''); assert.equal(view.compare,'previous'); assert.equal(view.report,'');
  assert.equal(desk.day('2024-02-29'),true); assert.equal(desk.day('2025-02-29'),false);
});
test('Warnings notify only after escalation, then once until confirmed recovery', () => {
  const warning={key:'dbmf:collection',title:'Collection failed',link:'/dbmf',notify:false};
  assert.equal(desk.transitions({},[warning]).opened.length,0);
  const first=desk.transitions({},[{...warning,notify:true}]); assert.equal(first.opened.length,1);
  assert.equal(desk.transitions(first.active,[{...warning,notify:true}]).opened.length,0);
  // A running retry with a lower warning level is not proof of recovery.
  const waiting=desk.transitions(first.active,[warning]); assert.equal(waiting.resolved.length,0);
  assert.equal(desk.transitions(waiting.active,[{...warning,notify:true}]).opened.length,0);
  const recovered=desk.transitions(waiting.active,[]); assert.equal(recovered.resolved.length,1);
  assert.equal(desk.transitions(recovered.active,[]).resolved.length,0);
  assert.equal(desk.transitions(recovered.active,[{...warning,notify:true}]).opened.length,1);
});
test('Independent incidents and browser reloads keep their own notification lifecycle', () => {
  const review={key:'dbmf:review',title:'Unfamiliar instrument',notify:true};
  const prices={key:'dan:prices',title:'Prices old',notify:true};
  const first=desk.transitions({},[review]);
  const reloaded=JSON.parse(JSON.stringify(first.active));
  const next=desk.transitions(reloaded,[review,prices]);
  assert.deepEqual(next.opened.map(x=>x.key),['dan:prices']);
  assert.deepEqual(desk.transitions(next.active,[prices]).resolved.map(x=>x.key),['dbmf:review']);
});

test('Dan bookmarks stay on contributor route after Today becomes the landing page', () => {
  assert.ok(desk.viewURL('dan', {page: 'history'}).startsWith('/dan?'));
});

test('Fund state preserves opaque report IDs and asset identities without leaking across funds', () => {
  const scope='fund:ARKK', report='e48c9f1abcdef';
  const view=desk.normalize(scope,{market:'cusip:594918104',report,compare:'date',compare_report:report});
  assert.equal(view.report,report);
  assert.equal(view.market,'cusip:594918104');
  const url=desk.viewURL(scope,view);
  assert.ok(url.startsWith('/funds/ARKK?'));
  assert.deepEqual(desk.fromURL(scope,new URL(url,'http://localhost'),{}),view);
  assert.ok(desk.viewURL('contributor:karen-finerman',{page:'history'}).startsWith('/contributors/karen-finerman?'));
});
