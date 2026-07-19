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

# Lane B — numeric-aware query enrichment. Max words _numeric_hints may append
# to a retrieval query (0 disables, hard-capped at 16 so no env value can let
# the hint drown the check topic that anchors the query).
_NUM_HINT_HARD_CAP = 16
try:
    SIEVE_NUM_HINT_TOKENS = max(0, min(int(os.getenv('SIEVE_NUM_HINT_TOKENS', '8')),
                                       _NUM_HINT_HARD_CAP))
except ValueError:
    SIEVE_NUM_HINT_TOKENS = 8


class SieveLiveError(RuntimeError):
    """Live sieve DB was required (SIEVE_STRICT) but unavailable."""

TIER_ICONS = {1: '🥇', 2: '🥈', 3: '🥉', 4: '📄', 5: '📝'}

# Which brain tables to search + how their columns map to a uniform citation.
# playbooks (census cols: name, summary, use_when, avoid_when,
# confidence_score, source_org/url, status) carry no embeddings, so the arm
# is FTS-only ('fts_only'), and no documents join ('no_doc_join') — their
# source_url is their own. use_when → the condition, summary → the action.
_TABLE_CFG = {
    'rules':         {'kind': 'rule',      'title': 'name',  't1': 'if_condition', 't2': 'then_logic',  'conf': 'confidence_score', 'risk': None},
    'principles':    {'kind': 'principle', 'title': 'title', 't1': 'statement',    't2': 'explanation', 'conf': 'confidence_score', 'risk': None},
    'anti_patterns': {'kind': 'ap',        'title': 'title', 't1': 'description',  't2': None,          'conf': None,               'risk': 'risk_level'},
    'playbooks':     {'kind': 'playbook',  'title': 'name',  't1': 'use_when',     't2': 'summary',     'conf': 'confidence_score', 'risk': None,
                      'fts_only': True, 'no_doc_join': True},
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
# Tier 4 — the DELIBERATE practitioner band (growth-domain operators):
# ranks above anonymous tier-5, below vendor-doc tier-3. Code fallback only;
# org-tiers.json is the shared source of truth.
_TIER_4 = {'Y Combinator', 'Reforge', 'a16z', 'First Round Review',
           'For Entrepreneurs', 'Demand Curve', 'Animalz', 'AppsFlyer',
           'ALM Corp', 'Amsive', 'CXL', 'Frase'}
_DEFAULT_TIER = 5

# SINGLE SOURCE: service/ruleset/org-tiers.json carries the canon map + tier
# bands and is shared with the snapshot path (ruleset/ranker.py) so the
# live and snapshot tables cannot drift. The in-code _CANON/_TIER_* above
# stay as the fallback when the file is missing/malformed.
_ORG_TIERS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'ruleset', 'org-tiers.json')
_TIER_MAP: Dict[str, int] = {}
try:
    import json as _json
    with open(_ORG_TIERS_PATH) as _f:
        _shared = _json.load(_f)
    _CANON = {**_CANON, **{str(k).strip().lower(): str(v)
                           for k, v in (_shared.get('canon') or {}).items()}}
    for _band, _orgs in (_shared.get('tiers') or {}).items():
        for _o in (_orgs or []):
            _TIER_MAP[str(_o)] = int(_band)
except Exception:  # noqa: BLE001 — fall back to the in-code tables
    _TIER_MAP = {}


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
    shared = _TIER_MAP.get(c)
    if shared in (1, 2, 3, 4):
        return shared
    if c in _TIER_1:
        return 1
    if c in _TIER_2:
        return 2
    if c in _TIER_3:
        return 3
    if c in _TIER_4:
        return 4
    return 4 if (org and '.' in org and org.strip().lower() != 'personal blog') else _DEFAULT_TIER


def curated_tier(org: Optional[str]) -> int:
    """Tier for GATING — tier_of WITHOUT the dotted-domain heuristic.

    tier_of's fallback lets ANY unrecognized dotted org ('someblog.com')
    display as tier 4; that is a display/tiebreak courtesy, not membership
    in the curated practitioner band. A tier GATE (retrieve_batch's NORM
    slot) must only admit orgs actually resolved through org-tiers.json or
    the in-code fallback sets; everything else is _DEFAULT_TIER (5)."""
    c = canon_org(org)
    shared = _TIER_MAP.get(c)
    if shared in (1, 2, 3, 4):
        return shared
    for band, orgs in ((1, _TIER_1), (2, _TIER_2), (3, _TIER_3), (4, _TIER_4)):
        if c in orgs:
            return band
    return _DEFAULT_TIER


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


# ---------------------------------------------------------------------------
# Numeric-aware query enrichment (Lane B)
#
# Evidence states measurements in whatever unit the probe emitted
# ('LCP: 12,417ms'); rules state thresholds in prose units ('under 3 seconds',
# 'load time'). Neither BM25 nor the embedding reliably bridges '12417ms' →
# 'seconds', so the right rule sits in the candidate pool but loses the cut to
# lexical near-misses. _numeric_hints closes that gap DETERMINISTICALLY (pure
# lexical, no LLM, no I/O): detect measured quantities in the evidence,
# convert to the canonical unit rules speak in, and append the canonical value
# plus the metric's conceptual vocabulary. Output is capped
# (SIEVE_NUM_HINT_TOKENS, default 8 words) and appended AFTER the check topic
# so the hint can never drown it.
# ---------------------------------------------------------------------------

# Number: comma-grouped or plain integer + optional decimals. The lookbehind
# refuses matches inside identifiers/URLs/versions ('page-2s', 'v2', '1.2.3').
_NUM_PAT = r'(?<![\w/.,-])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?'
_RE_TIME = re.compile(_NUM_PAT +
                      r'\s*(milliseconds?|msecs?|ms|minutes?|mins?|seconds?|secs?|s)\b')
_RE_SIZE = re.compile(_NUM_PAT +
                      r'\s*(kilobytes?|kb|megabytes?|mb|gigabytes?|gb|bytes?)\b')
_RE_PCT = re.compile(_NUM_PAT + r'\s*(?:%|percent(?:age)?\b|pct\b)')
_RE_COUNT = re.compile(_NUM_PAT +
                       r'\s+(words?|characters?|chars?|links?|images?'
                       r'|requests?|redirects?|scripts?|elements?)\b')

# Conceptual vocabulary per counted noun (singular) — the words threshold
# rules actually use ('word count', 'redirect chain', 'title ... characters').
_COUNT_VOCAB = {
    'word': ('word', 'count', 'content', 'length'),
    'char': ('character', 'count', 'length'),
    'character': ('character', 'count', 'length'),
    'link': ('link', 'count'),
    'image': ('image', 'count'),
    'request': ('request', 'count', 'http'),
    'redirect': ('redirect', 'chain', 'count'),
    'script': ('script', 'count'),
    'element': ('element', 'count'),
}

# KB multiplier per size-unit prefix character (after unit normalization).
_SIZE_TO_KB = {'b': 1.0 / 1024, 'k': 1.0, 'm': 1024.0, 'g': 1024.0 * 1024}


def _fmt_qty(v: float) -> str:
    """Canonical number token: 1 decimal, integers without '.0'. Pure."""
    r = round(v, 1)
    return str(int(r)) if abs(r - int(r)) < 1e-9 else f'{r:.1f}'


def _match_val(m) -> float:
    return float(m.group(1).replace(',', '') + (m.group(2) or ''))


def _numeric_hints(text: Optional[str], max_tokens: Optional[int] = None) -> List[str]:
    """Deterministic numeric enrichment tokens for an evidence string.

    Detects measured quantities (time in ms/s/min, sizes in bytes/KB/MB/GB,
    percentages, counted nouns), converts each to its canonical unit, and
    returns canonical-value + conceptual-vocabulary words, first-match-per-
    class, in fixed class order (time, size, percent, count), de-duplicated,
    capped at max_tokens (default SIEVE_NUM_HINT_TOKENS; 0 disables). Pure
    function of its arguments — same evidence always yields the same hints.
    """
    if not text:
        return []
    cap = SIEVE_NUM_HINT_TOKENS if max_tokens is None else max_tokens
    try:
        cap = max(0, min(int(cap), _NUM_HINT_HARD_CAP))
    except (TypeError, ValueError):
        cap = SIEVE_NUM_HINT_TOKENS
    if cap == 0:
        return []
    t = re.sub(r'\s+', ' ', str(text)).strip().lower()
    hints: List[str] = []
    seen = set()

    def add(tokens):
        for tok in tokens:
            if len(hints) >= cap:
                return
            if tok not in seen:
                seen.add(tok)
                hints.append(tok)

    m = _RE_TIME.search(t)
    if m:
        v, unit = _match_val(m), m.group(3)
        if unit in ('ms', 'msec', 'msecs', 'millisecond', 'milliseconds'):
            sec = v / 1000.0
        elif unit.startswith('min'):
            sec = v * 60.0
        else:
            sec = v
        add([_fmt_qty(sec), 'seconds', 'load', 'time', 'speed'])

    m = _RE_SIZE.search(t)
    if m:
        kb = _match_val(m) * _SIZE_TO_KB[m.group(3)[0]]
        if kb >= 1024:
            add([_fmt_qty(kb / 1024.0), 'mb', 'megabytes', 'page', 'size', 'weight'])
        else:
            add([_fmt_qty(kb), 'kb', 'kilobytes', 'page', 'size', 'weight'])

    m = _RE_PCT.search(t)
    if m:
        add([_fmt_qty(_match_val(m)), 'percent', 'percentage', 'ratio'])

    m = _RE_COUNT.search(t)
    if m:
        noun = m.group(3)
        noun = noun[:-1] if noun.endswith('s') else noun
        add(_COUNT_VOCAB.get(noun, (noun, 'count')))

    return hints


def _query_for(check_id: str, evidence: Optional[str] = None) -> str:
    """Build the retrieval query for a check.

    The check-name body + section hint anchors the topic; the finding's OWN
    evidence (what the auditor actually observed on THIS page) leads the query
    so two findings on the same check with different problems retrieve
    different rules. Evidence is normalized (lowercased, whitespace-collapsed,
    truncated) so identical observations pin to the same embedding.

    Numeric-aware (Lane B): measured quantities in the evidence append their
    canonical-unit + conceptual-vocabulary tokens (_numeric_hints) at the END
    of the query — evidence still leads, the check topic still anchors, and
    evidence WITHOUT measurements builds a byte-identical legacy query.
    """
    clean = check_id.split(':', 1)[-1]
    m = re.match(r'^([A-Z])\d', clean)
    hint = _SECTION_HINT.get(m.group(1), '') if m else ''
    body = re.sub(r'^[A-Z]\d+[a-z]?_', '', clean).replace('_', ' ').strip()
    base = (body + ' ' + hint).strip() or clean
    if evidence:
        ev_full = re.sub(r'\s+', ' ', str(evidence)).strip().lower()
        ev = ev_full[:200]
        if ev:
            q = (ev + ' ' + base).strip()
            present = set(re.split(r'[^a-z0-9.]+', q.lower()))
            hints = [h for h in _numeric_hints(ev_full) if h not in present]
            return (q + ' ' + ' '.join(hints)).strip() if hints else q
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
                                  'superseded_by', 'document_id',
                                  'domain_tag', 'last_verified', 'created_at')
        """)
        for t, c in cur.fetchall():
            out.setdefault(t, set()).add(c)
    return out


def _select_head(cfg, score_sql: str, cols: Optional[set] = None) -> str:
    t2 = f"t.{cfg['t2']}" if cfg['t2'] else "''"
    cols = cols or set()
    prov = "t.url_provenance" if 'url_provenance' in cols else "NULL"
    # Metadata columns the playbooks table (and any future kind) may lack —
    # probed per connection like the rest; absent → honest NULL, never an
    # SQL error that silently removes the whole table from retrieval.
    dtag = "t.domain_tag" if 'domain_tag' in cols else "NULL"
    lv = "t.last_verified::text" if 'last_verified' in cols else "NULL"
    ca = "t.created_at" if 'created_at' in cols else "NULL"
    # S4: join documents on the real document_id FK when the column exists;
    # fall back to the legacy source_refs_json regex only on an older DB.
    # no_doc_join kinds (playbooks) carry their own source_url — no join.
    if cfg.get('no_doc_join'):
        join = ""
        src_url = "NULLIF(t.source_url,'')"
    else:
        if 'document_id' in cols:
            doc_join = "d.id = t.document_id"
        else:
            doc_join = "d.id = NULLIF(substring(t.source_refs_json from '\\d+'), '')"
        join = f"""
        LEFT JOIN sieve.documents d
          ON {doc_join}"""
        src_url = "COALESCE(NULLIF(t.source_url,''), d.source_url)"
    # D2: last_verified and created_at are DISTINCT columns. A never-re-verified
    # rule (last_verified NULL) must NOT borrow created_at and display as
    # 'verified' — that fabricates freshness. created_at rides through only as an
    # honest 'added' date.
    return f"""
        SELECT t.id, t.{cfg['title']} AS title, t.{cfg['t1']} AS text1, {t2} AS text2,
               {dtag} AS domain_tag, {_conf_expr(cfg)} AS conf, t.source_org,
               {src_url} AS source_url,
               {lv} AS last_verified,
               {ca} AS created_at,
               {prov} AS url_provenance,
               {score_sql} AS score
        FROM sieve.{{table}} t{join}
    """


def _trust_filter(cols: Optional[set]) -> str:
    """Exclude only genuinely DEAD guidance — deprecated/rejected status and
    superseded rows. Live taxonomy: active/candidate/deprecated/rejected (the old
    'retired'/'superseded' strings are gone — supersession is the superseded_by
    FK). Each clause is guarded by _optional_cols so this degrades on an older DB.

    NOTE: we do NOT drop contested='t' rows. 'Contested' means a rule has a
    conflicting counterpart (conflict_pair_id), NOT that it is wrong — and
    blanket-excluding all contested rows discards ONE SIDE of every conflict,
    including authoritative tier-1 guidance (measured: 1,403 rules incl. Google,
    Schema.org, Backlinko, Ahrefs). Conflict resolution belongs in sieve-ingest
    enrichment (pick the winner per pair, mark the loser superseded); until then,
    keeping a contested-but-authoritative rule beats silently dropping it. When
    the loser is marked superseded_by, THIS filter already excludes it."""
    if not cols:
        return ""
    clauses = []
    if 'status' in cols:
        clauses.append(
            "coalesce(t.status,'active') NOT IN ('deprecated','rejected','retired','superseded')")
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


def _prov_method(raw) -> Optional[str]:
    """Extract the url_provenance 'method' (exact / exact-upgrade / doc-join /
    neighbor / observed-crawl). NULL = legacy row whose URL came with the
    original extraction — treated as trustworthy, only 'neighbor' (a
    similarity-adopted guess) is demoted."""
    if not raw:
        return None
    try:
        import json as _json
        d = _json.loads(raw) if isinstance(raw, str) else raw
        return d.get('method') if isinstance(d, dict) else None
    except Exception:
        return None


def _row_to_cite(r) -> Dict[str, Any]:
    org = r.get('source_org')
    t = tier_of(org)
    try:
        conf = float(r.get('conf') or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    prov = _prov_method(r.get('url_provenance'))
    return {
        'id': r.get('id'), 'kind': r.get('kindtag', 'rule'),
        'tier': t, 'tier_icon': TIER_ICONS.get(t, '📝'),
        'source_org': canon_org(org) or org, 'source_org_raw': org,
        'source_url': r.get('source_url'),
        'url_provenance_method': prov,
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

def _search_all_tables(conn, query_text: str, per_table_k: int) -> List[Dict[str, Any]]:
    """Vector-first (pinned embedding), FTS-fallback search across the three
    brain tables on an OPEN connection. Returns raw rows tagged with kindtag
    and _layer (the per-layer relevance floor depends on it). Passes each
    table's optional-column set through so the trust filter APPLIES — a bare
    cols=None would silently disable it and let rejected rows be cited."""
    from psycopg2.extras import RealDictCursor
    words = list(dict.fromkeys(re.findall(r'[a-zA-Z0-9][a-zA-Z0-9-]{2,}', query_text.lower())))
    ts_query = ' OR '.join(words) or query_text
    rows: List[Dict[str, Any]] = []
    cols_by_table = _optional_cols(conn)
    qvec_lit = _pinned_query_vec(conn, query_text)    # None => FTS-only
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        for table, cfg in _TABLE_CFG.items():
            cols = cols_by_table.get(table, set())
            found = []
            layer = 'vector'
            try:
                # fts_only kinds (playbooks) have no embeddings — skip the
                # doomed vector attempt and go straight to keyword search.
                if qvec_lit and not cfg.get('fts_only'):
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
    return rows


def live_citations(check_id: str, page_type: Optional[str] = None,
                   industry: Optional[str] = None,
                   max_citations: int = 3,
                   evidence: Optional[str] = None,
                   pool: Optional[int] = None) -> Optional[List[Dict[str, Any]]]:
    """Return citation dicts for a check from the live brain, or None so the
    caller falls back to the snapshot path. Never raises — EXCEPT under
    SIEVE_STRICT, where a live-brain failure raises SieveLiveError so the
    audit fails loudly instead of silently citing the April snapshot.

    Retrieval is EVIDENCE-based (the finding's observation leads the query),
    RELEVANCE-floored (off-topic hits are dropped, not the nearest-of-noise),
    and ranked relevance-first with authority a bounded tiebreak. If the
    corpus embedding model differs from SIEVE_EMBED_MODEL the vector layer is
    refused (falls to FTS) rather than cosine-comparing incompatible spaces.

    pool (judge-at-selection): when set (> max_citations) the final cut
    deepens to `pool` candidates. per_table_k stays a function of
    max_citations ON PURPOSE — the retrieved candidate set and the ranking
    sort are identical with or without a pool, so the first max_citations
    entries of a pooled result are byte-identical to the pool-less call
    (the degradation guarantee citation_attach's selection judging relies on)."""
    if not live_enabled():
        return None
    try:
        import psycopg2
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
                        # fts_only kinds (playbooks): no embeddings, straight to FTS.
                        if qvec_lit and not cfg.get('fts_only'):
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
    cut = max_citations
    if pool:
        try:
            cut = max(max_citations, int(pool))
        except (TypeError, ValueError):
            cut = max_citations
    ranked = _rank_and_floor(cites, cut)
    return ranked or None

# ---------------------------------------------------------------------------
# Programmatic retrieval (POST /api/brain/retrieve — AnswerMonk NORMS layer)
# ---------------------------------------------------------------------------

def _fetch_by_ids(conn, kind: str, ids: List[str]) -> List[Dict[str, Any]]:
    """Exact-id fetch (curated mappings path). Same trust gate + citation
    shape as search; relevance is 1.0 by definition (caller asked for them)."""
    from psycopg2.extras import RealDictCursor
    table = next((t for t, c in _TABLE_CFG.items() if c['kind'] == kind), None)
    if not table or not ids:
        return []
    cfg = _TABLE_CFG[table]
    cols = _optional_cols(conn).get(table, set())
    sql = _select_head(cfg, '1.0', cols).format(table=table) + \
        " WHERE t.id = ANY(%s)" + _trust_filter(cols)
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, ([str(i) for i in ids],))
        rows = cur.fetchall()
    for r in rows:
        r['kindtag'] = kind
        r['_layer'] = 'ids'
    return rows


def _spec_query_text(spec: Dict[str, Any]) -> str:
    """Search text for one retrieve_batch spec. Pure, unit-testable seam:
    a check_id query is evidence-led via _query_for(check_id, evidence) —
    the same construction the in-audit citation path uses (contract §3)."""
    if spec.get('q'):
        return str(spec['q'])
    if spec.get('check_id'):
        return _query_for(spec['check_id'], spec.get('evidence'))
    return ''


def _norm_gate(cites: List[Dict[str, Any]], min_tier: int) -> List[Dict[str, Any]]:
    """The NORM-slot tier gate, enforced in code (pure + unit-testable):
    min_tier is clamped to 4 — tier 5 (unattributed/observed) can never be
    requested past the gate — and membership is judged by curated_tier, so
    tier_of's dotted-domain display heuristic ('someblog.com' → 4) does not
    admit uncurated sources into the practitioner band."""
    mt = min(int(min_tier), 4)
    return [c for c in cites
            if curated_tier(c.get('source_org_raw')) <= mt]


def retrieve_batch(queries: List[Dict[str, Any]], min_tier: int = 4,
                   max_citations: int = 3) -> Dict[str, Any]:
    """Batch retrieval for server-to-server consumers (AnswerMonk).

    queries: [{key, q?|check_id?|rule_ids?, evidence?}] — one result list per key.
      q        : free-text search (vector-first, FTS fallback)
      check_id : auditor-style id, expanded via _query_for; when the spec
                 carries `evidence`, it LEADS the query (evidence-led, §3)
      rule_ids : exact rule-id fetch (curated-mapping path)
    min_tier: NORM-slot gate (_norm_gate) — only citations whose org sits at
      CURATED tier <= min_tier are returned. Default 4: tier 4 is the
      explicit PRACTITIONER band (YC/Reforge/a16z-class growth orgs,
      reconciled in org-tiers.json) — the old default of 3 excluded 62% of
      the rule corpus. The gate uses curated_tier, NOT tier_of: an
      unrecognized dotted-domain org may DISPLAY as tier 4 but does not pass
      a tier gate. Tier 5 (unattributed/observed) is excluded in code —
      min_tier is clamped to 4 — anonymous knowledge can never be a norm.
    Never raises; on any failure returns {'live': False, 'results': {}}.
    """
    if not live_enabled():
        return {'live': False, 'results': {}, 'reason': 'live brain disabled'}
    try:
        import psycopg2
    except Exception:
        return {'live': False, 'results': {}, 'reason': 'psycopg2 unavailable'}

    per_table_k = max(8, max_citations * 4)
    results: Dict[str, List[Dict[str, Any]]] = {}
    try:
        conn = psycopg2.connect(SIEVE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        try:
            for spec in queries:
                key = str(spec.get('key') or '')
                if not key:
                    continue
                try:
                    if spec.get('rule_ids'):
                        rows = _fetch_by_ids(conn, 'rule', list(spec['rule_ids']))
                    else:
                        text = _spec_query_text(spec)
                        if not text:
                            results[key] = []
                            continue
                        rows = _search_all_tables(conn, text, per_table_k)
                    cites = _norm_gate([_row_to_cite(r) for r in rows], min_tier)
                    results[key] = _rank_and_floor(cites, max_citations)
                except Exception as qe:
                    log.warning('brain retrieve failed for key %s: %s', key, qe)
                    results[key] = []
        finally:
            conn.close()
    except Exception as e:
        log.warning('brain retrieve batch failed: %s', e)
        return {'live': False, 'results': {}, 'reason': str(e)[:200]}
    return {'live': True, 'results': results}


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
