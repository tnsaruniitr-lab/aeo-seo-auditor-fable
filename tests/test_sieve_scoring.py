"""Phase 4 offline tests — rule-weighted scoring (mode D).

Proves the safety contract:
  (a) default (LAMBDA=0) is byte-identical to the unweighted formula;
  (b) a VERIFIED binding re-weights WITHIN a section, bounded and symmetric;
  (c) an UNVERIFIED binding never moves the score.

Prints SIEVE_SCORING_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import scoring  # noqa: E402


def _section_score(findings):
    s = scoring.compute_from_findings(findings)
    vals = [v for v in s['section_scores'].values() if v is not None]
    assert len(vals) == 1, s['section_scores']  # all findings are section A
    return vals[0]


BASE = [
    {'check_id': 'A1_https', 'status': 'pass'},
    {'check_id': 'A2_title', 'status': 'fail'},
]


def test_default_is_byte_identical():
    scoring.RULE_WEIGHT_LAMBDA = 0.0
    # unweighted: (1 pass + 0.5*0 warn) / 2 applicable * 100 = 50.0
    assert _section_score(BASE) == 50.0


def test_verified_binding_reweights_bounded_and_symmetric():
    lam = scoring.RULE_WEIGHT_LAMBDA
    scoring.RULE_WEIGHT_LAMBDA = 0.5
    try:
        bound = {'kind': 'rule', 'id': 1, 'confidence_score': '1.0',
                 'binding_verified': True, 'basis': 'deterministic'}
        # bind the FAIL: weight 1.5 -> (1*1 + 1.5*0)/(1+1.5)*100 = 40.0 (lower)
        fail_bound = [BASE[0], {**BASE[1], 'bound_rule': bound}]
        assert _section_score(fail_bound) == 40.0
        # SYMMETRIC: bind the PASS instead -> (1.5*1 + 1*0)/2.5*100 = 60.0 (mirror)
        pass_bound = [{**BASE[0], 'bound_rule': bound}, BASE[1]]
        assert _section_score(pass_bound) == 60.0
        # BOUNDED: even a huge lambda cannot push the weight past 1.5
        scoring.RULE_WEIGHT_LAMBDA = 100.0
        assert scoring._check_weight({'bound_rule': bound}) == scoring._RULE_WEIGHT_MAX
    finally:
        scoring.RULE_WEIGHT_LAMBDA = lam


def test_unverified_binding_does_not_move_score():
    lam = scoring.RULE_WEIGHT_LAMBDA
    scoring.RULE_WEIGHT_LAMBDA = 0.5
    try:
        unverified = {'kind': 'rule', 'id': 1, 'confidence_score': '1.0',
                      'binding_verified': False, 'reason': 'not-a-candidate'}
        findings = [BASE[0], {**BASE[1], 'bound_rule': unverified}]
        assert _section_score(findings) == 50.0   # unchanged
    finally:
        scoring.RULE_WEIGHT_LAMBDA = lam


def test_ap_severity_from_risk():
    assert scoring._binding_severity({'kind': 'ap', 'risk_level': 'high'}) == 1.0
    assert scoring._binding_severity({'kind': 'ap', 'risk_level': 'low'}) == 0.3
    assert scoring._binding_severity({'kind': 'rule', 'confidence_score': '0.8'}) == 0.8
    # a status can never be flipped: weight only scales the mean, never the value
    scoring.RULE_WEIGHT_LAMBDA = 0.5
    try:
        allfail = [{'check_id': 'A1_x', 'status': 'fail',
                    'bound_rule': {'kind': 'rule', 'id': 1, 'confidence_score': '1.0',
                                   'binding_verified': True}}]
        assert _section_score(allfail) == 0.0   # weighting a lone fail is still 0
    finally:
        scoring.RULE_WEIGHT_LAMBDA = 0.0


if __name__ == '__main__':
    test_default_is_byte_identical()
    test_verified_binding_reweights_bounded_and_symmetric()
    test_unverified_binding_does_not_move_score()
    test_ap_severity_from_risk()
    print('SIEVE_SCORING_OK')
