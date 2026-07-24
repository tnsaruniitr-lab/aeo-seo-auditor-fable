"""
binding_gate.py — Mode B: verify an LLM-authored rule binding.

For LLM-judged findings the model must bind a rule chosen from the candidate
pool it was shown. This gate is the CODE that decides whether that binding is
trustworthy — replacing the old "attach top-3 by check-name string match" and
the >=2-token _plausible acceptance as the scoring-bearing check. Three tests,
cheapest-and-strongest first:

  1. existence  — (kind, id) resolves to a real brain row.
  2. provenance — the row has a verbatim source excerpt tied to a content hash.
  3. candidacy  — the id is a member of the candidate pool retrieval returns
                  for THIS finding (kills prose-scraped / hallucinated ids
                  without threading per-finding state).
  4. support    — the finding's evidence and the rule's then_action/text share
                  enough distinctive vocabulary (topical support).

A finding is NEVER dropped: binding_verified + reason are stamped, and an
unverified binding is simply excluded from scoring weight (Phase 4). Pure +
deterministic; resolver and candidate-pool are injectable for offline tests.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

_STOP = frozenset(
    'the a an and or of to in for on with is are be as at by from this that it its into '
    'over under about not no your you can should must if then when where which who what how'.split())


def _tokens(*texts: Any) -> Set[str]:
    out: Set[str] = set()
    for s in texts:
        if isinstance(s, str):
            out |= {w for w in re.findall(r'[a-z0-9]+', s.lower())
                    if len(w) >= 3 and w not in _STOP}
    return out


def _norm_id(v: Any) -> Optional[str]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, str):
        m = re.fullmatch(r'\s*(\d+)\s*', v)
        return m.group(1) if m else None
    return None


def supports(evidence_tokens: Set[str], row: Dict[str, Any]) -> bool:
    """Topical support between a finding's evidence and a rule row. Conservative
    (refuses on no evidence) — the same bar used to mint a fix source."""
    fetched = _tokens(row.get('name'), row.get('title'), row.get('if_condition'),
                      row.get('then_action'), row.get('statement'),
                      row.get('description'), row.get('explanation'))
    if not evidence_tokens or not fetched:
        return False
    inter = evidence_tokens & fetched
    ratio = len(inter) / min(len(evidence_tokens), len(fetched))
    return len(inter) >= 3 and ratio >= 0.20


def verify_binding(
    finding: Dict[str, Any],
    resolve: Callable[[str, Any], Optional[dict]],
    candidates_for: Callable[[Dict[str, Any]], Set[Tuple[str, str]]],
) -> Dict[str, Any]:
    """Verify finding['bound_rule'] in place; return the bound_rule (or None)."""
    br = finding.get('bound_rule')
    if not isinstance(br, dict):
        return None
    kind, rid = br.get('kind'), _norm_id(br.get('id'))
    if not kind or rid is None:
        br['binding_verified'] = False
        br['reason'] = 'malformed-ref'
        return br

    row = resolve(kind, rid)
    if not row:
        br['binding_verified'] = False
        br['reason'] = 'not-found'
        return br

    from rule_eval import source_faithful
    if not source_faithful(row):
        br['binding_verified'] = False
        br['reason'] = 'source-unattested'
        br['source_faithful'] = False
        return br

    # Mode C skips the LLM-candidacy/topical gates, but never provenance.
    if br.get('basis') == 'deterministic':
        br.update({
            'binding_verified': True, 'reason': 'ok',
            'name': row.get('name') or row.get('title'),
            'source_org': row.get('source_org'),
            'source_url': row.get('source_url'),
            'confidence_score': str(row.get('confidence_score') or ''),
            'source_excerpt': str(row.get('source_excerpt') or '')[:1000],
            'source_content_hash': row.get('source_content_hash'),
            'provenance_status': row.get('provenance_status'),
            'source_faithful': True,
        })
        return br

    pool = candidates_for(finding) or set()
    if (kind, rid) not in pool:
        br['binding_verified'] = False
        br['reason'] = 'not-a-candidate'
        return br

    ev = _tokens(finding.get('evidence'), finding.get('title'))
    if not supports(ev, row):
        br['binding_verified'] = False
        br['reason'] = 'unsupported'
        return br

    # Verified: stamp verbatim fields from the resolved row (never the model's).
    br.update({
        'binding_verified': True, 'reason': 'ok', 'basis': br.get('basis') or 'llm-cited',
        'name': row.get('name') or row.get('title'),
        'source_org': row.get('source_org'),
        'source_url': row.get('source_url'),
        'confidence_score': str(row.get('confidence_score') or ''),
        'source_excerpt': str(row.get('source_excerpt') or '')[:1000],
        'source_content_hash': row.get('source_content_hash'),
        'provenance_status': row.get('provenance_status'),
        'source_faithful': True,
    })
    return br


def verify_bindings(
    audit: Dict[str, Any],
    resolve: Callable[[str, Any], Optional[dict]],
    candidates_for: Callable[[Dict[str, Any]], Set[Tuple[str, str]]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Verify every finding's bound_rule. Mutates and returns (audit, stats).
    Never raises, never drops a finding."""
    stats = {'applied': True, 'bindings': 0, 'verified': 0,
             'not_found': 0, 'source_unattested': 0,
             'not_candidate': 0, 'unsupported': 0}
    try:
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats
        for f in findings:
            if not isinstance(f, dict) or not isinstance(f.get('bound_rule'), dict):
                continue
            stats['bindings'] += 1
            br = verify_binding(f, resolve, candidates_for)
            if not br:
                continue
            if br.get('binding_verified'):
                stats['verified'] += 1
            else:
                reason = br.get('reason')
                key = {'not-found': 'not_found', 'not-a-candidate': 'not_candidate',
                       'source-unattested': 'source_unattested',
                       'unsupported': 'unsupported'}.get(reason)
                if key:
                    stats[key] += 1
    except Exception as e:  # noqa: BLE001
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
    return audit, stats


def _selftest() -> None:
    rows = {
        ('rule', '1496'): {'id': 1496, 'name': 'FAQPage markup must match visible FAQ content',
                           'then_action': 'ensure faqpage schema answers appear visibly on the page',
                           'source_org': 'Google', 'source_url': 'https://g/faq', 'confidence_score': '0.9',
                           'source_excerpt': 'FAQPage content must be visible to users.',
                           'source_content_hash': 'hash-faq',
                           'provenance_status': 'verified_excerpt'},
        ('rule', '1489'): {'id': 1489, 'name': 'Enforce HTTPS across the domain',
                           'then_action': 'redirect http to https everywhere', 'source_org': 'Perplexity',
                           'source_excerpt': 'Redirect HTTP traffic to HTTPS.',
                           'source_content_hash': 'hash-https',
                           'provenance_status': 'verified_excerpt'},
    }
    resolve = lambda k, i: rows.get((k, str(i)))
    # candidate pool: what retrieval would surface for the FAQ finding
    pool = {('rule', '1496'), ('rule', '761')}

    def candidates_for(f):
        return pool

    audit = {'findings': [
        # VERIFIED: real id, in pool, evidence supports the rule
        {'check_id': 'D9_faqpage', 'status': 'fail',
         'evidence': 'faqpage schema present but the faq answers are not visible on the page',
         'bound_rule': {'kind': 'rule', 'id': 1496, 'basis': 'llm-cited'}},
        # NOT-A-CANDIDATE: real id but retrieval never surfaced it (hallucinated pick)
        {'check_id': 'D9_faqpage', 'status': 'fail',
         'evidence': 'faqpage schema present but answers hidden',
         'bound_rule': {'kind': 'rule', 'id': 1489, 'basis': 'llm-cited'}},
        # NOT-FOUND: id does not exist
        {'check_id': 'D9_faqpage', 'status': 'fail', 'evidence': 'x',
         'bound_rule': {'kind': 'rule', 'id': 999999, 'basis': 'llm-cited'}},
        # UNSUPPORTED: in pool + exists, but evidence is off-topic
        {'check_id': 'D9_faqpage', 'status': 'fail',
         'evidence': 'the page loads slowly on mobile networks',
         'bound_rule': {'kind': 'rule', 'id': 1496, 'basis': 'llm-cited'}},
        # DETERMINISTIC: pre-verified, untouched
        {'check_id': 'A1_https', 'status': 'fail',
         'bound_rule': {'kind': 'rule', 'id': 1489, 'basis': 'deterministic',
                        'binding_verified': True}},
    ]}
    audit, stats = verify_bindings(audit, resolve, candidates_for)
    f = audit['findings']
    assert f[0]['bound_rule']['binding_verified'] is True and f[0]['bound_rule']['reason'] == 'ok', f[0]
    assert f[0]['bound_rule']['source_url'] == 'https://g/faq'
    assert f[1]['bound_rule']['binding_verified'] is False and f[1]['bound_rule']['reason'] == 'not-a-candidate', f[1]
    assert f[2]['bound_rule']['binding_verified'] is False and f[2]['bound_rule']['reason'] == 'not-found', f[2]
    assert f[3]['bound_rule']['binding_verified'] is False and f[3]['bound_rule']['reason'] == 'unsupported', f[3]
    assert f[4]['bound_rule']['binding_verified'] is True, 'deterministic binding untouched'
    assert stats == {'applied': True, 'bindings': 5, 'verified': 2,
                     'source_unattested': 0,
                     'not_found': 1, 'not_candidate': 1, 'unsupported': 1}, stats

    unattested = {'check_id': 'D9_faqpage', 'status': 'fail',
                  'evidence': 'faqpage schema but answers hidden',
                  'bound_rule': {'kind': 'rule', 'id': 1496}}
    bare_resolve = lambda k, i: {'id': 1496, 'name': 'FAQPage visible'}
    br = verify_binding(unattested, bare_resolve, candidates_for)
    assert br['binding_verified'] is False and br['reason'] == 'source-unattested', br

    for shape in ({}, {'findings': None}, {'findings': [1]}):
        _, s = verify_bindings(dict(shape), resolve, candidates_for)
        assert s['applied'] is True, (shape, s)

    print('BINDING_GATE_OK')


if __name__ == '__main__':
    _selftest()
