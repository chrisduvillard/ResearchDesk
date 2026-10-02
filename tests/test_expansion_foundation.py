import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from tracker import db
from tracker.api import app
from tracker.history import ingest
from conftest import page, moment


def test_migration_preserves_evidence_and_is_repeatable(database):
    ingest(database, page(), moment(), resolve=False)
    old = dict(database.execute("SELECT * FROM snapshots").fetchone())
    db.initialize(database)
    db.initialize(database)
    assert dict(database.execute("SELECT * FROM snapshots").fetchone()) == old
    tables = {
        r[0]
        for r in database.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "contributors" in tables
    assert database.execute("SELECT count(*) FROM contributors").fetchone()[0] == 6
    assert database.execute("SELECT count(*) FROM funds").fetchone()[0] == 10
    assert database.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_is_atomic(database):
    from tracker.migrations import apply_migrations

    version = database.execute("PRAGMA user_version").fetchone()[0]

    def broken(conn):
        conn.execute("CREATE TABLE should_rollback(id INTEGER)")
        raise RuntimeError("interrupted")

    with pytest.raises(RuntimeError):
        apply_migrations(database, [(version + 1, broken)])
    assert not database.execute(
        "SELECT 1 FROM sqlite_master WHERE name='should_rollback'"
    ).fetchone()
    assert database.execute("PRAGMA user_version").fetchone()[0] == version


def test_owner_session_csrf_logout_and_reset(database):
    from tracker.auth import setup_owner

    setup_owner(database, "a long local test password")
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/v2/contributors").status_code == 200
        assert client.post("/api/v2/calls", json={}).status_code == 401
        assert (
            client.post("/api/v2/auth/login", json={"password": "wrong"}).status_code
            == 401
        )
        response = client.post(
            "/api/v2/auth/login", json={"password": "a long local test password"}
        )
        assert response.status_code == 200
        assert "HttpOnly" in response.headers["set-cookie"]
        assert "Secure" in response.headers["set-cookie"]
        csrf = response.json()["csrf_token"]
        assert client.post("/api/v2/auth/logout").status_code == 403
        assert (
            client.post(
                "/api/v2/auth/logout", headers={"X-CSRF-Token": csrf}
            ).status_code
            == 200
        )
        assert not client.get("/api/v2/auth").json()["authenticated"]
        client.post(
            "/api/v2/auth/login", json={"password": "a long local test password"}
        )
        setup_owner(database, "another long test password")
        assert not client.get("/api/v2/auth").json()["authenticated"]
    assert (
        database.execute("SELECT password_hash FROM owner")
        .fetchone()[0]
        .startswith("$argon2id$")
    )


def test_login_throttled_and_cross_origin_rejected(database):
    from tracker.auth import setup_owner

    setup_owner(database, "a long local test password")
    with TestClient(app, base_url="https://testserver") as client:
        assert (
            client.post(
                "/api/v2/auth/login",
                json={"password": "a long local test password"},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        for _ in range(5):
            assert (
                client.post("/api/v2/auth/login", json={"password": "bad"}).status_code
                == 401
            )
        assert (
            client.post(
                "/api/v2/auth/login", json={"password": "a long local test password"}
            ).status_code
            == 429
        )


def test_pre_migration_backup_is_immutable_and_sessions_invalidated(database):
    from tracker.collector import backup, migration_backup
    from tracker.auth import setup_owner
    import tarfile, sqlite3

    setup_owner(database, "a long local test password")
    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/api/v2/auth/login", json={"password": "a long local test password"}
        )
    protected = migration_backup(database)
    content = protected.read_bytes()
    backup(database)
    assert protected.read_bytes() == content
    with tarfile.open(protected) as archive:
        archive.extract("tracker.sqlite3", db.DATA_DIR / "checked", filter="data")
    with sqlite3.connect(db.DATA_DIR / "checked/tracker.sqlite3") as restored:
        assert restored.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_owner_reviewed_identity_mapping_unifies_research_without_mutating_evidence(
    database,
):
    from tracker.research import sync_legacy
    from tracker.auth import setup_owner

    setup_owner(database, "a long local test password")
    sync_legacy(database)
    with database:
        database.execute(
            "INSERT INTO assets(id,symbol,name,kind,verified) VALUES('cusip:594918104','MSFT','Microsoft','security',1)"
        )
    with TestClient(app, base_url="https://testserver") as client:
        csrf = client.post(
            "/api/v2/auth/login", json={"password": "a long local test password"}
        ).json()["csrf_token"]
        response = client.put(
            "/api/v2/assets/cusip:594918104/mapping",
            json={
                "canonical_id": "legacy:MSFT",
                "source_url": "https://www.microsoft.com/en-us/investor/",
                "reason": "Verified same US listing and security",
            },
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200, response.text
        result = client.get("/api/v2/assets/cusip:594918104").json()
        assert result["id"] == "legacy:MSFT"
        assert "cusip:594918104" in result["related_ids"]
        assert database.execute(
            "SELECT id FROM assets WHERE id='cusip:594918104'"
        ).fetchone()


def test_password_reset_cannot_leave_old_login_session(database, monkeypatch):
    import contextlib, threading
    from tracker.auth import setup_owner

    setup_owner(database, "a long local test password")
    original = db.database
    threads = []
    started = threading.Event()
    finished = threading.Event()

    def reset():
        with original() as conn:
            setup_owner(conn, "replacement local test password")
        finished.set()

    class CheckedCursor:
        def __init__(self, cursor):
            self.cursor = cursor

        def fetchone(self):
            row = self.cursor.fetchone()
            if not started.is_set():
                started.set()
                thread = threading.Thread(target=reset)
                threads.append(thread)
                thread.start()
                # Old code lets reset finish in this gap; corrected code holds the
                # writer reservation until session insertion is committed.
                finished.wait(1)
            return row

    class Connection:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *args):
            return self.conn.__exit__(*args)

        def execute(self, sql, args=()):
            result = self.conn.execute(sql, args)
            return (
                CheckedCursor(result)
                if sql == "SELECT 1 FROM owner WHERE password_hash=?"
                else result
            )

    @contextlib.contextmanager
    def wrapped():
        with original() as conn:
            yield Connection(conn)

    monkeypatch.setattr(db, "database", wrapped)
    with TestClient(app, base_url="https://testserver") as client:
        client.post(
            "/api/v2/auth/login", json={"password": "a long local test password"}
        )
        for thread in threads:
            thread.join(5)
        assert finished.is_set()
        assert not client.get("/api/v2/auth").json()["authenticated"]


def test_history_cursor_scope_and_restore(database):
    from tracker.research import sync_legacy

    sync_legacy(database)
    with database:
        for _ in range(3):
            database.execute(
                "INSERT INTO research_events(contributor_id,evidence_type,asset_id,symbol,kind,direction,eligible,available_at,source_url,excerpt) VALUES('dan-nathan','disclosure','legacy:MSFT','MSFT','added','bullish',0,?,'https://www.cnbc.com/','test')",
                (db.iso(),),
            )
    with TestClient(app) as client:
        first = client.get("/api/v2/contributors/dan-nathan/events?limit=2")
        assert len(first.json()) == 2
        cursor = first.headers["X-Next-Cursor"]
        second = client.get(
            "/api/v2/contributors/dan-nathan/events",
            params={"cursor": cursor, "limit": 2},
        )
        assert len(second.json()) == 1
        assert {r["id"] for r in first.json()}.isdisjoint(
            r["id"] for r in second.json()
        )
        assert (
            client.get(
                "/api/v2/contributors/karen-finerman/events", params={"cursor": cursor}
            ).status_code
            == 422
        )
        with database:
            db.set_setting(database, "activity_epoch", "restored")
        reset = client.get(
            "/api/v2/contributors/dan-nathan/events", params={"cursor": cursor}
        )
        assert reset.headers["X-Cursor-Reset"] == "true"
