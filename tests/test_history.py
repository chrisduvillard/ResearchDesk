import gzip
import json
import pytest
from tracker import db
from tracker.parser import extract, parse_positions, aggregate, ParseError
from tracker.history import ingest, current_positions, latest_snapshot, review
from conftest import page, moment, BASELINE


def test_supplied_disclosure_parses_five_correct_exposures():
    _, text, as_of = extract(page())
    positions = parse_positions(text)
    assert {p['symbol']:p['direction'] for p in positions} == {'DRAM':'bearish','TBLA':'bullish','TMCR':'bullish','MSFT':'bearish','TLT':'bullish'}
    assert as_of.isoformat() == '2026-09-30T20:30:00+00:00'
    tlt = next(p for p in positions if p['symbol']=='TLT')
    assert tlt['expiry_month'] == 12 and tlt['expiry_year'] is None
    assert tlt['confidence'] == 'inferred'


@pytest.mark.parametrize('wording,expected', [
 ('long MSFT','bullish'), ('short MSFT','bearish'),
 ('long MSFT Oct puts','bearish'), ('short MSFT Oct puts','bullish'),
 ('long MSFT Oct calls','bullish'), ('short MSFT Oct calls','bearish'),
 ('long MSFT Oct put spread','bearish'), ('short MSFT Oct put spread','bullish'),
 ('long MSFT Oct call spread','bullish'), ('short MSFT Oct call spread','bearish'),
 ('long MSFT Oct bull put spread','bullish'), ('long MSFT Oct bear call spread','bearish'),
 ('long MSFT Oct put credit spread','bullish'), ('long MSFT Oct call credit spread','bearish'),
 ('long MSFT Oct 450/440 put spread','bearish'),
 ('long MSFT Oct put calendar spread','conditional'),
 ('long MSFT Oct call ratio spread','conditional'),
 ('long MSFT Oct complex put spread','unknown'),
 ('long MSFT covered calls','bullish'), ('long MSFT straddle','large_move'),
 ('short MSFT bull put spread','unknown'),
])
def test_strategy_direction_or_explicit_uncertainty(wording, expected):
    assert parse_positions('Dan Nathan is '+wording+'.')[0]['direction'] == expected


def test_inherited_side_changes_and_hedged_exposure():
    positions = parse_positions('Dan Nathan is long MSFT, short TLT calls, and TBLA puts.')
    assert [p['direction'] for p in positions] == ['bullish','bullish','bearish']
    positions = parse_positions('Dan Nathan is long MSFT, and MSFT Oct puts.')
    assert aggregate(positions) == 'mixed'


@pytest.mark.parametrize('text', ['Dan Nathan is long MSFT & QQQ.', 'Dan Nathan is long MSFT, and a number of other securities.', 'Dan Nathan is long MSFT, MSFT.', 'Dan Nathan has some positions.'])
def test_incomplete_parse_is_rejected(text):
    with pytest.raises(ParseError):
        parse_positions(text)


def test_baseline_archive_and_duplicate_detection(database):
    first = ingest(database, page(), moment(), resolve=False)
    assert first['events'] == 5
    assert database.execute('SELECT sum(eligible) FROM events').fetchone()[0] == 0
    row = latest_snapshot(database)
    assert gzip.decompress((db.DATA_DIR/row['raw_path']).read_bytes()).decode() == page()
    reordered = 'Dan Nathan is long TLT Dec CALL SPREAD, MSFT Oct put spread, TMCR, TBLA, and the DRAM Oct Put Spread.'
    second = ingest(database, page(reordered, 'Disclosures as of 10/1/26 (4:30 PM ET):'), moment('2026-10-01T22:15:00-04:00'), resolve=False)
    assert second['events'] == 0
    assert database.execute('SELECT count(*) FROM snapshots').fetchone()[0] == 2


def test_removal_and_flip_have_distinct_meanings(database):
    ingest(database, page(), moment(), resolve=False)
    changed = 'Dan Nathan is long TBLA, TMCR, MSFT Oct call spread, and TLT Dec call spread.'
    result = ingest(database, page(changed,'Disclosures as of 10/1/26 (4:30 PM ET):'), moment('2026-10-01T22:15:00-04:00'), resolve=False)
    assert result['events'] == 2
    removed = database.execute("SELECT * FROM events WHERE kind='removed'").fetchone()
    assert removed['symbol']=='DRAM' and removed['direction']=='unknown' and not removed['eligible']
    flip = database.execute("SELECT * FROM events WHERE kind='direction_changed'").fetchone()
    assert flip['symbol']=='MSFT' and flip['direction']=='bullish' and flip['eligible']


def test_roll_keeps_direction_without_new_signal(database):
    ingest(database, page(), moment(), resolve=False)
    changed = BASELINE.replace('MSFT Oct Put Spread','MSFT Nov Put Spread')
    ingest(database, page(changed,'Disclosures as of 10/1/26 (4:30 PM ET):'), moment('2026-10-01T22:15:00-04:00'), resolve=False)
    event = database.execute('SELECT * FROM events ORDER BY id DESC').fetchone()
    assert event['kind']=='strategy_changed' and not event['eligible']


@pytest.mark.parametrize('html', ['<html>Service unavailable</html>', page('Dan Nathan holds various options.'), page(header='Disclosures as of unknown:')])
def test_scrape_failure_never_removes_positions(database, html):
    ingest(database, page(), moment(), resolve=False)
    result = ingest(database, html, moment('2026-10-01T22:15:00-04:00'), resolve=False)
    assert result['status']=='rejected'
    assert len(current_positions(database))==5
    assert database.execute('SELECT count(*) FROM events').fetchone()[0]==5


def test_older_cached_and_future_dated_pages_are_not_applied(database):
    ingest(database, page(), moment(), resolve=False)
    assert ingest(database, page('Dan Nathan is long MSFT.','Disclosures as of 9/29/26 (4:30 PM ET):'), moment(), resolve=False)['status']=='older'
    assert ingest(database, page(header='Disclosures as of 10/5/26 (4:30 PM ET):'), moment(), resolve=False)['status']=='rejected'
    assert len(current_positions(database))==5


def test_explicit_empty_disclosure_and_reentry(database):
    ingest(database, page(), moment(), resolve=False)
    empty = ingest(database, page('Dan Nathan has no positions.','Disclosures as of 10/1/26 (4:30 PM ET):'), moment('2026-10-01T22:15:00-04:00'), resolve=False)
    assert empty['events']==5 and current_positions(database)==[]
    ingest(database, page('Dan Nathan is long MSFT.','Disclosures as of 10/2/26 (4:30 PM ET):'), moment('2026-10-02T22:15:00-04:00'), resolve=False)
    assert database.execute('SELECT kind,eligible FROM events ORDER BY id DESC').fetchone()[:] == ('added',1)


def test_review_preserves_source_and_does_not_create_new_trade(database):
    ingest(database, page(), moment(), resolve=False)
    review(database, 'MSFT', 'unknown', 'Portfolio hedge details unavailable', moment('2026-10-01T10:00:00-04:00'))
    assert next(p for p in current_positions(database) if p['symbol']=='MSFT')['direction']=='unknown'
    assert latest_snapshot(database)['disclosure']==BASELINE
    event=database.execute('SELECT * FROM events ORDER BY id DESC').fetchone()
    assert event['kind']=='correction' and not event['eligible']
    again=ingest(database,page(),moment('2026-10-01T22:15:00-04:00'),resolve=False)
    assert again['events']==0


def test_unknown_instrument_is_not_scored_before_verification(database):
    ingest(database,page('Dan Nathan has no positions.'),moment(),resolve=False)
    ingest(database,page('Dan Nathan is long ZZZZ.','Disclosures as of 10/1/26 (4:30 PM ET):'),moment('2026-10-01T22:15:00-04:00'),resolve=False)
    assert not database.execute('SELECT eligible FROM events ORDER BY id DESC').fetchone()[0]
    assert database.execute("SELECT name FROM instruments WHERE symbol='ZZZZ'").fetchone()[0]=='Unverified instrument (ZZZZ)'


@pytest.mark.parametrize('continuation', [
    'He also holds TLT Dec call spread.',
    'Additionally, Dan is long TLT Dec call spread.',
    'TLT Dec call spread.',
])
def test_unrecognized_disclosure_continuation_never_implies_removal(database, continuation):
    ingest(database, page(), moment(), resolve=False)
    changed = page('Dan Nathan is long MSFT.\n' + continuation,
                   'Disclosures as of 10/1/26 (4:30 PM ET):')
    result = ingest(database, changed, moment('2026-10-01T22:15:00-04:00'), resolve=False)
    assert result['status'] == 'rejected'
    assert len(current_positions(database)) == 5
    assert database.execute('SELECT count(*) FROM events').fetchone()[0] == 5


def test_disclosure_paragraphs_stay_inside_source_body():
    # A footer from another module must not become part of the disclosure.
    body = [
        {'tagName': 'p', 'children': ['Disclosures as of 9/30/26 (4:30 PM ET):']},
        {'tagName': 'p', 'children': ['Dan Nathan is long MSFT.']},
        {'tagName': 'p', 'children': []},
        {'tagName': 'p', 'children': ['He is long TLT Dec call spread.']},
    ]
    state = {'modules': [
        {'children': body},
        {'children': [{'tagName': 'p', 'children': ['Subscribe to our newsletter.']}]},
    ]}
    html = '<script>window.__s_data=' + json.dumps(state) + ';</script>'
    _, disclosure, _ = extract(html)
    assert {p['symbol'] for p in parse_positions(disclosure)} == {'MSFT', 'TLT'}
