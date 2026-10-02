#!/usr/bin/env python3
"""Check a Research Desk backup without changing it or restoring live data."""

import argparse
from contextlib import closing
import gzip
import hashlib
from pathlib import Path, PurePosixPath
import sqlite3
import sys
import tarfile
import tempfile
import zlib


def extract_candidate(archive_path, destination):
    """Only regular files and directories belong in application backups."""
    with tarfile.open(archive_path, 'r:gz') as archive:
        seen = set()
        for member in archive.getmembers():
            name = PurePosixPath(member.name)
            if (name.is_absolute() or '..' in name.parts or '\\' in member.name
                    or not (member.isfile() or member.isdir()) or name in seen):
                raise ValueError(f'Unsafe archive member: {member.name!r}')
            seen.add(name)
        archive.extractall(destination, filter='data')
        # Read through the gzip trailer too, so a bad CRC/truncation cannot pass.
        while archive.fileobj.read(1024 * 1024):
            pass


def source_digest(directory, raw_path):
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(f'Unsafe source path: {raw_path!r}')
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or '..' in relative.parts or '\\' in raw_path:
        raise ValueError(f'Unsafe source path: {raw_path!r}')
    source = (directory / raw_path).resolve()
    if not source.is_relative_to(directory):
        raise ValueError(f'Unsafe source path: {raw_path!r}')
    if not source.is_file():
        raise ValueError(f'Missing source archive: {raw_path!r}')
    try:
        with gzip.open(source, 'rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest()
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError(f'Cannot read source archive {raw_path!r}: {exc}') from exc


def check_database(directory):
    database = directory / 'tracker.sqlite3'
    if not database.is_file():
        raise ValueError('Database tracker.sqlite3 is missing from the archive root')
    for suffix in ('-wal', '-shm', '-journal'):
        if (directory / ('tracker.sqlite3' + suffix)).exists():
            raise ValueError('Database sidecar found; use an archive made by the backup command')
    counts = {}
    # The candidate is an isolated SQLite backup, without a live WAL. Read-only
    # immutable mode also prevents journal/sidecar writes during the check.
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        conn.execute('PRAGMA trusted_schema=OFF')
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('Database integrity check failed')
        violation = conn.execute('PRAGMA foreign_key_check').fetchone()
        if violation:
            raise ValueError(f'Database foreign key check failed: {violation!r}')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'snapshots', 'dbmf_reports'}.issubset(tables):
            raise ValueError('Database is missing Research Desk source tables')
        digests = {}
        for table, hash_column in [('snapshots', 'content_hash'),
                                   ('dbmf_reports', 'raw_hash'),
                                   ('dbmf_observations', 'raw_hash')]:
            if table not in tables:  # Older DBMF backups predate observations.
                continue
            counts[table] = 0
            for row_id, raw_path, expected in conn.execute(f'SELECT id,raw_path,{hash_column} FROM {table}'):
                if raw_path not in digests:
                    digests[raw_path] = source_digest(directory, raw_path)
                if digests[raw_path] != expected:
                    raise ValueError(f'Source hash mismatch: {table} row {row_id}, {raw_path!r}')
                counts[table] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path, help='Path to a YYYY-MM-DD.tar.gz backup')
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix='research-desk-backup-check-') as work:
            directory = Path(work).resolve()
            extract_candidate(args.archive, directory)
            counts = check_database(directory)
    except sqlite3.Error as exc:
        print(f'Backup check failed: Database error: {exc}', file=sys.stderr)
        return 1
    except (OSError, EOFError, ValueError, tarfile.TarError, zlib.error) as exc:
        print(f'Backup check failed: {exc}', file=sys.stderr)
        return 1
    observations = (f"{counts['dbmf_observations']} DBMF observations"
                    if 'dbmf_observations' in counts else 'legacy backup without DBMF observations')
    print(f"Backup verified: SQLite integrity and foreign keys OK; "
          f"source hashes checked for {counts['snapshots']} CNBC snapshots, "
          f"{counts['dbmf_reports']} DBMF reports, {observations}. "
          'No live data was changed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
