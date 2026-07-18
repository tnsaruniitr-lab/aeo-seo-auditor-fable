"""
check_vocab.py — canonical check_id vocabulary + post-loop normalization.

WHY: the agent emits variant check_ids between otherwise-identical runs
(measured: only 74 of ~100 matched across a back-to-back pair — e.g.
'A10_robots_txt' one run, 'A10_robots_txt_crawling' the next). That breaks
cross-run comparability, the delta engine, and per-check citation pinning.

The canonical vocabulary is the mappings key set of
service/ruleset/brain-mappings.json (108 checks, letter+number prefixes are
unique). Normalization is conservative — renaming now requires SEMANTIC
agreement, not just an occupied letter+number slot (the old rule rewrote
cross-topic ids like 'A1_robots_txt_crawling' into 'A1_https_enforcement',
silently changing what the finding claims to be about):

  - exact canonical id                       -> untouched, vocab_status='canonical'
  - sub-check id ('A2b_...')                 -> untouched (deterministic
    scripts emit these; they are stable already), no flag
  - same letter+number, different tail       -> renamed ONLY when the semantic
    guard agrees (explicit alias in ruleset/check-aliases.json, OR >=1
    meaningful stopword-stripped token shared between the variant tail and
    the canonical tail); then vocab_status='aliased' and original_check_id
    records the pre-rename id. Collision with an id another finding already
    uses -> untouched, flagged (as before).
  - guard failure / unknown slot / unparseable -> untouched,
    vocab_status='foreign' (the model's id is kept verbatim — downstream
    passes must treat it as evidence-led, never mapping-led)

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
_ALIASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'ruleset', 'check-aliases.json')
_CHECK_RE = re.compile(r'^([A-J])(\d+)([a-z]?)_')

_REGISTRY = None  # (canonical_ids: set, by_prefix: {(letter, num): canonical})
_ALIASES = None   # {variant_id: canonical_id} — explicit known-drift renames


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


def _load_aliases() -> Dict[str, str]:
    global _ALIASES
    if _ALIASES is None:
        try:
            with open(_ALIASES_PATH) as f:
                raw = json.load(f).get('aliases') or {}
            _ALIASES = {k: v for k, v in raw.items()
                        if isinstance(k, str) and isinstance(v, str)}
        except Exception as e:  # noqa: BLE001 — missing/bad table just disables it
            log.warning('check-aliases.json unavailable: %s', e)
            _ALIASES = {}
    return _ALIASES


# ---------------------------------------------------------------------------
# Semantic guard — a rename must be provably about the SAME check.
# ---------------------------------------------------------------------------

# Generic audit-vocabulary filler that appears in check-name tails without
# carrying topical meaning ('A5_robots_meta_present' vs 'A2_title_present'
# must not alias on 'present').
_TAIL_STOP = frozenset(
    'the a an and or of to in for on with is are be has have had no not '
    'check checks checked page pages present presence missing valid invalid '
    'proper properly correct correctly ensure ensures without via'.split())


def _tail_tokens(check_id: str) -> set:
    """Stopword-stripped tokens of a check id's name tail ('A10_robots_txt'
    -> {'robots', 'txt'})."""
    tail = _CHECK_RE.sub('', check_id)
    return {w for w in re.findall(r'[a-z0-9]+', tail.lower())
            if len(w) >= 2 and w not in _TAIL_STOP}


def _semantically_same(variant: str, canonical_id: str) -> bool:
    """May `variant` be renamed to `canonical_id`? True only with an explicit
    alias-table entry OR >=1 meaningful shared token between the two name
    tails. Cross-topic slot squatters ('A1_robots_txt_crawling' vs
    'A1_https_enforcement') fail both tests and stay foreign."""
    if _load_aliases().get(variant) == canonical_id:
        return True
    return bool(_tail_tokens(variant) & _tail_tokens(canonical_id))


def normalize_check_ids(audit: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Rename variant check_ids in audit['findings'] to their canonical form
    when the semantic guard approves, and stamp vocabulary provenance
    (vocab_status / original_check_id) on every finding with a check_id.
    Mutates and returns the audit plus a stats dict. Never raises."""
    stats: Dict[str, Any] = {'applied': True, 'renamed': 0, 'unknown': 0,
                             'collisions': 0, 'foreign': 0, 'renames': []}
    try:
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats
        canonical, by_prefix = _load_registry()
        in_use = {f.get('check_id') for f in findings if isinstance(f, dict)}
        for f in findings:
            if not isinstance(f, dict):
                continue
            cid = f.get('check_id')
            if not isinstance(cid, str) or not cid:
                continue
            if cid in canonical:
                f['vocab_status'] = 'canonical'
                continue
            m = _CHECK_RE.match(cid)
            if m and m.group(3):
                continue  # sub-check ('A2b_...') — untouched, no flag
            target = by_prefix.get((m.group(1), m.group(2))) if m else None
            if target is None:
                # unknown slot / unparseable — model-invented, left verbatim
                f['vocab_status'] = 'foreign'
                stats['unknown'] += 1
                stats['foreign'] += 1
                continue
            if not _semantically_same(cid, target):
                # slot occupied but semantically divergent — a rename here
                # would rewrite what the finding is ABOUT. Keep the model's
                # id; downstream retrieval goes evidence-led.
                f['vocab_status'] = 'foreign'
                stats['foreign'] += 1
                continue
            if target in in_use:
                # collision guard (unchanged): the canonical id is already
                # taken by another finding. Left with a non-canonical id, so
                # downstream must treat it as foreign.
                stats['collisions'] += 1
                f['vocab_status'] = 'foreign'
                stats['foreign'] += 1
                continue
            f['check_id'] = target
            f['original_check_id'] = cid
            f['vocab_status'] = 'aliased'
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
    a1 = by_prefix[('A', '1')]            # A1_https_enforcement
    audit = {'findings': [
        {'check_id': 'A10_robots_txt', 'status': 'fail'},       # variant -> renamed
        {'check_id': a10.replace('A10', 'A1'), 'status': 'na'}, # CROSS-TOPIC squatter
        {'check_id': 'A2b_title_uniqueness_sample'},            # sub-check -> untouched
        {'check_id': 'Z99_not_a_thing'},                        # unknown -> untouched
        {'check_id': a1, 'status': 'pass'},                     # exact canonical
        {'check_id': 'B10_cwv', 'status': 'warn'},              # alias-table rename
        {'no_check_id': True},
    ]}
    audit, stats = normalize_check_ids(audit)
    f = audit['findings']
    ids = [x.get('check_id') for x in f]

    # Positive: shared-token variant renamed, provenance stamped.
    assert ids[0] == a10, (ids, stats)
    assert f[0]['vocab_status'] == 'aliased', f[0]
    assert f[0]['original_check_id'] == 'A10_robots_txt', f[0]

    # INVERTED (semantic guard): 'A1_robots_txt_crawling' shares NO meaningful
    # token with 'A1_https_enforcement' — must NOT be renamed, must be flagged.
    assert ids[1] == a10.replace('A10', 'A1'), (ids, stats)
    assert f[1]['vocab_status'] == 'foreign' and 'original_check_id' not in f[1], f[1]

    assert ids[2] == 'A2b_title_uniqueness_sample', ids
    assert 'vocab_status' not in f[2], ('sub-checks stay unflagged', f[2])
    assert ids[3] == 'Z99_not_a_thing' and f[3]['vocab_status'] == 'foreign', f[3]
    assert f[4]['vocab_status'] == 'canonical' and 'original_check_id' not in f[4], f[4]

    # Alias table: 'B10_cwv' has zero token overlap with the canonical tail,
    # but is an explicit known-drift alias -> renamed.
    assert ids[5] == 'B10_core_web_vitals', (ids, stats)
    assert f[5]['vocab_status'] == 'aliased' and f[5]['original_check_id'] == 'B10_cwv', f[5]

    assert stats['renamed'] == 2, stats                          # A10 variant + B10 alias
    assert stats['unknown'] == 1 and stats['foreign'] == 2, stats  # Z99 + A1 squatter

    # Collision guard: variant must NOT rename onto an id another finding uses.
    audit2 = {'findings': [
        {'check_id': a10, 'status': 'pass'},
        {'check_id': 'A10_robots_txt', 'status': 'fail'},
    ]}
    audit2, s2 = normalize_check_ids(audit2)
    assert audit2['findings'][1]['check_id'] == 'A10_robots_txt', audit2
    assert audit2['findings'][1]['vocab_status'] == 'foreign', audit2
    assert s2['collisions'] == 1, s2

    # Guard internals: cross-topic tails do not overlap; filler words carry
    # no aliasing weight.
    assert not _semantically_same(a10.replace('A10', 'A1'), a1)
    assert _semantically_same('A1_https', a1)
    assert not _semantically_same('A5_robots_meta_present', 'A2_title_present')

    # Robustness: junk shapes never raise.
    for shape in ({}, {'findings': None}, {'findings': ['x', 3]}):
        _, s3 = normalize_check_ids(dict(shape))
        assert s3['applied'] is True, (shape, s3)

    print('VOCAB_OK')


if __name__ == '__main__':
    _selftest()
