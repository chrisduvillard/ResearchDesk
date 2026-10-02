import json
from datetime import datetime
import pytest
from tracker import db

BASELINE = "Dan Nathan is long the DRAM Oct Put Spread, TBLA, TMCR, MSFT Oct Put Spread, and TLT Dec call spread."


def page(text=BASELINE, header="Disclosures as of 9/30/26 (4:30 PM ET):"):
    nodes = [{"tagName":"p","children":[{"tagName":"strong","children":[header]}]}]
    for sentence in text.split("\n"):
        nodes.append({"tagName":"p","children":[sentence]})
    return "<html><script>window.__s_data=" + json.dumps({"page":{"body":{"children":nodes}}}) + "; window.__c_data={};</script></html>"


def moment(value="2026-09-30T22:15:00-04:00"):
    return datetime.fromisoformat(value)


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    conn = db.connect()
    db.initialize(conn)
    yield conn
    conn.close()
