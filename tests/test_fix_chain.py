#!/usr/bin/env python3
"""Fix chain (contract §2/§3/§7) — stdlib-only, no DB/network.

Quality invariants proven here:
  - agent._backstop_finding_fixes: fail/warn findings without a model-authored
    `fix` get the narrative fix for the same check_id (original id counts for
    renamed findings); list-shaped fixes are joined "; "; nothing is invented
  - main._audit_to_compact: the finding's own `fix` wins over the legacy
    title-keyword hint; fullReportUrl is forced to https for non-localhost
    hosts (proxy-terminated TLS) and left alone for localhost
  - /api/brain/retrieve evidence param: BrainQuery accepts `evidence`, the
    endpoint truncates it server-side to 400 chars, and retrieve_batch's query
    text is evidence-led via _query_for(check_id, evidence) (§3)
  - deprecation guard: deprecated-guidance.json patterns match retired
    guidance (HowTo rich results, FAQ rich-result promises) but NOT current
    guidance about the same features; excluded+counted in citation_attach and
    ground_fix_sources; excluded in ranker.select_citations (§7)

Run from the service dir:
    cd service && python3 ../tests/test_fix_chain.py
Prints FIX_CHAIN_OK on success, exits non-zero on failure.
"""

import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service', 'ruleset'))

# ---------------------------------------------------------------------------
# 1) agent._backstop_finding_fixes — deterministic narrative join (§2)
# ---------------------------------------------------------------------------
import agent  # noqa: E402

audit = {
    'findings': [
        # model authored a fix -> kept verbatim (capped)
        {'check_id': 'A1_https_enforcement', 'status': 'fail',
         'fix': '  Redirect http to https; add HSTS  '},
        # model emitted steps as a LIST -> joined "; " per contract
        {'check_id': 'A3_meta_description', 'status': 'warn',
         'fix': ['Write a 150-char meta description', 'Include the brand name']},
        # empty fix, narrative fix exists for the check -> joined
        {'check_id': 'D14_hreflang_coverage', 'status': 'fail', 'fix': ''},
        # renamed finding: narrative keyed on the model's ORIGINAL spelling
        {'check_id': 'A10_robots_txt_crawling', 'status': 'fail',
         'original_check_id': 'A10_robots_txt'},
        # no narrative fix anywhere -> stays fix-less, never invented
        {'check_id': 'F9_answer_capsule', 'status': 'warn'},
        # pass finding untouched
        {'check_id': 'D6_required_fields', 'status': 'pass'},
    ],
    'narrative': {
        'top_5_fixes': [
            {'check_id': 'D14_hreflang_coverage',
             'title': 'Add hreflang link tags for the en/de variants'},
            'junk-not-a-dict',
        ],
        'all_fixes': [
            {'check_id': 'A10_robots_txt',
             'title': 'Remove the Disallow rule blocking /blog'},
            {'check_id': 'D14_hreflang_coverage', 'title': 'dup — first wins'},
        ],
    },
}
stats = agent._backstop_finding_fixes(audit)
f = audit['findings']
assert f[0]['fix'] == 'Redirect http to https; add HSTS', f[0]
assert f[1]['fix'] == 'Write a 150-char meta description; Include the brand name', f[1]
assert f[2]['fix'] == 'Add hreflang link tags for the en/de variants', f[2]
assert f[3]['fix'] == 'Remove the Disallow rule blocking /blog', f[3]
assert 'fix' not in f[4] or not f[4].get('fix'), f[4]
assert 'fix' not in f[5], f[5]
assert stats == {'applied': True, 'eligible': 5, 'model_provided': 2,
                 'joined': 2, 'unfilled': 1}, stats

# Robustness: junk shapes never raise.
for shape in ({}, {'findings': None}, {'findings': [7]}, {'findings': [{}], 'narrative': 'x'}):
    s = agent._backstop_finding_fixes(dict(shape))
    assert s['applied'] is True, (shape, s)

# ---------------------------------------------------------------------------
# 2) main._audit_to_compact — fix field + https fullReportUrl (§2 + repo-local)
# ---------------------------------------------------------------------------
import main  # noqa: E402

compact_audit = {
    'audit_id': 'a1', 'url': 'https://x.example', 'domain': 'x.example',
    'scoring': {'overall_score': 70, 'overall_grade': 'C'},
    'findings': [
        {'check_id': 'A1_https_enforcement', 'status': 'fail', 'severity': 'high',
         'evidence': 'served over http',
         'fix': 'Redirect http to https; add HSTS header'},
        {'check_id': 'A3_meta_description', 'status': 'warn', 'severity': 'low',
         'evidence': 'meta description missing', 'fix_type': 'PAGE HTML FIX'},
    ],
}
c = main._audit_to_compact(compact_audit)
assert c['issues'][0]['fix'] == 'Redirect http to https; add HSTS header', c['issues'][0]
# no own fix -> legacy fallback (fix_type title-cased) still fills the slot
assert c['issues'][1]['fix'] == 'Page Html Fix', c['issues'][1]

_url = lambda scheme, netloc, host: types.SimpleNamespace(  # noqa: E731
    url=types.SimpleNamespace(scheme=scheme, netloc=netloc, hostname=host))
c = main._audit_to_compact(compact_audit, request=_url('http', 'auditor.up.railway.app', 'auditor.up.railway.app'))
assert c['fullReportUrl'] == 'https://auditor.up.railway.app/x.example', c['fullReportUrl']
c = main._audit_to_compact(compact_audit, request=_url('http', 'localhost:8000', 'localhost'))
assert c['fullReportUrl'] == 'http://localhost:8000/x.example', c['fullReportUrl']
c = main._audit_to_compact(compact_audit, request=_url('https', 'audits.growthmonk.ai', 'audits.growthmonk.ai'))
assert c['fullReportUrl'] == 'https://audits.growthmonk.ai/x.example', c['fullReportUrl']

# ---------------------------------------------------------------------------
# 3) /api/brain/retrieve evidence param (§3)
# ---------------------------------------------------------------------------
# BrainQuery accepts evidence; endpoint truncates to 400 and forwards it.
_real_sb = sys.modules.get('sieve_brain')
_stub = types.ModuleType('sieve_brain')
_captured = {}


def _stub_retrieve_batch(specs, min_tier=3, max_citations=3):
    _captured['specs'] = specs
    return {'live': True, 'results': {s['key']: [] for s in specs}}


_stub.retrieve_batch = _stub_retrieve_batch
sys.modules['sieve_brain'] = _stub
try:
    req = main.BrainRetrieveRequest(queries=[
        main.BrainQuery(key='k1', check_id='A1_https_enforcement',
                        evidence='E' * 600),
        main.BrainQuery(key='k2', q='free text'),
    ])
    out = main.api_brain_retrieve(req, True)
    assert out['live'] is True and out['requested'] == 2, out
    spec1 = _captured['specs'][0]
    assert spec1['check_id'] == 'A1_https_enforcement', spec1
    assert spec1['evidence'] == 'E' * 400, ('server-side truncate 400', len(spec1['evidence']))
    assert _captured['specs'][1]['evidence'] is None, _captured['specs'][1]
finally:
    if _real_sb is not None:
        sys.modules['sieve_brain'] = _real_sb
    else:
        del sys.modules['sieve_brain']

# retrieve_batch's query text is evidence-led, same construction as in-audit.
import sieve_brain  # noqa: E402

q = sieve_brain._spec_query_text({'check_id': 'A1_https_enforcement',
                                  'evidence': '  Site  SERVED over http, no HSTS  '})
assert q.startswith('site served over http, no hsts'), q
assert q == sieve_brain._query_for('A1_https_enforcement',
                                   'Site SERVED over http, no HSTS'), q
assert sieve_brain._spec_query_text({'q': 'free text', 'check_id': 'A1_x',
                                     'evidence': 'ignored'}) == 'free text'
assert sieve_brain._spec_query_text({}) == ''

# ---------------------------------------------------------------------------
# 4) Deprecation guard (§7) — matcher direction + all three exclusion sites
# ---------------------------------------------------------------------------
from ranker import BrainIndex, deprecated_match, select_citations  # noqa: E402

# Retired guidance matches...
dep = deprecated_match({'name': 'Add HowTo schema markup for rich results eligibility'})
assert dep is not None and dep['since'] == '2023-08', dep
assert deprecated_match({'then_action': 'Apply FAQPage schema markup to help '
                                        'pages qualify for FAQ rich results'}) is not None
# ...but CURRENT guidance about the same features does not:
assert deprecated_match({'name': 'Structure how-to guides with numbered steps'}) is None
assert deprecated_match({
    'name': 'FAQPage Eligibility Restricted to Government and Health Sites',
    'if_condition': 'a site implements FAQPage structured data and is not a '
                    'government or health site',
    'then_action': 'FAQ rich results will not be shown for this site'}) is None
assert deprecated_match(None) is None and deprecated_match({}) is None

# ranker.select_citations: a curated mapping pointing at retired guidance is
# excluded; the on-topic rule still comes through.
brain = BrainIndex(
    rules_by_id={
        10: {'id': 10, 'name': 'Add HowTo schema markup for rich results eligibility',
             'source_org': 'Google', 'confidence_score': '0.99'},
        11: {'id': 11, 'name': 'Provide FAQPage mainEntity with acceptedAnswer',
             'source_org': 'Schema.org', 'confidence_score': '0.98'},
    },
    aps_by_id={}, playbooks_by_id={}, principles_by_id={},
    check_to_rules={'D7_faq_schema': {'rules': [10, 11], 'anti_patterns': []}},
)
cites = select_citations(brain, 'D7_faq_schema')
assert [x['id'] for x in cites] == [11], cites

# citation_attach: excluded BEFORE the cap (a fresh candidate takes the slot)
# and counted in stats.deprecated_excluded.
import citation_attach  # noqa: E402


def _fake_qb(check_id, page_type, industry, max_citations, evidence=None,
             evidence_led=False):
    return {'citations': [
        {'id': 'dep', 'kind': 'rule',
         'name': 'Add HowTo schema markup for rich results eligibility'},
        {'id': 'g1', 'kind': 'rule', 'name': 'Enforce HTTPS sitewide',
         'then_action': 'redirect http to https and serve an hsts header'},
        {'id': 'g2', 'kind': 'rule', 'name': 'G2'},
        {'id': 'g3', 'kind': 'rule', 'name': 'G3'},
    ]}


citation_attach._query_brain_fn = _fake_qb
try:
    a, s = citation_attach.attach_citations({'findings': [
        {'check_id': 'A1_https_enforcement', 'status': 'fail',
         'evidence': 'site served over http, no HSTS header'},
    ]})
    ids = [x['id'] for x in a['findings'][0]['citations']]
    assert ids == ['g1', 'g2', 'g3'], ('deprecated cite must be excluded pre-cap', ids)
    assert s['deprecated_excluded'] == 1, s
    assert s['citations_attached'] == 3, s
finally:
    citation_attach._query_brain_fn = None

# ground_fix_sources: a WHY reference resolving to retired guidance is
# excluded+counted; the on-topic reference still mints its source.
import citation_grounding  # noqa: E402

_keep_cache = citation_grounding._SNAPSHOT_CACHE
citation_grounding._SNAPSHOT_CACHE = types.SimpleNamespace(
    rules_by_id={
        500: {'id': 500, 'name': 'Add HowTo schema markup for rich results eligibility',
              'if_condition': 'page contains instructional steps',
              'then_action': 'add howto schema markup with step images for rich results',
              'confidence_score': '0.97', 'source_org': 'Google',
              'source_url': 'https://ex/howto'},
        501: {'id': 501, 'name': 'Enforce HTTPS across the entire domain',
              'if_condition': 'IF the site is served over http',
              'then_action': 'THEN redirect http to https and add an HSTS header',
              'confidence_score': '0.95', 'source_org': 'Google',
              'source_url': 'https://ex/https'},
    },
    aps_by_id={}, principles_by_id={},
)
try:
    a3 = {'narrative': {'top_5_fixes': [
        {'title': 'Add HowTo schema markup for rich results',
         'why': 'Mark up the instructional steps with HowTo schema markup for '
                'rich results eligibility per Sieve Rule #500. Also enforce '
                'https: redirect http to https and add an HSTS header across '
                'the domain per Sieve Rule #501.'},
    ]}}
    a3, s3 = citation_grounding.ground_fix_sources(a3)
    srcs = a3['narrative']['top_5_fixes'][0].get('sources', [])
    assert [(x['kind'], x['id']) for x in srcs] == [('rule', '501')], srcs
    assert s3['deprecated_excluded'] == 1 and s3['resolved'] == 1, s3
finally:
    citation_grounding._SNAPSHOT_CACHE = _keep_cache

print('FIX_CHAIN_OK')
