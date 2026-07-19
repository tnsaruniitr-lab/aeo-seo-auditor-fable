"""
export_snapshots.py — re-export the offline ruleset snapshots from the LIVE
Sieve brain (Railway `sieve` schema) into service/ruleset/*.json.

RUNBOOK (needs the live DB — cannot run offline):
    SIEVE_DB_URL=<railway pg url> python3 service/scripts/export_snapshots.py
    # optional: EXPORT_TABLES=rules,playbooks   (default: all four kinds)
    # optional: EXPORT_DIR=/path/to/ruleset     (default: service/ruleset)
  then: bash tests/run_tests.sh   (the snapshot selftests run against the
  real files), review the diff, update the snapshot date the renderer
  discloses (grep main.py for 'SNAPSHOT ruleset (') if the export date
  moved, and commit the JSONs together with that date bump.

WHY: the 2026-04-21 export predates the curation-lifecycle columns, so the
committed files carry no `status`/`last_verified`/`url_provenance`/
`created_at` — the snapshot ranker (ruleset/ranker.py) tolerates their
absence (rows default to active, freshness renders as unknown) but can only
honor the live path's trust filter and freshness disclosure once a re-export
carries them. This script exports ALL FOUR kinds (rules, principles,
anti_patterns, playbooks) with those fields WHEN the live schema has them.

Rows are NOT status-filtered at export time — deliberately. The by-id maps
keep deprecated rows so the grounding path can distinguish 'deprecated' from
'missing'; the ranker's status gate (status_excluded) makes them unciteable
at retrieval/selection time, mirroring sieve_brain._trust_filter.

Column probing mirrors sieve_brain._optional_cols: every lifecycle column is
selected only when information_schema says it exists, so this script works
against both the current live schema and an older one (absent -> key omitted,
which the ranker reads as honest None/active).
"""

from __future__ import annotations

import json
import os
import sys

import psycopg2
from psycopg2.extras import RealDictCursor

DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')

_HERE = os.path.dirname(os.path.abspath(__file__))
EXPORT_DIR = os.getenv('EXPORT_DIR') or os.path.normpath(
    os.path.join(_HERE, '..', 'ruleset'))

# table -> (snapshot filename, [(db expression or column, export key), ...])
# Text-field renames match the committed snapshots (then_logic -> then_action).
_KINDS = {
    'rules': ('rules-snapshot.json', [
        ('name', 'name'), ('if_condition', 'if_condition'),
        ('then_logic', 'then_action'),
        ('confidence_score', 'confidence_score'),
    ]),
    'principles': ('principles-snapshot.json', [
        ('title', 'title'), ('statement', 'statement'),
        ('explanation', 'explanation'),
        ('confidence_score', 'confidence_score'),
    ]),
    'anti_patterns': ('anti-patterns-snapshot.json', [
        ('title', 'title'), ('description', 'description'),
        ('risk_level', 'risk_level'),
    ]),
    'playbooks': ('playbooks-snapshot.json', [
        ('name', 'name'), ('summary', 'summary'),
        ('use_when', 'use_when'), ('avoid_when', 'avoid_when'),
        ('confidence_score', 'confidence_score'),
    ]),
}

# Lifecycle/provenance columns carried for ALL kinds when the schema has them.
_LIFECYCLE = ('status', 'last_verified', 'created_at', 'url_provenance',
              'superseded_by')


def _cols(conn, table: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='sieve' AND table_name=%s", (table,))
        return {r[0] for r in cur.fetchall()}


def _export_table(conn, table: str) -> list:
    have = _cols(conn, table)
    filename, fields = _KINDS[table]
    sel = ['t.id AS id']
    for col, key in fields:
        if col in have:
            sel.append(f't.{col} AS {key}')
    for col in ('domain_tag', 'source_org'):
        if col in have:
            sel.append(f't.{col} AS {col}')
    for col in _LIFECYCLE:
        if col in have:
            cast = '::text' if col in ('last_verified', 'created_at') else ''
            sel.append(f't.{col}{cast} AS {col}')

    # source_url/source_title resolution mirrors sieve_brain._select_head:
    # playbooks carry their own; the core kinds fall back to the documents
    # join (document_id FK when present, legacy source_refs_json regex else).
    join = ''
    if table == 'playbooks' or not ({'document_id', 'source_refs_json'} & have):
        if 'source_url' in have:
            sel.append("NULLIF(t.source_url,'') AS source_url")
        if 'source_title' in have:
            sel.append("NULLIF(t.source_title,'') AS source_title")
    else:
        on = ('d.id = t.document_id' if 'document_id' in have else
              "d.id = NULLIF(substring(t.source_refs_json from '\\d+'), '')")
        join = f' LEFT JOIN sieve.documents d ON {on}'
        dcols = _cols(conn, 'documents')
        own = "NULLIF(t.source_url,'')" if 'source_url' in have else 'NULL'
        durl = 'd.source_url' if 'source_url' in dcols else 'NULL'
        sel.append(f'COALESCE({own}, {durl}) AS source_url')
        town = "NULLIF(t.source_title,'')" if 'source_title' in have else 'NULL'
        dtitle = 'd.title' if 'title' in dcols else 'NULL'
        sel.append(f'COALESCE({town}, {dtitle}) AS source_title')

    sql = (f"SELECT {', '.join(sel)} FROM sieve.{table} t{join} "
           f"ORDER BY t.id")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        return [dict(r) for r in cur.fetchall()]


def main() -> int:
    if not DB_URL:
        print('ERROR: set SIEVE_DB_URL (or DATABASE_URL) — this export needs '
              'the live brain; see the runbook in this file.', file=sys.stderr)
        return 2
    tables = [t.strip() for t in
              os.getenv('EXPORT_TABLES', ','.join(_KINDS)).split(',')
              if t.strip() in _KINDS]
    conn = psycopg2.connect(DB_URL, connect_timeout=10)
    try:
        for table in tables:
            rows = _export_table(conn, table)
            filename = _KINDS[table][0]
            path = os.path.join(EXPORT_DIR, filename)
            with open(path, 'w') as f:
                json.dump(rows, f, ensure_ascii=False,
                          separators=(',', ':'), default=str)
            n_status = sum(1 for r in rows if r.get('status'))
            n_lv = sum(1 for r in rows if r.get('last_verified'))
            print(f'{table}: {len(rows)} rows -> {path} '
                  f'(status on {n_status}, last_verified on {n_lv})')
    finally:
        conn.close()
    print('Done. Re-run bash tests/run_tests.sh and commit the JSONs '
          'with the renderer snapshot-date bump (see runbook).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
