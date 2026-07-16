#!/usr/bin/env python3
"""ab_compare.py — quality-parity diff between two audit JSONs of the SAME URL.

The before/after gate for prompt or pipeline changes (e.g. the Phase-13
runtime-citations change): run one audit on the old code and one on the new,
then compare everything that defines audit quality. Known run-to-run noise on
identical code (measured 2026-07-04): score drift ~2.9 points, ~6 status flips
across ~90 checks, so treat deltas inside that band as noise, not regression.

Usage:
    python3 scripts/ab_compare.py baseline.json candidate.json

Exit 0 when every gate passes, 1 otherwise. Gates:
  - score delta <= 4.0 (noise band + margin)
  - per-check status parity >= 90% on shared check_ids
  - every fail/warn check carries >= 1 citation in BOTH files
  - citation grounding: 0 unresolved citations in the candidate
  - narrative + compact-contract fields present in the candidate
"""

import json
import sys


def load(path):
    with open(path) as f:
        return json.load(f)


def checks_by_id(audit):
    out = {}
    for section in (audit.get('detailed_findings') or []):
        for c in (section.get('checks') or section.get('findings') or []):
            cid = c.get('check_id')
            if cid:
                out[cid] = c
    if not out:
        for c in (audit.get('findings') or []):
            cid = c.get('check_id')
            if cid:
                out[cid] = c
    return out


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    a, b = load(sys.argv[1]), load(sys.argv[2])
    failures = []

    # --- score ---
    sa = (a.get('scoring') or {}).get('overall_score')
    sb = (b.get('scoring') or {}).get('overall_score')
    if sa is not None and sb is not None:
        delta = abs(sa - sb)
        print(f"score: {sa} -> {sb} (delta {delta:.1f}, noise band ~2.9)")
        if delta > 4.0:
            failures.append(f"score delta {delta:.1f} exceeds 4.0 gate")
    else:
        print(f"score: {sa} -> {sb} (one side missing — check gates/inconclusive)")

    # --- per-check status parity ---
    ca, cb = checks_by_id(a), checks_by_id(b)
    shared = sorted(set(ca) & set(cb))
    only_a, only_b = sorted(set(ca) - set(cb)), sorted(set(cb) - set(ca))
    flips = [(cid, ca[cid].get('status'), cb[cid].get('status'))
             for cid in shared if ca[cid].get('status') != cb[cid].get('status')]
    parity = (len(shared) - len(flips)) / len(shared) * 100 if shared else 0
    print(f"checks: {len(ca)} vs {len(cb)} ({len(shared)} shared, "
          f"{len(only_a)} only-baseline, {len(only_b)} only-candidate)")
    print(f"status parity on shared: {parity:.1f}% ({len(flips)} flips, "
          f"noise band ~6 flips)")
    for cid, x, y in flips[:12]:
        print(f"  flip {cid}: {x} -> {y}")
    if shared and parity < 90.0:
        failures.append(f"status parity {parity:.1f}% below 90% gate")

    # --- citations on fail/warn ---
    for name, audit, cmap in (("baseline", a, ca), ("candidate", b, cb)):
        eligible = [c for c in cmap.values()
                    if (c.get('status') or '').lower() in ('fail', 'warn')]
        uncited = [c.get('check_id') for c in eligible if not c.get('citations')]
        total_cites = sum(len(c.get('citations') or []) for c in eligible)
        print(f"{name}: {len(eligible)} fail/warn checks, {total_cites} citations, "
              f"{len(uncited)} uncited")
        if uncited:
            failures.append(f"{name}: uncited fail/warn checks: {uncited[:8]}")

    # --- grounding + attach stats (candidate must be fully grounded) ---
    md = b.get('metadata') or {}
    ground = md.get('citation_grounding') or {}
    attach = md.get('citation_attachment') or {}
    print(f"candidate attach: {attach.get('checks_cited')}/{attach.get('checks_eligible')} "
          f"cited, {attach.get('citations_attached')} attached")
    print(f"candidate grounding: {ground.get('regrounded_live')} live, "
          f"{ground.get('regrounded_snapshot')} snapshot, "
          f"{ground.get('unresolved')} unresolved")
    if (ground.get('unresolved') or 0) > 0:
        failures.append(f"candidate has {ground['unresolved']} unresolved citations")

    # --- narrative + compact-contract fields ---
    narrative = b.get('narrative') or {}
    for field in ('executive_diagnosis',):
        if not narrative.get(field):
            failures.append(f"candidate missing narrative.{field}")
    for field in ('scoring', 'detailed_findings'):
        if not b.get(field):
            failures.append(f"candidate missing top-level {field}")
    if not (b.get('top_5_fixes') or b.get('all_fixes') or narrative.get('top_5_fixes')):
        failures.append("candidate missing top fixes")

    # --- cost telemetry (informational) ---
    for name, audit in (("baseline", a), ("candidate", b)):
        m = audit.get('metadata') or {}
        print(f"{name} cost: nominal ${m.get('cost_usd')} true ${m.get('cost_usd_true', 'n/a')} "
              f"(in={m.get('input_tokens')} out={m.get('output_tokens')} "
              f"cache_r={m.get('cache_read_tokens', 'n/a')} cache_w={m.get('cache_creation_tokens', 'n/a')})")

    print()
    if failures:
        print("AB_COMPARE_FAIL")
        for f_ in failures:
            print(f"  ✗ {f_}")
        return 1
    print("AB_COMPARE_OK — quality parity within noise band")
    return 0


if __name__ == '__main__':
    sys.exit(main())
