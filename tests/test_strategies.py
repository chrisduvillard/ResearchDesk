import json
import math
import pytest
from fastapi.testclient import TestClient
from tracker.api import app
from tracker.payoff import calculate
from tracker.strategies import analyze, payoff_for, leg
from tracker.parser import parse_positions
from tracker.history import ingest, current_positions, attach_details
from tracker.scores import calculate as scores
from conftest import page, moment


def model(legs, cost=None, **kw):
    return dict(legs=legs, same_expiry=True, net_cost=cost, **kw)


@pytest.mark.parametrize("legs,cost,minimum,maximum,roots", [
    ([leg("call",1,100),leg("call",-1,110)],300,-300,700,[103]),
    ([leg("put",1,110),leg("put",-1,100)],300,-300,700,[107]),
    ([leg("stock",100),leg("call",-1,110)],8800,-8800,2200,[88]),
    ([leg("stock",100),leg("put",1,95)],10200,-700,None,[102]),
    ([leg("stock",100),leg("put",1,90),leg("call",-1,110)],10000,-1000,1000,[100]),
    ([leg("call",1,90),leg("call",-2,100),leg("call",1,110)],200,-200,800,[92,108]),
    ([leg("call",1,90),leg("call",-2,100),leg("call",1,120)],200,-1200,800,[92,108]),
    ([leg("put",1,80),leg("put",-1,90),leg("call",-1,110),leg("call",1,120)],-300,-700,300,[87,113]),
    ([leg("call",1,100),leg("put",1,100)],1000,-1000,None,[90,110]),
    ([leg("call",-1,100),leg("put",-1,100)],-1000,None,1000,[90,110]),
])
def test_known_expiration_results(legs,cost,minimum,maximum,roots):
    result=calculate(model(legs,cost))
    assert result["minimum"]==minimum
    assert result["maximum"]==maximum
    assert result["minimum_unbounded"]==(minimum is None)
    assert result["maximum_unbounded"]==(maximum is None)
    assert result["breakevens"]==roots


def test_ratio_spread_tail_and_zero_interval_are_not_hidden_by_chart_limits():
    result=calculate(model([leg("call",1,100),leg("call",-2,110)],0,range_min=105,range_max=115))
    assert result["minimum_unbounded"]
    assert result["maximum"]==1000
    assert result["breakevens"]==[120]
    assert result["breakeven_ranges"]==[{"start":0,"to":100}]
    assert [r["trend"] for r in result["regions"]]==["flat","rising","falling"]
    assert min(p["price"] for p in result["points"])==105


def test_unknown_premium_never_becomes_zero_cost():
    result=calculate(model([leg("call",1,100),leg("call",-1,110)]))
    assert result["mode"]=="terminal_value"
    assert result["breakevens"]==[]
    assert result["breakeven_ranges"]==[]
    assert result["model"]["net_cost"] is None
    assert "not profit" in result["explanation"]


def test_synthetic_stock_put_call_parity_at_expiration():
    result=calculate(model([leg("call",1,100),leg("put",-1,100)]))
    assert all(p["value"]==pytest.approx(100*(p["price"]-100),abs=1e-5) for p in result["points"])
    assert result["direction"]=="bullish"


@pytest.mark.parametrize("bad", [
    {}, {"legs":[]}, model([leg("call",0,100)]),
    model([leg("call",1,float("nan"))]), model([leg("call",True,100)]),
    model([leg("call",1,-1)]), model([leg("call",1,100)],float("inf")),
    model([leg("stock",1,100)]), model([leg("call",1,100)]*17),
    model([leg("call",1,100)],range_min=100,range_max=100),
    {"legs":[leg("call",1,100),leg("call",-1,110)]},
    model([leg("call",1,100,"2026-10-16"),leg("call",-1,100,"2026-11-20")]),
    model([leg("call",1,100,"2026-02-30")]),
])
def test_invalid_or_incomplete_models_are_rejected(bad):
    with pytest.raises(ValueError):
        calculate(bad)


@pytest.mark.parametrize("side,text,family,direction,ready", [
    ("long","Oct covered calls","covered_call","bullish",False),
    ("long","Oct 110 covered call","covered_call","bullish",True),
    ("long","Oct 95 protective put","protective_put","bullish",True),
    ("long","Oct 90/110 protective collar","collar","bullish",True),
    ("long","Oct 100 straddle","straddle","large_move",True),
    ("short","Oct 90/110 strangle","strangle","range_bound",True),
    ("long","Oct 90/100/110 call butterfly","butterfly","range_bound",True),
    ("short","Oct 80/90/110/120 put condor","condor","large_move",True),
    ("long","Oct 80/90/110/120 credit iron condor","condor","range_bound",True),
    ("long","Oct 90/100/110 debit iron butterfly","butterfly","large_move",True),
    ("long","Oct iron condor","condor","conditional",False),
    ("long","Oct/Nov call calendar spread","calendar","conditional",False),
    ("long","Oct 100/110 1x2 call ratio spread","ratio","conditional",False),
    ("long","Oct call backspread","ratio","conditional",False),
    ("long","Oct 90/110 bullish risk reversal","risk_reversal","bullish",True),
    ("long","Oct risk reversal","risk_reversal","conditional",False),
    ("short","Oct 100 synthetic stock","synthetic","bearish",True),
])
def test_strategy_catalog(side,text,family,direction,ready):
    a=analyze(side,text)
    assert (a["family"],a["direction"],a["payoff_available"])==(family,direction,ready)
    assert a["confidence"]=="inferred"
    assert a["basis"]
    if ready:
        assert payoff_for(a)["direction"]==direction


def test_strikes_are_sorted_by_structure_not_text_order():
    a=analyze("long","Oct 110/100 put spread for $3 debit")
    result=payoff_for(a)
    assert a["legs"][0]["quantity"]==-1 and a["legs"][0]["strike"]==100
    assert result["minimum"]==-300 and result["maximum"]==700


def test_named_structure_does_not_invent_expiration_or_actual_size():
    a=analyze("long","Oct 100/110 call spread")
    assert all(l["expiry"] is None for l in a["legs"])
    assert any("Exact expiration" in missing for missing in a["missing"])
    assert a["confidence"]=="inferred"
    assert "actual position size is unknown" in a["basis"]


def test_explicit_legs_preserve_list_boundaries_and_quantity():
    p=parse_positions("Dan Nathan is long MSFT options [long 1 2026-10-16 100 call @ 10; short 2 2026-10-16 110 calls @ 5], and TLT.")
    assert [i["symbol"] for i in p]==["MSFT","TLT"]
    a=p[0]["analysis"]
    assert a["confidence"]=="explicit" and a["net_cost"]==0
    assert a["payoff_available"] and payoff_for(a)["minimum_unbounded"]
    assert not a["score_eligible"]


def test_explicit_legs_need_same_day_not_just_same_month():
    a=analyze("long","options [long 1 Oct 100 call; short 1 Oct 110 call]")
    assert not a["payoff_available"]
    a=analyze("long","options [short 1 2026-10-16 100 call; long 1 2026-11-20 100 call]")
    assert a["family"]=="calendar" and not a["payoff_available"]


def test_payoff_endpoint_and_analysis_never_write_history(database):
    ingest(database,page(),moment(),resolve=False)
    before=database.execute("SELECT count(*) FROM events").fetchone()[0]
    with TestClient(app) as client:
        body=client.get("/api/analysis/MSFT").json()
        assert body["name"]=="Microsoft" and body["strategies"][0]["analysis"]["family"]=="vertical"
        assert body["strategies"][0]["payoff"] is None
        assert client.post("/api/payoff",json=model([leg("call",1,100)],500)).json()["breakevens"]==[105]
        assert client.post("/api/payoff",json={"legs":[]}).status_code==400
        assert client.post("/api/payoff",content="x"*32001).status_code==413
    assert database.execute("SELECT count(*) FROM events").fetchone()[0]==before


def test_complex_bullish_strategy_does_not_enter_directional_scorecard(database):
    ingest(database,page("Dan Nathan has no positions."),moment(),resolve=False)
    ingest(database,page("Dan Nathan is long MSFT covered calls.","Disclosures as of 10/1/26 (4:30 PM ET):"),moment("2026-10-01T22:15:00-04:00"),resolve=False)
    assert current_positions(database)[0]["direction"]=="bullish"
    assert scores(database)["signals"]==[]


def test_parser_upgrade_is_a_correction_but_real_new_disclosures_still_count(database):
    ingest(database,page("Dan Nathan is long MSFT covered calls."),moment(),resolve=False)
    snap=database.execute("SELECT positions_json FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()
    old=json.loads(snap[0]);old[0]["direction"]="unknown"
    database.execute("UPDATE snapshots SET parser_version='1.0.1',positions_json=?",(json.dumps(old),))
    database.commit()
    ingest(database,page("Dan Nathan is long MSFT covered calls, and TLT.","Disclosures as of 10/1/26 (4:30 PM ET):"),moment("2026-10-01T22:15:00-04:00"),resolve=False)
    events=database.execute("SELECT symbol,kind,eligible FROM events WHERE kind!='baseline' ORDER BY symbol").fetchall()
    assert [tuple(row) for row in events]==[("MSFT","correction",0),("TLT","added",1)]


def test_sourced_details_are_audited_apply_only_to_identical_wording_and_exclude_scores(database):
    ingest(database,page("Dan Nathan has no positions."),moment(),resolve=False)
    ingest(database,page("Dan Nathan is long MSFT Oct put spread.","Disclosures as of 10/1/26 (4:30 PM ET):"),moment("2026-10-01T22:15:00-04:00"),resolve=False)
    original=database.execute("SELECT positions_json FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()[0]
    attach_details(database,"MSFT",model([leg("put",1,110),leg("put",-1,100)],300),
                   "Source transcript reference","Strikes confirmed",observed=moment("2026-10-02T09:00:00-04:00"))
    p=current_positions(database)[0]
    assert p["analysis"]["payoff_available"] and p["confidence"]=="reviewed"
    assert database.execute("SELECT positions_json FROM snapshots ORDER BY id DESC LIMIT 1").fetchone()[0]==original
    assert database.execute("SELECT kind,eligible FROM events ORDER BY id DESC LIMIT 1").fetchone()[:]==("correction",0)
    assert scores(database,now=moment("2026-10-03T22:00:00-04:00"))["summary"][0]["reviewed"]==1
    ingest(database,page("Dan Nathan is long MSFT Nov put spread.","Disclosures as of 10/2/26 (4:30 PM ET):"),moment("2026-10-02T22:15:00-04:00"),resolve=False)
    assert not current_positions(database)[0]["analysis"]["payoff_available"]


def test_stock_and_option_entry_costs_are_combined_only_when_complete():
    a=analyze("long","options [long 100 shares @ 90; short 1 2026-10-16 110 call @ 2]")
    p=payoff_for(a)
    assert a["net_cost"]==8800 and p["maximum"]==2200 and p["minimum"]==-8800
    a=analyze("long","options [long 100 shares; short 1 2026-10-16 110 call @ 2]")
    assert a["net_cost"] is None and payoff_for(a)["mode"]=="terminal_value"


def test_manual_direction_review_hides_unconfirmed_payoff(database):
    from tracker.history import review
    ingest(database,page("Dan Nathan is long MSFT Oct 100/110 call spread."),moment(),resolve=False)
    assert current_positions(database)[0]["analysis"]["payoff_available"]
    review(database,"MSFT","unknown","Structure may be a calendar",moment("2026-10-01T10:00:00-04:00"))
    p=current_positions(database)[0]
    assert p["direction"]==p["analysis"]["direction"]=="unknown"
    assert not p["analysis"]["payoff_available"]


def test_invalid_explicit_leg_cannot_drop_other_positions(database):
    ingest(database,page(),moment(),resolve=False)
    broken=page("Dan Nathan is long MSFT options [long 1 2026-10-16 500 call; something else], and TLT.",
                "Disclosures as of 10/1/26 (4:30 PM ET):")
    assert ingest(database,broken,moment("2026-10-01T22:15:00-04:00"),resolve=False)["status"]=="rejected"
    assert len(current_positions(database))==5


def test_new_direction_codes_survive_export_and_removal(database):
    from tracker.export import pine_export
    ingest(database,page("Dan Nathan is long MSFT Oct straddle."),moment(),resolve=False)
    assert "|baseline|large_move|" in pine_export(database,"MSFT")
    ingest(database,page("Dan Nathan has no positions.","Disclosures as of 10/1/26 (4:30 PM ET):"),
           moment("2026-10-01T22:15:00-04:00"),resolve=False)
    assert "|removed|unknown|" in pine_export(database,"MSFT")
    assert scores(database)["signals"]==[]


def test_backup_retains_sourced_model_and_evidence(database,tmp_path):
    import tarfile, sqlite3
    from tracker.collector import backup
    ingest(database,page("Dan Nathan is long MSFT Oct call spread."),moment(),resolve=False)
    attach_details(database,"MSFT",model([leg("call",1,100),leg("call",-1,110)],300),
                   "Verified transcript","Confirmed legs",observed=moment("2026-10-01T10:00:00-04:00"))
    saved=backup(database,moment())
    restore=tmp_path/"details-restore"
    with tarfile.open(saved) as archive:
        archive.extractall(restore,filter="data")
    with sqlite3.connect(restore/"tracker.sqlite3") as conn:
        from tracker.db import SCHEMA_VERSION
        assert conn.execute("PRAGMA user_version").fetchone()[0]==SCHEMA_VERSION
        assert conn.execute("PRAGMA integrity_check").fetchone()[0]=="ok"
        row=conn.execute("SELECT model_json,source FROM strategy_details").fetchone()
        assert calculate(json.loads(row[0]))["maximum"]==700
        assert row[1]=="Verified transcript"


@pytest.mark.parametrize("wording,expected", [
    ("Oct 100 synthetic long stock","bullish"),
    ("Oct 100 synthetic short stock","bearish"),
])
def test_explicit_synthetic_stock_names(wording,expected):
    a=analyze("long",wording)
    assert a["direction"]==expected and a["payoff_available"]
