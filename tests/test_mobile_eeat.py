#!/usr/bin/env python3
"""Offline tests for roadmap 2.2 (mobile emulation + content parity + honest
CWV labeling) and 2.4 (deterministic E-E-A-T subset feeding the G section).

Covers, with NO network access:
  - AUDIT_MOBILE_RENDER flag semantics (default on; 0/false/no/off disable)
  - mobile emulation profile (390x844, DPR 3, mobile UA, touch)
  - mobile_parity.content_signals extraction
  - parity_check on synthetic desktop/mobile fixtures: identical → pass,
    thin mobile → fail, moderate divergence → warn, empty/na paths,
    determinism, check id + evidence_tier
  - honest CWV constants: 'lab (single run)' label, INP-requires-CrUX note,
    and that render_page_js never fabricates an INP value (source scan)
  - E-E-A-T checks G1 / G2 / G7b / G7c, positive + negative fixtures
  - evidence tiers for the new check ids (parents G7/A9 stay llm-judged)
  - run_all_checks wiring (via the offline content-checks-skipped path)
  - PCR_WEIGHTS unchanged (sum 1.0, same 9 sections)

Run from anywhere:  python3 tests/test_mobile_eeat.py
Prints MOBILE_EEAT_OK on success, exits non-zero on failure.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'service'))
sys.path.insert(0, os.path.join(HERE, '..', 'service', 'scripts'))

import mobile_parity as mp  # noqa: E402
import scoring  # noqa: E402
import deterministic_checks as dc  # noqa: E402

FAILURES = []


def check(name, cond, detail=''):
    if cond:
        print(f'  ok  {name}')
    else:
        FAILURES.append(name)
        print(f'  FAIL {name} — {detail}')


def fixture(name):
    with open(os.path.join(HERE, 'fixtures', name)) as f:
        return f.read()


DESKTOP = fixture('parity_desktop.html')
MOBILE_THIN = fixture('parity_mobile_thin.html')
EEAT_RICH = fixture('eeat_rich.html')
EEAT_BARE = fixture('eeat_bare.html')


# ---------------------------------------------------------------- flag
print('[flag] AUDIT_MOBILE_RENDER semantics')
check('default (unset) is ON', mp.mobile_render_enabled(env={}) is True)
check("'0' disables", mp.mobile_render_enabled(env={'AUDIT_MOBILE_RENDER': '0'}) is False)
check("'false' disables", mp.mobile_render_enabled(env={'AUDIT_MOBILE_RENDER': 'False'}) is False)
check("'off' disables", mp.mobile_render_enabled(env={'AUDIT_MOBILE_RENDER': 'off'}) is False)
check("'1' enables", mp.mobile_render_enabled(env={'AUDIT_MOBILE_RENDER': '1'}) is True)
check('flag env var name is AUDIT_MOBILE_RENDER',
      mp.MOBILE_FLAG_ENV == 'AUDIT_MOBILE_RENDER')

kw = mp.mobile_context_kwargs()
check('mobile profile: 390x844 viewport, DPR 3, touch, mobile UA',
      kw['viewport'] == {'width': 390, 'height': 844}
      and kw['device_scale_factor'] == 3
      and kw['is_mobile'] is True and kw['has_touch'] is True
      and 'iPhone' in kw['user_agent'], str(kw))

# ---------------------------------------------------------------- cwv labels
print('[cwv] honest lab labeling constants')
check("CWV label is 'lab (single run)'", mp.CWV_LAB_LABEL == 'lab (single run)')
check('INP note says field data / CrUX and refuses a lab value',
      'CrUX' in mp.INP_FIELD_NOTE and 'INP' in mp.INP_FIELD_NOTE
      and 'no INP value' in mp.INP_FIELD_NOTE, mp.INP_FIELD_NOTE)
# render_page_js must label lab CWV and must not fabricate INP: the tool
# result carries cwv_source + inp_note, and never an inp_ms metric.
tools_src = open(os.path.join(HERE, '..', 'service', 'tools.py')).read()
check('render_page_js stamps cwv_source + inp_note (source-verified)',
      '"cwv_source"' in tools_src and '"inp_note"' in tools_src)
check('render_page_js never emits an INP number (no inp_ms key)',
      'inp_ms' not in tools_src)

# ---------------------------------------------------------------- signals
print('[signals] content_signals extraction')
sig = mp.content_signals(DESKTOP)
check('desktop fixture: text + 5 headings + title/h1/meta extracted',
      sig['text_chars'] > 800 and sig['heading_count'] == 5
      and sig['title'].startswith('Precision Widgets')
      and sig['h1_first'].startswith('precision widgets')
      and sig['meta_description'].startswith('Compare precision'),
      str({k: sig[k] for k in ('text_chars', 'heading_count', 'title')}))
empty = mp.content_signals('')
check('empty html yields zeroed signals',
      empty['text_chars'] == 0 and empty['headings'] == []
      and empty['title'] is None)

# ---------------------------------------------------------------- parity
print('[parity] mobile-vs-desktop comparator')
r = mp.parity_check(DESKTOP, DESKTOP)
check('identical passes', r['status'] == 'pass', r['evidence'])
check('parity check id + measured tier',
      r['check_id'] == 'A9b_mobile_content_parity'
      and r['evidence_tier'] == 'measured', str(r))
check('identical ratio is 1.0',
      r['detail']['text_ratio_mobile_vs_desktop'] == 1.0, str(r['detail']))

r = mp.parity_check(DESKTOP, MOBILE_THIN)
check('thin mobile fails', r['status'] == 'fail', r['evidence'])
check('fail detail: ratio < 0.5 and missing headings recorded',
      r['detail']['text_ratio_mobile_vs_desktop'] < 0.5
      and r['detail']['headings_missing_count'] == 4, str(r['detail']))
check('fail evidence names mobile-first indexing',
      'mobile-first indexing' in r['evidence'], r['evidence'])

# Moderate divergence: same text volume, one heading removed → warn
moderate_mobile = DESKTOP.replace(
    '<h2>Maintenance planner</h2>', '<p>Maintenance planner</p>')
r = mp.parity_check(DESKTOP, moderate_mobile)
check('one missing heading warns', r['status'] == 'warn', r['evidence'])
check('warn detail names the missing heading',
      r['detail']['headings_missing_on_mobile'] == ['maintenance planner'],
      str(r['detail']['headings_missing_on_mobile']))

# Key-element mismatch only (different title) → warn
retitled = DESKTOP.replace(
    '<title>Precision Widgets — Full Catalog and Buying Guide</title>',
    '<title>Acme</title>')
r = mp.parity_check(DESKTOP, retitled)
check('title mismatch warns', r['status'] == 'warn'
      and r['detail']['title_match'] is False, r['evidence'])

r = mp.parity_check(DESKTOP, '')
check('empty mobile html is na', r['status'] == 'na', r['evidence'])
r = mp.parity_check('', DESKTOP)
check('empty desktop html is na', r['status'] == 'na', r['evidence'])

# Flag-off / failure path result shape (what render_page_js attaches)
r = mp.parity_na('mobile render disabled via AUDIT_MOBILE_RENDER=0')
check('parity_na is a valid na check result',
      r['status'] == 'na' and r['check_id'] == 'A9b_mobile_content_parity'
      and r['evidence_tier'] == 'measured'
      and 'AUDIT_MOBILE_RENDER' in r['evidence'], str(r))

# Determinism: same inputs → identical output
check('parity_check is deterministic',
      mp.parity_check(DESKTOP, MOBILE_THIN) == mp.parity_check(DESKTOP, MOBILE_THIN))

# ---------------------------------------------------------------- G1
print('[G1] author byline')
r = dc.check_g1_author_byline(EEAT_RICH)
check('rich fixture passes (visible byline + schema author)',
      r['status'] == 'pass' and 'Jane Smith' in str(r['detail']), r['evidence'])
r = dc.check_g1_author_byline(EEAT_BARE)
check('bare fixture fails (no byline, no schema author)',
      r['status'] == 'fail', r['evidence'])
check("prose 'By using our services' is not a byline false positive",
      'using' not in str(dc.check_g1_author_byline(EEAT_BARE)['detail']).lower())
schema_only = ('<html><body><p>No attribution shown.</p>'
               '<script type="application/ld+json">'
               '{"@type":"Article","author":{"@type":"Person","name":"Omar Haddad"}}'
               '</script></body></html>')
r = dc.check_g1_author_byline(schema_only)
check('schema-only author warns (add a visible byline)',
      r['status'] == 'warn' and 'Omar Haddad' in r['evidence'], r['evidence'])
r = dc.check_g1_author_byline('<div class="byline">Dr. Lena Fox</div>')
check('byline-class markup counts as visible attribution',
      r['status'] == 'pass', r['evidence'])
r = dc.check_g1_author_byline('<p>Written by Marta Alvarez</p>')
check("'Written by <Name>' matches case-insensitively on the verb",
      r['status'] == 'pass' and 'Marta Alvarez' in str(r['detail']),
      r['evidence'])
r = dc.check_g1_author_byline(
    '<html><body><main><h1>Payments platform</h1><p>Automate invoices.</p></main></body></html>')
check('non-editorial SaaS homepage is not penalized for lacking a byline',
      r['status'] == 'na' and 'not required' in r['evidence'], r['evidence'])

# ---------------------------------------------------------------- G2
print('[G2] schema-author linkage')
r = dc.check_g2_schema_author_linkage(EEAT_RICH)
check('rich fixture passes (Article author → Person with sameAs)',
      r['status'] == 'pass' and 'sameAs' in r['evidence'], r['evidence'])
r = dc.check_g2_schema_author_linkage(EEAT_BARE)
check('Article without author fails', r['status'] == 'fail', r['evidence'])
bare_string_author = ('<script type="application/ld+json">'
                      '{"@type":"BlogPosting","author":"Jane Smith"}</script>')
r = dc.check_g2_schema_author_linkage(bare_string_author)
check('bare-string author warns (unlinked)', r['status'] == 'warn', r['evidence'])
person_no_sameas = ('<script type="application/ld+json">'
                    '{"@type":"NewsArticle","author":{"@type":"Person","name":"J S"}}'
                    '</script>')
r = dc.check_g2_schema_author_linkage(person_no_sameas)
check('Person author without sameAs/jobTitle warns',
      r['status'] == 'warn', r['evidence'])
graph_ref = ('<script type="application/ld+json">'
             '{"@graph":['
             '{"@type":"Article","author":{"@id":"#jane"}},'
             '{"@type":"Person","@id":"#jane","name":"Jane Smith",'
             '"sameAs":["https://linkedin.example/jane"]}'
             ']}</script>')
r = dc.check_g2_schema_author_linkage(graph_ref)
check('author @id reference resolves through @graph',
      r['status'] == 'pass' and 'Jane Smith' in r['evidence'], r['evidence'])
r = dc.check_g2_schema_author_linkage('<html><body>plain</body></html>')
check('no Article/author schema is na', r['status'] == 'na', r['evidence'])

# ---------------------------------------------------------------- G7b
print('[G7b] about/contact discoverability')
r = dc.check_g7b_about_contact(EEAT_RICH)
check('rich fixture passes (nav about-us + contact, footer impressum)',
      r['status'] == 'pass', r['evidence'])
check('nav/footer membership recorded',
      r['detail']['about_in_nav_or_footer'] is True
      and r['detail']['contact_in_nav_or_footer'] is True, str(r['detail']))
r = dc.check_g7b_about_contact(EEAT_BARE)
check('bare fixture fails (no about, no contact)',
      r['status'] == 'fail', r['evidence'])
r = dc.check_g7b_about_contact('<footer><a href="/impressum">Impressum</a></footer>')
check('impressum-only warns (about missing)',
      r['status'] == 'warn' and 'about' in r['evidence'].lower(), r['evidence'])

# ---------------------------------------------------------------- G7c
print('[G7c] editorial/review-policy link')
r = dc.check_g7c_editorial_policy(EEAT_RICH)
check('rich fixture passes (footer editorial-policy link)',
      r['status'] == 'pass', r['evidence'])
r = dc.check_g7c_editorial_policy(EEAT_BARE)
check('bare fixture warns (never fails — YMYL-weighted signal)',
      r['status'] == 'warn', r['evidence'])
pp_only = ('<script type="application/ld+json">'
           '{"@type":"NewsMediaOrganization",'
           '"publishingPrinciples":"https://x.example/principles"}</script>')
r = dc.check_g7c_editorial_policy(pp_only)
check('publishingPrinciples schema alone passes',
      r['status'] == 'pass' and 'publishingPrinciples' in r['evidence'],
      r['evidence'])

# ---------------------------------------------------------------- tiers
print('[tiers] evidence tiers for the new ids')
check("A9b_mobile_content_parity is 'measured'",
      scoring.evidence_tier_for('A9b_mobile_content_parity') == 'measured')
check("A9 (viewport, LLM-classified parent) stays 'llm-judged'",
      scoring.evidence_tier_for('A9_viewport_meta') == 'llm-judged')
for cid in ('G1_author_byline', 'G2_author_schema_credentials',
            'G7b_about_contact_discoverability', 'G7c_editorial_policy_link'):
    check(f"{cid.split('_')[0]} is 'measured'",
          scoring.evidence_tier_for(cid) == 'measured', cid)
check("G7 (privacy/terms parent) stays 'llm-judged'",
      scoring.evidence_tier_for('G7_privacy_terms') == 'llm-judged')
check("G9 stays 'llm-judged'",
      scoring.evidence_tier_for('G9_content_freshness_recency') == 'llm-judged')

# ---------------------------------------------------------------- wiring
# run_all_checks wiring — offline via the content-checks-skipped path (an
# HTTP 500 fetch is simulated by monkeypatching fetch; no check function
# executes, so no network is touched).
print('[wiring] run_all_checks includes the E-E-A-T checks')
_orig_fetch = dc.fetch
dc.fetch = lambda url, **kw: ('<html>server error</html>', url, 500, {}, [])
try:
    out = dc.run_all_checks('https://offline.invalid/')
finally:
    dc.fetch = _orig_fetch
check('G1/G2/G7b/G7c wired into the orchestrator',
      {'G1_author_byline', 'G2_author_schema_credentials',
       'G7b_about_contact_discoverability', 'G7c_editorial_policy_link'}
      <= set(out.get('checks', {})), str(sorted(out.get('checks', {}))))
check("wired checks carry evidence_tier='measured'",
      all(out['checks'][cid].get('evidence_tier') == 'measured'
          for cid in ('G1_author_byline', 'G7b_about_contact_discoverability')),
      str(out['checks'].get('G1_author_byline')))

# ---------------------------------------------------------------- weights
print('[weights] PCR_WEIGHTS unchanged')
check('PCR_WEIGHTS sums to exactly 1.0',
      abs(sum(scoring.PCR_WEIGHTS.values()) - 1.0) < 1e-9,
      str(sum(scoring.PCR_WEIGHTS.values())))
check('no new weight categories (9 sections, I excluded)',
      set(scoring.PCR_WEIGHTS) == {
          'A_technical', 'B_performance', 'C_onpage', 'D_schema',
          'E_aeo_discovery', 'F_aeo_extraction', 'G_aeo_trust',
          'H_aeo_selection', 'J_entity'},
      str(sorted(scoring.PCR_WEIGHTS)))

# ---------------------------------------------------------------- result
if FAILURES:
    print(f'\n{len(FAILURES)} failure(s): {FAILURES}')
    sys.exit(1)
print('\nMOBILE_EEAT_OK')
