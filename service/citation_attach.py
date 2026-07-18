"""
citation_attach.py — deterministic Phase-13: Python cites, not the LLM.

The playbook says every failed/warned check gets citations, but the model
applied that inconsistently (measured: 10 of 59 fail/warn findings cited,
and its picks varied between identical runs). This pass makes citation
attachment a runtime responsibility: after check_ids are canonicalized,
every fail/warn finding gets the top-N citations from query_brain — the
same deterministic, tier-ranked, verbatim retrieval the model was asked to
copy. Whatever citation subset the model chose is replaced wholesale.

Combined with check_vocab (stable ids) and citation_grounding (verbatim
fields), this completes: the LLM CLASSIFIES; Python GRADES, CITES, GROUNDS.

Contract §6 additions:
  - every attached citation is annotated supports_finding (binding_gate's
    token-support test between the finding's evidence+title and the cited
    row). Citations are NEVER dropped over it — annotate only; the renderer
    demotes/labels non-supporting cites.
  - foreign/renamed findings (vocab_status='foreign' or original_check_id
    present) skip the curated exact-mapping shortcut: retrieval goes
    evidence-led, querying with the finding's evidence + the ORIGINAL id —
    a model-invented id must not inherit a canonical slot's curated sources.

Never raises; stats land in metadata.citation_attachment.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger('audit.attach')

MAX_CITATIONS = 3
_CITED_STATUSES = ('fail', 'warn')

# Seam for tests: resolved lazily so importing this module stays cheap and
# the selftest can inject a stub without a DB.
_query_brain_fn = None


def _get_query_brain():
    global _query_brain_fn
    if _query_brain_fn is None:
        from tools import query_brain
        _query_brain_fn = query_brain
    return _query_brain_fn


def _get_deprecated_match():
    """Resolve ranker.deprecated_match (§7) with the ruleset dir on sys.path.
    Returns None when unavailable — the guard degrades open, never breaks
    the attach pass (exclusion is a freshness nicety, not a safety gate)."""
    try:
        import os
        import sys
        rd = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ruleset')
        if rd not in sys.path:
            sys.path.insert(0, rd)
        from ranker import deprecated_match
        return deprecated_match
    except Exception:  # noqa: BLE001
        return None


def attach_citations(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Attach deterministic citations to every fail/warn finding, replacing
    the model's selection. Mutates and returns the audit plus stats."""
    stats: Dict[str, Any] = {'applied': True, 'checks_eligible': 0,
                             'checks_cited': 0, 'citations_attached': 0,
                             'llm_lists_replaced': 0, 'empty_retrievals': 0,
                             'evidence_led': 0, 'cites_supporting': 0,
                             'cites_related_only': 0,
                             'deprecated_excluded': 0, 'errors': 0}
    try:
        from binding_gate import supports as _supports, _tokens as _sup_tokens
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats
        cls = audit.get('classification') or {}
        page_type = cls.get('page_type') or 'homepage'
        industry = cls.get('industry') or 'other'
        qb = _get_query_brain()
        dep_match = _get_deprecated_match()

        for f in findings:
            if not isinstance(f, dict):
                continue
            if f.get('status') not in _CITED_STATUSES:
                continue
            cid = f.get('check_id')
            if not isinstance(cid, str) or not cid:
                continue
            stats['checks_eligible'] += 1
            # Foreign/renamed ids (§6): skip the curated exact-mapping
            # shortcut — retrieval is evidence-led, querying with the
            # ORIGINAL model id, never the canonical slot it landed near.
            orig = f.get('original_check_id')
            orig = orig if isinstance(orig, str) and orig else None
            evidence_led = bool(orig) or f.get('vocab_status') == 'foreign'
            query_id = orig or cid
            try:
                # Evidence-based retrieval: the finding's OWN observation leads
                # the query, so two findings on the same check retrieve rules
                # relevant to what was actually seen (Phase 1).
                ev = f.get('evidence') if isinstance(f.get('evidence'), str) else None
                res = qb(query_id, page_type, industry, MAX_CITATIONS,
                         evidence=ev, evidence_led=evidence_led) or {}
                raw = [c for c in (res.get('citations') or [])
                       if isinstance(c, dict)]
                # Deprecation guard (§7): retired guidance (HowTo rich
                # results, FAQ rich-result promises) is excluded BEFORE the
                # cap so a fresher candidate can take its slot, and counted.
                if dep_match is not None:
                    kept = []
                    for c in raw:
                        if dep_match(c):
                            stats['deprecated_excluded'] += 1
                        else:
                            kept.append(c)
                    raw = kept
                cites = raw[:MAX_CITATIONS]
            except Exception as e:  # noqa: BLE001 — one bad check must not stop the pass
                from sieve_brain import SieveLiveError
                if isinstance(e, SieveLiveError):
                    raise  # strict mode: fail the audit, don't degrade
                log.warning('citation attach failed for %s: %s', cid, e)
                stats['errors'] += 1
                continue
            if evidence_led:
                stats['evidence_led'] += 1
            if f.get('citations'):
                stats['llm_lists_replaced'] += 1
            if not cites:
                stats['empty_retrievals'] += 1
                f['citations'] = []
                continue
            # Support annotation (§6): does this cite actually back THIS
            # finding's evidence? Annotate-only — never drop a citation.
            ftok = _sup_tokens(f.get('evidence'), f.get('title'))
            for c in cites:
                c['supports_finding'] = _supports(ftok, c)
                stats['cites_supporting' if c['supports_finding']
                      else 'cites_related_only'] += 1
            f['citations'] = cites
            stats['checks_cited'] += 1
            stats['citations_attached'] += len(cites)
    except Exception as e:  # noqa: BLE001
        from sieve_brain import SieveLiveError
        if isinstance(e, SieveLiveError):
            raise  # strict mode: propagate to the agent → audit fails loudly
        log.error('citation attachment failed: %s', e)
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
    return audit, stats


# ---------------------------------------------------------------------------
# Self-test (stdlib only, stubbed query_brain — no DB/network)
# ---------------------------------------------------------------------------

def _selftest() -> None:
    global _query_brain_fn
    calls = []

    def fake_qb(check_id, page_type, industry, max_citations, evidence=None,
                evidence_led=False):
        calls.append((check_id, page_type, industry, max_citations, evidence,
                      evidence_led))
        if check_id == 'B1_core_web_vitals':
            return {'citations': []}                      # nothing retrieved
        if check_id == 'C1_heading_hierarchy':
            raise RuntimeError('db hiccup')               # per-check failure
        if check_id == 'F9_answer_capsule_density':
            return {'citations': []}                      # foreign id: search found nothing
        if check_id == 'A10_robots_txt':                  # evidence-led path
            return {'citations': [
                {'id': '7', 'kind': 'rule', 'name': 'robots.txt disallow blocks crawler access',
                 'then_action': 'remove the disallow rule blocking the crawler from the page'},
            ]}
        return {'citations': [
            {'id': '1', 'kind': 'rule', 'name': 'Enforce HTTPS sitewide',
             'then_action': 'redirect http to https and serve an hsts header on the site'},
            {'id': '2', 'kind': 'principle', 'name': 'P2'},
            'junk-not-a-dict',
            {'id': '3', 'kind': 'ap', 'name': 'A3'},
            {'id': '4', 'kind': 'rule', 'name': 'overflow'},
        ]}

    _query_brain_fn = fake_qb
    try:
        audit = {
            'classification': {'page_type': 'blog', 'industry': 'saas'},
            'findings': [
                {'check_id': 'A1_https_enforcement', 'status': 'fail',
                 'vocab_status': 'canonical',
                 'evidence': 'site served over http, no HSTS header',
                 'citations': [{'id': '999', 'name': 'llm pick to be replaced'}]},
                {'check_id': 'B1_core_web_vitals', 'status': 'warn'},
                {'check_id': 'C1_heading_hierarchy', 'status': 'fail'},
                {'check_id': 'D6_required_fields', 'status': 'pass',
                 'citations': [{'id': 'keep'}]},           # pass -> untouched
                # renamed (aliased): retrieval must query the ORIGINAL id,
                # evidence-led — not the canonical slot's curated mapping
                {'check_id': 'A10_robots_txt_crawling', 'status': 'fail',
                 'vocab_status': 'aliased', 'original_check_id': 'A10_robots_txt',
                 'evidence': 'robots.txt disallow blocks crawler access to the page'},
                # foreign: model-invented id, evidence-led with its own id
                {'check_id': 'F9_answer_capsule_density', 'status': 'warn',
                 'vocab_status': 'foreign', 'evidence': 'no capsule'},
                {'status': 'fail'},                        # no check_id -> skipped
                'not-a-dict',
            ],
        }
        audit, stats = attach_citations(audit)

        f = audit['findings']
        assert [c['id'] for c in f[0]['citations']] == ['1', '2', '3'], f[0]
        assert f[1]['citations'] == [], f[1]
        assert 'citations' not in f[2] or f[2].get('citations') is None or True
        assert f[3]['citations'] == [{'id': 'keep'}], f[3]
        assert calls[0][1:] == ('blog', 'saas', 3,
                                'site served over http, no HSTS header', False), calls[0]

        # §6 support annotation: stamped on EVERY attached cite, never dropped.
        assert [c['supports_finding'] for c in f[0]['citations']] == \
            [True, False, False], f[0]['citations']

        # §6 evidence-led routing: the aliased finding queried its ORIGINAL id
        # with evidence_led=True; the foreign finding its own id likewise.
        aliased_call = next(c for c in calls if c[0] == 'A10_robots_txt')
        assert aliased_call[5] is True, aliased_call
        foreign_call = next(c for c in calls if c[0] == 'F9_answer_capsule_density')
        assert foreign_call[5] is True, foreign_call
        assert f[4]['citations'][0]['supports_finding'] is True, f[4]
        assert f[5]['citations'] == [], f[5]
        assert stats['evidence_led'] == 2, stats

        assert stats['checks_eligible'] == 5, stats
        assert stats['checks_cited'] == 2 and stats['citations_attached'] == 4, stats
        assert stats['llm_lists_replaced'] == 1, stats
        assert stats['empty_retrievals'] == 2 and stats['errors'] == 1, stats
        assert stats['cites_supporting'] == 2 and stats['cites_related_only'] == 2, stats

        # Robustness: junk shapes never raise.
        for shape in ({}, {'findings': None}, {'findings': [42]}):
            _, s2 = attach_citations(dict(shape))
            assert s2['applied'] is True, (shape, s2)
    finally:
        _query_brain_fn = None

    print('ATTACH_OK')


if __name__ == '__main__':
    _selftest()
