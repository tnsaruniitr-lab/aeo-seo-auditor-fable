"""
check_vocab.py — canonical check_id vocabulary + post-loop normalization.

WHY: the agent emits variant check_ids between otherwise-identical runs
(measured: only 74 of ~100 matched across a back-to-back pair — e.g.
'A10_robots_txt' one run, 'A10_robots_txt_crawling' the next). That breaks
cross-run comparability, the delta engine, and per-check citation pinning.

The canonical vocabulary is the mappings key set of
service/ruleset/brain-mappings.json (108 checks, letter+number prefixes are
unique). Normalization is conservative:

  - exact canonical id                       -> untouched
  - sub-check id ('A2b_...')                 -> untouched (deterministic
    scripts emit these; they are stable already)
  - same letter+number, different tail       -> renamed to the canonical id,
    UNLESS another finding already uses it (collision -> untouched, flagged)
  - unknown letter+number / unparseable      -> untouched, flagged

Never raises; returns (audit, stats) like the other post-loop passes.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger('audit.vocab')

_MAPPINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'ruleset', 'brain-mappings.json')
_CHECK_RE = re.compile(r'^([A-J])(\d+)([a-z]?)_')

_REGISTRY = None  # (canonical_ids: set, by_prefix: {(letter, num): canonical})


def _load_registry():
    global _REGISTRY
    if _REGISTRY is None:
        with open(_MAPPINGS_PATH) as f:
            keys = list(json.load(f)['mappings'].keys())
        canonical = set(keys)
        by_prefix: Dict[Tuple[str, str], str] = {}
        for k in keys:
            m = _CHECK_RE.match(k)
            if m and not m.group(3):  # sub-letter canonicals would be ambiguous
                by_prefix[(m.group(1), m.group(2))] = k
        _REGISTRY = (canonical, by_prefix)
    return _REGISTRY


def canonical_check_id(check_id: Any) -> Optional[str]:
    """The canonical id this check_id should map to, or None if it is already
    canonical / a sub-check / unknown."""
    if not isinstance(check_id, str):
        return None
    canonical, by_prefix = _load_registry()
    if check_id in canonical:
        return None
    m = _CHECK_RE.match(check_id)
    if not m or m.group(3):  # unparseable or sub-check ('A2b_...') — leave
        return None
    return by_prefix.get((m.group(1), m.group(2)))


def normalize_check_ids(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Rename variant check_ids in audit['findings'] to their canonical form.
    Mutates and returns the audit plus a stats dict. Never raises."""
    stats: Dict[str, Any] = {'applied': True, 'renamed': 0, 'unknown': 0,
                             'collisions': 0, 'renames': []}
    try:
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats
        canonical, _ = _load_registry()
        in_use = {f.get('check_id') for f in findings if isinstance(f, dict)}
        for f in findings:
            if not isinstance(f, dict):
                continue
            cid = f.get('check_id')
            if not isinstance(cid, str) or not cid:
                continue
            target = canonical_check_id(cid)
            if target is None:
                # flag genuinely foreign ids (not canonical, not a sub-check,
                # prefix not in the registry) for observability
                m = _CHECK_RE.match(cid)
                is_sub_check = bool(m and m.group(3))
                if cid not in canonical and not is_sub_check:
                    stats['unknown'] += 1
                continue
            if target in in_use:
                stats['collisions'] += 1
                continue
            f['check_id'] = target
            in_use.discard(cid)
            in_use.add(target)
            stats['renamed'] += 1
            if len(stats['renames']) < 20:
                stats['renames'].append({'from': cid, 'to': target})
    except Exception as e:  # noqa: BLE001 — must never break the audit
        log.error('check_id normalization failed: %s', e)
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
    return audit, stats


# ---------------------------------------------------------------------------
# Self-test (stdlib only, uses the real brain-mappings.json in the repo)
# ---------------------------------------------------------------------------

def _selftest() -> None:
    canonical, by_prefix = _load_registry()
    assert len(canonical) >= 100, len(canonical)
    assert ('A', '10') in by_prefix, by_prefix

    a10 = by_prefix[('A', '10')]          # A10_robots_txt_crawling
    audit = {'findings': [
        {'check_id': 'A10_robots_txt', 'status': 'fail'},       # variant -> renamed
        {'check_id': a10.replace('A10', 'A1'), 'status': 'na'}, # A1_... variant of A1
        {'check_id': 'A2b_title_uniqueness_sample'},            # sub-check -> untouched
        {'check_id': 'Z99_not_a_thing'},                        # unknown -> untouched
        {'no_check_id': True},
    ]}
    audit, stats = normalize_check_ids(audit)
    ids = [f.get('check_id') for f in audit['findings']]
    assert ids[0] == a10, (ids, stats)
    assert stats['renamed'] == 2, stats                          # A10 + A1 variants
    assert ids[2] == 'A2b_title_uniqueness_sample', ids
    assert ids[3] == 'Z99_not_a_thing' and stats['unknown'] >= 1, (ids, stats)

    # Collision guard: variant must NOT rename onto an id another finding uses.
    audit2 = {'findings': [
        {'check_id': a10, 'status': 'pass'},
        {'check_id': 'A10_robots_txt', 'status': 'fail'},
    ]}
    audit2, s2 = normalize_check_ids(audit2)
    assert audit2['findings'][1]['check_id'] == 'A10_robots_txt', audit2
    assert s2['collisions'] == 1, s2

    # Robustness: junk shapes never raise.
    for shape in ({}, {'findings': None}, {'findings': ['x', 3]}):
        _, s3 = normalize_check_ids(dict(shape))
        assert s3['applied'] is True, (shape, s3)

    print('VOCAB_OK')


if __name__ == '__main__':
    _selftest()
