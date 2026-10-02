import pytest
from tracker.calendar import entry_session, chart_date, horizon_date, schedule
from tracker.history import ingest, review
from tracker.scores import calculate
from conftest import page, moment


@pytest.mark.parametrize('observed,entry',[
 ('2026-09-30T08:00:00-04:00','2026-09-30'),
 ('2026-09-30T09:30:00-04:00','2026-10-01'),
 ('2026-09-30T16:30:00-04:00','2026-10-01'),
 ('2026-10-03T12:00:00-04:00','2026-10-05'),
 ('2026-07-03T12:00:00-04:00','2026-07-06'),
 ('2026-11-27T13:01:00-05:00','2026-11-30'),
 ('2026-03-06T16:30:00-05:00','2026-03-09'),
])
def test_next_observable_open_respects_calendar(observed, entry):
    assert entry_session(moment(observed))==entry


def test_chart_date_and_entry_date_are_distinct():
    observed=moment('2026-09-30T22:15:00-04:00')
    assert chart_date(observed)=='2026-09-30'
    assert entry_session(observed)=='2026-10-01'
    assert chart_date(moment('2026-10-03T22:15:00-04:00'))=='2026-10-05'
    assert horizon_date('2026-10-01',1)=='2026-10-01'
    assert horizon_date('2026-10-01',5)=='2026-10-07'


def test_daily_schedule_tracks_new_york_dst():
    due,next_run=schedule(moment('2026-03-08T12:00:00+00:00'))
    assert due.utcoffset().total_seconds()==-18000
    assert next_run.utcoffset().total_seconds()==-14400
    assert next_run.hour==22 and next_run.minute==15


def seed_signal(conn):
    ingest(conn,page('Dan Nathan has no positions.','Disclosures as of 9/29/26 (4:30 PM ET):'),moment('2026-09-29T22:15:00-04:00'),resolve=False)
    ingest(conn,page('Dan Nathan is long MSFT Oct put spread.'),moment(),resolve=False)


def add_price(conn,day,opening,close,complete=1):
    conn.execute("INSERT INTO prices(symbol,date,open,high,low,close,complete,fetched_at,provider) VALUES('MSFT',?,?,?,?,?,?,?,?)",(day,opening,max(opening,close)+1,min(opening,close)-1,close,complete,'2026-10-10T22:00:00+00:00','test: adjusted prices'))
    conn.commit()


def test_directional_scores_and_same_instrument_benchmark(database):
    seed_signal(database)
    add_price(database,'2026-10-01',100,90)
    add_price(database,'2026-10-07',95,80)
    scores=calculate(database,now=moment('2026-10-10T22:00:00+00:00'))
    assert len(scores['signals'])==1
    one=scores['summary'][0]
    assert one['follow']['mean']==pytest.approx(.1)
    assert one['oppose']['mean']==pytest.approx(-.1)
    assert one['always_long']['mean']==pytest.approx(-.1)
    assert one['follow']['win_rate']==1
    assert scores['summary'][1]['follow']['mean']==pytest.approx(.2)
    assert scores['summary'][2]['pending']==1
    assert scores['summary'][2]['follow']['n']==0


def test_missing_and_incomplete_prices_do_not_become_zero_returns(database):
    seed_signal(database)
    add_price(database,'2026-10-01',100,90,complete=0)
    result=calculate(database,now=moment('2026-10-10T22:00:00+00:00'))
    assert result['summary'][0]['missing']==1
    assert result['summary'][0]['follow']['mean'] is None


def test_source_date_is_not_used_as_backtest_entry(database):
    seed_signal(database)
    signal=calculate(database)['signals'][0]
    assert signal['entry_date']=='2026-10-01'


def test_retroactive_review_excluded_without_changing_raw_event(database):
    seed_signal(database)
    add_price(database,'2026-10-01',100,90)
    review(database,'MSFT','bullish','Corrected after further context',moment('2026-10-02T09:00:00-04:00'))
    result=calculate(database,now=moment('2026-10-10T22:00:00+00:00'))
    assert result['summary'][0]['reviewed']==1
    assert result['summary'][0]['follow']['n']==0
    assert result['signals'][0]['direction']=='bearish'
