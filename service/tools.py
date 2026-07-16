"""
tools.py — Tool implementations for the audit agent.

Exposes 5 client-side tools the agent calls via Anthropic tool-use, plus
declares 2 Anthropic SERVER-side tools (web_search, web_fetch) that run
inside Anthropic's infrastructure with byte-for-byte parity to the chat
WebSearch / WebFetch tools.

Client-side tools (we dispatch in dispatch_tool):
    1. render_page_js(url)                        — Playwright: post-JS HTML + perf metrics
    2. run_deterministic_scripts(url)             — subprocess to skill-unified/scripts/run_deterministic.sh
    3. query_brain(check_id, page_type, industry) — wraps ranker.select_citations
    4. read_reference(name)                       — read skill-unified/references/{name}.md
    5. persist_audit(audit_data)                  — Supabase INSERT (best-effort) + always local

Server-side tools (Anthropic handles execution; we only declare in TOOLS_SPEC):
    6. web_search                                 — Anthropic native ($10 / 1k searches)
    7. web_fetch                                  — Anthropic native (free, only token costs)

Design rules:
    - Client tools never raise. They catch all exceptions and return {"error": str}.
    - Output payloads are bounded (truncate long fields) — keeps agent context small.
    - No tool depends on agent state — they're pure functions of their args + env.
    - Server tools are dispatched by Anthropic itself; agent.py skips them in its
      dispatch loop (they appear as web_search_tool_use / web_fetch_tool_use blocks
      in the assistant message, with results inlined as *_tool_result blocks).

ENV VARS
    ANTHROPIC_API_KEY        (required — also pays for web_search/web_fetch usage)
    SUPABASE_URL             (optional, for persist_audit)
    SUPABASE_SERVICE_KEY     (optional, for persist_audit)
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger('audit.tools')

# Paths — the deterministic core, ruleset, and references are co-located under
# the service directory (clean single-implementation layout).
THIS_DIR = Path(__file__).resolve().parent
RULESET_DIR = THIS_DIR / 'ruleset'
SCRIPTS_DIR = THIS_DIR / 'scripts'
REFERENCES_DIR = THIS_DIR / 'references'

sys.path.insert(0, str(RULESET_DIR))

# SSRF guard — the auditor fetches whatever URL a caller (or the model, or a
# competitor list, or a sitemap) hands it, from inside a cloud host. Every
# url-taking tool is validated at dispatch against private/loopback/metadata
# targets. This is the fetch-path enforcement (safety.check_url_safe was
# previously only wired at HTTP submission, never on the tools themselves).
from safety import check_url_safe

# Storage backend selector. When DATABASE_URL is set (Railway Postgres attached),
# persistence goes to Postgres; otherwise it falls through to the Supabase REST
# path below. Same shapes either way — callers don't change.
try:
    import db as _pgdb
except Exception:
    _pgdb = None


def _use_pg() -> bool:
    return _pgdb is not None and _pgdb.pg_enabled()


# ============================================================================
# NOTE: web_fetch and web_search are Anthropic SERVER-side tools.
# They are NOT implemented here — Anthropic executes them. We only declare
# them in TOOLS_SPEC at the bottom of this file. See:
#   https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool
#   https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool
# ============================================================================


# ============================================================================
# TOOL: render_page_js (Playwright) — desktop pass + guarded mobile pass
# ============================================================================

# Mobile emulation profile + parity comparator + honest CWV labels (2.2).
# Stdlib-only module; safe to import even where Playwright is absent.
import mobile_parity

_DESKTOP_UA = "Mozilla/5.0 (compatible; AEO-Auditor/1.0; +Playwright)"


def _render_pass(browser, url: str, context_kwargs: Dict[str, Any]):
    """One render pass in a fresh browser context. Returns (metrics, html).

    Shared by the desktop pass and the mobile-emulation pass so both report
    the identical metric set. LCP/CLS here are LAB values from this single
    run (labeled by the caller); INP is never measured or fabricated.
    """
    ctx = browser.new_context(**context_kwargs)
    try:
        page = ctx.new_page()

        console_errors: List[str] = []
        page.on("console", lambda msg: console_errors.append(msg.text)
                if msg.type == "error" else None)
        request_count = [0]
        page.on("request", lambda _: request_count.__setitem__(0, request_count[0] + 1))

        t0 = time.time()
        response = page.goto(url, wait_until="networkidle", timeout=30000)
        load_time_ms = (time.time() - t0) * 1000

        # TTFB from response timing
        ttfb_ms = None
        try:
            timing = response.request.timing if response else None
            if timing:
                ttfb_ms = timing.get("responseStart")
        except Exception:
            pass

        # LCP via PerformanceObserver — best-effort, LAB (single run)
        try:
            lcp = page.evaluate("""
                () => new Promise((resolve) => {
                    let lcp = null;
                    try {
                        new PerformanceObserver((list) => {
                            const entries = list.getEntries();
                            if (entries.length) lcp = entries[entries.length - 1].startTime;
                        }).observe({type: 'largest-contentful-paint', buffered: true});
                    } catch(e) {}
                    setTimeout(() => resolve(lcp), 2000);
                })
            """)
        except Exception:
            lcp = None

        # CLS — also best-effort, LAB (single run)
        try:
            cls = page.evaluate("""
                () => new Promise((resolve) => {
                    let cls = 0;
                    try {
                        new PerformanceObserver((list) => {
                            for (const entry of list.getEntries()) {
                                if (!entry.hadRecentInput) cls += entry.value;
                            }
                        }).observe({type: 'layout-shift', buffered: true});
                    } catch(e) {}
                    setTimeout(() => resolve(cls), 1500);
                })
            """)
        except Exception:
            cls = None

        # SPA framework detection
        spa = page.evaluate("""
            () => {
                const out = [];
                if (window.__NEXT_DATA__) out.push('Next.js');
                if (window.__NUXT__) out.push('Nuxt');
                if (window.React || document.querySelector('[data-reactroot]')) out.push('React');
                if (window.Vue) out.push('Vue');
                if (window.ng) out.push('Angular');
                return out;
            }
        """) or []

        html = page.content()
        title = page.title()
        h1 = ""
        try:
            h1_node = page.query_selector("h1")
            if h1_node:
                h1 = (h1_node.inner_text() or "").strip()[:300]
        except Exception:
            pass

        metrics = {
            "post_js_html_size": len(html),
            "title": title[:300],
            "h1_first": h1,
            "ttfb_ms": ttfb_ms,
            "lcp_ms": lcp,
            "cls": cls,
            "load_time_ms": round(load_time_ms, 1),
            "request_count": request_count[0],
            "console_errors": console_errors[:20],
            "spa_signals": spa,
        }
        return metrics, html
    finally:
        try:
            ctx.close()
        except Exception:
            pass


def render_page_js(url: str) -> Dict[str, Any]:
    """Render a page with a real browser (Playwright + Chromium).

    Runs a DESKTOP pass (original behavior) and — unless AUDIT_MOBILE_RENDER
    is set to 0/false — a MOBILE emulation pass (viewport 390x844, DPR 3,
    mobile UA, touch), then computes a deterministic mobile-vs-desktop
    content-parity check.

    Returns post-JS HTML and performance metrics (desktop fields unchanged,
    mobile fields additive):
        {
          "url": str,
          "post_js_html_size": int,
          "title": str,
          "h1_first": str,
          "ttfb_ms": float,
          "lcp_ms": float | None,   # LAB value — single run, not field data
          "cls": float | None,      # LAB value — single run, not field data
          "load_time_ms": float,
          "request_count": int,
          "console_errors": [str],
          "spa_signals": [str],     # detected frameworks (Next.js, React, etc.)
          "cwv_source": "lab (single run)",
          "inp_note": str,          # INP requires CrUX field data — never fabricated
          "mobile": { same metric keys | "skipped" | "error" },
          "mobile_parity": {        # deterministic check, evidence_tier=measured
            "check_id": "A9b_mobile_content_parity",
            "status": "pass|warn|fail|na", "evidence": str, "detail": {...}
          },
        }
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"error": "playwright not installed. pip install playwright && playwright install chromium"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                desktop, desktop_html = _render_pass(
                    browser, url, {"user_agent": _DESKTOP_UA})
                result: Dict[str, Any] = {"url": url, **desktop}
                # Honest CWV labeling: LCP/CLS above are lab numbers from ONE
                # run. INP needs CrUX field data — publish the note, never a value.
                result["cwv_source"] = mobile_parity.CWV_LAB_LABEL
                result["inp_note"] = mobile_parity.INP_FIELD_NOTE

                if not mobile_parity.mobile_render_enabled():
                    reason = (f"mobile render disabled via "
                              f"{mobile_parity.MOBILE_FLAG_ENV}=0")
                    result["mobile"] = {"skipped": reason}
                    result["mobile_parity"] = mobile_parity.parity_na(reason)
                else:
                    # Mobile pass is best-effort: a failure here must never
                    # cost the audit its desktop metrics (same graceful-skip
                    # posture as the Playwright-missing path).
                    try:
                        mobile, mobile_html = _render_pass(
                            browser, url, mobile_parity.mobile_context_kwargs())
                        mobile["viewport"] = (
                            f"{mobile_parity.MOBILE_VIEWPORT['width']}x"
                            f"{mobile_parity.MOBILE_VIEWPORT['height']} "
                            f"@{mobile_parity.MOBILE_DEVICE_SCALE_FACTOR}x (touch)")
                        mobile["user_agent"] = mobile_parity.MOBILE_USER_AGENT
                        mobile["cwv_source"] = mobile_parity.CWV_LAB_LABEL
                        result["mobile"] = mobile
                        result["mobile_parity"] = mobile_parity.parity_check(
                            desktop_html, mobile_html)
                    except Exception as me:  # noqa: BLE001 — degrade, don't raise
                        result["mobile"] = {"error": f"{type(me).__name__}: {me}"}
                        result["mobile_parity"] = mobile_parity.parity_na(
                            f"mobile render failed: {type(me).__name__}")
            finally:
                browser.close()

            return result
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "url": url}


# ============================================================================
# TOOL: run_deterministic_scripts
# ============================================================================

DETERMINISTIC_SCRIPT = SCRIPTS_DIR / "run_deterministic.sh"


def run_deterministic_scripts(url: str, timeout: int = 180) -> Dict[str, Any]:
    """Invoke skill-unified/scripts/run_deterministic.sh and parse JSON output.

    Returns the orchestrator JSON: bots_eye_view, all_checks, overall_summary,
    sitemap_analysis, robots_txt_analysis, schema_completeness.
    """
    if not DETERMINISTIC_SCRIPT.exists():
        return {"error": f"Script not found: {DETERMINISTIC_SCRIPT}"}

    # Run in its own process group so a timeout can kill the ENTIRE subtree.
    # run_deterministic.sh backgrounds 5 child scripts (curl/python3); a plain
    # subprocess.run(timeout=) kills only the direct bash child, leaving curl
    # grandchildren orphaned and — because they inherit the stdout pipe — can
    # block the parent's reaping past the deadline. killpg cleans them all up.
    try:
        proc = subprocess.Popen(
            ["bash", str(DETERMINISTIC_SCRIPT), url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.communicate()
        except (ProcessLookupError, PermissionError):
            pass
        return {"error": f"timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        return {
            "error": f"JSON parse: {e}",
            "stdout_first_2000": (stdout or "")[:2000],
            "stderr_first_2000": (stderr or "")[:2000],
        }


# ============================================================================
# TOOL 5: query_brain
# ============================================================================

_BRAIN_CACHE = None


def _get_brain():
    global _BRAIN_CACHE
    if _BRAIN_CACHE is None:
        from ranker import BrainIndex
        _BRAIN_CACHE = BrainIndex.from_export_dir(str(RULESET_DIR))
    return _BRAIN_CACHE


def query_brain(check_id: str, page_type: str = "homepage",
                industry: str = "other", max_citations: int = 3,
                evidence: str = None) -> Dict[str, Any]:
    """Query the Sieve brain for citations relevant to a given check_id.

    Returns top-N rules + anti-patterns ranked by tier ASC, confidence DESC.

    Resolution order for the check_id:
        1. Exact match  ('A2b_title_uniqueness_sample' → exact key)
        2. Strip trailing letter from the numeric prefix
           ('A2b_title_uniqueness_sample' → 'A2_*' prefix scan)
        3. Bare-section prefix scan
           ('A2b_anything' → first key starting with 'A2_')

    This handles the case where the deterministic scripts emit sub-check IDs
    like 'A2b_title_uniqueness_sample' (a sub-check of the parent A2) but
    the brain mapping was authored against the parent 'A2_title_tag'. Falling
    back to the parent gives reasonable citations for sub-checks.
    """
    # LIVE brain (opt-in via SIEVE_LIVE): retrieve from the fresh Sieve DB
    # (23k rules, real source_url + authority-tier ranking + last_verified).
    # ADDITIVE — on any miss/error this returns None and we fall straight
    # through to the snapshot ranker below. The static path is never touched.
    try:
        import sieve_brain
        live = sieve_brain.live_citations(check_id, page_type, industry, max_citations,
                                          evidence=evidence)
        if live:
            return {
                "check_id": check_id,
                "resolved_to": None,
                "citations": live,
                "source": "sieve-live",
            }
    except Exception as _live_err:  # noqa: BLE001 — never let live path break query_brain
        # ...EXCEPT in strict mode: SIEVE_STRICT means the operator chose
        # 'fail the audit' over 'silently cite the April snapshot'.
        import sieve_brain as _sb
        if isinstance(_live_err, _sb.SieveLiveError):
            raise
        log.debug('live brain unavailable, using snapshot: %s', _live_err)

    try:
        from ranker import select_citations
        brain = _get_brain()
    except Exception as e:
        return {"error": f"brain load: {type(e).__name__}: {e}", "citations": []}

    resolved_id = _resolve_check_id(check_id, brain.check_to_rules)

    try:
        citations = select_citations(
            brain=brain, check_id=resolved_id,
            page_type=page_type, industry=industry,
            max_citations=max_citations,
        )
        # Trim verbose fields
        for c in citations:
            for k in ("if_condition", "then_action", "description"):
                if k in c and isinstance(c[k], str):
                    c[k] = c[k][:500]
        return {
            "check_id": check_id,
            "resolved_to": resolved_id if resolved_id != check_id else None,
            "citations": citations,
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "citations": []}


def _resolve_check_id(check_id: str, mappings: Dict[str, Any]) -> str:
    """Resolve a (possibly sub-check) ID to a key present in brain-mappings.

    Examples:
        'A2_title_tag'                 → 'A2_title_tag'                 (exact)
        'A2b_title_uniqueness_sample'  → 'A2_title_tag'                 (strip 'b')
        'D14_hreflang_coverage'        → 'D14_hreflang_coverage'        (exact)
        'C12b_datemodified_staleness'  → 'C12_visible_date_staleness'   (parent prefix)
        'unknown_check_id'             → 'unknown_check_id'             (no resolution)
    """
    if check_id in mappings:
        return check_id

    # Extract the section prefix (e.g. "A2b" → "A2", "C12b" → "C12")
    import re
    m = re.match(r"^([A-J])(\d+)([a-z])?(_.*)?$", check_id)
    if not m:
        return check_id  # not a section-style ID, return as-is

    section, num, letter, suffix = m.groups()
    bare_prefix = f"{section}{num}"

    # Try the bare prefix as a key starter (e.g. "A2_")
    candidates = [k for k in mappings if k.startswith(bare_prefix + "_")]
    if candidates:
        # If the original had a sub-letter (A2b), prefer non-sub-lettered keys
        # If multiple, take the shortest/most generic name
        return sorted(candidates, key=len)[0]

    return check_id


# ============================================================================
# TOOL 6: read_reference
# ============================================================================

# Whitelist — only these reference files are loadable
ALLOWED_REFERENCES = {
    "static-rules", "check-definitions", "schema-validation",
    "knowledge-seo", "knowledge-performance", "knowledge-aeo",
    "knowledge-geo", "aeo-framework", "geo-framework",
    "scoring-rubric", "brain-mappings", "competitor-gap-template",
    "supabase-queries",
}


def read_reference(name: str) -> Dict[str, Any]:
    """Read a reference markdown file from skill-unified/references/.

    Args:
        name: file basename without .md (e.g. "static-rules", "schema-validation")
    """
    if name not in ALLOWED_REFERENCES:
        return {
            "error": f"unknown reference: {name}",
            "allowed": sorted(ALLOWED_REFERENCES),
        }
    path = REFERENCES_DIR / f"{name}.md"
    if not path.exists():
        return {"error": f"file missing: {path}"}
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"name": name, "size_bytes": len(content), "content": content}


# ============================================================================
# TOOL 7: persist_audit
# ============================================================================

def persist_audit(audit_data: Dict[str, Any]) -> Dict[str, Any]:
    """Persist a completed audit to Supabase — both the audit summary row
    and one row per finding.

    Writes to project htowtfnbfmjmbeftfhis (set via SUPABASE_URL env var):
        - public.website_audits          (1 row, 30-column schema)
        - public.website_audit_findings  (N rows, FK on audit_id)

    Uses the service_role key (SUPABASE_SERVICE_KEY) which bypasses RLS.
    If either env var is unset, returns a local-only no-op success.

    Returns:
        {
          "persisted": bool,
          "supabase_row_id": str | None,   # the table's gen_random_uuid() id
          "audit_id": str,                  # the agent's own audit uuid
          "findings_persisted": int,
          "error": str | None,
        }
    """
    if _use_pg():
        return _pgdb.persist_audit(audit_data)
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    audit_id = audit_data.get("audit_id")

    if not (supabase_url and supabase_key):
        return {"persisted": False, "supabase_row_id": None,
                "audit_id": audit_id, "findings_persisted": 0,
                "note": "Supabase env vars not set — local-only persistence."}

    try:
        import httpx
    except ImportError:
        return {"persisted": False, "error": "httpx not installed",
                "audit_id": audit_id, "findings_persisted": 0}

    # Normalize the URL — tolerate a SUPABASE_URL that was pasted with a
    # trailing /rest/v1 (a common mistake; persist appends that itself).
    base = supabase_url.rstrip("/")
    for suffix in ("/rest/v1", "/rest"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    base = base.rstrip("/")

    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }

    classification = audit_data.get("classification", {}) or {}
    context = audit_data.get("context", {}) or {}
    scoring = audit_data.get("scoring", {}) or {}
    findings = audit_data.get("findings", []) or []

    # ---- Audit summary row — matches the 30-column website_audits schema ----
    audit_row = {
        "audit_id": audit_id,
        "url": audit_data.get("url"),
        "domain": audit_data.get("domain"),
        "audit_date": audit_data.get("date"),
        "page_type": classification.get("page_type"),
        "industry": classification.get("industry"),
        "company_name": classification.get("company_name"),
        "confidence": classification.get("confidence"),
        "competitors": context.get("competitors"),
        "test_queries": context.get("test_queries"),
        "gates": audit_data.get("gates"),
        "section_scores": scoring.get("section_scores"),
        "page_citation_readiness": scoring.get("page_citation_readiness"),
        "brand_ai_presence": scoring.get("brand_ai_presence"),
        "seo_score": scoring.get("seo_score"),
        "aeo_score": scoring.get("aeo_score"),
        "citation_readiness": scoring.get("citation_readiness"),
        "overall_score": scoring.get("overall_score"),
        "overall_grade": scoring.get("overall_grade"),
        "narrative": audit_data.get("narrative"),
        "competitor_comparison": audit_data.get("competitor_comparison"),
        "bots_eye_view": audit_data.get("bots_eye_view"),
        "performance": audit_data.get("performance"),
        "supplementary_findings": audit_data.get("supplementary_findings"),
        "metadata": audit_data.get("metadata"),
        "findings_count": len(findings),
        "duration_seconds": audit_data.get("duration_seconds"),
        "audit_mode": (audit_data.get("metadata", {}) or {}).get("version", "agent"),
    }

    try:
        with httpx.Client(timeout=20.0) as client:
            # 1. Insert the audit summary row. resolution=merge-duplicates makes
            #    a re-persist of the same audit_id idempotent (upsert on the
            #    unique audit_id column).
            r = client.post(
                f"{base}/rest/v1/website_audits",
                headers={**headers,
                         "Prefer": "return=representation,resolution=merge-duplicates"},
                json=audit_row,
            )
            if r.status_code not in (200, 201):
                return {"persisted": False,
                        "error": f"website_audits insert status {r.status_code}: "
                                 f"{r.text[:400]}",
                        "audit_id": audit_id, "findings_persisted": 0}
            inserted = r.json()
            row_id = inserted[0].get("id") if inserted else None

            # 2. Insert findings (batch). Insert FIRST, then delete the older
            #    rows only after the new insert succeeds — so a failed insert
            #    can't leave 0 findings behind a non-zero findings_count
            #    (the table never ends up emptier than it started).
            findings_persisted = 0
            if findings:
                findings_rows = [
                    {
                        "audit_id": audit_id,
                        "check_id": f.get("check_id"),
                        "section": f.get("section"),
                        "status": f.get("status"),
                        "severity": f.get("severity"),
                        "evidence": (f.get("evidence") or "")[:4000],
                        "truth_badge": f.get("truth_badge"),
                        "fix_type": f.get("fix_type"),
                        "citations": f.get("citations"),
                    }
                    for f in findings
                ]
                # Tag this insert generation so we can delete only the rows
                # that existed BEFORE it, leaving the just-inserted rows intact.
                fr = client.post(
                    f"{base}/rest/v1/website_audit_findings",
                    headers={**headers, "Prefer": "return=representation"},
                    json=findings_rows,
                )
                if fr.status_code in (200, 201):
                    try:
                        new_ids = [row.get("id") for row in fr.json()
                                   if isinstance(row, dict) and row.get("id") is not None]
                    except Exception:
                        new_ids = []
                    findings_persisted = len(findings_rows)
                    # Remove prior rows for this audit_id, excluding the ones we
                    # just inserted. If we couldn't read the new ids, skip the
                    # delete entirely rather than risk deleting the fresh rows.
                    if new_ids:
                        id_list = ','.join(str(i) for i in new_ids)
                        client.delete(
                            f"{base}/rest/v1/website_audit_findings",
                            headers=headers,
                            params={"audit_id": f"eq.{audit_id}",
                                    "id": f"not.in.({id_list})"},
                        )
                else:
                    # Audit row saved; findings failed — report partial success.
                    # We did NOT delete anything, so prior findings (if any) stay.
                    return {"persisted": True, "supabase_row_id": row_id,
                            "audit_id": audit_id, "findings_persisted": 0,
                            "error": f"findings insert status {fr.status_code}: "
                                     f"{fr.text[:300]}"}

        return {"persisted": True, "supabase_row_id": row_id,
                "audit_id": audit_id, "findings_persisted": findings_persisted}
    except Exception as e:
        return {"persisted": False, "error": f"{type(e).__name__}: {e}",
                "audit_id": audit_id, "findings_persisted": 0}


def _supabase_base_headers():
    """Return (base_url, headers) for Supabase REST, or (None, None) if env
    vars are unset. Shared by persist_audit (write) and fetch_audit (read)."""
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_SERVICE_KEY")
    if not (supabase_url and supabase_key):
        return None, None
    base = supabase_url.rstrip("/")
    for suffix in ("/rest/v1", "/rest"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
    base = base.rstrip("/")
    headers = {
        "apikey": supabase_key,
        "Authorization": f"Bearer {supabase_key}",
        "Content-Type": "application/json",
    }
    return base, headers


def fetch_audit(domain: Optional[str] = None,
                audit_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fetch a persisted audit from Supabase and reassemble it into the full
    audit-JSON shape — the exact shape run_audit_agent produces and the
    homepage renderFull() expects.

    Provide exactly one of:
        domain   — returns the LATEST audit for that domain
        audit_id — returns that specific audit

    Returns the audit dict, or None if not found / storage not configured.
    """
    if _use_pg():
        return _pgdb.fetch_audit(domain=domain, audit_id=audit_id)
    base, headers = _supabase_base_headers()
    if base is None:
        return None

    try:
        import httpx
    except ImportError:
        return None

    if audit_id:
        params = {"audit_id": f"eq.{audit_id}", "limit": "1"}
    elif domain:
        params = {"domain": f"eq.{domain}",
                  "order": "created_at.desc", "limit": "1"}
    else:
        return None

    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(f"{base}/rest/v1/website_audits",
                            headers=headers, params=params)
            if r.status_code != 200:
                return None
            rows = r.json()
            if not rows:
                return None
            row = rows[0]

            fr = client.get(f"{base}/rest/v1/website_audit_findings",
                            headers=headers,
                            params={"audit_id": f"eq.{row['audit_id']}",
                                    "order": "id.asc"})
            findings = fr.json() if fr.status_code == 200 else []
    except Exception:
        return None

    # Reassemble into the canonical audit-JSON shape renderFull() consumes.
    return {
        "audit_id": row.get("audit_id"),
        "url": row.get("url"),
        "domain": row.get("domain"),
        "date": row.get("audit_date"),
        "duration_seconds": row.get("duration_seconds"),
        "classification": {
            "page_type": row.get("page_type"),
            "industry": row.get("industry"),
            "company_name": row.get("company_name"),
            "confidence": row.get("confidence"),
        },
        "context": {
            "competitors": row.get("competitors"),
            "test_queries": row.get("test_queries"),
        },
        "gates": row.get("gates"),
        "scoring": {
            "section_scores": row.get("section_scores"),
            "page_citation_readiness": row.get("page_citation_readiness"),
            "brand_ai_presence": row.get("brand_ai_presence"),
            "seo_score": row.get("seo_score"),
            "aeo_score": row.get("aeo_score"),
            "citation_readiness": row.get("citation_readiness"),
            "overall_score": row.get("overall_score"),
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
            {
                "check_id": f.get("check_id"),
                "section": f.get("section"),
                "status": f.get("status"),
                "severity": f.get("severity"),
                "evidence": f.get("evidence"),
                "truth_badge": f.get("truth_badge"),
                "fix_type": f.get("fix_type"),
                "citations": f.get("citations"),
            }
            for f in findings
        ],
        "loaded_from": "supabase",
        "created_at": row.get("created_at"),
    }


def list_audits_for_domain(domain: str, limit: int = 10) -> list:
    """Return a compact list of past audits for a domain (newest first) —
    audit_id, score, grade, date — for the 'previous audits' UI."""
    if _use_pg():
        return _pgdb.list_audits_for_domain(domain, limit)
    base, headers = _supabase_base_headers()
    if base is None:
        return []
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                f"{base}/rest/v1/website_audits",
                headers=headers,
                params={"domain": f"eq.{domain}",
                        "select": "audit_id,overall_score,overall_grade,"
                                  "audit_date,created_at,findings_count",
                        "order": "created_at.desc",
                        "limit": str(limit)},
            )
            return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def list_all_audits(limit: int = 60) -> list:
    """Return all persisted audits, newest first — powers the homepage
    library grid. Compact projection: enough for an audit card."""
    if _use_pg():
        return _pgdb.list_all_audits(limit)
    base, headers = _supabase_base_headers()
    if base is None:
        return []
    try:
        import httpx
        with httpx.Client(timeout=15.0) as client:
            r = client.get(
                f"{base}/rest/v1/website_audits",
                headers=headers,
                params={"select": "audit_id,domain,url,page_type,industry,"
                                  "overall_score,overall_grade,findings_count,"
                                  "audit_date,created_at",
                        "order": "created_at.desc",
                        "limit": str(limit)},
            )
            return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def _domain_variants(domain: str) -> set:
    """Normalize a domain and return the {bare, www} forms persist may have
    stored (persist keeps whatever the URL host was, incl. a www. prefix)."""
    d = (domain or "").strip().lower()
    d = re.sub(r"^https?://", "", d).strip("/").split("/")[0]
    bare = d[4:] if d.startswith("www.") else d
    return {v for v in (bare, "www." + bare) if v and "." in v}


def delete_audits(domain: Optional[str] = None,
                  audit_id: Optional[str] = None) -> Dict[str, Any]:
    """Delete persisted audit(s) from Supabase — findings first, then the
    audit rows. Provide exactly one of `domain` (removes ALL audits for that
    domain, www/bare) or `audit_id` (removes that one). Returns a summary
    with the audit_ids removed and row counts. Never raises."""
    if _use_pg():
        return _pgdb.delete_audits(domain=domain, audit_id=audit_id)
    base, headers = _supabase_base_headers()
    if base is None:
        return {"deleted": False, "error": "Supabase not configured"}
    try:
        import httpx
    except ImportError:
        return {"deleted": False, "error": "httpx not installed"}

    try:
        with httpx.Client(timeout=20.0) as client:
            # 1. Resolve the set of audit_ids to remove.
            if audit_id:
                target_ids = [audit_id]
            elif domain:
                variants = _domain_variants(domain)
                if not variants:
                    return {"deleted": False, "error": f"invalid domain '{domain}'"}
                inlist = ",".join(f'"{v}"' for v in sorted(variants))
                r = client.get(
                    f"{base}/rest/v1/website_audits",
                    headers=headers,
                    params={"domain": f"in.({inlist})", "select": "audit_id"},
                )
                if r.status_code != 200:
                    return {"deleted": False,
                            "error": f"lookup failed: {r.status_code} {r.text[:200]}"}
                target_ids = [row["audit_id"] for row in r.json()
                              if row.get("audit_id")]
            else:
                return {"deleted": False, "error": "provide domain or audit_id"}

            if not target_ids:
                return {"deleted": True, "audit_ids": [], "audits_deleted": 0,
                        "findings_deleted": 0, "note": "no matching audits"}

            ids_in = ",".join(f'"{i}"' for i in target_ids)
            rep = {**headers, "Prefer": "return=representation"}

            # 2. Delete child findings, then 3. the audit rows.
            fr = client.delete(f"{base}/rest/v1/website_audit_findings",
                               headers=rep, params={"audit_id": f"in.({ids_in})"})
            findings_deleted = (len(fr.json()) if fr.status_code in (200, 206)
                                and fr.text.strip().startswith("[") else 0)

            ar = client.delete(f"{base}/rest/v1/website_audits",
                               headers=rep, params={"audit_id": f"in.({ids_in})"})
            if ar.status_code not in (200, 204, 206):
                return {"deleted": False,
                        "error": f"audit delete failed: {ar.status_code} {ar.text[:200]}",
                        "findings_deleted": findings_deleted}
            audits_deleted = (len(ar.json()) if ar.status_code in (200, 206)
                              and ar.text.strip().startswith("[") else len(target_ids))

        return {"deleted": True, "audit_ids": target_ids,
                "audits_deleted": audits_deleted,
                "findings_deleted": findings_deleted}
    except Exception as e:
        return {"deleted": False, "error": f"{type(e).__name__}: {e}"}


# ============================================================================
# TOOL DISPATCH TABLE — used by agent.py
# ============================================================================

# Only CLIENT-side tools are dispatched in this table.
# Server-side tools (web_search, web_fetch) are executed by Anthropic itself.
# NOTE: persist_audit is intentionally NOT here — it is a post-loop step
# called directly by run_audit_agent() with the real audit_id + complete
# audit dict, not a tool the agent invokes mid-loop.
TOOLS_IMPL = {
    "render_page_js": render_page_js,
    "run_deterministic_scripts": run_deterministic_scripts,
    "query_brain": query_brain,
    "read_reference": read_reference,
}

# Names of Anthropic server-side tools — agent.py skips these in dispatch
# (their tool_use blocks are handled by Anthropic's servers, with results
# returned inline as *_tool_result blocks in the same assistant turn).
SERVER_TOOL_NAMES = {"web_search", "web_fetch"}


# ============================================================================
# JSONSCHEMA SPEC FOR ANTHROPIC TOOL-USE API
# ============================================================================

TOOLS_SPEC = [
    # ----- Anthropic SERVER-side tools -------------------------------------
    # These are executed by Anthropic infrastructure (same backend the chat
    # WebSearch / WebFetch tools use). We do not implement them in TOOLS_IMPL.
    # Pricing: web_search = $10 per 1,000 searches; web_fetch = free (token
    # costs only). Both work on claude-sonnet-4-6 with no beta header.
    {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": 8,  # cap per audit — covers Phase 3a/3b/3c (4 queries) + Phase 9 (2) + headroom
    },
    {
        "type": "web_fetch_20250910",
        "name": "web_fetch",
        "max_uses": 8,  # cap per audit — Phase 1 target + Phase 8 (5 competitors) + headroom
        "citations": {"enabled": True},
        "max_content_tokens": 100_000,
    },
    # ----- Client-side tools (we dispatch these in agent.py) ----------------
    {
        "name": "render_page_js",
        "description": (
            "Render a URL with a real headless Chromium browser via Playwright. "
            "Runs a desktop pass AND a mobile-emulation pass (390x844 @3x, "
            "mobile UA, touch). Returns post-JS HTML size, title, first H1, "
            "performance metrics (TTFB, LCP, CLS, load time, request count), "
            "console errors, detected SPA framework signals, a `mobile` metric "
            "block, and a deterministic `mobile_parity` check "
            "(A9b_mobile_content_parity, evidence_tier=measured) comparing "
            "rendered text volume, headings and title/H1/meta between the two "
            "passes. LCP/CLS are LAB values from a single run (`cwv_source`); "
            "INP is never measured here (`inp_note` — requires CrUX field "
            "data). Use for Phase 1.5 performance measurement and to detect "
            "SPA-without-SSR cases. Slower than web_fetch (~5–10s) — only use "
            "when JS rendering or perf metrics are needed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "run_deterministic_scripts",
        "description": (
            "Invoke the skill's deterministic bash + Python script suite. "
            "Returns the full orchestrator JSON: bots_eye_view (5 UA probes "
            "+ classification), all_checks (D9, A7b, J2, A4b, B1, D4, C12b, "
            "A2b, D14, D12), overall_summary, sitemap_analysis (parsed XML, "
            "URL count, target_url presence), robots_txt_analysis (per-UA "
            "allowlist), schema_completeness (entities + missing fields). "
            "Call this once early in Phase 1.6 — it's the foundation of "
            "Phase 5–7 deterministic checks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "query_brain",
        "description": (
            "Query the Sieve brain (12,764 entries) for citations supporting "
            "a specific check. Returns top-3 rules + anti-patterns ranked by "
            "tier (1=Google/Schema.org, 2=Backlinko/Vercel, 3=SEL, 4=specialized) "
            "and confidence. Use during Phase 13 (citation enrichment) for "
            "every failed/warned check. check_id should match the static-rules "
            "namespace (e.g. 'D14_hreflang_coverage', 'A2b_title_uniqueness_sample')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "check_id": {"type": "string"},
                "page_type": {"type": "string", "default": "homepage"},
                "industry": {"type": "string", "default": "other"},
                "max_citations": {"type": "integer", "default": 3},
            },
            "required": ["check_id"],
        },
    },
    {
        "name": "read_reference",
        "description": (
            "Read a reference markdown file from skill-unified/references/. "
            "Use this to load detailed criteria when needed. Available files: "
            "static-rules (all 103 check criteria), check-definitions (truth "
            "badges + fix types), schema-validation (per-entity required "
            "fields), knowledge-seo / knowledge-performance / knowledge-aeo / "
            "knowledge-geo (research thresholds), aeo-framework (4-stage "
            "model), geo-framework (3-dimension model), scoring-rubric "
            "(weights + grades + DO NOW/PLAN/LATER matrix), brain-mappings "
            "(check_id → rule mappings), competitor-gap-template (Phase 8 "
            "comparison shape), supabase-queries (persistence SQL). "
            "Reference files are large — only load what you need for the "
            "current phase."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": sorted(ALLOWED_REFERENCES),
                },
            },
            "required": ["name"],
        },
    },
]


# ============================================================================
# DISPATCH
# ============================================================================

_URL_TAKING_TOOLS = frozenset({'render_page_js', 'run_deterministic_scripts'})


def dispatch_tool(name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
    """Run a tool by name with the given input dict. Returns the tool's output
    or {"error": ...} if the tool is unknown or raises."""
    impl = TOOLS_IMPL.get(name)
    if impl is None:
        return {"error": f"unknown tool: {name}"}
    # SSRF guard on every url-taking tool. The target may be model-chosen, from
    # a competitor list, or from a sitemap — all attacker-influenceable — so we
    # reject private/loopback/link-local/metadata destinations before fetching.
    if name in _URL_TAKING_TOOLS and isinstance(tool_input, dict):
        target = tool_input.get('url')
        if target is not None:
            ok, reason = check_url_safe(str(target))
            if not ok:
                log.warning('SSRF guard blocked %s(url=%s): %s', name, target, reason)
                return {"error": f"URL rejected by SSRF guard: {reason}", "url": target}
    try:
        return impl(**tool_input)
    except TypeError as e:
        return {"error": f"bad arguments to {name}: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


if __name__ == "__main__":
    # Smoke test from CLI: python tools.py <tool> <json_args>
    if len(sys.argv) < 2:
        print("Usage: python tools.py <tool_name> [json_args]")
        print("Tools:", ", ".join(TOOLS_IMPL.keys()))
        sys.exit(1)
    tool_name = sys.argv[1]
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = dispatch_tool(tool_name, args)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str)[:5000])
