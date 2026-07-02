"""
delta.py — Fix-verification / re-score / delta engine.

THE PRODUCT LOOP. An auditor's commercial value is not the one-shot teardown —
it is proving movement: "we told you to fix X; your score went 62 → 81, 7
findings resolved, 1 regressed." That audit → fix → re-audit → delta loop is the
retention hook and the ROI artifact a customer (or an AnswerMonk partner) can
show. It was entirely absent before.

This module diffs two audits of the same domain by `check_id` and reports what
resolved, persisted, newly appeared, or regressed — plus the score delta. It is
pure data work: no LLM call, fully deterministic, cheap to run on every re-audit.

Stdlib only (Supabase fetch is delegated to tools.fetch_audit).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# Status ranking for detecting improvement/regression on the SAME check.
_STATUS_RANK = {'fail': 0, 'warn': 1, 'na': 2, 'pass': 3}


def _index_findings(audit: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map check_id -> finding for one audit (last write wins on dupes)."""
    out: Dict[str, Dict[str, Any]] = {}
    for f in (audit.get('findings') or []):
        if isinstance(f, dict):
            cid = str(f.get('check_id') or '').strip()
            if cid:
                out[cid] = f
    return out


def _status(f: Optional[Dict[str, Any]]) -> str:
    if not f:
        return 'absent'
    return str(f.get('status', 'na')).strip().lower()


def compute_delta(prior: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Diff two audits (prior → current) of the same page.

    Returns a structured delta:
        {
          score_delta: {prior, current, change, grade_prior, grade_current},
          resolved:   [check_id, ...]   # was fail/warn, now pass (or gone-good)
          regressed:  [check_id, ...]   # was pass, now fail/warn
          new_issues: [check_id, ...]   # absent/na before, now fail/warn
          persisting: [check_id, ...]   # fail/warn in both
          summary: str,
        }
    """
    p_idx = _index_findings(prior)
    c_idx = _index_findings(current)
    all_ids = set(p_idx) | set(c_idx)

    resolved, regressed, new_issues, persisting = [], [], [], []
    for cid in sorted(all_ids):
        ps, cs = _status(p_idx.get(cid)), _status(c_idx.get(cid))
        p_bad = ps in ('fail', 'warn')
        c_bad = cs in ('fail', 'warn')
        if p_bad and not c_bad:
            resolved.append(cid)
        elif not p_bad and c_bad and ps != 'absent':
            regressed.append(cid)
        elif c_bad and ps in ('absent', 'na'):
            new_issues.append(cid)
        elif p_bad and c_bad:
            persisting.append(cid)

    def _score(a: Dict[str, Any]) -> Optional[float]:
        v = (a.get('scoring') or {}).get('overall_score')
        return v if isinstance(v, (int, float)) else None

    def _grade(a: Dict[str, Any]) -> Optional[str]:
        return (a.get('scoring') or {}).get('overall_grade')

    sp, sc = _score(prior), _score(current)
    change = round(sc - sp, 1) if (sp is not None and sc is not None) else None

    direction = (
        'no prior score to compare' if change is None else
        f'up {change}' if change > 0 else
        f'down {abs(change)}' if change < 0 else
        'unchanged'
    )
    summary = (
        f"Score {direction}"
        + (f" ({sp} → {sc})" if sp is not None and sc is not None else "")
        + f". {len(resolved)} resolved, {len(regressed)} regressed, "
          f"{len(new_issues)} new, {len(persisting)} still open."
    )

    return {
        'score_delta': {
            'prior': sp, 'current': sc, 'change': change,
            'grade_prior': _grade(prior), 'grade_current': _grade(current),
        },
        'resolved': resolved,
        'regressed': regressed,
        'new_issues': new_issues,
        'persisting': persisting,
        'counts': {
            'resolved': len(resolved), 'regressed': len(regressed),
            'new_issues': len(new_issues), 'persisting': len(persisting),
        },
        'summary': summary,
    }


def delta_against_prior(current: Dict[str, Any],
                        prior_audit_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Compute the delta of `current` against a prior audit.

    If prior_audit_id is given, diffs against that specific audit. Otherwise
    fetches the most recent PRIOR audit for the same domain from Supabase.
    Returns None when there is no prior audit to compare against.
    """
    try:
        from tools import fetch_audit, list_audits_for_domain
    except Exception:
        return None

    prior: Optional[Dict[str, Any]] = None
    if prior_audit_id:
        prior = fetch_audit(audit_id=prior_audit_id)
    else:
        domain = current.get('domain')
        cur_id = current.get('audit_id')
        if not domain:
            return None
        # Most recent audit for the domain that isn't the current one.
        rows = list_audits_for_domain(domain, limit=5) or []
        for row in rows:
            rid = row.get('audit_id') if isinstance(row, dict) else None
            if rid and rid != cur_id:
                prior = fetch_audit(audit_id=rid)
                break

    if not prior:
        return None
    return compute_delta(prior, current)
