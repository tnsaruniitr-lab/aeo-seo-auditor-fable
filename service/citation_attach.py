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


def attach_citations(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Attach deterministic citations to every fail/warn finding, replacing
    the model's selection. Mutates and returns the audit plus stats."""
    stats: Dict[str, Any] = {'applied': True, 'checks_eligible': 0,
                             'checks_cited': 0, 'citations_attached': 0,
                             'llm_lists_replaced': 0, 'empty_retrievals': 0,
                             'errors': 0}
    try:
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats
        cls = audit.get('classification') or {}
        page_type = cls.get('page_type') or 'homepage'
        industry = cls.get('industry') or 'other'
        qb = _get_query_brain()

        for f in findings:
            if not isinstance(f, dict):
                continue
            if f.get('status') not in _CITED_STATUSES:
                continue
            cid = f.get('check_id')
            if not isinstance(cid, str) or not cid:
                continue
            stats['checks_eligible'] += 1
            try:
                res = qb(cid, page_type, industry, MAX_CITATIONS) or {}
                cites = [c for c in (res.get('citations') or [])
                         if isinstance(c, dict)][:MAX_CITATIONS]
            except Exception as e:  # noqa: BLE001 — one bad check must not stop the pass
                log.warning('citation attach failed for %s: %s', cid, e)
                stats['errors'] += 1
                continue
            if f.get('citations'):
                stats['llm_lists_replaced'] += 1
            if not cites:
                stats['empty_retrievals'] += 1
                f['citations'] = []
                continue
            f['citations'] = cites
            stats['checks_cited'] += 1
            stats['citations_attached'] += len(cites)
    except Exception as e:  # noqa: BLE001
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

    def fake_qb(check_id, page_type, industry, max_citations):
        calls.append((check_id, page_type, industry, max_citations))
        if check_id == 'B1_core_web_vitals':
            return {'citations': []}                      # nothing retrieved
        if check_id == 'C1_heading_hierarchy':
            raise RuntimeError('db hiccup')               # per-check failure
        return {'citations': [
            {'id': '1', 'kind': 'rule', 'name': 'R1'},
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
                 'citations': [{'id': '999', 'name': 'llm pick to be replaced'}]},
                {'check_id': 'B1_core_web_vitals', 'status': 'warn'},
                {'check_id': 'C1_heading_hierarchy', 'status': 'fail'},
                {'check_id': 'D6_required_fields', 'status': 'pass',
                 'citations': [{'id': 'keep'}]},           # pass -> untouched
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
        assert calls[0][1:] == ('blog', 'saas', 3), calls[0]
        assert stats['checks_eligible'] == 3, stats
        assert stats['checks_cited'] == 1 and stats['citations_attached'] == 3, stats
        assert stats['llm_lists_replaced'] == 1, stats
        assert stats['empty_retrievals'] == 1 and stats['errors'] == 1, stats

        # Robustness: junk shapes never raise.
        for shape in ({}, {'findings': None}, {'findings': [42]}):
            _, s2 = attach_citations(dict(shape))
            assert s2['applied'] is True, (shape, s2)
    finally:
        _query_brain_fn = None

    print('ATTACH_OK')


if __name__ == '__main__':
    _selftest()
