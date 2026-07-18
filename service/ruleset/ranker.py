"""
ranker.py — Deterministic citation selector for the audit pipeline.

Replaces the LLM-driven "pick 2-3 rules from candidates" step with pure
sorting logic. Same input → same output, guaranteed.

USAGE
    from ranker import BrainIndex, select_citations

    # Load once at startup
    brain = BrainIndex.from_export_dir('auditor-ruleset-export/')

    # Per failed check
    citations = select_citations(
        brain=brain,
        check_id='D6_required_fields',
        candidate_rule_ids=[1668, 1532, 1600, 1654, 1674, 1695, 1710],
        page_type='medical_business',
        industry='healthcare',
        max_citations=3,
    )

DESIGN
    Decision tree, in order:
        1. Load candidate rules from snapshot by ID
        2. Filter by domain_tag if check has known domain
        3. Filter by page_type tag (skip if rule has no tag — universal)
        4. Filter by industry tag (skip if rule has no tag — universal)
        5. Sort by:
            primary:   tier_rank ASC (Tier 1 before Tier 2 before ...)
            secondary: confidence DESC (0.99 before 0.85)
            tertiary:  id ASC (deterministic tiebreaker)
        6. Return top N

NO LLM. NO API CALLS. Pure deterministic Python.

VERSION
    1.0 — extracted 2026-05-01 from the unified build.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ----------------------------------------------------------------------
# TIER MAPPING (source_org → tier rank)
# ----------------------------------------------------------------------

# Tier 1 — Primary sources (official documentation)
TIER_1_SOURCES = {
    'Google', 'Schema.org', 'Perplexity', 'Bing', 'Microsoft',
    'W3C', 'Apple', 'Apple Developer', 'OpenAI', 'Anthropic',
    'Mozilla', 'developers.google.com', 'docs.perplexity.ai',
    'developer.mozilla.org', 'schema.org',
}

# Tier 2 — Research / data-driven studies
TIER_2_SOURCES = {
    'Backlinko', 'backlinko.com', 'Ahrefs', 'Semrush',
    'Princeton', 'arXiv', 'Vercel', 'BrightEdge',
    'Princeton/arXiv', 'vercel.com',
}

# Tier 3 — Industry analysis
TIER_3_SOURCES = {
    'Search Engine Land', 'Search Engine Journal', 'Moz',
    'HubSpot', 'blog.hubspot.com', 'searchengineland.com',
    'searchenginejournal.com', 'moz.com',
}

# Tier 4 — Specialized / niche
TIER_4_SOURCES = {
    'amsive.com', 'almcorp.com', 'cxl.com',
    'seerinteractive.com', 'Y Combinator', 'apptweak',
    'Shopify', 'Buffer', 'frase.io', 'animalz.co',
    'b2bcontentos.com', 'appsflyer.com',
}

# Default tier for unknown source_org
DEFAULT_TIER = 5

TIER_ICONS = {
    1: '🥇',
    2: '🥈',
    3: '🥉',
    4: '📎',
    5: '·',
}


def get_tier_rank(source_org: Optional[str]) -> int:
    """Map a source_org string to a tier rank (lower = more authoritative)."""
    if not source_org:
        return DEFAULT_TIER
    if source_org in TIER_1_SOURCES:
        return 1
    if source_org in TIER_2_SOURCES:
        return 2
    if source_org in TIER_3_SOURCES:
        return 3
    if source_org in TIER_4_SOURCES:
        return 4
    return DEFAULT_TIER


# ----------------------------------------------------------------------
# BRAIN INDEX (in-memory snapshot lookup)
# ----------------------------------------------------------------------

@dataclass
class BrainIndex:
    """In-memory index over the 4 Sieve brain snapshots.

    Build once at audit-service startup. Subsequent lookups are O(1).
    """
    rules_by_id: Dict[int, dict]
    aps_by_id: Dict[int, dict]
    playbooks_by_id: Dict[int, dict]
    principles_by_id: Dict[int, dict]
    check_to_rules: Dict[str, dict]  # from brain-mappings.json
    snapshot_date: Optional[str] = None   # brain-mappings 'last_curated'
    _bm25_index: Optional[dict] = field(default=None, repr=False, compare=False)

    @classmethod
    def from_export_dir(cls, export_dir: str) -> 'BrainIndex':
        """Load all 4 snapshot files + brain-mappings into a single index."""
        def _load_array(filename: str) -> Dict[int, dict]:
            path = os.path.join(export_dir, filename)
            if not os.path.exists(path):
                return {}
            with open(path) as f:
                arr = json.load(f)
            return {entry['id']: entry for entry in arr if 'id' in entry}

        snap_date = None
        def _load_mappings() -> Dict[str, dict]:
            nonlocal snap_date
            path = os.path.join(export_dir, 'brain-mappings.json')
            if not os.path.exists(path):
                return {}
            with open(path) as f:
                data = json.load(f)
            snap_date = data.get('last_curated')
            return data.get('mappings', {})

        mappings = _load_mappings()
        return cls(
            rules_by_id=_load_array('rules-snapshot.json'),
            aps_by_id=_load_array('anti-patterns-snapshot.json'),
            playbooks_by_id=_load_array('playbooks-snapshot.json'),
            principles_by_id=_load_array('principles-snapshot.json'),
            check_to_rules=mappings,
            snapshot_date=snap_date,
        )

    def stats(self) -> Dict:
        return {
            'rules': len(self.rules_by_id),
            'anti_patterns': len(self.aps_by_id),
            'playbooks': len(self.playbooks_by_id),
            # principles are LOADED but were historically unreachable; search()
            # (Phase 2) now retrieves them, so this count reflects real coverage.
            'principles': len(self.principles_by_id),
            'mapped_checks': len(self.check_to_rules),
        }

    # ------------------------------------------------------------------
    # Phase 2 — index-backed snapshot retrieval (BM25 over ALL three kinds).
    # The old snapshot path could only ever cite the 141 hand-mapped ids and
    # never a single principle. search() indexes rules + anti_patterns +
    # principles and retrieves on the finding's EVIDENCE, so the ~9.9k-row
    # library and every principle become reachable offline, with the same
    # relevance-floored, relevance-first policy as the live path.
    # ------------------------------------------------------------------
    def _bm25(self) -> dict:
        if self._bm25_index is None:
            self.__dict__['_bm25_index'] = _build_bm25(self)
        return self._bm25_index

    def search(self, query: str, max_citations: int = 3,
               kinds: Optional[tuple] = None,
               min_score: Optional[float] = None) -> List[dict]:
        return _bm25_search(self, query, max_citations, kinds, min_score)


# BM25 (Okapi) over the snapshot corpus — stdlib only, built once, memoized.
_BM25_K1 = 1.5
_BM25_B = 0.75
SNAPSHOT_MIN_SCORE = float(os.getenv('SIEVE_SNAPSHOT_MIN_SCORE', '2.0'))
_NEUTRAL_AP_CONF = 0.75   # anti-patterns have no measured confidence; do NOT
                          # fabricate one from risk_level (which inflated APs
                          # past real high-confidence rules). risk_level is
                          # carried as its own field for downstream weighting.
_STOP = frozenset(
    'the a an and or of to in for on with is are be as at by from this that it its into '
    'over under about not no your you can should must if then when where which who what how'.split())
_SEARCH_FIELDS = {
    'rule':      ('name', 'if_condition', 'then_action'),
    'ap':        ('title', 'description'),
    'principle': ('title', 'statement', 'explanation'),
}


def _tok(text: Optional[str]) -> List[str]:
    return [w for w in re.findall(r'[a-z0-9]+', (text or '').lower())
            if len(w) >= 3 and w not in _STOP]


def _doc_text(kind: str, row: dict) -> str:
    return ' '.join(str(row.get(f) or '') for f in _SEARCH_FIELDS[kind])


def _build_bm25(brain: 'BrainIndex') -> dict:
    docs = []  # (kind, id, row)
    for rid, r in brain.rules_by_id.items():
        docs.append(('rule', rid, r))
    for aid, a in brain.aps_by_id.items():
        docs.append(('ap', aid, a))
    for pid, p in brain.principles_by_id.items():
        docs.append(('principle', pid, p))
    tfs, doclen, df = [], [], Counter()
    inv = defaultdict(list)
    for i, (kind, _id, row) in enumerate(docs):
        tf = Counter(_tok(_doc_text(kind, row)))
        tfs.append(tf)
        doclen.append(sum(tf.values()))
        for t in tf:
            df[t] += 1
    N = len(docs)
    avgdl = (sum(doclen) / N) if N else 0.0
    idf = {t: math.log(1 + (N - d + 0.5) / (d + 0.5)) for t, d in df.items()}
    for i, tf in enumerate(tfs):
        for t, f in tf.items():
            inv[t].append((i, f))
    return {'docs': docs, 'idf': idf, 'inv': inv, 'doclen': doclen, 'avgdl': avgdl, 'N': N}


def _bm25_search(brain: 'BrainIndex', query: str, max_citations: int,
                 kinds: Optional[tuple], min_score: Optional[float]) -> List[dict]:
    idx = brain._bm25()
    floor = SNAPSHOT_MIN_SCORE if min_score is None else min_score
    scores: Dict[int, float] = defaultdict(float)
    for t in set(_tok(query)):
        idf = idx['idf'].get(t)
        if not idf or not idx['avgdl']:
            continue
        for i, f in idx['inv'].get(t, ()):
            dl = idx['doclen'][i]
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / idx['avgdl'])
            if denom:
                scores[i] += idf * (f * (_BM25_K1 + 1)) / denom
    hits = []
    for i, s in scores.items():
        if s < floor:
            continue
        kind, _id, row = idx['docs'][i]
        if kinds and kind not in kinds:
            continue
        hits.append(_snapshot_cite(kind, row, s, brain.snapshot_date))
    # Relevance-first, tier a bounded tiebreak within an integer BM25 band.
    hits.sort(key=lambda c: (
        -int(c['relevance']), c['tier'], -_confidence_float(c),
        c.get('kind') or '', c.get('id', 0)))
    return hits[:max_citations]


def _snapshot_cite(kind: str, row: dict, score: float, snapshot_date: Optional[str]) -> dict:
    org = row.get('source_org')
    tier = get_tier_rank(org)
    cite = {
        'id': row.get('id'), 'kind': kind,
        'tier': tier, 'tier_icon': TIER_ICONS[tier],
        'source_org': org, 'source_url': row.get('source_url'),
        'name': row.get('name') or row.get('title'),
        'if_condition': (row.get('if_condition') or row.get('statement')
                         or row.get('description') or '')[:500],
        'then_action': (row.get('then_action') or row.get('explanation') or '')[:500],
        'domain_tag': row.get('domain_tag'),
        'relevance': round(score, 4),
        'retrieval_layer': 'bm25',
        'from': 'snapshot', 'snapshot_date': snapshot_date,
        'freshness': 'snapshot', 'last_verified': None,
    }
    if kind == 'ap':
        cite['risk_level'] = row.get('risk_level')
        cite['guidance_kind'] = 'avoid'
        cite['confidence_score'] = str(_NEUTRAL_AP_CONF)
    else:
        cite['confidence_score'] = str(_confidence_float(row))
        cite['guidance_kind'] = 'apply'
    return cite


# ----------------------------------------------------------------------
# DEPRECATION GUARD (contract §7) — deprecated-guidance.json lists
# case-insensitive regex/substring patterns for guidance the ecosystem has
# retired (seed: Google's 2023-08 HowTo rich-results deprecation + FAQ
# rich-results restriction). A candidate citation whose text matches is
# EXCLUDED from selection — an audit must never prescribe a dead feature.
# Callers with a stats channel (citation_attach, ground_fix_sources) count
# exclusions as `deprecated_excluded`; select_citations excludes silently.
# ----------------------------------------------------------------------

_DEPRECATED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'deprecated-guidance.json')
_DEPRECATED_CACHE: Optional[List[dict]] = None
_DEPRECATED_TEXT_FIELDS = ('name', 'title', 'if_condition', 'then_action',
                           'description', 'statement', 'explanation')


def deprecated_entries() -> List[dict]:
    """Load + compile deprecated-guidance.json once. Never raises; a missing
    or malformed file (or one bad pattern) degrades to fewer entries."""
    global _DEPRECATED_CACHE
    if _DEPRECATED_CACHE is None:
        entries = []
        try:
            with open(_DEPRECATED_PATH) as f:
                raw = json.load(f)
            for e in raw if isinstance(raw, list) else []:
                if not isinstance(e, dict) or not e.get('pattern'):
                    continue
                try:
                    e = dict(e)
                    e['_re'] = re.compile(str(e['pattern']), re.IGNORECASE)
                    entries.append(e)
                except re.error:
                    continue
        except Exception:
            entries = []
        _DEPRECATED_CACHE = entries
    return _DEPRECATED_CACHE


def deprecated_match(cite: Optional[dict]) -> Optional[dict]:
    """Return the deprecated-guidance entry whose pattern matches this
    candidate citation's text, else None. Case-insensitive; checks every
    text-bearing field the three snapshot kinds carry."""
    if not isinstance(cite, dict):
        return None
    text = ' '.join(str(cite.get(k) or '') for k in _DEPRECATED_TEXT_FIELDS)
    for entry in deprecated_entries():
        if entry['_re'].search(text):
            return entry
    return None


# ----------------------------------------------------------------------
# CITATION SELECTION (the deterministic ranker)
# ----------------------------------------------------------------------

def _confidence_float(rule: dict) -> float:
    """Parse confidence_score (string in snapshot) to float; default to 0.5."""
    raw = rule.get('confidence_score', 0.5)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.5


def select_citations(
    brain: BrainIndex,
    check_id: str,
    page_type: Optional[str] = None,
    industry: Optional[str] = None,
    max_citations: int = 3,
    include_anti_patterns: bool = True,
) -> List[dict]:
    """Deterministic citation selection for a failed check.

    Args:
        brain: loaded BrainIndex (from from_export_dir)
        check_id: e.g., 'D6_required_fields' (must exist in brain-mappings)
        page_type: optional, e.g., 'medical_business' (used if rule has tags)
        industry: optional, e.g., 'healthcare' (used if rule has tags)
        max_citations: how many to return (default 3)
        include_anti_patterns: include APs from the mapping (default True)

    Returns:
        List of rule/AP objects, sorted deterministically:
        Each entry includes: {kind: 'rule'|'ap', tier, tier_icon,
                              and all snapshot fields}.

    NOT a list of IDs — returns full objects ready for citation.
    """
    mapping = brain.check_to_rules.get(check_id, {})
    rule_ids = mapping.get('rules', [])
    ap_ids = mapping.get('anti_patterns', []) if include_anti_patterns else []

    candidates: List[dict] = []

    _snap = brain.snapshot_date

    # Collect rule candidates
    for rid in rule_ids:
        rule = brain.rules_by_id.get(rid)
        if not rule:
            continue
        candidates.append({
            **rule,
            'kind': 'rule',
            'tier': get_tier_rank(rule.get('source_org')),
            'tier_icon': TIER_ICONS[get_tier_rank(rule.get('source_org'))],
            'guidance_kind': 'apply',
            'from': 'snapshot', 'snapshot_date': _snap,
            'freshness': 'snapshot', 'last_verified': None,
        })

    # Collect anti-pattern candidates
    for apid in ap_ids:
        ap = brain.aps_by_id.get(apid)
        if not ap:
            continue
        # APs have no measured confidence. DO NOT fabricate one from risk_level
        # (that inflated high-risk APs above real high-confidence rules and
        # collapsed two orthogonal axes into the sort key). Use a neutral
        # constant for ranking; carry risk_level as its own field.
        candidates.append({
            **ap,
            'kind': 'ap',
            'tier': get_tier_rank(ap.get('source_org')),
            'tier_icon': TIER_ICONS[get_tier_rank(ap.get('source_org'))],
            'risk_level': ap.get('risk_level', 'medium'),
            'guidance_kind': 'avoid',
            'confidence_score': str(_NEUTRAL_AP_CONF),   # neutral, not risk-derived
            'from': 'snapshot', 'snapshot_date': _snap,
            'freshness': 'snapshot', 'last_verified': None,
        })

    # Deprecation guard (§7): curated mappings can outlive the guidance they
    # point at — drop candidates whose text prescribes a retired feature
    # (HowTo rich results, FAQ rich-result eligibility). No stats channel
    # here; the attach pass counts exclusions on the paths that reach findings.
    candidates = [c for c in candidates if deprecated_match(c) is None]

    # Filter by page_type if rules carry tags (gracefully degrade if untagged)
    if page_type:
        candidates = [
            c for c in candidates
            if not c.get('applies_to_page_types')
            or page_type in c['applies_to_page_types']
            or 'all' in c['applies_to_page_types']
        ]

    # Filter by industry if rules carry tags
    if industry:
        candidates = [
            c for c in candidates
            if not c.get('applies_to_industries')
            or industry in c['applies_to_industries']
            or 'all' in c['applies_to_industries']
        ]

    # Sort: tier ASC, confidence DESC, id ASC
    candidates.sort(key=lambda c: (
        c['tier'],
        -_confidence_float(c),
        c.get('id', 0),
    ))

    return candidates[:max_citations]


def format_citation(citation: dict) -> str:
    """Format a single citation in the standard report style.

    Output looks like:
        🥇 Per Schema.org — "Organization must include name and URL"
           [Sieve Rule #1668, confidence 0.98]
           Source: schema.org/Organization
    """
    icon = citation.get('tier_icon', '·')
    org = citation.get('source_org', 'unknown')
    name = citation.get('name') or citation.get('title', '(no name)')
    rid = citation.get('id', '?')
    kind = citation.get('kind', 'rule')
    kind_label = {'rule': 'Rule', 'ap': 'AP', 'anti_pattern': 'AP',
                  'principle': 'Principle'}.get(kind, 'Item')
    conf = _confidence_float(citation)
    url = citation.get('source_url', '')
    src_title = citation.get('source_title', '')

    lines = [
        f'{icon} Per {org} — "{name}"',
        f'   [Sieve {kind_label} #{rid}, confidence {conf:.2f}]',
    ]
    if url:
        lines.append(f'   Source: {url}')
    elif src_title:
        lines.append(f'   Source title: {src_title}')

    return '\n'.join(lines)


# ----------------------------------------------------------------------
# SELF-TEST
# ----------------------------------------------------------------------

def _selftest():
    """Run smoke tests against the actual snapshots in this directory."""
    here = os.path.dirname(os.path.abspath(__file__))
    brain = BrainIndex.from_export_dir(here)
    stats = brain.stats()

    print('=' * 60)
    print('Brain stats:')
    print('=' * 60)
    for k, v in stats.items():
        print(f'  {k}: {v}')
    print()

    # Test 1: tier mapping
    assert get_tier_rank('Google') == 1
    assert get_tier_rank('Schema.org') == 1
    assert get_tier_rank('backlinko.com') == 2
    assert get_tier_rank('Search Engine Land') == 3
    assert get_tier_rank('amsive.com') == 4
    assert get_tier_rank('Personal Blog') == 5
    assert get_tier_rank(None) == 5
    print('✓ Tier mapping works')

    # Test 2: BrainIndex loaded all 4 files
    assert stats['rules'] >= 4900, f'rules: {stats["rules"]}'
    assert stats['anti_patterns'] >= 2800
    assert stats['playbooks'] >= 1200
    assert stats['principles'] >= 3700
    print('✓ All 4 snapshots loaded')

    # Test 3: spot-check known rule lookups
    rule_1668 = brain.rules_by_id.get(1668)
    assert rule_1668 is not None, 'Rule #1668 missing from snapshot'
    assert rule_1668.get('source_org') == 'Schema.org', f'wrong source: {rule_1668.get("source_org")}'
    print(f'✓ Rule #1668: "{rule_1668["name"][:60]}" — {rule_1668["source_org"]}')

    ap_4763 = brain.aps_by_id.get(4763)
    assert ap_4763 is not None
    assert ap_4763.get('risk_level') == 'high'
    print(f'✓ AP #4763: "{ap_4763["title"][:60]}" — {ap_4763["risk_level"]} risk')

    # Test 4: citation selection determinism
    if 'D6_required_fields' in brain.check_to_rules:
        result1 = select_citations(brain, 'D6_required_fields',
                                    page_type='medical_business',
                                    industry='healthcare')
        result2 = select_citations(brain, 'D6_required_fields',
                                    page_type='medical_business',
                                    industry='healthcare')
        ids1 = [c['id'] for c in result1]
        ids2 = [c['id'] for c in result2]
        assert ids1 == ids2, f'NON-deterministic! {ids1} != {ids2}'
        print(f'✓ Deterministic: D6 returned {ids1} on both runs')

        if result1:
            print()
            print('Sample citation block (D6, healthcare):')
            print('-' * 60)
            for cit in result1:
                print(format_citation(cit))
                print()
    else:
        print('  (D6_required_fields not in brain-mappings; skipping selection test)')

    print()
    print('All ranker.py self-tests passed ✓')


if __name__ == '__main__':
    _selftest()
