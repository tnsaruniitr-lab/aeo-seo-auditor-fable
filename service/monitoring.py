"""
monitoring.py — Structured metrics for audit health and quality drift.

Before this, production was flying blind: no success rate, no p95 duration, no
per-audit cost trend, no score-distribution drift signal. Once the headline
score is model-influenced (even with deterministic grading, the check statuses
are model-assigned), you MUST be able to see quality regressions.

This module emits one structured JSON metric line per event. That is
intentionally a seam: today it lands in the logs (grep/ship to any log-based
metrics backend); swap `_emit` for a StatsD/OTLP/Prometheus push without
touching call sites. Never raises — a metrics failure must never fail an audit.

Stdlib only.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

log = logging.getLogger('audit.metrics')


def _emit(event: str, fields: Dict[str, Any]) -> None:
    """Emit one structured metric line. Replace this body to push to a TSDB."""
    try:
        payload = {'metric': event}
        payload.update({k: v for k, v in fields.items() if v is not None})
        log.info('METRIC %s', json.dumps(payload, default=str, ensure_ascii=False))
    except Exception:  # metrics must never break the caller
        pass


def audit_started(audit_id: str, url: str, mode: str,
                  profile: Optional[str] = None) -> None:
    # profile=None (default/full runs) is dropped by _emit — the metric line
    # for existing callers is unchanged; light runs stamp profile='light'.
    _emit('audit.started', {'audit_id': audit_id, 'url': url, 'mode': mode,
                            'profile': profile})


def audit_completed(audit: Dict[str, Any]) -> None:
    """Record a successful audit with its cost/quality signals."""
    scoring = (audit or {}).get('scoring') or {}
    md = (audit or {}).get('metadata') or {}
    _emit('audit.completed', {
        'audit_id': audit.get('audit_id'),
        'domain': audit.get('domain'),
        # None for full audits (dropped by _emit); 'light' for light runs.
        'profile': md.get('profile'),
        'target': md.get('target'),
        'overall_score': scoring.get('overall_score'),
        'overall_grade': scoring.get('overall_grade'),
        'inconclusive': scoring.get('inconclusive', False),
        'duration_seconds': audit.get('duration_seconds'),
        'input_tokens': md.get('input_tokens'),
        'output_tokens': md.get('output_tokens'),
        'cache_read_tokens': md.get('cache_read_tokens'),
        'cache_creation_tokens': md.get('cache_creation_tokens'),
        'web_search_requests': md.get('web_search_requests'),
        'cost_usd': md.get('cost_usd'),
        'cost_usd_true': md.get('cost_usd_true'),
        'ai_visibility_cost_usd': md.get('ai_visibility_cost_usd'),
        'tool_call_count': md.get('tool_call_count'),
        'scoring_authority': md.get('scoring_authority'),
        'persisted': (md.get('persistence') or {}).get('persisted'),
    })


def audit_failed(audit_id: str, url: str, reason: str,
                 duration_seconds: Optional[float] = None) -> None:
    _emit('audit.failed', {
        'audit_id': audit_id, 'url': url, 'reason': reason,
        'duration_seconds': duration_seconds,
    })


def audit_reaped(audit_id: str, age_seconds: float) -> None:
    _emit('audit.reaped', {'audit_id': audit_id, 'age_seconds': round(age_seconds, 1)})
