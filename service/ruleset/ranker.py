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
#
# SINGLE SOURCE: org-tiers.json (same directory) carries the canon map +
# tier bands and is shared with the live path (sieve_brain.py) so the two
# tables cannot drift. The in-code sets below are the FALLBACK ONLY, kept
# for safety when the JSON is missing/malformed (this directory is a
# portable export — a consumer may copy ranker.py alone).
# ----------------------------------------------------------------------

_ORG_TIERS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'org-tiers.json')

# Fallback canon map (ported from sieve_brain.canon_org): the brain stores a
# mix of canonical names ('Google') and raw domains/variants
# ('developers.google.com', 'Google Search Central'). Canonicalize BEFORE the
# tier lookup so name-drift rules don't sink to tier 5 on the snapshot path.
_CANON_FALLBACK = {
    'google search central': 'Google', 'google': 'Google',
    'developers.google.com': 'Google', 'support.google.com': 'Google',
    'schema.org': 'Schema.org',
    'bing webmaster tools': 'Bing', 'bing.com': 'Bing', 'bing': 'Bing',
    'w3c': 'W3C', 'w3.org': 'W3C',
    'mdn web docs': 'MDN', 'developer.mozilla.org': 'MDN', 'mozilla': 'MDN',
    'web.dev': 'web.dev',
    'perplexity': 'Perplexity', 'perplexity.ai': 'Perplexity',
    'docs.perplexity.ai': 'Perplexity',
    'openai': 'OpenAI', 'platform.openai.com': 'OpenAI',
    'anthropic': 'Anthropic',
    'backlinko.com': 'Backlinko', 'backlinko': 'Backlinko',
    'moz.com': 'Moz', 'moz': 'Moz',
    'ahrefs.com': 'Ahrefs', 'ahrefs': 'Ahrefs',
    'semrush.com': 'Semrush', 'semrush': 'Semrush',
    'search engine land': 'Search Engine Land',
    'searchengineland.com': 'Search Engine Land',
    'search engine journal': 'Search Engine Journal',
    'searchenginejournal.com': 'Search Engine Journal',
}


def _load_org_tiers():
    """Load org-tiers.json → (canon_map, {canonical_name: tier}). Never
    raises; empty maps mean 'use the code fallbacks'."""
    canon, tiers = {}, {}
    try:
        with open(_ORG_TIERS_PATH) as f:
            data = json.load(f)
        canon = {str(k).strip().lower(): str(v)
                 for k, v in (data.get('canon') or {}).items()}
        for band, orgs in (data.get('tiers') or {}).items():
            for o in (orgs or []):
                tiers[str(o)] = int(band)
    except Exception:
        canon, tiers = {}, {}
    return canon, tiers


_SHARED_CANON, _SHARED_TIERS = _load_org_tiers()


def canon_org(org: Optional[str]) -> str:
    """Canonical source-org name (live-parity: mirrors sieve_brain.canon_org)."""
    if not org:
        return ''
    cmap = _SHARED_CANON or _CANON_FALLBACK
    key = org.strip().lower()
    if key in cmap:
        return cmap[key]
    key2 = re.sub(r'^www\.', '', key)
    if key2 in cmap:
        return cmap[key2]
    return org.strip()


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

# Tier 4 — Specialized / niche + the DELIBERATE practitioner band
# (growth-domain operators: ranks above anonymous tier-5, below tier-3).
TIER_4_SOURCES = {
    'amsive.com', 'almcorp.com', 'cxl.com',
    'seerinteractive.com', 'Y Combinator', 'apptweak',
    'Shopify', 'Buffer', 'frase.io', 'animalz.co',
    'b2bcontentos.com', 'appsflyer.com',
    'Reforge', 'a16z', 'First Round Review', 'For Entrepreneurs',
    'Demand Curve', 'Animalz', 'AppsFlyer', 'ALM Corp', 'Amsive',
    'CXL', 'Frase',
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
    """Map a source_org string to a tier rank (lower = more authoritative).

    Canonicalizes FIRST (live-parity via org-tiers.json + canon_org) so
    name-drift rows ('Google Search Central', 'moz') tier correctly, then
    looks the canonical name up in the shared tier table. The in-code sets
    remain as the fallback when org-tiers.json is absent."""
    if not source_org:
        return DEFAULT_TIER
    canon = canon_org(source_org)
    shared = _SHARED_TIERS.get(canon)
    if shared in (1, 2, 3, 4):
        return shared
    for tier, srcs in ((1, TIER_1_SOURCES), (2, TIER_2_SOURCES),
                       (3, TIER_3_SOURCES), (4, TIER_4_SOURCES)):
        if source_org in srcs or canon in srcs:
            return tier
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
            # principles AND playbooks are LOADED and, since search() (Phase 2
            # + the playbook wiring), genuinely retrievable — these counts
            # reflect real coverage, not dead weight.
            'principles': len(self.principles_by_id),
            'mapped_checks': len(self.check_to_rules),
        }

    # ------------------------------------------------------------------
    # Phase 2 — index-backed snapshot retrieval (BM25 over ALL four kinds).
    # The old snapshot path could only ever cite the 141 hand-mapped ids and
    # never a single principle or playbook. search() indexes rules +
    # anti_patterns + principles + playbooks and retrieves on the finding's
    # EVIDENCE, so the full library becomes reachable offline, with the same
    # relevance-floored, relevance-first, status-gated policy as the live path.
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
    'playbook':  ('name', 'summary', 'use_when'),
}

# ----------------------------------------------------------------------
# STATUS GATE — live-parity (_trust_filter in sieve_brain.py): guidance the
# curation lifecycle has retired (deprecated/rejected/retired/superseded, or
# rows superseded by a newer one) is unciteable from the snapshot too.
# Snapshots exported before `status` was carried have no such key — those
# rows are treated as active (the filter degrades open on absent fields,
# exactly like the live SQL's coalesce(t.status,'active')).
# ----------------------------------------------------------------------

EXCLUDED_STATUSES = frozenset({'deprecated', 'rejected', 'retired', 'superseded'})


def status_excluded(row: Optional[dict]) -> bool:
    """True when a snapshot row's lifecycle status makes it unciteable."""
    if not isinstance(row, dict):
        return False
    if str(row.get('status') or 'active').strip().lower() in EXCLUDED_STATUSES:
        return True
    return bool(row.get('superseded_by'))


def _row_freshness(row: dict) -> dict:
    """Freshness/lifecycle fields carried from the snapshot row WHEN PRESENT
    (post re-export); older snapshot files simply yield None values."""
    return {
        'last_verified': (str(row['last_verified'])[:10]
                          if row.get('last_verified') else None),
        'status': row.get('status'),
        'added': (str(row['created_at'])[:10]
                  if row.get('created_at') else None),
    }


def _tok(text: Optional[str]) -> List[str]:
    return [w for w in re.findall(r'[a-z0-9]+', (text or '').lower())
            if len(w) >= 3 and w not in _STOP]


def _doc_text(kind: str, row: dict) -> str:
    return ' '.join(str(row.get(f) or '') for f in _SEARCH_FIELDS[kind])


def _build_bm25(brain: 'BrainIndex') -> dict:
    # Status gate at INDEX time (live-parity): retired rows are never
    # retrievable. They stay in the by-id maps so the grounding path can
    # distinguish 'deprecated' from 'missing'.
    docs = []  # (kind, id, row)
    for kind, by_id in (('rule', brain.rules_by_id),
                        ('ap', brain.aps_by_id),
                        ('principle', brain.principles_by_id),
                        ('playbook', brain.playbooks_by_id)):
        for rid, row in by_id.items():
            if not status_excluded(row):
                docs.append((kind, rid, row))
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
        # Playbooks map use_when → the condition and summary → the action, so
        # the uniform when-it-applies/what-to-do citation shape holds.
        'if_condition': (row.get('if_condition') or row.get('statement')
                         or row.get('description') or row.get('use_when') or '')[:500],
        'then_action': (row.get('then_action') or row.get('explanation')
                        or row.get('summary') or '')[:500],
        'domain_tag': row.get('domain_tag'),
        'relevance': round(score, 4),
        'retrieval_layer': 'bm25',
        'from': 'snapshot', 'snapshot_date': snapshot_date,
        # Freshness carried from the export WHEN PRESENT, never fabricated.
        'freshness': 'snapshot', **_row_freshness(row),
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
# case-insensitive regex patterns for guidance the ecosystem has retired
# (seed: Google's 2023-08 HowTo rich-results deprecation + FAQ rich-results
# restriction). A candidate citation whose text matches is EXCLUDED from
# selection — an audit must never prescribe a dead feature.
#
# Patterns are anchored to PRESCRIPTIVE framing (imperative verb + feature,
# or feature tied to rich results in-clause), never mere mention, and each
# entry may carry a `negative_pattern`: a row whose text also matches it
# (anti-reliance phrasing — 'no longer', 'do not rely', 'deprecated',
# 'restricted to'...) is NOT excluded. The corpus's own deprecation notices
# (rule 'FAQ Schema No Longer Supports Rich Results', AP 'Relying on FAQ
# Schema for Rich Results') are exactly the citations to surface when the
# audited page carries the dead markup.
#
# Callers with a stats channel (citation_attach, ground_fix_sources) count
# exclusions as `deprecated_excluded`; select_citations has no stats channel
# so it counts into module-level DEPRECATION_STATS for observability.
# ----------------------------------------------------------------------

_DEPRECATED_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'deprecated-guidance.json')
_DEPRECATED_CACHE: Optional[List[dict]] = None
_DEPRECATED_TEXT_FIELDS = ('name', 'title', 'if_condition', 'then_action',
                           'description', 'statement', 'explanation',
                           'summary', 'use_when', 'avoid_when')

# select_citations has no stats channel; count its exclusions here so the
# quality loss is observable (attach/grounding count in their own stats).
DEPRECATION_STATS = {'select_citations_excluded': 0}


def deprecated_entries() -> List[dict]:
    """Load + compile deprecated-guidance.json once. Never raises; a missing
    or malformed file (or one bad pattern — positive OR negative) degrades to
    fewer entries."""
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
                    if e.get('negative_pattern'):
                        e['_neg_re'] = re.compile(str(e['negative_pattern']),
                                                  re.IGNORECASE)
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
    text-bearing field the three snapshot kinds carry. A row whose text also
    matches the entry's negative_pattern (anti-reliance phrasing: the row
    itself flags the deprecation) is NOT a match — such rows are current
    guidance the audit should cite, not stale guidance to suppress."""
    if not isinstance(cite, dict):
        return None
    text = ' '.join(str(cite.get(k) or '') for k in _DEPRECATED_TEXT_FIELDS)
    for entry in deprecated_entries():
        if entry['_re'].search(text):
            neg = entry.get('_neg_re')
            if neg is not None and neg.search(text):
                continue
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
    playbook_ids = mapping.get('playbooks', [])

    candidates: List[dict] = []

    _snap = brain.snapshot_date

    # Collect rule candidates (status gate: retired rows are unciteable,
    # live-parity with sieve_brain._trust_filter)
    for rid in rule_ids:
        rule = brain.rules_by_id.get(rid)
        if not rule or status_excluded(rule):
            continue
        candidates.append({
            **rule,
            'kind': 'rule',
            'tier': get_tier_rank(rule.get('source_org')),
            'tier_icon': TIER_ICONS[get_tier_rank(rule.get('source_org'))],
            'guidance_kind': 'apply',
            'from': 'snapshot', 'snapshot_date': _snap,
            'freshness': 'snapshot', **_row_freshness(rule),
        })

    # Collect anti-pattern candidates
    for apid in ap_ids:
        ap = brain.aps_by_id.get(apid)
        if not ap or status_excluded(ap):
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
            'freshness': 'snapshot', **_row_freshness(ap),
        })

    # Collect playbook candidates (mappings may curate them now that the
    # kind is retrievable; use_when/summary map onto the uniform shape)
    for pbid in playbook_ids:
        pb = brain.playbooks_by_id.get(pbid)
        if not pb or status_excluded(pb):
            continue
        candidates.append({
            **pb,
            'kind': 'playbook',
            'tier': get_tier_rank(pb.get('source_org')),
            'tier_icon': TIER_ICONS[get_tier_rank(pb.get('source_org'))],
            'if_condition': (pb.get('use_when') or '')[:500],
            'then_action': (pb.get('summary') or '')[:500],
            'guidance_kind': 'apply',
            'from': 'snapshot', 'snapshot_date': _snap,
            'freshness': 'snapshot', **_row_freshness(pb),
        })

    # Deprecation guard (§7): curated mappings can outlive the guidance they
    # point at — drop candidates whose text prescribes a retired feature
    # (HowTo rich results, FAQ rich-result eligibility). No stats channel
    # here, so count into module-level DEPRECATION_STATS for observability.
    kept = []
    for c in candidates:
        if deprecated_match(c) is None:
            kept.append(c)
        else:
            DEPRECATION_STATS['select_citations_excluded'] += 1
    candidates = kept

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
                  'principle': 'Principle',
                  'playbook': 'Playbook'}.get(kind, 'Item')
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
