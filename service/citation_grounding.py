"""
citation_grounding.py — post-LLM re-grounding of finding citations.

WHY: in agent mode the LLM copies query_brain output into its final
<audit> JSON, and that copy step is untrusted — measured on production
data only ~half of the quoted rule texts survived verbatim (truncation,
paraphrase, occasionally invented remediation attributed to a tier-1
source). System-prompt instructions alone cannot fix a copy step.

This module makes quotes trustworthy BY CONSTRUCTION: after the loop,
every citation in audit['findings'][*]['citations'] is re-fetched from
its authoritative store by (kind, id) and all content/source fields are
overwritten with the stored values. The LLM contributes only the
check -> citation-id mapping.

Resolution order per citation:
  1. live sieve brain (SIEVE_LIVE=1 + DB URL) by kind+id
  2. repo snapshot (service/ruleset/*.json) by kind+id
  3. unresolved -> citation kept, flagged {'grounded': 'unresolved',
     'verbatim': False} so renderers/analytics can discount it.

Never raises (HANDOFF invariant 1: additive; a grounding failure must
never drop a completed audit). All outcomes are reported in the stats
dict persisted under metadata.citation_grounding.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger('audit.grounding')

# ranker.py lives in service/ruleset/ (same pattern as tools.py:56).
_RULESET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ruleset')
if _RULESET_DIR not in sys.path:
    sys.path.insert(0, _RULESET_DIR)

# Same 500-char contract as query_brain / sieve_brain quote fields.
_TEXT_CAP = 500

# kind -> where it lives (live sieve table + snapshot index attr) and how its
# columns map onto the uniform citation shape (name/if_condition/then_action).
_KIND_CFG = {
    'rule': {
        'table': 'rules', 'snapshot_attr': 'rules_by_id',
        'title': 'name', 't1': 'if_condition', 't2': 'then_logic',
        'snap_t1': 'if_condition', 'snap_t2': 'then_action',
        'conf': 'confidence_score', 'risk': None,
    },
    'principle': {
        'table': 'principles', 'snapshot_attr': 'principles_by_id',
        'title': 'title', 't1': 'statement', 't2': 'explanation',
        'snap_t1': 'statement', 'snap_t2': 'explanation',
        'conf': 'confidence_score', 'risk': None,
    },
    'ap': {
        'table': 'anti_patterns', 'snapshot_attr': 'aps_by_id',
        'title': 'title', 't1': 'description', 't2': None,
        'snap_t1': 'description', 'snap_t2': None,
        'conf': None, 'risk': 'risk_level',
    },
}

_KIND_ALIASES = {
    'rule': 'rule', 'rules': 'rule',
    'principle': 'principle', 'principles': 'principle',
    'ap': 'ap', 'anti_pattern': 'ap', 'anti-pattern': 'ap',
    'antipattern': 'ap', 'anti_patterns': 'ap',
}

# The fields this module owns. Whatever the LLM put there is replaced;
# any other keys on the citation object are left untouched.
_AUTHORITATIVE_TEXT_FIELDS = ('name', 'if_condition', 'then_action')

# Kind-specific alias fields also owned by this module — popped before the
# authoritative update so a rule citation can't keep a stale 'statement' etc.
_ALIAS_FIELDS = ('title', 'statement', 'explanation', 'description',
                 'risk_level', 'source_title')

# The three brain tables have ~94% id overlap (rules/principles/anti_patterns
# all start at 1), so (kind, id) with a wrong kind resolves to an UNRELATED
# record. Before accepting a fetched record we require minimal lexical overlap
# between what the model cited and what the store returned; on failure we try
# the other kinds, and give up as 'unresolved' rather than mis-attribute.
_STOPWORDS = {'the', 'and', 'for', 'with', 'that', 'this', 'must', 'should',
              'when', 'then', 'your', 'from', 'have', 'are', 'not', 'all',
              'any', 'use', 'page', 'site'}


def _text_tokens(*texts: Any) -> set:
    toks = set()
    for s in texts:
        if isinstance(s, str):
            toks |= {w for w in re.findall(r'[a-z0-9]+', s.lower())
                     if len(w) >= 3 and w not in _STOPWORDS}
    return toks


def _claimed_tokens(c: Dict[str, Any]) -> set:
    return _text_tokens(c.get('name'), c.get('title'), c.get('if_condition'),
                        c.get('statement'), c.get('description'),
                        c.get('then_action'), c.get('explanation'))


def _plausible(claimed: set, auth: Dict[str, Any]) -> bool:
    """Does the fetched record plausibly match what the model cited?"""
    fetched = _text_tokens(auth.get('name'), auth.get('if_condition'),
                           auth.get('then_action'))
    if not claimed or not fetched:
        return True
    inter = claimed & fetched
    return len(inter) >= 2 or \
        (len(inter) / max(1, min(len(claimed), len(fetched)))) >= 0.34


def _norm_kind(kind: Any) -> Optional[str]:
    if not isinstance(kind, str):
        return None
    return _KIND_ALIASES.get(kind.strip().lower())


def _norm_id(value: Any) -> Optional[str]:
    """Canonical citation id as a digit string ('1668'); None if unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, str):
        m = re.fullmatch(r'\s*(\d+)\s*', value)
        if m:
            return m.group(1)
    return None


def _cap(text: Any) -> str:
    return (text if isinstance(text, str) else '')[:_TEXT_CAP]


# ---------------------------------------------------------------------------
# Live fetch (sieve schema) — batch, one connection for the whole audit
# ---------------------------------------------------------------------------

def _fetch_live_rows(wanted: Dict[str, List[str]]) -> Dict[Tuple[str, str], Dict[str, Any]]:
    """Fetch authoritative rows for {kind: [ids]} from the live sieve brain.
    Returns {(kind, id): citation_fields}. Empty dict when live is off or on
    any connection-level failure (caller falls back to the snapshot)."""
    import sieve_brain
    if not sieve_brain.live_enabled() or not any(wanted.values()):
        return {}
    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor
    except Exception:
        return {}

    out: Dict[Tuple[str, str], Dict[str, Any]] = {}
    try:
        conn = psycopg2.connect(sieve_brain.SIEVE_DB_URL, connect_timeout=10)
        conn.autocommit = True
        try:
            cols_by_table = sieve_brain._optional_cols(conn)
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                for kind, ids in wanted.items():
                    if not ids:
                        continue
                    cfg = _KIND_CFG[kind]
                    cols = cols_by_table.get(cfg['table'], set())
                    t2 = f"t.{cfg['t2']}" if cfg['t2'] else "''"
                    conf = f"t.{cfg['conf']}" if cfg['conf'] else "NULL"
                    risk = f"t.{cfg['risk']}" if cfg['risk'] else "NULL"
                    prov = "t.url_provenance" if 'url_provenance' in cols else "NULL"
                    # Retired/superseded guidance is unciteable on this path too:
                    # a by-id fetch that resurrects a retired rule as 'verified'
                    # would undo the retrieval-side status filter.
                    status_f = sieve_brain._status_filter(cols)
                    cur.execute(
                        f"""
                        SELECT t.id, t.{cfg['title']} AS title, t.{cfg['t1']} AS text1,
                               {t2} AS text2, t.domain_tag, {conf} AS conf,
                               {risk} AS risk, t.source_org,
                               COALESCE(NULLIF(t.source_url,''), d.source_url) AS source_url,
                               COALESCE(t.last_verified::text, t.created_at) AS created_at,
                               {prov} AS url_provenance
                        FROM sieve.{cfg['table']} t
                        LEFT JOIN sieve.documents d
                          ON d.id = NULLIF(substring(t.source_refs_json from '\\d+'), '')
                        WHERE t.id = ANY(%s){status_f}
                        """,
                        (ids,),
                    )
                    for r in cur.fetchall():
                        rid = _norm_id(r.get('id'))
                        if rid:
                            out[(kind, rid)] = _live_row_fields(kind, r)
        finally:
            conn.close()
    except Exception as e:
        if sieve_brain.SIEVE_STRICT:
            raise sieve_brain.SieveLiveError(
                f're-grounding requires the live brain (SIEVE_STRICT): {e}')
        # Keep whatever was already fetched — snapshot covers the rest.
        log.warning('live re-grounding fetch failed (%s) — using snapshot', e)
        return out
    return out


def _live_row_fields(kind: str, r: Dict[str, Any]) -> Dict[str, Any]:
    """Uniform authoritative fields from a live sieve row (mirrors the shape
    sieve_brain._row_to_cite emits, minus retrieval-only metadata)."""
    import sieve_brain
    org = r.get('source_org')
    tier = sieve_brain.tier_of(org)
    conf = r.get('conf')
    if conf is None and kind == 'ap':
        # Same risk->confidence map as the snapshot path (ranker.py)
        conf = {'high': 0.95, 'medium': 0.80, 'low': 0.65}.get(
            str(r.get('risk') or '').strip().lower(), 0.75)
    try:
        conf_str = str(round(float(conf), 2))
    except (TypeError, ValueError):
        conf_str = '0.0'
    fields = {
        'kind': kind,
        'tier': tier,
        'tier_icon': sieve_brain.TIER_ICONS.get(tier, '📝'),
        'source_org': sieve_brain.canon_org(org) or org,
        'source_org_raw': org,
        'source_url': r.get('source_url'),
        'name': _cap(r.get('title')),
        'confidence_score': conf_str,
        'if_condition': _cap(r.get('text1')),
        'then_action': _cap(r.get('text2')),
        'domain_tag': r.get('domain_tag'),
        'last_verified': str(r.get('created_at'))[:10] if r.get('created_at') else None,
        # Overwrites the retrieval-time value so a re-grounded source_url never
        # carries a stale provenance label from a URL it replaced.
        'url_provenance': r.get('url_provenance'),
        'from': 'sieve-live',
    }
    if kind == 'ap':
        fields['risk_level'] = r.get('risk')
        fields['description'] = fields['if_condition']
    if kind == 'principle':
        fields['title'] = fields['name']
        fields['statement'] = fields['if_condition']
        fields['explanation'] = fields['then_action']
    return fields


# ---------------------------------------------------------------------------
# Snapshot fetch (service/ruleset/*.json)
# ---------------------------------------------------------------------------

_SNAPSHOT_CACHE = None


def _load_snapshot_index():
    global _SNAPSHOT_CACHE
    if _SNAPSHOT_CACHE is None:
        from ranker import BrainIndex
        _SNAPSHOT_CACHE = BrainIndex.from_export_dir(_RULESET_DIR)
    return _SNAPSHOT_CACHE


def _snapshot_fields(kind: str, rid: str) -> Optional[Dict[str, Any]]:
    try:
        brain = _load_snapshot_index()
    except Exception as e:
        log.warning('snapshot unavailable for re-grounding: %s', e)
        return None
    cfg = _KIND_CFG[kind]
    row = getattr(brain, cfg['snapshot_attr'], {}).get(int(rid))
    if not isinstance(row, dict):
        return None
    from ranker import get_tier_rank, TIER_ICONS
    tier = get_tier_rank(row.get('source_org'))
    conf = row.get(cfg['conf']) if cfg['conf'] else None
    if conf is None and kind == 'ap':
        conf = {'high': 0.95, 'medium': 0.80, 'low': 0.65}.get(
            str(row.get('risk_level') or '').strip().lower(), 0.75)
    fields = {
        'kind': kind,
        'tier': tier,
        'tier_icon': TIER_ICONS.get(tier, '📝'),
        'source_org': row.get('source_org'),
        'source_url': row.get('source_url'),
        'source_title': row.get('source_title'),
        'name': _cap(row.get(cfg['title']) or row.get('name') or row.get('title')),
        'confidence_score': str(conf) if conf is not None else None,
        'if_condition': _cap(row.get(cfg['snap_t1'])),
        'then_action': _cap(row.get(cfg['snap_t2']) if cfg['snap_t2'] else ''),
        'domain_tag': row.get('domain_tag'),
        'url_provenance': None,  # snapshot rows carry no provenance — clear any stale label
        'from': 'snapshot',
    }
    if kind == 'ap':
        fields['risk_level'] = row.get('risk_level')
        fields['description'] = fields['if_condition']
    if kind == 'principle':
        fields['title'] = fields['name']
        fields['statement'] = fields['if_condition']
        fields['explanation'] = fields['then_action']
    return fields


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def reground_citations(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Overwrite every finding citation's content/source fields with the
    authoritative stored values, resolved by (kind, id). Mutates and returns
    the audit plus a stats dict. Never raises."""
    stats = {'applied': True, 'citations_total': 0, 'regrounded_live': 0,
             'regrounded_snapshot': 0, 'unresolved': 0, 'text_corrected': 0}
    try:
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats

        # Pass 1 — collect every cited id, batched under ALL kinds: ids
        # overlap across the three tables, and cross-kind lookup is what lets
        # us both detect a mislabeled kind and recover from it.
        wanted: Dict[str, List[str]] = {k: [] for k in _KIND_CFG}
        for f in findings:
            if not isinstance(f, dict):
                continue
            for c in (f.get('citations') or []):
                if not isinstance(c, dict):
                    continue
                rid = _norm_id(c.get('id'))
                if rid:
                    for k in _KIND_CFG:
                        if rid not in wanted[k]:
                            wanted[k].append(rid)

        try:
            live_rows = _fetch_live_rows(wanted)
        except Exception as e:  # noqa: BLE001 — grounding must never break the audit
            log.warning('live re-grounding unavailable: %s', e)
            stats['live_error'] = f'{type(e).__name__}: {e}'
            live_rows = {}

        # Pass 2 — overwrite each citation in place.
        for f in findings:
            if not isinstance(f, dict):
                continue
            citations = f.get('citations')
            if not isinstance(citations, list):
                continue
            for c in citations:
                if not isinstance(c, dict):
                    stats['unresolved'] += 1
                    stats['citations_total'] += 1
                    continue
                stats['citations_total'] += 1
                labeled = _norm_kind(c.get('kind'))
                rid = _norm_id(c.get('id'))
                claimed = _claimed_tokens(c)

                # Candidate kinds: the labeled one first; the other tables
                # only when the citation carries text we can sanity-check
                # against (otherwise a bare (id) would accept any table).
                kinds: List[str] = [labeled] if labeled else []
                if claimed:
                    kinds += [k for k in _KIND_CFG if k not in kinds]

                auth = None
                grounded = 'unresolved'
                matched_kind = None
                if rid:
                    for k in kinds:
                        cand = live_rows.get((k, rid))
                        src = 'sieve-live'
                        if cand is None:
                            cand = _snapshot_fields(k, rid)
                            src = 'snapshot'
                        if cand is None:
                            continue
                        # For the labeled kind with no claimed text we trust
                        # the (kind, id) mapping; otherwise require overlap.
                        if claimed and not _plausible(claimed, cand):
                            continue
                        auth, grounded, matched_kind = cand, src, k
                        break

                if auth is None:
                    c['grounded'] = 'unresolved'
                    c['verbatim'] = False
                    stats['unresolved'] += 1
                    continue

                changed = any(
                    (c.get(k) or '') != (auth.get(k) or '')
                    for k in _AUTHORITATIVE_TEXT_FIELDS if k in c
                )
                if labeled and matched_kind != labeled:
                    stats['kind_corrected'] = stats.get('kind_corrected', 0) + 1
                for k in _ALIAS_FIELDS:
                    c.pop(k, None)
                c.update(auth)
                c['id'] = rid
                c['grounded'] = grounded
                c['verbatim'] = True
                stats['regrounded_live' if grounded == 'sieve-live'
                      else 'regrounded_snapshot'] += 1
                if changed:
                    stats['text_corrected'] += 1
    except Exception as e:  # noqa: BLE001
        log.error('citation re-grounding failed: %s', e)
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
    return audit, stats


# ---------------------------------------------------------------------------
# Fix-source resolution — make the WHY paragraphs cite their receipts.
# The model's top-fix narratives reference brain objects inline ("Sieve
# Principle #1109", "rule id:25694") but carry no links. Python parses those
# references, resolves each (kind, id) through the same live->snapshot chain
# as citations, and attaches fix['sources'] = [{kind,id,name,source_org,
# source_url,last_verified,...}] — so the rendered claim links to the actual
# source document, deterministically.
# ---------------------------------------------------------------------------

_REF_RE = re.compile(
    r'(?:sieve\s+)?(rule|principle|anti[-_\s]?pattern|ap)s?'
    r'\s*(?:\(\s*)?(?:id\s*[:#=]?\s*|#)\s*(\d{1,6})',
    re.IGNORECASE)
_MAX_FIX_SOURCES = 4


def _parse_refs(text: str) -> List[Tuple[str, str]]:
    """Ordered, deduped (kind, id) references mentioned in narrative text."""
    out: List[Tuple[str, str]] = []
    for kind_word, rid in _REF_RE.findall(text or ''):
        kind = _norm_kind(kind_word)
        if kind and (kind, rid) not in out:
            out.append((kind, rid))
    return out


def ground_fix_sources(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve brain-object references in narrative.top_5_fixes into linked
    sources. Mutates and returns (audit, stats). Never raises."""
    stats: Dict[str, Any] = {'applied': True, 'fixes_scanned': 0,
                             'refs_found': 0, 'resolved': 0, 'unresolved': 0}
    try:
        fixes = (audit.get('narrative') or {}).get('top_5_fixes')
        if not isinstance(fixes, list) or not fixes:
            return audit, stats

        per_fix_refs: List[List[Tuple[str, str]]] = []
        wanted: Dict[str, List[str]] = {k: [] for k in _KIND_CFG}
        for f in fixes:
            if not isinstance(f, dict):
                per_fix_refs.append([])
                continue
            text = ' '.join(str(f.get(k) or '') for k in ('title', 'why', 'before', 'after'))
            refs = _parse_refs(text)[:_MAX_FIX_SOURCES]
            per_fix_refs.append(refs)
            stats['fixes_scanned'] += 1
            stats['refs_found'] += len(refs)
            for kind, rid in refs:
                if rid not in wanted[kind]:
                    wanted[kind].append(rid)

        if not stats['refs_found']:
            return audit, stats

        try:
            live_rows = _fetch_live_rows(wanted)
        except Exception as e:  # noqa: BLE001
            log.warning('live fix-source fetch unavailable: %s', e)
            live_rows = {}

        for f, refs in zip(fixes, per_fix_refs):
            if not isinstance(f, dict) or not refs:
                continue
            sources = []
            for kind, rid in refs:
                auth = live_rows.get((kind, rid)) or _snapshot_fields(kind, rid)
                if auth is None:
                    stats['unresolved'] += 1
                    continue
                stats['resolved'] += 1
                sources.append({
                    'kind': kind, 'id': rid,
                    'name': auth.get('name'),
                    'source_org': auth.get('source_org'),
                    'source_url': auth.get('source_url'),
                    'last_verified': auth.get('last_verified'),
                    'confidence_score': auth.get('confidence_score'),
                    'from': auth.get('from'),
                })
            if sources:
                f['sources'] = sources
    except Exception as e:  # noqa: BLE001
        log.error('fix-source grounding failed: %s', e)
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
    return audit, stats


# ---------------------------------------------------------------------------
# Self-test (stdlib only, no DB/network) — wired into tests/run_tests.sh
# ---------------------------------------------------------------------------

def _selftest() -> None:
    import types
    global _SNAPSHOT_CACHE

    # Stub the snapshot: rule 1668, an UNRELATED rule 2001 (id collides with
    # AP 2001 — mirrors the ~94% cross-table id overlap in the real brain),
    # and AP 2001. Live layer off.
    stub_index = _SNAPSHOT_CACHE = types.SimpleNamespace(
        rules_by_id={
            1668: {
                'id': 1668, 'name': 'Organization schema must include name and URL',
                'if_condition': 'IF the page declares an Organization entity',
                'then_action': 'THEN include name, url and logo properties',
                'confidence_score': '0.98', 'source_org': 'Schema.org',
                'source_url': 'https://schema.org/Organization',
                'source_title': 'Organization - Schema.org', 'domain_tag': 'seo',
            },
            2001: {
                'id': 2001, 'name': 'Indicate hreflang for multi-language variants',
                'if_condition': 'IF localized variants exist',
                'then_action': 'THEN declare hreflang annotations',
                'confidence_score': '0.97', 'source_org': 'Google',
            },
        },
        aps_by_id={2001: {
            'id': 2001, 'title': 'FAQ schema without visible FAQ content',
            'description': 'Marking up FAQs that are not visible on the page',
            'risk_level': 'high', 'source_org': 'Google',
            'source_url': 'https://developers.google.com/search/docs/appearance/structured-data/faqpage',
        }},
        principles_by_id={},
    )

    audit = {'findings': [
        {'check_id': 'D6_required_fields', 'status': 'fail', 'citations': [
            # LLM paraphrased the quote and dropped the source fields
            {'id': '1668', 'kind': 'rule',
             'name': 'Org schema needs name/URL (paraphrased)',
             'if_condition': 'totally rewritten by the model',
             'then_action': 'invented remediation text'},
            # unknown id -> must be flagged, not dropped, never raise
            {'id': 999999, 'kind': 'rule', 'name': 'ghost rule'},
        ]},
        {'check_id': 'D7_faq', 'status': 'warn', 'citations': [
            {'id': 2001, 'kind': 'anti_pattern', 'name': 'FAQ abuse'},
            # kind MISLABELED as rule; text matches the AP, not the colliding
            # hreflang rule 2001 -> must recover cross-kind, not mis-attribute
            {'id': 2001, 'kind': 'rule', 'name': 'FAQ markup abuse',
             'description': 'FAQs marked up but not visible'},
            'not-even-a-dict',
            # kindless AND textless -> nothing to verify against -> unresolved
            {'id': '1668'},
            # labeled kind, bare id (no text): trust the (kind, id) mapping
            {'id': 1668, 'kind': 'rule'},
        ]},
        {'check_id': 'A1_no_citations', 'status': 'pass'},
    ]}

    audit, stats = reground_citations(audit)
    c1 = audit['findings'][0]['citations'][0]
    assert c1['name'] == 'Organization schema must include name and URL', c1
    assert c1['if_condition'].startswith('IF the page declares'), c1
    assert c1['then_action'].startswith('THEN include name'), c1
    assert c1['source_org'] == 'Schema.org' and c1['tier'] == 1, c1
    assert c1['source_url'] == 'https://schema.org/Organization', c1
    assert c1['verbatim'] is True and c1['grounded'] == 'snapshot', c1

    ghost = audit['findings'][0]['citations'][1]
    assert ghost['grounded'] == 'unresolved' and ghost['verbatim'] is False, ghost
    assert ghost['name'] == 'ghost rule', 'unresolved citations must keep their text'

    ap = audit['findings'][1]['citations'][0]
    assert ap['kind'] == 'ap' and ap['risk_level'] == 'high', ap
    assert ap['if_condition'].startswith('Marking up FAQs'), ap

    mislabeled = audit['findings'][1]['citations'][1]
    assert mislabeled['kind'] == 'ap', ('mislabeled kind must recover to the '
                                        'plausible table, not the id-colliding rule', mislabeled)
    assert mislabeled['name'] == 'FAQ schema without visible FAQ content', mislabeled
    assert 'hreflang' not in (mislabeled.get('name') or ''), mislabeled

    kindless = audit['findings'][1]['citations'][3]
    assert kindless['grounded'] == 'unresolved' and kindless['verbatim'] is False, kindless

    bare = audit['findings'][1]['citations'][4]
    assert bare['grounded'] == 'snapshot' and bare['name'].startswith('Organization schema'), bare

    assert stats == {'applied': True, 'citations_total': 7, 'regrounded_live': 0,
                     'regrounded_snapshot': 4, 'unresolved': 3,
                     'text_corrected': 3, 'kind_corrected': 1}, stats

    # Robustness: snapshot loader exploding must degrade, never raise.
    _SNAPSHOT_CACHE = None
    def _boom():
        raise RuntimeError('snapshot gone')
    globals()['_load_snapshot_index'], keep = _boom, _load_snapshot_index
    try:
        a2, s2 = reground_citations({'findings': [
            {'citations': [{'id': 1, 'kind': 'rule', 'name': 'x'}]}]})
        assert s2['applied'] is True and s2['unresolved'] == 1, s2
        assert a2['findings'][0]['citations'][0]['grounded'] == 'unresolved'
    finally:
        globals()['_load_snapshot_index'] = keep

    # No-op shapes: missing/absent findings must not crash.
    for shape in ({}, {'findings': None}, {'findings': 'bogus'}):
        _, s3 = reground_citations(dict(shape))
        assert s3['applied'] is True and s3['citations_total'] == 0, (shape, s3)

    # Fix-source resolution: WHY-paragraph references become linked sources.
    _SNAPSHOT_CACHE = stub_index
    audit3 = {'narrative': {'top_5_fixes': [
        {'title': 'Fix org schema',
         'why': 'Per Sieve Rule #1668 (confidence 0.98) this is required; '
                'see also principle id:999 which does not exist.'},
        {'why': 'no brain references in this one'},
        'junk-not-a-dict',
    ]}}
    audit3, s4 = ground_fix_sources(audit3)
    f0 = audit3['narrative']['top_5_fixes'][0]
    assert [(x['kind'], x['id']) for x in f0.get('sources', [])] == [('rule', '1668')], f0
    assert f0['sources'][0]['source_url'] == 'https://schema.org/Organization', f0
    assert 'sources' not in audit3['narrative']['top_5_fixes'][1]
    assert s4 == {'applied': True, 'fixes_scanned': 2, 'refs_found': 2,
                  'resolved': 1, 'unresolved': 1}, s4
    # Parser tolerates the real-world phrasings seen in production audits.
    assert _parse_refs('Schema.org Principle #1250 and anti-pattern id: 42 and Rule id=25694') == \
        [('principle', '1250'), ('ap', '42'), ('rule', '25694')], \
        _parse_refs('Schema.org Principle #1250 and anti-pattern id: 42 and Rule id=25694')
    for shape in ({}, {'narrative': None}, {'narrative': {'top_5_fixes': 'x'}}):
        _, s5 = ground_fix_sources(dict(shape))
        assert s5['applied'] is True and s5['refs_found'] == 0, (shape, s5)

    print('GROUNDING_OK')


if __name__ == '__main__':
    _selftest()
