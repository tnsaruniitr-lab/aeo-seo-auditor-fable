#!/usr/bin/env python3
"""
entailment_benchmark.py — calibration harness for the citation entailment
judge against the 206-pair labelled set (build-context 2026-07-19).

Runs citation_entailment.judge_pair over every labelled pair and reports the
two acceptance metrics the labelled-set report defined for "evidence-grade":

    strict displayed-proof precision  (judged 'supports' whose gold label is
                                       supports)                >= 95%
    missed-support                    (gold supports the judge did NOT mark
                                       supports)                <= 5%

USAGE (needs a REAL ANTHROPIC_API_KEY — this harness makes live haiku calls;
~206 short judgments on a cold cache, ~$0.05; re-runs are mostly cache hits
when DATABASE_URL points at the auditor DB):

    cd service
    ANTHROPIC_API_KEY=sk-... python3 scripts/entailment_benchmark.py \
        --pairs "/path/to/labelled-pairs-2026-07-19.jsonl" \
        [--audit /path/to/full-audit.json ...] \
        [--limit N] [--json] [--no-cache]

After ANY prompt or model revision, run with --no-cache (and bump
citation_entailment.PROMPT_VERSION with prompt changes): acceptance numbers
must come from the live judge, never from cached verdicts of the old one.

INPUT FIDELITY: the labelled JSONL carries (check, citation-name, org, gold
label) but NOT the finding evidence or the rule's if/then text. The harness
reconstructs the judge's full input:
  - citation if/then/kind/id: joined from the bundled snapshot ruleset
    (service/ruleset) by normalized citation name;
  - finding evidence: joined from --audit file(s) (the audit JSONs whose
    findings produced the pairs) by check_id.
Pairs that cannot be enriched are still judged on the reduced fields and
counted separately (reduced_fidelity) — treat a run with a high
reduced_fidelity count as a lower bound, not the acceptance number.
The gold 'reason' field is NEVER shown to the judge (it states the answer).

Exit code: 0 when both targets met, 1 otherwise, 2 on setup errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

STRICT_PRECISION_TARGET = 95.0
MISSED_SUPPORT_TARGET = 5.0


def _norm_name(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', (s or '').lower()).strip()


def _snapshot_by_name():
    """name-normalized -> full citation dict, from the bundled ruleset."""
    ruleset_dir = os.path.join(_SERVICE, 'ruleset')
    if ruleset_dir not in sys.path:
        sys.path.insert(0, ruleset_dir)
    from ranker import BrainIndex
    brain = BrainIndex.from_export_dir(ruleset_dir)
    out = {}
    for kind, rows in (('rule', brain.rules_by_id),
                       ('principle', brain.principles_by_id),
                       ('ap', brain.aps_by_id)):
        for rid, row in rows.items():
            name = row.get('name') or row.get('title')
            if not name:
                continue
            out.setdefault(_norm_name(name), {
                'kind': kind, 'id': rid, 'name': name,
                'if_condition': row.get('if_condition') or row.get('statement')
                                or row.get('description'),
                'then_action': row.get('then_action') or row.get('then_logic')
                               or row.get('explanation'),
                'source_org': row.get('source_org'),
            })
    return out


def _evidence_by_check(audit_paths):
    """check_id -> finding evidence, from the source audit JSON(s)."""
    out = {}
    for p in audit_paths:
        with open(p, encoding='utf-8') as fh:
            audit = json.load(fh)
        for f in (audit.get('findings') or []):
            if isinstance(f, dict) and f.get('check_id') and f.get('evidence'):
                out.setdefault(f['check_id'], f['evidence'])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n', 2)[1])
    ap.add_argument('--pairs', required=True, help='labelled-pairs JSONL')
    ap.add_argument('--audit', action='append', default=[],
                    help='full audit JSON to join finding evidence from (repeatable)')
    ap.add_argument('--limit', type=int, default=0, help='judge only the first N pairs')
    ap.add_argument('--json', action='store_true', help='emit raw metrics JSON only')
    ap.add_argument('--no-cache', action='store_true',
                    help='bypass the LRU/DB verdict caches so every pair '
                         'exercises the LIVE judge (use after any prompt/model '
                         'change; results are still written back)')
    args = ap.parse_args()

    if not os.getenv('ANTHROPIC_API_KEY'):
        print('ERROR: ANTHROPIC_API_KEY not set — this harness makes live '
              'judge calls and cannot run offline.', file=sys.stderr)
        return 2

    import citation_entailment as ce

    try:
        by_name = _snapshot_by_name()
    except Exception as e:  # noqa: BLE001
        print(f'WARN: snapshot ruleset unavailable ({e}) — all pairs run reduced-fidelity',
              file=sys.stderr)
        by_name = {}
    ev_by_check = _evidence_by_check(args.audit)

    pairs = []
    with open(args.pairs, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                pairs.append(json.loads(line))
    if args.limit:
        pairs = pairs[:args.limit]

    n = len(pairs)
    shown_supports = 0          # judge said supports (displayed as proof)
    shown_correct = 0           # ... and gold agrees
    gold_supports = 0
    gold_supports_missed = 0    # gold supports the judge did NOT mark supports
    reduced_fidelity = 0
    errors = 0
    confusion = {}              # (gold, judged) -> count

    conn = ce._open_conn()
    try:
        for i, p in enumerate(pairs):
            gold = p.get('label')
            check = p.get('check')
            cite = dict(by_name.get(_norm_name(p.get('cn') or '')) or {})
            if not cite:
                reduced_fidelity += 1
                cite = {'kind': 'rule', 'id': None, 'name': p.get('cn'),
                        'source_org': p.get('org')}
            evidence = ev_by_check.get(check)
            if evidence is None:
                reduced_fidelity += 1
            finding = {'check_id': check, 'status': 'fail',
                       'evidence': evidence, 'vocab_status': p.get('vs')}
            try:
                judged = ce.judge_pair(finding, cite, conn=conn,
                                       use_cache=not args.no_cache)['verdict']
            except Exception as e:  # noqa: BLE001
                errors += 1
                judged = 'ERROR'
                print(f'  [{i}] judge error: {e}', file=sys.stderr)
            confusion[(gold, judged)] = confusion.get((gold, judged), 0) + 1
            if gold == 'supports':
                gold_supports += 1
                if judged != 'supports':
                    gold_supports_missed += 1
            if judged == 'supports':
                shown_supports += 1
                if gold == 'supports':
                    shown_correct += 1
    finally:
        if conn is not None:
            conn.close()

    strict = (100.0 * shown_correct / shown_supports) if shown_supports else 0.0
    missed = (100.0 * gold_supports_missed / gold_supports) if gold_supports else 0.0
    passed = strict >= STRICT_PRECISION_TARGET and missed <= MISSED_SUPPORT_TARGET

    metrics = {
        'pairs': n, 'errors': errors, 'reduced_fidelity_joins': reduced_fidelity,
        'shown_as_proof': shown_supports,
        'strict_displayed_proof_precision_pct': round(strict, 1),
        'strict_target_pct': STRICT_PRECISION_TARGET,
        'gold_supports': gold_supports,
        'missed_support_pct': round(missed, 1),
        'missed_support_target_pct': MISSED_SUPPORT_TARGET,
        'confusion': {f'{g}->{j}': c for (g, j), c in sorted(confusion.items())},
        'accepted': passed,
    }
    if args.json:
        print(json.dumps(metrics, indent=1))
    else:
        print(f'pairs judged            : {n} ({errors} errors, '
              f'{reduced_fidelity} reduced-fidelity joins)')
        print(f'strict precision (shown): {strict:.1f}%  (target >= {STRICT_PRECISION_TARGET}%)')
        print(f'missed-support          : {missed:.1f}%  (target <= {MISSED_SUPPORT_TARGET}%)')
        print('confusion               :')
        for (g, j), c in sorted(confusion.items()):
            print(f'  gold={g:<9} judged={j:<9} {c}')
        print('ACCEPTED' if passed else 'NOT ACCEPTED — do not ship '
              '"evidence-grade" language on this judge/prompt')
    return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
