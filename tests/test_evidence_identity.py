#!/usr/bin/env python3
"""Evidence-identity chain (contract §1/§5/§6) — stdlib-only, no DB/network.

Quality invariants proven here:
  - scoring.observed_method_for: deterministic method labels — off-page
    families (H*/I*) are never labeled on-page; non-measured checks are
    'model-judgment', never dressed as measurements; H-family details that
    actually carry competitor-crawl data refine to 'observed-competitor'
  - agent._join_observed honesty gate: `observed` attaches ONLY on a real
    deterministic script match (no URL-only fallback); measured_value is the
    SCRIPT's evidence, never the model's rewording; model-emitted observed
    blocks are STRIPPED (observed is runtime-owned — a fabricated proof can't
    ride through the gate)
  - tools.query_brain evidence_led: the curated exact-mapping shortcut is
    skipped and the search query is led by evidence + the ORIGINAL id tail
  - _audit_to_compact carries vocabStatus/originalCheckId per issue (§1) and
    supportsFinding per citation (§6)
  - INDEX_HTML labels supports_finding===false cites "related — not direct
    proof" and sorts them after supporting cites within a tier

Run from the service dir:
    cd service && python3 ../tests/test_evidence_identity.py
Prints EVIDENCE_IDENTITY_OK on success, exits non-zero on failure.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))

# ---------------------------------------------------------------------------
# 1) observed_method_for — deterministic method labels (§5)
# ---------------------------------------------------------------------------
from scoring import observed_method_for  # noqa: E402

assert observed_method_for('A1_https_enforcement') == 'measured-on-page'
assert observed_method_for('det_checks:A10_robots_txt_crawling') == 'measured-on-page'
assert observed_method_for('H1_content_depth_vs_competitors') == 'observed-off-page'
assert observed_method_for('I2_brand_mentions') == 'observed-off-page'
assert observed_method_for('F3_answer_capsule') == 'model-judgment'
assert observed_method_for('') == 'model-judgment'
assert observed_method_for(None) == 'model-judgment'

# competitor refinement: H-family detail that ACTUALLY carries competitor data
# is 'observed-competitor'; anything else keeps its plain label
assert observed_method_for('H1_content_depth_vs_competitors',
                           {'competitors_crawled': 5}) == 'observed-competitor'
assert observed_method_for('det_checks:H4_schema_vs_competitors',
                           {'competitor_median_words': 2400}) == 'observed-competitor'
assert observed_method_for('H1_content_depth_vs_competitors',
                           {'words': 700}) == 'observed-off-page'
assert observed_method_for('H1_content_depth_vs_competitors',
                           {'competitors_crawled': 0}) == 'observed-off-page'
assert observed_method_for('H1_content_depth_vs_competitors',
                           {'competitors': []}) == 'observed-off-page'
assert observed_method_for('I2_brand_mentions',
                           {'competitors_crawled': 5}) == 'observed-off-page'
assert observed_method_for('A1_https_enforcement',
                           {'competitors_crawled': 5}) == 'measured-on-page'


# ---------------------------------------------------------------------------
# 2) agent._join_observed — honesty gate (§5)
# ---------------------------------------------------------------------------
import agent  # noqa: E402

audit = {'findings': [
    # matched script check: observed attaches, script's words win
    {'check_id': 'A1_https_enforcement', 'status': 'fail',
     'evidence': "the model's rewording of the problem"},
    # LLM-only finding, no script match: NO observed block at all
    {'check_id': 'F3_answer_capsule', 'status': 'fail', 'evidence': 'model judged'},
    # off-page family with a script match whose detail carries competitor
    # data: method must say competitor, not plain off-page
    {'check_id': 'H1_content_depth_vs_competitors', 'status': 'warn'},
    # model-emitted observed with no script match: STRIPPED, not preserved
    {'check_id': 'A5_robots_meta_indexing',
     'observed': {'method': 'measured-on-page', 'customer_url': 'fabricated'}},
]}
scripts_output = {
    'url': 'https://cust.example/page',
    'all_checks': {
        'det_checks:A1_https_enforcement': {
            'status': 'fail',
            'evidence': 'http not redirected to https; no HSTS header (script)',
            'detail': {'final_url': 'https://cust.example/page', 'hsts': False}},
        'det_checks:H1_content_depth_vs_competitors': {
            'status': 'warn',
            'evidence': 'competitor median 2400 words vs your 700',
            'detail': {'competitors_crawled': 5}},
    },
}
stats = agent._join_observed(audit, scripts_output)
f0, f1, f2, f3 = audit['findings']
assert f0['observed']['measured_value'] == \
    'http not redirected to https; no HSTS header (script)', f0
assert f0['observed']['method'] == 'measured-on-page', f0
assert f0['observed']['detail'] == {'final_url': 'https://cust.example/page',
                                    'hsts': False}, f0
assert f0['observed']['customer_url'] == 'https://cust.example/page', f0
assert 'observed' not in f1, ('no deterministic match -> no observed block '
                              '(URL-only fallback is gone)', f1)
assert f2['observed']['method'] == 'observed-competitor', f2
assert 'observed' not in f3, ('model-emitted observed must be stripped '
                              '(runtime-owned)', f3)
assert stats == {'applied': True, 'joined': 2, 'unmatched': 2,
                 'stripped_model_observed': 1, 'findings': 4}, stats

# junk shapes never raise
for shape in ({}, {'findings': None}, {'findings': ['x', 3]}):
    s = agent._join_observed(dict(shape), None)
    assert s['applied'] is True, (shape, s)
s = agent._join_observed({'findings': [{'check_id': 'A1_https_enforcement'}]},
                         {'all_checks': 'junk'})
assert s == {'applied': True, 'joined': 0, 'unmatched': 1,
             'stripped_model_observed': 0, 'findings': 1}, s


# ---------------------------------------------------------------------------
# 3) query_brain evidence_led — curated shortcut skipped, original-id query (§6)
# ---------------------------------------------------------------------------
import tools  # noqa: E402
import ranker  # noqa: E402

calls = {'curated': 0, 'search_q': None}


class _StubBrain:
    check_to_rules = {'A10_robots_txt_crawling': {'rules': [9]}}
    snapshot_date = '2026-04-21'

    def search(self, q, k):
        calls['search_q'] = q
        return [{'kind': 'rule', 'id': 1, 'name': 'robots txt crawl guidance'}]


def _stub_select(**kw):
    calls['curated'] += 1
    return [{'kind': 'rule', 'id': 9, 'name': 'curated pick'}]


_real_select = ranker.select_citations
_real_cache = tools._BRAIN_CACHE
ranker.select_citations = _stub_select
tools._BRAIN_CACHE = _StubBrain()
try:
    # evidence-led (foreign/renamed): curated mapping NEVER consulted; the
    # search query is evidence + the ORIGINAL id's tail
    res = tools.query_brain('A10_robots_txt', evidence='crawler blocked by disallow',
                            evidence_led=True)
    assert calls['curated'] == 0, 'curated shortcut must be skipped when evidence_led'
    assert calls['search_q'].startswith('crawler blocked by disallow'), calls
    assert 'robots txt' in calls['search_q'], calls
    assert res['citations'] and res['citations'][0]['id'] == 1, res
    assert res['resolved_to'] is None, ('evidence_led must not resolve to a '
                                        'canonical slot', res)

    # default path unchanged: curated mapping consulted first
    res2 = tools.query_brain('A10_robots_txt_crawling', evidence='x')
    assert calls['curated'] == 1, 'default path must still use the curated mapping'
    assert res2['citations'][0]['id'] == 9, res2
finally:
    ranker.select_citations = _real_select
    tools._BRAIN_CACHE = _real_cache


# ---------------------------------------------------------------------------
# 4) compact payload — vocabStatus/originalCheckId (§1) + supportsFinding (§6)
# ---------------------------------------------------------------------------
import main  # noqa: E402

compact = main._audit_to_compact({
    'audit_id': 'a1', 'url': 'https://x.example', 'domain': 'x.example',
    'scoring': {'overall_score': 70, 'overall_grade': 'C'},
    'findings': [
        {'check_id': 'A10_robots_txt_crawling', 'status': 'fail', 'severity': 'high',
         'vocab_status': 'aliased', 'original_check_id': 'A10_robots_txt',
         'evidence': 'robots.txt blocks the page',
         'citations': [
             {'kind': 'rule', 'id': 7, 'name': 'robots guidance', 'tier': 1,
              'source_org': 'Google', 'supports_finding': True},
             {'kind': 'rule', 'id': 8, 'name': 'adjacent guidance', 'tier': 1,
              'source_org': 'Google', 'supports_finding': False},
         ]},
        {'check_id': 'F9_capsule_density', 'status': 'warn', 'severity': 'low',
         'vocab_status': 'foreign', 'evidence': 'no capsule'},
    ],
})
i0, i1 = compact['issues']
assert i0['vocabStatus'] == 'aliased' and i0['originalCheckId'] == 'A10_robots_txt', i0
assert [c['supportsFinding'] for c in i0['citations']] == [True, False], i0
assert i1['vocabStatus'] == 'foreign' and i1['originalCheckId'] is None, i1

# legacy findings (no provenance stamps): fields present, null, nothing breaks
legacy = main._audit_to_compact({
    'audit_id': 'a2', 'domain': 'd', 'scoring': {},
    'findings': [{'check_id': 'A1', 'status': 'fail', 'evidence': 'x',
                  'citations': [{'kind': 'rule', 'id': 1, 'name': 'n'}]}],
})
li = legacy['issues'][0]
assert li['vocabStatus'] is None and li['originalCheckId'] is None, li
assert li['citations'][0]['supportsFinding'] is None, li


# ---------------------------------------------------------------------------
# 5) INDEX_HTML — "related, not proof" label + demotion sort (§6)
# ---------------------------------------------------------------------------
html = main.INDEX_HTML
assert 'related — not direct proof' in html, 'non-supporting cite label missing'
assert "c.supports_finding === false" in html, 'supports_finding gate missing'
# the tier sort must demote non-supporting cites BEFORE the URL-less tiebreak
sort_ix = html.index('a.supports_finding === false ? 1 : 0')
url_ix = html.index('a.source_url ? 0 : 1')
assert sort_ix < url_ix, 'supports_finding must be the primary within-tier sort key'

print('EVIDENCE_IDENTITY_OK')
