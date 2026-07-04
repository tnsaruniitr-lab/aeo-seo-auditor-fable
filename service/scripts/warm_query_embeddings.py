#!/usr/bin/env python3
"""
warm_query_embeddings.py — pre-pin query embeddings for every canonical check.

Pins the query vector for all brain-mappings check_ids into
public.check_query_embeddings (see sieve_brain._pinned_query_vec), so the
very first audit after a deploy is already deterministic and needs no
OpenAI calls for canonical checks. Safe to re-run (ON CONFLICT DO NOTHING);
re-run after changing SIEVE_EMBED_MODEL or _query_for().

Usage:
    OPENAI_API_KEY=... DATABASE_URL=<pg url> python3 warm_query_embeddings.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def main() -> int:
    import psycopg2
    import sieve_brain
    from check_vocab import _load_registry

    if not sieve_brain.live_enabled():
        print('SIEVE_LIVE/DATABASE_URL not configured', file=sys.stderr)
        return 2
    if not os.getenv('OPENAI_API_KEY'):
        print('OPENAI_API_KEY not set — nothing to warm', file=sys.stderr)
        return 2

    canonical, _ = _load_registry()
    conn = psycopg2.connect(sieve_brain.SIEVE_DB_URL, connect_timeout=10)
    conn.autocommit = True
    pinned = failed = 0
    try:
        for cid in sorted(canonical):
            query = sieve_brain._query_for(cid)
            lit = sieve_brain._pinned_query_vec(conn, query)
            if lit:
                pinned += 1
            else:
                failed += 1
                print(f'  ! no vector for {cid}', file=sys.stderr)
    finally:
        conn.close()
    print(f'warmed {pinned}/{len(canonical)} canonical check queries'
          f'{f" ({failed} failed)" if failed else ""}')
    return 0 if not failed else 1


if __name__ == '__main__':
    sys.exit(main())
