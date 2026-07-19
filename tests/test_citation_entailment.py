"""Citation entailment — the DISPLAY decision for citations moves from the
lexical supports_finding gate (50.4% strict precision on the 2026-07-19
labelled set) to a cached LLM judgment stamped as c['entailment'].

Offline by construction: a stub judge is injected through the module seam
(_judge_fn) and a fake DB connection through _connect_fn — no API, no
Postgres. Covers: cache-key stability + DDL, LRU + DB cache behavior,
fail-safe paths (no key / raising judge / exhausted budget), agent.py wiring
order, main.py render branches, and the compact-payload field.

Prints ENTAILMENT_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import citation_entailment as ce  # noqa: E402

_SERVICE = os.path.join(os.path.dirname(__file__), '..', 'service')


def _reset():
    ce._LRU.clear()
    ce._judge_fn = None
    ce._connect_fn = lambda: None  # never touch a real DB in tests
    ce._table_ready = False
    ce.TOTAL_BUDGET_S = 30.0


def _finding(check='A1_https_enforcement', evidence='no HSTS header on https responses',
             status='fail', cites=None):
    return {'check_id': check, 'status': status, 'evidence': evidence,
            'citations': cites if cites is not None else []}


def _cite(kind='rule', cid=7, name='Use HTTP Observatory security headers audit',
          **kw):
    c = {'kind': kind, 'id': cid, 'name': name,
         'if_condition': 'missing security headers such as HSTS',
         'then_action': 'add the missing headers before launch',
         'source_org': 'MDN', 'supports_finding': False}
    c.update(kw)
    return c


# ---------------------------------------------------------------------------
# 1. cache key: stable, base-keyed, evidence-sensitive; DDL targets the
#    auditor's own table
# ---------------------------------------------------------------------------

def test_cache_key_stability_and_ddl():
    _reset()
    f, c = _finding(), _cite()
    k1 = ce.cache_key(f, c)
    k2 = ce.cache_key(dict(f), dict(c))
    assert k1 == k2 and len(k1) == 64, (k1, k2)          # deterministic sha256
    # Aliased/renamed ids canonicalize to the same check BASE -> same key
    k_alias = ce.cache_key(_finding(check='A1_https'), c)
    assert k_alias == k1, 'check base (A1) must drive the key, not the full id'
    # Different evidence on the same check -> re-judged
    assert ce.cache_key(_finding(evidence='served over http'), c) != k1
    # Different rule -> different key
    assert ce.cache_key(f, _cite(cid=8)) != k1
    # kind participates (rule #7 != ap #7)
    assert ce.cache_key(f, _cite(kind='ap')) != k1
    # The JUDGED text participates: an in-place brain edit to the rule's
    # if/then re-judges — the TTL-less DB cache must not serve the verdict
    # computed over the old text forever.
    assert ce.cache_key(f, _cite(if_condition='rewritten condition')) != k1
    assert ce.cache_key(f, _cite(then_action='rewritten action')) != k1
    # Model + prompt version participate: a judge/prompt revision invalidates
    # (the acceptance benchmark reads the LIVE judge, not the old one's cache).
    saved_model, saved_pv = ce.MODEL, ce.PROMPT_VERSION
    try:
        ce.MODEL = 'claude-other-model'
        assert ce.cache_key(f, c) != k1
        ce.MODEL = saved_model
        ce.PROMPT_VERSION = saved_pv + '-test'  # always differs from current
        assert ce.cache_key(f, c) != k1
    finally:
        ce.MODEL, ce.PROMPT_VERSION = saved_model, saved_pv
    assert ce.cache_key(f, c) == k1                      # restored -> stable
    # Benchmark escape hatch: judge_pair(use_cache=False) skips cache reads.
    import inspect
    assert 'use_cache' in inspect.signature(ce.judge_pair).parameters
    bench = open(os.path.join(_SERVICE, 'scripts', 'entailment_benchmark.py'),
                 encoding='utf-8').read()
    assert '--no-cache' in bench and 'use_cache=not args.no_cache' in bench
    # DDL auto-creates the auditor's OWN cache table (check_query_embeddings pattern)
    assert 'CREATE TABLE IF NOT EXISTS public.citation_entailment_cache' in ce._TABLE_DDL
    assert 'cache_key' in ce._TABLE_DDL and 'PRIMARY KEY' in ce._TABLE_DDL


# ---------------------------------------------------------------------------
# 2. verdict parsing: strict JSON first, word-boundary fallback
# ---------------------------------------------------------------------------

def test_parse_verdict():
    assert ce._parse_verdict('{"verdict":"supports"}') == 'supports'
    assert ce._parse_verdict('noise {"verdict": "unrelated"} noise') == 'unrelated'
    assert ce._parse_verdict('unrelated') == 'unrelated'   # NOT 'related' (substring trap)
    assert ce._parse_verdict('related') == 'related'
    assert ce._parse_verdict('supports or maybe related') is None  # ambiguous
    assert ce._parse_verdict('no verdict here') is None


# ---------------------------------------------------------------------------
# 3. stub judge: stamping, stats, LRU dedupe (one call per unique tuple)
# ---------------------------------------------------------------------------

def test_stub_judge_stamps_and_caches():
    _reset()
    calls = []

    def stub(finding, citation):
        calls.append((finding.get('check_id'), citation.get('id')))
        return {7: 'supports', 8: 'related', 9: 'unrelated'}[citation['id']]

    ce._judge_fn = stub
    shared = _cite(cid=7)  # same rule attached to two findings w/ same evidence
    findings = [
        _finding(cites=[shared, _cite(cid=8), _cite(cid=9)]),
        _finding(cites=[dict(shared)]),                     # LRU hit, no 2nd call
        _finding(check='A1_https', cites=[dict(shared)]),   # same BASE -> hit too
        _finding(status='pass', cites=[_cite(cid=8)]),      # pass -> not judged
        'junk', {'status': 'fail'},                         # junk shapes tolerated
    ]
    stats = ce.judge_citations(findings)
    assert stats['applied'] is True, stats
    assert findings[0]['citations'][0]['entailment'] == 'supports'
    assert findings[0]['citations'][1]['entailment'] == 'related'
    assert findings[0]['citations'][2]['entailment'] == 'unrelated'
    assert findings[1]['citations'][0]['entailment'] == 'supports'
    assert findings[2]['citations'][0]['entailment'] == 'supports'
    assert 'entailment' not in findings[3]['citations'][0], 'pass findings untouched'
    assert len(calls) == 3, calls                     # unique tuples only
    assert stats['api_calls'] == 3 and stats['cache_hits'] == 2, stats
    assert stats['judged'] == 5 and stats['citations_seen'] == 5, stats
    assert (stats['supports'], stats['related'], stats['unrelated']) == (3, 1, 1), stats
    # judge_pair reports cached=True on a repeat
    r = ce.judge_pair(_finding(), _cite(cid=7))
    assert r == {'verdict': 'supports', 'cached': True}, r


# ---------------------------------------------------------------------------
# 4. DB cache: DDL executed, verdict written once, read back on a cold LRU
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, store, executed):
        self.store, self.executed, self._row = store, executed, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append(sql.strip().split()[0].upper() + ':' + sql[:60])
        if sql.strip().startswith('INSERT'):
            self.store.setdefault(params[0], params[1])
        elif sql.strip().startswith('SELECT'):
            key = params[0]
            self._row = (self.store[key],) if key in self.store else None

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self):
        self.store, self.executed, self.closed = {}, [], False

    def cursor(self):
        return _FakeCursor(self.store, self.executed)

    def close(self):
        self.closed = True


def test_db_cache_roundtrip():
    _reset()
    conn = _FakeConn()
    ce._connect_fn = lambda: conn
    calls = []
    ce._judge_fn = lambda f, c: calls.append(1) or 'related'

    findings = [_finding(cites=[_cite(cid=42)])]
    stats = ce.judge_citations(findings)
    assert stats['api_calls'] == 1 and len(conn.store) == 1, (stats, conn.store)
    assert any(e.startswith('CREATE') for e in conn.executed), 'DDL not auto-created'
    assert conn.closed is True, 'batch connection must be closed'

    # Cold LRU (new process) -> served from the DB, no API call
    ce._LRU.clear()
    conn2 = _FakeConn()
    conn2.store = conn.store
    ce._connect_fn = lambda: conn2
    ce._judge_fn = lambda f, c: (_ for _ in ()).throw(AssertionError('must not call API'))
    findings2 = [_finding(cites=[_cite(cid=42)])]
    stats2 = ce.judge_citations(findings2)
    assert stats2['cache_hits'] == 1 and stats2['api_calls'] == 0, stats2
    assert findings2[0]['citations'][0]['entailment'] == 'related'


# ---------------------------------------------------------------------------
# 5. fail-safe paths: no key / raising judge / exhausted budget -> 'unjudged',
#    audit never blocked, everything counted
# ---------------------------------------------------------------------------

def test_failsafe_no_api_key():
    _reset()
    saved = os.environ.pop('ANTHROPIC_API_KEY', None)
    try:
        findings = [_finding(cites=[_cite(cid=1), _cite(cid=2)])]
        stats = ce.judge_citations(findings)
        assert stats['applied'] is True and stats['no_api_key'] is True, stats
        assert stats['unjudged'] == 2 and stats['judged'] == 0, stats
        assert all(c['entailment'] == 'unjudged' for c in findings[0]['citations'])
    finally:
        if saved is not None:
            os.environ['ANTHROPIC_API_KEY'] = saved


def test_failsafe_judge_raises():
    _reset()

    def flaky(finding, citation):
        if citation['id'] == 2:
            raise TimeoutError('6s per-call timeout')
        return 'supports'

    ce._judge_fn = flaky
    findings = [_finding(cites=[_cite(cid=1), _cite(cid=2), _cite(cid=3)])]
    stats = ce.judge_citations(findings)
    assert stats['applied'] is True, stats
    ents = [c['entailment'] for c in findings[0]['citations']]
    assert ents == ['supports', 'unjudged', 'supports'], ents
    assert stats['errors'] == 1 and stats['unjudged'] == 1 and stats['judged'] == 2, stats


def test_failsafe_budget_early_stop_still_serves_cache():
    _reset()
    ce.TOTAL_BUDGET_S = -1.0  # budget exhausted before the first call
    ce._judge_fn = lambda f, c: (_ for _ in ()).throw(AssertionError('no API past budget'))
    seeded_f, seeded_c = _finding(), _cite(cid=5)
    ce._lru_put(ce.cache_key(seeded_f, seeded_c), 'supports')
    findings = [_finding(cites=[_cite(cid=5), _cite(cid=6)])]
    stats = ce.judge_citations(findings)
    assert stats['budget_exhausted'] is True, stats
    ents = [c['entailment'] for c in findings[0]['citations']]
    assert ents == ['supports', 'unjudged'], ents        # cache hits stay free
    assert stats['cache_hits'] == 1 and stats['api_calls'] == 0, stats


# ---------------------------------------------------------------------------
# 6. wiring: agent.py runs the judge AFTER re-grounding (final verbatim text),
#    before binding verification, gated by CITATION_ENTAILMENT (default ON)
# ---------------------------------------------------------------------------

def test_agent_wiring_order_and_gate():
    src = open(os.path.join(_SERVICE, 'agent.py'), encoding='utf-8').read()
    i_reground = src.index('CITATION RE-GROUNDING')
    i_entail = src.index('CITATION ENTAILMENT')
    i_binding = src.index('BINDING VERIFICATION')
    assert i_reground < i_entail < i_binding, \
        'entailment must run after re-grounding and before binding verification'
    assert "os.getenv('CITATION_ENTAILMENT', '1')" in src, 'env gate must default ON'
    assert 'from citation_entailment import judge_citations' in src
    assert '"citation_entailment"' in src or "'citation_entailment'" in src, \
        'stats must land in metadata.citation_entailment'


# ---------------------------------------------------------------------------
# 7. render branches (main.py): supports=proof, related=see-also,
#    unrelated=hidden-not-dropped, unjudged=lexical fallback; compact carries
#    the verdict
# ---------------------------------------------------------------------------

def test_render_branches_and_compact_field():
    import main
    src = open(os.path.join(_SERVICE, 'main.py'), encoding='utf-8').read()
    # unrelated: hidden from display (skipped BEFORE dedup), kept in JSON
    assert "if (c.entailment === 'unrelated') continue;" in src
    # related: collapsed see-also section, rendered after the proof tiers
    assert "if (c.entailment === 'related') { seeAlso.push(c); continue; }" in src
    assert 'See also — related guidance (' in src
    # judged supports overrides the lexical demotion + fallback label
    assert "x.entailment === 'supports'" in src
    assert "c.supports_finding === false && c.entailment !== 'supports'" in src

    # compact payload carries entailment per citation
    slim = main._slim_citation(_cite(cid=3, entailment='supports'))
    assert slim['entailment'] == 'supports' and slim['supportsFinding'] is False, slim
    audit = {'audit_id': 'a', 'domain': 'd', 'scoring': {},
             'findings': [_finding(cites=[_cite(cid=3, entailment='related',
                                                source_url='https://ex/hsts')])]}
    compact = main._audit_to_compact(audit)
    assert compact['issues'][0]['citations'][0]['entailment'] == 'related', compact
    # absent judgment stays None -> consumers get the unjudged fallback signal
    audit2 = {'audit_id': 'a', 'domain': 'd', 'scoring': {},
              'findings': [_finding(cites=[_cite(cid=4)])]}
    assert main._audit_to_compact(audit2)['issues'][0]['citations'][0]['entailment'] is None


# ---------------------------------------------------------------------------
# 8. candidate annotation preserved: the lexical gate still stamps
#    supports_finding (citation_attach), entailment never overwrites it
# ---------------------------------------------------------------------------

def test_lexical_gate_stays_candidate_annotation():
    _reset()
    ce._judge_fn = lambda f, c: 'supports'
    findings = [_finding(cites=[_cite(cid=11, supports_finding=False)])]
    ce.judge_citations(findings)
    c = findings[0]['citations'][0]
    assert c['supports_finding'] is False and c['entailment'] == 'supports', c


if __name__ == '__main__':
    test_cache_key_stability_and_ddl()
    test_parse_verdict()
    test_stub_judge_stamps_and_caches()
    test_db_cache_roundtrip()
    test_failsafe_no_api_key()
    test_failsafe_judge_raises()
    test_failsafe_budget_early_stop_still_serves_cache()
    test_agent_wiring_order_and_gate()
    test_render_branches_and_compact_field()
    test_lexical_gate_stays_candidate_annotation()
    _reset()
    print('ENTAILMENT_OK')
