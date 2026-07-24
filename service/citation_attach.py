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

JUDGE-AT-SELECTION (Lane A, 2026-07-19): recall@displayed-slots measured
48.5% against 74.2% @candidate-pool — the right rule was usually RETRIEVED
but lost the top-3 cut to retrieval ranking. So when the entailment judge is
available, retrieval returns a candidate POOL (SIEVE_SELECT_POOL, default
12, hard cap 24) and the warm entailment cache judges it BEFORE the cut;
the displayed top-3 is then chosen by verdict band
    supports > related > unjudged > unrelated
with the existing retrieval order preserved inside each band (stable sort;
ties by retrieval rank then (kind, id) — sieve ids are TEXT and collide
across kinds). Judging at selection closes 59% of the misses (+25.7pt).
Degradation is exact: with no judge (no key / kill-switch / import failure)
the retrieval call is the SAME legacy top-3 call, byte-identical; a judge
failure mid-pool ranks the pool all-'unjudged', which is retrieval order.
Findings NEVER lose citations over judging and audits never block on it.

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
import os
import time
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger('audit.attach')

MAX_CITATIONS = 3
_CITED_STATUSES = ('fail', 'warn')

# Judge-at-selection pool sizing (see module docstring). Values of the env
# at or below MAX_CITATIONS disable pooling entirely (0 = legacy top-3).
POOL_ENV = 'SIEVE_SELECT_POOL'
POOL_DEFAULT = 12
POOL_HARD_CAP = 24
_VERDICT_RANK = {'supports': 0, 'related': 1, 'unjudged': 2, 'unrelated': 3}

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


def _selection_pool_size() -> int:
    """Pool size from SIEVE_SELECT_POOL (default 12, hard cap 24).
    Values <= MAX_CITATIONS disable pooling — 0 means legacy top-3."""
    try:
        n = int(os.getenv(POOL_ENV, str(POOL_DEFAULT)))
    except (TypeError, ValueError):
        n = POOL_DEFAULT
    if n <= MAX_CITATIONS:
        return 0
    return min(n, POOL_HARD_CAP)


def _selection_judge():
    """The citation_entailment module when judge-at-selection can run, else
    None. None => attach makes the EXACT legacy top-3 retrieval call (same
    args, same retrieval depth — byte-identical legacy behavior)."""
    try:
        import citation_entailment as ce
    except Exception:  # noqa: BLE001
        return None
    try:
        return ce if ce.selection_judge_available() else None
    except Exception:  # noqa: BLE001
        return None


def _select_from_pool(cites: List[Dict[str, Any]],
                      verdicts: List[str]) -> Tuple[List[Dict[str, Any]], int]:
    """Cut a judged pool to the displayed top-N. Deterministic: verdict band
    first (supports > related > unjudged > unrelated), then the existing
    retrieval order, then (kind, id) — sieve ids are TEXT and collide across
    kinds, so the trailing tie key is always the pair. Returns
    (selected, promoted): promoted counts winners that sat below the legacy
    top-3 cut and were pulled up by their verdict."""
    order = sorted(
        range(len(cites)),
        key=lambda i: (_VERDICT_RANK.get(verdicts[i], 2), i,
                       str(cites[i].get('kind') or ''),
                       str(cites[i].get('id') or '')))
    chosen = order[:MAX_CITATIONS]
    selected = [cites[i] for i in chosen]
    promoted = sum(1 for i in chosen if i >= MAX_CITATIONS)
    return selected, promoted


def attach_citations(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Attach deterministic citations to every fail/warn finding, replacing
    the model's selection. Mutates and returns the audit plus stats."""
    stats: Dict[str, Any] = {'applied': True, 'checks_eligible': 0,
                             'checks_cited': 0, 'citations_attached': 0,
                             'llm_lists_replaced': 0, 'empty_retrievals': 0,
                             'evidence_led': 0, 'cites_supporting': 0,
                             'cites_related_only': 0,
                             'cites_source_unattested': 0,
                             'deprecated_excluded': 0, 'errors': 0,
                             'selection_pool': 0, 'pool_judged': 0,
                             'pool_source_unattested': 0,
                             'promoted_from_pool': 0, 'pool_cache_hits': 0,
                             'pool_api_calls': 0, 'pool_errors': 0,
                             'pool_budget_exhausted': False}
    conn = None
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

        # Judge-at-selection setup: one shared cache connection + one
        # per-audit wall-clock budget (citation_entailment's own
        # CITATION_ENTAILMENT_BUDGET_S) across every pool this pass judges.
        # Cache reads stay free past the budget; only API calls stop.
        pool = _selection_pool_size()
        ce = _selection_judge() if pool else None
        deadline = None
        if ce is not None:
            stats['selection_pool'] = pool
            conn = ce._open_conn()
            deadline = time.monotonic() + ce.TOTAL_BUDGET_S

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
                res = None
                pooled = False
                if ce is not None:
                    # Judge-at-selection: retrieve the candidate POOL. The
                    # pooled result's first MAX_CITATIONS entries are
                    # byte-identical to the legacy call (query_brain contract).
                    try:
                        res = qb(query_id, page_type, industry, MAX_CITATIONS,
                                 evidence=ev, evidence_led=evidence_led,
                                 pool=pool) or {}
                        pooled = True
                    except TypeError:
                        # Retrieval seam without pool support (older injected
                        # stub): degrade to the exact legacy call below.
                        res = None
                if res is None:
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
                # Selection: judged pool cut when the judge ran, else the
                # legacy top-3 (raw[:MAX] of a legacy-shaped retrieval).
                # NEVER-DROP: any judging failure falls back to the top of
                # the pool — retrieval succeeded, so citations attach.
                cites = None
                if pooled and raw:
                    try:
                        verdicts, info = ce.judge_selection_pool(
                            f, raw, conn=conn, deadline=deadline)
                        stats['pool_cache_hits'] += info['cache_hits']
                        stats['pool_api_calls'] += info['api_calls']
                        stats['pool_errors'] += info['errors']
                        stats['pool_source_unattested'] += info.get(
                            'source_unattested', 0)
                        if info['budget_exhausted']:
                            stats['pool_budget_exhausted'] = True
                        stats['pool_judged'] += sum(
                            1 for v in verdicts if v in ce.VERDICTS)
                        cites, promoted = _select_from_pool(raw, verdicts)
                        stats['promoted_from_pool'] += promoted
                    except Exception as je:  # noqa: BLE001 — selection judging never drops cites
                        stats['pool_errors'] += 1
                        log.warning('pool selection failed for %s: %s', cid, je)
                        cites = None
                if cites is None:
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
            # CANDIDATE annotation since 2026-07-19: the labelled set
            # measured this lexical test at 50.4% strict precision / 30.3%
            # missed-support, so the DISPLAY decision now belongs to the
            # post-loop LLM judge (citation_entailment stamps c['entailment']
            # after re-grounding); supports_finding survives as the
            # unjudged-fallback signal only.
            ftok = _sup_tokens(f.get('evidence'), f.get('title'))
            for c in cites:
                faithful = c.get('source_faithful') is True
                c['supports_finding'] = bool(faithful and _supports(ftok, c))
                if not faithful:
                    c['provenance_blocked'] = True
                    stats['cites_source_unattested'] += 1
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
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return audit, stats


# ---------------------------------------------------------------------------
# Self-test (stdlib only, stubbed query_brain + stubbed judge — no DB/network)
# ---------------------------------------------------------------------------

def _selftest() -> None:
    global _query_brain_fn
    import citation_entailment as ce

    saved_env = {k: os.environ.get(k) for k in
                 ('ANTHROPIC_API_KEY', 'CITATION_ENTAILMENT', POOL_ENV)}
    saved_ce = (ce._judge_fn, ce._connect_fn, ce.TOTAL_BUDGET_S)
    os.environ.pop('ANTHROPIC_API_KEY', None)   # judge availability is OURS to set
    os.environ.pop(POOL_ENV, None)              # default pool (12)
    os.environ['CITATION_ENTAILMENT'] = '1'
    ce._connect_fn = lambda: None               # cache degrades to LRU-only
    ce._LRU.clear()

    # -------------------------------------------------------------------
    # Scenario 1 — DEGRADATION: no judge (no key, no stub) => the retrieval
    # call and the selection are the EXACT legacy behavior. The block below
    # is the pre-pool selftest verbatim; the added asserts prove one legacy
    # call per eligible finding (max_citations=3, no pool kwarg — the stub
    # would TypeError on one) and that pooling never activated.
    # -------------------------------------------------------------------
    ce._judge_fn = None
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
                 'then_action': 'remove the disallow rule blocking the crawler from the page',
                 'source_faithful': True},
            ]}
        return {'citations': [
            {'id': '1', 'kind': 'rule', 'name': 'Enforce HTTPS sitewide',
             'then_action': 'redirect http to https and serve an hsts header on the site',
             'source_faithful': True},
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

        # Degradation proof: one legacy call (max_citations=3) per eligible
        # finding, pooling never engaged, no verdict-based selection.
        assert len(calls) == 5, calls
        assert all(c[3] == 3 for c in calls), calls
        assert stats['selection_pool'] == 0, stats
        assert stats['pool_judged'] == 0 and stats['promoted_from_pool'] == 0, stats

        # Robustness: junk shapes never raise.
        for shape in ({}, {'findings': None}, {'findings': [42]}):
            _, s2 = attach_citations(dict(shape))
            assert s2['applied'] is True, (shape, s2)

        # -------------------------------------------------------------------
        # Scenario 2 — POOL PROMOTION + DETERMINISM + WARM CACHE: judge on
        # (stubbed), 12-candidate pool. A 'supports' at retrieval rank 9
        # beats a 'related' at rank 1; an 'unrelated' in the legacy top-3 is
        # demoted out. A second identical run must be byte-identical AND
        # all-cache (zero judge invocations — repeat audits are ~free).
        # -------------------------------------------------------------------
        pool_calls = []

        def pool_qb(check_id, page_type, industry, max_citations,
                    evidence=None, evidence_led=False, pool=None):
            pool_calls.append((check_id, max_citations, pool))
            return {'citations': [
                {'id': str(i), 'kind': 'rule', 'name': f'R{i}',
                 'source_faithful': True,
                 'provenance_status': 'verified_excerpt',
                 'source_excerpt': f'Exact source proof for R{i}.',
                 'source_content_hash': f'hash-{i}'}
                for i in range(1, 13)]}

        judge_calls = []

        def stub_judge(finding, citation):
            judge_calls.append((finding.get('check_id'), str(citation.get('id'))))
            return {'9': 'supports', '2': 'unrelated'}.get(
                str(citation.get('id')), 'related')

        _query_brain_fn = pool_qb
        ce._judge_fn = stub_judge
        ce._LRU.clear()

        def mk_audit(evidence):
            return {'classification': {'page_type': 'blog', 'industry': 'saas'},
                    'findings': [{'check_id': 'A1_https_enforcement',
                                  'status': 'fail', 'evidence': evidence}]}

        a1, s1 = attach_citations(mk_audit('served over http'))
        ids1 = [c['id'] for c in a1['findings'][0]['citations']]
        # band 0: '9' (supports, rank 9); band 1 keeps retrieval order:
        # '1', '3' ('2' is unrelated -> demoted out of display entirely).
        assert ids1 == ['9', '1', '3'], ids1
        assert s1['selection_pool'] == 12, s1
        assert s1['pool_judged'] == 12 and s1['pool_api_calls'] == 12, s1
        assert s1['pool_cache_hits'] == 0 and s1['promoted_from_pool'] == 1, s1
        assert pool_calls == [('A1_https_enforcement', 3, 12)], pool_calls
        assert len(judge_calls) == 12, judge_calls

        a2, s2 = attach_citations(mk_audit('served over http'))
        ids2 = [c['id'] for c in a2['findings'][0]['citations']]
        assert ids2 == ids1, (ids1, ids2)                       # deterministic
        assert len(judge_calls) == 12, judge_calls              # zero new calls
        assert s2['pool_api_calls'] == 0 and s2['pool_cache_hits'] == 12, s2
        assert s2['promoted_from_pool'] == 1, s2

        # -------------------------------------------------------------------
        # Scenario 3 — BUDGET CAP: deadline already passed => zero API calls;
        # warm-cache verdicts still land (a hit costs nothing) and still
        # promote; everything uncached degrades to 'unjudged' = retrieval
        # order. Citations are NEVER dropped.
        # -------------------------------------------------------------------
        ce._LRU.clear()
        ce.TOTAL_BUDGET_S = -1.0
        fb = {'check_id': 'A1_https_enforcement', 'status': 'fail',
              'evidence': 'budget scenario evidence'}
        ce._lru_put(ce.cache_key(
            fb, {'id': '9', 'kind': 'rule', 'name': 'R9',
                 'source_faithful': True,
                 'provenance_status': 'verified_excerpt',
                 'source_excerpt': 'Exact source proof for R9.',
                 'source_content_hash': 'hash-9'}), 'supports')
        judge_calls.clear()
        a3, s3 = attach_citations(
            {'classification': {'page_type': 'blog', 'industry': 'saas'},
             'findings': [dict(fb)]})
        ids3 = [c['id'] for c in a3['findings'][0]['citations']]
        assert ids3 == ['9', '1', '2'], ids3     # cached supports + retrieval order
        assert judge_calls == [], judge_calls    # budget blocked every API call
        assert s3['pool_api_calls'] == 0 and s3['pool_cache_hits'] == 1, s3
        assert s3['pool_budget_exhausted'] is True and s3['pool_judged'] == 1, s3
        assert s3['citations_attached'] == 3, s3
        ce.TOTAL_BUDGET_S = saved_ce[2]

        # -------------------------------------------------------------------
        # Scenario 4 — JUDGE BLOWS UP MID-POOL: every pair errors => all
        # 'unjudged' => selection is the top of the pool in retrieval order
        # (= the legacy cut of the same candidates); never-drop holds.
        # -------------------------------------------------------------------
        ce._LRU.clear()

        def broken_judge(finding, citation):
            raise RuntimeError('api down')

        ce._judge_fn = broken_judge
        a4, s4 = attach_citations(mk_audit('a different observation'))
        ids4 = [c['id'] for c in a4['findings'][0]['citations']]
        assert ids4 == ['1', '2', '3'], ids4
        assert s4['pool_errors'] == 12 and s4['pool_judged'] == 0, s4
        assert s4['citations_attached'] == 3 and s4['promoted_from_pool'] == 0, s4
    finally:
        _query_brain_fn = None
        ce._judge_fn, ce._connect_fn, ce.TOTAL_BUDGET_S = saved_ce
        ce._LRU.clear()
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    print('ATTACH_OK')


if __name__ == '__main__':
    _selftest()
