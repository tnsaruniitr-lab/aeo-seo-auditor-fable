"""
billing.py — Per-key metering, quota, and Stripe metered-billing scaffold.

Before this, there was NO path to money and NO cost guardrail: the programmatic
API could be driven for free while burning uncapped Anthropic tokens per audit —
a single integration partner or abuser could run unbounded paid-cost audits
against the operator's card.

This module gives each API key an identity, meters audits against a Supabase
`api_usage` table, enforces a monthly quota, and exposes the seam for Stripe
usage-based billing. The Stripe calls are scaffolded (guarded behind
STRIPE_SECRET_KEY) with explicit TODOs — wire real price/meter IDs to go live.

Design: fail OPEN on infrastructure errors (never block a legitimate paid audit
because Supabase hiccuped) but fail CLOSED on a confirmed over-quota. Never
raises to the caller; returns a decision object.

Env:
    BILLING_ENABLED=1            turn metering/quota on (off by default)
    FREE_MONTHLY_QUOTA=50        audits/key/month before quota blocks
    STRIPE_SECRET_KEY=sk_...     enable Stripe usage reporting (optional)
    STRIPE_METER_EVENT=audit_run the Stripe billing meter event name
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any, Dict, Optional

log = logging.getLogger('audit.billing')

BILLING_ENABLED = os.getenv('BILLING_ENABLED') == '1'
FREE_MONTHLY_QUOTA = int(os.getenv('FREE_MONTHLY_QUOTA', '50'))
STRIPE_SECRET_KEY = os.getenv('STRIPE_SECRET_KEY', '')
STRIPE_METER_EVENT = os.getenv('STRIPE_METER_EVENT', 'audit_run')


def key_id(api_key: str) -> str:
    """Stable, non-reversible identifier for an API key (never store the key)."""
    return 'k_' + hashlib.sha256((api_key or '').encode()).hexdigest()[:16]


def _month_bucket(now_iso: Optional[str]) -> str:
    """YYYY-MM bucket. now_iso is passed in (callers stamp time) so this stays
    pure/testable and avoids clock calls here."""
    return (now_iso or '')[:7] or 'unknown'


def check_and_meter(api_key: str, *, now_iso: Optional[str] = None) -> Dict[str, Any]:
    """Decide whether this key may run an audit, and record one unit of usage.

    Returns {allowed: bool, reason: str, key_id, used, quota, remaining}.
    - Billing OFF  → always allowed (metering skipped).
    - Over quota   → allowed=False (fail closed on confirmed over-quota).
    - Infra error  → allowed=True (fail open; log for follow-up).
    """
    kid = key_id(api_key)
    if not BILLING_ENABLED:
        return {'allowed': True, 'reason': 'billing disabled', 'key_id': kid,
                'used': None, 'quota': None, 'remaining': None}

    bucket = _month_bucket(now_iso)
    try:
        used = _get_usage(kid, bucket)
    except Exception as e:  # fail open on infra error
        log.warning('billing usage lookup failed (%s) — allowing', e)
        return {'allowed': True, 'reason': 'usage-lookup-failed (fail-open)',
                'key_id': kid, 'used': None, 'quota': FREE_MONTHLY_QUOTA,
                'remaining': None}

    if used >= FREE_MONTHLY_QUOTA:
        return {'allowed': False, 'reason': f'monthly quota {FREE_MONTHLY_QUOTA} reached',
                'key_id': kid, 'used': used, 'quota': FREE_MONTHLY_QUOTA, 'remaining': 0}

    # Record the unit (best-effort) and report it to Stripe if configured.
    try:
        _increment_usage(kid, bucket)
    except Exception as e:
        log.warning('billing increment failed (%s) — allowing', e)
    _report_to_stripe(kid)

    return {'allowed': True, 'reason': 'ok', 'key_id': kid,
            'used': used + 1, 'quota': FREE_MONTHLY_QUOTA,
            'remaining': max(0, FREE_MONTHLY_QUOTA - used - 1)}


# ---------------------------------------------------------------------------
# Supabase-backed usage counter (reuses tools._supabase_base_headers)
# Table `api_usage` (create via migration): key_id text, month text,
#   count int, updated_at timestamptz; primary key (key_id, month).
# ---------------------------------------------------------------------------

def _get_usage(kid: str, bucket: str) -> int:
    from tools import _supabase_base_headers
    base, headers = _supabase_base_headers()
    if base is None:
        return 0
    import httpx
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f'{base}/rest/v1/api_usage', headers=headers,
                       params={'key_id': f'eq.{kid}', 'month': f'eq.{bucket}',
                               'select': 'count', 'limit': '1'})
        if r.status_code == 200 and r.json():
            return int(r.json()[0].get('count', 0))
    return 0


def _increment_usage(kid: str, bucket: str) -> None:
    """Upsert count+1 for (key_id, month). Uses PostgREST merge-duplicates."""
    from tools import _supabase_base_headers
    base, headers = _supabase_base_headers()
    if base is None:
        return
    import httpx
    current = _get_usage(kid, bucket)
    body = {'key_id': kid, 'month': bucket, 'count': current + 1}
    h = dict(headers)
    h['Prefer'] = 'resolution=merge-duplicates'
    with httpx.Client(timeout=10.0) as client:
        client.post(f'{base}/rest/v1/api_usage', headers=h, json=body)


def _report_to_stripe(kid: str) -> None:
    """Report one usage unit to Stripe's billing meter.

    SCAFFOLD — enabled only when STRIPE_SECRET_KEY is set. To go live:
      1. Create a Stripe billing Meter with event_name == STRIPE_METER_EVENT.
      2. Map each api key_id to a Stripe customer id (add a column to api_usage
         or a keys table).
      3. Uncomment the meter-event POST below and supply stripe_customer_id.
    """
    if not STRIPE_SECRET_KEY:
        return
    try:
        # import stripe; stripe.api_key = STRIPE_SECRET_KEY
        # stripe.billing.MeterEvent.create(
        #     event_name=STRIPE_METER_EVENT,
        #     payload={'stripe_customer_id': <lookup kid>, 'value': '1'},
        # )
        log.info('stripe meter event (scaffold) key_id=%s event=%s', kid, STRIPE_METER_EVENT)
    except Exception as e:  # never break an audit on a billing-report error
        log.warning('stripe report failed: %s', e)
