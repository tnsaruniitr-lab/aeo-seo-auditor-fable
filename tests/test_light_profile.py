#!/usr/bin/env python3
"""LIGHT audit profile — deterministic 8-factor checks, pipeline, tagging.

Covers:
  1. Each new/changed deterministic check over saved-bytes fixtures
     (light_rich.html / light_bare.html) — no live network anywhere.
  2. llms.txt evaluation incl. the SPA catch-all (HTML body) false-positive
     guard.
  3. Per-bot robots breakdown (check_robots_txt.per_bot_access) + the mapping
     onto canonical measured ids E1/E10/E13.
  4. Full light pipeline via injected fetchers: envelope shape, profile/target
     tagging, 100%-measured findings, deterministic scoring, shadow coverage,
     zero LLM calls, inconclusive transport path.
  5. AnswerMonk payload shape for target='competitor' (metadata tags carried;
     default full-profile payload byte-compatible: no metadata key).
  6. Vocabulary: new bases are measured; the LLM cousins (E5/F3/F6/F8) keep
     their llm-judged tier — full-profile behavior unchanged.
  7. API models + idempotency key separation (profile/target).

Run: python3 tests/test_light_profile.py
Prints LIGHT_PROFILE_OK on success; exits non-zero on failure.
"""

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'service'))
sys.path.insert(0, os.path.join(HERE, '..', 'service', 'scripts'))
os.environ.setdefault('AUDIT_MODE', 'deterministic')

import light_checks as lc  # noqa: E402
import check_robots_txt as crt  # noqa: E402
import light_profile as lp  # noqa: E402
from scoring import evidence_tier_for  # noqa: E402
from persistence import build_answermonk_payload  # noqa: E402

RICH = open(os.path.join(HERE, 'fixtures', 'light_rich.html')).read()
BARE = open(os.path.join(HERE, 'fixtures', 'light_bare.html')).read()

failures = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok: {name}')
    else:
        failures.append(name)
        print(f'  FAIL: {name} {detail}')


# ---------------------------------------------------------------------------
print('[1] factor checks over fixture bytes')
rich_checks = lc.run_page_checks(RICH)
bare_checks = lc.run_page_checks(BARE)

check('E5b depth pass on rich (>500 words)',
      rich_checks['E5b_raw_html_depth']['status'] == 'pass')
check('E5b depth fail on bare (<200 words)',
      bare_checks['E5b_raw_html_depth']['status'] == 'fail')
check('D15 LocalBusiness+geo pass on rich',
      rich_checks['D15_localbusiness_geo_schema']['status'] == 'pass'
      and rich_checks['D15_localbusiness_geo_schema']['detail']['geo']['latitude'] == 52.5296)
check('D15 fail on bare (no LB entity)',
      bare_checks['D15_localbusiness_geo_schema']['status'] == 'fail')
check('C13 city derived from schema address + found in title AND h1',
      rich_checks['C13_city_in_title_h1']['status'] == 'pass'
      and rich_checks['C13_city_in_title_h1']['detail']['city'] == 'Berlin'
      and rich_checks['C13_city_in_title_h1']['detail']['city_source'] == 'schema_address')
check('C13 na on bare (no city derivable)',
      bare_checks['C13_city_in_title_h1']['status'] == 'na')
c13_sup = lc.check_city_in_title_h1(RICH, city='München')
check('C13 supplied city not on page -> fail',
      c13_sup['status'] == 'fail' and c13_sup['detail']['city_source'] == 'request')
check('F3b FAQ present pass on rich (visible + schema)',
      rich_checks['F3b_faq_content_present']['status'] == 'pass')
check('F3b fail on bare', bare_checks['F3b_faq_content_present']['status'] == 'fail')
check('D9 schema-vs-visible pass on rich (all questions visible)',
      rich_checks['D9_faqpage_schema_vs_visible']['status'] == 'pass')
check('D9 na on bare', bare_checks['D9_faqpage_schema_vs_visible']['status'] == 'na')
# D9 mismatch: schema questions whose text is NOT in the visible HTML
mismatch_html = RICH.replace('Was kostet eine Behandlung ohne Rezept?', 'X1', 1)
# (only the visible copies matter — strip the FAQ section entirely instead)
mismatch_html = ('<html><head><script type="application/ld+json">'
                 + json.dumps({"@type": "FAQPage", "mainEntity": [
                     {"@type": "Question", "name": "Is this question visible anywhere?",
                      "acceptedAnswer": {"@type": "Answer", "text": "no"}},
                     {"@type": "Question", "name": "What about this second question?",
                      "acceptedAnswer": {"@type": "Answer", "text": "also no"}}]})
                 + '</script></head><body><p>Completely different visible text.</p></body></html>')
check('D9 mismatch fail (schema FAQ invisible)',
      lc.check_faq_schema_integrity(mismatch_html)['status'] == 'fail')
check('F6b question headings pass on rich',
      rich_checks['F6b_question_headings']['status'] == 'pass'
      and rich_checks['F6b_question_headings']['detail']['question_headings'] >= 3)
no_q = '<html><body><h2>Services</h2><h2>Pricing</h2><h2>Contact</h2></body></html>'
check('F6b fail when 0 of N headings are questions',
      lc.check_question_headings(no_q)['status'] == 'fail')
check('F6b warn when no headings at all',
      bare_checks['F6b_question_headings']['status'] == 'warn')
# Study pass rule (playbook 2026-07-21): >=2 interrogative headings passes,
# even at a low ratio (cited-winner median = 2, e.g. 2 of 7 H2s).
two_q = ('<html><body><h2>What does treatment cost?</h2>'
         '<h2>How long does a session take?</h2><h2>Services</h2>'
         '<h2>Pricing</h2><h2>Team</h2><h2>Insurance</h2>'
         '<h2>Contact</h2></body></html>')
check('F6b pass at exactly 2 question headings among 7 (study rule >=2)',
      lc.check_question_headings(two_q)['status'] == 'pass')
one_q = ('<html><body><h2>What does treatment cost?</h2><h2>Services</h2>'
         '<h2>Pricing</h2><h2>Team</h2><h2>Contact</h2></body></html>')
check('F6b warn at exactly 1 question heading',
      lc.check_question_headings(one_q)['status'] == 'warn')
check('E5b stamps its threshold basis (BEV SSR bands, not study depth bands)',
      rich_checks['E5b_raw_html_depth']['detail'].get('threshold_basis')
      == 'bev_ssr_classification')
check('F8b prices pass on rich (visible € amounts)',
      rich_checks['F8b_prices_visible']['status'] == 'pass'
      and rich_checks['F8b_prices_visible']['detail']['visible_price_matches'] >= 2)
check('F8b fail on bare', bare_checks['F8b_prices_visible']['status'] == 'fail')
schema_only = ('<html><body><p>Great service, contact us.</p>'
               '<script type="application/ld+json">'
               '{"@type":"Offer","price":"49.00","priceCurrency":"EUR"}'
               '</script></body></html>')
check('F8b warn when price only in schema',
      lc.check_prices_on_page(schema_only)['status'] == 'warn')

# ---------------------------------------------------------------------------
print('[2] llms.txt evaluation (incl. SPA catch-all guard) — canonical E14 bands')
check('llms.txt real text file -> pass',
      lc.check_llms_txt(200, '# MySite\n\n- /docs: product docs\n')['status'] == 'pass')
html_catchall = lc.check_llms_txt(200, '<!doctype html><html><body>app</body></html>')
check('llms.txt 200-but-HTML catch-all -> fail (not counted as present)',
      html_catchall['status'] == 'fail' and html_catchall['detail'].get('soft_200_html'))
check('llms.txt 404 -> fail', lc.check_llms_txt(404, None, 'HTTP 404')['status'] == 'fail')
check('llms.txt fetch error -> na (unknown, not asserted absent)',
      lc.check_llms_txt(None, None, 'timeout')['status'] == 'na')
check('llms.txt 5xx -> na (server error, not assessable)',
      lc.check_llms_txt(503, 'oops')['status'] == 'na')
check('llms.txt empty 200 -> fail', lc.check_llms_txt(200, '   ')['status'] == 'fail')

# Cross-path agreement: the light path (pre-fetched bytes) and the full
# profile's check_e14_llms_txt (fetch + classify) must return the SAME status
# for the same fixture bytes — E14 is one check id with one set of bands.
import deterministic_checks as dc  # noqa: E402

_E14_FIXTURES = [
    ('# MySite\n\n- /docs: product docs\n', 200, 'text/plain'),
    ('<!doctype html><html><body>app</body></html>', 200, 'text/html'),
    ('<html><body>homepage</body></html>', 200, 'text/plain'),  # lying ctype
    ('   ', 200, 'text/plain'),
    ('', 404, 'text/html'),
    ('oops', 503, 'text/plain'),
    ('', 0, ''),
]
_dc_fetch = dc.fetch
try:
    agree = True
    for body, status, ctype in _E14_FIXTURES:
        def _fake(url, timeout=15, allow_redirects=True, user_agent=None,
                  _b=body, _s=status, _c=ctype):
            return _b, url, _s, {'Content-Type': _c}, []
        dc.fetch = _fake
        full = dc.check_e14_llms_txt('https://x.example/page')['status']
        light = lc.check_llms_txt(status, body, None, ctype)['status']
        if full != light:
            agree = False
            print(f'  MISMATCH: status={status} full={full} light={light}')
    check('E14 light path == full deterministic path on identical bytes', agree)
finally:
    dc.fetch = _dc_fetch

# ---------------------------------------------------------------------------
print('[3] per-bot robots breakdown + canonical id mapping')
ROBOTS_BODY = """
User-agent: GPTBot
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: *
Disallow: /admin/
Sitemap: https://example.com/sitemap.xml
"""
parsed = crt.parse_robots_txt(ROBOTS_BODY)
access = crt.per_bot_access(parsed, '/')
check('GPTBot denied', access['GPTBot']['allowed'] is False
      and access['GPTBot']['explicit'] is True)
check('Google-Extended denied', access['Google-Extended']['allowed'] is False)
check('ClaudeBot allowed via wildcard', access['ClaudeBot']['allowed'] is True
      and access['ClaudeBot']['explicit'] is False)
check('Googlebot allowed', access['Googlebot']['allowed'] is True)
check('every checked bot has a verdict',
      set(access) == set(crt.BOTS_TO_CHECK))

robots_out = {
    'robots_txt': {'http_code': 200, 'reachable': True,
                   'sitemaps_declared': ['https://example.com/sitemap.xml']},
    'checks': {
        'target_path_not_disallowed': {'status': 'fail', 'severity': 'high',
                                       'evidence': 'blocked for: GPTBot'},
        'robots_declares_sitemap': {'status': 'pass', 'severity': 'info',
                                    'evidence': '1 sitemap declared'},
    },
    'per_bot_access': access,
}
rf = {f['check_id']: f for f in lp.robots_findings(robots_out, 'https://example.com/')}
check('E10 fails when GPTBot (OpenAI family) denied',
      rf['E10_claudebot_chatgpt_applebot']['status'] == 'fail'
      and 'GPTBot' in rf['E10_claudebot_chatgpt_applebot']['evidence'])
check('E1 PerplexityBot passes', rf['E1_perplexitybot_allowed']['status'] == 'pass')
check('E3 Googlebot passes', rf['E3_googlebot_allowed']['status'] == 'pass')
check('E13 CCBot passes', rf['E13_ccbot_llm_training_access']['status'] == 'pass')
check('A10 carries the script verdict', rf['A10_robots_txt_crawling']['status'] == 'fail')
check('A11 sitemap pass', rf['A11_sitemap_referenced']['status'] == 'pass')
unreachable = lp.robots_findings({'robots_txt': {'http_code': 503,
                                                 'reachable': False},
                                  'checks': {}, 'per_bot_access': {}},
                                 'https://example.com/')
check('unreachable robots -> per-bot verdicts degrade to warn',
      all(f['status'] in ('warn', 'na') for f in unreachable))

# ---------------------------------------------------------------------------
print('[4] full light pipeline via injected fetchers (no network)')
BEV_OK = {
    'classification': 'fully_accessible',
    'probes': {'default': {'h1_first': 'Physiotherapie in Berlin Mitte',
                           'http_code': 200}},
    'summary': {'http_code_default': 200, 'final_url': '',
                'redirects_followed': 0, 'visible_words_default': 583,
                'faq_visible': 3, 'faq_schema': 3,
                'faq_schema_questions_visible': 3, 'faq_integrity': 'ok',
                'same_html_as_404_url': False, 'soft_404_redirect': False,
                'cloaking_detected': False, 'bot_blocking_detected': False,
                'critical_issues': [], 'spa_signals': []},
}
FETCHERS = {
    'bev': lambda url: dict(BEV_OK),
    'robots': lambda url: dict(robots_out),
    'page': lambda url: (RICH, 200, None),
    'llms': lambda url: ('# PhysioPlus llms.txt\n- /preise\n', 200, None),
}
progress_hits = []
with tempfile.TemporaryDirectory() as td:
    audit = lp.run_light_audit(
        'https://physioplus-berlin.example/', output_dir=td,
        target='competitor', session_ref='sess-9', city=None,
        progress_callback=progress_hits.append,
        persist=False, fetchers=FETCHERS)
    json_written = bool(audit.get('json_path')
                        and os.path.exists(audit['json_path']))

check('envelope: profile/target/session_ref stamped',
      audit['profile'] == 'light' and audit['target'] == 'competitor'
      and audit['session_ref'] == 'sess-9'
      and audit['metadata']['profile'] == 'light'
      and audit['metadata']['target'] == 'competitor')
check('zero LLM calls', audit['metadata']['llm_calls'] == 0
      and audit['metadata']['cost_usd'] == 0.0
      and audit['narrative']['tokens_used'] == 0)
check('scoring is runtime-deterministic with a numeric score',
      audit['scoring']['computed_by'] == 'runtime-deterministic'
      and isinstance(audit['scoring']['overall_score'], (int, float)))
fnd = audit['findings']
ids = {f['check_id'] for f in fnd}
expected_ids = {
    'E1_perplexitybot_allowed', 'E2_bingpreview_allowed', 'E3_googlebot_allowed',
    'E10_claudebot_chatgpt_applebot', 'E13_ccbot_llm_training_access',
    'A10_robots_txt_crawling', 'A11_sitemap_referenced', 'E14_llms_txt',
    'E5b_raw_html_depth', 'D15_localbusiness_geo_schema', 'C13_city_in_title_h1',
    'F3b_faq_content_present', 'D9_faqpage_schema_vs_visible',
    'F6b_question_headings', 'F8b_prices_visible',
}
check('all 8 factors covered (15 checks emitted)', ids == expected_ids, ids ^ expected_ids)
check('every finding is measured with an on-page observed block',
      all(f['evidence_tier'] == 'measured'
          and f['observed']['method'] == 'measured-on-page'
          and f['observed']['customer_url'] for f in fnd))
check('deterministic variants marked',
      all(f.get('factor_variant') == 'deterministic'
          for f in fnd if f['check_id'] in
          ('E5b_raw_html_depth', 'F3b_faq_content_present',
           'F6b_question_headings', 'F8b_prices_visible',
           'C13_city_in_title_h1')))
check('no citations / no fix generation on the light path',
      all(f.get('citations') == [] for f in fnd)
      and audit['narrative']['top_5_fixes'] == []
      and audit['narrative']['why_not_cited'] == [])
shadow = audit['scoring'].get('shadow')
check('shadow score present and fully covered (all findings observed)',
      isinstance(shadow, dict)
      and shadow['coverage']['findings_counted'] == shadow['coverage']['findings_total'])
check('gates present with tier-0 verdicts',
      audit['gates']['content_access'] == 'pass'
      and audit['gates']['page_existence'] == 'pass')
check('json artifact written', json_written)
check('progress hints emitted with pct_hint',
      progress_hits and all('pct_hint' in p for p in progress_hits))
check('classification heuristic ran (local business page)',
      audit['classification']['page_type'] == 'local_business')

# Inconclusive transport path — http_error must yield INCONCLUSIVE, not a grade.
BEV_ERR = {'classification': 'http_error',
           'summary': {'http_code_default': 500, 'critical_issues': []},
           'probes': {}}
with tempfile.TemporaryDirectory() as td:
    audit_err = lp.run_light_audit(
        'https://physioplus-berlin.example/', output_dir=td, persist=False,
        fetchers={**FETCHERS, 'bev': lambda url: dict(BEV_ERR),
                  'page': lambda url: (None, 500, 'HTTP 500')})
check('transport-inconclusive -> INCONCLUSIVE grade, no content findings',
      audit_err['scoring']['overall_grade'] == 'INCONCLUSIVE'
      and audit_err['scoring']['overall_score'] is None
      and 'E5b_raw_html_depth' not in {f['check_id'] for f in audit_err['findings']}
      and audit_err['narrative'].get('inconclusive') is True)

# Degraded-BEV path — probe infrastructure fails but the page fetch succeeds:
# content_access must be derived from the fetched bytes (BEV word-count
# bands), NOT default to 'fail' on an empty classification.
BEV_DOWN = {'error': 'bev probe timed out after 120s'}
with tempfile.TemporaryDirectory() as td:
    audit_deg = lp.run_light_audit(
        'https://physioplus-berlin.example/', output_dir=td, persist=False,
        fetchers={**FETCHERS, 'bev': lambda url: dict(BEV_DOWN)})
check('degraded BEV + fetchable rich page -> content_access pass (derived), '
      'bev_degraded flagged, not inconclusive',
      audit_deg['gates']['content_access'] == 'pass'
      and audit_deg['gates']['details']['bev_classification'] == 'fully_accessible'
      and audit_deg['gates']['details']['bev_degraded'] is True
      and audit_deg['scoring']['overall_grade'] != 'INCONCLUSIVE'
      and isinstance(audit_deg['scoring']['overall_score'], (int, float)))

# ---------------------------------------------------------------------------
print('[5] AnswerMonk ingest payload — competitor tagging, back-compat')
payload = build_answermonk_payload(audit)
check('required ingest fields present',
      payload['audit_id'] == audit['audit_id']
      and payload['url'] == audit['url']
      and payload['domain'] == 'physioplus-berlin.example')
check('metadata carries profile/target/session_ref for competitor light audit',
      payload.get('metadata') == {'profile': 'light', 'target': 'competitor',
                                  'session_ref': 'sess-9'})
check('findings + jsonb blocks ride along',
      isinstance(payload.get('findings'), list) and len(payload['findings']) == 15
      and isinstance(payload.get('scoring'), dict)
      and isinstance(payload.get('gates'), dict))
# Back-compat: a default full-profile audit (no profile/target keys anywhere)
# must produce a payload with NO metadata key — byte-identical to before.
full_audit = {
    'audit_id': 'full-1', 'url': 'https://example.com/', 'domain': 'example.com',
    'date': '2026-07-22', 'classification': {'company_name': 'Ex'},
    'scoring': {'overall_score': 70}, 'findings': [{'check_id': 'A1'}],
    'narrative': {'executive_diagnosis': 'x'}, 'gates': {'crawlability': 'pass'},
    'metadata': {'version': '5.0-agent', 'cost_usd': 1.2},
}
full_payload = build_answermonk_payload(full_audit)
check('default full-profile payload unchanged (no metadata key)',
      'metadata' not in full_payload, full_payload.get('metadata'))
brand_light = dict(audit)
brand_light = json.loads(json.dumps(audit))
brand_light['target'] = 'brand'
brand_light['session_ref'] = None
brand_light['metadata']['target'] = 'brand'
brand_light['metadata']['session_ref'] = None
check('brand light audit still tags profile+target',
      build_answermonk_payload(brand_light).get('metadata') ==
      {'profile': 'light', 'target': 'brand'})

# ---------------------------------------------------------------------------
print('[6] vocabulary: new bases measured, LLM cousins untouched')
for cid in ('C13_city_in_title_h1', 'D15_localbusiness_geo_schema',
            'E14_llms_txt', 'E5b_raw_html_depth',
            'F3b_faq_content_present', 'F6b_question_headings',
            'F8b_prices_visible'):
    check(f'{cid.split("_")[0]} is measured', evidence_tier_for(cid) == 'measured')
for cid in ('E5_content_in_raw_html', 'F3_faq_section',
            'F6_headings_as_questions', 'F8_specific_facts'):
    check(f'{cid.split("_")[0]} (LLM cousin) stays llm-judged',
          evidence_tier_for(cid) == 'llm-judged')
mappings = json.load(open(os.path.join(HERE, '..', 'service', 'ruleset',
                                       'brain-mappings.json')))['mappings']
check('new ids registered in brain-mappings',
      all(k in mappings for k in ('C13_city_in_title_h1', 'E14_llms_txt',
                                  'F8b_prices_visible')))

# ---------------------------------------------------------------------------
print('[7] API models + idempotency key separation')
import main  # noqa: E402  (AUDIT_MODE=deterministic set above)
from main import (StartAuditRequest, BatchStartAuditRequest,  # noqa: E402
                  _find_recent_audit_for_url, JOBS, JOBS_LOCK)
r = StartAuditRequest(url='https://x.com')
check('defaults: profile=full target=brand (request without profile = today)',
      r.profile == 'full' and r.target == 'brand' and r.session_ref is None)
r2 = StartAuditRequest(url='https://x.com', profile='light',
                       target='competitor', sessionRef='s1', city='Berlin')
check('light/competitor/sessionRef/city accepted',
      (r2.profile, r2.target, r2.session_ref, r2.city) ==
      ('light', 'competitor', 's1', 'Berlin'))
try:
    BatchStartAuditRequest(urls=['a.com', 'b.com', 'c.com', 'd.com', 'e.com'])
    check('batch rejects >4 urls', False)
except Exception:
    check('batch rejects >4 urls', True)
b = BatchStartAuditRequest(urls=['a.com', 'b.com'], target='competitor',
                           sessionRef='s2')
check('batch model accepts up to 4 with target/sessionRef',
      b.target == 'competitor' and b.session_ref == 's2')
check('batch route registered',
      any(getattr(rt, 'path', '') == '/api/audit/start-batch'
          for rt in main.app.routes))
check('light jobs dispatch to a dedicated executor sized to the light pool '
      '(parallel batches, not BackgroundTasks serialization)',
      main._LIGHT_EXECUTOR._max_workers == main.MAX_CONCURRENT_LIGHT_AUDITS)

import time as _time  # noqa: E402
with JOBS_LOCK:
    JOBS['idem-full'] = {'audit_id': 'idem-full', 'status': 'running',
                         'url': 'https://same.com', '_submitted_at': _time.time()}
    JOBS['idem-light'] = {'audit_id': 'idem-light', 'status': 'running',
                          'url': 'https://same.com', 'profile': 'light',
                          'target': 'competitor', '_submitted_at': _time.time()}
check('full request matches only the full job',
      _find_recent_audit_for_url('https://same.com') == 'idem-full')
check('light+competitor request matches only the light job',
      _find_recent_audit_for_url('https://same.com', profile='light',
                                 target='competitor') == 'idem-light')
check('light+brand request matches neither (no supabase fallback for light)',
      _find_recent_audit_for_url('https://same.com', profile='light',
                                 target='brand') is None)
with JOBS_LOCK:
    JOBS.pop('idem-full', None)
    JOBS.pop('idem-light', None)

compact = main._audit_to_compact(audit)
check('compact result tags profile/target/sessionRef for the light audit',
      compact.get('profile') == 'light' and compact.get('target') == 'competitor'
      and compact.get('sessionRef') == 'sess-9')
full_compact = main._audit_to_compact(
    {'audit_id': 'x', 'url': 'u', 'domain': 'd',
     'scoring': {'overall_score': 70}, 'findings': [], 'narrative': {}})
check('default full-profile compact gains no tagging keys',
      'profile' not in full_compact and 'target' not in full_compact
      and 'sessionRef' not in full_compact)

check('per-job ETA drives progress derivation',
      main._derive_progress_pct({'status': 'running',
                                 'started_at': main.datetime.now(
                                     main.timezone.utc).isoformat(),
                                 'expected_seconds': 45}) >= 5)

# ---------------------------------------------------------------------------
if failures:
    print(f'\n{len(failures)} FAILED: {failures}')
    sys.exit(1)
print('\nLIGHT_PROFILE_OK tier-0 gates + 8 measured factors deterministic; '
      'profile/target/sessionRef tagged through envelope, compact, and '
      'answermonk metadata; idempotency keyed by profile+target; default '
      'full-profile payload byte-compatible')
