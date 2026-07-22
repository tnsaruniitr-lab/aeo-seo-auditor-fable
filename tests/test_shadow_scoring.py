#!/usr/bin/env python3
"""SHADOW dual-score (evidence-weighted PCR) — stdlib-only, no DB/network.

Quality invariants proven here:
  - CLASSIC BYTE-IDENTITY: the classic scoring dict for a mixed-tier fixture
    is byte-identical (canonical JSON) to the golden captured on the
    pre-shadow code — the shadow is purely additive, never a re-score
  - shadow math: same section/weight math as PCR but counting ONLY
    evidence-backed findings — an attached observed block whose method is a
    real observation; sections with zero evidence-backed findings are
    excluded + renormalized exactly like the classic empty-section rule
  - FORGERY CLOSED: neither a model-stamped evidence_tier nor a
    measured-family check_id makes a finding evidence-backed without a
    runtime observed block (the tier and the id are both model-authored on
    the agent path; the observed block is runtime-owned)
  - coverage honesty: findings_counted/findings_total cover only PCR-scored
    sections — I (GEO) findings feed BAP, not PCR, and are never claimed
  - null paths: no evidence-backed section => shadow null WITH a reason;
    transport-inconclusive classic => shadow suppressed
  - clamp_shadow backstop: forged shadow values neutralized (clamp/enum,
    coverage int-guarded) on BOTH copies — scoring.shadow via validate_audit
    AND the metadata.scoring_shadow mirror at the compact API
  - surfaces: _audit_to_compact carries nullable shadowScore (scoring.shadow
    first, metadata.scoring_shadow fallback for reloaded audits); INDEX_HTML
    renders the modest line only when shadow is non-null

Run from the service dir:
    cd service && python3 ../tests/test_shadow_scoring.py
Prints SHADOW_SCORING_OK on success, exits non-zero on failure.
"""

import copy
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))

from scoring import finalize_scoring, validate_audit  # noqa: E402

_FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
with open(os.path.join(_FIX, 'shadow_fixture.json')) as fh:
    FIXTURE = json.load(fh)
with open(os.path.join(_FIX, 'shadow_classic_golden.json')) as fh:
    GOLDEN = json.load(fh)


# ---------------------------------------------------------------------------
# 1) CLASSIC BYTE-IDENTITY — shadow means shadow (regression gate)
# ---------------------------------------------------------------------------
sc = finalize_scoring(copy.deepcopy(FIXTURE))['scoring']
# cite_readiness (Phase 2) is additive like the shadow — excluded from the
# classic byte-identity golden the same way.
classic = {k: v for k, v in sc.items()
           if k not in ('shadow', 'shadow_reason',
                        'cite_readiness', 'cite_readiness_reason')}
assert json.dumps(classic, sort_keys=True) == json.dumps(GOLDEN, sort_keys=True), \
    'classic scoring dict changed — the shadow must be purely additive'

# determinism: same input -> same shadow too
sc2 = finalize_scoring(copy.deepcopy(FIXTURE))['scoring']
assert sc == sc2, 'non-deterministic recompute with shadow'


# ---------------------------------------------------------------------------
# 2) Shadow math on the mixed-tier fixture
#    evidence-backed (observed attached by a runtime producer):
#    A1 (measured-on-page, fail), B1 (measured-on-page, pass),
#    H1 (observed-off-page, warn).
#    NOT backed: E9 (model-stamped 'measured' tier but NO observed block —
#    the forgeable stamp is ignored), A6/C1/J1 (llm, no observed),
#    F3 (observed method=model-judgment), I2 (GEO — feeds BAP, not PCR),
#    D9 (status na -> not applicable).
#    Shadow sections: A=0, B=100, H=50 over weights .16/.10/.08
#    => 14/0.34 = 41.2; classic = 58.6 (golden) => delta -17.4
# ---------------------------------------------------------------------------
sh = sc['shadow']
assert sh is not None and 'shadow_reason' not in sc, sc
assert sh['pcr_evidence'] == 41.2, sh
assert sh['grade_evidence'] == 'F', sh
assert sh['delta_vs_classic'] == -17.4, sh
assert sh['coverage'] == {'findings_counted': 3, 'findings_total': 8,
                          'sections_with_data': 3}, sh


# ---------------------------------------------------------------------------
# 2b) FORGERY CLOSED — neither a model-stamped tier nor a measured-family
#     check_id counts without a runtime-attached observed block
# ---------------------------------------------------------------------------
forgery = finalize_scoring({
    'bots_eye_view': {'classification': 'fully_accessible'},
    'findings': [
        # forged explicit stamp on an LLM-judged check, no observed block
        {'check_id': 'F3_capsule', 'section': 'F', 'status': 'pass',
         'evidence_tier': 'measured'},
        # measured-family id, but the scripts never measured it (no observed)
        {'check_id': 'A1_https_enforcement', 'section': 'A', 'status': 'pass'},
        {'check_id': 'C1_heading', 'section': 'C', 'status': 'fail'},
    ],
})['scoring']
assert forgery['shadow'] is None, (
    'a model-forgeable tier/check_id must not make the shadow evidence-backed',
    forgery['shadow'])
assert forgery['shadow_reason'] == \
    'no evidence-backed findings in any scored section', forgery


# ---------------------------------------------------------------------------
# 3) Renormalization — a single evidence-backed section IS the shadow score
# ---------------------------------------------------------------------------
one = finalize_scoring({
    'bots_eye_view': {'classification': 'fully_accessible'},
    'findings': [
        {'check_id': 'B1_ttfb', 'section': 'B', 'status': 'warn',
         'observed': {'method': 'measured-on-page'}},                # backed
        {'check_id': 'C1_heading', 'section': 'C', 'status': 'pass'},  # llm
        {'check_id': 'F3_capsule', 'section': 'F', 'status': 'fail'},  # llm
    ],
})['scoring']
assert one['shadow']['pcr_evidence'] == 50.0, one['shadow']
assert one['shadow']['coverage'] == {'findings_counted': 1, 'findings_total': 3,
                                     'sections_with_data': 1}, one['shadow']
# classic is untouched by the exclusion: (50*.10 + 100*.13 + 0*.13)/0.36
assert one['page_citation_readiness'] == round((50 * .10 + 100 * .13) / .36, 1), one


# ---------------------------------------------------------------------------
# 3b) 'observed-competitor' (H-family competitor-crawl detail) is a REAL
#     observation — the shadow counts it like any other observed method
# ---------------------------------------------------------------------------
comp = finalize_scoring({
    'bots_eye_view': {'classification': 'fully_accessible'},
    'findings': [
        {'check_id': 'H2_comparison_table', 'section': 'H', 'status': 'pass',
         'observed': {'method': 'observed-competitor',
                      'detail': {'competitors_crawled': 5}}},
        {'check_id': 'C1_heading', 'section': 'C', 'status': 'fail'},  # llm
    ],
})['scoring']
assert comp['shadow']['pcr_evidence'] == 100.0, comp['shadow']
assert comp['shadow']['coverage'] == {'findings_counted': 1, 'findings_total': 2,
                                      'sections_with_data': 1}, comp['shadow']


# ---------------------------------------------------------------------------
# 4) Null paths — no evidence at all, and transport-inconclusive
# ---------------------------------------------------------------------------
none = finalize_scoring({
    'bots_eye_view': {'classification': 'fully_accessible'},
    'findings': [
        {'check_id': 'C1_heading', 'section': 'C', 'status': 'pass'},
        {'check_id': 'F3_capsule', 'section': 'F', 'status': 'fail',
         'observed': {'method': 'model-judgment'}},
    ],
})['scoring']
assert none['shadow'] is None, none
assert none['shadow_reason'] == 'no evidence-backed findings in any scored section', none
assert none['page_citation_readiness'] is not None, 'classic must still score'

gated = finalize_scoring({
    'bots_eye_view': {'classification': 'bot_blocked'},
    'findings': [{'check_id': 'A1_https_enforcement', 'section': 'A',
                  'status': 'fail'}],
})['scoring']
assert gated['inconclusive'] is True and gated['shadow'] is None, gated
assert 'shadow_reason' in gated, gated


# ---------------------------------------------------------------------------
# 5) validate_audit backstop — forged shadow neutralized, honest one survives
# ---------------------------------------------------------------------------
forged = validate_audit({
    'scoring': {'overall_score': 70, 'overall_grade': 'C',
                'shadow': {'pcr_evidence': '99"><script>', 'grade_evidence': 'Z++',
                           'delta_vs_classic': 'junk',
                           'coverage': {'findings_counted': 1}}},
    'findings': [],
})['scoring']['shadow']
assert isinstance(forged['pcr_evidence'], float), forged
assert forged['grade_evidence'] in ('A+', 'A', 'B+', 'B', 'C+', 'C', 'D+', 'D',
                                    'F', 'INCONCLUSIVE'), forged
assert forged['delta_vs_classic'] is None, forged
assert forged['coverage'] == {'findings_counted': 1}, forged

# coverage clamp: non-int/negative/unknown keys dropped, floats floored,
# negatives clamped to 0; an all-junk coverage collapses to None
covjunk = validate_audit({
    'scoring': {'overall_score': 70, 'overall_grade': 'C',
                'shadow': {'pcr_evidence': 50.0, 'grade_evidence': 'D',
                           'delta_vs_classic': 0.0,
                           'coverage': {'findings_counted': 'x',
                                        'findings_total': -3,
                                        'sections_with_data': 2.9,
                                        'evil': 1}}},
    'findings': [],
})['scoring']['shadow']
assert covjunk['coverage'] == {'findings_total': 0, 'sections_with_data': 2}, covjunk
alljunk = validate_audit({
    'scoring': {'overall_score': 70, 'overall_grade': 'C',
                'shadow': {'pcr_evidence': 50.0, 'grade_evidence': 'D',
                           'delta_vs_classic': 0.0, 'coverage': 'evil'}},
    'findings': [],
})['scoring']['shadow']
assert alljunk['coverage'] is None, alljunk

# non-dict shadow -> None; legitimate negative delta survives the clamp
junk = validate_audit({'scoring': {'overall_score': 70, 'overall_grade': 'C',
                                   'shadow': 'evil'}, 'findings': []})
assert junk['scoring']['shadow'] is None, junk['scoring']
honest = validate_audit({'scoring': {'overall_score': 70, 'overall_grade': 'C',
                                     'shadow': {'pcr_evidence': 61.2,
                                                'grade_evidence': 'C',
                                                'delta_vs_classic': -8.8}},
                         'findings': []})['scoring']['shadow']
assert honest == {'pcr_evidence': 61.2, 'grade_evidence': 'C',
                  'delta_vs_classic': -8.8}, honest
# idempotent
again = validate_audit({'scoring': {'overall_score': 70, 'overall_grade': 'C',
                                    'shadow': dict(honest)}, 'findings': []})
assert again['scoring']['shadow'] == honest, again['scoring']


# ---------------------------------------------------------------------------
# 6) Surfaces — compact shadowScore (+ metadata fallback) and INDEX_HTML line
# ---------------------------------------------------------------------------
import main  # noqa: E402

base = {'audit_id': 'a1', 'domain': 'd', 'url': 'https://d',
        'findings': [], 'narrative': {}}
c = main._audit_to_compact({**base, 'scoring': {
    'overall_score': 58.6, 'overall_grade': 'D+',
    'shadow': {'pcr_evidence': 41.2, 'grade_evidence': 'F',
               'delta_vs_classic': -17.4,
               'coverage': {'findings_counted': 3, 'findings_total': 8,
                            'sections_with_data': 3}}}})
assert c['shadowScore'] == {'score': 41.2, 'grade': 'F',
                            'coverage': {'findings_counted': 3,
                                         'findings_total': 8,
                                         'sections_with_data': 3}}, c

# reloaded audit: scoring.shadow dropped by fetch_audit -> metadata fallback
c2 = main._audit_to_compact({**base, 'scoring': {'overall_score': 58.6},
                             'metadata': {'scoring_shadow': {
                                 'pcr_evidence': 41.2, 'grade_evidence': 'F',
                                 'coverage': {'findings_counted': 3,
                                              'findings_total': 8,
                                              'sections_with_data': 3}}}})
assert c2['shadowScore'] and c2['shadowScore']['score'] == 41.2, c2

# TAMPERED metadata mirror: the fallback never passed validate_audit, so the
# compact API must clamp it itself — and must clamp a COPY, never rewriting
# the stored metadata in a GET handler
tampered = {'scoring_shadow': {'pcr_evidence': '99"><script>',
                               'grade_evidence': 'Z++',
                               'delta_vs_classic': 'junk',
                               'coverage': {'findings_counted': 'x',
                                            'findings_total': 4}}}
c4 = main._audit_to_compact({**base, 'scoring': {'overall_score': 58.6},
                             'metadata': tampered})
assert c4['shadowScore'] == {'score': 99.0, 'grade': 'A+',
                             'coverage': {'findings_total': 4}}, c4
assert tampered['scoring_shadow']['pcr_evidence'] == '99"><script>', \
    'compact clamp must not mutate the stored metadata mirror'

# no shadow anywhere -> null, key still present
c3 = main._audit_to_compact({**base, 'scoring': {'overall_score': 58.6}})
assert 'shadowScore' in c3 and c3['shadowScore'] is None, c3

html = main.INDEX_HTML
assert 'Evidence-weighted (shadow):' in html, 'shadow hero line missing'
assert 'counts only runtime-observed findings' in html, 'shadow hero caption missing'
assert "if (!sh) return '';" in html, 'shadow line must not render when null'

print('SHADOW_SCORING_OK')
