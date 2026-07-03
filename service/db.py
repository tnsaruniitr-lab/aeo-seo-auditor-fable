"""
db.py — Postgres storage backend (Railway Postgres).

A drop-in alternative to the Supabase REST persistence. When DATABASE_URL is
set (Railway provisions this when you attach a Postgres service), the storage
functions in tools.py / persistence.py / billing.py delegate here instead of
hitting Supabase. Same inputs, same return shapes — so nothing else changes.

Design:
    - Sync psycopg2 (matches the synchronous codebase). One short-lived
      connection per operation — simple and safe for this traffic level.
    - JSONB columns named exactly like the Supabase schema, so fetch_audit's
      reassembly is identical.
    - init_schema() is idempotent (CREATE TABLE IF NOT EXISTS) and called once
      at startup. Postgres 13+ has gen_random_uuid() built in.

Never raises into the request path — every public function catches and returns
a safe default, mirroring the Supabase code's best-effort contract.
"""

from __future__ import annotations

import logging
import os
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

log = logging.getLogger('audit.db')

_SCHEMA_READY = False


def pg_enabled() -> bool:
    """True when a Postgres DATABASE_URL is configured."""
    return bool(os.getenv('DATABASE_URL'))


def _connect():
    """Open a psycopg2 connection to DATABASE_URL. Returns None if unavailable."""
    url = os.getenv('DATABASE_URL')
    if not url:
        return None
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        log.warning('psycopg2 not installed — Postgres backend unavailable')
        return None
    try:
        import psycopg2
        conn = psycopg2.connect(url, connect_timeout=10)
        conn.autocommit = True
        return conn
    except Exception as e:
        log.warning('pg connect failed: %s', e)
        return None


def _jsonb(v):
    """Wrap a Python value for a JSONB column (psycopg2 Json adapter)."""
    from psycopg2.extras import Json
    return Json(v) if v is not None else None


def _clean(v):
    """Normalize DB values for JSON output — Decimal → float."""
    if isinstance(v, Decimal):
        return float(v)
    return v


# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS website_audits (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    audit_id text UNIQUE NOT NULL,
    url text, domain text, audit_date text,
    page_type text, industry text, company_name text, confidence text,
    competitors jsonb, test_queries jsonb, gates jsonb,
    section_scores jsonb,
    page_citation_readiness numeric, brand_ai_presence numeric,
    seo_score numeric, aeo_score numeric, citation_readiness numeric,
    overall_score numeric, overall_grade text,
    narrative jsonb, competitor_comparison jsonb, bots_eye_view jsonb,
    performance jsonb, supplementary_findings jsonb, metadata jsonb,
    findings_count int, duration_seconds numeric, audit_mode text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS website_audits_domain_idx ON website_audits (domain);
CREATE INDEX IF NOT EXISTS website_audits_created_idx ON website_audits (created_at DESC);

CREATE TABLE IF NOT EXISTS website_audit_findings (
    id bigserial PRIMARY KEY,
    audit_id text NOT NULL,
    check_id text, section text, status text, severity text,
    evidence text, truth_badge text, fix_type text, citations jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS findings_audit_idx ON website_audit_findings (audit_id);

CREATE TABLE IF NOT EXISTS audit_jobs (
    audit_id text PRIMARY KEY,
    url text, status text, error text,
    submitted_at timestamptz, completed_at timestamptz,
    result_summary jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_usage (
    key_id text NOT NULL,
    month text NOT NULL,
    count int NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (key_id, month)
);

CREATE TABLE IF NOT EXISTS suppressed_domains (
    domain text PRIMARY KEY,
    reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


def init_schema() -> bool:
    """Create tables if missing. Idempotent. Called once at startup."""
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return True
    conn = _connect()
    if conn is None:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA_SQL)
        _SCHEMA_READY = True
        log.info('Postgres schema ready')
        return True
    except Exception as e:
        log.error('init_schema failed: %s', e)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AUDITS
# ---------------------------------------------------------------------------

def persist_audit(audit: Dict[str, Any]) -> Dict[str, Any]:
    audit_id = audit.get('audit_id')
    conn = _connect()
    if conn is None:
        return {"persisted": False, "audit_id": audit_id, "findings_persisted": 0,
                "note": "DATABASE_URL not set"}
    classification = audit.get('classification', {}) or {}
    context = audit.get('context', {}) or {}
    scoring = audit.get('scoring', {}) or {}
    findings = audit.get('findings', []) or []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO website_audits (
                    audit_id, url, domain, audit_date, page_type, industry,
                    company_name, confidence, competitors, test_queries, gates,
                    section_scores, page_citation_readiness, brand_ai_presence,
                    seo_score, aeo_score, citation_readiness, overall_score,
                    overall_grade, narrative, competitor_comparison, bots_eye_view,
                    performance, supplementary_findings, metadata, findings_count,
                    duration_seconds, audit_mode
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (audit_id) DO UPDATE SET
                    url=EXCLUDED.url, domain=EXCLUDED.domain, audit_date=EXCLUDED.audit_date,
                    page_type=EXCLUDED.page_type, industry=EXCLUDED.industry,
                    company_name=EXCLUDED.company_name, confidence=EXCLUDED.confidence,
                    competitors=EXCLUDED.competitors, test_queries=EXCLUDED.test_queries,
                    gates=EXCLUDED.gates, section_scores=EXCLUDED.section_scores,
                    page_citation_readiness=EXCLUDED.page_citation_readiness,
                    brand_ai_presence=EXCLUDED.brand_ai_presence, seo_score=EXCLUDED.seo_score,
                    aeo_score=EXCLUDED.aeo_score, citation_readiness=EXCLUDED.citation_readiness,
                    overall_score=EXCLUDED.overall_score, overall_grade=EXCLUDED.overall_grade,
                    narrative=EXCLUDED.narrative, competitor_comparison=EXCLUDED.competitor_comparison,
                    bots_eye_view=EXCLUDED.bots_eye_view, performance=EXCLUDED.performance,
                    supplementary_findings=EXCLUDED.supplementary_findings, metadata=EXCLUDED.metadata,
                    findings_count=EXCLUDED.findings_count, duration_seconds=EXCLUDED.duration_seconds,
                    audit_mode=EXCLUDED.audit_mode
                RETURNING id
            """, (
                audit_id, audit.get('url'), audit.get('domain'), audit.get('date'),
                classification.get('page_type'), classification.get('industry'),
                classification.get('company_name'), classification.get('confidence'),
                _jsonb(context.get('competitors')), _jsonb(context.get('test_queries')),
                _jsonb(audit.get('gates')), _jsonb(scoring.get('section_scores')),
                scoring.get('page_citation_readiness'), scoring.get('brand_ai_presence'),
                scoring.get('seo_score'), scoring.get('aeo_score'),
                scoring.get('citation_readiness'), scoring.get('overall_score'),
                scoring.get('overall_grade'), _jsonb(audit.get('narrative')),
                _jsonb(audit.get('competitor_comparison')), _jsonb(audit.get('bots_eye_view')),
                _jsonb(audit.get('performance')), _jsonb(audit.get('supplementary_findings')),
                _jsonb(audit.get('metadata')), len(findings),
                audit.get('duration_seconds'),
                (audit.get('metadata', {}) or {}).get('version', 'agent'),
            ))
            row_id = cur.fetchone()[0]

            # Replace findings for this audit_id (transactional-ish via autocommit
            # single connection: delete then insert).
            cur.execute("DELETE FROM website_audit_findings WHERE audit_id=%s", (audit_id,))
            fp = 0
            for f in findings:
                cur.execute("""
                    INSERT INTO website_audit_findings
                        (audit_id, check_id, section, status, severity, evidence,
                         truth_badge, fix_type, citations)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (
                    audit_id, f.get('check_id'), f.get('section'), f.get('status'),
                    f.get('severity'), (f.get('evidence') or '')[:4000],
                    f.get('truth_badge'), f.get('fix_type'), _jsonb(f.get('citations')),
                ))
                fp += 1
        return {"persisted": True, "supabase_row_id": str(row_id),
                "audit_id": audit_id, "findings_persisted": fp, "backend": "postgres"}
    except Exception as e:
        return {"persisted": False, "error": f"{type(e).__name__}: {e}",
                "audit_id": audit_id, "findings_persisted": 0}
    finally:
        conn.close()


def _reassemble(row: Dict[str, Any], findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "audit_id": row.get("audit_id"), "url": row.get("url"),
        "domain": row.get("domain"), "date": row.get("audit_date"),
        "duration_seconds": _clean(row.get("duration_seconds")),
        "classification": {
            "page_type": row.get("page_type"), "industry": row.get("industry"),
            "company_name": row.get("company_name"), "confidence": row.get("confidence"),
        },
        "context": {"competitors": row.get("competitors"),
                    "test_queries": row.get("test_queries")},
        "gates": row.get("gates"),
        "scoring": {
            "section_scores": row.get("section_scores"),
            "page_citation_readiness": _clean(row.get("page_citation_readiness")),
            "brand_ai_presence": _clean(row.get("brand_ai_presence")),
            "seo_score": _clean(row.get("seo_score")),
            "aeo_score": _clean(row.get("aeo_score")),
            "citation_readiness": _clean(row.get("citation_readiness")),
            "overall_score": _clean(row.get("overall_score")),
            "overall_grade": row.get("overall_grade"),
        },
        "narrative": row.get("narrative"),
        "competitor_comparison": row.get("competitor_comparison"),
        "bots_eye_view": row.get("bots_eye_view"),
        "performance": row.get("performance"),
        "supplementary_findings": row.get("supplementary_findings"),
        "metadata": row.get("metadata"),
        "findings_count": row.get("findings_count"),
        "findings": [
            {"check_id": f.get("check_id"), "section": f.get("section"),
             "status": f.get("status"), "severity": f.get("severity"),
             "evidence": f.get("evidence"), "truth_badge": f.get("truth_badge"),
             "fix_type": f.get("fix_type"), "citations": f.get("citations")}
            for f in findings
        ],
        "loaded_from": "postgres",
        "created_at": str(row.get("created_at")) if row.get("created_at") else None,
    }


def fetch_audit(domain: Optional[str] = None,
                audit_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return None
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if audit_id:
                cur.execute("SELECT * FROM website_audits WHERE audit_id=%s LIMIT 1", (audit_id,))
            elif domain:
                cur.execute("SELECT * FROM website_audits WHERE domain=%s "
                            "ORDER BY created_at DESC LIMIT 1", (domain,))
            else:
                return None
            row = cur.fetchone()
            if not row:
                return None
            cur.execute("SELECT * FROM website_audit_findings WHERE audit_id=%s ORDER BY id ASC",
                        (row['audit_id'],))
            findings = cur.fetchall()
        return _reassemble(dict(row), [dict(f) for f in findings])
    except Exception as e:
        log.warning('fetch_audit failed: %s', e)
        return None
    finally:
        conn.close()


def list_audits_for_domain(domain: str, limit: int = 10) -> list:
    conn = _connect()
    if conn is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT audit_id, overall_score, overall_grade, audit_date,
                       created_at, findings_count
                FROM website_audits WHERE domain=%s ORDER BY created_at DESC LIMIT %s
            """, (domain, limit))
            return [_row_clean(dict(r)) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def list_all_audits(limit: int = 60) -> list:
    conn = _connect()
    if conn is None:
        return []
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT audit_id, domain, url, page_type, industry, overall_score,
                       overall_grade, findings_count, audit_date, created_at
                FROM website_audits ORDER BY created_at DESC LIMIT %s
            """, (limit,))
            return [_row_clean(dict(r)) for r in cur.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _row_clean(r: Dict[str, Any]) -> Dict[str, Any]:
    out = {k: _clean(v) for k, v in r.items()}
    if out.get('created_at') is not None:
        out['created_at'] = str(out['created_at'])
    return out


def _domain_variants(domain: str) -> set:
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).strip("/").split("/")[0]
    bare = d[4:] if d.startswith("www.") else d
    return {v for v in (bare, "www." + bare) if v and "." in v}


def delete_audits(domain: Optional[str] = None,
                  audit_id: Optional[str] = None) -> Dict[str, Any]:
    conn = _connect()
    if conn is None:
        return {"deleted": False, "error": "DATABASE_URL not set"}
    try:
        with conn.cursor() as cur:
            if audit_id:
                target_ids = [audit_id]
            elif domain:
                variants = tuple(sorted(_domain_variants(domain)))
                if not variants:
                    return {"deleted": False, "error": f"invalid domain '{domain}'"}
                cur.execute("SELECT audit_id FROM website_audits WHERE domain = ANY(%s)",
                            (list(variants),))
                target_ids = [r[0] for r in cur.fetchall()]
            else:
                return {"deleted": False, "error": "provide domain or audit_id"}
            if not target_ids:
                return {"deleted": True, "audit_ids": [], "audits_deleted": 0,
                        "findings_deleted": 0, "note": "no matching audits"}
            cur.execute("DELETE FROM website_audit_findings WHERE audit_id = ANY(%s)", (target_ids,))
            fdel = cur.rowcount
            cur.execute("DELETE FROM website_audits WHERE audit_id = ANY(%s)", (target_ids,))
            adel = cur.rowcount
        return {"deleted": True, "audit_ids": target_ids,
                "audits_deleted": adel, "findings_deleted": fdel, "backend": "postgres"}
    except Exception as e:
        return {"deleted": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# JOBS / USAGE / SUPPRESSION
# ---------------------------------------------------------------------------

def save_job_status(job: Dict[str, Any]) -> bool:
    conn = _connect()
    if conn is None or not job.get('audit_id'):
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO audit_jobs (audit_id, url, status, error, submitted_at,
                                        completed_at, result_summary, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (audit_id) DO UPDATE SET
                    url=EXCLUDED.url, status=EXCLUDED.status, error=EXCLUDED.error,
                    submitted_at=EXCLUDED.submitted_at, completed_at=EXCLUDED.completed_at,
                    result_summary=EXCLUDED.result_summary, updated_at=now()
            """, (job.get('audit_id'), job.get('url'), job.get('status'), job.get('error'),
                  job.get('submitted_at'), job.get('completed_at'),
                  _jsonb(job.get('result_summary'))))
        return True
    except Exception as e:
        log.debug('save_job_status pg miss: %s', e)
        return False
    finally:
        conn.close()


def load_job_status(audit_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect()
    if conn is None:
        return None
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM audit_jobs WHERE audit_id=%s LIMIT 1", (audit_id,))
            r = cur.fetchone()
            return _row_clean(dict(r)) if r else None
    except Exception:
        return None
    finally:
        conn.close()


def get_usage(key_id: str, month: str) -> int:
    conn = _connect()
    if conn is None:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT count FROM api_usage WHERE key_id=%s AND month=%s", (key_id, month))
            r = cur.fetchone()
            return int(r[0]) if r else 0
    except Exception:
        return 0
    finally:
        conn.close()


def increment_usage(key_id: str, month: str) -> None:
    conn = _connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO api_usage (key_id, month, count, updated_at)
                VALUES (%s,%s,1, now())
                ON CONFLICT (key_id, month) DO UPDATE SET
                    count = api_usage.count + 1, updated_at = now()
            """, (key_id, month))
    except Exception as e:
        log.debug('increment_usage pg miss: %s', e)
    finally:
        conn.close()


def persist_suppression(domain: str) -> bool:
    conn = _connect()
    if conn is None or not domain:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO suppressed_domains (domain) VALUES (%s) "
                        "ON CONFLICT (domain) DO NOTHING", (domain,))
        return True
    except Exception:
        return False
    finally:
        conn.close()


def load_suppressions() -> list:
    conn = _connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT domain FROM suppressed_domains LIMIT 1000")
            return [r[0] for r in cur.fetchall() if r[0]]
    except Exception:
        return []
    finally:
        conn.close()


def remove_suppression(domain: str) -> bool:
    conn = _connect()
    if conn is None or not domain:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM suppressed_domains WHERE domain=%s", (domain,))
        return True
    except Exception:
        return False
    finally:
        conn.close()
