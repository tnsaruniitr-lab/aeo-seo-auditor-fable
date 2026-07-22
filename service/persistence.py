"""
persistence.py — Durability layer: job status + artifact regeneration.

Two ephemeral-state problems this addresses:

1. Report links rot INCONSISTENTLY after a redeploy. The audit JSON is persisted
   to Supabase, but .md and .pdf artifacts lived only on the container's local
   disk, so after any restart /audit/{id}/md and /pdf 404'd while /json still
   worked. The audit JSON is a complete description of the report, so both
   formats can be REGENERATED from Supabase on demand — making all three formats
   fall back identically. That is what regenerate_markdown / regenerate_pdf do.

2. Job status is in-memory only, so a redeploy loses the record that an audit
   ever ran. save_job_status / load_job_status write-through to a Supabase
   `audit_jobs` table (best-effort) so status survives a restart. (A full
   separate-worker durable queue is the next step; this makes the RECORD durable
   without changing the execution model.)

Everything is best-effort and never raises into the request path.

Stdlib + httpx (already a dependency).
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger('audit.persistence')


# ---------------------------------------------------------------------------
# Artifact regeneration — rebuild .md / .pdf from the persisted audit JSON
# ---------------------------------------------------------------------------

def _load_audit(audit_id: str) -> Optional[Dict[str, Any]]:
    try:
        from tools import fetch_audit
        return fetch_audit(audit_id=audit_id)
    except Exception as e:
        log.warning('regenerate: fetch_audit failed for %s: %s', audit_id, e)
        return None


def _render_compat(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Adapt a persisted audit to the shape the renderers expect (mirrors
    agent._render_compat so regeneration matches first-run rendering)."""
    return {
        'audit_id': audit.get('audit_id'),
        'url': audit.get('url'),
        'domain': audit.get('domain'),
        'date': audit.get('date'),
        'duration_seconds': audit.get('duration_seconds'),
        'classification': audit.get('classification', {}),
        'scoring': audit.get('scoring', {}),
        'findings': audit.get('findings', []),
        'narrative': audit.get('narrative', {}),
        'scripts_output': {
            'bots_eye_view': audit.get('bots_eye_view', {}),
            'all_checks': {f.get('check_id'): f for f in audit.get('findings', [])
                           if isinstance(f, dict) and f.get('check_id')},
        },
        'brain_stats': (audit.get('metadata', {}) or {}).get('brain_stats', {}),
    }


def regenerate_markdown(audit_id: str) -> Optional[str]:
    """Return the Markdown report for a persisted audit, rebuilt from Supabase.
    None if the audit isn't found or rendering fails."""
    audit = _load_audit(audit_id)
    if not audit:
        return None
    try:
        from audit_pipeline import render_markdown_report
        return render_markdown_report(_render_compat(audit))
    except Exception as e:
        log.warning('regenerate_markdown failed for %s: %s', audit_id, e)
        return None


def regenerate_pdf(audit_id: str, out_dir: Optional[Path] = None) -> Optional[Path]:
    """Rebuild the PDF for a persisted audit from Supabase. Returns the path or
    None. Writes into out_dir (or a temp dir) so it works on an ephemeral FS."""
    audit = _load_audit(audit_id)
    if not audit:
        return None
    try:
        from audit_pipeline import render_pdf_summary
        base_dir = Path(out_dir) if out_dir else Path(tempfile.gettempdir())
        base_dir.mkdir(parents=True, exist_ok=True)
        slug = str(audit.get('domain', 'audit')).replace('.', '-')
        base_path = base_dir / f"{slug}-{str(audit.get('audit_id',''))[:8]}"
        return render_pdf_summary(_render_compat(audit), base_path)
    except Exception as e:
        log.warning('regenerate_pdf failed for %s: %s', audit_id, e)
        return None


# ---------------------------------------------------------------------------
# AnswerMonk sync — push a persisted audit to the visibility-report ingest
# endpoint (POST {ANSWERMONK_BASE_URL}/api/external-audits). The receiver
# zod-validates against externalAuditIngestSchema and upserts by audit_id,
# so re-posting the same audit is always safe.
# ---------------------------------------------------------------------------

ANSWERMONK_POST_ATTEMPTS = 2
ANSWERMONK_RETRY_DELAY_S = 1.0
ANSWERMONK_MAX_FINDINGS = 500  # ingest schema caps findings at 500


def build_answermonk_payload(audit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map an audit dict onto the externalAuditIngestSchema shape.
    Returns None when a required field (audit_id / url / domain) is missing —
    the receiver would reject it with a 400 anyway."""
    audit_id = audit.get('audit_id')
    url = audit.get('url')
    domain = audit.get('domain')
    if not (audit_id and url and domain):
        return None
    payload: Dict[str, Any] = {
        'audit_id': str(audit_id)[:200],
        'url': str(url)[:2000],
        'domain': str(domain)[:255],
    }
    brand = (audit.get('classification') or {}).get('company_name')
    if brand:
        payload['brand_name'] = str(brand)[:255]
    if audit.get('date'):
        payload['date'] = str(audit['date'])[:40]
    findings = audit.get('findings')
    if isinstance(findings, list):
        rows = [f for f in findings if isinstance(f, dict)]
        if rows:
            payload['findings'] = rows[:ANSWERMONK_MAX_FINDINGS]
    for key in ('scoring', 'narrative', 'performance', 'bots_eye_view', 'gates'):
        val = audit.get(key)
        if isinstance(val, dict) and val:
            payload[key] = val
    # Profile/target tagging (LIGHT profile + competitor audits) rides in the
    # ingest schema's optional `metadata` record. STRICTLY conditional: a
    # default full-profile audit carries none of these keys, so its payload is
    # byte-identical to before — backward compatible by construction.
    md = audit.get('metadata') if isinstance(audit.get('metadata'), dict) else {}
    tags = {}
    for k in ('profile', 'target', 'session_ref'):
        v = audit.get(k) or md.get(k)
        if v:
            tags[k] = str(v)[:200]
    if tags:
        payload['metadata'] = tags
    return payload


def post_to_answermonk(audit: Dict[str, Any]) -> Dict[str, Any]:
    """POST a persisted audit to AnswerMonk. Best-effort: logs failures,
    retries once on network errors / 5xx (not on 4xx — those won't change),
    and never raises into the audit path. No-op unless both
    ANSWERMONK_BASE_URL and EXTERNAL_AUDITOR_KEY are set."""
    base = (os.getenv('ANSWERMONK_BASE_URL') or '').rstrip('/')
    key = os.getenv('EXTERNAL_AUDITOR_KEY')
    if not (base and key):
        return {'posted': False,
                'note': 'ANSWERMONK_BASE_URL / EXTERNAL_AUDITOR_KEY not set'}
    payload = build_answermonk_payload(audit)
    if payload is None:
        log.warning('answermonk sync skipped: audit missing audit_id/url/domain')
        return {'posted': False, 'error': 'audit missing audit_id/url/domain'}
    endpoint = f'{base}/api/external-audits'
    last_err = None
    try:
        import httpx
        for attempt in range(1, ANSWERMONK_POST_ATTEMPTS + 1):
            try:
                with httpx.Client(timeout=15.0) as client:
                    r = client.post(endpoint, json=payload,
                                    headers={'x-auditor-key': key})
                if 200 <= r.status_code < 300:
                    log.info('answermonk sync ok: audit %s -> %s',
                             payload['audit_id'], endpoint)
                    return {'posted': True, 'status': r.status_code,
                            'attempts': attempt}
                last_err = f'status {r.status_code}: {r.text[:300]}'
                if 400 <= r.status_code < 500:
                    break
            except Exception as e:
                last_err = f'{type(e).__name__}: {e}'
            if attempt < ANSWERMONK_POST_ATTEMPTS:
                time.sleep(ANSWERMONK_RETRY_DELAY_S)
    except Exception as e:
        last_err = f'{type(e).__name__}: {e}'
    log.warning('answermonk sync failed for %s: %s',
                payload.get('audit_id'), last_err)
    return {'posted': False, 'error': last_err}


# ---------------------------------------------------------------------------
# Durable job status — write-through to Supabase `audit_jobs`
# Table (create via migration): audit_id text primary key, url text,
#   status text, error text, submitted_at timestamptz, completed_at timestamptz,
#   result_summary jsonb.
# ---------------------------------------------------------------------------

def _pg():
    """Return the Postgres backend if DATABASE_URL is configured, else None."""
    try:
        import db
        if db.pg_enabled():
            return db
    except Exception:
        pass
    return None


def save_job_status(job: Dict[str, Any]) -> bool:
    """Upsert a job's status. Postgres if configured, else Supabase. Best-effort."""
    pg = _pg()
    if pg:
        return pg.save_job_status(job)
    try:
        from tools import _supabase_base_headers
        base, headers = _supabase_base_headers()
        if base is None:
            return False
        import httpx
        row = {
            'audit_id': job.get('audit_id'),
            'url': job.get('url'),
            'status': job.get('status'),
            'error': job.get('error'),
            'submitted_at': job.get('submitted_at'),
            'completed_at': job.get('completed_at'),
            'result_summary': job.get('result_summary'),
        }
        if not row['audit_id']:
            return False
        h = dict(headers)
        h['Prefer'] = 'resolution=merge-duplicates'
        with httpx.Client(timeout=10.0) as client:
            r = client.post(f'{base}/rest/v1/audit_jobs', headers=h, json=row)
            return r.status_code in (200, 201, 204)
    except Exception as e:
        log.debug('save_job_status best-effort miss: %s', e)
        return False


def persist_suppression(domain: str) -> bool:
    """Durably record a suppressed (taken-down) domain in Supabase so it survives
    a redeploy (in-memory suppression alone was lost on restart — ENG-7). Table
    `suppressed_domains` (domain text primary key, created_at timestamptz)."""
    pg = _pg()
    if pg:
        return pg.persist_suppression(domain)
    try:
        from tools import _supabase_base_headers
        base, headers = _supabase_base_headers()
        if base is None or not domain:
            return False
        import httpx
        h = dict(headers)
        h['Prefer'] = 'resolution=merge-duplicates'
        with httpx.Client(timeout=10.0) as client:
            r = client.post(f'{base}/rest/v1/suppressed_domains',
                            headers=h, json={'domain': domain})
            return r.status_code in (200, 201, 204)
    except Exception as e:
        log.debug('persist_suppression best-effort miss: %s', e)
        return False


def load_suppressions() -> list:
    """Load durable suppressed domains at startup. Returns [] if unavailable."""
    pg = _pg()
    if pg:
        return pg.load_suppressions()
    try:
        from tools import _supabase_base_headers
        base, headers = _supabase_base_headers()
        if base is None:
            return []
        import httpx
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f'{base}/rest/v1/suppressed_domains',
                           headers=headers, params={'select': 'domain', 'limit': '1000'})
            if r.status_code == 200:
                return [row['domain'] for row in r.json() if row.get('domain')]
    except Exception as e:
        log.debug('load_suppressions best-effort miss: %s', e)
    return []


def remove_suppression(domain: str) -> bool:
    """Delete a durable suppression (un-suppress)."""
    pg = _pg()
    if pg:
        return pg.remove_suppression(domain)
    try:
        from tools import _supabase_base_headers
        base, headers = _supabase_base_headers()
        if base is None or not domain:
            return False
        import httpx
        with httpx.Client(timeout=10.0) as client:
            r = client.delete(f'{base}/rest/v1/suppressed_domains',
                              headers=headers, params={'domain': f'eq.{domain}'})
            return r.status_code in (200, 204)
    except Exception as e:
        log.debug('remove_suppression best-effort miss: %s', e)
        return False


def load_job_status(audit_id: str) -> Optional[Dict[str, Any]]:
    """Load a job's status (Postgres if configured, else Supabase). Used when
    it's gone from memory after a redeploy. None if not found / not configured."""
    pg = _pg()
    if pg:
        return pg.load_job_status(audit_id)
    try:
        from tools import _supabase_base_headers
        base, headers = _supabase_base_headers()
        if base is None:
            return None
        import httpx
        with httpx.Client(timeout=10.0) as client:
            r = client.get(f'{base}/rest/v1/audit_jobs', headers=headers,
                           params={'audit_id': f'eq.{audit_id}', 'limit': '1'})
            if r.status_code == 200 and r.json():
                return r.json()[0]
    except Exception as e:
        log.debug('load_job_status best-effort miss: %s', e)
    return None
