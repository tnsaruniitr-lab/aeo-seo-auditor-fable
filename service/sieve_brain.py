"""
sieve_brain.py — LIVE citation retrieval from the Sieve brain (Railway `sieve` schema).

ADDITIVE + SAFE. This is an OPT-IN alternate source for query_brain's citations:
  - Enabled only when SIEVE_LIVE=1 AND a DB URL is configured.
  - On ANY error it returns None, and the caller falls back to the snapshot ranker.
    The static path is never touched.

Retrieval is LAYERED (best signal first, each degrades to the next):
  1. SEMANTIC (vector): embed the check query (OpenAI text-embedding-3-small, 1536-d)
     and cosine-search rules + principles + anti_patterns (needs embeddings + key).
  2. KEYWORD (FTS): full-text over the same three tables (no key needed).
  3. -> None: caller uses the snapshot ranker.
Candidates from whichever layer are re-ranked by AUTHORITY TIER -> confidence ->
match score, and each carries source_org + source_url + last_verified.

Stdlib + psycopg2 (dependency). OpenAI is optional (only for the semantic layer).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger('audit.sieve')

SIEVE_LIVE = os.getenv('SIEVE_LIVE', '') in ('1', 'true', 'True')
SIEVE_DB_URL = os.getenv('SIEVE_DB_URL') or os.getenv('DATABASE_URL')
EMBED_MODEL = os.getenv('SIEVE_EMBED_MODEL', 'text-embedding-3-small')
# SIEVE_STRICT=1: a live-brain failure FAILS the audit instead of silently
# degrading to the repo snapshot. Default off — a transient Postgres hiccup
# must not break AnswerMonk's audits — but the option exists once freshness
# monitoring has proven the DB stable.
SIEVE_STRICT = os.getenv('SIEVE_STRICT', '') in ('1', 'true', 'True')

# Phase 1 — retrieval floor. A citation must be RELEVANT, not merely
# authoritative. Below the cosine floor a vector hit is noise; below the
# ts_rank floor an FTS hit is noise. Env-tunable so a labelled sweep can
# calibrate without a code change. When nothing clears the floor,
# live_citations returns None and the caller falls to the snapshot path —
# "we found nothing relevant" is now representable.
SIEVE_MIN_RELEVANCE = float(os.getenv('SIEVE_MIN_RELEVANCE', '0.28'))   # cosine similarity
SIEVE_MIN_TS_RANK = float(os.getenv('SIEVE_MIN_TS_RANK', '0.0'))        # ts_rank (@@ already gates)
# Relevance is PRIMARY, authority a bounded tiebreak WITHIN a band: a tier-1
# row far away can no longer outrank a tier-2 row that is on-topic. Rows whose
# relevance falls in the same band are ordered by tier; rows in a better band
# always win regardless of tier.
RELEVANCE_BAND = float(os.getenv('SIEVE_RELEVANCE_BAND', '0.10'))


class SieveLiveError(RuntimeError):
    """Live sieve DB was required (SIEVE_STRICT) but unavailable."""

TIER_ICONS = {1: '🥇', 2: '🥈', 3: '🥉', 4: '📄', 5: '📝'}

# Which brain tables to search + how their columns map to a uniform citation.
_TABLE_CFG = {
    'rules':         {'kind': 'rule',      'title': 'name',  't1': 'if_condition', 't2': 'then_logic',  'conf': 'confidence_score', 'risk': None},
    'principles':    {'kind': 'principle', 'title': 'title', 't1': 'statement',    't2': 'explanation', 'conf': 'confidence_score', 'risk': None},
    'anti_patterns': {'kind': 'ap',        'title': 'title', 't1': 'description',  't2': None,          'conf': None,               'risk': 'risk_level'},
}


def live_enabled() -> bool:
    return bool(SIEVE_LIVE and SIEVE_DB_URL)


# ---------------------------------------------------------------------------
# Source-org canonicalization + tiering
# The brain stores a mix of canonical names ("Google", "Schema.org") and raw
# domains ("backlinko.com"). Canonicalize BEFORE tiering so authoritative
# sources don't sink to tier 5 (the latent tier bug, now active).
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
    return 4 if (org and '.' in org and org.strip().lower() != 'personal blog') else _DEFAULT_TIER


# ---------------------------------------------------------------------------
# check_id -> search query
# ---------------------------------------------------------------------------

_SECTION_HINT = {
    'A': 'technical seo crawl index', 'B': 'performance core web vitals speed',
    'C': 'on-page content heading title', 'D': 'schema structured data json-ld',
    'E': 'aeo discovery crawler sitemap', 'F': 'aeo extraction answer citation entity',
    'G': 'trust author credentials e-e-a-t', 'H': 'competitive comparison',
    'I': 'geo brand presence ai overview', 'J': 'entity consistency nap sameas',
}


def _query_for(check_id: str, evidence: Optional[str] = None) -> str:
    """Build the retrieval query for a check.

    The check-name body + section hint anchors the topic; the finding's OWN
    evidence (what the auditor actually observed on THIS page) leads the query
    so two findings on the same check with different problems retrieve
    different rules. Evidence is normalized (lowercased, whitespace-collapsed,
    truncated) so identical observations pin to the same embedding.
    """
    clean = check_id.split(':', 1)[-1]
    m = re.match(r'^([A-Z])\d', clean)
    hint = _SECTION_HINT.get(m.group(1), '') if m else ''
    body = re.sub(r'^[A-Z]\d+[a-z]?_', '', clean).replace('_', ' ').strip()
    base = (body + ' ' + hint).strip() or clean
    if evidence:
        ev = re.sub(r'\s+', ' ', str(evidence)).strip().lower()[:200]
        if ev:
            return (ev + ' ' + base).strip()
    return base


def _relevance_floor_for(layer: Optional[str]) -> float:
    """Per-layer noise floor: cosine vs ts_rank live on different scales."""
    return SIEVE_MIN_TS_RANK if layer == 'fts' else SIEVE_MIN_RELEVANCE


def _rank_and_floor(cites: List[Dict[str, Any]], max_citations: int) -> List[Dict[str, Any]]:
    """Drop off-topic citations, then rank RELEVANCE-first with authority as a
    bounded tiebreak within a relevance band. Pure + deterministic (id is the
    final key) so it is unit-testable without a DB."""
    kept = [c for c in cites
            if float(c.get('relevance') or 0.0) >= _relevance_floor_for(c.get('retrieval_layer'))]

    def sortkey(c: Dict[str, Any]):
        rel = float(c.get('relevance') or 0.0)
        # More-relevant band sorts first (negative so ASC puts best band first).
        band = -int(rel / RELEVANCE_BAND) if RELEVANCE_BAND > 0 else 0
        try:
            conf = float(c.get('confidence_score') or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        return (band, c.get('tier', 5), -round(conf, 1), -round(rel, 4),
                c.get('url_spec', 2), _prov_rank(c), c.get('kind') or '', str(c.get('id') or ''))

    kept.sort(key=sortkey)
    return kept[:max_citations]


# ---------------------------------------------------------------------------
# Embedding (semantic layer) — optional
# ---------------------------------------------------------------------------

def _embed_query(text: str) -> Optional[list]:
    """Embed the query in the same space as the corpus. None if unavailable."""
    if not os.getenv('OPENAI_API_KEY'):
        return None
    try:
        from openai import OpenAI
        client = OpenAI()
        r = client.embeddings.create(model=EMBED_MODEL, input=[text[:6000]])
        return r.data[0].embedding
    except Exception as e:
        log.info('query embedding unavailable (%s) — using FTS', e)
        return None


def _vec_literal(v) -> str:
    return '[' + ','.join(f'{x:.7f}' for x in v) + ']'


# ---------------------------------------------------------------------------
# Pinned query embeddings (determinism)
# OpenAI embeddings drift call-to-call (~1e-4 per component, survives the
# 7-decimal rounding), which made retrieval non-reproducible run-to-run.
# The FIRST embedding computed for a query text is stored in the auditor's
# own schema (public.check_query_embeddings — NOT the sieve brain, which the
# auditor must never write) and reused forever, so the same check searches
# with the same vector every run. Also removes the per-audit embedding cost
# after warm-up. ON CONFLICT + re-read converges concurrent audits that
# raced to embed the same query onto a single winner.
# ---------------------------------------------------------------------------

_EMBED_MEMO: Dict[str, str] = {}
_EMBED_TABLE_DDL = """
    CREATE TABLE IF NOT EXISTS public.check_query_embeddings (
        query_hash        text PRIMARY KEY,
        model             text NOT NULL,
        query_text        text NOT NULL,
        embedding_literal text NOT NULL,
        created_at        timestamptz NOT NULL DEFAULT now()
    )
"""
_embed_table_ready = False


def _pinned_query_vec(conn, text: str) -> Optional[str]:
    """Vector literal for the query, pinned for reproducibility. Falls back to
    a fresh (unpinned) embedding if the cache table is unreachable; None means
    no semantic layer (caller uses FTS)."""
    global _embed_table_ready
    import hashlib
    key = hashlib.md5(f'{EMBED_MODEL}:{text}'.encode()).hexdigest()
    if key in _EMBED_MEMO:
        return _EMBED_MEMO[key]
    got = None
    try:
        with conn.cursor() as cur:
            if not _embed_table_ready:
                cur.execute(_EMBED_TABLE_DDL)
                _embed_table_ready = True
            cur.execute('SELECT embedding_literal FROM public.check_query_embeddings'
                        ' WHERE query_hash = %s', (key,))
            row = cur.fetchone()
            got = row[0] if row else None
        if got is None:
            vec = _embed_query(text)
            if vec is None:
                return None
            lit = _vec_literal(vec)
            with conn.cursor() as cur:
                cur.execute(
                    'INSERT INTO public.check_query_embeddings'
                    ' (query_hash, model, query_text, embedding_literal)'
                    ' VALUES (%s, %s, %s, %s) ON CONFLICT (query_hash) DO NOTHING',
                    (key, EMBED_MODEL, text, lit))
                # A concurrent audit may have won the insert race — always
                # use the stored row so every process agrees.
                cur.execute('SELECT embedding_literal FROM public.check_query_embeddings'
                            ' WHERE query_hash = %s', (key,))
                row = cur.fetchone()
                got = row[0] if row else lit
    except Exception as e:
        log.info('pinned-embedding cache unavailable (%s) — using fresh embedding', e)
        vec = _embed_query(text)
        return _vec_literal(vec) if vec else None
    if got:
        _EMBED_MEMO[key] = got
    return got


# ---------------------------------------------------------------------------
# SQL builders (uniform citation shape across the 3 tables)
# ---------------------------------------------------------------------------

def _conf_expr(cfg) -> str:
    if cfg['conf']:
        return f"t.{cfg['conf']}"
    # anti_patterns: map risk_level -> a confidence-ish number for ranking
    return ("CASE lower(coalesce(t.risk_level,'')) WHEN 'high' THEN '0.9' "
            "WHEN 'low' THEN '0.65' ELSE '0.8' END")


# Columns that may not exist yet on a given deployment (added by sieve-ingest
# migrations). Probed PER CONNECTION (one ~1ms information_schema lookup per
# audit query) — a process-lifetime cache would keep a migration that lands
# mid-process (e.g. Monday's url_provenance) invisible until redeploy, and a
# dropped column would poison BOTH the vector and FTS paths identically,
# silently removing whole tables from retrieval.
def _optional_cols(conn) -> Dict[str, set]:
    out: Dict[str, set] = {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT table_name, column_name FROM information_schema.columns
            WHERE table_schema='sieve'
              AND column_name IN ('url_provenance', 'status', 'contested',
                                  'superseded_by', 'document_id')
        """)
        for t, c in cur.fetchall():
            out.setdefault(t, set()).add(c)
    return out


def _select_head(cfg, score_sql: str, cols: Optional[set] = None) -> str:
    t2 = f"t.{cfg['t2']}" if cfg['t2'] else "''"
    cols = cols or set()
    prov = "t.url_provenance" if 'url_provenance' in cols else "NULL"
    # S4: join documents on the real document_id FK when the column exists;
    # fall back to the legacy source_refs_json regex only on an older DB.
    if 'document_id' in cols:
        doc_join = "d.id = t.document_id"
    else:
        doc_join = "d.id = NULLIF(substring(t.source_refs_json from '\\d+'), '')"
    # D2: last_verified and created_at are DISTINCT columns. A never-re-verified
    # rule (last_verified NULL) must NOT borrow created_at and display as
    # 'verified' — that fabricates freshness. created_at rides through only as an
    # honest 'added' date.
    return f"""
        SELECT t.id, t.{cfg['title']} AS title, t.{cfg['t1']} AS text1, {t2} AS text2,
               t.domain_tag, {_conf_expr(cfg)} AS conf, t.source_org,
               COALESCE(NULLIF(t.source_url,''), d.source_url) AS source_url,
               t.last_verified::text AS last_verified,
               t.created_at AS created_at,
               {prov} AS url_provenance,
               {score_sql} AS score
        FROM sieve.{{table}} t
        LEFT JOIN sieve.documents d
          ON {doc_join}
    """


def _trust_filter(cols: Optional[set]) -> str:
    """Only TRUSTED, non-dead, uncontested guidance is citeable. The live sieve
    taxonomy is active/candidate/deprecated/rejected (the old 'retired'/'superseded'
    string statuses are gone — supersession is now the superseded_by FK). Each
    clause is guarded by _optional_cols so this degrades cleanly on an older DB.
    Excludes: deprecated+rejected (kept retired/superseded for back-compat),
    contested='t' rows, and any row with superseded_by set."""
    if not cols:
        return ""
    clauses = []
    if 'status' in cols:
        clauses.append(
            "coalesce(t.status,'active') NOT IN ('deprecated','rejected','retired','superseded')")
    if 'contested' in cols:
        clauses.append("coalesce(t.contested,'f') <> 't'")
    if 'superseded_by' in cols:
        clauses.append("t.superseded_by IS NULL")
    return (" AND " + " AND ".join(clauses)) if clauses else ""


# Back-compat alias — some call sites / tests reference the old name.
_status_filter = _trust_filter


def _corpus_model(conn) -> Optional[str]:
    """The embedding model the corpus was actually embedded with, recorded by
    embed_brain into sieve.embedding_meta. None if unrecorded (older corpus) —
    in which case we cannot detect a space mismatch and proceed as today."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT model FROM sieve.embedding_meta "
                        "WHERE table_name = 'rules' ORDER BY embedded_at DESC LIMIT 1")
            row = cur.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _search_vector(cur, table, cfg, qvec_literal, k, cols=None, min_rel=None):
    """Cosine search WITH a similarity floor: rows further than the floor are
    excluded in-DB before LIMIT, so the top-k is drawn only from relevant rows
    (never the nearest-of-the-irrelevant)."""
    floor = SIEVE_MIN_RELEVANCE if min_rel is None else min_rel
    sql = _select_head(cfg, '1 - (t.embedding <=> %s::vector)', cols).format(table=table) + \
        " WHERE t.embedding IS NOT NULL" + _status_filter(cols) + \
        " AND (1 - (t.embedding <=> %s::vector)) >= %s" + \
        " ORDER BY t.embedding <=> %s::vector LIMIT %s"
    cur.execute(sql, (qvec_literal, qvec_literal, floor, qvec_literal, k))
    return cur.fetchall()


def _search_fts(cur, table, cfg, ts_query, k, cols=None):
    tsv = (f"to_tsvector('english', coalesce(t.{cfg['title']},'')||' '||"
           f"coalesce(t.{cfg['t1']},'')" + (f"||' '||coalesce(t.{cfg['t2']},'')" if cfg['t2'] else '') + ")")
    # id tie-break: ts_rank produces frequent exact ties, and an untied LIMIT
    # boundary changed citation IDENTITY run-to-run (ids are digit-text; the
    # bigint cast gives numeric order). The vector path needs no tie-break:
    # with a pinned query vector the HNSW scan is deterministic, and a
    # secondary sort key would defeat the index.
    sql = _select_head(cfg, f"ts_rank({tsv}, websearch_to_tsquery('english', %s))", cols).format(table=table) + \
        f" WHERE {tsv} @@ websearch_to_tsquery('english', %s)" + _status_filter(cols) + \
        " ORDER BY score DESC, t.id::bigint ASC LIMIT %s"
    cur.execute(sql, (ts_query, ts_query, k))
    return cur.fetchall()


def _row_to_cite(r) -> Dict[str, Any]:
    org = r.get('source_org')
    t = tier_of(org)
    try:
        conf = float(r.get('conf') or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    return {
        'id': r.get('id'), 'kind': r.get('kindtag', 'rule'),
        'tier': t, 'tier_icon': TIER_ICONS.get(t, '📝'),
        'source_org': canon_org(org) or org, 'source_org_raw': org,
        'source_url': r.get('source_url'),
        'name': r.get('title'),
        'confidence_score': str(round(conf, 2)),
        'if_condition': (r.get('text1') or '')[:500],
        'then_action': (r.get('text2') or '')[:500],
        'domain_tag': r.get('domain_tag'),
        # D2 — honest freshness: last_verified is emitted ONLY when the rule was
        # genuinely re-verified; a NULL stays None (never borrows created_at).
        'last_verified': str(r.get('last_verified'))[:10] if r.get('last_verified') else None,
        'added': str(r.get('created_at'))[:10] if r.get('created_at') else None,
        'relevance': round(float(r.get('score') or 0.0), 4),
        'url_spec': _url_spec(r.get('source_url')),
        'url_provenance': r.get('url_provenance'),
        'retrieval_layer': r.get('_layer', 'vector'),
        'from': 'sieve-live',
    }


def _prov_rank(c: Dict[str, Any]) -> int:
    """extracted (0) beats unknown/legacy (1) beats neighbor-inferred (2):
    a URL the rule was actually extracted from is honest provenance; a
    similarity-adopted neighbor URL is a hint, not a receipt."""
    p = c.get('url_provenance')
    return 0 if p == 'extracted' else (2 if p == 'neighbor-inferred' else 1)


# Known generic hubs (not exact pages) — a citation should prefer a real page over these.
_GENERIC_HUBS = {
    'developers.google.com/search', 'developers.google.com/search/docs',
    'docs.perplexity.ai', 'platform.openai.com/docs', 'www.bing.com/webmasters/help',
}


def _url_spec(u: Optional[str]) -> int:
    """Tiebreak rank: 0 = specific page, 1 = generic hub / bare domain, 2 = no url."""
    if not u:
        return 2
    path = u.split('://', 1)[-1].rstrip('/')
    if path in _GENERIC_HUBS or '/' not in path:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

def live_citations(check_id: str, page_type: Optional[str] = None,
                   industry: Optional[str] = None,
                   max_citations: int = 3,
                   evidence: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """Return citation dicts for a check from the live brain, or None so the
    caller falls back to the snapshot path. Never raises — EXCEPT under
    SIEVE_STRICT, where a live-brain failure raises SieveLiveError so the
    audit fails loudly instead of silently citing the April snapshot.

    Retrieval is EVIDENCE-based (the finding's observation leads the query),
    RELEVANCE-floored (off-topic hits are dropped, not the nearest-of-noise),
    and ranked relevance-first with authority a bounded tiebreak. If the
    corpus embedding model differs from SIEVE_EMBED_MODEL the vector layer is
    refused (falls to FTS) rather than cosine-comparing incompatible spaces."""
    if not live_enabled():
        return None
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception:
        return None

    query = _query_for(check_id, evidence)
    words = list(dict.fromkeys(re.findall(r'[a-zA-Z0-9][a-zA-Z0-9-]{2,}', query.lower())))
    ts_query = ' OR '.join(words) or query
    per_table_k = max(8, max_citations * 4)

    rows: List[Dict[str, Any]] = []
    degraded: Optional[str] = None
    try:
        conn = psycopg2.connect(SIEVE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        try:
            cols_by_table = _optional_cols(conn)
            # Embedding-space guard: a same-dimension model swap (e.g. ada-002
            # vs 3-small, both 1536-d) yields a VALID `<=>` across incompatible
            # spaces — no error, wrong neighbours cited as authoritative. Refuse
            # the vector layer on a recorded mismatch.
            corpus_model = _corpus_model(conn)
            if corpus_model and corpus_model != EMBED_MODEL:
                if SIEVE_STRICT:
                    raise SieveLiveError(
                        f'embedding-space mismatch: corpus={corpus_model} runtime={EMBED_MODEL}')
                log.warning('embedding model mismatch (corpus=%s runtime=%s) — using FTS',
                            corpus_model, EMBED_MODEL)
                degraded = 'embed_model_mismatch'
                qvec_lit = None
            else:
                qvec_lit = _pinned_query_vec(conn, query)    # None => FTS-only
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for table, cfg in _TABLE_CFG.items():
                    cols = cols_by_table.get(table, set())
                    found = []
                    layer = 'vector'
                    try:
                        if qvec_lit:
                            found = _search_vector(cur, table, cfg, qvec_lit, per_table_k, cols)
                        else:
                            layer = 'fts'
                            found = _search_fts(cur, table, cfg, ts_query, per_table_k, cols)
                    except Exception as te:
                        # e.g. no embedding column yet -> fall back to FTS for this table
                        log.info('vector search on %s failed (%s), trying FTS', table, te)
                        try:
                            layer = 'fts'
                            found = _search_fts(cur, table, cfg, ts_query, per_table_k, cols)
                        except Exception:
                            found = []
                    for r in found:
                        r['kindtag'] = cfg['kind']
                        r['_layer'] = layer
                    rows += found
        finally:
            conn.close()
    except Exception as e:
        if SIEVE_STRICT:
            raise SieveLiveError(f'live brain required (SIEVE_STRICT) but failed: {e}')
        log.warning('live brain query failed (%s) — falling back to snapshot', e)
        return None

    if not rows:
        return None

    cites = [_row_to_cite(r) for r in rows]
    if degraded:
        for c in cites:
            c['retrieval_degraded'] = degraded

    # RELEVANCE-first, floored ranking (Phase 1). Off-topic hits are dropped;
    # authority only reorders WITHIN a relevance band. If nothing clears the
    # floor, return None so the caller falls to the snapshot path rather than
    # emitting confident-but-irrelevant tier-1 citations.
    ranked = _rank_and_floor(cites, max_citations)
    return ranked or None


def stats() -> Dict[str, Any]:
    """Liveness/coverage probe for /readyz."""
    if not live_enabled():
        return {'live': False, 'reason': 'SIEVE_LIVE off or no DB url'}
    try:
        import psycopg2
        conn = psycopg2.connect(SIEVE_DB_URL, connect_timeout=8)
        conn.autocommit = True
        out = {'live': True, 'semantic': bool(os.getenv('OPENAI_API_KEY'))}
        for t in _TABLE_CFG:
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT count(*), count(embedding) FROM sieve.{t}")
                    total, emb = cur.fetchone()
                    out[t] = {'rows': total, 'embedded': emb}
            except Exception:
                with conn.cursor() as cur:  # embedding column not present yet
                    conn.rollback()
                    cur.execute(f"SELECT count(*) FROM sieve.{t}")
                    out[t] = {'rows': cur.fetchone()[0], 'embedded': 0}
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM public.check_query_embeddings")
                out['pinned_query_embeddings'] = cur.fetchone()[0]
        except Exception:
            conn.rollback()
            out['pinned_query_embeddings'] = 0
        # Freshness: THE number that says whether the brain is being fed.
        # stale_days > the ingest cadence (7d) means the weekly loop is broken —
        # this is the dependency-free watchdog for the whole freshness chain.
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT max(last_verified)::date::text,
                           EXTRACT(day FROM now() - max(last_verified))::int
                    FROM sieve.rules
                """)
                verified_through, stale_days = cur.fetchone()
                out['verified_through'] = verified_through
                out['stale_days'] = stale_days
                out['stale'] = bool(stale_days is not None and stale_days > 14)
        except Exception:
            conn.rollback()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT run_id, started_at::date::text, status "
                            "FROM sieve.ingest_runs ORDER BY run_id DESC LIMIT 1")
                row = cur.fetchone()
                if row:
                    out['last_ingest_run'] = {'run_id': row[0], 'date': row[1],
                                              'status': row[2]}
        except Exception:
            conn.rollback()  # ingest control tables not present on this DB
        conn.close()
        return out
    except Exception as e:
        return {'live': False, 'error': f'{type(e).__name__}: {e}'}
