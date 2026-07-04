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
import os
from dataclasses import dataclass
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

        def _load_mappings() -> Dict[str, dict]:
            path = os.path.join(export_dir, 'brain-mappings.json')
            if not os.path.exists(path):
                return {}
            with open(path) as f:
                data = json.load(f)
            return data.get('mappings', {})

        return cls(
            rules_by_id=_load_array('rules-snapshot.json'),
            aps_by_id=_load_array('anti-patterns-snapshot.json'),
            playbooks_by_id=_load_array('playbooks-snapshot.json'),
            principles_by_id=_load_array('principles-snapshot.json'),
            check_to_rules=_load_mappings(),
        )

    def stats(self) -> Dict:
        return {
            'rules': len(self.rules_by_id),
            'anti_patterns': len(self.aps_by_id),
            'playbooks': len(self.playbooks_by_id),
            'principles': len(self.principles_by_id),
            'mapped_checks': len(self.check_to_rules),
        }


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
        })

    # Collect anti-pattern candidates
    for apid in ap_ids:
        ap = brain.aps_by_id.get(apid)
        if not ap:
            continue
        # APs use 'risk_level' instead of 'confidence_score' — translate
        risk = ap.get('risk_level', 'medium')
        ap_confidence = {'high': 0.95, 'medium': 0.80, 'low': 0.65}.get(risk, 0.75)
        candidates.append({
            **ap,
            'kind': 'ap',
            'tier': get_tier_rank(ap.get('source_org')),
            'tier_icon': TIER_ICONS[get_tier_rank(ap.get('source_org'))],
            'confidence_score': str(ap_confidence),  # synthesized for sort
        })

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
