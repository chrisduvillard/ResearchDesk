import csv
import gzip
import io
import json
import sqlite3
import tarfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tracker import db
from tracker.api import app
from tracker.collector import backup
from tracker.dbmf import parser, store, prices, collector
from tracker.dbmf.markets import identify

FIXTURES = Path(__file__).parent / 'fixtures' / 'dbmf'
NOW = datetime.fromisoformat('2026-10-01T22:30:00-04:00')


def live():
    return (FIXTURES / 'live.html').read_text()


def changed(*, day=None, remove=None, change=None, append=None):
    soup = BeautifulSoup(live(), 'html.parser')
    if day:
        for cell in soup.select('tbody .value_date'):
            cell.string = day
    if remove:
        for row in soup.select('tbody tr'):
            if remove in row.select_one('.security_name').text:
                row.decompose()
    if change:
        selector, value = change
        soup.select_one(selector).string = value
    if append:
        body = soup.select_one('tbody')
        body.insert(len(body.contents)-2, BeautifulSoup(append, 'html.parser'))
    return str(soup)


def import_history(conn, day):
    text=(FIXTURES / (day+'.txt')).read_text()
    return store.ingest(conn, text.encode(), NOW, kind='historical', source_url='https://www.imgp.com/'+day+'.pdf', extracted_text=text)


def test_live_every_row_reconciles_and_keeps_precise_denominator():
    report=parser.parse_live(live(),NOW)
    assert report['net_assets']=='5243820755.56'
    assert len(report['holdings'])==20
    first=report['holdings'][0]
    assert first['market_id']=='us2y'
    assert first['notional']=='-5284498755.18'
    assert Decimal(first['exposure_pct'])==Decimal('-100.77573207621311801252074908364948117170269115040')
    for row in report['holdings']:
        assert abs(Decimal(row['exposure_pct'])/100-Decimal(row['weight']))<=Decimal('.005')
    assert len([h for h in report['holdings'] if h['market_id']=='tbills'])==10


@pytest.mark.parametrize('text',[
    lambda:live().replace('</table>',''),
    lambda:live().replace('TOTAL NET ASSETS','NET ASSETS UNKNOWN'),
    lambda:live().replace('US 2YR NOTE (CBT) DEC26','UNRECOGNIZED FUTURE'),
    lambda:changed(change=('tbody .weight','0.01')),
    lambda:changed(change=('tbody .value_date','10/02/2026')),
    lambda:live().replace('<td class="weight">-1.01</td>',''),
    lambda:live().replace('iMGP DBi Managed Futures Strategy ETF - DBMF','Different Fund'),
    lambda:changed(change=('tbody .market_value','NaN')),
    lambda:changed(day='10/02/2026'),
])
def test_malformed_reports_retain_current_and_archive_rejection(database,text):
    first=store.ingest(database,live(),NOW)
    bad=store.ingest(database,text(),NOW)
    assert bad['status']=='rejected'
    assert (db.DATA_DIR/bad['raw_path']).exists()
    assert store.reports(database)[-1]['id']==first['id']
    assert database.execute('SELECT count(*) FROM dbmf_holdings WHERE report_id=?',(bad['id'],)).fetchone()[0]==0


def test_repeats_reordering_and_same_date_revisions(database):
    first=store.ingest(database,live(),NOW)
    repeated=store.ingest(database,'<!--new page decoration-->'+live(),NOW+timedelta(hours=1))
    assert repeated['duplicate'] and repeated['id']==first['id']
    assert repeated['raw_path']!=first['raw_path']
    soup=BeautifulSoup(live(),'html.parser');rows=soup.select('tbody tr');rows[0].insert_before(rows[1].extract())
    assert store.ingest(database,str(soup),NOW)['duplicate']
    revised=store.ingest(database,changed(change=('tbody .market_value','-5284498755.19')),NOW+timedelta(hours=2))
    assert revised['id']!=first['id']
    assert database.execute('SELECT revision FROM dbmf_reports WHERE id=?',(revised['id'],)).fetchone()[0]==2
    # A true reversion after a revision remains an auditable third revision.
    reverted=store.ingest(database,live(),NOW+timedelta(hours=3))
    assert not reverted['duplicate']
    assert database.execute('SELECT revision FROM dbmf_reports WHERE id=?',(reverted['id'],)).fetchone()[0]==3
    assert len(store.reports(database))==1


def test_opposing_expiries_and_collateral_are_aggregated_before_netting(database):
    soup=BeautifulSoup(live(),'html.parser')
    row=BeautifulSoup(str(soup.select('tbody tr')[0]),'html.parser').tr
    row.select_one('.security_name').string='US 2YR NOTE (CBT) MAR27'
    row.select_one('.isin').string='DISTINCT1';row.select_one('.ticker').string='TUH7'
    row.select_one('.market_value').string='5243820755.56';row.select_one('.shares_qty').string='5200000000'
    row.select_one('.weight').string='1.00'
    soup.select('tbody tr')[-1].insert_before(row)
    store.ingest(database,str(soup),NOW)
    result=store.snapshot(database,store.reports(database)[-1]);m={r['id']:r for r in result['markets']}
    assert float(m['us2y']['net_pct'])==pytest.approx(-.775732076213)
    assert float(m['us2y']['gross_pct'])==pytest.approx(200.775732076213)
    assert m['us2y']['holding_count']==2
    assert m['tbills']['holding_count']==10
    assert all(m[key]['holding_count']==1 for key in ('us10y','uslong'))
    assert float(result['summary']['gross_pct'])<sum(float(r['gross_pct']) for r in result['markets'])


def test_absent_market_zero_only_after_complete_replacement(database):
    store.ingest(database,live(),NOW)
    incomplete=changed(remove='GOLD').replace('</tbody>','')
    assert store.ingest(database,incomplete,NOW)['status']=='rejected'
    assert next(m for m in store.aggregate(database,store.reports(database)[-1]) if m['id']=='gold')['holding_count']==1
    store.ingest(database,changed(day='10/02/2026',remove='GOLD'),NOW+timedelta(days=1))
    gold=next(m for m in store.exposure_response(database)['current']['markets'] if m['id']=='gold')
    assert gold['net_pct']=='0' and gold['absent']
    assert float(gold['change_pp'])<0


@pytest.mark.parametrize('day,assets,rows',[
    ('2021-12-31','60379415',25),('2022-03-31','108356106',14),('2022-06-30','332973092',14),
    ('2022-12-31','951319050',14),('2023-06-30','707774458',15),('2023-12-31','684736610',15),
    ('2024-03-31','937615416',12),('2024-12-31','1256976631',12),
    ('2025-09-30','1520386907',17),('2026-03-31','3308615561',16),
    ('2025-12-31','2098103649',12),('2026-06-30','3887474179',18),
    ('2022-09-30','920594178',14),('2023-03-31','748914300',14),
    ('2024-09-30','984233941',15),('2025-03-31','1210874821',12),
    ('2023-09-30','948676275',15),('2024-06-30','1067071298',12),
])
def test_official_historical_layouts_use_current_notional(day,assets,rows):
    result=parser.parse_historical((FIXTURES/(day+'.txt')).read_text(),NOW)
    assert result['source_date']==day and result['net_assets']==assets and len(result['holdings'])==rows
    if day=='2025-09-30':
        gold=next(r for r in result['holdings'] if r['market_id']=='gold')
        assert gold['notional']=='307919400'  # Not original 278607353 or gain 29312047.
        assert sum(Decimal(r['notional']) for r in result['holdings'] if r['market_id']=='tbills')==1070795384
        assert next(r for r in result['holdings'] if r['market_id']=='repo')['notional']=='22772717'


def test_historical_incomplete_wrong_fund_unknown_and_wrong_columns_rejected():
    text=(FIXTURES/'2025-09-30.txt').read_text()
    samples=[text.replace('307,919,400','278,607,353'),text.replace('Gold 100 Oz Futures','Unknown Future'),
             text.replace('Total Long','MISSING TOTAL'),text.replace('iMGP DBi Managed Futures Strategy ETF','Other Fund'),
             '\n'.join(line for line in text.splitlines() if 'Euro FX Currency Futures' not in line),
             text.replace('1,520,386,907','1,520,386,900')]
    for sample in samples:
        with pytest.raises(ValueError):parser.parse_historical(sample,NOW)


def test_out_of_order_imports_comparison_dates_and_csv_revisions(database):
    store.ingest(database,live(),NOW)
    import_history(database,'2026-03-31');import_history(database,'2025-09-30')
    assert store.reports(database)[-1]['source_date']=='2026-10-01'
    result=store.exposure_response(database,'week')
    assert result['requested_date']=='2026-09-24' and result['comparison_date']=='2026-03-31'
    assert store.exposure_response(database,'month')['requested_date']=='2026-09-01'
    assert store.exposure_response(database,'date','2020-01-01')['comparison_missing']
    history=store.history_response(database)
    assert [r['date'] for r in history['observations']]==['2025-09-30','2026-03-31','2026-10-01']
    assert history['interpolation'] is False
    assert len(store.history_response(database,'2026-04-01')['observations'])==1
    store.ingest(database,changed(change=('tbody .market_value','-5284498755.19')),NOW)
    rows=list(csv.DictReader(io.StringIO(store.csv_export(database))))
    assert {r['revision'] for r in rows if r['reporting_date']=='2026-10-01'}=={'1','2'}


@pytest.mark.parametrize('name,expected',[
    ('US 2YR NOTE (CBT) DEC26','us2y'),('US 10YR NOTE (CBT)DEC26','us10y'),('US LONG BOND(CBT) DEC26','uslong'),
    ('U.S. Treasury 10-Year Ultra Note Futures','us10ultra'),('U.S. Treasury Ultra-Long Bond Futures','usultra'),
    ('CASH','cash'),('Fixed Income Clearing Corp.','repo'),('TREASURY BILL','tbills')])
def test_instrument_names_and_maturities(name,expected):
    assert identify(name)==expected


def test_schedules_dst_both_slots_startup_and_retries(database):
    before=datetime.fromisoformat('2026-10-01T09:59:00-04:00')
    due,upcoming=collector.schedule(before)
    assert due.isoformat()=='2026-09-30T22:30:00-04:00' and upcoming.hour==10
    now=before+timedelta(minutes=1)
    assert collector.is_due(database,now)
    def run(status,at):
        database.execute('INSERT INTO dbmf_runs(started_at,finished_at,slot,status) VALUES(?,?,?,?)',(db.iso(at),db.iso(at),db.iso(collector.schedule(at)[0]),status));database.commit()
    run('error',now)
    assert not collector.is_due(database,now+timedelta(minutes=4))
    assert collector.is_due(database,now+timedelta(minutes=5))
    run('partial',now+timedelta(minutes=5))
    assert not collector.is_due(database,now+timedelta(minutes=34))
    run('error',now+timedelta(minutes=35))
    assert not collector.is_due(database,now+timedelta(hours=3))
    evening=now.replace(hour=22,minute=30)
    assert collector.is_due(database,evening)
    run('running',evening)
    assert collector.is_due(database,evening+timedelta(minutes=2))
    run('success',evening+timedelta(minutes=3))
    assert not collector.is_due(database,evening+timedelta(hours=1))
    winter=datetime.fromisoformat('2026-11-01T10:00:00-05:00')
    assert db.iso(collector.schedule(winter)[0])=='2026-11-01T15:00:00+00:00'
    spring=datetime.fromisoformat('2026-03-08T10:00:00-04:00')
    assert db.iso(collector.schedule(spring)[0])=='2026-03-08T14:00:00+00:00'


def test_completion_rules_equities_futures_and_currencies():
    at=lambda stamp:datetime.fromisoformat(stamp)
    assert not prices.completed('2026-10-01','ETF proxy',at('2026-10-01T15:59:00-04:00'))
    assert prices.completed('2026-10-01','ETF proxy',at('2026-10-01T16:00:00-04:00'))
    assert not prices.completed('2026-10-01','futures',at('2026-10-01T17:59:00-04:00'))
    assert prices.completed('2026-10-01','futures',at('2026-10-01T18:00:00-04:00'))
    assert not prices.completed('2026-10-01','currency',at('2026-10-02T00:59:00+00:00'))
    assert prices.completed('2026-10-01','currency',at('2026-10-02T01:00:00+00:00'))
    assert prices.completed('2025-11-28','ETF proxy',at('2025-11-28T13:00:00-05:00'))
    assert not prices.completed('2025-11-27','ETF proxy',at('2025-11-28T13:00:00-05:00'))


def test_yen_inversion_high_low_and_negative_futures():
    assert prices.transform([150,152,149,151],True)==pytest.approx([1/150,1/149,1/152,1/151])
    assert prices.transform([-10,-5,-40,-37])==(-10,-5,-40,-37)
    with pytest.raises(ValueError):prices.transform([0,1,0,1],True)


def test_price_failures_and_missing_bars_preserve_cache(database,monkeypatch):
    database.execute("UPDATE dbmf_markets SET provider_symbol=NULL WHERE id!='wti'");database.commit()
    frame=pd.DataFrame({'Open':[-10.,5.],'High':[-5.,7.],'Low':[-40.,4.],'Close':[-37.,6.],'Volume':[10.,20.]},index=pd.to_datetime(['2020-04-20','2020-04-21']))
    class Ticker:
        def history(self,**kwargs):
            assert kwargs['auto_adjust'] is False
            return frame
    monkeypatch.setattr(prices.yf,'Ticker',lambda _:Ticker())
    assert prices.refresh(database,NOW)=={}
    assert database.execute("SELECT close FROM dbmf_prices WHERE date='2020-04-20'").fetchone()[0]==-37
    frame=frame.iloc[-1:]
    assert 'wti' in prices.refresh(database,NOW)
    assert database.execute('SELECT count(*) FROM dbmf_prices').fetchone()[0]==2
    frame=frame.iloc[0:0]
    assert 'wti' in prices.refresh(database,NOW)
    assert database.execute('SELECT count(*) FROM dbmf_prices').fetchone()[0]==2


def test_invalid_provider_bars_are_visible_gaps_and_not_fabricated(database,monkeypatch):
    database.execute("UPDATE dbmf_markets SET provider_symbol=NULL WHERE id!='jpy'");database.commit()
    frame=pd.DataFrame({'Open':[150.,150.],'High':[152.,151.],'Low':[149.,149.],'Close':[151.,152.]},index=pd.to_datetime(['2026-09-29','2026-09-30']))
    class Ticker:
        def history(self,**kwargs):return frame
    monkeypatch.setattr(prices.yf,'Ticker',lambda _:Ticker())
    assert prices.refresh(database,NOW)=={}
    row=database.execute("SELECT * FROM dbmf_prices WHERE market_id='jpy'").fetchone()
    assert row['date']=='2026-09-29' and row['close']==pytest.approx(1/151)
    assert row['high']==pytest.approx(1/149) and row['low']==pytest.approx(1/152)
    gap, = db.setting(database,'dbmf_price_gaps_jpy')
    assert gap['date']=='2026-09-30' and gap['reason']=='Invalid OHLC bar'
    assert gap['issues']==['Close is outside the low–high range']
    assert gap['raw_ohlc']=={'open':'150.0','high':'151.0','low':'149.0','close':'152.0'}
    assert gap['symbol']=='JPY=X' and gap['checked_at']==db.iso(NOW)
    assert gap['validator_version']==prices.VALIDATOR_VERSION
    with TestClient(app) as client:
        data=client.get('/api/dbmf/prices/jpy').json()
    assert data['omitted_bars']==[gap]
    assert 'before any currency inversion' in data['quality_note']


def test_catalog_date_mismatch_is_archived_and_rejected(database):
    text=(FIXTURES/'2026-06-30.txt').read_text()
    result=store.ingest(database,text.encode(),NOW,kind='historical',extracted_text=text,expected_date='2025-06-30')
    assert result['status']=='rejected' and 'Catalog date' in result['error']
    assert not store.reports(database)


@pytest.mark.parametrize('doctype', ['NCSR', 'NCSRS'])
def test_public_document_viewer_accepts_alphanumeric_cusip(monkeypatch, doctype):
    from tracker.dbmf import backfill
    calls = []
    root = 'https://connect.rightprospectus.com'
    def download(url):
        calls.append(url)
        if url == root + '/assets/version.txt':
            return b'"public-version"'
        if url == root + '/assets/index-public-version.js':
            return b'VITE_ARC_DIGITAL_TSR_API_KEY:`public123`'
        assert url == ('https://services.dfinsolutions.com/documentservice/documents/cusip/'
                       '53700T678/doctype/' + doctype + '?subscription-key=public123')
        return b'%PDF verified issuer document'
    monkeypatch.setattr(backfill, 'download', download)
    assert backfill.download_report(root + '/iMGP/TVT/53700T678/' + doctype + '?site=iMGP_Funds').startswith(b'%PDF')
    assert len(calls) == 3
    with pytest.raises(ValueError, match='Unrecognized'):
        backfill.download_report(root + '/iMGP/TVT/invalid/' + doctype + '?site=iMGP_Funds')


def test_api_read_only_history_gaps_evidence_errors_and_backup(database,tmp_path):
    accepted=store.ingest(database,live(),NOW);import_history(database,'2025-09-30')
    original=database.total_changes
    with TestClient(app) as client:
        status = client.get('/api/dbmf/status').json()
        assert status['latest_report_id'] == 2 and status['current']['id'] == accepted['id']
        for url in ['/dbmf','/api/dbmf/status','/api/dbmf/exposures','/api/dbmf/history','/api/dbmf/prices/jpy','/api/dbmf/reports','/api/dbmf/reports/1','/api/dbmf/export/history.csv']:
            assert client.get(url).status_code==200,url
        source=client.get('/api/dbmf/reports/1/source')
        assert source.text==live() and source.headers['content-type'].startswith('text/plain')
        assert client.get('/api/dbmf/exposures?compare=date').status_code==400
        assert client.get('/api/dbmf/exposures?compare_date=not-a-date').status_code==422
        assert client.get('/api/dbmf/exposures?report_id=99999').status_code==404
        assert client.get('/api/dbmf/prices/unknown').status_code==404
        assert client.get('/api/dbmf/reports/99999/source').status_code==404
        assert client.post('/api/dbmf/exposures',json={}).status_code==405
    assert database.total_changes==original
    assert len(store.reports(database))==2
    path=backup(database,NOW);restore=tmp_path/'restore'
    with tarfile.open(path) as tar:tar.extractall(restore,filter='data')
    with sqlite3.connect(restore/'tracker.sqlite3') as conn:
        assert conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert conn.execute('PRAGMA user_version').fetchone()[0]==db.SCHEMA_VERSION
        assert conn.execute('SELECT count(*) FROM dbmf_holdings').fetchone()[0]==37
        for row in conn.execute('SELECT raw_path FROM dbmf_reports'):
            assert gzip.decompress((restore/row[0]).read_bytes())


def test_collector_repeated_runs_record_attempt_and_preserve_snapshot(database,monkeypatch):
    monkeypatch.setattr(collector,'download',lambda _:live().encode())
    monkeypatch.setattr(collector,'refresh',lambda _: {'gold':'Provider unavailable'})
    result=collector.collect()
    assert result['status']=='partial'
    again=collector.collect()
    assert again['report']['duplicate']
    assert database.execute('SELECT count(*) FROM dbmf_runs').fetchone()[0]==2
    assert database.execute("SELECT count(*) FROM dbmf_reports WHERE status='accepted'").fetchone()[0]==1
    monkeypatch.setattr(collector,'download',lambda _:b'<html>broken</html>')
    assert collector.collect()['status']=='error'
    assert store.reports(database)[-1]['id']==result['report']['id']
