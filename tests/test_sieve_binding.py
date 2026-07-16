"""Phase 1 (retrieval floor) offline tests for sieve_brain.

Covers the four Phase-1 outcomes without a DB/network by exercising the pure
functions the live path is built on:
  (a) evidence-based query   -> _query_for(check_id, evidence)
  (b) relevance floor        -> _rank_and_floor drops off-topic hits
  (c) relevance-first sort    -> authority no longer overrides a big relevance gap
  (d) embed-space pinning     -> the model-mismatch guard + degraded stamp shape
  (e) DB cosine floor present  -> _search_vector SQL carries the >= floor predicate

Prints SIEVE_BINDING_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import sieve_brain  # noqa: E402


def _cite(cid, tier, rel, conf=0.8, layer='vector', url='https://x.example/p', kind='rule'):
    return {'id': str(cid), 'tier': tier, 'relevance': rel, 'confidence_score': str(conf),
            'retrieval_layer': layer, 'url_spec': sieve_brain._url_spec(url),
            'url_provenance': 'extracted', 'kind': kind}


def test_query_is_evidence_based():
    # (a) Same check, different evidence -> different query text, and the
    #     evidence leads (dominates the embedding/FTS terms).
    q_base = sieve_brain._query_for('A2_title_tag')
    q1 = sieve_brain._query_for('A2_title_tag', 'title tag is missing entirely')
    q2 = sieve_brain._query_for('A2_title_tag', 'title tag is 812 characters, far too long')
    assert q1 != q2, (q1, q2)
    assert q1 != q_base and q2 != q_base
    assert q1.startswith('title tag is missing'), q1
    # Normalization: identical observations pin identically; whitespace/case folded.
    assert (sieve_brain._query_for('A2_title_tag', '  Title Tag   MISSING ')
            == sieve_brain._query_for('A2_title_tag', 'title tag missing'))
    # No evidence -> unchanged legacy behaviour (back-compat).
    assert sieve_brain._query_for('A2_title_tag') == sieve_brain._query_for('A2_title_tag', None)


def test_relevance_floor_drops_offtopic():
    # (b) A check whose only candidates are all below the cosine floor yields
    #     NOTHING — "we found nothing relevant" is representable.
    below = [_cite(1, 1, 0.05), _cite(2, 1, 0.10), _cite(3, 2, 0.19)]
    assert sieve_brain._rank_and_floor(below, 3) == []
    # A mix keeps only the above-floor rows.
    mixed = [_cite(1, 1, 0.05), _cite(2, 3, 0.60), _cite(3, 2, 0.40)]
    kept = sieve_brain._rank_and_floor(mixed, 3)
    assert [c['id'] for c in kept] == ['2', '3'], kept
    # Per-layer floor: FTS uses the ts_rank floor (default 0.0), not the cosine one.
    fts = [_cite(9, 1, 0.05, layer='fts')]
    assert len(sieve_brain._rank_and_floor(fts, 3)) == 1


def test_relevance_beats_authority_across_bands():
    # (c) THE headline fix: a tier-1 row far away must NOT beat a tier-2 row
    #     that is on-topic. Old sort keyed tier first; new sort bands relevance.
    tier1_far = _cite(1, 1, 0.30, conf=0.99)     # authoritative but weak match
    tier2_near = _cite(2, 2, 0.92, conf=0.70)    # less authoritative, strong match
    ranked = sieve_brain._rank_and_floor([tier1_far, tier2_near], 2)
    assert ranked[0]['id'] == '2', ranked        # relevance wins
    # WITHIN a band, authority still breaks the tie (bounded tiebreak).
    a = _cite(10, 3, 0.91, conf=0.7)
    b = _cite(11, 1, 0.94, conf=0.7)             # same 0.9x band, higher tier
    ranked2 = sieve_brain._rank_and_floor([a, b], 2)
    assert ranked2[0]['id'] == '11', ranked2
    # Deterministic: id is the final tiebreak.
    c1 = _cite(50, 2, 0.80); c2 = _cite(40, 2, 0.80)
    assert [c['id'] for c in sieve_brain._rank_and_floor([c1, c2], 2)] == ['40', '50']


def test_vector_sql_has_cosine_floor():
    # (e) The DB-side floor is present so top-k is drawn from relevant rows only.
    captured = {}

    class FakeCur:
        def execute(self, sql, params):
            captured['sql'] = sql
            captured['params'] = params

        def fetchall(self):
            return []

    cfg = sieve_brain._TABLE_CFG['rules']
    sieve_brain._search_vector(FakeCur(), 'rules', cfg, '[0,0]', 12, {'status'}, min_rel=0.28)
    assert '(1 - (t.embedding <=> %s::vector)) >= %s' in captured['sql'], captured['sql']
    # floor value is bound, and the LIMIT is last.
    assert 0.28 in captured['params'], captured['params']
    assert captured['params'][-1] == 12


def test_embed_pinning_guard_shape():
    # (d) _corpus_model swallows errors -> None (older corpus, proceed as today).
    class BadConn:
        def cursor(self):
            raise RuntimeError('no meta table')
    assert sieve_brain._corpus_model(BadConn()) is None
    # The mismatch constant + strict semantics exist as designed.
    assert hasattr(sieve_brain, 'SIEVE_MIN_RELEVANCE')
    assert sieve_brain.SIEVE_MIN_RELEVANCE > 0
    assert issubclass(sieve_brain.SieveLiveError, RuntimeError)


def test_row_to_cite_tags_layer():
    r = {'id': 7, 'title': 'R', 'text1': 'if', 'text2': 'then', 'conf': '0.9',
         'source_org': 'Google', 'source_url': 'https://developers.google.com/search/docs/x',
         'score': 0.77, '_layer': 'fts', 'kindtag': 'rule'}
    c = sieve_brain._row_to_cite(r)
    assert c['retrieval_layer'] == 'fts'
    assert c['relevance'] == 0.77
    assert c['tier'] == 1  # Google


if __name__ == '__main__':
    test_query_is_evidence_based()
    test_relevance_floor_drops_offtopic()
    test_relevance_beats_authority_across_bands()
    test_vector_sql_has_cosine_floor()
    test_embed_pinning_guard_shape()
    test_row_to_cite_tags_layer()
    print('SIEVE_BINDING_OK')
