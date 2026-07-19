"""Corpus-wiring offline tests — playbooks retrievable + rendered, canon-org
tier hygiene (live/snapshot parity via org-tiers.json), snapshot freshness
fields carried WHEN PRESENT, and status-filter parity with the live path.
Runs against the REAL committed snapshots plus small fixtures. No DB/network.

Prints CORPUS_WIRING_OK on success (run_tests.sh contract)."""

import inspect
import os
import sys

_ROOT = os.path.join(os.path.dirname(__file__), '..')
_RULESET = os.path.join(_ROOT, 'service', 'ruleset')
sys.path.insert(0, _RULESET)
sys.path.insert(0, os.path.join(_ROOT, 'service'))

import ranker  # noqa: E402
import sieve_brain  # noqa: E402

BRAIN = ranker.BrainIndex.from_export_dir(_RULESET)


def _fixture_brain(**overrides):
    base = dict(rules_by_id={}, aps_by_id={}, playbooks_by_id={},
                principles_by_id={}, check_to_rules={},
                snapshot_date='2026-05-03')
    base.update(overrides)
    return ranker.BrainIndex(**base)


def test_playbooks_reachable_via_bm25():
    # The 1,213-row playbook kind was loaded but unreachable (dead kind).
    # An evidence-flavored query must now surface playbooks from the REAL
    # corpus, mapped onto the uniform citation shape.
    hits = BRAIN.search('audit existing content for generative engine '
                        'optimization readiness AI citation rate',
                        max_citations=8)
    pb = next((h for h in hits if h['kind'] == 'playbook'), None)
    assert pb is not None, [h['kind'] for h in hits]
    row = BRAIN.playbooks_by_id[pb['id']]
    # use_when -> the condition, summary -> the action (uniform shape).
    assert pb['if_condition'] == (row.get('use_when') or '')[:500]
    assert pb['then_action'] == (row.get('summary') or '')[:500]
    assert pb['guidance_kind'] == 'apply'
    assert pb['from'] == 'snapshot' and pb['retrieval_layer'] == 'bm25'
    # kinds filter narrows to playbooks only.
    only = BRAIN.search('generative engine optimization content audit',
                        max_citations=5, kinds=('playbook',))
    assert only and all(h['kind'] == 'playbook' for h in only)
    # format_citation labels the kind.
    assert 'Playbook' in ranker.format_citation(pb)


def test_playbook_render_and_grounding_aliases():
    # Report renderer: the kind badge exists (no more 'Item' fallback).
    src = open(os.path.join(_ROOT, 'service', 'main.py')).read()
    assert "playbook:'Playbook'" in src
    # Grounding path: the alias resolves and the kind config points at the
    # snapshot attr + use_when/summary text fields.
    import citation_grounding as cg
    assert cg._KIND_ALIASES.get('playbooks') == 'playbook'
    cfg = cg._KIND_CFG['playbook']
    assert cfg['snapshot_attr'] == 'playbooks_by_id'
    assert cfg['t1'] == 'use_when' and cfg['t2'] == 'summary'
    # Live path: the FTS-only arm exists (no embeddings on playbooks) and
    # skips the documents join (a playbook's source_url is its own).
    live = sieve_brain._TABLE_CFG['playbooks']
    assert live.get('fts_only') and live.get('no_doc_join')
    head = sieve_brain._select_head(live, '1.0', {'status'})
    assert 'LEFT JOIN sieve.documents' not in head


def test_canon_org_tiering_fixes_drift():
    # Name-drift rows used to sink to tier 5 on the snapshot path because the
    # raw string missed the tier tables. canon_org runs BEFORE the lookup now.
    for raw, want in (('Google Search Central', 1), ('developers.google.com', 1),
                      ('support.google.com', 1), ('moz', 2), ('ahrefs', 2),
                      ('searchengineland.com', 3)):
        assert ranker.get_tier_rank(raw) == want, (raw, ranker.get_tier_rank(raw))
    # The DELIBERATE tier-4 practitioner band (growth-domain operators), by
    # canonical name AND by drifted variant.
    for raw in ('Y Combinator', 'ycombinator.com', 'Reforge', 'reforge.com',
                'a16z', 'andreessen horowitz', 'First Round Review',
                'forentrepreneurs.com', 'Demand Curve', 'animalz.co',
                'appsflyer.com', 'ALM Corp', 'amsive.com', 'CXL', 'frase.io'):
        assert ranker.get_tier_rank(raw) == 4, (raw, ranker.get_tier_rank(raw))
    # Practitioner content ranks above anonymous, below vendor docs.
    assert ranker.get_tier_rank('Some Random Blog') == 5
    assert ranker.get_tier_rank(None) == 5
    # LIVE/SNAPSHOT PARITY: both paths load the same org-tiers.json, so the
    # two tier tables cannot drift; spot-check the live path agrees.
    assert ranker._SHARED_TIERS and ranker._SHARED_TIERS == sieve_brain._TIER_MAP
    assert sieve_brain.canon_org('Google Search Central') == 'Google'
    for raw in ('reforge.com', 'Y Combinator', 'cxl.com'):
        assert sieve_brain.tier_of(raw) == 4, raw


def test_snapshot_citation_carries_last_verified_when_present():
    # Post re-export rows carry lifecycle fields; the ranker surfaces them
    # instead of hardcoding None. Absent (the committed 2026-04-21 files)
    # -> honest None, never fabricated.
    fresh = {'id': 9001, 'name': 'Canonical tags on paginated archives',
             'if_condition': 'archive pages exist', 'then_action': 'set canonicals',
             'source_org': 'Google', 'confidence_score': '0.9',
             'last_verified': '2026-07-01T00:00:00', 'status': 'active',
             'created_at': '2026-05-10T12:00:00'}
    cite = ranker._snapshot_cite('rule', fresh, 3.2, '2026-05-03')
    assert cite['last_verified'] == '2026-07-01'
    assert cite['status'] == 'active'
    assert cite['added'] == '2026-05-10'
    stale = ranker._snapshot_cite('rule', {'id': 1, 'name': 'x'}, 3.0, '2026-05-03')
    assert stale['last_verified'] is None and stale['status'] is None
    # Curated-mapping path carries them too.
    brain = _fixture_brain(rules_by_id={9001: fresh},
                           check_to_rules={'X1_fixture': {'rules': [9001]}})
    cites = ranker.select_citations(brain, 'X1_fixture')
    assert cites and cites[0]['last_verified'] == '2026-07-01'
    # And the real committed corpus (pre-lifecycle export) stays honest-None.
    real = BRAIN.search('title tag length', max_citations=1)
    assert real and real[0]['last_verified'] is None


def test_status_filter_parity_with_live():
    # Same lifecycle set on both paths: every status the snapshot gate
    # excludes appears in the live SQL filter, and both drop superseded rows.
    live_sql = sieve_brain._trust_filter({'status', 'superseded_by'})
    for s in sorted(ranker.EXCLUDED_STATUSES):
        assert s in live_sql, s
    assert 't.superseded_by IS NULL' in live_sql
    assert ranker.status_excluded({'status': 'deprecated'})
    assert ranker.status_excluded({'status': 'Rejected'})
    assert ranker.status_excluded({'status': 'active', 'superseded_by': 123})
    assert not ranker.status_excluded({'status': 'active'})
    assert not ranker.status_excluded({'status': None})
    assert not ranker.status_excluded({})           # pre-lifecycle snapshots
    # contested is NOT excluded (dropping it discards one authoritative side
    # of every conflict — same policy as the live filter).
    assert not ranker.status_excluded({'status': 'candidate', 'contested': 't'})

    # BM25 gate: a deprecated row is unretrievable; its active twin is found.
    dead = {'id': 1, 'name': 'zzunique wombatfact guidance retired',
            'source_org': 'Google', 'status': 'deprecated',
            'if_condition': 'zzunique wombatfact observed', 'then_action': 'x'}
    alive = {'id': 2, 'name': 'zzunique wombatfact guidance current',
             'source_org': 'Google', 'status': 'active',
             'if_condition': 'zzunique wombatfact observed', 'then_action': 'x'}
    brain = _fixture_brain(rules_by_id={1: dead, 2: alive})
    got = brain.search('zzunique wombatfact', max_citations=5, min_score=0.0)
    assert [c['id'] for c in got] == [2], got
    # Curated-mapping gate: the deprecated id resolves to nothing…
    brain2 = _fixture_brain(rules_by_id={1: dead, 2: alive},
                            check_to_rules={'X2_fixture': {'rules': [1, 2]}})
    assert [c['id'] for c in ranker.select_citations(brain2, 'X2_fixture')] == [2]
    # …but stays in the by-id map so grounding can say 'deprecated', not 'missing'.
    assert brain2.rules_by_id.get(1) is not None


def test_norm_gate_default_includes_practitioner_tier():
    # retrieve_batch defaults to the tier-4 practitioner band now that tier 4
    # is meaningful (the old default of 3 excluded 62% of the rule corpus)…
    sig = inspect.signature(sieve_brain.retrieve_batch)
    assert sig.parameters['min_tier'].default == 4
    # …and the endpoint model matches, with the change documented.
    src = open(os.path.join(_ROOT, 'service', 'main.py')).read()
    assert 'min_tier: int = Field(4, ge=1, le=5)' in src
    assert 'PRACTITIONER band' in src
    assert 'never be a norm' in (sieve_brain.retrieve_batch.__doc__ or '')

    # The gate is enforced in CODE, not prose. curated_tier admits only orgs
    # resolved through org-tiers.json / the fallback sets — tier_of's
    # dotted-domain display heuristic must not open the practitioner band to
    # every dotted org in the corpus.
    assert sieve_brain.tier_of('somerandomblog.com') == 4        # display
    assert sieve_brain.curated_tier('somerandomblog.com') == 5   # gate
    assert sieve_brain.curated_tier('reforge.com') == 4
    assert sieve_brain.curated_tier('Google Search Central') == 1
    assert sieve_brain.curated_tier(None) == 5
    cites = [
        {'id': 1, 'source_org_raw': 'Google', 'tier': 1},
        {'id': 2, 'source_org_raw': 'Reforge', 'tier': 4},          # curated 4: in
        {'id': 3, 'source_org_raw': 'somerandomblog.com', 'tier': 4},  # heuristic 4: OUT
        {'id': 4, 'source_org_raw': None, 'tier': 5},                  # unattributed: OUT
    ]
    assert [c['id'] for c in sieve_brain._norm_gate(cites, 4)] == [1, 2]
    # Tier 5 exclusion is a clamp, not documentation: an explicit min_tier=5
    # request behaves exactly like 4 — anonymous knowledge can never be a norm.
    assert sieve_brain._norm_gate(cites, 5) == sieve_brain._norm_gate(cites, 4)
    assert [c['id'] for c in sieve_brain._norm_gate(cites, 3)] == [1]


if __name__ == '__main__':
    test_playbooks_reachable_via_bm25()
    test_playbook_render_and_grounding_aliases()
    test_canon_org_tiering_fixes_drift()
    test_snapshot_citation_carries_last_verified_when_present()
    test_status_filter_parity_with_live()
    test_norm_gate_default_includes_practitioner_tier()
    print('CORPUS_WIRING_OK')
