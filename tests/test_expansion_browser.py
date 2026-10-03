"""Optional real-browser journeys: RESEARCH_BROWSER=1 python -m pytest -q this-file.

Install the test-only playwright package and set BROWSER_EXECUTABLE, or install
Playwright Chromium. Test data and the web server are isolated from production.
"""

import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
import pytest
from tracker import db

pytestmark = pytest.mark.skipif(
    os.environ.get("RESEARCH_BROWSER") != "1",
    reason="Opt-in local browser verification",
)


@pytest.fixture
def browser_app(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    from tracker.auth import setup_owner
    from tracker.research import sync_legacy
    from tracker.briefing import generate

    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    password = secrets.token_urlsafe(24)
    with db.database() as conn:
        db.initialize(conn)
        setup_owner(conn, password)
        sync_legacy(conn)
        generate(conn)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = os.environ | {"DATA_DIR": str(tmp_path), "APP_SECURE_COOKIES": "0"}
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tracker.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        import urllib.request

        for _ in range(100):
            try:
                urllib.request.urlopen(origin + "/health", timeout=0.2)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("Preview server did not start")
        with sync_playwright() as play:
            browser = play.chromium.launch(
                executable_path=os.environ.get("BROWSER_EXECUTABLE") or None,
                args=["--no-sandbox"],
            )
            context = browser.new_context(permissions=["notifications"])
            yield context, origin, password
            context.close()
            browser.close()
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_owner_call_approval_navigation_and_mobile(browser_app):
    from playwright.sync_api import expect

    context, origin, password = browser_app
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin)
    expect(page.get_by_role("heading", name="Today", exact=True)).to_be_visible()
    page.get_by_role("button", name="Owner login").click()
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Log in", exact=True).click()
    expect(page.get_by_role("button", name="Log out")).to_be_visible()
    page.goto(origin + "/research?view=record&contributor=karen-finerman")
    page.locator("select[name=asset_id]").select_option("legacy:MSFT")
    page.get_by_label("Source link", exact=True).fill("https://www.cnbc.com/video/test")
    page.get_by_label("Stated horizon", exact=True).fill("20 sessions")
    payload = '<img src=x onerror="window.pwned=1"> bullish Microsoft'
    page.get_by_label("Short supporting excerpt", exact=True).fill(payload)
    page.get_by_role("button", name="Save draft").click()
    expect(page.get_by_role("heading", name="On-air calls")).to_be_visible()
    expect(page.get_by_text(payload, exact=True)).to_be_visible()
    assert page.evaluate("window.pwned") is None
    page.get_by_role("button", name="Approve this revision").click()
    expect(page.locator(".badge").filter(has_text="approved")).to_be_visible()
    page.get_by_role("link", name="legacy:MSFT", exact=True).click()
    expect(page.get_by_role("heading", name="MSFT · Microsoft")).to_be_visible()
    page.go_back()
    expect(page.get_by_role("heading", name="On-air calls")).to_be_visible()
    page.get_by_role("link", name="Edit", exact=True).click()
    expect(
        page.get_by_role("heading", name="Revise a call", exact=True)
    ).to_be_visible()
    expect(
        page.get_by_role("textbox", name="Short supporting excerpt", exact=True)
    ).to_have_value(payload)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(origin + "/research?view=contributors")
    expect(page.get_by_role("heading", name="Contributors", exact=True)).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert errors == []


def test_notification_cross_tab_cursor_restore_and_no_archive_flood(browser_app):
    from tracker.research import activity

    context, origin, _ = browser_app
    # Delivery API is a browser dependency; keep app cursor/locking code real.
    context.add_init_script("""window.Notification=class {
      static permission='granted'; static async requestPermission(){return 'granted'};
      constructor(title){let rows=JSON.parse(localStorage.getItem('test-notifications')||'[]');rows.push(title);localStorage.setItem('test-notifications',JSON.stringify(rows));}
    };""")
    a = context.new_page()
    b = context.new_page()
    with db.database() as conn, conn:
        activity(
            conn,
            "old",
            "cnbc:dan-nathan",
            None,
            "added",
            None,
            db.iso(),
            "Old archive",
            "https://www.cnbc.com/",
            "event",
            1,
        )
    for page in [a, b]:
        page.goto(origin + "/research?view=sources")
        page.wait_for_selector("#notifications")
    a.locator("#notifications").click()
    from playwright.sync_api import expect

    expect(a.locator("#notifications")).to_have_text("Disable browser notifications")
    a.evaluate("notify()")
    b.evaluate("notify()")
    assert (
        a.evaluate("JSON.parse(localStorage.getItem('test-notifications')||'[]')") == []
    )
    with db.database() as conn, conn:
        activity(
            conn,
            "new",
            "cnbc:dan-nathan",
            None,
            "added",
            None,
            db.iso(),
            "Fresh observation",
            "https://www.cnbc.com/",
            "event",
            2,
        )
    a.evaluate("notify()")
    b.evaluate("notify()")
    assert a.evaluate("JSON.parse(localStorage.getItem('test-notifications'))") == [
        "Fresh observation"
    ]
    with db.database() as conn, conn:
        db.set_setting(conn, "activity_epoch", "restored-copy")
    a.evaluate("notify()")
    b.evaluate("notify()")
    assert a.evaluate("JSON.parse(localStorage.getItem('test-notifications'))") == [
        "Fresh observation"
    ]


def test_legacy_bookmark_redirect(browser_app):
    from playwright.sync_api import expect

    context, origin, _ = browser_app
    page = context.new_page()
    page.goto(origin + "/?page=history&range=12#history")
    page.wait_for_url("**/dan?**")
    assert "/dan?" in page.url and "page=history" in page.url and "range=12" in page.url


def test_shared_fund_and_contributor_switchers(browser_app):
    from playwright.sync_api import expect
    from conftest import page as disclosure_page, moment
    from tracker.history import ingest
    from tracker.research import ingest_disclosure, sync_legacy
    from tracker.dbmf import store
    from tracker.funds import accept_report, parse_kmlm, sync_dbmf

    context, origin, _ = browser_app
    fixtures = Path(__file__).parent / "fixtures"
    with db.database() as conn:
        ingest(conn, disclosure_page(), moment(), resolve=False)
        ingest_disclosure(conn, "karen-finerman", disclosure_page("Karen Finerman is long MSFT."), moment())
        store.ingest(conn, (fixtures / "dbmf/live.html").read_text(), moment("2026-10-01T22:30:00-04:00"))
        sync_legacy(conn)
        sync_dbmf(conn)
        raw = (fixtures / "funds/kmlm.csv").read_bytes()
        accept_report(conn, "KMLM", parse_kmlm(raw, net_assets="562525502", expected_date="2026-10-01"), raw)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(origin)
    expect(page.locator("#desk-person option[value='josh-brown']")).to_have_count(0)
    expect(page.locator("#desk-fund option[value='WTMF']")).to_have_count(0)
    page.get_by_label("Fund", exact=True).select_option("DBMF")
    page.wait_for_url(re.compile(r"/dbmf(?:\?|$)"))
    expect(page.get_by_role("heading", name="Current positioning")).to_be_visible()
    expect(page.get_by_label("Fund", exact=True)).to_have_value("DBMF")
    page.get_by_label("Fund", exact=True).select_option("KMLM")
    expect(page.get_by_role("heading", name="KMLM’s market exposure")).to_be_visible()
    expect(page.get_by_label("Fund", exact=True)).to_have_value("KMLM")
    page.go_back()
    expect(page.get_by_label("Fund", exact=True)).to_have_value("DBMF")
    page.get_by_label("CNBC contributor", exact=True).select_option("dan-nathan")
    page.wait_for_url(re.compile(r"/dan(?:\?|$)"))
    expect(page.get_by_role("heading", name="Dan Nathan’s positions")).to_be_visible()
    page.get_by_label("CNBC contributor", exact=True).select_option("karen-finerman")
    expect(page.get_by_role("heading", name="Karen Finerman’s positions")).to_be_visible()
    expect(page.get_by_label("CNBC contributor", exact=True)).to_have_value("karen-finerman")
    page.get_by_role("link", name="Compare funds", exact=True).click()
    expect(page.get_by_role("heading", name="Fund positioning")).to_be_visible()
    expect(page.get_by_role("link", name="WTMF", exact=True)).to_have_count(0)
    page.go_back()
    expect(page.get_by_label("CNBC contributor", exact=True)).to_have_value("karen-finerman")
    page.goto(origin + "/research?view=funds&fund=DBMF")
    page.wait_for_url(re.compile(r"/dbmf(?:\?|$)"))
    for path in ["/", "/dan", "/dbmf", "/research?view=funds&fund=ARKK"]:
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(origin + path)
        expect(page.get_by_label("Fund", exact=True)).to_be_visible()
        expect(page.get_by_label("CNBC contributor", exact=True)).to_be_visible()
        assert page.locator("#desk-navigation").evaluate("e => e.scrollWidth <= e.clientWidth")
    assert errors == []


def test_analytics_displays_classes_benchmarks_and_coverage(browser_app):
    from playwright.sync_api import expect
    from tracker.analytics import enqueue, process_job

    context, origin, _ = browser_app
    events = [
        dict(
            id=i + 1,
            asset_id=a,
            available_at="2026-01-02T12:00:00+00:00",
            direction="bullish",
            eligible=True,
            kind="call",
            details_json="{}",
            contributor_id="dan-nathan",
            evidence_type="call",
        )
        for i, a in enumerate(["legacy:MSFT", "bond"])
    ]
    inputs = dict(
        events=events,
        assets={"legacy:MSFT": {"kind": "Companies"}, "bond": {"kind": "Bonds"}},
        sessions=["2026-01-02"],
        prices={
            a: {"2026-01-02": dict(open=100, close=110, dividend=0, split=0)}
            for a in ["legacy:MSFT", "bond", "spy"]
        },
        benchmarks={"legacy:MSFT": "spy", "bond": "spy"},
        config={"horizons": [1], "horizon": 1},
        as_of="2026-01-03T00:00:00+00:00",
    )
    with db.database() as conn:
        score = enqueue(conn, "scorecard", inputs)
        process_job(conn, score)
        portfolio = enqueue(conn, "simulation", inputs)
        process_job(conn, portfolio)
    page = context.new_page()
    page.goto(origin + "/research?view=analytics&run=" + score)
    expect(
        page.get_by_role("columnheader", name="Asset class", exact=True)
    ).to_be_visible()
    expect(page.get_by_role("cell", name="Companies", exact=True)).to_be_visible()
    expect(page.get_by_role("cell", name="Bonds", exact=True)).to_be_visible()
    expect(
        page.get_by_role("columnheader", name="Always-long net", exact=True)
    ).to_be_visible()
    page.goto(origin + "/research?view=analytics&run=" + portfolio)
    expect(
        page.locator(".badge").filter(has_text="Exploratory historical")
    ).to_be_visible()
    expect(
        page.get_by_text(
            "Coverage: 2 eligible signals · 0 missing entry prices · 0 open positions.",
            exact=True,
        )
    ).to_have_count(2)
    expect(page.get_by_text("Buy-and-hold spy:", exact=False)).to_have_count(2)


def test_full_dashboards_for_other_people_and_equity_funds(browser_app):
    from playwright.sync_api import expect
    from test_shared_desks import contributors, fund_report
    from tracker.analysis_worker import price_batch

    context, origin, _ = browser_app
    with db.database() as conn:
        contributors(conn)
        fund_report(conn, 'ARKK', '2026-09-30', '4')
        fund_report(conn, 'ARKK', '2026-10-01', '6', raw=b'second')
        price_batch(conn,'legacy:MSFT','chart',dict(adjustment='Split and dividend adjusted',bars=[dict(time='2026-09-30',open=100,high=110,low=99,close=105,volume=10),dict(time='2026-10-01',open=105,high=112,low=103,close=110,volume=12)]),db.utcnow())
    p=context.new_page(); errors=[]
    p.on('pageerror',lambda e:errors.append(str(e)))
    p.goto(origin+'/contributors/karen-finerman')
    expect(p.get_by_role('heading',name='Karen Finerman’s positions')).to_be_visible()
    expect(p.locator('#chart canvas').first).to_be_visible()
    p.get_by_role('button',name=re.compile('History')).first.click()
    expect(p.locator('#history-body tr')).to_have_count(2)
    p.get_by_role('button',name='Scorecard',exact=True).click()
    expect(p.locator('#score-cards article')).to_have_count(3)
    p.get_by_label('Fund',exact=True).select_option('ARKK')
    expect(p.get_by_role('heading',name='ARKK’s holdings')).to_be_visible()
    expect(p.locator('#market-bars .market-bar-row')).to_have_count(1)
    expect(p.locator('#dbmf-chart canvas').first).to_be_visible()
    expect(p.locator('#heatmap .heatmap-cell')).to_have_count(2)
    p.locator('#heatmap .heatmap-cell').first.click()
    expect(p.locator('#comparison-caption')).to_contain_text('30 Sept 2026')
    p.locator('#contract-evidence').evaluate('(e)=>e.open=true')
    expect(p.locator('#evidence-content')).to_contain_text('Microsoft')
    expect(p.locator('#evidence-content')).not_to_contain_text('NaN')
    expect(p.locator('#evidence-content')).not_to_contain_text('Signed notional')
    expect(p.locator('#health-content')).to_contain_text('ARKK')
    expect(p.locator('#health-content')).not_to_contain_text('Dan Nathan')
    p.set_viewport_size({'width':390,'height':844})
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth')
    p.reload()
    expect(p.locator('#evidence-report')).not_to_have_value('NaN')
    assert errors==[]


def test_shared_contributor_retains_owner_direction_review(browser_app):
    from playwright.sync_api import expect
    from test_shared_desks import contributors
    context,origin,password=browser_app
    with db.database() as conn: contributors(conn)
    p=context.new_page()
    p.goto(origin+'/contributors/karen-finerman?symbol=MSFT')
    expect(p.locator('#review-direction')).to_be_hidden()
    assert context.request.post(origin+'/api/v2/auth/login',data={'password':password}).ok
    p.reload()
    p.get_by_role('button',name='Review direction',exact=True).click()
    expect(p.locator('#direction-review')).to_be_visible()
    p.get_by_label('Reviewed direction',exact=True).select_option('unknown')
    p.get_by_label('Review reason',exact=True).fill('Attribution requires further evidence')
    p.get_by_role('button',name='Save review',exact=True).click()
    expect(p.locator('#chart-direction')).to_contain_text('Unknown')
    assert context.request.get(origin+'/api/v2/contributors/guy-adami/positions').json()[0]['direction']=='bearish'
