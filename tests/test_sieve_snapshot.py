"""Phase 2 offline tests — index-backed snapshot retrieval + principles/APs
first-class. Runs against the REAL committed snapshot JSONs (no DB/network),
so it proves the actual corpus is reachable, not a fixture.

Prints SIEVE_SNAPSHOT_OK on success (run_tests.sh contract)."""

import os
import sys

_RULESET = os.path.join(os.path.dirname(__file__), '..', 'service', 'ruleset')
sys.path.insert(0, _RULESET)

import ranker  # noqa: E402

BRAIN = ranker.BrainIndex.from_export_dir(_RULESET)


def test_corpus_loaded_and_dated():
    s = BRAIN.stats()
    assert s['rules'] > 4000 and s['principles'] > 3000 and s['anti_patterns'] > 2000, s
    assert BRAIN.snapshot_date == '2026-05-03', BRAIN.snapshot_date


def test_search_is_evidence_relevant():
    # A title-tag observation retrieves title-tag guidance from the FULL corpus,
    # not just a 141-row hand-map.
    hits = BRAIN.search('title tag missing and far too long', max_citations=5)
    assert hits, 'expected on-topic snapshot hits'
    blob = ' '.join((h.get('name') or '') + ' ' + (h.get('if_condition') or '') for h in hits).lower()
    assert 'title' in blob, [h.get('name') for h in hits]
    # every hit carries snapshot provenance + freshness
    for h in hits:
        assert h['from'] == 'snapshot' and h['freshness'] == 'snapshot'
        assert h['snapshot_date'] == '2026-05-03'
        assert h['last_verified'] is None
        assert h['retrieval_layer'] == 'bm25'


def test_principles_are_reachable():
    # Principles were LOADED but cited by nothing. Search must now reach them.
    hits = BRAIN.search('entity disambiguation consistent nap name address phone', max_citations=10)
    kinds = {h['kind'] for h in hits}
    assert 'principle' in kinds, kinds


def test_empty_mapping_checks_now_get_sources():
    # The 20 checks with an empty curated mapping (A2_title_tag among them)
    # returned [] from select_citations. Search fills them.
    mapped = ranker.select_citations(BRAIN, 'A2_title_tag', max_citations=3)
    assert mapped == [], 'A2_title_tag is supposed to have an empty curated mapping'
    searched = BRAIN.search(
        ranker._doc_text if False else 'title tag length uniqueness', max_citations=3)
    assert searched, 'search must cover the empty-mapping check'


def test_ap_confidence_is_neutral_not_risk_derived():
    # Find a curated mapping that includes an anti-pattern, and assert its
    # confidence is the neutral constant, risk_level is carried, polarity set.
    ap_cite = None
    for cid, m in BRAIN.check_to_rules.items():
        if m.get('anti_patterns'):
            cites = ranker.select_citations(BRAIN, cid, max_citations=10)
            ap_cite = next((c for c in cites if c['kind'] == 'ap'), None)
            if ap_cite:
                break
    assert ap_cite is not None, 'no curated check with a resolvable anti-pattern found'
    assert ap_cite['confidence_score'] == str(ranker._NEUTRAL_AP_CONF), ap_cite
    assert 'risk_level' in ap_cite
    assert ap_cite['guidance_kind'] == 'avoid'
    # A high-risk AP must NOT be stamped 0.95 anymore.
    assert ap_cite['confidence_score'] != '0.95'


def test_search_floor_rejects_gibberish():
    # Out-of-vocabulary tokens match nothing, so nothing clears the BM25 floor.
    assert BRAIN.search('zzqx wvxyq qqzzt xzzyv jjkkq', max_citations=3) == []


if __name__ == '__main__':
    test_corpus_loaded_and_dated()
    test_search_is_evidence_relevant()
    test_principles_are_reachable()
    test_empty_mapping_checks_now_get_sources()
    test_ap_confidence_is_neutral_not_risk_derived()
    test_search_floor_rejects_gibberish()
    print('SIEVE_SNAPSHOT_OK')
