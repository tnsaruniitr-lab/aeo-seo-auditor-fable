"""
rule_eval.py — Mode C: deterministic rule binding for MEASURED checks.

For checks whose status is computed by the deterministic scripts over real
bytes (scoring.MEASURED_CHECK_BASES), the rule outcome is not a matter of LLM
judgement — so we bind a curated rule VERBATIM from rule-bindings.json when its
predicate holds. binding_verified is True by construction (a human curated the
id→check link and the script decided the status), and the bound rule can carry
scoring weight (Phase 4). An unmapped base gets no binding — never a wrong one.

Pure + deterministic; no LLM, no network. The rule ROW (for name/source_url) is
resolved via an injectable resolver so this is unit-testable without a DB.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Dict, Optional, Tuple

_RULESET_DIR = os.path.join(os.path.dirname(__file__), 'ruleset')
_BINDINGS_PATH = os.path.join(_RULESET_DIR, 'rule-bindings.json')
_CITED = ('fail', 'warn')
_CHECK_BASE_RE = re.compile(r'^([A-J]\d{1,2}[a-z]?)(?:_|$)')

_BINDINGS: Optional[Dict[str, list]] = None
_MEASURED: Optional[frozenset] = None


def _load_bindings() -> Dict[str, list]:
    global _BINDINGS
    if _BINDINGS is None:
        try:
            with open(_BINDINGS_PATH) as f:
                _BINDINGS = (json.load(f) or {}).get('bindings', {}) or {}
        except Exception:
            _BINDINGS = {}
    return _BINDINGS


def _measured_bases() -> frozenset:
    global _MEASURED
    if _MEASURED is None:
        try:
            from scoring import MEASURED_CHECK_BASES
            _MEASURED = MEASURED_CHECK_BASES
        except Exception:
            _MEASURED = frozenset()
    return _MEASURED


def check_base(check_id: Optional[str]) -> Optional[str]:
    if not isinstance(check_id, str):
        return None
    m = _CHECK_BASE_RE.match(check_id.split(':', 1)[-1])
    return m.group(1) if m else None


def _default_resolver() -> Callable[[str, Any], Optional[dict]]:
    """Resolve (kind, id) to a snapshot row; falls back to an empty resolver."""
    try:
        import sys
        sys.path.insert(0, _RULESET_DIR)
        from ranker import BrainIndex
        brain = BrainIndex.from_export_dir(_RULESET_DIR)
        by = {'rule': brain.rules_by_id, 'ap': brain.aps_by_id,
              'principle': brain.principles_by_id}

        def resolve(kind: str, rid: Any) -> Optional[dict]:
            try:
                return by.get(kind, {}).get(int(rid))
            except (TypeError, ValueError):
                return None
        return resolve
    except Exception:
        return lambda kind, rid: None


def evaluate_measured_bindings(
    audit: Dict[str, Any],
    resolve: Optional[Callable[[str, Any], Optional[dict]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Attach a verbatim, verified bound_rule to every MEASURED fail/warn finding
    that has a curated binding. Mutates and returns (audit, stats). Never raises."""
    stats = {'applied': True, 'measured_findings': 0, 'bound': 0,
             'unresolved': 0, 'no_binding': 0}
    try:
        findings = audit.get('findings')
        if not isinstance(findings, list):
            return audit, stats
        if resolve is None:
            resolve = _default_resolver()
        measured = _measured_bases()
        bindings = _load_bindings()

        for f in findings:
            if not isinstance(f, dict) or f.get('status') not in _CITED:
                continue
            base = check_base(f.get('check_id'))
            if not base or base not in measured:
                continue
            stats['measured_findings'] += 1
            candidates = bindings.get(base) or []
            if not candidates:
                stats['no_binding'] += 1
                continue
            bound = None
            for b in candidates:
                allowed = (b.get('when') or {}).get('status') or list(_CITED)
                if f.get('status') not in allowed:
                    continue
                row = resolve(b.get('kind'), b.get('id'))
                if not row:
                    stats['unresolved'] += 1
                    continue
                bound = {
                    'kind': b.get('kind'), 'id': b.get('id'),
                    'name': row.get('name') or row.get('title'),
                    'source_org': row.get('source_org'),
                    'source_url': row.get('source_url'),
                    'confidence_score': str(row.get('confidence_score') or ''),
                    'if_condition': (row.get('if_condition') or row.get('statement')
                                     or row.get('description') or '')[:500],
                    'then_action': (row.get('then_action') or row.get('explanation') or '')[:500],
                    'binding_verified': True,
                    'basis': 'deterministic',
                }
                break
            if bound:
                f['bound_rule'] = bound
                stats['bound'] += 1
            else:
                stats['no_binding'] += 1
    except Exception as e:  # noqa: BLE001 — binding must never break an audit
        stats['applied'] = False
        stats['error'] = f'{type(e).__name__}: {e}'
    return audit, stats


def _selftest() -> None:
    # A fake resolver so we need no DB: only rule 1489 exists.
    def resolve(kind, rid):
        if (kind, int(rid)) == ('rule', 1489):
            return {'id': 1489, 'name': 'Enforce HTTPS across entire domain',
                    'source_org': 'Perplexity', 'source_url': 'https://ex/https',
                    'if_condition': 'if served over http', 'then_action': 'redirect to https',
                    'confidence_score': '0.95'}
        return None

    audit = {'findings': [
        {'check_id': 'A1_https_enforcement', 'status': 'fail'},     # measured + bound
        {'check_id': 'A3_meta_description', 'status': 'warn'},      # measured, id 8192 unresolved here
        {'check_id': 'F5_answer_quality', 'status': 'fail'},        # NOT measured -> skip
        {'check_id': 'A1_https_enforcement', 'status': 'pass'},     # pass -> skip
    ]}
    audit, stats = evaluate_measured_bindings(audit, resolve=resolve)
    f = audit['findings']
    assert f[0]['bound_rule']['id'] == 1489, f[0]
    assert f[0]['bound_rule']['binding_verified'] is True
    assert f[0]['bound_rule']['basis'] == 'deterministic'
    assert f[0]['bound_rule']['source_url'] == 'https://ex/https'
    assert 'bound_rule' not in f[1], 'unresolved id must NOT bind a wrong rule'
    assert 'bound_rule' not in f[2], 'non-measured check must not be deterministically bound'
    assert 'bound_rule' not in f[3]
    assert stats['bound'] == 1 and stats['measured_findings'] == 2, stats
    assert stats['unresolved'] == 1, stats

    # Robustness: junk shapes never raise.
    for shape in ({}, {'findings': None}, {'findings': [7]}):
        _, s = evaluate_measured_bindings(dict(shape), resolve=resolve)
        assert s['applied'] is True, (shape, s)

    print('RULE_EVAL_OK')


if __name__ == '__main__':
    _selftest()
