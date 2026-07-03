"""
sieve_brain.py — LIVE citation retrieval from the Sieve brain (Railway `sieve` schema).

ADDITIVE + SAFE. This is an OPT-IN alternate source for query_brain's citations:
  - Enabled only when SIEVE_LIVE=1 AND a DB URL is configured.
  - On ANY error (no DB, no schema, query failure) it returns None, and the caller
    falls back to the existing snapshot ranker. The static path is never touched.

Why retrieval instead of the old id-map: the fresh brain (rules=23k, ids differ
from the 4,980-row snapshot) can't be reached by the snapshot's hardcoded
check_id→rule_id mapping. So we retrieve by full-text over the rule text, then
rank by AUTHORITY TIER → confidence → relevance, and resolve each rule's source
URL via source_refs_json → documents.source_url. Output shape matches
ranker.select_citations so nothing downstream changes.

Stdlib + psycopg2 (already a dependency).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger('audit.sieve')

SIEVE_LIVE = os.getenv('SIEVE_LIVE', '') in ('1', 'true', 'True')
# Reuse the auditor's own Postgres (same instance, `sieve` schema) unless a
# dedicated brain URL is provided.
SIEVE_DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')

TIER_ICONS = {1: '🥇', 2: '🥈', 3: '🥉', 4: '📄', 5: '📝'}


def live_enabled() -> bool:
    return bool(SIEVE_LIVE and SIEVE_DB_URL)


# ---------------------------------------------------------------------------
# Source-org canonicalization + tiering
# The brain stores a mix of canonical names ("Google", "Schema.org") and raw
# domains ("backlinko.com", "ycombinator.com"). Canonicalize BEFORE tiering so
# authoritative sources don't sink to tier 5 (the latent tier bug, now active).
# ---------------------------------------------------------------------------

_CANON = {
    'google search central': 'Google', 'google': 'Google',
    'developers.google.com': 'Google', 'support.google.com': 'Google',
    'schema.org': 'Schema.org',
    'bing webmaster tools': 'Bing', 'bing.com': 'Bing', 'bing': 'Bing',
    'w3c': 'W3C', 'w3.org': 'W3C',
    'mdn web docs': 'MDN', 'developer.mozilla.org': 'MDN', 'mozilla': 'MDN',
    'web.dev': 'web.dev',
    'perplexity': 'Perplexity', 'perplexity.ai': 'Perplexity', 'docs.perplexity.ai': 'Perplexity',
    'openai': 'OpenAI', 'platform.openai.com': 'OpenAI',
    'anthropic': 'Anthropic',
    'backlinko.com': 'Backlinko', 'backlinko': 'Backlinko',
    'moz.com': 'Moz', 'moz': 'Moz',
    'ahrefs.com': 'Ahrefs', 'ahrefs': 'Ahrefs',
    'semrush.com': 'Semrush', 'semrush': 'Semrush',
    'search engine land': 'Search Engine Land', 'searchengineland.com': 'Search Engine Land',
    'search engine journal': 'Search Engine Journal', 'searchenginejournal.com': 'Search Engine Journal',
}
# Authority tiers keyed on canonical names.
_TIER_1 = {'Google', 'Schema.org', 'Bing', 'W3C', 'web.dev', 'MDN', 'Perplexity', 'OpenAI', 'Anthropic'}
_TIER_2 = {'Backlinko', 'Ahrefs', 'Semrush', 'Moz'}
_TIER_3 = {'Search Engine Land', 'Search Engine Journal'}
_DEFAULT_TIER = 5


def canon_org(org: Optional[str]) -> str:
    if not org:
        return ''
    key = org.strip().lower()
    if key in _CANON:
        return _CANON[key]
    # strip www. and try the bare domain
    key2 = re.sub(r'^www\.', '', key)
    if key2 in _CANON:
        return _CANON[key2]
    return org.strip()


def tier_of(org: Optional[str]) -> int:
    c = canon_org(org)
    if c in _TIER_1:
        return 1
    if c in _TIER_2:
        return 2
    if c in _TIER_3:
        return 3
    # unknown authoritative-ish domains vs the "Personal Blog" long tail
    return 4 if (org and '.' in org and org.strip().lower() != 'personal blog') else _DEFAULT_TIER


# ---------------------------------------------------------------------------
# check_id → search query
# ---------------------------------------------------------------------------

_SECTION_HINT = {
    'A': 'technical seo crawl index', 'B': 'performance core web vitals speed',
    'C': 'on-page content heading title', 'D': 'schema structured data json-ld',
    'E': 'aeo discovery crawler sitemap', 'F': 'aeo extraction answer citation entity',
    'G': 'trust author credentials e-e-a-t', 'H': 'competitive comparison',
    'I': 'geo brand presence ai overview', 'J': 'entity consistency nap sameas',
}


def _query_for(check_id: str) -> str:
    """Turn 'D6_required_fields' into a search query 'required fields' + a
    section-topic hint, so full-text retrieval finds relevant rules."""
    clean = check_id.split(':', 1)[-1]
    m = re.match(r'^([A-Z])\d', clean)
    hint = _SECTION_HINT.get(m.group(1), '') if m else ''
    # drop the leading code token, split words
    body = re.sub(r'^[A-Z]\d+[a-z]?_', '', clean).replace('_', ' ').strip()
    return (body + ' ' + hint).strip() or clean


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def live_citations(check_id: str, page_type: Optional[str] = None,
                   industry: Optional[str] = None,
                   max_citations: int = 3) -> Optional[List[Dict[str, Any]]]:
    """Return citation dicts for a check from the live brain, or None to signal
    the caller to fall back to the snapshot path. Never raises."""
    if not live_enabled():
        return None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception:
        return None

    query = _query_for(check_id)
    # websearch_to_tsquery ANDs bare words (too strict → 0 hits). OR the terms
    # so partial matches are found, then ts_rank orders by how well each matches.
    words = list(dict.fromkeys(re.findall(r'[a-zA-Z0-9][a-zA-Z0-9-]{2,}', query.lower())))
    ts_query = ' OR '.join(words) or query
    try:
        conn = psycopg2.connect(SIEVE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Candidate pool by full-text relevance; re-ranked by tier below.
                cur.execute(
                    """
                    SELECT r.id, r.name, r.if_condition, r.then_logic, r.domain_tag,
                           r.confidence_score, r.source_org, r.created_at,
                           r.source_refs_json,
                           d.source_url, d.title AS doc_title,
                           ts_rank(
                             to_tsvector('english',
                               coalesce(r.name,'')||' '||coalesce(r.if_condition,'')||' '||coalesce(r.then_logic,'')),
                             websearch_to_tsquery('english', %s)
                           ) AS relevance
                    FROM sieve.rules r
                    LEFT JOIN sieve.documents d
                      ON d.id = NULLIF(substring(r.source_refs_json from '\\d+'), '')
                    WHERE to_tsvector('english',
                            coalesce(r.name,'')||' '||coalesce(r.if_condition,'')||' '||coalesce(r.then_logic,''))
                          @@ websearch_to_tsquery('english', %s)
                    ORDER BY relevance DESC
                    LIMIT %s
                    """,
                    (ts_query, ts_query, max(24, max_citations * 6)),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning('live brain query failed (%s) — falling back to snapshot', e)
        return None

    if not rows:
        return None

    cites = []
    for r in rows:
        org = r.get('source_org')
        t = tier_of(org)
        try:
            conf = float(r.get('confidence_score') or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        cites.append({
            'id': r.get('id'),
            'kind': 'rule',
            'tier': t,
            'tier_icon': TIER_ICONS.get(t, '📝'),
            'source_org': canon_org(org) or org,
            'source_org_raw': org,
            'source_url': r.get('source_url'),
            'source_doc_title': r.get('doc_title'),
            'name': r.get('name'),
            'confidence_score': str(round(conf, 2)),
            'if_condition': (r.get('if_condition') or '')[:500],
            'then_action': (r.get('then_logic') or '')[:500],
            'domain_tag': r.get('domain_tag'),
            'last_verified': str(r.get('created_at'))[:10] if r.get('created_at') else None,
            'relevance': round(float(r.get('relevance') or 0.0), 4),
            'from': 'sieve-live',
        })

    # Authority-first ranking: tier ASC, then confidence DESC, then relevance DESC,
    # then id ASC (deterministic). Surfaces Google/Schema.org/Perplexity above the
    # 53%-of-corpus "Personal Blog" long tail.
    cites.sort(key=lambda c: (
        c['tier'], -float(c['confidence_score']), -c['relevance'], c['id'] or 0
    ))
    return cites[:max_citations]


def stats() -> Dict[str, Any]:
    """Lightweight liveness/coverage probe for /readyz."""
    if not live_enabled():
        return {'live': False, 'reason': 'SIEVE_LIVE off or no DB url'}
    try:
        import psycopg2
        conn = psycopg2.connect(SIEVE_DB_URL, connect_timeout=8)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM sieve.rules")
            rules = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM sieve.documents WHERE source_url <> '' AND source_url IS NOT NULL")
            docs_with_url = cur.fetchone()[0]
        conn.close()
        return {'live': True, 'rules': rules, 'documents_with_url': docs_with_url}
    except Exception as e:
        return {'live': False, 'error': f'{type(e).__name__}: {e}'}
