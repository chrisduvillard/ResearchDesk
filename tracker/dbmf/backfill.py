"""Verified issuer locations; every import still validates fund, date and all rows."""
import fcntl
import json
import logging
import re
from urllib.parse import urlencode

from .. import db
from .collector import download
from .store import ingest

log = logging.getLogger(__name__)
ROOT = 'https://www.imgp.com/wp-content/uploads/'
SOURCES = [
    ('2025-09-30', ROOT + 'imgp-us-q32025.pdf'),
    ('2026-03-31', ROOT + '2026/06/Litman-Gregory-NPORT-F-3.31.26-19357A-bannerless.pdf'),
    ('2021-12-31', ROOT + '2022/03/LGFT-Annual-Report-12.31.21-Web-Ready.pdf'),
    ('2022-03-31', ROOT + '2022/09/Litman-Gregory-324347A-NPORT-PART-F-3-31-22-cycle.pdf'),
    ('2022-06-30', ROOT + '2022/09/Litman-SAR_063022_Web-Ready.pdf'),
    ('2022-09-30', ROOT + '2023/03/Q3-2022-Holdings.pdf'),
    ('2022-12-31', ROOT + '2023/03/Litman_AR_Web_Ready.pdf'),
    ('2023-03-31', ROOT + '2023/09/Litman-Gregory-266988A-NPORT-PART-F-3.31.23.pdf'),
    ('2023-06-30', ROOT + '2023/09/06302022-SemiAnnual-Report.pdf'),
    ('2023-09-30', ROOT + '2024/08/Q3-23-Holdings.pdf'),
    ('2023-12-31', ROOT + '2024/03/Annual-Report-to-Shareholders-December-2023.pdf'),
    ('2024-03-31', ROOT + '2024/08/Q1-2024-Holdings.pdf'),
    ('2024-06-30', ROOT + '2024/08/iMGP-Funds-Form-N-CSR-Information.pdf'),
    ('2024-09-30', ROOT + '2024/12/Litman-Gregory-11851A-without-banner.pdf'),
    ('2024-12-31', ROOT + '2025/03/iMGP-Funds-Form-N-CSR-Information-December-2024.pdf'),
    ('2025-03-31', ROOT + '2025/06/Litman-Gregory-NPORT-F-3.31.25-795096A.pdf'),
    ('2025-12-31', 'https://connect.rightprospectus.com/iMGP/TVT/53700T678/NCSR?site=iMGP_Funds'),
    ('2026-06-30', 'https://connect.rightprospectus.com/iMGP/TVT/53700T678/NCSRS?site=iMGP_Funds'),
]


def download_report(url):
    if not url.startswith('https://connect.rightprospectus.com/'):
        return download(url)
    # The issuer's public document viewer fetches its current JavaScript bundle,
    # then the public document API. Follow that same route without browser state
    # or private credentials; never persist the bundle's public subscription token.
    match = re.fullmatch(r'https://connect\.rightprospectus\.com/iMGP/TVT/([A-Z0-9]{9})/(NCSR|NCSRS)\?site=iMGP_Funds', url)
    if not match:
        raise ValueError('Unrecognized issuer document viewer URL')
    root = 'https://connect.rightprospectus.com'
    version = str(json.loads(download(root + '/assets/version.txt')))
    if not re.fullmatch(r'[\w-]+', version):
        raise ValueError('Document viewer asset version changed')
    script = download(root + '/assets/index-' + version + '.js').decode()
    token = re.search(r'VITE_ARC_DIGITAL_TSR_API_KEY:`([a-zA-Z0-9]+)`', script)
    if not token:
        raise ValueError('Public document viewer configuration changed')
    api = 'https://services.dfinsolutions.com/documentservice/documents/cusip/' + match[1] + '/doctype/' + match[2]
    return download(api + '?' + urlencode({'subscription-key': token[1]}))
SEC_SOURCES = [
    ('2019-12-31', 'https://www.sec.gov/Archives/edgar/data/1359057/000089853120000163/imdbietf-ncsra.htm'),
    ('2020-06-30', 'https://www.sec.gov/Archives/edgar/data/1359057/000089853120000413/imdbietf-ncsrs.htm'),
    ('2020-12-31', 'https://www.sec.gov/Archives/edgar/data/1359057/000089853121000180/imdbietf-ncsra.htm'),
]


def backfill(force=False):
    db.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (db.DATA_DIR / 'dbmf-backfill.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with db.database() as conn:
            db.initialize(conn)
            results = []
            for expected, url in SOURCES:
                if not force and conn.execute("SELECT 1 FROM dbmf_runs WHERE kind='historical' AND source_url=? AND status='success'", (url,)).fetchone():
                    continue
                with conn:
                    rid = conn.execute("INSERT INTO dbmf_runs(started_at,kind,status,source_url) VALUES(?,'historical','running',?)", (db.iso(), url)).lastrowid
                report, error, raw = None, None, None
                try:
                    raw = download_report(url)
                    # The source date comes from the document; the catalog date
                    # guards against an issuer replacing a URL with a new period.
                    report = ingest(conn, raw, source_url=url, kind='historical', expected_date=expected)
                    if report['status'] != 'accepted':
                        error = report['error']
                except Exception as exc:
                    error = str(exc)[:1500]
                    # Archive and record rejected downloaded PDFs too.
                    if raw:
                        from .store import archive
                        path, _ = archive(raw, 'pdf')
                        with conn:
                            conn.execute('UPDATE dbmf_runs SET raw_path=? WHERE id=?', (path, rid))
                with conn:
                    conn.execute('UPDATE dbmf_runs SET finished_at=?,status=?,error=?,report_id=?,raw_path=coalesce(?,raw_path) WHERE id=?',
                                 (db.iso(), 'error' if error else 'success', error, report['id'] if report else None, report['raw_path'] if report else None, rid))
                result = dict(date=expected, url=url, error=error, report=report)
                results.append(result)
                log.info('DBMF historical %s: %s', expected, error or 'accepted')
                raw = None
            access = []
            for day, url in SEC_SOURCES:
                try:
                    raw = download(url)
                    from .store import archive
                    path, _ = archive(raw, 'html')
                    access.append(dict(date=day, url=url, status='Downloaded; SEC HTML adapter requires verification before import', raw_path=path))
                except Exception as exc:
                    access.append(dict(date=day, url=url, status=str(exc)[:500]))
            with conn:
                db.set_setting(conn, 'dbmf_backfill_checked_at', db.iso())
                db.set_setting(conn, 'dbmf_sec_access', access)
            return results
