import csv
import io
import json
import sqlite3
import tarfile
from datetime import timedelta
from pathlib import Path
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from tracker import db
from tracker.api import app
from tracker.collector import backup, is_due
from tracker.export import pine_export, csv_export
from tracker.history import ingest, add_event, current_positions
from tracker.prices import refresh
from conftest import page, moment


def test_pine_export_has_correct_name_mapping_and_observation_date(database):
    ingest(database,page(),moment(),resolve=False)
    result=pine_export(database,'TLT',generated='2026-10-01T12:00:00+00:00')
    lines=result.splitlines()
    header=lines[0].split('|')
    assert header[0]=='DN2'
    assert header[1]=='NASDAQ:TLT' and header[2]=='iShares 20+ Year Treasury Bond ETF'
    assert lines[1].split('|')[0]=='20260930'
    assert 'baseline|bullish' in lines[1]
    assert header[5:7]==['1','1']


def test_export_limits_and_full_csv_history(database):
    ingest(database,page('Dan Nathan is long MSFT.'),moment(),resolve=False)
    positions=current_positions(database)
    for i in range(1,500):
        add_event(database,1,'MSFT',positions,positions,moment()+timedelta(days=i),'2026-09-30T20:30:00+00:00','strategy_changed')
    database.commit()
    text=pine_export(database,'MSFT')
    head=text.splitlines()[0].split('|')
    assert len(text)<=40960 and int(head[6])<=400 and int(head[5])==500
    assert int(head[6])<int(head[5])
    assert len(list(csv.reader(io.StringIO(csv_export(database)))))==501


def test_api_name_first_evidence_and_exports(database):
    ingest(database,page(),moment(),resolve=False)
    with TestClient(app) as client:
        assert client.get('/health').json()['status']=='ok'
        positions=client.get('/api/positions').json()
        assert {p['name'] for p in positions}=={'Roundhill Memory ETF','Taboola','The Metals Royalty Company','Microsoft','iShares 20+ Year Treasury Bond ETF'}
        events=client.get('/api/events?limit=2').json()
        assert events['has_more'] and len(events['items'])==2
        assert len(client.get('/api/events?symbol=MSFT').json()['items'])==1
        assert client.get('/api/export/pine/TLT').status_code==200
        assert client.get('/api/export/pine/BOGUS').status_code==400
        assert client.get('/api/prices/BOGUS').status_code==404
        assert client.get('/api/scorecard').json()['signals']==[]
        assert 'text/plain' in client.get('/api/snapshots/1/source').headers['content-type']
        assert client.get('/api/snapshots/1/source').text==page()
        assert client.get('/api/snapshots/999').status_code==404
        assert client.get('/downloads/dan-nathan.pine').text.startswith('//@version=6')
        assert 'default-src' in client.get('/').headers['content-security-policy']


def test_backup_restores_complete_database_and_source(database, tmp_path):
    ingest(database,page(),moment(),resolve=False)
    archive=backup(database,moment())
    restore=tmp_path/'restore-check'
    restore.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(restore,filter='data')
    with sqlite3.connect(restore/'tracker.sqlite3') as restored:
        assert restored.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert restored.execute('SELECT count(*) FROM events').fetchone()[0]==5
        raw=restored.execute('SELECT raw_path FROM snapshots').fetchone()[0]
        assert (restore/raw).exists()


def test_schedule_catches_up_and_limits_retries(database):
    now=moment('2026-10-01T22:16:00-04:00')
    assert is_due(database,now)
    stamp=db.iso(now)
    database.execute("INSERT INTO runs(started_at,finished_at,status) VALUES(?,?,'error')",(stamp,stamp));database.commit()
    assert not is_due(database,now+timedelta(minutes=4))
    assert is_due(database,now+timedelta(minutes=5))
    stamp=db.iso(now+timedelta(minutes=5))
    database.execute("INSERT INTO runs(started_at,finished_at,status) VALUES(?,?,'success')",(stamp,stamp));database.commit()
    assert not is_due(database,now+timedelta(hours=2))
    assert is_due(database,now+timedelta(days=1))


def test_price_refresh_is_atomic_and_rejects_truncated_history(database,monkeypatch):
    ingest(database,page('Dan Nathan is long MSFT.'),moment(),resolve=False)
    full=pd.DataFrame({'Open':[100.,105.],'High':[107.,112.],'Low':[99.,103.],'Close':[105.,110.],'Volume':[10.,11.],'Dividends':[0.,0.],'Stock Splits':[0.,0.]},index=pd.to_datetime(['2026-09-29','2026-09-30']).tz_localize('America/New_York'))
    class FakeTicker:
        def history(self,**kwargs):
            assert kwargs['auto_adjust'] is True
            return full
    monkeypatch.setattr('tracker.prices.yf.Ticker',lambda _:FakeTicker())
    assert refresh(database,now=moment('2026-10-01T10:00:00-04:00'))=={}
    assert database.execute("SELECT count(*) FROM prices WHERE symbol='MSFT'").fetchone()[0]==2
    original=full.copy()
    full=full.iloc[-1:]
    assert 'MSFT' in refresh(database,now=moment('2026-10-01T10:00:00-04:00'))
    assert database.execute("SELECT count(*) FROM prices WHERE symbol='MSFT'").fetchone()[0]==2
    # A corporate-action adjustment refreshes the entire cached interval.
    full=original.copy()
    for col in ['Open','High','Low','Close']:
        full[col]=full[col]/2
    assert refresh(database,now=moment('2026-10-01T10:00:00-04:00'))=={}
    assert database.execute("SELECT open FROM prices ORDER BY date LIMIT 1").fetchone()[0]==50


def test_api_read_operations_do_not_change_event_history(database):
    ingest(database,page(),moment(),resolve=False)
    with TestClient(app) as client:
        for route in ['/api/status','/api/positions','/api/instruments','/api/events','/api/timeline/MSFT','/api/prices/MSFT','/api/scorecard','/api/export/history.csv']:
            assert client.get(route).status_code==200
    assert database.execute('SELECT count(*) FROM events').fetchone()[0]==5
