"""Reviewed, persistent extensions to the built-in DBMF market definitions."""
import hashlib
import json
import re
from urllib.parse import urlsplit

from .. import db
from .markets import MAPPING_VERSION, UnknownInstrument, identify, normalize_name

CATEGORIES = {'Bonds', 'Equities', 'Commodities', 'Currencies', 'Short-term rates', 'Collateral'}


class Catalog:
    def __init__(self, conn):
        self.aliases = {row['normalized_name']: row['market_id']
                        for row in conn.execute('SELECT * FROM dbmf_aliases')}
        content = json.dumps([MAPPING_VERSION, sorted(self.aliases.items())], separators=(',', ':'))
        self.version = MAPPING_VERSION + '-' + hashlib.sha256(content.encode()).hexdigest()[:16]

    def identify(self, name):
        custom = self.aliases.get(normalize_name(name))
        try:
            built_in = identify(name)
        except UnknownInstrument:
            built_in = None
        if custom and built_in and custom != built_in:
            raise ValueError(f'Conflicting instrument mappings: {name}')
        if custom or built_in:
            return custom or built_in
        raise UnknownInstrument(f'Unrecognized instrument: {name}')


def _text(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f'{label} must be nonempty text, at most {maximum} characters')
    if any(ord(char) < 32 for char in value):
        raise ValueError(f'{label} contains control characters')
    return value.strip()


def import_mapping(conn, document):
    """Atomic, additive review. Existing identities cannot be reassigned silently."""
    if not isinstance(document, dict) or set(document) - {'market', 'market_id', 'aliases', 'source_url', 'reason'}:
        raise ValueError('Unexpected mapping fields')
    source = _text(document.get('source_url'), 'source_url', 1500)
    parsed_url = urlsplit(source)
    if parsed_url.scheme not in ('https', 'http') or not parsed_url.hostname or parsed_url.username or parsed_url.password:
        raise ValueError('source_url must be a public evidence URL without credentials')
    reason = _text(document.get('reason'), 'reason', 1000)
    aliases = document.get('aliases')
    if not isinstance(aliases, list) or not 1 <= len(aliases) <= 50:
        raise ValueError('Supply 1 to 50 literal contract names in aliases')
    names = {}
    for name in aliases:
        name = _text(name, 'alias')
        if re.search(r'[*?{}\[\]|^$\\]', name):
            raise ValueError('Aliases are literal contract names, not regular expressions')
        key = normalize_name(name)
        if len(key) < 3:
            raise ValueError('Alias is too short after normalization')
        names[key] = name
    spec = document.get('market')
    if ('market' in document) == ('market_id' in document):
        raise ValueError('Supply either a new market definition or an existing market_id')
    if spec is not None:
        allowed = {'id', 'name', 'category', 'value_basis', 'provider_symbol', 'price_reference', 'price_kind', 'invert'}
        if not isinstance(spec, dict) or set(spec) - allowed:
            raise ValueError('Unexpected market definition fields')
        market_id = spec.get('id')
        name = _text(spec.get('name'), 'market name', 120)
        category = spec.get('category')
        if category not in CATEGORIES:
            raise ValueError('Unknown market category')
        basis = 'market_value' if category == 'Collateral' else 'signed_notional'
        if spec.get('value_basis') != basis:
            raise ValueError(f'Confirm value_basis={basis} from the source before adding this market')
        symbol = spec.get('provider_symbol')
        reference = spec.get('price_reference')
        kind = spec.get('price_kind', 'unavailable')
        invert = spec.get('invert', False)
        if not isinstance(invert, bool):
            raise ValueError('invert must be a boolean')
        if symbol is None:
            if kind != 'unavailable' or invert or reference is not None:
                raise ValueError('A missing price symbol must use unavailable, without inversion or a reference')
            reference = 'No verified price reference'
        else:
            if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9.^=\-]{1,30}', symbol):
                raise ValueError('Invalid price-provider symbol')
            if kind not in ('futures', 'currency', 'ETF proxy'):
                raise ValueError('Specify futures, currency or ETF proxy for the price reference')
            reference = _text(reference, 'price_reference', 160)
            if invert and kind != 'currency':
                raise ValueError('Only currency references support inversion')
            if kind == 'currency' and category != 'Currencies':
                raise ValueError('Currency price references require the Currencies category')
    else:
        market_id = document.get('market_id')
    if not isinstance(market_id, str) or not re.fullmatch(r'[a-z][a-z0-9_]{1,39}', market_id):
        raise ValueError('Invalid market_id')

    # Take a write transaction before checking conflicts so two administrative
    # imports cannot overwrite each other's reviewed aliases.
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute('SELECT * FROM dbmf_markets WHERE id=?', (market_id,)).fetchone()
        if spec is not None and existing:
            expected = (name, category, symbol, reference, kind, int(invert))
            actual = tuple(existing[key] for key in ('name', 'category', 'provider_symbol', 'price_reference', 'price_kind', 'invert'))
            if expected != actual:
                raise ValueError('Existing market identity/reference differs; use market_id to add aliases')
        if spec is None and existing is None:
            raise ValueError('Unknown market_id; supply a complete new market definition')
        catalog = Catalog(conn)
        for alias in names.values():
            try:
                target = catalog.identify(alias)
            except UnknownInstrument:
                target = None
            if target is not None and target != market_id:
                raise ValueError(f'Alias already belongs to {target}: {alias}')
        changed = False
        if existing is None:
            conn.execute('''INSERT INTO dbmf_markets
                (id,name,category,provider_symbol,price_reference,price_kind,invert,sort_order,mapping_version)
                VALUES(?,?,?,?,?,?,?,?,?)''',
                (market_id, name, category, symbol, reference, kind, int(invert),
                 conn.execute('SELECT coalesce(max(sort_order),0)+1 FROM dbmf_markets').fetchone()[0], 'reviewed'))
            changed = True
        for key, alias in names.items():
            cursor = conn.execute('''INSERT OR IGNORE INTO dbmf_aliases
                (normalized_name,original_name,market_id,source_url,reason,created_at) VALUES(?,?,?,?,?,?)''',
                (key, alias, market_id, source, reason, db.iso()))
            changed = changed or bool(cursor.rowcount)
        version = Catalog(conn).version
        if changed:
            conn.execute('''INSERT INTO dbmf_mapping_changes
                (created_at,source_url,reason,document_json,catalog_version) VALUES(?,?,?,?,?)''',
                (db.iso(), source, reason, json.dumps(document, sort_keys=True), version))
    return dict(market_id=market_id, aliases=list(names), changed=changed, catalog_version=version)
