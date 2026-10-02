import gzip
import hashlib
import zlib
from datetime import datetime


def initialize(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS dbmf_markets(
      id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
      provider_symbol TEXT, price_reference TEXT, price_kind TEXT, invert INTEGER NOT NULL,
      sort_order INTEGER NOT NULL, mapping_version TEXT NOT NULL,
      price_checked_at TEXT, price_error TEXT);
    CREATE TABLE IF NOT EXISTS dbmf_reports(
      id INTEGER PRIMARY KEY, source_date TEXT, net_assets TEXT,
      collected_at TEXT NOT NULL, source_url TEXT NOT NULL, source_kind TEXT NOT NULL,
      raw_path TEXT NOT NULL, raw_hash TEXT NOT NULL, fingerprint TEXT,
      parser_version TEXT NOT NULL, mapping_version TEXT NOT NULL,
      status TEXT NOT NULL, error TEXT, revision INTEGER NOT NULL DEFAULT 0,
      metadata_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS dbmf_reports_date ON dbmf_reports(source_date,status,id);
    CREATE TABLE IF NOT EXISTS dbmf_holdings(
      id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES dbmf_reports(id),
      row_number INTEGER NOT NULL, market_id TEXT NOT NULL REFERENCES dbmf_markets(id),
      original_name TEXT NOT NULL, identifier TEXT, ticker TEXT, quantity TEXT, expiry TEXT,
      notional TEXT NOT NULL, weight TEXT, exposure_pct TEXT NOT NULL, evidence TEXT NOT NULL,
      UNIQUE(report_id,row_number));
    CREATE TABLE IF NOT EXISTS dbmf_runs(
      id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
      slot TEXT, kind TEXT NOT NULL DEFAULT 'live', status TEXT NOT NULL, error TEXT,
      report_id INTEGER REFERENCES dbmf_reports(id), raw_path TEXT, source_url TEXT,
      price_errors TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX IF NOT EXISTS dbmf_runs_slot ON dbmf_runs(slot,kind,id);
    CREATE TABLE IF NOT EXISTS dbmf_prices(
      market_id TEXT NOT NULL REFERENCES dbmf_markets(id), date TEXT NOT NULL,
      open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
      volume REAL, fetched_at TEXT NOT NULL, provider TEXT NOT NULL,
      PRIMARY KEY(market_id,date));
    CREATE TABLE IF NOT EXISTS dbmf_aliases(
      normalized_name TEXT PRIMARY KEY, original_name TEXT NOT NULL,
      market_id TEXT NOT NULL REFERENCES dbmf_markets(id),
      source_url TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS dbmf_mapping_changes(
      id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, source_url TEXT NOT NULL,
      reason TEXT NOT NULL, document_json TEXT NOT NULL, catalog_version TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS dbmf_replay_attempts(
      id INTEGER PRIMARY KEY, source_report_id INTEGER NOT NULL REFERENCES dbmf_reports(id),
      result_report_id INTEGER REFERENCES dbmf_reports(id), adapter_version TEXT NOT NULL,
      attempted_at TEXT NOT NULL, status TEXT NOT NULL, error TEXT);
    CREATE INDEX IF NOT EXISTS dbmf_replay_source ON dbmf_replay_attempts(source_report_id,id);
    CREATE TABLE IF NOT EXISTS dbmf_observations(
      id INTEGER PRIMARY KEY, report_id INTEGER NOT NULL REFERENCES dbmf_reports(id),
      collected_at TEXT NOT NULL, raw_path TEXT NOT NULL, raw_hash TEXT NOT NULL,
      source_url TEXT NOT NULL,
      source_observation_id INTEGER REFERENCES dbmf_observations(id),
      run_id INTEGER UNIQUE REFERENCES dbmf_runs(id), collected_before TEXT,
      timestamp_basis TEXT NOT NULL DEFAULT 'acquisition');
    CREATE INDEX IF NOT EXISTS dbmf_observations_report ON dbmf_observations(report_id,collected_at,id);
    ''')
    # All services initialize at startup. Serialize the inspect/ALTER pair so
    # another service cannot add a column between those two statements.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in conn.execute('PRAGMA table_info(dbmf_reports)')}
        for name, definition in [('replayed_from', 'INTEGER REFERENCES dbmf_reports(id)'),
                                 ('processed_at', 'TEXT')]:
            if name not in columns:
                conn.execute(f'ALTER TABLE dbmf_reports ADD COLUMN {name} {definition}')
        # Acquisition evidence is append-only. A replay resolution points to its
        # original acquisition, retaining its order even within the same second.
        # Seed old databases once per report under the initialization writer lock.
        for row in conn.execute('''SELECT * FROM dbmf_reports r WHERE NOT EXISTS
            (SELECT 1 FROM dbmf_observations WHERE report_id=r.id) ORDER BY r.id''').fetchall():
            source = conn.execute('SELECT id FROM dbmf_observations WHERE report_id=? ORDER BY id LIMIT 1',
                                  (row['replayed_from'],)).fetchone() if row['replayed_from'] else None
            conn.execute('''INSERT INTO dbmf_observations
                (report_id,collected_at,raw_path,raw_hash,source_url,source_observation_id)
                VALUES(?,?,?,?,?,?)''', (row['id'], row['collected_at'], row['raw_path'],
                    row['raw_hash'], row['source_url'], source['id'] if source else None))
        _migrate_run_observations(conn)
        conn.execute('''CREATE VIEW IF NOT EXISTS dbmf_observed_reports AS
            SELECT r.*,o.collected_at AS last_observed_at,
                   o.collected_before AS last_observed_before,
                   o.timestamp_basis AS observation_time_basis,
                   (SELECT coalesce(source_observation_id,id) FROM dbmf_observations
                    WHERE report_id=r.id ORDER BY id LIMIT 1) AS original_observation_order,
                   coalesce(o.source_observation_id,o.id) AS observation_order
            FROM dbmf_reports r LEFT JOIN dbmf_observations o ON o.id=(
                SELECT id FROM dbmf_observations WHERE report_id=r.id
                ORDER BY collected_at DESC,(timestamp_basis='acquisition') DESC,
                    coalesce(source_observation_id,id) DESC,id DESC LIMIT 1)''')
    from .markets import initialize as seed_markets
    seed_markets(conn)


def _migrate_run_observations(conn):
    """Recover legacy duplicates only from verified archives and bounded runs.

    Old runs do not retain the exact fetch time. Their start is a lower bound,
    never a fabricated acquisition timestamp; a later fetch inside an overlapping
    run interval cannot be established, and exact evidence wins a timestamp tie.
    Failed/missing archive evidence is left
    untouched. Modern acquisitions already cover their run and need no import.
    """
    from .. import db

    rows = conn.execute('''SELECT r.*,p.source_kind FROM dbmf_runs r
        JOIN dbmf_reports p ON p.id=r.report_id
        WHERE p.status='accepted' AND r.status IN ('success','partial')
        AND r.finished_at IS NOT NULL AND r.raw_path IS NOT NULL
        AND NOT EXISTS(SELECT 1 FROM dbmf_observations WHERE run_id=r.id)
        ORDER BY r.started_at,r.id''').fetchall()
    for row in rows:
        try:
            start, finish = (datetime.fromisoformat(row[key]) for key in ('started_at', 'finished_at'))
            if start.utcoffset() is None or finish.utcoffset() is None or start > finish:
                continue
            lower, upper = db.iso(start), db.iso(finish)
            if conn.execute('''SELECT 1 FROM dbmf_observations WHERE report_id=?
                AND raw_path=? AND source_observation_id IS NULL
                AND collected_at>=? AND collected_at<=?''',
                (row['report_id'], row['raw_path'], lower, upper)).fetchone():
                continue
            # Stored filenames are content addresses. Check the path before
            # opening it, then verify the bytes rather than trusting a run link.
            suffix = 'pdf' if row['source_kind'] == 'historical' else 'html'
            digest = row['raw_path'].removeprefix('dbmf/raw/').removesuffix('.' + suffix + '.gz')
            if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                continue
            if row['raw_path'] != f'dbmf/raw/{digest}.{suffix}.gz':
                continue
            raw = gzip.decompress((db.DATA_DIR / row['raw_path']).read_bytes())
            if hashlib.sha256(raw).hexdigest() != digest:
                continue
        except (OSError, EOFError, ValueError, zlib.error):
            continue
        conn.execute('''INSERT INTO dbmf_observations
            (report_id,collected_at,collected_before,raw_path,raw_hash,source_url,run_id,timestamp_basis)
            VALUES(?,?,?,?,?,?,?,'run_start_lower_bound')''',
            (row['report_id'], lower, upper, row['raw_path'], digest,
             row['source_url'] or conn.execute('SELECT source_url FROM dbmf_reports WHERE id=?',
                 (row['report_id'],)).fetchone()[0], row['id']))
