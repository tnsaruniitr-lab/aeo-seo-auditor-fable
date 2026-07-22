#!/usr/bin/env python3
"""CITE-READINESS Phase 2 — stdlib-only (+main import for the compact seam).

Quality invariants proven here:
  - CONFIG LOADING: aeo-scoring-model.json loads + caches; a missing/corrupt
    config yields citeReadiness null (never a crash, never a fabricated score)
  - GATE ZERO vs INCONCLUSIVE: a tier-0 gate fail (e.g. GPTBot disallowed)
    zeroes citeReadiness WITH gates[] detail while classic PCR still scores;
    a transport-inconclusive probe yields citeReadiness null with a reason —
    the two paths are distinct (an unreached page is not a zero page)
  - unknown gates fail OPEN: missing robots/probe data never zeroes
  - RENORMALIZATION: score = sum(weight x presence)/sum(applicable weights);
    factors with no applicable check are excluded from both sums
  - DISPATCH DEFAULTING: local_business/high -> local_service; low confidence
    or absent classification -> product weights
  - E14 llms.txt: pass needs HTTP 200 AND a non-HTML text/markdown body;
    soft-200 HTML shells are rejected with the first ~100 chars as evidence
  - ROBOTS GPTBot/Google-Extended: per-bot tier-0 verdicts parsed per
    RFC 9309 (specific group beats wildcard; 5xx/unreachable -> unknown)
  - SURFACES: validate_audit clamps forged cite_readiness; _audit_to_compact
    carries nullable citeReadiness (scoring first, metadata mirror fallback)

Run from the service dir:
    cd service && python3 ../tests/test_cite_readiness.py
Prints CITE_READINESS_OK on success, exits non-zero on failure.
"""

import copy
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service', 'scripts'))

import scoring  # noqa: E402
from scoring import (  # noqa: E402
    clamp_cite_readiness, compute_cite_readiness, finalize_scoring,
    load_scoring_model, validate_audit,
)


def _base_audit():
    """A reachable, gate-clean audit with three factor-bearing findings."""
    return {
        'url': 'https://x.example/page',
        'classification': {'page_type': 'software_application',
                           'industry': 'saas', 'confidence': 'high'},
        'bots_eye_view': {
            'classification': 'fully_accessible',
            'summary': {'visible_words_default': 512,
                        'http_code_default': 200,
                        'final_url': 'https://x.example/page'},
            'probes': {'gpt': {'http_code': 200},
                       'claude': {'http_code': 200},
                       'perp': {'http_code': 200}},
        },
        'robots_ai_access': {
            'evaluated_path': '/page', 'basis': 'parsed',
            'bots': {b: {'allowed': True, 'root_allowed': True, 'evidence': 'ok'}
                     for b in ('GPTBot', 'ClaudeBot', 'Google-Extended',
                               'PerplexityBot')},
        },
        'scoring': {},
        'findings': [
            # faq_schema factor (weight 22): D9 pass + F4 pass + F3 pass -> 1.0
            {'check_id': 'D9_faqpage_schema_vs_visible', 'section': 'D', 'status': 'pass',
             'evidence': '6 visible FAQ pairs match 6 in FAQPage schema.'},
            {'check_id': 'F4_faq_semantic_markup', 'section': 'F', 'status': 'pass',
             'evidence': 'details/summary markup present.'},
            {'check_id': 'F3_faq_section', 'section': 'F', 'status': 'pass',
             'evidence': '6 Q&A pairs.'},
            # question_headings factor (weight 15): F6 warn -> 0.5
            {'check_id': 'F6_headings_as_questions', 'section': 'F', 'status': 'warn',
             'evidence': 'only 1 interrogative heading.'},
            # llms_txt factor (weight 12): E14 fail -> 0.0
            {'check_id': 'E14_llms_txt', 'section': 'E', 'status': 'fail',
             'evidence': 'No llms.txt: HTTP 404.'},
            # A1 for the https gate detail path
            {'check_id': 'A1_https_enforcement', 'section': 'A', 'status': 'pass',
             'evidence': 'HTTPS enforced.',
             'observed': {'method': 'measured-on-page',
                          'detail': {'final_url': 'https://x.example/page'}}},
        ],
    }


# ---------------------------------------------------------------------------
# 1) CONFIG LOADING — real file loads + caches; absence tolerated -> null
# ---------------------------------------------------------------------------
model = load_scoring_model()
assert isinstance(model, dict) and model.get('version'), 'config did not load'
assert model is load_scoring_model(), 'model not cached at module level'
assert any(f.get('id') == 'llms_txt' for f in model['tier1_factors']), model

_orig_path, _orig_cache = scoring._SCORING_MODEL_PATH, scoring._SCORING_MODEL_CACHE
scoring._SCORING_MODEL_PATH = '/nonexistent/aeo-scoring-model.json'
scoring._SCORING_MODEL_CACHE = False
assert load_scoring_model() is None, 'missing config must yield None'
assert compute_cite_readiness(_base_audit()) is None, \
    'citeReadiness must be null (not crash/fabricate) without the config'
sc_noconf = finalize_scoring(_base_audit())['scoring']
assert sc_noconf['cite_readiness'] is None and \
    'cite_readiness_reason' in sc_noconf, sc_noconf
assert sc_noconf['overall_score'] is not None, \
    'classic PCR must be untouched by a missing cite-readiness config'
scoring._SCORING_MODEL_PATH, scoring._SCORING_MODEL_CACHE = _orig_path, _orig_cache

# ---------------------------------------------------------------------------
# 2) RENORMALIZATION MATH — (22*1.0 + 15*0.5 + 12*0.0) / (22+15+12) * 100
#    = 29.5/49*100 = 60.2; na factors excluded from both sums
# ---------------------------------------------------------------------------
cr = compute_cite_readiness(_base_audit())
assert cr is not None, 'gate-clean audit must produce a citeReadiness object'
assert cr['score'] == 60.2, cr['score']
by_id = {f['id']: f for f in cr['factors']}
assert by_id['faq_schema']['status'] == 'pass' and by_id['faq_schema']['points'] == 22.0, by_id['faq_schema']
assert by_id['question_headings']['status'] == 'warn' and by_id['question_headings']['points'] == 7.5, by_id['question_headings']
assert by_id['llms_txt']['status'] == 'fail' and by_id['llms_txt']['points'] == 0.0, by_id['llms_txt']
assert by_id['llms_txt']['lift'] == 2.4, 'measured lift must ride the factor row'
assert by_id['depth_structure']['status'] == 'na', 'unmeasured factor must be na'
assert 'E14_llms_txt' in by_id['llms_txt']['check_ids'], by_id['llms_txt']
assert all(g['status'] == 'pass' for g in cr['gates']), cr['gates']
assert cr['calibration_version'] == model['version'], cr
assert cr['business_type'] == 'product' and cr['own_site_weight'] == 0.4, cr

# determinism
assert compute_cite_readiness(_base_audit()) == cr, 'non-deterministic'

# ---------------------------------------------------------------------------
# 3) GATE ZEROING vs INCONCLUSIVE — distinct paths
# ---------------------------------------------------------------------------
# 3a. GPTBot disallowed -> score 0 with gates[] detail; classic PCR unaffected
blocked = _base_audit()
blocked['robots_ai_access']['bots']['GPTBot']['allowed'] = False
sc_blocked = finalize_scoring(blocked)['scoring']
crb = sc_blocked['cite_readiness']
assert crb['score'] == 0.0, crb
assert crb['zeroed_by_gates'] == ['ai_bot_allowed'], crb
gate = next(g for g in crb['gates'] if g['id'] == 'ai_bot_allowed')
assert gate['status'] == 'fail' and 'GPTBot' in gate['evidence'], gate
assert crb['factors'], 'factors (forfeited points) must still be reported'
assert sc_blocked['overall_score'] is not None and \
    sc_blocked['overall_grade'] != 'INCONCLUSIVE', \
    'classic PCR must keep scoring when only cite-readiness zeroes'

# 3b. server_rendered gate: < 300 raw words -> zero
thin = _base_audit()
thin['bots_eye_view']['summary']['visible_words_default'] = 42
crt = compute_cite_readiness(thin)
assert crt['score'] == 0.0 and 'server_rendered' in crt['zeroed_by_gates'], crt

# 3c. transport-inconclusive -> citeReadiness NULL (not zero), classic INCONCLUSIVE
inconclusive = _base_audit()
inconclusive['bots_eye_view']['classification'] = 'unresolved_redirect'
sc_inc = finalize_scoring(inconclusive)['scoring']
assert sc_inc['overall_grade'] == 'INCONCLUSIVE', sc_inc['overall_grade']
assert sc_inc['cite_readiness'] is None, \
    'unreached page must stay null, never a zero'
assert 'transport inconclusive' in sc_inc['cite_readiness_reason'], sc_inc
assert compute_cite_readiness(inconclusive) is None, \
    'direct call must honor the transport gate too'

# 3d. unknown gates fail OPEN — no robots data + no probes != zero
unknown = _base_audit()
del unknown['robots_ai_access']
del unknown['bots_eye_view']['probes']
cru = compute_cite_readiness(unknown)
assert cru['score'] == 60.2, ('unknown gate must not zero', cru)
g_ai = next(g for g in cru['gates'] if g['id'] == 'ai_bot_allowed')
assert g_ai['status'] == 'unknown', g_ai

# ---------------------------------------------------------------------------
# 4) DISPATCH DEFAULTING
# ---------------------------------------------------------------------------
local = _base_audit()
local['classification'] = {'page_type': 'local_business',
                           'industry': 'healthcare', 'confidence': 'high'}
crl = compute_cite_readiness(local)
assert crl['business_type'] == 'local_service' and crl['own_site_weight'] == 1.0, crl
assert crl['classification_confidence'] == 'high', crl

lowconf = _base_audit()
lowconf['classification'] = {'page_type': 'local_business',
                             'industry': 'healthcare', 'confidence': 'low'}
crlow = compute_cite_readiness(lowconf)
assert crlow['business_type'] == 'product', \
    ('low confidence must default to product weights', crlow)
assert 'default' in crlow['dispatch_basis'], crlow

noclass = _base_audit()
del noclass['classification']
crn = compute_cite_readiness(noclass)
assert crn['business_type'] == 'product' and \
    crn['classification_confidence'] is None, crn

# ---------------------------------------------------------------------------
# 5) E14 llms.txt — pass / soft-200 reject / 404 / unreachable
# ---------------------------------------------------------------------------
import deterministic_checks as dc  # noqa: E402

_dc_fetch = dc.fetch


def _stub(body, status, ctype='text/plain'):
    def fake_fetch(url, timeout=15, allow_redirects=True, user_agent=None):
        return body, url, status, {'Content-Type': ctype}, []
    return fake_fetch


try:
    dc.fetch = _stub('# Acme\n\n- [Docs](https://x.example/docs): product docs\n', 200)
    r = dc.check_e14_llms_txt('https://x.example/page')
    assert r['status'] == 'pass', r
    assert r['detail']['soft_200_html'] is False, r
    assert '# Acme' in r['detail']['first_100_chars'], r

    dc.fetch = _stub('<!DOCTYPE html><html><head><title>Acme</title></head>'
                     '<body>app shell</body></html>', 200, 'text/html')
    r = dc.check_e14_llms_txt('https://x.example/page')
    assert r['status'] == 'fail', ('soft-200 HTML must be rejected', r)
    assert r['detail']['soft_200_html'] is True, r
    assert 'Soft-200' in r['evidence'] and '<!DOCTYPE html>' in r['evidence'], r
    assert len(r['detail']['first_100_chars']) <= 100, r

    # HTML sniff must work even with a lying text/plain content-type
    dc.fetch = _stub('<html><body>homepage</body></html>', 200, 'text/plain')
    assert dc.check_e14_llms_txt('https://x.example/')['status'] == 'fail'

    dc.fetch = _stub('', 404, 'text/html')
    r = dc.check_e14_llms_txt('https://x.example/page')
    assert r['status'] == 'fail' and '404' in r['evidence'], r

    dc.fetch = _stub('', 0)
    assert dc.check_e14_llms_txt('https://x.example/page')['status'] == 'na'

    dc.fetch = _stub('oops', 503)
    assert dc.check_e14_llms_txt('https://x.example/page')['status'] == 'na'
finally:
    dc.fetch = _dc_fetch

# E14 is registered as a measured base
from scoring import evidence_tier_for  # noqa: E402
assert evidence_tier_for('E14_llms_txt') == 'measured', 'E14 not in MEASURED_CHECK_BASES'

# ---------------------------------------------------------------------------
# 6) ROBOTS — per-bot tier-0 verdicts (GPTBot / Google-Extended parsing)
# ---------------------------------------------------------------------------
import check_robots_txt as crt_mod  # noqa: E402

assert 'GPTBot' in crt_mod.AI_CRAWLERS_ONLY and \
    'Google-Extended' in crt_mod.AI_CRAWLERS_ONLY, 'named bots missing from UA list'
assert set(crt_mod.TIER0_GATE_BOTS) == \
    {'GPTBot', 'ClaudeBot', 'Google-Extended', 'PerplexityBot'}

_robots_body = (
    'User-agent: *\nAllow: /\n\n'
    'User-agent: GPTBot\nDisallow: /\n\n'
    'User-agent: Google-Extended\nDisallow: /private\n'
)
parsed = crt_mod.parse_robots_txt(_robots_body)
acc = crt_mod.evaluate_tier0_bot_access(parsed, '/', True, False, 200)
assert acc['basis'] == 'parsed', acc
assert acc['bots']['GPTBot']['allowed'] is False, \
    ('GPTBot Disallow:/ must be detected', acc['bots']['GPTBot'])
assert acc['bots']['GPTBot']['root_allowed'] is False, acc['bots']['GPTBot']
assert acc['bots']['Google-Extended']['allowed'] is True, \
    ('root not under /private', acc['bots']['Google-Extended'])
assert acc['bots']['ClaudeBot']['allowed'] is True, \
    ('wildcard Allow must apply', acc['bots']['ClaudeBot'])
assert acc['bots']['PerplexityBot']['allowed'] is True, acc['bots']

acc_priv = crt_mod.evaluate_tier0_bot_access(parsed, '/private/page', True, False, 200)
assert acc_priv['bots']['Google-Extended']['allowed'] is False, \
    ('Google-Extended path Disallow must fire', acc_priv['bots']['Google-Extended'])
assert acc_priv['bots']['ClaudeBot']['allowed'] is True, acc_priv['bots']

# 5xx / unreachable robots -> unknown (None), never permissive-True or False
acc_5xx = crt_mod.evaluate_tier0_bot_access(parsed, '/', False, True, 503)
assert acc_5xx['basis'] == 'unavailable_5xx' and \
    all(v['allowed'] is None for v in acc_5xx['bots'].values()), acc_5xx
acc_net = crt_mod.evaluate_tier0_bot_access(parsed, '/', False, False, 0)
assert acc_net['basis'] == 'unreachable' and \
    acc_net['bots']['GPTBot']['allowed'] is None, acc_net
# 4xx = RFC 9309 permissive default
acc_404 = crt_mod.evaluate_tier0_bot_access(
    crt_mod.parse_robots_txt(''), '/', False, False, 404)
assert acc_404['basis'] == 'permissive_default' and \
    acc_404['bots']['GPTBot']['allowed'] is True, acc_404

# gate consumes the structured verdicts end-to-end
e2e = _base_audit()
e2e['robots_ai_access'] = {'basis': 'parsed', 'evaluated_path': '/',
                           'bots': acc['bots']}
cre = compute_cite_readiness(e2e)
assert cre['score'] == 0.0 and cre['zeroed_by_gates'] == ['ai_bot_allowed'], cre

# ---------------------------------------------------------------------------
# 7) CLAMP + SURFACES — validate_audit backstop, compact payload both copies
# ---------------------------------------------------------------------------
forged = finalize_scoring(_base_audit())
forged['scoring']['cite_readiness'] = {
    'score': '99"><script>', 'gates': [{'id': 'x', 'status': 'EVIL',
                                        'evidence': 'e' * 999}],
    'factors': [{'id': 'faq_schema', 'weight': 'NaN', 'status': 'bogus',
                 'points': float('inf'), 'check_ids': ['D9'], 'lift': 'x'}],
    'business_type': 'weird', 'classification_confidence': 'sure',
    'calibration_version': 'v' * 200,
}
vsc = validate_audit(forged)['scoring']['cite_readiness']
assert vsc['score'] is None or isinstance(vsc['score'], float), vsc
assert vsc['gates'][0]['status'] == 'unknown' and \
    len(vsc['gates'][0]['evidence']) <= 300, vsc
assert vsc['factors'][0]['status'] == 'na' and \
    vsc['factors'][0]['weight'] == 0.0 and vsc['factors'][0]['points'] == 0.0, vsc
assert vsc['factors'][0]['lift'] is None, vsc
assert vsc['business_type'] == 'product' and \
    vsc['classification_confidence'] is None, vsc
assert len(vsc['calibration_version']) <= 40, vsc
assert clamp_cite_readiness(None) is None and clamp_cite_readiness('x') is None

import main  # noqa: E402

# scoring copy first
finalized = finalize_scoring(_base_audit())
finalized['audit_id'] = 'cr-1'
finalized['domain'] = 'x.example'
compact = main._audit_to_compact(copy.deepcopy(finalized))
assert compact['citeReadiness'] is not None, 'compact must carry citeReadiness'
assert compact['citeReadiness']['score'] == 60.2, compact['citeReadiness']
assert compact['citeReadiness']['calibration_version'] == model['version']
assert compact['score'] is not None and compact['grade'] not in (None, ''), \
    'overall score/grade must keep riding PCR beside citeReadiness'

# metadata mirror fallback (reloaded audit: flat scoring, mirror in metadata)
reloaded = {
    'audit_id': 'cr-2', 'url': 'https://x.example', 'domain': 'x.example',
    'scoring': {'overall_score': 70, 'overall_grade': 'C'},
    'findings': [],
    'metadata': {'cite_readiness': finalized['scoring']['cite_readiness']},
}
compact2 = main._audit_to_compact(reloaded)
assert compact2['citeReadiness'] is not None and \
    compact2['citeReadiness']['score'] == 60.2, \
    'metadata mirror must survive the DB reload path'

# nullable: neither copy present -> null, payload still well-formed
compact3 = main._audit_to_compact({
    'audit_id': 'cr-3', 'url': 'https://x.example', 'domain': 'x.example',
    'scoring': {'overall_score': 70, 'overall_grade': 'C'}, 'findings': []})
assert compact3['citeReadiness'] is None

print('CITE_READINESS_OK config-null-tolerant; renormalized 60.2; '
      'gate-zero distinct from INCONCLUSIVE-null; unknown gates fail open; '
      'dispatch defaults to product on low confidence; E14 soft-200 rejected; '
      'GPTBot/Google-Extended per-bot verdicts; clamp + compact both copies')
