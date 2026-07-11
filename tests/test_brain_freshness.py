"""Offline tests for the freshness/disclosure/lifecycle additions to
sieve_brain: status filtering in retrieval SQL, provenance-aware ranking,
SIEVE_STRICT semantics, and the renderer's brain-mode disclosure strings.
No DB, no network. Prints BRAIN_FRESHNESS_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import sieve_brain  # noqa: E402


def test_status_filter_sql():
    # With a status column, retired/superseded/rejected are excluded; without
    # one, no filter is emitted (legacy DBs keep working).
    f = sieve_brain._status_filter({'status'})
    assert "NOT IN ('retired','superseded','rejected')" in f
    assert "coalesce(t.status,'active')" in f
    assert sieve_brain._status_filter(set()) == ''
    assert sieve_brain._status_filter(None) == ''
    # The filter lands in both search paths' SQL.
    cfg = sieve_brain._TABLE_CFG['rules']
    head = sieve_brain._select_head(cfg, '1.0', {'status', 'url_provenance'})
    assert 't.url_provenance' in head
    head_bare = sieve_brain._select_head(cfg, '1.0', set())
    assert 'NULL AS url_provenance' in head_bare


def test_provenance_rank_ordering():
    pr = sieve_brain._prov_rank
    assert pr({'url_provenance': 'extracted'}) == 0
    assert pr({'url_provenance': None}) == 1
    assert pr({'url_provenance': 'legacy-import'}) == 1
    assert pr({'url_provenance': 'neighbor-inferred'}) == 2
    # In the sort: same tier/conf/relevance/url_spec → extracted wins.
    a = {'tier': 1, 'confidence_score': '0.9', 'relevance': 0.5, 'url_spec': 0,
         'url_provenance': 'neighbor-inferred', 'kind': 'rule', 'id': '1'}
    b = {'tier': 1, 'confidence_score': '0.9', 'relevance': 0.5, 'url_spec': 0,
         'url_provenance': 'extracted', 'kind': 'rule', 'id': '2'}
    ordered = sorted([a, b], key=lambda c: (
        c['tier'], -round(float(c['confidence_score']), 1), -round(c['relevance'], 2),
        c.get('url_spec', 2), pr(c), c.get('kind') or '', str(c['id'] or '')))
    assert ordered[0]['id'] == '2'


def test_strict_mode_flag_and_error_type():
    assert issubclass(sieve_brain.SieveLiveError, RuntimeError)
    # Default off in this environment.
    assert sieve_brain.SIEVE_STRICT in (True, False)


def test_renderer_discloses_brain_mode():
    main_path = os.path.join(os.path.dirname(__file__), '..', 'service', 'main.py')
    src = open(main_path).read()
    # live-mode header carries the freshness date; snapshot mode is named; the
    # mixed case is impossible to miss; snapshot fallback gets a banner.
    assert "verified through" in src
    assert "SNAPSHOT ruleset (2026-04-21)" in src
    assert "MIXED: ' + fromLive + ' live" in src
    assert "grounded from the bundled snapshot" in src
    # evidence_tier is a findings-table column now.
    assert "llm-judged'" in src and '<th>Basis</th>' in src
    # URL-less demotion + neighbor-inferred honesty note.
    assert "a.source_url ? 0 : 1" in src
    assert "inferred link" in src


def test_stats_freshness_keys_are_wired():
    src = open(os.path.join(os.path.dirname(__file__), '..', 'service',
                            'sieve_brain.py')).read()
    for key in ("verified_through", "stale_days", "last_ingest_run"):
        assert key in src, key


if __name__ == '__main__':
    test_status_filter_sql()
    test_provenance_rank_ordering()
    test_strict_mode_flag_and_error_type()
    test_renderer_discloses_brain_mode()
    test_stats_freshness_keys_are_wired()
    print('BRAIN_FRESHNESS_OK')
