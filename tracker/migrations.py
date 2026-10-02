"""Numbered, atomic expansion migrations. Legacy evidence stays in place."""

from . import db

CONTRIBUTORS = [
    ("dan-nathan", "Dan Nathan"),
    ("karen-finerman", "Karen Finerman"),
    ("guy-adami", "Guy Adami"),
    ("josh-brown", "Josh Brown"),
    ("steve-weiss", "Steve Weiss"),
    ("tim-seymour", "Tim Seymour"),
]
FUNDS = ["DBMF", "KMLM", "CTA", "WTMF", "ARKK", "ARKQ", "ARKW", "ARKG", "ARKF", "ARKX"]


def foundation(conn):
    # execute each statement: executescript would commit halfway through a migration.
    statements = """
    CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
    CREATE TABLE contributors(id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE funds(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL);
    CREATE TABLE sources(id TEXT PRIMARY KEY, contributor_id TEXT REFERENCES contributors(id),
      fund_id TEXT REFERENCES funds(id), url TEXT NOT NULL, adapter TEXT,
      enabled INTEGER NOT NULL DEFAULT 0, coverage TEXT NOT NULL DEFAULT 'unavailable',
      reason TEXT, CHECK((contributor_id IS NULL) != (fund_id IS NULL)));
    CREATE TABLE assets(id TEXT PRIMARY KEY, symbol TEXT NOT NULL, name TEXT NOT NULL,
      kind TEXT NOT NULL, currency TEXT, exchange TEXT, provider_symbol TEXT,
      verified INTEGER NOT NULL DEFAULT 0, provenance TEXT, sector TEXT, sector_as_of TEXT,
      benchmark_id TEXT REFERENCES assets(id));
    CREATE INDEX assets_search ON assets(symbol,name);
    CREATE TABLE source_runs(id INTEGER PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id),
      started_at TEXT NOT NULL, finished_at TEXT, slot TEXT, status TEXT NOT NULL,
      error TEXT, report_date TEXT, scheduled INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX source_runs_scope ON source_runs(source_id,id);
    CREATE TABLE disclosures(id INTEGER PRIMARY KEY, contributor_id TEXT NOT NULL REFERENCES contributors(id),
      source_id TEXT NOT NULL REFERENCES sources(id), source_as_of TEXT, acquired_at TEXT NOT NULL,
      processed_at TEXT NOT NULL, raw_path TEXT NOT NULL, content_hash TEXT NOT NULL,
      parser_version TEXT NOT NULL, status TEXT NOT NULL, error TEXT, text TEXT,
      positions_json TEXT, legacy_id INTEGER UNIQUE REFERENCES snapshots(id));
    CREATE INDEX disclosures_scope ON disclosures(contributor_id,status,id);
    CREATE TABLE research_events(id INTEGER PRIMARY KEY, contributor_id TEXT NOT NULL REFERENCES contributors(id),
      evidence_type TEXT NOT NULL, asset_id TEXT REFERENCES assets(id), symbol TEXT NOT NULL,
      kind TEXT NOT NULL, direction TEXT NOT NULL, eligible INTEGER NOT NULL,
      public_at TEXT, available_at TEXT NOT NULL, source_url TEXT NOT NULL,
      excerpt TEXT NOT NULL, disclosure_id INTEGER REFERENCES disclosures(id), call_revision_id INTEGER,
      legacy_id INTEGER UNIQUE REFERENCES events(id), details_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX research_events_scope ON research_events(contributor_id,evidence_type,available_at,id);
    CREATE INDEX research_events_asset ON research_events(asset_id,id);
    CREATE TABLE contributor_reviews(id INTEGER PRIMARY KEY, contributor_id TEXT NOT NULL REFERENCES contributors(id),
      symbol TEXT NOT NULL, signature TEXT NOT NULL, direction TEXT NOT NULL,
      reason TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE calls(id INTEGER PRIMARY KEY, contributor_id TEXT NOT NULL REFERENCES contributors(id),
      current_revision INTEGER, created_at TEXT NOT NULL);
    CREATE TABLE call_revisions(id INTEGER PRIMARY KEY, call_id INTEGER NOT NULL REFERENCES calls(id),
      revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('draft','approved','retracted')),
      document_json TEXT NOT NULL, recorded_at TEXT NOT NULL, approved_at TEXT,
      UNIQUE(call_id,revision));
    CREATE TABLE owner(id INTEGER PRIMARY KEY CHECK(id=1), password_hash TEXT NOT NULL, changed_at TEXT NOT NULL);
    CREATE TABLE sessions(token_hash TEXT PRIMARY KEY, csrf_token TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
    CREATE TABLE login_attempts(id INTEGER PRIMARY KEY, attempted_at TEXT NOT NULL);
    CREATE TABLE audit_log(id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
      entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, created_at TEXT NOT NULL, details_json TEXT NOT NULL);
    CREATE TABLE activity(id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, source_id TEXT REFERENCES sources(id),
      asset_id TEXT REFERENCES assets(id), kind TEXT NOT NULL, public_at TEXT,
      available_at TEXT NOT NULL, recorded_at TEXT NOT NULL, title TEXT NOT NULL,
      evidence_url TEXT NOT NULL, reference_type TEXT NOT NULL, reference_id TEXT NOT NULL,
      details_json TEXT NOT NULL DEFAULT '{}');
    CREATE INDEX activity_time ON activity(recorded_at,id);
    CREATE INDEX activity_asset ON activity(asset_id,id);
    CREATE TABLE fund_reports(id TEXT PRIMARY KEY, fund_id TEXT NOT NULL REFERENCES funds(id),
      source_id TEXT NOT NULL REFERENCES sources(id), source_date TEXT NOT NULL, acquired_at TEXT,
      processed_at TEXT NOT NULL, raw_path TEXT NOT NULL, content_hash TEXT NOT NULL,
      parser_version TEXT NOT NULL, status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
      complete INTEGER NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}', legacy_id INTEGER UNIQUE REFERENCES dbmf_reports(id));
    CREATE INDEX fund_reports_scope ON fund_reports(fund_id,source_date,id);
    CREATE TABLE fund_holdings(id INTEGER PRIMARY KEY, report_id TEXT NOT NULL REFERENCES fund_reports(id),
      asset_id TEXT REFERENCES assets(id), instrument_key TEXT NOT NULL, name TEXT NOT NULL,
      measure TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL,
      quantity TEXT, notional TEXT, expiry TEXT, currency TEXT, collateral INTEGER NOT NULL DEFAULT 0,
      evidence TEXT NOT NULL, UNIQUE(report_id,instrument_key,measure));
    CREATE TABLE fund_observations(id INTEGER PRIMARY KEY, report_id TEXT NOT NULL REFERENCES fund_reports(id),
      acquired_at TEXT NOT NULL, raw_path TEXT NOT NULL, content_hash TEXT NOT NULL,
      legacy_id INTEGER UNIQUE REFERENCES dbmf_observations(id));
    CREATE TABLE price_batches(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, provider TEXT NOT NULL,
      kind TEXT NOT NULL, inputs_json TEXT NOT NULL, content_hash TEXT NOT NULL);
    CREATE TABLE analysis_runs(id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
      created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, inputs_json TEXT NOT NULL,
      result_json TEXT, error TEXT, input_hash TEXT NOT NULL UNIQUE);
    CREATE TABLE simulation_definitions(id INTEGER PRIMARY KEY, created_at TEXT NOT NULL,
      contributor_id TEXT NOT NULL REFERENCES contributors(id), evidence_type TEXT NOT NULL,
      mode TEXT NOT NULL, forward_start TEXT, previous_id INTEGER REFERENCES simulation_definitions(id),
      config_json TEXT NOT NULL);
    CREATE TABLE briefings(id INTEGER PRIMARY KEY, cutoff TEXT NOT NULL UNIQUE, previous_cutoff TEXT NOT NULL,
      created_at TEXT NOT NULL, activity_ids_json TEXT NOT NULL);
    CREATE TABLE alert_rules(id INTEGER PRIMARY KEY, source_id TEXT REFERENCES sources(id), asset_id TEXT REFERENCES assets(id),
      enabled INTEGER NOT NULL DEFAULT 1, config_json TEXT NOT NULL);
    """
    for statement in statements.split(";"):
        if statement.strip():
            conn.execute(statement)
    for slug, name in CONTRIBUTORS:
        conn.execute("INSERT INTO contributors VALUES(?,?)", (slug, name))
        supported = slug in ("dan-nathan", "karen-finerman", "guy-adami")
        conn.execute(
            "INSERT INTO sources(id,contributor_id,url,adapter,enabled,coverage,reason) VALUES(?,?,?,?,?,?,?)",
            (
                "cnbc:" + slug,
                slug,
                f"https://www.cnbc.com/{slug}/",
                "cnbc" if supported else None,
                int(supported),
                "observation" if supported else "unavailable",
                None
                if supported
                else "Official dated disclosure automation has not been qualified. Reviewed calls remain available.",
            ),
        )
    urls = {
        "DBMF": "https://imgpfunds.com/im-dbi-managed-futures-strategy-etf/",
        "CTA": "https://www.simplify.us/etfs/cta-simplify-managed-futures-strategy-etf",
        "KMLM": "https://kraneshares.com/etf/kmlm/",
        "WTMF": "https://www.wisdomtree.com/us/products/alternative/wtmf",
    }
    for symbol in FUNDS:
        conn.execute(
            "INSERT INTO funds VALUES(?,?,?)",
            (
                symbol,
                symbol,
                "equity" if symbol.startswith("ARK") else "managed_futures",
            ),
        )
        conn.execute(
            "INSERT INTO sources(id,fund_id,url,adapter,enabled,coverage,reason) VALUES(?,?,?,?,?,?,?)",
            (
                "fund:" + symbol,
                symbol,
                urls.get(symbol, "https://www.ark-funds.com/our-etfs"),
                "dbmf" if symbol == "DBMF" else None,
                int(symbol == "DBMF"),
                "observation" if symbol == "DBMF" else "unavailable",
                "Requires five scheduled successes across two reporting dates"
                if symbol == "DBMF"
                else "Official automated source requires qualification; manual imports are disabled.",
            ),
        )
    conn.execute(
        "INSERT INTO alert_rules(config_json) VALUES(?)",
        (
            '{"contributor_changes":true,"futures_pp":5,"equity_pp":1,"flip_min":1,"source_health":true}',
        ),
    )
    db.set_setting(conn, "activity_epoch", __import__("secrets").token_hex(16))


MIGRATIONS = [(5, foundation)]


def apply_migrations(conn, migrations=None):
    if conn.in_transaction:
        raise RuntimeError(
            "Migrations require a connection without an active transaction"
        )
    for version, apply in migrations or MIGRATIONS:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            if current >= version:
                continue
            if version != current + 1:
                raise RuntimeError(f"Migration gap: {current} to {version}")
            apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations VALUES(?,?)", (version, db.iso())
            )
            conn.execute(f"PRAGMA user_version={version}")


def research_integrity(conn):
    conn.execute("""CREATE TABLE asset_links(alias_id TEXT PRIMARY KEY REFERENCES assets(id),
      canonical_id TEXT NOT NULL REFERENCES assets(id),source_url TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
      CHECK(alias_id!=canonical_id))""")
    conn.execute(
        "ALTER TABLE research_events ADD COLUMN eligible_at_creation INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute("ALTER TABLE research_events ADD COLUMN invalidated_at TEXT")
    conn.execute("UPDATE research_events SET eligible_at_creation=eligible")
    conn.execute("""CREATE TRIGGER event_initial_eligibility AFTER INSERT ON research_events
      BEGIN UPDATE research_events SET eligible_at_creation=NEW.eligible WHERE id=NEW.id; END""")
    conn.execute(
        "CREATE INDEX fund_holdings_asset ON fund_holdings(asset_id,report_id)"
    )
    conn.execute(
        "CREATE INDEX price_batches_latest ON price_batches(provider,kind,created_at)"
    )
    conn.execute("CREATE INDEX source_success ON source_runs(source_id,status,id)")


MIGRATIONS.append((6, research_integrity))
