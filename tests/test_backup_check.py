from contextlib import closing
import gzip
import hashlib
import io
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile

import pytest

from conftest import moment, page
from tracker.collector import backup
from tracker.dbmf import store
from tracker.history import ingest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'check-backup.py'


@pytest.fixture
def saved_backup(database):
    ingest(database, page(), moment(), resolve=False)
    raw = (Path(__file__).parent / 'fixtures/dbmf/live.html').read_text()
    now = moment('2026-10-01T22:30:00-04:00')
    store.ingest(database, raw, now)
    # Duplicate acquisitions have their own archived response, not a new report.
    store.ingest(database, '<!-- second acquisition -->' + raw, now)
    return backup(database, now)


def run_check(archive, tmp_path):
    return subprocess.run([sys.executable, str(SCRIPT), str(archive)],
                          capture_output=True, text=True,
                          env={**os.environ, 'TMPDIR': str(tmp_path)})


def unpack(archive, tmp_path):
    directory = tmp_path / 'candidate'
    directory.mkdir()
    with tarfile.open(archive, 'r:gz') as source:
        source.extractall(directory, filter='data')
    return directory


def pack(directory, tmp_path):
    archive = tmp_path / 'modified.tar.gz'
    with tarfile.open(archive, 'w:gz') as target:
        for child in directory.iterdir():
            target.add(child, arcname=child.name)
    return archive


def assert_rejected(result, reason):
    assert result.returncode != 0
    assert 'Backup check failed:' in result.stderr
    assert reason.lower() in result.stderr.lower()
    assert 'Traceback' not in result.stderr


def test_real_backup_verifies_without_changing_archive_or_live_data(saved_backup, database, tmp_path):
    before = saved_backup.read_bytes()
    rows = list(database.execute('SELECT * FROM snapshots'))
    result = run_check(saved_backup, tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'verified' in result.stdout.lower()
    assert saved_backup.read_bytes() == before
    assert list(database.execute('SELECT * FROM snapshots')) == rows


@pytest.mark.parametrize('table,hash_column', [
    ('snapshots', 'content_hash'), ('dbmf_reports', 'raw_hash'),
    ('dbmf_observations', 'raw_hash'),
])
def test_rejects_mismatched_source_hashes(saved_backup, tmp_path, table, hash_column):
    directory = unpack(saved_backup, tmp_path)
    with closing(sqlite3.connect(directory / 'tracker.sqlite3')) as conn, conn:
        conn.execute(f'UPDATE {table} SET {hash_column}=? WHERE id=(SELECT max(id) FROM {table})', ('0' * 64,))
    assert_rejected(run_check(pack(directory, tmp_path), tmp_path), 'hash mismatch')


def test_rejects_missing_duplicate_acquisition_source(saved_backup, tmp_path):
    directory = unpack(saved_backup, tmp_path)
    with closing(sqlite3.connect(directory / 'tracker.sqlite3')) as conn, conn:
        raw = conn.execute('SELECT raw_path FROM dbmf_observations ORDER BY id DESC LIMIT 1').fetchone()[0]
    (directory / raw).unlink()
    assert_rejected(run_check(pack(directory, tmp_path), tmp_path), 'missing source')


def test_legacy_backup_without_observations_is_supported(saved_backup, tmp_path):
    directory = unpack(saved_backup, tmp_path)
    with closing(sqlite3.connect(directory / 'tracker.sqlite3')) as conn, conn:
        conn.execute('DROP VIEW dbmf_observed_reports')
        conn.execute('DROP TABLE dbmf_observations')
    result = run_check(pack(directory, tmp_path), tmp_path)
    assert result.returncode == 0, result.stderr
    assert 'legacy' in result.stdout.lower()


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'unrelated'])
def test_requires_real_tracker_database(saved_backup, tmp_path, damage):
    directory = unpack(saved_backup, tmp_path)
    db_path = directory / 'tracker.sqlite3'
    db_path.unlink()
    if damage == 'corrupt':
        db_path.write_bytes(b'not a SQLite database')
    elif damage == 'unrelated':
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute('CREATE TABLE unrelated(id INTEGER)')
    assert_rejected(run_check(pack(directory, tmp_path), tmp_path), 'database')


def test_rejects_foreign_key_damage(saved_backup, tmp_path):
    directory = unpack(saved_backup, tmp_path)
    with closing(sqlite3.connect(directory / 'tracker.sqlite3')) as conn, conn:
        conn.execute('UPDATE events SET snapshot_id=99999')
    assert_rejected(run_check(pack(directory, tmp_path), tmp_path), 'foreign key')


@pytest.mark.parametrize('member_kind', ['traversal', 'symlink'])
def test_rejects_archive_escape_without_writing_outside_temporary_directory(tmp_path, member_kind):
    archive = tmp_path / 'unsafe.tar.gz'
    with tarfile.open(archive, 'w:gz') as target:
        member = tarfile.TarInfo('../escaped' if member_kind == 'traversal' else 'outside-link')
        if member_kind == 'symlink':
            member.type = tarfile.SYMTYPE
            member.linkname = str(tmp_path / 'escaped')
            target.addfile(member)
        else:
            member.size = 4
            target.addfile(member, io.BytesIO(b'oops'))
    assert_rejected(run_check(archive, tmp_path), 'unsafe archive')
    assert not (tmp_path / 'escaped').exists()


def test_rejects_database_source_path_outside_extracted_directory(saved_backup, tmp_path):
    directory = unpack(saved_backup, tmp_path)
    outside = tmp_path / 'outside.html.gz'
    outside.write_bytes(gzip.compress(b'outside source'))
    with closing(sqlite3.connect(directory / 'tracker.sqlite3')) as conn, conn:
        conn.execute('UPDATE snapshots SET raw_path=?,content_hash=?',
                     (str(outside), hashlib.sha256(b'outside source').hexdigest()))
    assert_rejected(run_check(pack(directory, tmp_path), tmp_path), 'unsafe source path')
    assert gzip.decompress(outside.read_bytes()) == b'outside source'


def test_rejects_invalid_gzip_archive_with_clear_error(tmp_path):
    archive = tmp_path / 'invalid.tar.gz'
    archive.write_bytes(b'not a gzip archive')
    assert_rejected(run_check(archive, tmp_path), 'gzip')


@pytest.mark.parametrize('suffix', ['-wal', '-shm', '-journal'])
def test_rejects_database_sidecars_that_could_change_restored_data(saved_backup, tmp_path, suffix):
    directory = unpack(saved_backup, tmp_path)
    (directory / ('tracker.sqlite3' + suffix)).write_bytes(b'unchecked sidecar')
    assert_rejected(run_check(pack(directory, tmp_path), tmp_path), 'sidecar')
