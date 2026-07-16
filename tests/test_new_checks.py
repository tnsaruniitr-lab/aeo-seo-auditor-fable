#!/usr/bin/env python3
"""Offline tests for the newly wired deterministic checks (roadmap 0.2) and
the evidence-tier plumbing (roadmap 0.1).

Covers, positive + negative, with NO network access (robots.txt bodies and
http-probe results are injected):
  - A5_robots_meta_indexing   noindex via meta robots AND X-Robots-Tag,
                              plus the robots.txt-vs-noindex contradiction
  - A1_https_enforcement      http→https redirect + HSTS presence
  - B9_no_mixed_content       active vs passive http:// subresources
  - A3_meta_description       presence / length band / duplicate-of-title
  - C10_open_graph_tags       og:title / og:description / og:image
  - E4_no_nosnippet_noarchive nosnippet / max-snippet:0 / data-nosnippet
  - E12_no_noarchive          noarchive directive
  - evidence tiers            scoring.evidence_tier_for + finalize_scoring
                              stamping + scoring['evidence_tiers'] counts
  - PCR_WEIGHTS               still sums to exactly 1.0 (no new categories)

Run from anywhere:  python3 tests/test_new_checks.py
Prints NEWCHECKS_OK on success, exits non-zero on failure.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'service'))
sys.path.insert(0, os.path.join(HERE, '..', 'service', 'scripts'))

import deterministic_checks as dc  # noqa: E402
import scoring  # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok  {name}')
    else:
        FAILURES.append(name)
        print(f'  FAIL {name} — {detail}')


with open(os.path.join(HERE, 'fixtures', 'new_checks_clean.html')) as f:
    CLEAN = f.read()
with open(os.path.join(HERE, 'fixtures', 'new_checks_broken.html')) as f:
    BROKEN = f.read()

HTTPS_URL = 'https://www.acme-widgets.example/widgets/precision'
ROBOTS_ALLOW = 'User-agent: *\nDisallow:\n'
ROBOTS_BLOCK = 'User-agent: *\nDisallow: /widgets/\n'


# ---------------------------------------------------------------- A3
print('[A3] meta description')
r = dc.check_a3_meta_description(CLEAN)
check('clean page passes (120-160 chars, distinct from title)',
      r['status'] == 'pass', r['evidence'])
r = dc.check_a3_meta_description(BROKEN)
check('duplicate-of-title warns',
      r['status'] == 'warn' and 'title' in r['evidence'].lower(), r['evidence'])
r = dc.check_a3_meta_description('<html><head><title>x</title></head></html>')
check('missing description fails', r['status'] == 'fail', r['evidence'])
r = dc.check_a3_meta_description(
    '<head><title>T</title><meta name="description" content="Too short."></head>')
check('short description warns',
      r['status'] == 'warn' and 'short' in r['evidence'], r['evidence'])

# ---------------------------------------------------------------- C10
print('[C10] Open Graph basics')
r = dc.check_c10_open_graph(CLEAN)
check('all og tags present passes', r['status'] == 'pass', r['evidence'])
r = dc.check_c10_open_graph(BROKEN)
check('no og tags fails', r['status'] == 'fail', r['evidence'])
r = dc.check_c10_open_graph('<meta property="og:title" content="Only title">')
check('partial og warns and names the missing tags',
      r['status'] == 'warn' and 'og:image' in r['evidence'], r['evidence'])

# ---------------------------------------------------------------- E4
print('[E4] nosnippet / max-snippet')
r = dc.check_e4_nosnippet_directives(CLEAN, {})
check('clean page passes (max-snippet:-1 is unrestricted)',
      r['status'] == 'pass', r['evidence'])
r = dc.check_e4_nosnippet_directives(BROKEN, {})
check('meta nosnippet fails', r['status'] == 'fail', r['evidence'])
r = dc.check_e4_nosnippet_directives('', {'X-Robots-Tag': 'max-snippet:0'})
check('X-Robots-Tag max-snippet:0 fails', r['status'] == 'fail', r['evidence'])
r = dc.check_e4_nosnippet_directives('', {'x-robots-tag': 'max-snippet:20'})
check('small max-snippet warns (case-insensitive header)',
      r['status'] == 'warn', r['evidence'])
r = dc.check_e4_nosnippet_directives('<div data-nosnippet>secret</div>', {})
check('data-nosnippet attribute warns', r['status'] == 'warn', r['evidence'])

# ---------------------------------------------------------------- E12
print('[E12] noarchive')
r = dc.check_e12_noarchive(CLEAN, {})
check('clean page passes', r['status'] == 'pass', r['evidence'])
r = dc.check_e12_noarchive(BROKEN, {})
check('meta noarchive fails', r['status'] == 'fail', r['evidence'])
r = dc.check_e12_noarchive('', {'X-Robots-Tag': 'googlebot: noarchive'})
check('UA-scoped X-Robots-Tag noarchive fails',
      r['status'] == 'fail', r['evidence'])

# ---------------------------------------------------------------- A5
print('[A5] robots meta indexing + robots.txt contradiction')
r = dc.check_a5_robots_meta_indexing(CLEAN, {}, HTTPS_URL,
                                     robots_txt=ROBOTS_ALLOW, robots_status=200)
check('indexable page passes', r['status'] == 'pass', r['evidence'])
r = dc.check_a5_robots_meta_indexing(BROKEN, {}, HTTPS_URL,
                                     robots_txt=ROBOTS_ALLOW, robots_status=200)
check('meta noindex fails (opts out)',
      r['status'] == 'fail' and 'opts out' in r['evidence'], r['evidence'])
r = dc.check_a5_robots_meta_indexing(BROKEN, {}, HTTPS_URL,
                                     robots_txt=ROBOTS_BLOCK, robots_status=200)
check('noindex + robots-blocked flags the contradiction',
      r['status'] == 'fail' and 'Contradiction' in r['evidence'], r['evidence'])
check('contradiction detail records robots_txt_blocked',
      r['detail'].get('robots_txt_blocked') is True, str(r['detail']))
r = dc.check_a5_robots_meta_indexing(CLEAN, {'X-Robots-Tag': 'noindex, nofollow'},
                                     HTTPS_URL, robots_txt=ROBOTS_ALLOW,
                                     robots_status=200)
check('X-Robots-Tag noindex fails even with clean meta robots',
      r['status'] == 'fail' and 'x-robots-tag' in str(r['detail']), r['evidence'])
r = dc.check_a5_robots_meta_indexing(CLEAN, {}, HTTPS_URL,
                                     robots_txt='', robots_status=404)
check('missing robots.txt (404) treated as permissive, still passes',
      r['status'] == 'pass', r['evidence'])

# ---------------------------------------------------------------- B9
print('[B9] mixed content')
r = dc.check_b9_mixed_content(CLEAN, 'https://www.acme-widgets.example/')
check('clean HTTPS page passes (anchors/canonical http:// ignored)',
      r['status'] == 'pass', r['evidence'])
r = dc.check_b9_mixed_content(BROKEN, 'https://www.broken-corp.example/')
check('active mixed content (script/stylesheet) fails',
      r['status'] == 'fail' and r['detail']['active_count'] == 2, r['evidence'])
r = dc.check_b9_mixed_content('<img src="http://x.example/a.png">',
                              'https://x.example/')
check('passive-only mixed content warns', r['status'] == 'warn', r['evidence'])
r = dc.check_b9_mixed_content(BROKEN, 'http://www.broken-corp.example/')
check('non-HTTPS page is na', r['status'] == 'na', r['evidence'])

# ---------------------------------------------------------------- A1
print('[A1] HTTPS enforcement')
HSTS = {'Strict-Transport-Security': 'max-age=63072000; includeSubDomains'}
probe_redirects = {'final_url': 'https://www.acme-widgets.example/', 'status': 200}
probe_stays_http = {'final_url': 'http://www.acme-widgets.example/', 'status': 200}
probe_dead = {'final_url': 'http://www.acme-widgets.example/', 'status': 0}

r = dc.check_a1_https_enforcement('https://www.acme-widgets.example/',
                                  'https://www.acme-widgets.example/',
                                  HSTS, [], http_probe=probe_redirects)
check('https + redirect + HSTS passes', r['status'] == 'pass', r['evidence'])
r = dc.check_a1_https_enforcement('https://www.acme-widgets.example/',
                                  'https://www.acme-widgets.example/',
                                  {}, [], http_probe=probe_redirects)
check('missing HSTS warns',
      r['status'] == 'warn' and 'Strict-Transport-Security' in r['evidence'],
      r['evidence'])
r = dc.check_a1_https_enforcement('https://www.acme-widgets.example/',
                                  'https://www.acme-widgets.example/',
                                  HSTS, [], http_probe=probe_stays_http)
check('live http duplicate (no redirect) fails',
      r['status'] == 'fail', r['evidence'])
r = dc.check_a1_https_enforcement('http://www.acme-widgets.example/',
                                  'https://www.acme-widgets.example/',
                                  HSTS, [{'from': 'http://...', 'to': 'https://...', 'status': 301}])
check('http input resolving to https counts as redirect observed (no probe)',
      r['status'] == 'pass' and r['detail']['redirect_enforced'] is True,
      r['evidence'])
r = dc.check_a1_https_enforcement('http://insecure.example/',
                                  'http://insecure.example/', {}, [])
check('page served over http fails', r['status'] == 'fail', r['evidence'])
r = dc.check_a1_https_enforcement('https://www.acme-widgets.example/',
                                  'https://www.acme-widgets.example/',
                                  HSTS, [], http_probe=probe_dead)
check('port-80-closed is not punished (passes with HSTS)',
      r['status'] == 'pass', r['evidence'])

# ---------------------------------------------------------------- tiers
print('[tiers] evidence tiers (roadmap 0.1)')
check("A5 is 'measured'",
      scoring.evidence_tier_for('A5_robots_meta_indexing') == 'measured')
check("prefixed det_checks:B1_ttfb is 'measured'",
      scoring.evidence_tier_for('det_checks:B1_ttfb') == 'measured')
check("F1 (LLM-judged check) is 'llm-judged'",
      scoring.evidence_tier_for('F1_first_paragraph_answers_query') == 'llm-judged')
check("A2 (title tag, LLM-classified) is 'llm-judged' while A2b is 'measured'",
      scoring.evidence_tier_for('A2_title_tag') == 'llm-judged'
      and scoring.evidence_tier_for('A2b_title_uniqueness_sample') == 'measured')
check("junk check_id falls back to 'llm-judged'",
      scoring.evidence_tier_for(None) == 'llm-judged'
      and scoring.evidence_tier_for('weird_id') == 'llm-judged')

audit = {
    'bots_eye_view': {'classification': 'fully_accessible'},
    'findings': [
        {'check_id': 'A5_robots_meta_indexing', 'section': 'A', 'status': 'fail'},
        {'check_id': 'F1_first_paragraph_answers_query', 'section': 'F',
         'status': 'pass'},
        {'check_id': 'E4_no_nosnippet_noarchive', 'section': 'E',
         'status': 'pass', 'evidence_tier': 'measured'},   # pre-set → preserved
        # G9 stays LLM-judged (G1 gained a deterministic implementation in 2.4)
        {'check_id': 'G9_content_freshness_recency', 'section': 'G',
         'status': 'warn', 'evidence_tier': 'BOGUS'},      # invalid → re-derived
    ],
}
finalized = scoring.finalize_scoring(audit)
fnd = finalized['findings']
check('finalize_scoring stamps evidence_tier on every finding',
      all(f.get('evidence_tier') in ('measured', 'llm-judged') for f in fnd),
      str([f.get('evidence_tier') for f in fnd]))
check('deterministic id stamped measured, LLM id stamped llm-judged',
      fnd[0]['evidence_tier'] == 'measured'
      and fnd[1]['evidence_tier'] == 'llm-judged')
check('invalid tier value re-derived (G9 → llm-judged)',
      fnd[3]['evidence_tier'] == 'llm-judged')
tiers = finalized['scoring'].get('evidence_tiers')
check("scoring metadata carries tier counts {measured: 2, llm-judged: 2}",
      tiers == {'measured': 2, 'llm-judged': 2}, str(tiers))
check('scoring still computes deterministically with tiers present',
      isinstance(finalized['scoring'].get('overall_score'), float),
      str(finalized['scoring'].get('overall_score')))

# run_all_checks stamps evidence_tier='measured' on every emitted check —
# verified structurally, offline, via the content-checks-skipped path (an
# HTTP 500 fetch is simulated by monkeypatching fetch; no check function
# executes, so no network is touched).
_orig_fetch = dc.fetch
dc.fetch = lambda url, **kw: ('<html>server error</html>', url, 500, {}, [])
try:
    out = dc.run_all_checks('https://offline.invalid/')
finally:
    dc.fetch = _orig_fetch
check('run_all_checks skips content checks on HTTP 500',
      out.get('content_checks_skipped') is True and len(out.get('checks', {})) > 0,
      str(out)[:200])
check("every run_all_checks result carries evidence_tier='measured'",
      all(c.get('evidence_tier') == 'measured'
          for c in out['checks'].values()), str(out['checks'])[:200])
check('new checks are wired into the orchestrator',
      {'A5_robots_meta_indexing', 'A1_https_enforcement', 'B9_no_mixed_content',
       'A3_meta_description', 'C10_open_graph_tags', 'E4_no_nosnippet_noarchive',
       'E12_no_noarchive'} <= set(out['checks']), str(sorted(out['checks'])))

# ---------------------------------------------------------------- weights
print('[weights] PCR_WEIGHTS unchanged')
check('PCR_WEIGHTS sums to exactly 1.0',
      abs(sum(scoring.PCR_WEIGHTS.values()) - 1.0) < 1e-9,
      str(sum(scoring.PCR_WEIGHTS.values())))
check('no new weight categories added (9 sections, I excluded)',
      set(scoring.PCR_WEIGHTS) == {
          'A_technical', 'B_performance', 'C_onpage', 'D_schema',
          'E_aeo_discovery', 'F_aeo_extraction', 'G_aeo_trust',
          'H_aeo_selection', 'J_entity'},
      str(sorted(scoring.PCR_WEIGHTS)))

# ---------------------------------------------------------------- result
if FAILURES:
    print(f'\n{len(FAILURES)} failure(s): {FAILURES}')
    sys.exit(1)
print('\nNEWCHECKS_OK')
