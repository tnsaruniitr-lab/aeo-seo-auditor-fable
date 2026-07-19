"""
main.py — FastAPI service exposing the standalone auditor.

Three endpoints:
    POST /audit          — kick off an audit (async, returns audit_id)
    GET  /audit/{id}     — fetch status + result (poll until completed)
    GET  /healthz        — service health check

Plus convenience routes:
    GET /audit/{id}.json — raw JSON
    GET /audit/{id}.md   — Markdown report
    GET /audit/{id}.pdf  — PDF summary
    GET /audit/{id}.html — preview-friendly HTML render

USAGE
    pip install fastapi uvicorn anthropic python-dotenv
    export ANTHROPIC_API_KEY="sk-ant-..."
    uvicorn main:app --host 0.0.0.0 --port 8000

ENVIRONMENT VARIABLES
    ANTHROPIC_API_KEY  — required for audit narrative generation
    AUDIT_OUTPUT_DIR   — where to write audit artifacts (default: ./audits/)
    PORT               — port to bind (default: 8000)
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# ----------------------------------------------------------------------
# LOGGING — configured early so all modules picking up loggers inherit it
# ----------------------------------------------------------------------

LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO').upper()

logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    datefmt='%H:%M:%S',
    stream=sys.stdout,
    force=True,
)
log = logging.getLogger('audit')

# Quiet down noisy third-party loggers unless we asked for DEBUG
if LOG_LEVEL != 'DEBUG':
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('anthropic').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)

# Load .env if present (graceful fallback if python-dotenv not installed)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import secrets

from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends, status, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, ConfigDict, HttpUrl, Field
from typing import List as _List  # avoid clash with the Optional/Dict already imported

from audit_pipeline import run_audit as run_audit_deterministic
from safety import check_url_safe
from site_context import sanitize_site_context

# Agent-mode (full parity with the chat skill) is opt-in via AUDIT_MODE env var.
# Default is "agent" once the parity layer is deployed; falls back automatically
# if dependencies (Playwright, Tavily key, etc.) aren't ready.
AUDIT_MODE = os.getenv('AUDIT_MODE', 'agent').lower().strip()

try:
    from agent import run_audit_agent
    AGENT_AVAILABLE = True
except Exception as _agent_import_err:
    AGENT_AVAILABLE = False
    _AGENT_IMPORT_ERROR = str(_agent_import_err)

# Supabase read path — for reloading persisted audits by domain or id.
try:
    from tools import (fetch_audit, list_audits_for_domain, list_all_audits,
                       delete_audits)
except Exception:
    def fetch_audit(*_a, **_k):  # type: ignore
        return None
    def list_audits_for_domain(*_a, **_k):  # type: ignore
        return []
    def list_all_audits(*_a, **_k):  # type: ignore
        return []
    def delete_audits(*_a, **_k):  # type: ignore
        return {"deleted": False, "error": "tools.delete_audits unavailable"}

# Durability, product-loop, metering, and metrics modules. Guarded so a missing
# optional module never blocks service boot.
try:
    from persistence import (regenerate_markdown, regenerate_pdf, save_job_status,
                             persist_suppression, load_suppressions, remove_suppression)
except Exception:
    def regenerate_markdown(*_a, **_k):  # type: ignore
        return None
    def regenerate_pdf(*_a, **_k):  # type: ignore
        return None
    def save_job_status(*_a, **_k):  # type: ignore
        return False
    def persist_suppression(*_a, **_k):  # type: ignore
        return False
    def load_suppressions(*_a, **_k):  # type: ignore
        return []
    def remove_suppression(*_a, **_k):  # type: ignore
        return False
try:
    from delta import delta_against_prior
except Exception:
    def delta_against_prior(*_a, **_k):  # type: ignore
        return None
try:
    import monitoring
except Exception:
    monitoring = None  # type: ignore
try:
    import billing
except Exception:
    billing = None  # type: ignore


def run_audit(url: str, output_dir: str, progress_callback=None,
              site_context: Optional[Dict[str, Any]] = None,
              skip_visibility: bool = False):
    """Dispatch to the chosen audit pipeline.

    Modes:
        - 'agent'         : full 15-phase parity loop (matches chat skill)
        - 'deterministic' : legacy fast path (scripts + 1 Sonnet call)
        - 'auto'          : agent if available, else deterministic

    progress_callback (optional): called with a dict {phase, tool, turn,
    tool_count, elapsed_seconds, last_tool_ms} after each tool call when
    running in agent mode. Ignored by the deterministic path.

    site_context (optional): sanitized site-wide crawl signals for the audited
    page (see site_context.py). Threaded into the agent prompt as measured,
    narrative-only CONTEXT; never touches scoring.
    """
    mode = AUDIT_MODE
    if mode == 'agent' and not AGENT_AVAILABLE:
        # Hard-fail if the user explicitly asked for agent and it's broken
        raise RuntimeError(
            f"AUDIT_MODE=agent requested but agent module failed to import: "
            f"{_AGENT_IMPORT_ERROR}"
        )
    if mode == 'auto':
        mode = 'agent' if AGENT_AVAILABLE else 'deterministic'
    if mode == 'agent':
        return run_audit_agent(url, output_dir=output_dir, verbose=False,
                                progress_callback=progress_callback,
                                site_context=site_context,
                                skip_visibility=skip_visibility)
    # Deterministic path has no narrative to enrich — site_context is ignored
    # (and it runs no visibility sweep, so skip_visibility is moot).
    return run_audit_deterministic(url, output_dir=output_dir)


# ----------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------

OUTPUT_DIR = Path(os.getenv('AUDIT_OUTPUT_DIR', './audits/')).expanduser().resolve()
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# In-memory job tracker (sufficient for single-instance v1).
# For multi-instance scale, swap for Redis or DB.
JOBS: Dict[str, Dict] = {}
JOBS_LOCK = threading.Lock()

# Resource bounds. Audits spawn chromium + run the agent loop; without a cap,
# concurrent submissions exhaust memory/CPU. We bound queued+running audits
# and evict old finished jobs so JOBS can't grow without limit.
MAX_CONCURRENT_AUDITS = int(os.getenv('MAX_CONCURRENT_AUDITS', '3'))
MAX_TRACKED_JOBS = int(os.getenv('MAX_TRACKED_JOBS', '500'))
# A running audit older than this is considered hung and reaped to 'error'
# so it can't hold a slot / sit in 'running' forever.
MAX_AUDIT_SECONDS = int(os.getenv('MAX_AUDIT_SECONDS', '1200'))

# Fail-closed switch: in production a missing auth env var must NOT silently
# expose the expensive/mutating endpoints. Detected via Railway's injected
# vars, or forced with AUDITOR_FAIL_CLOSED=1. Local dev (no signal) stays open.
IS_PRODUCTION = bool(
    os.getenv('RAILWAY_GIT_COMMIT_SHA') or os.getenv('RAILWAY_ENVIRONMENT')
    or os.getenv('AUDITOR_FAIL_CLOSED') == '1'
)


# Suppression denylist — domains that must not be audited or re-published
# (e.g. a brand that has formally objected / issued a takedown). Seeded from
# the SUPPRESSED_DOMAINS env var (comma-separated, durable across redeploys);
# a by-domain delete with suppress=1 also adds to it for the current process.
def _registrable(domain: str) -> str:
    """Normalize to a bare host: strip scheme, leading www., trailing slash."""
    d = (domain or '').strip().lower()
    d = re.sub(r'^https?://', '', d).strip('/').split('/')[0]
    return d[4:] if d.startswith('www.') else d


SUPPRESSED_DOMAINS = {
    _registrable(d) for d in os.getenv('SUPPRESSED_DOMAINS', '').split(',')
    if d.strip()
}
SUPPRESS_LOCK = threading.Lock()

# Merge durable suppressions from Supabase so takedowns survive redeploys, not
# only the ones seeded via the env var (ENG-7). Best-effort at boot.
try:
    for _d in load_suppressions():
        SUPPRESSED_DOMAINS.add(_registrable(_d))
except Exception:
    pass


def _is_suppressed(url_or_domain: str) -> bool:
    reg = _registrable(url_or_domain)
    with SUPPRESS_LOCK:
        return reg in SUPPRESSED_DOMAINS


def _purge_local_artifacts(domain: Optional[str] = None,
                           audit_id: Optional[str] = None) -> int:
    """Delete on-disk audit artifacts (.json/.md/.pdf/.html) for a domain or
    audit_id. Files are named '{slug}-{audit_id[:8]}.*' in OUTPUT_DIR."""
    removed = 0
    try:
        if audit_id:
            pattern = f'*-{audit_id[:8]}.*'
        elif domain:
            pattern = f'{_registrable(domain).replace(".", "-")}-*.*'
        else:
            return 0
        for p in OUTPUT_DIR.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    except Exception:
        pass
    return removed


def _purge_jobs(domain: Optional[str] = None,
                audit_id: Optional[str] = None) -> int:
    """Drop in-memory JOBS entries matching a domain or audit_id."""
    removed = 0
    with JOBS_LOCK:
        if audit_id:
            if JOBS.pop(audit_id, None) is not None:
                removed += 1
        elif domain:
            reg = _registrable(domain)
            for aid in [a for a, j in JOBS.items()
                        if _registrable(j.get('url', '')) == reg]:
                JOBS.pop(aid, None)
                removed += 1
    return removed


def _active_audit_count() -> int:
    """Count queued+running jobs. Caller must hold JOBS_LOCK."""
    return sum(1 for j in JOBS.values()
               if j.get('status') in ('queued', 'running'))


def _reap_and_evict_locked() -> None:
    """Reap hung 'running' jobs and evict old finished ones. Holds JOBS_LOCK."""
    now = time.time()
    # Reap hung audits. Note MAX_AUDIT_SECONDS (default 1200) is deliberately >
    # the agent loop's own TOTAL_BUDGET_SECONDS (900): the audit thread breaks
    # itself at 900s, so by the time the reaper fires the worker has already
    # exited on its own. The reaper is the backstop for the rare case where a
    # tool subprocess wedged past its own timeout. We record it so a spike in
    # reaps is visible rather than silent (ENG-6/SD-5).
    for aid, j in JOBS.items():
        if j.get('status') == 'running':
            sub = j.get('_submitted_at') or 0
            if sub and now - sub > MAX_AUDIT_SECONDS:
                j['status'] = 'error'
                j['error'] = f'audit exceeded {MAX_AUDIT_SECONDS}s wall-clock; reaped'
                j['completed_at'] = datetime.now(timezone.utc).isoformat()
                if monitoring:
                    monitoring.audit_reaped(aid, now - sub)
    # Evict oldest finished jobs over the cap
    if len(JOBS) > MAX_TRACKED_JOBS:
        finished = [(j.get('_submitted_at') or 0, aid)
                    for aid, j in JOBS.items()
                    if j.get('status') in ('completed', 'error')]
        finished.sort()
        for _, aid in finished[:len(JOBS) - MAX_TRACKED_JOBS]:
            JOBS.pop(aid, None)


# ----------------------------------------------------------------------
# REQUEST / RESPONSE MODELS
# ----------------------------------------------------------------------

class AuditRequest(BaseModel):
    url: HttpUrl


class AuditResponse(BaseModel):
    audit_id: str
    status: str  # 'queued' | 'running' | 'completed' | 'error'
    message: str
    poll_url: str


class AuditStatusResponse(BaseModel):
    audit_id: str
    status: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    duration_seconds: Optional[float] = None
    error: Optional[str] = None
    result_summary: Optional[dict] = None
    artifacts: Optional[dict] = None
    # Live progress (populated while status == 'running'):
    # {phase, tool, turn, tool_count, elapsed_seconds, last_tool_ms}
    progress: Optional[dict] = None
    # Agent diagnostic fields — populated when status == 'error' and the
    # failure originated in the agent loop (vs an exception at orchestration).
    agent_errors: Optional[list] = None
    raw_final_text_preview: Optional[str] = None
    agent_turns: Optional[int] = None
    tool_call_count: Optional[int] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


# ----------------------------------------------------------------------
# FASTAPI APP
# ----------------------------------------------------------------------

# Interactive API docs (/docs, /redoc, /openapi.json) enumerate every route and
# schema — useful in dev, needless attack-surface in prod. Disabled when a
# production signal is present.
app = FastAPI(
    title='AEO/SEO/GEO Auditor',
    description='Deterministic website audit service. '
                'Sieve-brain-backed + Anthropic Sonnet, with runtime-authoritative scoring.',
    version='5.0',
    docs_url=None if IS_PRODUCTION else '/docs',
    redoc_url=None if IS_PRODUCTION else '/redoc',
    openapi_url=None if IS_PRODUCTION else '/openapi.json',
)


@app.on_event('startup')
def _init_storage():
    """Create the Postgres schema on boot when DATABASE_URL is set (Railway
    Postgres). No-op when using Supabase or no DB. Never blocks startup."""
    try:
        import db
        if db.pg_enabled():
            ok = db.init_schema()
            log.info('storage backend = postgres (schema_ready=%s)', ok)
        else:
            log.info('storage backend = %s',
                     'supabase' if os.getenv('SUPABASE_URL') else 'none (in-memory only)')
    except Exception as e:
        log.warning('storage init skipped: %s', e)


# ----------------------------------------------------------------------
# AUTH — HTTP Basic, credentials from env vars (never in code)
# ----------------------------------------------------------------------
# Set in Railway: AUDIT_USERNAME and AUDIT_PASSWORD env vars.
# If either is unset, AUTH IS DISABLED (service is fully public).
# Always set both in production.

AUDIT_USERNAME = os.getenv('AUDIT_USERNAME', '')
AUDIT_PASSWORD = os.getenv('AUDIT_PASSWORD', '')
AUTH_ENABLED = bool(AUDIT_USERNAME and AUDIT_PASSWORD)

if not AUTH_ENABLED:
    log.warning('AUTH DISABLED — AUDIT_USERNAME / AUDIT_PASSWORD env vars not set. '
                'Service is publicly accessible. Set both in Railway to enable auth.')
else:
    log.info('AUTH enabled for user=%s', AUDIT_USERNAME)

# API key auth — for server-to-server integrations (AnswerMonk, etc.)
# Set in Railway: AUDIT_API_KEY env var. Independent of Basic Auth above.
AUDIT_API_KEY = os.getenv('AUDIT_API_KEY', '')
API_KEY_ENABLED = bool(AUDIT_API_KEY)

if not API_KEY_ENABLED:
    log.warning('API KEY AUTH DISABLED — AUDIT_API_KEY env var not set. '
                'Programmatic endpoints /api/audit/* are unauthenticated.')


def require_api_key(request: Request):
    """Verify X-API-Key header against env-var-defined AUDIT_API_KEY.

    If API_KEY_ENABLED is False (env var unset): fail CLOSED in production
    (refuse rather than silently expose a paid, LLM-spending endpoint), but
    stay open in local dev where no production signal is present.
    Constant-time comparison.
    """
    if not API_KEY_ENABLED:
        if IS_PRODUCTION:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='API key auth not configured. Set AUDIT_API_KEY '
                       '(fail-closed in production).',
            )
        return True
    key = request.headers.get('X-API-Key', '')
    if not key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Missing X-API-Key header',
        )
    if not secrets.compare_digest(key, AUDIT_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid API key',
        )
    return True


_basic = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_basic)):
    """Verify HTTP Basic credentials against env-var-defined username/password.

    If AUTH_ENABLED is False (env vars unset): fail CLOSED in production
    (refuse rather than silently expose internal/enumeration/expensive routes),
    but stay open in local dev where no production signal is present.
    Uses constant-time comparison to prevent timing attacks.
    Raises 401 with WWW-Authenticate header so browsers auto-prompt.
    """
    if not AUTH_ENABLED:
        if IS_PRODUCTION:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail='Auth not configured. Set AUDIT_USERNAME and '
                       'AUDIT_PASSWORD (fail-closed in production).',
            )
        return True

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Basic realm="AEO Auditor"'},
        )

    user_ok = secrets.compare_digest(credentials.username, AUDIT_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, AUDIT_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid credentials',
            headers={'WWW-Authenticate': 'Basic realm="AEO Auditor"'},
        )
    return True


# Sieve Brain Console (/brain + /api/brain/*): full ruleset visibility + source
# management without the CLI. Every route in the router is auth-gated here —
# the router itself defines none, so nothing ships open by accident.
import brain_console  # noqa: E402
app.include_router(brain_console.router, dependencies=[Depends(require_auth)])


def require_admin(request: Request,
                  credentials: Optional[HTTPBasicCredentials] = Depends(_basic)):
    """Auth for destructive admin ops (delete / suppress). Passes when EITHER
    a valid X-API-Key (server-to-server) OR valid HTTP Basic credentials (a
    logged-in browser admin) are presented — so the homepage delete button
    works without embedding the API key in client JS. Fail-closed in
    production when no auth is configured at all."""
    if API_KEY_ENABLED:
        key = request.headers.get('X-API-Key', '')
        if key and secrets.compare_digest(key, AUDIT_API_KEY):
            return True
    if AUTH_ENABLED and credentials is not None:
        if (secrets.compare_digest(credentials.username, AUDIT_USERNAME)
                and secrets.compare_digest(credentials.password, AUDIT_PASSWORD)):
            return True
    if not API_KEY_ENABLED and not AUTH_ENABLED:
        # Nothing configured. Fail closed in production, allow in local dev.
        if IS_PRODUCTION:
            raise HTTPException(status_code=503, detail='Admin auth not configured')
        return True
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail='Admin authentication required',
        headers={'WWW-Authenticate': 'Basic realm="AEO Auditor"'},
    )


@app.middleware('http')
async def request_logging_middleware(request, call_next):
    """Log slow or non-2xx HTTP responses. Skips the normal 2xx/<1s noise
    that uvicorn already prints, but always surfaces server errors and
    audit-related calls (which can be slow)."""
    t0 = time.time()
    try:
        response = await call_next(request)
    except Exception as e:
        elapsed_ms = int((time.time() - t0) * 1000)
        log.exception('request crashed: %s %s in %dms: %s',
                      request.method, request.url.path, elapsed_ms, e)
        raise
    elapsed_ms = int((time.time() - t0) * 1000)
    code = response.status_code
    is_audit_path = request.url.path.startswith('/audit')
    # Log conditions: server error, slow (>2s), or audit-related anomaly (>=400)
    if code >= 500:
        log.error('%s %s → %d in %dms', request.method, request.url.path, code, elapsed_ms)
    elif code >= 400 and is_audit_path:
        log.warning('%s %s → %d in %dms', request.method, request.url.path, code, elapsed_ms)
    elif elapsed_ms > 2000:
        log.info('SLOW %s %s → %d in %dms', request.method, request.url.path, code, elapsed_ms)
    return response


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AEO / SEO / GEO Auditor</title>
<script>
  // Set the theme BEFORE first paint: saved choice wins, else system.
  (function () {
    try {
      var saved = localStorage.getItem('aeo-theme');
      var dark = (saved === 'dark' || saved === 'light')
        ? saved === 'dark'
        : window.matchMedia('(prefers-color-scheme: dark)').matches;
      document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    } catch (e) { document.documentElement.dataset.theme = 'light'; }
  })();
</script>
<style>
  /* Luminous design system — from tnsaruniitr-lab/design-principles, as
     implemented in the growthmonk operator console: quiet tinted canvas,
     white surfaces with two-layer shadows, jewel-orb gradients themed via
     --g1/--g2, depth from soft light (auras) rather than hard borders,
     ambient + reactive + entrance motion, all reduced-motion guarded. */
  * { box-sizing:border-box; }
  :root {
    color-scheme: light;
    --brand:#6366f1; --brand-ink:#4f46e5; --teal:#14b8a6;
    --canvas-a:#f7f9fc; --canvas-b:#eef2f7; --surface:#ffffff; --inset:#f1f5f9;
    --ink:#0f172a; --ink-soft:#475569; --ink-faint:#94a3b8;
    --hairline:rgb(15 23 42 / .07); --edge:rgb(15 23 42 / .045);
    --track:rgb(15 23 42 / .08); --row-hover:rgb(99 102 241 / .045);
    --mut-bg:rgb(100 116 139 / .1); --mut-ink:#64748b;
    --ok-ink:#047857; --ok-bg:rgb(16 185 129 / .13); --ok:#10b981;
    --run-ink:#0369a1; --run-bg:rgb(56 189 248 / .16);
    --warn-ink:#b45309; --warn-bg:rgb(245 158 11 / .14); --warn:#f59e0b;
    --err-ink:#be123c; --err-bg:rgb(244 63 94 / .12); --err:#f43f5e;
    --crit-ink:#9f1239; --crit-bg:rgb(244 63 94 / .18);
    --glow-a:rgb(99 102 241 / .06); --glow-b:rgb(20 184 166 / .06);
    --shadow-card:0 1px 2px rgb(15 23 42 / .04), 0 4px 16px rgb(15 23 42 / .05);
    --shadow-lift:0 18px 44px rgb(15 23 42 / .14);
    --aurora-o:.5; --aura-o:.13; --aura-o-hover:.3;
    --code-bg:#0f172a; --code-ink:#e2e8f0;
    --g1:#6366f1; --g2:#4f46e5;
    /* legacy aliases used by older render paths */
    --fg-2:#475569; --muted:#64748b; --muted-2:#94a3b8; --accent:#4f46e5;
    --border:rgb(15 23 42 / .07);
  }
  :root[data-theme="dark"] {
    --brand-ink:#a5b4fc;
    --canvas-a:#0d1526; --canvas-b:#0b1220; --surface:#0f172a; --inset:#1e293b;
    --ink:#e2e8f0; --ink-soft:#94a3b8; --ink-faint:#64748b;
    --hairline:rgb(148 163 184 / .08); --edge:rgb(148 163 184 / .07);
    --track:rgb(148 163 184 / .14); --row-hover:rgb(99 102 241 / .09);
    --mut-bg:rgb(148 163 184 / .13); --mut-ink:#94a3b8;
    --ok-ink:#34d399; --ok-bg:rgb(16 185 129 / .16);
    --run-ink:#38bdf8; --run-bg:rgb(56 189 248 / .16);
    --warn-ink:#fbbf24; --warn-bg:rgb(245 158 11 / .15);
    --err-ink:#fb7185; --err-bg:rgb(244 63 94 / .16);
    --crit-ink:#fb7185; --crit-bg:rgb(244 63 94 / .22);
    --glow-a:rgb(99 102 241 / .1); --glow-b:rgb(20 184 166 / .07);
    --shadow-card:0 1px 2px rgb(2 6 23 / .5), 0 4px 18px rgb(2 6 23 / .42);
    --shadow-lift:0 18px 44px rgb(2 6 23 / .6);
    --aurora-o:.38; --aura-o:.16; --aura-o-hover:.32;
    --code-bg:#0b1220; --code-ink:#cbd5e1;
    --fg-2:#94a3b8; --muted:#94a3b8; --muted-2:#64748b; --accent:#a5b4fc;
    --border:rgb(148 163 184 / .08);
    color-scheme: dark;
  }
  html { background:var(--canvas-b); scroll-behavior:smooth; }
  html,body { margin:0; padding:0; }
  body {
    color:var(--ink); min-height:100vh;
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
    -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
    background:
      radial-gradient(1200px 600px at 100% 0, var(--glow-a), transparent 60%),
      radial-gradient(900px 500px at 0% 100%, var(--glow-b), transparent 55%),
      linear-gradient(180deg, var(--canvas-a), var(--canvas-b));
  }
  ::selection { background:rgb(99 102 241 / .22); }

  /* Ambient: drifting header aurora */
  .aurora-wrap { position:absolute; top:0; left:0; right:0; height:300px;
    overflow:hidden; pointer-events:none; z-index:0; }
  .aurora { position:absolute; top:-70px; left:50%; width:min(1100px,140vw);
    height:280px; filter:blur(46px); opacity:var(--aurora-o);
    background:
      radial-gradient(420px 200px at 18% 30%, rgb(99 102 241 / .45), transparent 60%),
      radial-gradient(380px 220px at 60% 12%, rgb(20 184 166 / .38), transparent 62%),
      radial-gradient(360px 200px at 86% 38%, rgb(139 92 246 / .35), transparent 60%);
    transform:translate3d(-50%,0,0);
    animation:drift 16s ease-in-out infinite alternate; }
  @keyframes drift {
    0% { transform:translate3d(-50%,-2%,0) scale(1); }
    50% { transform:translate3d(-50%,2%,0) scale(1.06); }
    100% { transform:translate3d(-50%,-1%,0) scale(1.03); } }

  .wrap { position:relative; z-index:1; max-width:1120px; margin:0 auto;
    padding:56px 28px 90px; }

  h1 { font-size:clamp(27px,4vw,36px); margin:0 0 8px; font-weight:800;
    letter-spacing:-0.025em; line-height:1.12;
    background:linear-gradient(100deg, var(--brand), var(--teal));
    -webkit-background-clip:text; background-clip:text; color:transparent; }
  h2 { font-size:11px; margin:0 0 16px; text-transform:uppercase;
    letter-spacing:0.14em; color:var(--ink-faint); font-weight:800;
    display:flex; align-items:center; gap:10px; }
  h2::after { content:''; flex:1; height:1px;
    background:linear-gradient(90deg, var(--hairline), transparent); }
  h3 { font-size:17px; margin:0; font-weight:700; letter-spacing:-0.01em;
    color:var(--ink); }
  h4 { font-size:10.5px; margin:0 0 7px; text-transform:uppercase;
    letter-spacing:0.12em; color:var(--ink-faint); font-weight:800; }
  p { margin:0 0 8px; }
  a { color:var(--brand-ink); text-decoration:none; }
  a:hover { text-decoration:underline; text-underline-offset:3px; }

  /* Header / form */
  .tagline { color:var(--ink-soft); margin-bottom:30px; font-size:14px; }
  form { display:flex; gap:12px; margin-bottom:34px; }
  input[type=url], input[type=text] { flex:1; background:var(--surface);
    border:1px solid var(--hairline); color:var(--ink); padding:14px 18px;
    border-radius:14px; font-size:15px; outline:none;
    box-shadow:var(--shadow-card);
    transition:border-color .2s, box-shadow .2s; }
  input[type=url]::placeholder, input[type=text]::placeholder { color:var(--ink-faint); }
  input[type=url]:focus, input[type=text]:focus { border-color:var(--brand);
    box-shadow:0 0 0 4px rgb(99 102 241 / .16), var(--shadow-card); }
  button { font:inherit; background:linear-gradient(135deg, var(--brand), #4f46e5);
    color:#fff; border:0; padding:14px 26px; border-radius:14px;
    font-weight:800; cursor:pointer; font-size:15px; letter-spacing:0.01em;
    box-shadow:0 6px 16px rgb(99 102 241 / .35);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s, filter .2s; }
  button:hover:not(:disabled) { transform:translateY(-1px); filter:brightness(1.06);
    box-shadow:0 10px 26px rgb(99 102 241 / .42); }
  button:active:not(:disabled) { transform:scale(.985); }
  button:disabled { opacity:.45; cursor:not-allowed; box-shadow:none; }

  /* Status / spinner */
  .status-card { background:var(--surface); border:1px solid var(--edge);
    border-radius:16px; padding:22px; box-shadow:var(--shadow-card); }
  .status { display:inline-flex; align-items:center; gap:9px; padding:6px 14px;
    border-radius:999px; font-size:12px; font-weight:700;
    background:rgb(99 102 241 / .12); color:var(--brand-ink); }
  .status.error { background:var(--err-bg); color:var(--err-ink); }
  .spinner { display:inline-block; width:11px; height:11px;
    border:2px solid rgb(99 102 241 / .25); border-top-color:var(--brand);
    border-radius:50%; animation:spin .7s linear infinite; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .err { color:var(--err-ink); white-space:pre-wrap;
    font-family:ui-monospace,Menlo,monospace; font-size:12px; margin-top:10px; }

  /* Sections & cards — staggered entrance */
  section { margin-top:34px; animation:rise .4s cubic-bezier(.2,.7,.2,1) both; }
  section:nth-of-type(1){animation-delay:.02s} section:nth-of-type(2){animation-delay:.06s}
  section:nth-of-type(3){animation-delay:.1s}  section:nth-of-type(4){animation-delay:.14s}
  section:nth-of-type(5){animation-delay:.18s} section:nth-of-type(6){animation-delay:.22s}
  section:nth-of-type(n+7){animation-delay:.26s}
  @keyframes rise { from { opacity:0; transform:translateY(6px); }
    to { opacity:1; transform:none; } }
  .card { background:var(--surface); border:1px solid var(--edge);
    border-radius:18px; padding:24px 26px; box-shadow:var(--shadow-card); }
  .card.tight { padding:16px 18px; }

  /* ── Hero ─────────────────────────────────────────────────────────── */
  .hero { position:relative; overflow:hidden; background:var(--surface);
    border:1px solid var(--edge); border-radius:22px; padding:30px 32px;
    display:grid; grid-template-columns:1fr auto; gap:18px 34px;
    align-items:center; box-shadow:var(--shadow-card);
    animation:rise .4s cubic-bezier(.2,.7,.2,1) both; }
  .hero::before { content:''; position:absolute; top:-42px; left:-36px;
    width:190px; height:190px; border-radius:50%; filter:blur(8px);
    opacity:var(--aura-o); pointer-events:none;
    background:radial-gradient(circle, var(--brand), transparent 68%); }
  .hero-kicker { display:inline-block; font-size:11px; font-weight:800;
    letter-spacing:0.14em; text-transform:uppercase; color:var(--brand-ink);
    background:rgb(99 102 241 / .1); padding:4px 12px; border-radius:999px;
    margin-bottom:12px; }
  .hero-meta-url { font-size:clamp(17px,2.4vw,23px); color:var(--ink);
    font-weight:800; word-break:break-all; letter-spacing:-0.02em; }
  .hero-meta-tags { color:var(--ink-soft); font-size:13px; margin-top:10px;
    display:flex; flex-wrap:wrap; gap:7px; }
  .meta-pill { background:var(--inset); border:1px solid var(--edge);
    border-radius:999px; padding:3px 11px; font-size:12px; color:var(--ink-soft);
    font-weight:600; }
  .hero-score { display:flex; align-items:center; gap:20px; }
  .gauge { position:relative; width:128px; height:128px; flex:none; }
  .gauge svg { width:100%; height:100%; transform:rotate(-90deg); }
  .gauge .track { fill:none; stroke:var(--track); stroke-width:9; }
  .gauge .arc { fill:none; stroke-width:9; stroke-linecap:round;
    transition:stroke-dashoffset 1s cubic-bezier(.2,.7,.2,1); }
  .gauge .arc.good { stroke:var(--teal); filter:drop-shadow(0 3px 6px rgb(20 184 166 / .45)); }
  .gauge .arc.warn { stroke:var(--warn); filter:drop-shadow(0 3px 6px rgb(245 158 11 / .4)); }
  .gauge .arc.bad { stroke:var(--err); filter:drop-shadow(0 3px 6px rgb(244 63 94 / .4)); }
  .gauge-center { position:absolute; inset:0; display:flex; flex-direction:column;
    align-items:center; justify-content:center; }
  .gauge-num { font-size:36px; font-weight:800; letter-spacing:-0.03em;
    line-height:1; font-variant-numeric:tabular-nums; color:var(--ink); }
  .gauge-cap { font-size:9.5px; color:var(--ink-faint); text-transform:uppercase;
    letter-spacing:0.14em; font-weight:800; margin-top:5px; }
  .grade { font-size:25px; font-weight:800; padding:10px 20px; border-radius:14px;
    line-height:1; background:var(--mut-bg); color:var(--mut-ink);
    letter-spacing:-0.01em; }
  .grade.A, .grade.B { background:var(--ok-bg); color:var(--ok-ink); }
  .grade.C { background:var(--warn-bg); color:var(--warn-ink); }
  .grade.D, .grade.F { background:var(--err-bg); color:var(--err-ink); }
  .hero-shadow { font-size:11.5px; color:var(--ink-faint); margin-top:8px;
    text-align:center; font-variant-numeric:tabular-nums; }
  .hero-diag { grid-column:1 / -1; color:var(--ink-soft); font-size:15.5px;
    line-height:1.65; padding:16px 0 2px; border-top:1px solid var(--hairline); }
  @media (max-width:640px) { .hero { grid-template-columns:1fr; }
    .hero-score { justify-content:flex-start; } }

  /* ── Score tiles: the jewel constellation ────────────────────────── */
  .tile-grid { display:grid; gap:13px;
    grid-template-columns:repeat(auto-fill,minmax(200px,1fr)); }
  .tile { position:relative; overflow:hidden; background:var(--surface);
    border:1px solid var(--edge); border-radius:16px; padding:16px 18px 15px;
    box-shadow:var(--shadow-card);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s;
    animation:rise .4s cubic-bezier(.2,.7,.2,1) both; }
  .tile:hover { transform:translateY(-3px); box-shadow:var(--shadow-lift); }
  .tile-aura { position:absolute; top:-40px; left:-32px; width:160px;
    height:160px; border-radius:50%; filter:blur(8px); opacity:var(--aura-o);
    pointer-events:none; transition:opacity .3s, transform .3s;
    background:radial-gradient(circle, var(--g1), transparent 68%); }
  .tile:hover .tile-aura { opacity:var(--aura-o-hover); transform:scale(1.1); }
  .tile-top { display:flex; align-items:center; gap:10px; margin-bottom:12px;
    position:relative; }
  .tile-orb { display:grid; place-items:center; width:34px; height:34px;
    flex:none; border-radius:11px; color:#fff; font-weight:800; font-size:14px;
    background:linear-gradient(135deg, var(--g1), var(--g2));
    box-shadow:0 6px 16px color-mix(in srgb, var(--g2) 45%, transparent),
      inset 0 1px 1px rgb(255 255 255 / .45);
    transition:transform .3s cubic-bezier(.2,.7,.2,1); }
  .tile:hover .tile-orb { transform:scale(1.06) rotate(-3deg); }
  .tile-label { font-size:10.5px; font-weight:800; letter-spacing:0.1em;
    text-transform:uppercase; color:var(--ink-faint); line-height:1.4; }
  .tile-val { font-size:29px; font-weight:800; letter-spacing:-0.03em;
    line-height:1; font-variant-numeric:tabular-nums; margin-bottom:12px;
    color:var(--ink); position:relative; }
  .tile-val.na { color:var(--ink-faint); font-size:21px; }
  .tile.good .tile-val { color:var(--ok-ink); }
  .tile.warn .tile-val { color:var(--warn-ink); }
  .tile.bad  .tile-val { color:var(--err-ink); }
  .tile-bar { height:5px; border-radius:99px; background:var(--track);
    overflow:hidden; position:relative; }
  .tile-fill { height:100%; border-radius:99px;
    transition:width .8s cubic-bezier(.2,.7,.2,1); }
  .tile.good .tile-fill { background:linear-gradient(90deg, var(--teal), #34d399); }
  .tile.warn .tile-fill { background:linear-gradient(90deg, #d97706, var(--warn)); }
  .tile.bad  .tile-fill { background:linear-gradient(90deg, #e11d48, var(--err)); }
  .score-extras { font-size:13px; color:var(--ink-soft); line-height:1.7; }
  .score-extras strong { color:var(--ink); font-weight:700; }

  /* ── Top fixes ───────────────────────────────────────────────────── */
  .fixes h2 { color:var(--ink-soft); }
  .fix { position:relative; overflow:hidden; background:var(--surface);
    border:1px solid var(--edge); border-radius:18px; padding:22px 24px;
    margin-bottom:14px; box-shadow:var(--shadow-card);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s; }
  .fix:hover { transform:translateY(-2px); box-shadow:var(--shadow-lift); }
  .fix::before { content:''; position:absolute; top:-44px; left:-36px;
    width:150px; height:150px; border-radius:50%; filter:blur(8px);
    opacity:var(--aura-o); pointer-events:none;
    background:radial-gradient(circle, var(--brand), transparent 68%); }
  .fix-header { display:flex; flex-wrap:wrap; gap:10px; align-items:center;
    margin-bottom:16px; position:relative; }
  .fix-rank { display:grid; place-items:center; min-width:34px; height:34px;
    padding:0 8px; font-size:13px; font-weight:800; color:#fff;
    background:linear-gradient(135deg, var(--brand), var(--brand-ink));
    border-radius:11px; font-variant-numeric:tabular-nums;
    box-shadow:0 6px 16px rgb(99 102 241 / .4),
      inset 0 1px 1px rgb(255 255 255 / .45); }
  .fix-title { flex:1; font-size:17px; font-weight:700; min-width:200px;
    letter-spacing:-0.01em; color:var(--ink); }
  .fix-body { display:grid; grid-template-columns:1fr; gap:12px; }
  @media (min-width:720px) { .fix-body { grid-template-columns:1fr 1fr; }
    .fix-body .why { grid-column:1 / -1; } }
  .fix-block { background:var(--inset); border:1px solid var(--edge);
    border-radius:12px; padding:14px 16px; }
  .fix-block.why { background:transparent; border-color:transparent; padding:6px 2px 0; }
  .fix-block p, .fix-block pre { margin:0; font-size:14px; line-height:1.6;
    color:var(--ink-soft); }
  pre { white-space:pre-wrap; word-break:break-word;
    font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:12.5px;
    background:var(--code-bg); color:var(--code-ink); border:0;
    padding:12px 14px; border-radius:10px; margin:6px 0; overflow-x:auto; }
  code { font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:12px;
    background:var(--mut-bg); color:var(--ink-soft); padding:2px 6px;
    border-radius:6px; }

  /* Badges */
  .badge { display:inline-flex; align-items:center; font-size:10.5px;
    font-weight:800; text-transform:uppercase; letter-spacing:0.06em;
    padding:4px 11px; border-radius:999px; background:var(--mut-bg);
    color:var(--mut-ink); white-space:nowrap; }
  .badge.impact-critical { background:var(--crit-bg); color:var(--crit-ink); }
  .badge.impact-high { background:var(--err-bg); color:var(--err-ink); }
  .badge.impact-medium { background:var(--warn-bg); color:var(--warn-ink); }
  .badge.impact-low { background:var(--mut-bg); color:var(--mut-ink); }
  .badge.effort-trivial, .badge.effort-easy { background:var(--ok-bg); color:var(--ok-ink); }
  .badge.effort-moderate { background:var(--warn-bg); color:var(--warn-ink); }
  .badge.effort-heavy { background:var(--err-bg); color:var(--err-ink); }
  .badge.truth { background:rgb(99 102 241 / .12); color:var(--brand-ink); }
  .badge.type { background:var(--mut-bg); color:var(--mut-ink); }
  .sev { display:inline-flex; font-size:10.5px; font-weight:800;
    letter-spacing:0.05em; text-transform:uppercase; padding:3px 10px;
    border-radius:999px; color:var(--mut-ink); background:var(--mut-bg); }
  .sev.critical { background:var(--crit-bg); color:var(--crit-ink); }
  .sev.high { background:var(--err-bg); color:var(--err-ink); }
  .sev.medium { background:var(--warn-bg); color:var(--warn-ink); }
  .sev.low, .sev.info { background:var(--mut-bg); color:var(--mut-ink); }

  /* Why not cited cards */
  .wnc-grid { display:grid; gap:14px; }
  @media (min-width:720px) { .wnc-grid { grid-template-columns:repeat(3,1fr); } }
  .wnc { position:relative; overflow:hidden; background:var(--surface);
    border:1px solid var(--edge); border-radius:16px; padding:20px 22px;
    box-shadow:var(--shadow-card);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s; }
  .wnc:hover { transform:translateY(-3px); box-shadow:var(--shadow-lift); }
  .wnc::before { content:''; position:absolute; top:-40px; left:-32px;
    width:140px; height:140px; border-radius:50%; filter:blur(8px);
    opacity:var(--aura-o); pointer-events:none;
    background:radial-gradient(circle, var(--teal), transparent 68%); }
  .wnc h3 { font-size:15px; margin:10px 0; position:relative; }
  .wnc p { color:var(--ink-soft); font-size:13.5px; line-height:1.6;
    position:relative; }

  /* Two-column diagnostic */
  .twocol { display:grid; gap:14px; }
  @media (min-width:720px) { .twocol { grid-template-columns:1fr 1fr; } }
  .stat-table { width:100%; border-collapse:collapse; font-size:13px; }
  .stat-table td { padding:8px 0; border-bottom:1px solid var(--hairline);
    vertical-align:top; }
  .stat-table tr:last-child td { border-bottom:0; }
  .stat-table td:first-child { color:var(--mut-ink); width:50%; }
  .stat-table td:last-child { color:var(--ink-soft); text-align:right;
    font-variant-numeric:tabular-nums; word-break:break-word; }

  /* Competitor / SOV table */
  .comp-table { width:100%; border-collapse:collapse; font-size:13px; }
  .comp-table th, .comp-table td { padding:10px 12px;
    border-bottom:1px solid var(--hairline); text-align:left; vertical-align:top; }
  .comp-table th { color:var(--ink-faint); font-weight:800; font-size:10.5px;
    text-transform:uppercase; letter-spacing:0.08em; }
  .comp-table tbody tr { transition:background .15s; }
  .comp-table tbody tr:hover { background:var(--row-hover); }
  .comp-table tr.target td { background:rgb(99 102 241 / .07); }
  .comp-table tr.target td:first-child { border-left:3px solid var(--brand);
    font-weight:700; }
  .comp-table td { font-variant-numeric:tabular-nums; color:var(--ink-soft); }
  .comp-table td:first-child { font-weight:600; color:var(--ink); }

  /* Findings table */
  details { background:var(--surface); border:1px solid var(--edge);
    border-radius:18px; padding:16px 20px; box-shadow:var(--shadow-card); }
  details > summary { cursor:pointer; user-select:none; color:var(--ink-soft);
    font-size:14px; font-weight:700; padding:4px 0; list-style:none;
    display:flex; align-items:center; gap:10px; }
  details > summary::before { content:'▸'; color:var(--ink-faint);
    transition:transform .15s; }
  details[open] > summary::before { transform:rotate(90deg); }
  details[open] > summary { margin-bottom:14px; }
  .findings-table { width:100%; border-collapse:collapse; font-size:13px; }
  .findings-table th, .findings-table td { padding:9px 10px;
    border-bottom:1px solid var(--hairline); text-align:left; vertical-align:top; }
  .findings-table th { color:var(--ink-faint); font-weight:800; font-size:10.5px;
    text-transform:uppercase; letter-spacing:0.08em; }
  .findings-table td { color:var(--ink-soft); }
  .findings-table tbody tr { transition:background .15s; }
  .findings-table tbody tr:hover { background:var(--row-hover); }
  .status-icon { display:inline-flex; width:22px; height:22px; border-radius:8px;
    align-items:center; justify-content:center; font-weight:800; font-size:12px; }
  .status-icon.fail { color:var(--err-ink); background:var(--err-bg); }
  .status-icon.warn { color:var(--warn-ink); background:var(--warn-bg); }
  .status-icon.pass { color:var(--ok-ink); background:var(--ok-bg); }
  .check-id { font-family:ui-monospace,"SF Mono",Menlo,monospace; font-size:12px;
    color:var(--mut-ink); }

  /* Quick wins */
  .qw-list { padding-left:0; list-style:none; margin:0; }
  .qw-list li { padding:10px 0 10px 30px; position:relative;
    border-bottom:1px solid var(--hairline); color:var(--ink-soft); font-size:14px; }
  .qw-list li:last-child { border-bottom:0; }
  .qw-list li:before { content:'✓'; position:absolute; left:2px; top:9px;
    color:var(--ok-ink); font-weight:800; width:18px; height:18px; font-size:11px;
    background:var(--ok-bg); border-radius:6px; display:flex;
    align-items:center; justify-content:center; }

  /* ── Sources cited ───────────────────────────────────────────────── */
  .tier { position:relative; overflow:hidden; background:var(--surface);
    border:1px solid var(--edge); border-radius:16px; padding:18px 20px;
    margin-bottom:14px; box-shadow:var(--shadow-card); }
  .tier:last-child { margin-bottom:0; }
  .tier h3 { font-size:11.5px; color:var(--ink-faint); margin:0 0 12px;
    text-transform:uppercase; letter-spacing:0.1em; font-weight:800;
    position:relative; }
  .tier.t1::before { content:''; position:absolute; top:-44px; left:-36px;
    width:170px; height:170px; border-radius:50%; filter:blur(8px);
    opacity:var(--aura-o); pointer-events:none;
    background:radial-gradient(circle, var(--brand), transparent 68%); }
  .tier.t1 h3 { color:var(--brand-ink); }
  .citation { font-size:13.5px; color:var(--ink-soft); padding:11px 0;
    border-bottom:1px solid var(--hairline); line-height:1.55; position:relative; }
  .citation:last-child { border-bottom:0; padding-bottom:2px; }
  .citation:first-of-type { padding-top:2px; }
  .citation .src { color:var(--ink); font-weight:800; font-size:13.5px; }
  .citation .nm { color:var(--ink-soft); }
  .citation a .nm:hover { color:var(--brand-ink); }
  .cite-reason { font-size:12.5px; color:var(--mut-ink); margin:7px 0 0;
    padding:9px 13px; border-left:3px solid var(--brand);
    background:rgb(99 102 241 / .06); border-radius:0 10px 10px 0;
    line-height:1.55; }
  .cite-ver { font-size:11px; color:var(--ink-faint); font-weight:700; }
  .cite-unver { font-size:10.5px; color:var(--warn-ink); font-weight:800;
    text-transform:uppercase; letter-spacing:0.05em; background:var(--warn-bg);
    padding:2px 9px; border-radius:999px; }

  /* Downloads */
  .links { display:flex; gap:10px; flex-wrap:wrap; }
  .links a { background:var(--surface); border:1px solid var(--edge);
    color:var(--ink-soft); padding:10px 18px; border-radius:999px;
    font-size:13px; font-weight:700; box-shadow:var(--shadow-card);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s,
      color .2s, background .2s; }
  .links a:hover { color:#fff;
    background:linear-gradient(135deg, var(--brand), var(--brand-ink));
    box-shadow:0 6px 16px rgb(99 102 241 / .35); transform:translateY(-1px);
    text-decoration:none; }

  /* Library */
  .lib-head { display:flex; align-items:baseline; justify-content:space-between;
    margin:44px 0 16px; }
  .lib-head h2 { margin:0; }
  .lib-count { color:var(--ink-faint); font-size:12px; }
  .lib-grid { display:grid; gap:13px;
    grid-template-columns:repeat(auto-fill,minmax(235px,1fr)); }
  .lib-card { position:relative; display:block; overflow:hidden;
    background:var(--surface); border:1px solid var(--edge); border-radius:16px;
    padding:18px 20px; color:inherit; box-shadow:var(--shadow-card);
    transition:transform .22s cubic-bezier(.2,.7,.2,1), box-shadow .22s; }
  .lib-card:hover { transform:translateY(-3px); box-shadow:var(--shadow-lift); }
  .lib-card::before { content:''; position:absolute; top:-40px; left:-32px;
    width:140px; height:140px; border-radius:50%; filter:blur(8px);
    opacity:var(--aura-o); pointer-events:none; transition:opacity .3s;
    background:radial-gradient(circle, var(--brand), transparent 68%); }
  .lib-card:hover::before { opacity:var(--aura-o-hover); }
  .lib-link { position:absolute; inset:0; z-index:1; border-radius:16px;
    text-decoration:none; }
  .lib-del { position:absolute; top:10px; right:10px; z-index:2; width:26px;
    height:26px; padding:0; border:1px solid var(--edge); border-radius:8px;
    background:var(--surface); color:var(--ink-faint); font-size:13px;
    line-height:1; cursor:pointer; opacity:0; box-shadow:none;
    transition:opacity .15s, background .15s, color .15s; }
  .lib-card:hover .lib-del, .lib-del:focus { opacity:1; }
  .lib-del:hover { background:var(--err-bg); color:var(--err-ink);
    transform:none; box-shadow:none; }
  .lib-domain { font-weight:700; font-size:14.5px; color:var(--ink);
    word-break:break-all; line-height:1.35; letter-spacing:-0.01em;
    position:relative; }
  .lib-sub { color:var(--ink-faint); font-size:12px; margin-top:4px;
    position:relative; }
  .lib-row { display:flex; align-items:center; gap:12px; margin-top:16px;
    position:relative; }
  .lib-score { font-size:30px; font-weight:800; letter-spacing:-0.03em;
    line-height:1; font-variant-numeric:tabular-nums; color:var(--ink); }
  .lib-grade { font-size:13px; font-weight:800; padding:4px 11px;
    border-radius:999px; line-height:1; background:var(--mut-bg);
    color:var(--mut-ink); }
  .lib-grade.A, .lib-grade.B { background:var(--ok-bg); color:var(--ok-ink); }
  .lib-grade.C { background:var(--warn-bg); color:var(--warn-ink); }
  .lib-grade.D, .lib-grade.F { background:var(--err-bg); color:var(--err-ink); }
  .lib-meta { margin-left:auto; text-align:right; color:var(--ink-faint);
    font-size:11px; line-height:1.5; }
  .lib-empty { color:var(--ink-faint); font-size:13px; padding:24px;
    background:var(--surface); border:1px dashed var(--hairline);
    border-radius:16px; text-align:center; }

  /* Footer */
  footer { color:var(--ink-faint); font-size:12px; margin-top:56px;
    text-align:center; padding-top:22px; border-top:1px solid var(--hairline); }
  footer a { color:var(--mut-ink); }

  /* Misc */
  .err-block { background:var(--err-bg); border:1px solid rgb(244 63 94 / .2);
    border-radius:12px; padding:14px 16px; color:var(--err-ink); }

  /* Fix sources — receipts under the WHY paragraph */
  .fix-sources { margin-top:10px; padding-top:10px;
    border-top:1px solid var(--hairline); font-size:12.5px;
    color:var(--ink-faint); line-height:1.9; }
  .fix-sources-label { font-size:10px; font-weight:800; text-transform:uppercase;
    letter-spacing:0.12em; color:var(--ink-faint); margin-right:10px; }
  .fix-sources a { color:var(--brand-ink); font-weight:600; }
  .fix-src-sep { margin:0 8px; color:var(--hairline); }

  /* Theme toggle */
  #theme-toggle { position:fixed; top:18px; right:18px; z-index:60;
    width:42px; height:42px; padding:0; border-radius:50%;
    background:var(--surface); color:var(--ink-soft);
    border:1px solid var(--edge); box-shadow:var(--shadow-card);
    font-size:17px; line-height:1; display:grid; place-items:center;
    cursor:pointer; transition:transform .22s cubic-bezier(.2,.7,.2,1),
      box-shadow .22s, color .2s; }
  #theme-toggle:hover { background:var(--surface); color:var(--brand-ink);
    filter:none; transform:translateY(-1px); box-shadow:var(--shadow-lift); }
  #theme-toggle:active { transform:scale(.94); }

  @media (prefers-reduced-motion: reduce) {
    .aurora { animation:none; }
    section, .tile, .hero { animation-duration:.01ms; }
    .tile:hover, .fix:hover, .wnc:hover, .lib-card:hover { transform:none; }
  }
</style>
</head>
<body>
<div class="aurora-wrap"><div class="aurora"></div></div>
<button id="theme-toggle" type="button" aria-label="Toggle dark / light mode" title="Toggle dark / light mode">☾</button>
<div class="wrap">
  <h1>AEO / SEO / GEO Auditor</h1>
  <div class="tagline">Full 97-check audit · Sieve brain (12,764 entries) · Claude Sonnet 4.6</div>

  <form id="f">
    <input id="url" type="text" inputmode="url" autocomplete="url"
           placeholder="example.com   (or https://example.com/page)"
           required autofocus spellcheck="false" autocapitalize="off">
    <button id="go" type="submit">Run audit</button>
  </form>

  <div id="out"></div>

  <div id="library"></div>

  <footer>
    JSON API: <a href="/api">/api</a> · Health: <a href="/healthz">/healthz</a> · Docs: <a href="/docs">/docs</a>
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const out = $('out');

// Dark / light toggle: click flips + persists; glyph mirrors the mode.
// With no saved choice we keep following the system preference live.
(function () {
  const btn = $('theme-toggle');
  if (!btn) return;
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const glyph = () => {
    btn.textContent = document.documentElement.dataset.theme === 'dark' ? '☀' : '☾';
  };
  glyph();
  btn.addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem('aeo-theme', next); } catch (e) {}
    glyph();
  });
  mq.addEventListener('change', (e) => {
    let saved = null;
    try { saved = localStorage.getItem('aeo-theme'); } catch (err) {}
    if (saved !== 'dark' && saved !== 'light') {
      document.documentElement.dataset.theme = e.matches ? 'dark' : 'light';
      glyph();
    }
  });
})();

function normalizeUrl(raw) {
  // Trim + auto-prepend https:// when the user types a bare domain like
  // "feelvaleo.com" or "www.feelvaleo.com/path". Leaves explicit http:// /
  // https:// untouched. Strips leading "//".
  let s = String(raw || '').trim();
  if (!s) return '';
  // Common paste artifacts: "http://https://..." or scheme typos
  s = s.replace(/^\s+|\s+$/g, '');
  if (/^https?:\/\//i.test(s)) return s;
  if (s.startsWith('//')) return 'https:' + s;
  return 'https://' + s;
}

$('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const url = normalizeUrl($('url').value);
  if (!url) return;
  // Reflect the normalized URL back in the field so the user sees what we sent
  $('url').value = url;
  $('go').disabled = true;
  out.innerHTML = '<div class="status-card"><span class="status"><span class="spinner"></span>Submitting…</span></div>';

  try {
    const r = await fetch('/audit', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({url})
    });
    if (!r.ok) throw new Error('Submit failed: ' + r.status + ' ' + (await r.text()));
    const {audit_id} = await r.json();
    await poll(audit_id);
  } catch (err) {
    out.innerHTML = renderError(err.message);
  } finally {
    $('go').disabled = false;
  }
});

async function poll(id) {
  const started = Date.now();
  let consecutive404 = 0;
  while (true) {
    const r = await fetch('/audit/' + id);
    const elapsed = Math.round((Date.now() - started) / 1000);

    // 404 = audit not in server's JOBS dict. Either it was never created,
    // or the server restarted (Railway redeploy) and the in-memory state
    // was wiped. Tolerate 2 consecutive 404s in case of a race; bail on 3rd.
    if (r.status === 404) {
      consecutive404 += 1;
      if (consecutive404 >= 3) {
        out.innerHTML = renderError(
          'Audit ' + id.slice(0,8) + '… not found on the server.\n\n' +
          'Most likely cause: the service redeployed mid-audit (Railway in-memory ' +
          'job state is wiped on every container restart). Please resubmit the URL.'
        );
        return;
      }
      await new Promise(r => setTimeout(r, 3000));
      continue;
    }
    consecutive404 = 0;

    if (!r.ok) {
      const body = await r.text().catch(() => '');
      out.innerHTML = renderError('Status check failed: HTTP ' + r.status +
        (body ? '\n\n' + body.slice(0, 500) : ''));
      return;
    }

    const data = await r.json();

    if (data.status === 'completed') {
      // Fetch full audit JSON for rich render
      try {
        const rJson = await fetch('/audit/' + id + '/json');
        if (rJson.ok) {
          const fullAudit = await rJson.json();
          renderFull(fullAudit, data, id);
          loadLibrary();  // refresh the grid so the new audit appears
          return;
        }
      } catch(e) { /* fall through to summary render */ }
      renderSummary(data, id);
      loadLibrary();
      return;
    }
    if (data.status === 'error') {
      let msg = data.error || 'unknown error';
      if (data.agent_errors && data.agent_errors.length) {
        msg += '\n\nAgent errors:\n  - ' + data.agent_errors.join('\n  - ');
      }
      if (data.raw_final_text_preview) {
        msg += '\n\nLast assistant text (preview):\n' + data.raw_final_text_preview.slice(0, 800);
      }
      const stats = [];
      if (data.agent_turns != null) stats.push(data.agent_turns + ' turns');
      if (data.tool_call_count != null) stats.push(data.tool_call_count + ' tool calls');
      if (data.input_tokens != null) stats.push((data.input_tokens + (data.output_tokens||0)) + ' tokens');
      if (stats.length) msg += '\n\nAgent stats: ' + stats.join(' · ');
      out.innerHTML = renderError(msg);
      return;
    }

    const prog = data.progress || {};
    const phaseLine = prog.phase || (data.status === 'queued' ? 'Queued' : 'Starting up...');
    const statsLine = [];
    if (prog.turn) statsLine.push('turn ' + prog.turn);
    if (prog.tool_count) statsLine.push(prog.tool_count + ' tool calls');
    if (prog.last_tool_ms) statsLine.push('last ' + prog.last_tool_ms + 'ms');
    const statsStr = statsLine.length ? ' · ' + statsLine.join(' · ') : '';

    out.innerHTML =
      '<div class="status-card">' +
        '<span class="status"><span class="spinner"></span>' +
          escapeHtml(phaseLine) + ' · ' + elapsed + 's' +
        '</span>' +
        (statsStr ? '<div style="color:var(--muted-2);font-size:12px;margin-top:6px;font-variant-numeric:tabular-nums">' + escapeHtml(statsStr.slice(3)) + '</div>' : '') +
        '<div style="color:var(--muted);font-size:13px;margin-top:12px;line-height:1.55">' +
          'Typical completion: 90–300 seconds depending on site complexity. The agent is running 95+ checks across Technical, Performance, On-Page, Schema, AEO Discovery/Extraction/Trust/Selection, GEO, and Entity Consistency — including a 5-competitor crawl and Sieve brain citation enrichment.' +
        '</div>' +
      '</div>';
    await new Promise(r => setTimeout(r, 3000));
  }
}

function renderError(msg) {
  return '<div class="status-card"><span class="status error">Error</span>' +
    '<div class="err">' + escapeHtml(msg) + '</div></div>';
}

function renderSummary(data, id) {
  // Fallback render when full JSON unavailable
  const s = data.result_summary || {};
  out.innerHTML = renderHero(s, data.duration_seconds, s.findings_count || 0) +
    renderDownloads(id, data.artifacts || {});
}

function renderFull(audit, status, id) {
  const cls = audit.classification || {};
  const sc = audit.scoring || {};
  const narr = audit.narrative || {};
  const findings = audit.findings || [];
  const competitors = audit.competitor_comparison || [];
  const bev = audit.bots_eye_view || (audit.scripts_output && audit.scripts_output.bots_eye_view) || {};
  const perf = audit.performance || {};

  const heroData = {
    url: audit.url,
    page_type: cls.page_type,
    industry: cls.industry,
    company_name: cls.company_name,
    overall_score: sc.overall_score ?? sc.page_citation_readiness,
    overall_grade: sc.overall_grade,
    // scoring.shadow is computed at audit time; reloaded audits carry it in
    // metadata.scoring_shadow (fetch_audit rebuilds scoring from flat columns).
    shadow: sc.shadow || (audit.metadata || {}).scoring_shadow || null,
    executive_diagnosis: narr.executive_diagnosis,
  };

  const html = [
    renderHero(heroData, audit.duration_seconds, findings.length),
    renderMeasuredVisibility(audit.measured_visibility || (audit.metadata || {}).measured_visibility),
    renderTopFixes(narr.top_5_fixes || []),
    renderWhyNotCited(narr.why_not_cited || [], (narr.top_5_fixes || []).length ? null : (findings.find(f => f.citations && f.citations.length))),
    renderScoreBreakdown(sc),
    renderTwoCol(bev, perf, audit),
    renderCompetitors(competitors, audit.domain),
    renderQuickWins(narr.quick_wins || []),
    renderSummary_text(narr.summary_what_to_do),
    renderAllFindings(findings),
    renderBrainSources(findings),
    renderDownloads(id, status.artifacts || {}),
  ];
  out.innerHTML = html.filter(Boolean).join('');
  // Reflect the audited domain in the URL so the page is bookmarkable /
  // shareable as audits.growthmonk.ai/{domain}.
  if (audit && audit.domain) {
    try { history.replaceState({}, '', '/' + audit.domain); } catch (e) {}
  }
}

function renderGauge(score) {
  // SVG ring gauge: r=56, circumference 2πr. Numeric-only inputs reach the
  // markup (score is coerced), so nothing user-controlled is interpolated.
  const n = Number(score);
  const has = Number.isFinite(n);
  const v = has ? Math.max(0, Math.min(100, n)) : 0;
  const C = 2 * Math.PI * 56;
  const off = C * (1 - v / 100);
  const band = !has ? 'bad' : v >= 75 ? 'good' : v >= 50 ? 'warn' : 'bad';
  return (
    '<div class="gauge">' +
      '<svg viewBox="0 0 128 128" aria-hidden="true">' +
        '<circle class="track" cx="64" cy="64" r="56"></circle>' +
        (has ? '<circle class="arc ' + band + '" cx="64" cy="64" r="56"' +
          ' stroke-dasharray="' + C.toFixed(2) + '"' +
          ' stroke-dashoffset="' + off.toFixed(2) + '"></circle>' : '') +
      '</svg>' +
      '<div class="gauge-center">' +
        '<div class="gauge-num">' + numOrDash(score) + '</div>' +
        '<div class="gauge-cap">/ 100</div>' +
      '</div>' +
    '</div>'
  );
}

function renderShadowLine(sh) {
  // SHADOW (evidence-weighted) score — rendered ONLY when the runtime computed
  // one (null = no evidence-backed findings). Number-coerced, so nothing
  // user-controlled reaches the markup.
  if (!sh) return '';
  const n = Number(sh.pcr_evidence);
  if (!Number.isFinite(n)) return '';
  return '<div class="hero-shadow">Evidence-weighted (shadow): ' + n.toFixed(1) +
    ' — counts only runtime-observed findings</div>';
}

function renderHero(s, duration, findingsCount) {
  const grade = (s.overall_grade || '').charAt(0).toUpperCase();
  const pills = [s.page_type, s.industry, s.company_name,
                 (duration ? duration + 's' : null),
                 findingsCount + ' findings']
    .filter(Boolean)
    .map(t => '<span class="meta-pill">' + escapeHtml(String(t)) + '</span>')
    .join('');
  return (
    '<div class="hero">' +
      '<div>' +
        '<div class="hero-kicker">AEO · SEO · GEO Audit</div>' +
        '<div class="hero-meta-url">' + escapeHtml(s.url || '') + '</div>' +
        '<div class="hero-meta-tags">' + pills + '</div>' +
      '</div>' +
      '<div>' +
        '<div class="hero-score">' +
          renderGauge(s.overall_score) +
          '<div class="grade ' + escapeHtml(grade) + '">' + escapeHtml(s.overall_grade || '—') + '</div>' +
        '</div>' +
        renderShadowLine(s.shadow) +
      '</div>' +
      (s.executive_diagnosis ? '<div class="hero-diag">' + escapeHtml(s.executive_diagnosis) + '</div>' : '') +
    '</div>'
  );
}

function renderMeasuredVisibility(mv) {
  if (!mv || !mv.measured || !mv.total_runs) return '';
  const engineNames = {openai: 'ChatGPT (OpenAI)', anthropic: 'Claude',
                       perplexity: 'Perplexity', gemini: 'Gemini', google_aio: 'Google AI Overviews'};
  // Engine inclusion tiles — rates are numeric-only by construction
  const engineJewels = {openai: ['#34d399', '#0d9488'], anthropic: ['#f59e0b', '#d97706'],
                        perplexity: ['#22d3ee', '#0891b2'], gemini: ['#818cf8', '#4338ca'],
                        google_aio: ['#38bdf8', '#2563eb']};
  let tiles = '';
  let vi = 0;
  for (const eng of (mv.engines || [])) {
    const inc = (mv.inclusion || {})[eng] || {};
    const cited = Number(inc.cited_rate);
    const ment = Number(inc.mentioned_rate);
    const band = cited >= 0.5 ? 'good' : (ment >= 0.5 ? 'warn' : 'bad');
    const g = engineJewels[eng] || ['#6366f1', '#4f46e5'];
    const name = engineNames[eng] || eng;
    tiles += '<div class="tile ' + band + '" style="--g1:' + g[0] + ';--g2:' + g[1] +
        ';animation-delay:' + (vi++ * 45) + 'ms">' +
      '<span class="tile-aura"></span>' +
      '<div class="tile-top">' +
        '<span class="tile-orb">' + escapeHtml(name.charAt(0).toUpperCase()) + '</span>' +
        '<div class="tile-label">' + escapeHtml(name) + '</div>' +
      '</div>' +
      '<div class="tile-val">' + (Number.isFinite(cited) ? Math.round(cited * 100) + '%' : '—') + '</div>' +
      '<div class="tile-bar"><div class="tile-fill" style="width:' + pctWidth(cited * 100) + '%"></div></div>' +
      '<div style="font-size:11.5px;color:var(--ink-faint);margin-top:9px;position:relative">cited in AI answers · mentioned ' +
        (Number.isFinite(ment) ? Math.round(ment * 100) + '%' : '—') +
      '</div>' +
    '</div>';
  }
  // Share-of-voice vs crawled competitors
  let sovRows = '';
  for (const r of (mv.share_of_voice || [])) {
    const sovNum = Number(r.sov);
    sovRows += '<tr' + (r.is_target ? ' class="target"' : '') + '>' +
      '<td>' + escapeHtml(r.domain || '') + (r.is_target ? ' (you)' : '') + '</td>' +
      '<td>' + (Number(r.citations) || 0) + '</td>' +
      '<td>' + (Number.isFinite(sovNum) ? Math.round(sovNum * 100) + '%' : '—') + '</td>' +
    '</tr>';
  }
  const meta = 'Measured ' + escapeHtml(String(mv.measured_at || '')) + ' · ' +
    (Number(mv.total_runs) || 0) + ' live answers · ' + (Number(mv.runs_per_query) || 1) +
    ' runs per query — real engine responses, not estimates. Full transcripts logged.';
  return '<section><h2>Measured AI visibility</h2>' +
    '<div class="tile-grid" style="grid-template-columns:repeat(auto-fill,minmax(230px,1fr))">' + tiles + '</div>' +
    (sovRows ? '<div class="card tight" style="margin-top:14px"><table class="comp-table"><thead><tr>' +
      '<th>Domain</th><th>Citations</th><th>Share of voice</th></tr></thead><tbody>' +
      sovRows + '</tbody></table></div>' : '') +
    '<div style="font-size:12px;color:var(--muted-2);margin-top:10px">' + meta + '</div>' +
    '</section>';
}

function renderTopFixes(fixes) {
  if (!fixes || !fixes.length) return '';
  let html = '<section class="fixes"><h2>Top fixes that matter</h2>';
  for (const f of fixes) {
    const impact = String(f.impact || '').toLowerCase();
    const effort = String(f.effort || '').toLowerCase();
    html +=
      '<div class="fix">' +
        '<div class="fix-header">' +
          '<span class="fix-rank">#' + (f.rank || '?') + '</span>' +
          '<span class="fix-title">' + escapeHtml(f.title || '') + '</span>' +
          (f.impact ? '<span class="badge impact-' + escapeHtml(impact) + '">' + escapeHtml(f.impact) + ' impact</span>' : '') +
          (f.effort ? '<span class="badge effort-' + escapeHtml(effort) + '">' + escapeHtml(f.effort) + ' effort</span>' : '') +
          (f.type ? '<span class="badge type">' + escapeHtml(f.type) + '</span>' : '') +
          (f.truth_badge ? '<span class="badge truth">' + escapeHtml(f.truth_badge) + '</span>' : '') +
        '</div>' +
        '<div class="fix-body">' +
          (f.before ? '<div class="fix-block"><h4>Currently</h4>' + formatBlock(f.before) + '</div>' : '') +
          (f.after ? '<div class="fix-block"><h4>Recommended</h4>' + formatBlock(f.after) + '</div>' : '') +
          (f.why ? '<div class="fix-block why"><h4>Why this matters</h4><p>' + escapeHtml(f.why) + '</p>' +
            renderFixSources(f.sources) + '</div>' : '') +
        '</div>' +
      '</div>';
  }
  html += '</section>';
  return html;
}

function renderFixSources(sources) {
  // Python-resolved receipts for the WHY paragraph's brain references
  // (metadata.fix_sources) — org + linked rule/principle name + verified date.
  const srcs = (sources || []).filter(s => s && typeof s === 'object' && (s.name || s.source_url));
  if (!srcs.length) return '';
  const items = srcs.map(s => {
    const label = escapeHtml((s.source_org || 'source') + ' — ' + (s.name || (s.kind + ' #' + s.id)));
    const u = s.source_url ? safeHref(s.source_url) : '';
    const idTag = (s.id != null && !isNaN(s.id))
      ? ' <code style="font-size:10.5px">[' + escapeHtml(String(s.kind || 'ref')) + ' #' + escapeHtml(String(s.id)) + ']</code>' : '';
    const ver = s.last_verified
      ? ' <span class="cite-ver">· verified ' + escapeHtml(String(s.last_verified)) + '</span>' : '';
    return (u ? '<a href="' + u + '" target="_blank" rel="noopener noreferrer">' : '') +
      label + (u ? '</a>' : '') + idTag + ver;
  });
  return '<div class="fix-sources"><span class="fix-sources-label">Sources</span>' +
    items.join('<span class="fix-src-sep">·</span>') + '</div>';
}

function renderWhyNotCited(items) {
  if (!items || !items.length) return '';
  let html = '<section><h2>Why this page isn\'t being cited</h2><div class="wnc-grid">';
  for (const it of items) {
    html +=
      '<div class="wnc">' +
        (it.badge ? '<span class="badge truth">' + escapeHtml(it.badge) + '</span>' : '') +
        '<h3>' + escapeHtml(it.title || '') + '</h3>' +
        '<p>' + escapeHtml(it.body || '') + '</p>' +
      '</div>';
  }
  html += '</div></section>';
  return html;
}

function renderScoreBreakdown(sc) {
  const sec = sc.section_scores || {};
  const order = ['A_technical','B_performance','C_onpage','D_schema',
    'E_aeo_discovery','F_aeo_extraction','G_aeo_trust','H_aeo_selection',
    'I_geo','J_entity'];
  const labels = {
    'A_technical': 'A · Technical SEO',
    'B_performance': 'B · Performance',
    'C_onpage': 'C · On-Page SEO',
    'D_schema': 'D · Schema',
    'E_aeo_discovery': 'E · AEO Discovery',
    'F_aeo_extraction': 'F · AEO Extraction',
    'G_aeo_trust': 'G · AEO Trust',
    'H_aeo_selection': 'H · AEO Selection',
    'I_geo': 'I · GEO',
    'J_entity': 'J · Entity Consistency',
  };
  // Jewel palette (design-principles playbook): cool spine, warm anchors.
  // Each section carries only --g1/--g2 — one component, ten identities.
  const jewels = {
    'A_technical':     ['#6366f1', '#4f46e5'],
    'B_performance':   ['#22d3ee', '#0891b2'],
    'C_onpage':        ['#8b5cf6', '#7c3aed'],
    'D_schema':        ['#38bdf8', '#2563eb'],
    'E_aeo_discovery': ['#14b8a6', '#0d9488'],
    'F_aeo_extraction':['#34d399', '#0d9488'],
    'G_aeo_trust':     ['#f59e0b', '#d97706'],
    'H_aeo_selection': ['#818cf8', '#4338ca'],
    'I_geo':           ['#f43f5e', '#e11d48'],
    'J_entity':        ['#06b6d4', '#0e7490'],
  };
  let rows = '';
  let ti = 0;
  for (const k of order) {
    if (!(k in sec)) continue;
    const v = sec[k];
    const g = jewels[k] || ['#6366f1', '#4f46e5'];
    const letter = k.charAt(0);
    const label = (labels[k] || k).replace(/^[A-J] · /, '');
    const vars = '--g1:' + g[0] + ';--g2:' + g[1] + ';animation-delay:' + (ti++ * 45) + 'ms';
    const orbTop = '<span class="tile-aura"></span>' +
      '<div class="tile-top">' +
        '<span class="tile-orb">' + escapeHtml(letter) + '</span>' +
        '<div class="tile-label">' + escapeHtml(label) + '</div>' +
      '</div>';
    if (v === null || v === undefined) {
      rows += '<div class="tile" style="' + vars + '">' + orbTop +
        '<div class="tile-val na">N/A</div>' +
        '<div class="tile-bar"></div>' +
      '</div>';
    } else {
      const cls = v >= 75 ? 'good' : v >= 50 ? 'warn' : 'bad';
      rows += '<div class="tile ' + cls + '" style="' + vars + '">' + orbTop +
        '<div class="tile-val">' + numOrDash(v) + '</div>' +
        '<div class="tile-bar"><div class="tile-fill" style="width:' + pctWidth(v) + '%"></div></div>' +
      '</div>';
    }
  }
  let extras = '';
  if (sc.brand_ai_presence != null) {
    const bapConf = sc.brand_ai_presence_confidence
      ? ' <span style="color:var(--muted-2)">(' + escapeHtml(String(sc.brand_ai_presence_confidence)) + ' confidence, directional)</span>'
      : '';
    extras += 'Brand AI Presence (BAP): <strong>' + numOrDash(sc.brand_ai_presence) + '</strong>' + bapConf +
      (sc.seo_score != null ? ' · SEO: <strong>' + numOrDash(sc.seo_score) + '</strong>' : '') +
      (sc.aeo_score != null ? ' · AEO: <strong>' + numOrDash(sc.aeo_score) + '</strong>' : '') +
      (sc.citation_readiness != null ? ' · Citation readiness: <strong>' + numOrDash(sc.citation_readiness) + '</strong>' : '');
  }
  return '<section><h2>Score breakdown</h2><div class="tile-grid">' + rows + '</div>' +
    (extras ? '<div class="card score-extras" style="margin-top:14px;padding-top:18px;border-top:1px solid var(--border)">' + extras + '</div>' : '') +
    '</section>';
}

function renderTwoCol(bev, perf, audit) {
  const cvb = bev.content_visible_to_bots || {};
  const pid = bev.page_identity || {};
  const summary = bev.summary || {};

  // Handle multiple possible field-name conventions across agent vs script outputs
  const faqVisible = cvb.faq_visible_pairs ?? cvb.faq_visible ?? summary.faq_visible ?? bev.faq_visible_pairs;
  const faqSchema = cvb.faq_schema_pairs ?? cvb.faq_schema ?? summary.faq_schema ?? bev.faq_schema_pairs;
  const faqStr = (faqVisible !== undefined && faqSchema !== undefined)
    ? faqVisible + ' / ' + faqSchema
    : null;
  const wordCount = cvb.visible_word_count ?? summary.visible_words_default ?? cvb.visible_words ?? bev.visible_word_count;

  const bevRows = [
    ['Visible word count', wordCount],
    ['Schema blocks', cvb.schema_block_count ?? summary.schema_blocks ?? bev.schema_block_count],
    ['FAQ visible / in schema', faqStr],
    ['Title', pid.title || bev.title],
    ['H1', pid.h1_first || bev.h1_first],
    ['Canonical', pid.canonical_tag || bev.canonical || 'none'],
    ['Meta robots', pid.meta_robots || bev.meta_robots || 'none'],
    ['Classification', bev.classification],
  ];
  // Honest CWV labeling: LCP/CLS come from ONE Playwright lab run — label
  // them as such, and never render an INP number (INP needs CrUX field data).
  const hasLabCwv = perf.lcp_ms != null || perf.cls != null;
  const mobParity = perf.mobile_parity || {};
  const perfRows = [
    ['TTFB', perf.ttfb_ms != null ? perf.ttfb_ms + ' ms' : null],
    ['LCP — lab (single run)', perf.lcp_ms != null ? perf.lcp_ms + ' ms' : null],
    ['CLS — lab (single run)', perf.cls != null ? perf.cls.toFixed ? perf.cls.toFixed(3) : perf.cls : null],
    ['INP', hasLabCwv ? 'not measured — requires field data (CrUX)' : null],
    ['Load time', perf.load_time_ms != null ? perf.load_time_ms + ' ms' : null],
    ['Request count', perf.request_count],
    ['SPA framework', (perf.spa_signals || []).join(', ') || 'none detected'],
    ['Console errors', perf.console_errors ? perf.console_errors.length : null],
    ['Mobile parity', mobParity.status ? mobParity.status +
      (mobParity.status === 'na' && mobParity.detail && mobParity.detail.reason
        ? ' (' + mobParity.detail.reason + ')' : '') : null],
  ];

  function tableRows(rows) {
    return rows.filter(r => r[1] !== null && r[1] !== undefined && r[1] !== '')
      .map(r => '<tr><td>' + escapeHtml(r[0]) + '</td><td>' + escapeHtml(String(r[1])) + '</td></tr>')
      .join('');
  }

  const bevHtml = tableRows(bevRows);
  const perfHtml = tableRows(perfRows);

  if (!bevHtml && !perfHtml) return '';

  return '<section><h2>How crawlers see this page</h2><div class="twocol">' +
    (bevHtml ? '<div class="card"><h3 style="font-size:14px;margin-bottom:10px">Bot\'s eye view</h3><table class="stat-table"><tbody>' + bevHtml + '</tbody></table></div>' : '') +
    (perfHtml ? '<div class="card"><h3 style="font-size:14px;margin-bottom:10px">Performance</h3><table class="stat-table"><tbody>' + perfHtml + '</tbody></table></div>' : '') +
    '</div></section>';
}

function renderCompetitors(comps, ourDomain) {
  if (!comps || !comps.length) return '';
  let rows = '';
  for (const c of comps) {
    const isUs = c.domain === ourDomain;
    rows +=
      '<tr' + (isUs ? ' class="target"' : '') + '>' +
        '<td>' + escapeHtml(c.domain || '') + (isUs ? ' (you)' : '') + '</td>' +
        '<td>' + (c.word_count ?? '—') + '</td>' +
        '<td>' + (c.faq_pairs ?? '—') + '</td>' +
        '<td>' + escapeHtml(((c.schema_types || [])).join(', ') || '—') + '</td>' +
        '<td>' + escapeHtml(c.dateModified || '—') + '</td>' +
        '<td>' + escapeHtml(c.author || '—') + '</td>' +
        '<td>' + (c.outbound_links ?? '—') + '</td>' +
      '</tr>';
  }
  return '<section><h2>How you compare to competitors</h2><div class="card tight">' +
    '<table class="comp-table"><thead><tr>' +
      '<th>Domain</th><th>Words</th><th>FAQ</th><th>Schema types</th>' +
      '<th>Modified</th><th>Author</th><th>Outbound</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></div></section>';
}

function renderQuickWins(items) {
  if (!items || !items.length) return '';
  let lis = '';
  for (const it of items) lis += '<li>' + escapeHtml(it) + '</li>';
  return '<section><h2>Quick wins (under 5 min each)</h2><div class="card"><ul class="qw-list">' +
    lis + '</ul></div></section>';
}

function renderSummary_text(s) {
  if (!s) return '';
  return '<section><h2>What to do this week</h2><div class="card" style="line-height:1.65;color:var(--fg-2)">' +
    escapeHtml(s) + '</div></section>';
}

function renderAllFindings(findings) {
  if (!findings || !findings.length) return '';
  // Group: failures first, then warnings, then passes
  const sortKey = f => (f.status === 'fail' ? 0 : f.status === 'warn' ? 1 : 2);
  const sorted = [...findings].sort((a,b) => sortKey(a) - sortKey(b));
  let rows = '';
  for (const f of sorted.slice(0, 120)) {
    const icon = f.status === 'fail' ? '✗' : f.status === 'warn' ? '⚠' : f.status === 'pass' ? '✓' : '·';
    const evTier = f.evidence_tier === 'measured'
      ? '<span class="badge" style="background:#dcfce7;color:#166534">measured</span>'
      : f.evidence_tier === 'llm-judged'
        ? '<span class="badge" style="background:#fef9c3;color:#854d0e">llm-judged</span>' : '—';
    rows +=
      '<tr>' +
        '<td><span class="status-icon ' + escapeHtml(f.status || '') + '">' + icon + '</span></td>' +
        '<td><span class="check-id">' + escapeHtml(f.check_id || '') + '</span></td>' +
        '<td>' + (f.severity ? '<span class="sev ' + escapeHtml(String(f.severity).toLowerCase()) + '">' + escapeHtml(f.severity) + '</span>' : '—') + '</td>' +
        '<td>' + evTier + '</td>' +
        '<td>' + escapeHtml((f.evidence || '').slice(0, 220)) + '</td>' +
        '<td>' + (f.truth_badge ? '<span class="badge truth">' + escapeHtml(f.truth_badge) + '</span>' : '') + '</td>' +
      '</tr>';
  }
  return '<section><details><summary>All findings (' + findings.length + ' checks · click to expand)</summary>' +
    '<table class="findings-table"><thead><tr>' +
      '<th></th><th>Check</th><th>Severity</th><th>Basis</th><th>Evidence</th><th>Truth</th>' +
    '</tr></thead><tbody>' + rows + '</tbody></table></details></section>';
}

function renderBrainSources(findings) {
  // Collect unique citations grouped by tier. Skip citations that have no
  // usable content (no name AND no source_org AND no source_url) — those
  // are the agent emitting partial/reshaped objects.
  const seen = new Set();
  const byTier = {1:[], 2:[], 3:[], 4:[], 5:[]};
  // Entailment-first display: the LLM judgment (post-loop, on the final
  // verbatim text) decides where a citation renders. 'supports' → proof;
  // 'related' → collapsed see-also below the proof tiers; 'unrelated' →
  // hidden here but KEPT in the JSON (never-drop); 'unjudged'/absent →
  // legacy supports_finding behavior.
  const seeAlso = [];
  // BRAIN-MODE DISCLOSURE: which brain answered, and how fresh it is — counted
  // over the SAME deduped set the header total uses (a rule cited by N checks
  // is one source, not N; malformed citations count nowhere).
  let fromLive = 0, fromSnap = 0, maxVer = '';
  for (const f of findings) {
    for (const c of (f.citations || [])) {
      const name = c.name || c.title;
      const hasContent = name || c.source_org || c.source_url;
      if (!hasContent) continue;  // skip empty/malformed
      // Hidden BEFORE dedup so a supporting duplicate of the same rule
      // (judged against a different finding) still renders.
      if (c.entailment === 'unrelated') continue;
      const dedupKey = (c.id ?? name) + ':' + (c.kind || '?');
      if (seen.has(dedupKey)) continue;
      seen.add(dedupKey);
      if (c.from === 'snapshot') fromSnap++; else if (c.from) fromLive++;
      if (c.last_verified && String(c.last_verified) > maxVer) maxVer = String(c.last_verified);
      if (c.entailment === 'related') { seeAlso.push(c); continue; }
      // Citations the grounding step could NOT verify against the brain keep
      // only LLM-claimed fields — never let them claim an authoritative tier.
      const tier = (c.verbatim === false) ? 5 : (c.tier || 5);
      (byTier[tier] = byTier[tier] || []).push(c);
    }
  }
  // Header counts every DISPLAYED source (proof tiers + see-also);
  // hidden 'unrelated' citations were never added to the deduped set.
  const totalCites = seen.size;
  if (!totalCites) return '';
  const tierLabels = {1:'🥇 Tier 1 — Authoritative', 2:'🥈 Tier 2 — Reputable',
                       3:'🥉 Tier 3 — Industry', 4:'📎 Tier 4 — Specialized', 5:'Other'};
  const mode = (fromLive === 0 && fromSnap === 0)
    ? 'from Sieve brain'   // legacy audit predating mode tagging — claim nothing
    : fromSnap === 0
      ? 'live Sieve brain' + (maxVer ? ' · verified through ' + escapeHtml(maxVer) : '')
      : fromLive === 0
        ? 'SNAPSHOT ruleset (2026-04-21) — live brain unavailable'
        : 'MIXED: ' + fromLive + ' live / ' + fromSnap + ' snapshot-fallback';
  const snapBanner = fromSnap > 0
    ? '<div style="background:#fef2f2;border:1px solid #fecaca;color:#991b1b;' +
      'border-radius:8px;padding:10px 14px;margin:0 0 12px 0;font-size:13px">' +
      '⚠ ' + fromSnap + ' source(s) were grounded from the bundled snapshot ' +
      'ruleset (April 2026) — the live brain was unreachable or did not hold ' +
      'them — treat their verified-dates as absent.</div>'
    : '';
  let html = '<section><h2>Sources cited (' + totalCites + ' · ' + mode + ')</h2>' + snapBanner;
  for (const t of [1,2,3,4,5]) {
    if (!byTier[t] || !byTier[t].length) continue;
    // Non-supporting citations sort after supporting ones within their tier
    // (§6: a "related, not proof" cite must never take the top position).
    // A judged 'supports' verdict overrides the lexical supports_finding
    // demotion — the labelled set showed the gate hides ~30% of true proof.
    // Then URL-less citations sort last — a receipt you can click beats a
    // name you have to trust.
    const demoted = (x) => (x.entailment === 'supports') ? 0 : (x.supports_finding === false ? 1 : 0);
    byTier[t].sort((a,b) =>
      (demoted(a) - demoted(b)) ||
      ((a.source_url ? 0 : 1) - (b.source_url ? 0 : 1)));
    html += '<div class="tier t' + t + '"><h3>' + tierLabels[t] + '</h3>';
    for (const c of byTier[t].slice(0, 12)) {
      const kindLabels = {rule:'Rule', ap:'AP', anti_pattern:'AP', principle:'Principle'};
      const kind = kindLabels[c.kind] || 'Item';
      const name = c.name || c.title || '(no name)';
      // Confidence rendered numeric-only: citation fields originate in crawled
      // rule text, so nothing from them is concatenated into HTML unescaped.
      // Guard empty/whitespace strings — Number('') is 0, not NaN.
      const confRaw = c.confidence_score;
      const confNum = (confRaw == null || String(confRaw).trim() === '') ? NaN : Number(confRaw);
      const conf = Number.isFinite(confNum) ? ' (conf ' + confNum.toFixed(2) + ')' : '';
      const risk = c.risk_level ? ' [' + escapeHtml(c.risk_level) + ' risk]' : '';
      const unverified = (c.verbatim === false)
        ? ' <span class="cite-unver">unverified</span>' : '';
      const verified = (c.last_verified && c.verbatim !== false)
        ? ' <span class="cite-ver">· verified ' + escapeHtml(String(c.last_verified)) + '</span>' : '';
      // Only show [#id] if id is actually a usable number
      const idTag = (c.id != null && c.id !== '' && !isNaN(c.id))
        ? ' <code style="font-size:11px">[Sieve ' + kind + ' #' + escapeHtml(String(c.id)) + ']</code>'
        : '';
      const safeUrl = c.source_url ? safeHref(c.source_url) : '';
      const noUrl = !c.source_url
        ? ' <span class="cite-unver" title="no source URL on this rule">no link</span>' : '';
      const provNote = c.url_provenance === 'neighbor-inferred'
        ? ' <span class="cite-unver" title="URL adopted from a similar rule, not the extraction page">inferred link</span>' : '';
      // §6 fallback (unjudged citations only): retrieval could not tie this
      // cite to the finding it decorates — still shown, honestly labeled.
      // A judged 'supports' verdict suppresses the label: the entailment
      // check IS the display decision now, the lexical gate is candidate
      // annotation.
      const related = (c.supports_finding === false && c.entailment !== 'supports')
        ? ' <span class="cite-unver" title="topically related guidance — not direct proof of this finding">related — not direct proof</span>' : '';
      // The rule's own reasoning, verbatim from the brain: when it applies →
      // what to do. Suppressed for unverified citations — their text is
      // LLM-claimed, not brain-backed.
      const why = (c.verbatim !== false) ? (c.if_condition || c.statement || c.description || '') : '';
      const act = (c.verbatim !== false) ? (c.then_action || c.explanation || '') : '';
      const reasoning = (why || act)
        ? '<div class="cite-reason">' +
          (why ? escapeHtml(String(why).slice(0, 240)) : '') +
          (why && act ? ' → ' : '') +
          (act ? escapeHtml(String(act).slice(0, 240)) : '') + '</div>'
        : '';
      html += '<div class="citation">' +
        '<span class="src">' + escapeHtml(c.source_org || 'unknown') + '</span> — ' +
        (safeUrl ? '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' : '') +
        '<span class="nm">' + escapeHtml(name) + '</span>' +
        (safeUrl ? '</a>' : '') +
        conf + risk + idTag + verified + unverified + noUrl + provNote + related + reasoning +
        '</div>';
    }
    html += '</div>';
  }
  // Entailment 'related' verdicts: same-topic guidance that does not prove a
  // specific finding — offered as a collapsed, smaller see-also AFTER the
  // supporting sources, never dressed as proof.
  if (seeAlso.length) {
    let sa = '';
    for (const c of seeAlso.slice(0, 20)) {
      const nm = c.name || c.title || '(no name)';
      const safeUrl = c.source_url ? safeHref(c.source_url) : '';
      sa += '<div class="citation" style="font-size:12px;opacity:.8">' +
        '<span class="src">' + escapeHtml(c.source_org || 'unknown') + '</span> — ' +
        (safeUrl ? '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' : '') +
        '<span class="nm">' + escapeHtml(nm) + '</span>' +
        (safeUrl ? '</a>' : '') + '</div>';
    }
    html += '<details class="see-also" style="margin-top:10px">' +
      '<summary style="font-size:13px;color:#6b7280;cursor:pointer">' +
      'See also — related guidance (' + seeAlso.length + ')</summary>' + sa + '</details>';
  }
  html += '</section>';
  return html;
}

function renderDownloads(id, artifacts) {
  return '<section><h2>Download artifacts</h2><div class="card tight">' +
    '<div class="links">' +
      '<a href="' + (artifacts.json || '/audit/'+id+'/json') + '" target="_blank">Full JSON</a>' +
      '<a href="' + (artifacts.markdown || '/audit/'+id+'/md') + '" target="_blank">Markdown report</a>' +
      '<a href="' + (artifacts.pdf || '/audit/'+id+'/pdf') + '" target="_blank">PDF summary</a>' +
    '</div></div></section>';
}

function formatBlock(text) {
  // Detect code-like content (JSON, HTML, multiline indented blocks) vs prose.
  if (!text) return '';
  const t = String(text);
  const looksCode = /^(\s*[<{]|<\w|<!|```)/m.test(t.trim()) || /\n\s{2,}/.test(t);
  if (looksCode) {
    // Strip ```lang fences if present
    const stripped = t.replace(/^```\w*\n?|\n?```$/g, '').trim();
    return '<pre>' + escapeHtml(stripped) + '</pre>';
  }
  return '<p>' + escapeHtml(t).replace(/\n/g, '<br>') + '</p>';
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Only allow http(s) hrefs. Citation source_url is model/page-derived, so a
// 'javascript:'/'data:' scheme would execute in this origin when clicked.
// Returns a safe href string, or '' when the scheme isn't http(s).
function safeHref(u) {
  try {
    const parsed = new URL(String(u), window.location.origin);
    if (parsed.protocol === 'http:' || parsed.protocol === 'https:') {
      return escapeHtml(parsed.href);
    }
  } catch (e) { /* malformed URL → no link */ }
  return '';
}

// Score values originate from the model and round-trip through the DB. A
// non-numeric value (e.g. '85"><script>...') interpolated into HTML text or a
// style attribute is a stored-XSS vector. numOrDash coerces to a finite number
// (rendered as-is — a number can't carry markup) or a safe em-dash. pctWidth
// additionally clamps to [0,100] for use inside style="width:...%". The server
// also validates scores before persisting (scoring.validate_audit) — this is
// defense in depth at the render layer.
function numOrDash(x) {
  const n = Number(x);
  return Number.isFinite(n) ? String(n) : '—';
}
function pctWidth(x) {
  const n = Number(x);
  if (!Number.isFinite(n)) return '0';
  return String(Math.max(0, Math.min(100, n)));
}

// ---- Slug routing: audits.growthmonk.ai/{domain} ----------------------
// On load, if the path is /{domain}, fetch and render that domain's
// latest persisted audit. Path '/' shows the empty form as normal.

async function loadAuditByDomain(domain) {
  out.innerHTML = '<div class="status-card"><span class="status">' +
    '<span class="spinner"></span>Loading latest audit for ' +
    escapeHtml(domain) + '…</span></div>';
  try {
    const r = await fetch('/api/by-domain/' + encodeURIComponent(domain));
    if (r.status === 404) {
      $('url').value = /^https?:\/\//.test(domain) ? domain : 'https://' + domain;
      out.innerHTML = '<div class="status-card">No saved audit for <strong>' +
        escapeHtml(domain) + '</strong> yet. Click "Run audit" above to create one.' +
        '</div>';
      return;
    }
    if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + (await r.text()));
    const audit = await r.json();
    const id = audit.audit_id;
    renderFull(audit, {artifacts: {
      json: '/audit/' + id + '/json',
      markdown: '/audit/' + id + '/md',
      pdf: '/audit/' + id + '/pdf',
    }}, id);
  } catch (err) {
    out.innerHTML = renderError('Could not load audit for ' +
      escapeHtml(domain) + '\n\n' + escapeHtml(err.message));
  }
}

// ---- Library: grid of past audits on the homepage --------------------

async function loadLibrary() {
  const lib = document.getElementById('library');
  if (!lib) return;
  try {
    const r = await fetch('/api/audits');
    if (!r.ok) return;
    const data = await r.json();
    renderLibrary(data.audits || []);
  } catch (e) { /* library is non-critical — fail silent */ }
}

function renderLibrary(audits) {
  const lib = document.getElementById('library');
  if (!lib) return;
  if (!audits.length) {
    lib.innerHTML =
      '<div class="lib-head"><h2>Recent audits</h2></div>' +
      '<div class="lib-empty">No audits yet — run your first one above. ' +
      'Once an audit completes it appears here, and you can reopen it any ' +
      'time at audits.growthmonk.ai/&lt;domain&gt;.</div>';
    return;
  }
  let cards = '';
  for (const a of audits) {
    const grade = (a.overall_grade || '').charAt(0).toUpperCase();
    const date = (a.created_at || a.audit_date || '').slice(0, 10);
    const meta = [a.page_type, a.industry].filter(Boolean).join(' · ');
    const domAttr = encodeURIComponent(a.domain || '');
    cards +=
      '<div class="lib-card">' +
        '<a class="lib-link" href="/' + domAttr +
          '" aria-label="Open ' + escapeHtml(a.domain || '') + '"></a>' +
        '<button class="lib-del" title="Delete this audit" ' +
          'data-id="' + escapeHtml(a.audit_id || '') + '" ' +
          'data-domain="' + escapeHtml(a.domain || '') + '" ' +
          'onclick="deleteAuditCard(event, this)">✕</button>' +
        '<div class="lib-domain">' + escapeHtml(a.domain || a.url || '—') + '</div>' +
        (meta ? '<div class="lib-sub">' + escapeHtml(meta) + '</div>' : '') +
        '<div class="lib-row">' +
          '<span class="lib-score">' + numOrDash(a.overall_score) + '</span>' +
          '<span class="lib-grade ' + escapeHtml(grade) + '">' +
            escapeHtml(a.overall_grade || '—') + '</span>' +
          '<span class="lib-meta">' +
            (a.findings_count != null ? a.findings_count + ' findings<br>' : '') +
            escapeHtml(date) +
          '</span>' +
        '</div>' +
      '</div>';
  }
  lib.innerHTML =
    '<div class="lib-head"><h2>Recent audits</h2>' +
    '<span class="lib-count">' + audits.length + ' audit' +
    (audits.length === 1 ? '' : 's') + '</span></div>' +
    '<div class="lib-grid">' + cards + '</div>';
}

async function deleteAuditCard(ev, btn) {
  ev.preventDefault();
  ev.stopPropagation();
  const auditId = btn.getAttribute('data-id') || '';
  const domain = btn.getAttribute('data-domain') || 'this domain';
  if (!auditId) { alert('No audit id on this card.'); return; }
  const blockToo = confirm(
    'Delete this audit for ' + domain + '?\\n\\n' +
    'OK = delete just this audit.\\n' +
    'Cancel = stop (nothing is deleted).'
  );
  if (!blockToo) return;
  const alsoBlock = confirm(
    'Also BLOCK ' + domain + ' from being re-audited or re-published?\\n\\n' +
    'OK = delete ALL audits for ' + domain + ' and block it (use for takedown requests).\\n' +
    'Cancel = delete only this one audit.'
  );
  btn.disabled = true;
  btn.textContent = '…';
  try {
    const url = alsoBlock
      ? '/api/audit/by-domain/' + encodeURIComponent(domain) + '?suppress=1'
      : '/api/audit/' + encodeURIComponent(auditId);
    const r = await fetch(url, { method: 'DELETE', credentials: 'same-origin' });
    if (r.status === 200) {
      loadLibrary();
    } else if (r.status === 401) {
      alert('Not authorized to delete. Refresh and sign in again.');
      btn.disabled = false; btn.textContent = '✕';
    } else {
      const t = await r.text();
      alert('Delete failed (' + r.status + '): ' + t.slice(0, 200));
      btn.disabled = false; btn.textContent = '✕';
    }
  } catch (e) {
    alert('Delete error: ' + e.message);
    btn.disabled = false; btn.textContent = '✕';
  }
}

(function init() {
  // Strip leading/trailing slashes from the path. '' => homepage form + library.
  const path = window.location.pathname.replace(/^\/+|\/+$/g, '');
  if (path) {
    // Customer-facing view: hide the internal "Run audit" form and the
    // library — show only the audit itself.
    const form = document.getElementById('f');
    const lib = document.getElementById('library');
    if (form) form.style.display = 'none';
    if (lib) lib.style.display = 'none';
    loadAuditByDomain(decodeURIComponent(path));
  } else {
    loadLibrary();
  }
})();
</script>
</body>
</html>
"""


@app.get('/', response_class=HTMLResponse)
def root(_: bool = Depends(require_auth)):
    return HTMLResponse(INDEX_HTML)


@app.get('/api')
def api_info(_: bool = Depends(require_auth)):
    return {
        'service': 'aeo-seo-auditor',
        'version': '4.0',
        'endpoints': {
            'POST /audit': 'Submit a URL for audit',
            'GET /audit/{id}': 'Fetch status + result',
            'GET /audit/{id}/{json,md,pdf}': 'Fetch specific artifact',
            'GET /healthz': 'Health check',
        },
        'docs': '/docs',
    }


def _brain_ok() -> bool:
    """Verify the brain snapshots load. Imports are module-load-safe."""
    from ranker import BrainIndex
    BrainIndex.from_export_dir(str(THIS_DIR / 'ruleset'))
    return True


@app.get('/healthz')
def healthz():
    """Public liveness check — minimal surface. Returns 200 when the brain
    loads. Does NOT expose auth/config posture (that was a 'service is wide
    open' beacon); the detailed readiness lives behind auth at /readyz."""
    try:
        _brain_ok()
        return {
            'status': 'ok',
            'brain_loaded': True,
            # Public on GitHub already; lets callers verify the live build.
            'git_sha': os.getenv('RAILWAY_GIT_COMMIT_SHA'),
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={'status': 'degraded', 'error': f'{type(e).__name__}: {e}'},
        )


# Cached live-brain stats for /readyz (60s TTL; probing must stay cheap).
_SIEVE_STATS_CACHE = {'at': 0.0, 'value': None}


@app.get('/readyz')
def readyz(_: bool = Depends(require_auth)):
    """Authenticated detailed readiness — config posture, brain stats,
    diagnostics. Gated so it can't be used to fingerprint the deployment."""
    try:
        from ranker import BrainIndex
        stats = BrainIndex.from_export_dir(
            str(THIS_DIR / 'ruleset')).stats()
    except Exception as e:
        return JSONResponse(status_code=503,
                            content={'status': 'degraded', 'error': f'{type(e).__name__}: {e}'})
    # Live sieve brain (the DB the citations actually come from when
    # SIEVE_LIVE=1) — best-effort AND cached (60s TTL) so a slow DB can't
    # stall readiness probes into platform-healthcheck timeouts.
    now = time.time()
    if _SIEVE_STATS_CACHE['value'] is None or now - _SIEVE_STATS_CACHE['at'] > 60:
        try:
            import sieve_brain
            _SIEVE_STATS_CACHE['value'] = sieve_brain.stats()
        except Exception as e:
            _SIEVE_STATS_CACHE['value'] = {'live': False,
                                           'error': f'{type(e).__name__}: {e}'}
        _SIEVE_STATS_CACHE['at'] = now
    return {
        'status': 'ok',
        'brain_stats': stats,
        'sieve_live_stats': _SIEVE_STATS_CACHE['value'],
        'anthropic_key_set': bool(os.getenv('ANTHROPIC_API_KEY')),
        'audit_mode': AUDIT_MODE,
        'agent_available': AGENT_AVAILABLE,
        'agent_import_error': None if AGENT_AVAILABLE else _AGENT_IMPORT_ERROR,
        'web_tools': 'anthropic_native_server_tools',
        'auth_enabled': AUTH_ENABLED,
        'api_key_enabled': API_KEY_ENABLED,
        'is_production': IS_PRODUCTION,
        'supabase_configured': bool(
            os.getenv('SUPABASE_URL') and os.getenv('SUPABASE_SERVICE_KEY')),
        'output_dir': str(OUTPUT_DIR),
        'active_audits': sum(1 for j in JOBS.values()
                             if j.get('status') in ('queued', 'running')),
        'tracked_jobs': len(JOBS),
        'git_sha': os.getenv('RAILWAY_GIT_COMMIT_SHA'),
    }


@app.post('/audit', response_model=AuditResponse)
async def submit_audit(req: AuditRequest, background_tasks: BackgroundTasks,
                        _: bool = Depends(require_auth)):
    """Submit a URL for audit. Returns audit_id immediately; audit runs async.

    Poll GET /audit/{id} for status. Typical completion: 60-120 seconds.
    """
    # Fail closed: in production, a missing auth config must not leave this
    # paid, chromium-spawning endpoint open to the internet.
    if IS_PRODUCTION and not AUTH_ENABLED:
        raise HTTPException(status_code=503,
                            detail='Auth not configured; submission disabled')

    audit_id = str(uuid.uuid4())
    url_str = str(req.url)

    # Suppressed domain — a brand that has objected must not be re-audited.
    if _is_suppressed(url_str):
        raise HTTPException(status_code=403,
                            detail='This domain has been suppressed and cannot be audited.')

    # SSRF guard — refuse internal / metadata / non-http(s) targets.
    safe, reason = check_url_safe(url_str)
    if not safe:
        log.warning('[%s] rejected unsafe url=%s (%s)', audit_id[:8], url_str, reason)
        raise HTTPException(status_code=400, detail=f'URL not allowed: {reason}')

    log.info('[%s] submitted url=%s mode=%s', audit_id[:8], url_str, AUDIT_MODE)

    with JOBS_LOCK:
        _reap_and_evict_locked()
        if _active_audit_count() >= MAX_CONCURRENT_AUDITS:
            raise HTTPException(
                status_code=429,
                detail=f'Too many audits in progress (max {MAX_CONCURRENT_AUDITS}); retry shortly')
        JOBS[audit_id] = {
            'audit_id': audit_id,
            'status': 'queued',
            'url': url_str,
            'started_at': None,
            'completed_at': None,
            'result': None,
            'error': None,
            '_submitted_at': time.time(),
        }

    background_tasks.add_task(_run_audit_background, audit_id, url_str)

    return AuditResponse(
        audit_id=audit_id,
        status='queued',
        message='Audit queued. Poll the audit endpoint for status.',
        poll_url=f'/audit/{audit_id}',
    )


def _maybe_fire_webhook(audit_id: str):
    """If this audit was started with a webhookUrl, POST the compact result
    (or failure payload) to it. Best-effort, no retries — pollers fall back."""
    with JOBS_LOCK:
        job = dict(JOBS.get(audit_id) or {})
    webhook_url = job.get('webhook_url')
    if not webhook_url:
        return
    status_ = job.get('status')
    if status_ == 'completed':
        result = job.get('result') or {}
        payload = _audit_to_compact(result) if isinstance(result, dict) else {}
        payload['status'] = 'complete'
    elif status_ == 'error':
        payload = {
            'status': 'failed',
            'auditId': audit_id,
            'reason': job.get('error') or 'audit failed',
            'agentErrors': job.get('agent_errors') or [],
        }
    else:
        return  # mid-flight — don't fire
    _send_webhook(webhook_url, payload)


def _run_audit_background(audit_id: str, url: str):
    """Background runner — invoked via FastAPI background_tasks."""
    sid = audit_id[:8]
    started = time.time()
    log.info('[%s] mode=%s dispatching url=%s', sid, AUDIT_MODE, url)

    with JOBS_LOCK:
        JOBS[audit_id]['status'] = 'running'
        JOBS[audit_id]['started_at'] = datetime.now(timezone.utc).isoformat()
        # Optional site-wide crawl context (API-start only; sanitized at intake)
        site_context = JOBS[audit_id].get('site_context')
        # API-start callers may opt out of the visibility sweep (they measure
        # it themselves); homepage submissions never set this → False.
        skip_visibility = bool(JOBS[audit_id].get('skip_visibility'))
    if monitoring:
        monitoring.audit_started(audit_id, url, AUDIT_MODE)

    def _update_progress(info: Dict):
        """Bound to this audit_id. Receives {phase, tool, turn, tool_count,
        elapsed_seconds, last_tool_ms} from the agent loop after each tool
        call. Writes into JOBS so the /audit/{id} endpoint can surface it."""
        with JOBS_LOCK:
            if audit_id in JOBS:
                JOBS[audit_id]['progress'] = info

    try:
        result = run_audit(url, output_dir=str(OUTPUT_DIR),
                            progress_callback=_update_progress,
                            site_context=site_context,
                            skip_visibility=skip_visibility)
        elapsed = round(time.time() - started, 1)

        # Agent path returns an error envelope (no exception) when it can't
        # produce a valid audit JSON. Detect that and mark as error so the
        # homepage doesn't think the audit succeeded.
        if isinstance(result, dict) and result.get('error'):
            log.error('[%s] failed in %ss: %s', sid, elapsed, result['error'])
            for ae in (result.get('agent_errors') or [])[:5]:
                log.error('[%s]   agent_error: %s', sid, ae)
            preview = (result.get('raw_final_text_preview') or '')[:300]
            if preview:
                log.error('[%s]   last_text_preview: %s', sid, preview.replace('\n', ' \\n '))
            with JOBS_LOCK:
                JOBS[audit_id]['status'] = 'error'
                JOBS[audit_id]['error'] = result['error']
                JOBS[audit_id]['agent_errors'] = result.get('agent_errors', [])
                JOBS[audit_id]['raw_final_text_preview'] = result.get('raw_final_text_preview', '')
                JOBS[audit_id]['agent_turns'] = result.get('agent_turns')
                JOBS[audit_id]['tool_call_count'] = result.get('tool_call_count')
                JOBS[audit_id]['input_tokens'] = result.get('input_tokens')
                JOBS[audit_id]['output_tokens'] = result.get('output_tokens')
                JOBS[audit_id]['completed_at'] = datetime.now(timezone.utc).isoformat()
            if monitoring:
                monitoring.audit_failed(audit_id, url, result.get('error', 'agent error'), elapsed)
            _persist_job_status(audit_id)
            _maybe_fire_webhook(audit_id)
            return

        score = (result.get('scoring') or {}).get('overall_score') if isinstance(result, dict) else None
        grade = (result.get('scoring') or {}).get('overall_grade') if isinstance(result, dict) else None
        log.info('[%s] completed in %ss score=%s grade=%s', sid, elapsed, score, grade)
        with JOBS_LOCK:
            JOBS[audit_id]['status'] = 'completed'
            JOBS[audit_id]['completed_at'] = datetime.now(timezone.utc).isoformat()
            JOBS[audit_id]['result'] = result
        if monitoring and isinstance(result, dict):
            monitoring.audit_completed(result)
        _persist_job_status(audit_id)
        _maybe_fire_webhook(audit_id)
    except Exception as e:
        elapsed = round(time.time() - started, 1)
        # Full traceback to stdout — Railway captures it in the log viewer.
        log.error('[%s] background task crashed in %ss: %s: %s',
                  sid, elapsed, type(e).__name__, e)
        log.error('[%s] traceback:\n%s', sid, traceback.format_exc())
        with JOBS_LOCK:
            JOBS[audit_id]['status'] = 'error'
            JOBS[audit_id]['error'] = f'{type(e).__name__}: {e}'
            JOBS[audit_id]['traceback'] = traceback.format_exc()
            JOBS[audit_id]['completed_at'] = datetime.now(timezone.utc).isoformat()
        if monitoring:
            monitoring.audit_failed(audit_id, url, f'{type(e).__name__}: {e}', elapsed)
        _persist_job_status(audit_id)
        _maybe_fire_webhook(audit_id)


def _persist_job_status(audit_id: str) -> None:
    """Write-through a job's terminal status to Supabase so the RECORD survives
    a redeploy even though the in-memory JOBS entry won't. Best-effort."""
    with JOBS_LOCK:
        job = dict(JOBS.get(audit_id) or {})
    if not job:
        return
    result = job.get('result') or {}
    summary = None
    if isinstance(result, dict):
        scoring = result.get('scoring') or {}
        summary = {
            'overall_score': scoring.get('overall_score'),
            'overall_grade': scoring.get('overall_grade'),
            'domain': result.get('domain'),
        }
    try:
        save_job_status({
            'audit_id': audit_id,
            'url': job.get('url'),
            'status': job.get('status'),
            'error': job.get('error'),
            'submitted_at': job.get('_submitted_at_iso') or job.get('started_at'),
            'completed_at': job.get('completed_at'),
            'result_summary': summary,
        })
    except Exception:
        pass


@app.get('/audit/{audit_id}', response_model=AuditStatusResponse)
def get_audit(audit_id: str, _: bool = Depends(require_auth)):
    """Fetch audit status + summary. Poll until status == 'completed'."""
    with JOBS_LOCK:
        job = JOBS.get(audit_id)
    if not job:
        raise HTTPException(status_code=404, detail='audit not found')

    response = {
        'audit_id': audit_id,
        'status': job['status'],
        'started_at': job.get('started_at'),
        'completed_at': job.get('completed_at'),
    }

    # Live progress info (only meaningful while status == 'running')
    if job.get('progress'):
        response['progress'] = job['progress']

    if job['status'] == 'error':
        response['error'] = job.get('error')
        # Surface agent diagnostic fields if present (set by _run_audit_background
        # when the agent returned an error envelope). These are critical for
        # debugging why the agent failed to produce valid output.
        for k in ('agent_errors', 'raw_final_text_preview', 'agent_turns',
                  'tool_call_count', 'input_tokens', 'output_tokens'):
            if k in job:
                response[k] = job[k]
        return response

    if job['status'] == 'completed' and job.get('result'):
        result = job['result']
        response['duration_seconds'] = result.get('duration_seconds')

        # Compact summary — full data via .json artifact
        response['result_summary'] = {
            'url': result.get('url'),
            'domain': result.get('domain'),
            'page_type': result.get('classification', {}).get('page_type'),
            'industry': result.get('classification', {}).get('industry'),
            'overall_score': result.get('scoring', {}).get('overall_score'),
            'overall_grade': result.get('scoring', {}).get('overall_grade'),
            'section_scores': result.get('scoring', {}).get('section_scores'),
            'findings_count': len(result.get('findings', [])),
            'executive_diagnosis': result.get('narrative', {}).get('executive_diagnosis'),
        }

        response['artifacts'] = {
            'json': f'/audit/{audit_id}/json',
            'markdown': f'/audit/{audit_id}/md',
            'pdf': f'/audit/{audit_id}/pdf',
        }

    return response


# Artifact endpoints use /audit/{id}/{format} (slash separator) to avoid
# greedy-matching with the bare /audit/{id} route.
@app.get('/audit/{audit_id}/json')
def get_audit_json(audit_id: str):
    """Full audit JSON — PUBLIC so customers can download their report.
    Serves the on-disk file if the audit is still in memory; otherwise
    falls back to Supabase (so old audit_ids survive Railway redeploys)."""
    with JOBS_LOCK:
        job = JOBS.get(audit_id)
    if job and job['status'] == 'completed':
        json_path = job['result'].get('json_path')
        if json_path and Path(json_path).exists():
            return FileResponse(json_path, media_type='application/json',
                                 filename=f'{audit_id}.json')
        # In memory but the ephemeral file is gone — serve the dict directly.
        return JSONResponse(job['result'])
    # Not in memory — reload from Supabase.
    audit = fetch_audit(audit_id=audit_id)
    if audit:
        return JSONResponse(audit)
    raise HTTPException(status_code=404, detail='audit not found')


@app.get('/audit/{audit_id}/md')
def get_audit_markdown(audit_id: str):
    """Markdown audit report — PUBLIC for customer download. Serves the on-disk
    artifact if present; otherwise regenerates it from the persisted audit JSON
    so the link survives redeploys identically to /json (no inconsistent 404)."""
    with JOBS_LOCK:
        job = JOBS.get(audit_id)
    if job and job['status'] == 'completed':
        md_path = job['result'].get('md_path')
        if md_path and Path(md_path).exists():
            return FileResponse(md_path, media_type='text/markdown',
                                 filename=f'{audit_id}.md')
    # Artifact gone or job evicted — rebuild from Supabase.
    md_text = regenerate_markdown(audit_id)
    if md_text is not None:
        return PlainTextResponse(md_text, media_type='text/markdown')
    raise HTTPException(status_code=404, detail='audit not found')


@app.get('/audit/{audit_id}/pdf')
def get_audit_pdf(audit_id: str):
    """1-page PDF summary — PUBLIC for customer download. Regenerates from the
    persisted audit JSON when the local artifact is gone (redeploy-safe)."""
    with JOBS_LOCK:
        job = JOBS.get(audit_id)
    if job and job['status'] == 'completed':
        pdf_path = job['result'].get('pdf_path')
        if pdf_path and Path(pdf_path).exists():
            return FileResponse(pdf_path, media_type='application/pdf',
                                 filename=f'{audit_id}.pdf')
    # Rebuild from Supabase into the output dir.
    regenerated = regenerate_pdf(audit_id, out_dir=OUTPUT_DIR)
    if regenerated and Path(regenerated).exists():
        return FileResponse(str(regenerated), media_type='application/pdf',
                             filename=f'{audit_id}.pdf')
    raise HTTPException(
        status_code=404,
        detail='PDF not available (audit not found or no PDF renderer on host)'
    )


@app.get('/audits')
def list_audits(_: bool = Depends(require_auth)):
    """List all audits in this service instance (in-memory only, lost on restart)."""
    with JOBS_LOCK:
        return {
            'count': len(JOBS),
            'audits': [
                {
                    'audit_id': a['audit_id'],
                    'url': a['url'],
                    'status': a['status'],
                    'started_at': a.get('started_at'),
                    'completed_at': a.get('completed_at'),
                }
                for a in JOBS.values()
            ],
        }


@app.post('/debug/persist-test')
def debug_persist_test(_: bool = Depends(require_auth)):
    """Diagnostic — write a synthetic dummy audit via persist_audit() to
    verify Supabase connectivity from THIS container, without running a
    full (~$2, ~7min) audit.

    Returns the persist result + non-secret env diagnostics. The test row
    has a recognizable 'persist-test-*' audit_id and can be cleaned up
    afterwards. Gated behind auth like every other route.
    """
    try:
        from tools import persist_audit
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={'error': f'cannot import persist_audit: {e}'})

    test_id = f'persist-test-{uuid.uuid4().hex[:8]}'
    now = datetime.now(timezone.utc)
    dummy = {
        'audit_id': test_id,
        'url': 'https://persist-test.example.com/',
        'domain': 'persist-test.example.com',
        'date': now.strftime('%Y-%m-%d'),
        'classification': {'page_type': 'homepage', 'industry': 'saas',
                            'company_name': 'Persist Test', 'confidence': 'high'},
        'context': {'competitors': ['example-a.com'],
                    'test_queries': {'primary': 'persistence test'}},
        'gates': {'crawlability': 'pass', 'content_access': 'pass',
                  'page_existence': 'pass'},
        'scoring': {'section_scores': {'A_technical': 100, 'B_performance': 100},
                    'page_citation_readiness': 99, 'brand_ai_presence': 99,
                    'seo_score': 99, 'aeo_score': 99, 'citation_readiness': 99,
                    'overall_score': 99, 'overall_grade': 'A+'},
        'narrative': {'executive_diagnosis': 'Synthetic persistence test row — '
                                             'safe to delete.'},
        'competitor_comparison': [],
        'bots_eye_view': {'classification': 'fully_accessible'},
        'performance': {'ttfb_ms': 1},
        'supplementary_findings': [],
        'metadata': {'version': 'persist-test'},
        'duration_seconds': 0.0,
        'findings': [
            {'check_id': 'TEST_A1', 'section': 'A', 'status': 'pass',
             'severity': 'info', 'evidence': 'Synthetic finding for persist test.',
             'truth_badge': 'HARD EVIDENCE', 'fix_type': None,
             'citations': [{'id': 1, 'kind': 'rule', 'tier': 1,
                            'source_org': 'TestOrg'}]},
        ],
    }
    result = persist_audit(dummy)

    # Non-secret env diagnostics — URL is not a secret; key shown only as
    # prefix + length so a wrong-key paste is visible without leaking it.
    surl = os.getenv('SUPABASE_URL', '')
    skey = os.getenv('SUPABASE_SERVICE_KEY', '')
    return {
        'test_audit_id': test_id,
        'persist_result': result,
        'env_diagnostics': {
            'SUPABASE_URL': surl or '(unset)',
            'SUPABASE_SERVICE_KEY_set': bool(skey),
            'SUPABASE_SERVICE_KEY_prefix': (skey[:10] + '...') if skey else None,
            'SUPABASE_SERVICE_KEY_length': len(skey),
        },
        'hint': 'If persisted=true, the integration works. If false, read '
                'persist_result.error. A service key should start with '
                '"sb_secret_" or "eyJ" — anything else is the wrong key.',
    }


# ----------------------------------------------------------------------
# AUDIT RELOAD — fetch persisted audits by domain or id (from Supabase)
# ----------------------------------------------------------------------

@app.get('/api/by-domain/{domain:path}')
def api_by_domain(domain: str):
    """Latest persisted audit for a domain, reassembled into the full
    audit-JSON shape the homepage renderer consumes.

    PUBLIC — powers the customer-facing /{domain} page. Customers visit
    the share link, the page JS calls this endpoint to fetch the audit."""
    audit = fetch_audit(domain=domain)
    if not audit:
        raise HTTPException(status_code=404,
                            detail=f'no persisted audit for domain: {domain}')
    return JSONResponse(audit)


@app.get('/api/by-id/{audit_id}')
def api_by_id(audit_id: str):
    """A specific audit by id — PUBLIC for shareable specific-run links.
    Checks in-memory JOBS first (freshest), then falls back to Supabase
    (survives redeploys)."""
    with JOBS_LOCK:
        job = JOBS.get(audit_id)
    if job and job.get('result') and not job['result'].get('error'):
        return JSONResponse(job['result'])
    audit = fetch_audit(audit_id=audit_id)
    if not audit:
        raise HTTPException(status_code=404, detail='audit not found')
    return JSONResponse(audit)


@app.get('/api/history/{domain:path}')
def api_history(domain: str):
    """Compact list of past audits for a domain (newest first).
    PUBLIC — for the "previous audits" section on customer-facing pages."""
    return {'domain': domain, 'audits': list_audits_for_domain(domain)}


@app.get('/api/audits')
def api_audits(_: bool = Depends(require_auth)):
    """All persisted audits, newest first — powers the homepage library grid."""
    return {'audits': list_all_audits()}


# ======================================================================
# PROGRAMMATIC API — for server-to-server integrations (AnswerMonk, etc.)
# All routes here require X-API-Key (set as AUDIT_API_KEY in Railway).
# Clean contract: 3 endpoints, idempotent start, never-lies status, compact
# result + ?full=1 escape hatch, optional webhook on completion.
# ======================================================================

# Section letter → human-readable category, for compact result mapping
_SECTION_LABELS = {
    'A': 'Technical SEO', 'B': 'Performance', 'C': 'On-Page SEO',
    'D': 'Schema', 'E': 'AEO Discovery', 'F': 'AEO Extraction',
    'G': 'AEO Trust', 'H': 'AEO Selection', 'I': 'GEO',
    'J': 'Entity Consistency',
}

# Idempotency window — same URL within this many seconds returns same audit_id
_IDEMPOTENCY_WINDOW_SECONDS = 60


class StartAuditRequest(BaseModel):
    # Accept both the wire spelling AnswerMonk sends (siteContext, matching
    # webhookUrl's camelCase) and the pythonic site_context.
    model_config = ConfigDict(populate_by_name=True)

    url: str
    webhookUrl: Optional[str] = None
    # OPTIONAL site-wide crawl context for the audited page (roadmap 1.4):
    # { orphan?: bool, clickDepth?: int, duplicateOf?: str, inLinks?: int }.
    # Validated leniently via sanitize_site_context — unknown keys and junk
    # values are dropped, and an unusable payload degrades to None (identical
    # to the field being absent). Narrative-only; never feeds scoring.
    site_context: Optional[Dict[str, Any]] = Field(default=None,
                                                   alias='siteContext')
    # Callers that measure AI visibility themselves (AnswerMonk probes the
    # same engines in its scoring phase) set this to skip the auditor's
    # post-loop visibility sweep — identical audit otherwise, ~30% cheaper.
    skip_visibility: bool = Field(default=False, alias='skipVisibility')


class StartAuditResponse(BaseModel):
    auditId: str
    estimatedSeconds: int
    reused: bool


def _normalize_url(raw: str) -> str:
    """Auto-prepend https:// when a bare domain is passed (e.g. 'feelvaleo.com')."""
    s = (raw or '').strip()
    if not s:
        return s
    if s.startswith('//'):
        return 'https:' + s
    if not s.lower().startswith(('http://', 'https://')):
        return 'https://' + s
    return s


def _find_recent_audit_for_url(url: str) -> Optional[str]:
    """Idempotency check — return an existing audit_id for `url` if one was
    submitted within the last _IDEMPOTENCY_WINDOW_SECONDS seconds and is
    queued, running, or recently completed. Otherwise None.

    Checks in-memory JOBS first (covers in-flight audits), then Supabase
    (covers completed audits that finished within the window)."""
    cutoff = time.time() - _IDEMPOTENCY_WINDOW_SECONDS
    with JOBS_LOCK:
        for aid, j in JOBS.items():
            if j.get('url') != url:
                continue
            started = j.get('_submitted_at') or 0
            if started < cutoff:
                continue
            # Skip outright failures — let the caller try again
            if j.get('status') == 'error':
                continue
            return aid
    # Supabase fallback for completed audits within the window
    try:
        domain = re.sub(r'^https?://', '', url).rstrip('/').split('/')[0]
        recents = list_audits_for_domain(domain, limit=3)
        from datetime import datetime as _dt
        for r in recents:
            created = r.get('created_at') or ''
            if not created:
                continue
            try:
                # Postgres returns ISO with offset; parse leniently
                ts = _dt.fromisoformat(created.replace('Z', '+00:00')).timestamp()
            except Exception:
                continue
            if ts >= cutoff:
                return r.get('audit_id')
    except Exception:
        pass
    return None


def _derive_progress_pct(job: Dict) -> int:
    """Rough 0–100 progress for status polling. Based on tool_count;
    typical complete audit emits ~28 tool calls."""
    status_ = job.get('status', 'queued')
    if status_ == 'completed':
        return 100
    if status_ == 'error':
        return 0
    if status_ == 'queued':
        return 2
    p = (job.get('progress') or {})
    tc = p.get('tool_count') or 0
    return max(5, min(95, int(tc / 30 * 95)))


def _public_status(job: Dict, in_supabase: bool) -> str:
    """Map internal JOBS status to the public 5-state enum."""
    s = job.get('status', 'queued') if job else None
    if s == 'queued':    return 'pending'
    if s == 'running':   return 'running'
    if s == 'completed': return 'complete'
    if s == 'error':     return 'failed'
    if in_supabase:      return 'complete'  # persisted; in-mem evicted
    return 'lost'


def _slim_citation(c: Dict) -> Optional[Dict]:
    """Trim a full finding citation to the fields a consumer needs to render a
    sourced receipt. Phase 5: the compact contract used to drop citations
    entirely, so AnswerMonk's technical-audit card could never show a source."""
    if not isinstance(c, dict):
        return None
    return {
        'kind': c.get('kind'), 'id': c.get('id'),
        'name': c.get('name'),
        'sourceOrg': c.get('source_org'), 'sourceUrl': c.get('source_url'),
        'tier': c.get('tier'), 'tierIcon': c.get('tier_icon'),
        'confidence': c.get('confidence_score'),
        'from': c.get('from'), 'freshness': c.get('freshness'),
        'snapshotDate': c.get('snapshot_date'),
        'lastVerified': c.get('last_verified'),
        'grounded': c.get('grounded'), 'verbatim': c.get('verbatim'),
        'supportsFinding': c.get('supports_finding'),  # §6: lexical CANDIDATE annotation (unjudged fallback)
        # Display verdict from the post-loop LLM entailment judge:
        # 'supports' (proof) | 'related' (see-also) | 'unrelated' (hide) |
        # 'unjudged'/None (legacy supports_finding behavior).
        'entailment': c.get('entailment'),
    }


def _slim_bound_rule(br: Dict) -> Optional[Dict]:
    if not isinstance(br, dict):
        return None
    return {
        'kind': br.get('kind'), 'id': br.get('id'), 'name': br.get('name'),
        'sourceOrg': br.get('source_org'), 'sourceUrl': br.get('source_url'),
        'confidence': br.get('confidence_score'),
        'bindingVerified': br.get('binding_verified'),
        'basis': br.get('basis'), 'reason': br.get('reason'),
    }


def _audit_to_compact(audit: Dict, request: Optional[Request] = None) -> Dict:
    """Map the full audit dict to the compact result shape AnswerMonk
    consumes for the third-segment card."""
    if not audit:
        return {}
    scoring = audit.get('scoring', {}) or {}
    findings = audit.get('findings', []) or []
    failed_or_warn = [f for f in findings if f.get('status') in ('fail', 'warn')]
    counts = {'critical': 0, 'high': 0, 'medium': 0, 'low': 0}
    for f in failed_or_warn:
        sev = (f.get('severity') or 'medium').lower()
        if sev in counts:
            counts[sev] += 1

    # Top issues sorted: critical → high → medium → low; fail before warn
    sev_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4}
    sorted_issues = sorted(failed_or_warn, key=lambda f: (
        0 if f.get('status') == 'fail' else 1,
        sev_rank.get((f.get('severity') or 'medium').lower(), 5),
    ))

    # Index top fixes by check_id for quicker "fix" hint lookup
    narrative = audit.get('narrative', {}) or {}
    fixes_by_topic = {}
    for fx in (narrative.get('top_5_fixes') or []):
        title_lc = (fx.get('title') or '').lower()
        fixes_by_topic[title_lc] = (fx.get('after') or fx.get('title') or '')[:200]

    issues = []
    for f in sorted_issues[:20]:
        section_letter = (f.get('section') or (f.get('check_id') or '')[:1]).upper()
        evidence = f.get('evidence') or ''
        # Title = first sentence/clause of evidence, capped
        title = evidence.split('.')[0][:120] or f.get('check_id', 'Issue')
        # §2: the finding's own executable fix (model-authored, or the runtime
        # backstop's narrative join) wins; the keyword hint below survives only
        # for audits persisted before the field existed.
        fix_hint = None
        own_fix = f.get('fix')
        if isinstance(own_fix, str) and own_fix.strip():
            fix_hint = own_fix.strip()[:500]
        if not fix_hint:
            check_id_lc = (f.get('check_id') or '').lower()
            for topic, after in fixes_by_topic.items():
                if any(tok in topic for tok in check_id_lc.split('_') if len(tok) > 3):
                    fix_hint = (after.split('.')[0])[:160]
                    break
        if not fix_hint:
            fix_hint = (f.get('fix_type') or '').replace('_', ' ').title() or None

        citations = [s for c in (f.get('citations') or [])[:3]
                     if (s := _slim_citation(c)) is not None]
        issues.append({
            'severity': f.get('severity'),
            'status': f.get('status'),  # additive: 'fail' | 'warn' — without it the
                                        # consumer must guess and warns get dressed
                                        # as fails in AnswerMonk's external_audits
            'category': _SECTION_LABELS.get(section_letter, section_letter or 'General'),
            'title': title,
            'fix': fix_hint,
            'checkId': f.get('check_id'),
            'evidenceTier': f.get('evidence_tier'),  # additive: 'measured' | 'llm-judged' | None (legacy audits)
            'vocabStatus': f.get('vocab_status'),    # §1: 'canonical' | 'aliased' | 'foreign' | None
            'originalCheckId': f.get('original_check_id'),  # §1: pre-rename id, present iff renamed
            'citations': citations,                  # Phase 5: sourced receipts (was dropped here)
            'boundRule': _slim_bound_rule(f.get('bound_rule')),  # the verified rule this verdict binds to
            'observed': f.get('observed'),           # D25/D27: the 'on YOUR page' proof half
        })

    total = len(failed_or_warn)
    crit_high = counts['critical'] + counts['high']
    summary = (f"{total} issue{'s' if total != 1 else ''} found — "
               f"{crit_high} critical or high severity")

    # Brain-mode disclosure over the emitted citations: was guidance grounded
    # from the live brain, the bundled snapshot, or a mix? (Path A previously
    # carried no provenance at all.)
    froms = {c.get('from') for iss in issues for c in iss['citations'] if c.get('from')}
    snap_dates = {c.get('snapshotDate') for iss in issues for c in iss['citations']
                  if c.get('from') == 'snapshot' and c.get('snapshotDate')}
    if not froms:
        sources_mode = 'none'
    elif froms == {'sieve-live'}:
        sources_mode = 'live'
    elif froms == {'snapshot'}:
        sources_mode = 'snapshot'
    else:
        sources_mode = 'mixed'

    # SHADOW dual-score (nullable) — evidence-weighted twin of the classic PCR.
    # Reloaded audits carry it in metadata.scoring_shadow (fetch_audit rebuilds
    # scoring from flat DB columns, which drops scoring.shadow). BOTH copies go
    # through scoring.clamp_shadow here: the metadata mirror never passed
    # validate_audit, and even the scoring copy could have been edited since —
    # nothing unclamped may reach the JSON API. Clamp a copy, not the audit.
    from scoring import clamp_shadow
    shadow = scoring.get('shadow')
    if not isinstance(shadow, dict):
        shadow = (audit.get('metadata') or {}).get('scoring_shadow')
    shadow = clamp_shadow(dict(shadow)) if isinstance(shadow, dict) else None
    shadow_score = ({'score': shadow.get('pcr_evidence'),
                     'grade': shadow.get('grade_evidence'),
                     'coverage': shadow.get('coverage')}
                    if isinstance(shadow, dict) else None)

    domain = audit.get('domain') or ''
    # Build full report URL — prefer the request's host if available
    if request is not None:
        scheme = request.url.scheme
        netloc = request.url.netloc
        # Behind the Railway/CDN edge the app terminates plain HTTP, so the
        # request scheme reads 'http' unless uvicorn trusts X-Forwarded-Proto
        # (--proxy-headers). Belt-and-braces: any non-local host is https —
        # an http:// fullReportUrl breaks as mixed content in consumers.
        host = (getattr(request.url, 'hostname', None)
                or netloc.rsplit(':', 1)[0]).strip('[]').lower()
        if scheme == 'http' and host not in ('localhost', '127.0.0.1', '::1', '0.0.0.0'):
            scheme = 'https'
        full_url = f"{scheme}://{netloc}/{domain}" if domain else None
    else:
        full_url = f"/{domain}" if domain else None

    return {
        'status': 'complete',
        'auditId': audit.get('audit_id'),
        'url': audit.get('url'),
        'domain': domain,
        'score': scoring.get('overall_score'),
        'grade': scoring.get('overall_grade'),
        'pageCitationReadiness': scoring.get('page_citation_readiness'),
        'brandAiPresence': scoring.get('brand_ai_presence'),
        'shadowScore': shadow_score,  # nullable {score, grade, coverage} — shadow only, never the grade

        'summary': summary,
        'severityCounts': counts,
        'issues': issues,
        'executiveDiagnosis': narrative.get('executive_diagnosis'),
        'sourcesMode': sources_mode,   # 'live' | 'snapshot' | 'mixed' | 'none'
        'snapshotDate': sorted(snap_dates)[-1] if snap_dates else None,
        'fullReportUrl': full_url,
        'completedAt': audit.get('date') or audit.get('created_at'),
        'durationSeconds': audit.get('duration_seconds'),
    }


WEBHOOK_SECRET = os.getenv('AUDIT_WEBHOOK_SECRET', '')
WEBHOOK_MAX_ATTEMPTS = int(os.getenv('WEBHOOK_MAX_ATTEMPTS', '3'))


def _send_webhook(webhook_url: str, payload: Dict) -> None:
    """Signed, retried webhook POST. Never raises.

    - Signs the exact JSON body with HMAC-SHA256 (X-Auditor-Signature:
      sha256=<hex>) when AUDIT_WEBHOOK_SECRET is set, so the receiver can verify
      origin + integrity (SEC-6 — receivers previously could not tell a real
      callback from a forged one).
    - Retries transient failures with backoff instead of a single best-effort
      shot (durable delivery)."""
    import hashlib
    import hmac
    import json as _json
    try:
        import httpx
    except Exception:
        return

    body = _json.dumps(payload, default=str, ensure_ascii=False).encode('utf-8')
    headers = {
        'User-Agent': 'growthmonk-auditor/1.0',
        'Content-Type': 'application/json',
    }
    if WEBHOOK_SECRET:
        sig = hmac.new(WEBHOOK_SECRET.encode('utf-8'), body, hashlib.sha256).hexdigest()
        headers['X-Auditor-Signature'] = f'sha256={sig}'

    for attempt in range(WEBHOOK_MAX_ATTEMPTS):
        try:
            with httpx.Client(timeout=10.0) as client:
                r = client.post(webhook_url, content=body, headers=headers)
            log.info('webhook → %s status=%d audit_id=%s attempt=%d',
                     webhook_url, r.status_code, payload.get('auditId', ''),
                     attempt + 1)
            if r.status_code < 500:
                return  # delivered (or a client error the receiver owns)
        except Exception as e:
            log.warning('webhook → %s attempt %d failed: %s',
                        webhook_url, attempt + 1, e)
        if attempt < WEBHOOK_MAX_ATTEMPTS - 1:
            time.sleep(2 ** attempt)


@app.post('/api/audit/start', response_model=StartAuditResponse)
async def api_audit_start(req: StartAuditRequest,
                           background_tasks: BackgroundTasks,
                           request: Request,
                           _: bool = Depends(require_api_key)):
    """Server-to-server audit trigger.

    Body: {url, webhookUrl?}
    Auth: X-API-Key
    Returns: {auditId, estimatedSeconds, reused}

    Idempotent: same URL submitted within 60s returns the same auditId
    with reused=true (prevents double-billing on retries / double-clicks).

    If webhookUrl is provided, the auditor will POST the compact result
    (or failure payload) to that URL when the audit settles. The webhook
    is best-effort — clients should still implement polling as a fallback."""
    # Fail closed in production when the API key isn't configured.
    if IS_PRODUCTION and not API_KEY_ENABLED:
        raise HTTPException(status_code=503,
                            detail='API key not configured; endpoint disabled')

    url = _normalize_url(req.url)
    if not url or '.' not in url:
        raise HTTPException(status_code=400,
                            detail='Invalid url — must be a domain or full URL')

    # Suppressed domain — refuse to (re-)audit a brand that has objected.
    if _is_suppressed(url):
        raise HTTPException(status_code=403,
                            detail='This domain has been suppressed and cannot be audited.')

    # SSRF guard for the audit target.
    safe, reason = check_url_safe(url)
    if not safe:
        log.warning('rejected unsafe api url=%s (%s)', url, reason)
        raise HTTPException(status_code=400, detail=f'URL not allowed: {reason}')

    # SSRF guard for the webhook callback — an API-key holder must not be able
    # to make the server POST to internal services.
    if req.webhookUrl:
        wsafe, wreason = check_url_safe(req.webhookUrl)
        if not wsafe:
            raise HTTPException(status_code=400,
                                detail=f'webhookUrl not allowed: {wreason}')

    # Idempotency first — a retry/double-click for an in-flight URL returns the
    # existing audit and must NOT be rejected by the concurrency gate below.
    existing = _find_recent_audit_for_url(url)
    if existing:
        log.info('[%s] idempotency hit for url=%s', existing[:8], url)
        return StartAuditResponse(auditId=existing,
                                   estimatedSeconds=180, reused=True)

    with JOBS_LOCK:
        _reap_and_evict_locked()
        if _active_audit_count() >= MAX_CONCURRENT_AUDITS:
            raise HTTPException(
                status_code=429,
                detail=f'Too many audits in progress (max {MAX_CONCURRENT_AUDITS}); retry shortly')

    # Metering / quota — charge only genuinely-new audits (idempotency hits above
    # already returned). Fails open on infra error, closed on confirmed quota.
    if billing is not None:
        decision = billing.check_and_meter(
            request.headers.get('X-API-Key', ''),
            now_iso=datetime.now(timezone.utc).isoformat(),
        )
        if not decision.get('allowed'):
            raise HTTPException(status_code=402,
                                detail=f"Quota exceeded: {decision.get('reason')}")

    audit_id = str(uuid.uuid4())
    site_context = sanitize_site_context(req.site_context)
    log.info('[%s] api/start url=%s webhook=%s site_context=%s', audit_id[:8], url,
             '(yes)' if req.webhookUrl else '(no)',
             ','.join(sorted(site_context)) if site_context else '(none)')
    with JOBS_LOCK:
        JOBS[audit_id] = {
            'audit_id': audit_id,
            'status': 'queued',
            'url': url,
            'started_at': None,
            'completed_at': None,
            'result': None,
            'error': None,
            'webhook_url': req.webhookUrl,
            'site_context': site_context,
            'skip_visibility': bool(req.skip_visibility),
            'submitted_via': 'api',
            '_submitted_at': time.time(),
        }
    background_tasks.add_task(_run_audit_background, audit_id, url)
    return StartAuditResponse(auditId=audit_id, estimatedSeconds=180, reused=False)


@app.get('/api/audit/{audit_id}/delta')
def api_audit_delta(audit_id: str, vs: Optional[str] = None,
                    _: bool = Depends(require_api_key)):
    """Fix-verification / re-score delta — THE product loop.

    Diffs this audit against a prior one of the same domain by check_id and
    returns what resolved / regressed / newly appeared / still open, plus the
    score change. This is the ROI artifact ("62 → 81, 7 resolved").

    Query param `vs` optionally pins a specific prior audit_id; otherwise the
    most recent earlier audit for the same domain is used.
    """
    # Prefer the in-memory result, else reload the persisted audit.
    current = None
    with JOBS_LOCK:
        job = JOBS.get(audit_id)
    if job and job.get('status') == 'completed' and isinstance(job.get('result'), dict):
        current = job['result']
    if current is None:
        current = fetch_audit(audit_id=audit_id)
    if current is None:
        raise HTTPException(status_code=404, detail='audit not found')

    result = delta_against_prior(current, prior_audit_id=vs)
    if result is None:
        return {
            'audit_id': audit_id,
            'delta': None,
            'message': 'No prior audit for this domain to compare against. '
                       'Re-audit after applying fixes to see movement.',
        }
    return {'audit_id': audit_id, 'delta': result}


@app.get('/api/audit/{audit_id}/status')
def api_audit_status(audit_id: str, _: bool = Depends(require_api_key)):
    """Status of an audit — always returns 200 with one of 5 statuses:
    pending | running | complete | failed | lost.

    'lost' = the audit_id is unknown to both in-memory state AND Supabase.
    This is the state your polling code should treat as 'give up silently'
    (e.g., a Railway redeploy wiped state before Supabase captured it)."""
    with JOBS_LOCK:
        job = dict(JOBS.get(audit_id) or {})
    # Confirm presence in Supabase if not in memory
    in_supabase = False
    if not job:
        persisted = fetch_audit(audit_id=audit_id)
        if persisted:
            in_supabase = True
            job = {'status': 'completed', 'result': persisted}

    status_ = _public_status(job, in_supabase) if job else 'lost'
    resp = {
        'auditId': audit_id,
        'status': status_,
        'progressPct': _derive_progress_pct(job) if job else 0,
    }
    if job.get('progress'):
        p = job['progress']
        resp['phase'] = p.get('phase')
        resp['elapsedSeconds'] = p.get('elapsed_seconds')
        resp['toolCount'] = p.get('tool_count')
    if status_ == 'failed':
        resp['error'] = job.get('error')
        if job.get('agent_errors'):
            resp['agentErrors'] = job['agent_errors']
    if status_ == 'complete':
        resp['durationSeconds'] = job.get('result', {}).get('duration_seconds') \
            if isinstance(job.get('result'), dict) else None
    return resp


@app.get('/api/audit/{audit_id}/result')
def api_audit_result(audit_id: str, request: Request,
                      full: int = 0, _: bool = Depends(require_api_key)):
    """The audit result.

    Default (?full not set, or full=0): compact shape — score, grade,
    summary, issues[], fullReportUrl — sized for the card render.

    ?full=1: the rich audit JSON (the same shape /api/by-id returns).

    Never returns 4xx for a known but not-yet-complete audit — returns
    200 with status='not_ready' so the polling loop stays simple.
    Failures return 200 with status='failed'. Unknown audit_id returns 404."""
    with JOBS_LOCK:
        job = dict(JOBS.get(audit_id) or {})

    audit_obj = None
    if job:
        result = job.get('result')
        if isinstance(result, dict) and not result.get('error'):
            audit_obj = result
        elif job.get('status') == 'error':
            return {'status': 'failed', 'auditId': audit_id,
                    'reason': job.get('error') or 'audit failed',
                    'agentErrors': job.get('agent_errors') or []}
        elif job.get('status') in ('queued', 'running'):
            return {'status': 'not_ready', 'auditId': audit_id,
                    'progressPct': _derive_progress_pct(job)}

    if audit_obj is None:
        audit_obj = fetch_audit(audit_id=audit_id)

    if audit_obj is None:
        raise HTTPException(status_code=404, detail='audit not found')

    if full:
        return audit_obj
    return _audit_to_compact(audit_obj, request=request)


# ----------------------------------------------------------------------
# ADMIN: DELETE / SUPPRESS AUDITS
# Removes a published audit from Supabase + disk + memory, and (optionally)
# suppresses the domain so it can't be re-audited or re-published. Use for
# takedown requests (a brand that objects under UWG/GDPR, etc.).
# Gated by the API key; fail-closed in production when no key is configured.
# ----------------------------------------------------------------------


@app.delete('/api/audit/by-domain/{domain:path}')
def delete_audit_by_domain(domain: str, suppress: int = 1,
                           _: bool = Depends(require_admin)):
    """Delete ALL persisted audits for a domain (Supabase + disk + memory).

    suppress=1 (default) also adds the domain to the in-process denylist so it
    can't be re-audited/re-published until restart. For a durable block, add
    the bare domain to the SUPPRESSED_DOMAINS env var on the host.
    """
    reg = _registrable(domain)
    if not reg or '.' not in reg:
        raise HTTPException(status_code=400, detail='invalid domain')

    db_result = delete_audits(domain=reg)
    files_removed = _purge_local_artifacts(domain=reg)
    jobs_removed = _purge_jobs(domain=reg)

    suppressed = False
    durable = False
    if suppress:
        with SUPPRESS_LOCK:
            SUPPRESSED_DOMAINS.add(reg)
        suppressed = True
        # Durable across redeploys (was in-memory-only — ENG-7).
        durable = persist_suppression(reg)

    log.warning('ADMIN delete domain=%s db=%s files=%d jobs=%d suppressed=%s durable=%s',
                reg, db_result, files_removed, jobs_removed, suppressed, durable)
    return {
        'domain': reg,
        'supabase': db_result,
        'local_files_removed': files_removed,
        'jobs_removed': jobs_removed,
        'suppressed': suppressed,
        'suppressed_durable': durable,
        'suppressed_note': (None if durable else
                            'In-memory only (Supabase not configured); also set SUPPRESSED_DOMAINS env var'),
    }


@app.delete('/api/audit/{audit_id}')
def delete_audit_by_id(audit_id: str, _: bool = Depends(require_admin)):
    """Delete a single audit by id (Supabase + disk + memory). Does not
    suppress the domain — use the by-domain route for that."""
    db_result = delete_audits(audit_id=audit_id)
    files_removed = _purge_local_artifacts(audit_id=audit_id)
    jobs_removed = _purge_jobs(audit_id=audit_id)
    log.warning('ADMIN delete audit_id=%s db=%s files=%d jobs=%d',
                audit_id, db_result, files_removed, jobs_removed)
    return {'audit_id': audit_id, 'supabase': db_result,
            'local_files_removed': files_removed, 'jobs_removed': jobs_removed}


@app.get('/api/suppressed')
def list_suppressed(_: bool = Depends(require_admin)):
    """List domains currently suppressed (env + in-process additions)."""
    with SUPPRESS_LOCK:
        return {'suppressed_domains': sorted(SUPPRESSED_DOMAINS)}


@app.delete('/api/suppressed/{domain:path}')
def unsuppress(domain: str, _: bool = Depends(require_admin)):
    """Remove a domain from the denylist, in-process AND durably (Supabase).
    A domain seeded via the SUPPRESSED_DOMAINS env var will reappear on restart
    — edit that on the host for env-seeded entries."""
    reg = _registrable(domain)
    with SUPPRESS_LOCK:
        SUPPRESSED_DOMAINS.discard(reg)
    durable_removed = remove_suppression(reg)
    return {'domain': reg, 'suppressed': False, 'durable_removed': durable_removed}


# ----------------------------------------------------------------------
# BRAIN RETRIEVAL — server-to-server NORMS layer for AnswerMonk.
# One brain client: consumers never touch the DB; this endpoint rides the
# hardened sieve_brain stack (vector-first + FTS fallback, status gate,
# authority tiering, url_provenance-aware ranking). Batched: one call per
# audit/roadmap build, results are cached by the caller in its cards.
# ----------------------------------------------------------------------

class BrainQuery(BaseModel):
    key: str = Field(..., max_length=120)
    q: Optional[str] = Field(None, max_length=400)          # free-text search
    check_id: Optional[str] = Field(None, max_length=120)   # auditor-style id
    rule_ids: Optional[_List[str]] = None                   # exact-id fetch
    evidence: Optional[str] = None    # §3: finding's observed evidence — leads
                                      # the check_id query exactly like the
                                      # in-audit path (server truncates to 400)


class BrainRetrieveRequest(BaseModel):
    queries: _List[BrainQuery] = Field(..., max_length=40)
    min_tier: int = Field(3, ge=1, le=5)      # NORM slot: tier<=3 = canonical orgs only
    max_citations: int = Field(3, ge=1, le=8)


@app.post('/api/brain/retrieve')
def api_brain_retrieve(req: BrainRetrieveRequest,
                       _: bool = Depends(require_api_key)):
    """Batch norm retrieval from the live Sieve brain.

    Per query: q (free-text) | check_id (expanded like the audit agent's own
    queries) | rule_ids (curated-mapping exact fetch). A check_id query may
    carry `evidence` (the finding's observed text, truncated server-side to
    400 chars) — it LEADS the retrieval query via _query_for(check_id,
    evidence), exactly like the in-audit path, so two findings on the same
    check with different problems retrieve different norms. Only status
    active/candidate rows are served; the NORM gate (min_tier, default 3)
    means unattributed/observed knowledge can never be returned as a norm.
    Response citations carry source_org, source_url, url_provenance_method,
    confidence_score, last_verified — render org+URL verbatim; the brain's
    name stays out of client copy.
    """
    try:
        import sieve_brain
        specs = []
        for q in req.queries:
            if not (q.q or q.check_id or q.rule_ids):
                continue
            specs.append({'key': q.key, 'q': q.q, 'check_id': q.check_id,
                          'rule_ids': q.rule_ids,
                          'evidence': (q.evidence or '')[:400] or None})
        if not specs:
            raise HTTPException(status_code=422,
                                detail='each query needs q, check_id, or rule_ids')
        out = sieve_brain.retrieve_batch(specs, min_tier=req.min_tier,
                                         max_citations=req.max_citations)
        out['requested'] = len(specs)
        return out
    except HTTPException:
        raise
    except Exception as e:
        # Never 500 the caller's roadmap build — degrade like every other
        # brain path: caller falls back to its vendored snapshot mappings.
        log.warning('brain retrieve endpoint failed: %s', e)
        return {'live': False, 'results': {}, 'reason': str(e)[:200]}


# ----------------------------------------------------------------------
# CATCH-ALL SLUG ROUTE — audits.growthmonk.ai/{domain}
# MUST be the LAST route registered so it never shadows explicit routes.
# Serves the homepage HTML; the page JS reads the path and fetches that
# domain's persisted audit via /api/by-domain.
# ----------------------------------------------------------------------

_RESERVED_SLUGS = {
    'api', 'healthz', 'audit', 'audits', 'debug', 'docs', 'redoc',
    'openapi.json', 'favicon.ico', 'static', 'robots.txt',
}


@app.get('/{slug}', response_class=HTMLResponse)
def slug_page(slug: str):
    """Catch-all for /{domain} — PUBLIC. This is the customer-facing audit
    view: share audits.growthmonk.ai/{their-domain} and they can see the
    audit without an auth prompt. Serves the SPA homepage; client JS
    resolves the slug to a persisted audit via /api/by-domain.

    Reserved words and audit-prefixed paths are excluded so explicit
    routes (which may still be auth-gated) are never shadowed."""
    if slug in _RESERVED_SLUGS or slug.startswith('audit'):
        raise HTTPException(status_code=404, detail='not found')
    return HTMLResponse(INDEX_HTML)


if __name__ == '__main__':
    import uvicorn
    port = int(os.getenv('PORT', 8000))
    uvicorn.run(app, host='0.0.0.0', port=port)
