"""Phase 5 (delivery, supply side) — the compact API contract now emits
citations + the bound rule + a brain-mode disclosure. Previously
_audit_to_compact dropped citations entirely, so AnswerMonk's technical-audit
card could never show a source. Imports main (fastapi is available in CI).

Prints COMPACT_CITATIONS_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import main  # noqa: E402


def _audit(citsA_from, citsB_from):
    return {
        'audit_id': 'a1', 'url': 'https://x.example', 'domain': 'x.example',
        'scoring': {'overall_score': 70, 'overall_grade': 'C'},
        'findings': [
            {'check_id': 'A1_https', 'status': 'fail', 'severity': 'high',
             'evidence': 'served over http',
             'citations': [{'kind': 'rule', 'id': 1489, 'name': 'Enforce HTTPS',
                            'source_org': 'Perplexity', 'source_url': 'https://ex/https',
                            'tier': 1, 'tier_icon': '🥇', 'confidence_score': '0.95',
                            'from': citsA_from, 'freshness': 'live', 'last_verified': '2026-05-01'}],
             'bound_rule': {'kind': 'rule', 'id': 1489, 'name': 'Enforce HTTPS',
                            'source_url': 'https://ex/https', 'binding_verified': True,
                            'basis': 'deterministic'}},
            {'check_id': 'A3_meta', 'status': 'warn', 'severity': 'low',
             'evidence': 'meta description missing',
             'citations': [{'kind': 'rule', 'id': 8192, 'name': 'Meta desc limit',
                            'source_org': 'backlinko.com', 'source_url': 'https://ex/meta',
                            'tier': 2, 'from': citsB_from, 'freshness': 'snapshot',
                            'snapshot_date': '2026-05-03', 'last_verified': None}]},
        ],
    }


def test_issues_carry_slim_citations_and_binding():
    c = main._audit_to_compact(_audit('sieve-live', 'snapshot'))
    i0 = c['issues'][0]
    assert len(i0['citations']) == 1
    cit = i0['citations'][0]
    assert cit['sourceUrl'] == 'https://ex/https' and cit['sourceOrg'] == 'Perplexity'
    assert cit['tierIcon'] == '🥇' and cit['from'] == 'sieve-live'
    assert i0['boundRule']['bindingVerified'] is True
    assert i0['boundRule']['sourceUrl'] == 'https://ex/https'


def test_sources_mode_disclosure():
    assert main._audit_to_compact(_audit('sieve-live', 'snapshot'))['sourcesMode'] == 'mixed'
    assert main._audit_to_compact(_audit('sieve-live', 'sieve-live'))['sourcesMode'] == 'live'
    assert main._audit_to_compact(_audit('snapshot', 'snapshot'))['sourcesMode'] == 'snapshot'
    c = main._audit_to_compact(_audit('snapshot', 'snapshot'))
    assert c['snapshotDate'] == '2026-05-03'


def test_no_citations_is_none_mode_not_crash():
    audit = {'audit_id': 'a', 'domain': 'd', 'scoring': {},
             'findings': [{'check_id': 'A1', 'status': 'fail', 'evidence': 'x'}]}
    c = main._audit_to_compact(audit)
    assert c['issues'][0]['citations'] == []
    assert c['issues'][0]['boundRule'] is None
    assert c['sourcesMode'] == 'none' and c['snapshotDate'] is None


if __name__ == '__main__':
    test_issues_carry_slim_citations_and_binding()
    test_sources_mode_disclosure()
    test_no_citations_is_none_mode_not_crash()
    print('COMPACT_CITATIONS_OK')
