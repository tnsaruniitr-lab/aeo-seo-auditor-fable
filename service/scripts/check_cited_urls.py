"""
check_cited_urls.py — weekly link-checker for the citation corpus.

Every URL the auditor can put in front of a customer should return 200. This
sweeps the DISTINCT source_urls of active, citeable rules (tier-1/2 orgs first),
records each result in sieve.link_checks, and reports hard-404s.

Deliberately conservative (Google/Bing docs bot-block aggressively):
  - 403/406/429 and network errors are recorded as 'blocked'/'error', NOT broken
  - only a hard 404/410 on TWO consecutive weekly sweeps counts as broken
  - nothing is demoted automatically here — the report lists repeat-404 URLs so
    the operator (or a later automated pass) can retire/fix them deliberately

Usage:
    SIEVE_DB_URL=... python3 scripts/check_cited_urls.py [--limit 500]
"""

from __future__ import annotations

import os
import sys
import time

import httpx
import psycopg2

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')
UA = {'User-Agent': 'Mozilla/5.0 (compatible; sieve-link-check/1.0)'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS sieve.link_checks (
    url          text NOT NULL,
    checked_at   timestamptz NOT NULL DEFAULT now(),
    status_code  int,
    verdict      text NOT NULL,   -- ok | broken | blocked | error
    note         text
);
CREATE INDEX IF NOT EXISTS link_checks_url_idx ON sieve.link_checks (url, checked_at DESC);
"""


def classify(code: int | None, err: str | None) -> str:
    if err:
        return 'error'
    if code in (404, 410):
        return 'broken'
    if code in (401, 403, 406, 429, 999):
        return 'blocked'   # bot-blocked ≠ broken; do not punish good sources
    if code and 200 <= code < 400:
        return 'ok'
    return 'error'


def main() -> int:
    limit = 500
    if '--limit' in sys.argv:
        limit = int(sys.argv[sys.argv.index('--limit') + 1])
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(SCHEMA)
    # Citeable URLs, most-authoritative first (they surface most in reports).
    cur.execute("""
        SELECT DISTINCT source_url FROM sieve.rules
        WHERE source_url IS NOT NULL AND source_url <> ''
          AND coalesce(status,'active') = 'active'
        ORDER BY source_url LIMIT %s
    """, (limit,))
    urls = [r[0] for r in cur.fetchall()]
    print(f'checking {len(urls)} distinct cited URLs...', flush=True)

    counts = {'ok': 0, 'broken': 0, 'blocked': 0, 'error': 0}
    with httpx.Client(timeout=20, follow_redirects=True, headers=UA) as client:
        for i, u in enumerate(urls):
            code, err = None, None
            try:
                r = client.head(u)
                if r.status_code in (405, 501):   # HEAD not allowed → GET
                    r = client.get(u)
                code = r.status_code
            except Exception as e:
                err = f'{type(e).__name__}: {e}'[:200]
            verdict = classify(code, err)
            counts[verdict] += 1
            cur.execute("INSERT INTO sieve.link_checks (url, status_code, verdict, note) "
                        "VALUES (%s,%s,%s,%s)", (u, code, verdict, err))
            if verdict == 'broken':
                print(f'  BROKEN {code} {u}', flush=True)
            if i % 50 == 49:
                print(f'  ...{i + 1}/{len(urls)}', flush=True)
            time.sleep(0.3)   # politeness

    # Repeat offenders: hard-404 on the two most recent sweeps.
    cur.execute("""
        WITH last2 AS (
            SELECT url, verdict,
                   row_number() OVER (PARTITION BY url ORDER BY checked_at DESC) rn
            FROM sieve.link_checks
        )
        SELECT url FROM last2 WHERE rn <= 2
        GROUP BY url HAVING count(*) = 2 AND bool_and(verdict = 'broken')
    """)
    repeat = [r[0] for r in cur.fetchall()]
    print(f"\nsummary: {counts} | repeat-404s (2 consecutive sweeps): {len(repeat)}")
    for u in repeat:
        print(f'  RETIRE-CANDIDATE {u}')
    conn.close()
    return 0 if not repeat else 3


if __name__ == '__main__':
    sys.exit(main())
