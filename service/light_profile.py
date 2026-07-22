"""
light_profile.py — LIGHT audit profile: Tier-0 fetchability gates + 8
deterministic factors. 100% fetch+parse; zero LLM calls, zero cost ceiling.

WHAT IT RUNS (and nothing else):
    - Tier-0 gates: the existing Bot's-Eye-View probe (bots_eye_view.sh —
      multi-UA curl, 404-shell comparison, cloaking/bot-blocking detection)
    - Factor 1: AI-bot robots.txt access — check_robots_txt.check_robots +
      the additive per-bot breakdown (per_bot_access), mapped onto the
      existing canonical measured ids E1/E2/E3/E10/E13/A10/A11
    - Factors 2-8: scripts/light_checks.py over one SSRF-guarded page fetch
      (check_schema_completeness.fetch_html) + one /llms.txt fetch

WHAT IT SKIPS (by design — the full/agent profile is untouched):
    Playwright render (LCP/CLS), web_search (company context, competitor
    discovery, GEO presence), competitor crawl, AI-visibility sweep,
    citation attach/grounding/entailment (no live-brain dependency), fix
    generation, and every LLM-classified check. Narrative is a deterministic
    template.

OUTPUT: the same audit envelope main.py/_audit_to_compact/persist_audit/
post_to_answermonk already consume, plus:
    profile='light', target ('brand'|'competitor'), session_ref, and
    metadata.{profile,target,session_ref,llm_calls=0}.

Scoring reuses scoring.compute_from_findings verbatim — absent sections are
renormalized natively. metadata.profile + audit.profile stamp the run so a
light PCR/grade is never mistaken for a full audit's like-for-like number
(the WinnerCompare mixed-universe lesson).

Stdlib + the existing service modules.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

THIS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = THIS_DIR / 'scripts'
for p in (str(THIS_DIR), str(SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from scoring import (compute_from_findings, validate_audit,  # noqa: E402
                     observed_method_for)
import light_checks  # noqa: E402
import check_robots_txt  # noqa: E402
from check_schema_completeness import fetch_html, normalize_type  # noqa: E402

log = logging.getLogger('audit.light')

LIGHT_VERSION = 'light-1.0'
BEV_SCRIPT = SCRIPTS_DIR / 'bots_eye_view.sh'
BEV_TIMEOUT_S = 120

VALID_TARGETS = ('brand', 'competitor')

# The 8 factors, for metadata/reporting.
LIGHT_FACTORS = [
    'ai_bot_robots_access', 'llms_txt', 'raw_html_depth',
    'localbusiness_geo_schema', 'city_in_title_h1', 'faq_content_schema',
    'question_headings', 'prices_on_page',
]

# Deterministic VARIANTS of checks the full profile classifies via LLM.
_DETERMINISTIC_VARIANT_IDS = frozenset({
    'E5b_raw_html_depth', 'F3b_faq_content_present', 'F6b_question_headings',
    'F8b_prices_visible', 'C13_city_in_title_h1',
})

_TRANSPORT_CLASSES = frozenset({
    'unresolved_redirect', 'bot_blocked', 'http_error', 'fetch_failed',
})


# ---------------------------------------------------------------------------
# Default fetchers — all riding the existing SSRF-guarded stack
# ---------------------------------------------------------------------------

def _default_bev(url: str) -> Dict[str, Any]:
    """Run the existing Tier-0 probe. Returns its JSON dict; degrades to an
    error dict (never raises)."""
    if not BEV_SCRIPT.exists():
        return {'error': f'bev script missing: {BEV_SCRIPT}'}
    try:
        r = subprocess.run(['bash', str(BEV_SCRIPT), url],
                           capture_output=True, text=True,
                           timeout=BEV_TIMEOUT_S)
        return json.loads(r.stdout)
    except subprocess.TimeoutExpired:
        return {'error': f'bev probe timed out after {BEV_TIMEOUT_S}s'}
    except (json.JSONDecodeError, Exception) as e:  # noqa: BLE001
        return {'error': f'bev probe failed: {type(e).__name__}: {e}'}


def _default_robots(url: str) -> Dict[str, Any]:
    try:
        return check_robots_txt.check_robots(url)
    except Exception as e:  # noqa: BLE001
        return {'error': f'robots check failed: {type(e).__name__}: {e}',
                'checks': {}, 'per_bot_access': {}}


def _default_page(url: str):
    """(html, status, error) via the schema validator's SSRF-guarded urllib
    fetcher — the redirect chain is validated hop by hop."""
    return fetch_html(url)


def _default_llms(url: str):
    p = urlparse(url)
    return fetch_html(f'{p.scheme}://{p.netloc}/llms.txt')


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------

def _finding(check_id: str, check: Dict[str, Any], url: str) -> Dict[str, Any]:
    """Wrap a light_checks-style check dict in the repo's finding shape.
    Producer-owned observed{} block so the shadow's evidence gate counts it
    (same contract as audit_pipeline.compute_section_scores)."""
    detail = check.get('detail') if isinstance(check.get('detail'), dict) else None
    f = {
        'check_id': check_id,
        'section': check_id[:1].upper(),
        'status': check.get('status', 'na'),
        'severity': check.get('severity', 'medium'),
        'evidence': check.get('evidence', ''),
        'evidence_tier': 'measured',
        'truth_badge': 'MEASURED',
        'observed': {
            'customer_url': url,
            'measured_value': check.get('evidence') or None,
            'detail': detail,
            'method': observed_method_for(check_id, detail),
        },
        'citations': [],
    }
    if check_id in _DETERMINISTIC_VARIANT_IDS:
        # Full profile classifies this factor via LLM; the light profile ships
        # a deterministic variant under a NEW base id (see scoring.py note).
        f['factor_variant'] = 'deterministic'
    return f


# (check_id, bots that must all be allowed, severity-on-deny, label)
_ROBOTS_BOT_CHECKS = [
    ('E1_perplexitybot_allowed', ['PerplexityBot'], 'high',
     'PerplexityBot'),
    ('E2_bingpreview_allowed', ['Bingbot', 'BingPreview'], 'medium',
     'Bingbot/BingPreview'),
    ('E3_googlebot_allowed', ['Googlebot'], 'critical', 'Googlebot'),
    ('E10_claudebot_chatgpt_applebot',
     ['ClaudeBot', 'Claude-Web', 'anthropic-ai', 'GPTBot', 'ChatGPT-User',
      'OAI-SearchBot', 'Applebot', 'Applebot-Extended'], 'high',
     'AI answer-engine crawlers (Claude/OpenAI/Applebot)'),
    ('E13_ccbot_llm_training_access', ['CCBot'], 'low',
     'CCBot (Common Crawl / LLM training)'),
]


def robots_findings(robots_out: Dict[str, Any], url: str) -> List[Dict[str, Any]]:
    """Map check_robots_txt output (+ per-bot breakdown) onto the canonical
    measured check ids E1/E2/E3/E10/E13/A10/A11."""
    checks = robots_out.get('checks') or {}
    access = robots_out.get('per_bot_access') or {}
    rt = robots_out.get('robots_txt') or {}
    reachable = bool(rt.get('reachable'))
    findings: List[Dict[str, Any]] = []

    for cid, bots, deny_sev, label in _ROBOTS_BOT_CHECKS:
        if not reachable or not access:
            check = {'status': 'warn', 'severity': 'medium',
                     'evidence': f'Cannot evaluate {label} access — robots.txt '
                                 f'unreachable (HTTP {rt.get("http_code")}).',
                     'detail': {'robots_http_code': rt.get('http_code')}}
        else:
            denied = [b for b in bots
                      if not (access.get(b) or {}).get('allowed', True)]
            per_bot = {b: (access.get(b) or {}) for b in bots}
            if denied:
                check = {'status': 'fail', 'severity': deny_sev,
                         'evidence': f'{label}: DENIED for {denied} by '
                                     f'robots.txt rules.',
                         'detail': {'denied': denied, 'per_bot': per_bot}}
            else:
                explicit = [b for b in bots
                            if (access.get(b) or {}).get('explicit')]
                check = {'status': 'pass', 'severity': 'info',
                         'evidence': f'{label}: all allowed'
                                     + (f' ({len(explicit)} explicitly listed).'
                                        if explicit else ' (wildcard/permissive).'),
                         'detail': {'per_bot': per_bot}}
        findings.append(_finding(cid, check, url))

    # A10 — target path crawlable (script's own aggregate verdict)
    tp = checks.get('target_path_not_disallowed') or {}
    findings.append(_finding('A10_robots_txt_crawling', {
        'status': tp.get('status', 'na'),
        'severity': tp.get('severity', 'medium'),
        'evidence': tp.get('evidence', 'robots.txt target-path verdict unavailable.'),
        'detail': {'robots_http_code': rt.get('http_code')},
    }, url))

    # A11 — sitemap declared in robots.txt
    sm = checks.get('robots_declares_sitemap') or {}
    findings.append(_finding('A11_sitemap_referenced', {
        'status': sm.get('status', 'na'),
        'severity': sm.get('severity', 'medium'),
        'evidence': sm.get('evidence', 'robots.txt sitemap verdict unavailable.'),
        'detail': {'sitemaps_declared': rt.get('sitemaps_declared')},
    }, url))
    return findings


# ---------------------------------------------------------------------------
# Gates (Tier-0)
# ---------------------------------------------------------------------------

def build_gates(bev: Dict[str, Any], robots_out: Dict[str, Any]) -> Dict[str, Any]:
    summary = bev.get('summary') or {}
    classification = bev.get('classification')
    rt = robots_out.get('robots_txt') or {}

    # page_existence 'fail' is reserved for transport-level nonexistence
    # (4xx/5xx/no-response) — scoring treats it as a hard INCONCLUSIVE gate.
    # A same-as-404 SPA shell is a CONTENT failure (spa_no_ssr scores as
    # fail-heavy on the deterministic path, it does not go inconclusive).
    page_existence = 'pass'
    if classification in ('http_error', 'fetch_failed', 'unresolved_redirect'):
        page_existence = 'fail'

    crawlability = 'pass'
    if classification == 'bot_blocked' or summary.get('bot_blocking_detected'):
        crawlability = 'fail'
    elif rt.get('http_code') and rt['http_code'] >= 500:
        crawlability = 'fail'   # RFC 9309: 5xx robots = assume full disallow
    elif not rt.get('reachable'):
        crawlability = 'warn'

    content_access = {
        'fully_accessible': 'pass',
        'partial_ssr': 'warn',
        'js_dependent': 'fail',
        'minimal_content': 'warn',
        'ssr_shell_js_hidden_content': 'fail',
        'spa_no_ssr': 'fail',
    }.get(classification or '', 'fail')

    return {
        'crawlability': crawlability,
        'content_access': content_access,
        'page_existence': page_existence,
        'details': {
            'bev_classification': classification,
            'bev_degraded': bool(bev.get('bev_degraded')),
            'http_code_default': summary.get('http_code_default'),
            'visible_words_default': summary.get('visible_words_default'),
            'robots_http_code': rt.get('http_code'),
            'bot_blocking_detected': summary.get('bot_blocking_detected'),
            'cloaking_detected': summary.get('cloaking_detected'),
            'critical_issues': (summary.get('critical_issues') or [])[:10],
        },
    }


# ---------------------------------------------------------------------------
# Deterministic narrative (no LLM, no fix generation)
# ---------------------------------------------------------------------------

def _narrative(findings: List[Dict[str, Any]], inconclusive: bool,
               reason: str) -> Dict[str, Any]:
    if inconclusive:
        return {
            'executive_diagnosis': f'Light audit inconclusive — {reason}. '
                                   f'No content factors could be measured.',
            'why_not_cited': [], 'top_5_fixes': [], 'quick_wins': [],
            'summary_what_to_do': 'Make the page reachable to non-JS clients, '
                                  'then re-run the audit.',
            'inconclusive': True, 'tokens_used': 0,
        }
    fails = [f for f in findings if f['status'] == 'fail']
    warns = [f for f in findings if f['status'] == 'warn']
    passes = [f for f in findings if f['status'] == 'pass']
    top = sorted(fails, key=lambda f: {'critical': 0, 'high': 1, 'medium': 2,
                                       'low': 3}.get(f.get('severity'), 4))
    diag = (f'Light audit — {len(findings)} deterministic checks over the '
            f'8 citation-readiness factors: {len(passes)} pass, '
            f'{len(warns)} warn, {len(fails)} fail.')
    if top:
        diag += f' Top issue: {top[0]["evidence"][:160]}'
    return {
        'executive_diagnosis': diag,
        'why_not_cited': [],
        'top_5_fixes': [],   # fix generation is a full-profile stage
        'quick_wins': [f'[{f["check_id"]}] {f["evidence"][:140]}'
                       for f in top[:5]],
        'summary_what_to_do': ('Address the failed measured factors above, '
                               'then run a full audit for fixes, citations, '
                               'and competitor context.'),
        'tokens_used': 0,
    }


# ---------------------------------------------------------------------------
# Classification (heuristic reuse — no LLM)
# ---------------------------------------------------------------------------

def _classify(bev: Dict[str, Any], entities: List[Dict[str, Any]],
              url: str) -> Dict[str, Any]:
    from audit_pipeline import classify_page_from_scripts
    compat = {
        'bots_eye_view': bev,
        'schema_completeness': {
            'entities': [{'type': normalize_type(e.get('@type'))}
                         for e in entities],
        },
    }
    try:
        c = classify_page_from_scripts(compat, url)
    except Exception:  # noqa: BLE001
        c = {'page_type': 'unknown', 'industry': 'other', 'confidence': 'low'}
    c.setdefault('company_name', None)
    return c


# ---------------------------------------------------------------------------
# MAIN ENTRY
# ---------------------------------------------------------------------------

def run_light_audit(url: str,
                    output_dir: str = './audits/',
                    target: str = 'brand',
                    session_ref: Optional[str] = None,
                    city: Optional[str] = None,
                    progress_callback: Optional[Callable[[Dict], None]] = None,
                    persist: bool = True,
                    fetchers: Optional[Dict[str, Callable]] = None) -> Dict[str, Any]:
    """Run a LIGHT audit. Returns the standard audit envelope.

    fetchers (tests only): {'bev','robots','page','llms'} overrides — lets the
    suite run the whole pipeline on saved bytes with no network.
    """
    if target not in VALID_TARGETS:
        target = 'brand'
    fx = fetchers or {}
    bev_fn = fx.get('bev', _default_bev)
    robots_fn = fx.get('robots', _default_robots)
    page_fn = fx.get('page', _default_page)
    llms_fn = fx.get('llms', _default_llms)

    audit_id = str(uuid.uuid4())
    started = time.time()

    def _progress(phase: str, pct: int):
        if progress_callback:
            try:
                progress_callback({'phase': phase, 'pct_hint': pct,
                                   'elapsed_seconds': round(time.time() - started, 1),
                                   'profile': 'light'})
            except Exception:  # noqa: BLE001
                pass

    _progress('light:probe', 10)

    # Fetch everything in parallel — each fetcher is independently
    # SSRF-guarded and never raises.
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_bev = pool.submit(bev_fn, url)
        f_robots = pool.submit(robots_fn, url)
        f_page = pool.submit(page_fn, url)
        f_llms = pool.submit(llms_fn, url)
        bev = f_bev.result() or {}
        robots_out = f_robots.result() or {}
        page_html, page_status, page_err = f_page.result()
        llms_body, llms_status, llms_err = f_llms.result()

    _progress('light:checks', 55)

    summary = bev.get('summary') or {}
    classification_label = bev.get('classification')
    if not classification_label and 'error' in bev:
        # BEV degraded — derive a transport class from the direct page fetch
        # so the transport gate still works.
        if page_html:
            # Content reached directly — classify from the fetched bytes with
            # the same word-count thresholds the BEV classifier uses, so the
            # content_access gate reflects what was actually measured. Without
            # this, build_gates would map the empty classification to 'fail'
            # and report a content-access failure caused purely by
            # probe-infrastructure failure, contradicting the measured
            # findings in the same envelope.
            wc = light_checks.visible_word_count(page_html)
            classification_label = ('fully_accessible' if wc >= 500
                                    else 'partial_ssr' if wc >= 200
                                    else 'minimal_content')
            bev = {**bev, 'classification': classification_label,
                   'summary': {'http_code_default': page_status,
                               'visible_words_default': wc},
                   'bev_degraded': True}
            summary = bev['summary']
        elif page_status and page_status >= 400:
            classification_label = 'bot_blocked' if page_status in (401, 403, 429) \
                else 'http_error'
            bev = {**bev, 'classification': classification_label,
                   'summary': {'http_code_default': page_status}}
        else:
            classification_label = 'fetch_failed'
            bev = {**bev, 'classification': classification_label, 'summary': {}}

    transport_inconclusive = classification_label in _TRANSPORT_CLASSES

    findings: List[Dict[str, Any]] = []
    findings.extend(robots_findings(robots_out, url))
    findings.append(_finding('E14_llms_txt',
                             light_checks.check_llms_txt(llms_status, llms_body,
                                                         llms_err), url))

    entities: List[Dict[str, Any]] = []
    if page_html and not transport_inconclusive:
        entities = light_checks.page_entities(page_html)
        page_checks = light_checks.run_page_checks(
            page_html, city=city,
            visible_words=summary.get('visible_words_default'))
        for cid, check in page_checks.items():
            findings.append(_finding(cid, check, url))
    elif not transport_inconclusive and not page_html:
        # BEV reached content but the direct fetch failed (rare split) —
        # content factors unmeasurable; record honestly as n/a.
        for cid in ('E5b_raw_html_depth', 'D15_localbusiness_geo_schema',
                    'C13_city_in_title_h1', 'F3b_faq_content_present',
                    'D9_faqpage_schema_vs_visible', 'F6b_question_headings',
                    'F8b_prices_visible'):
            findings.append(_finding(cid, {
                'status': 'na', 'severity': 'info',
                'evidence': f'Page bytes unavailable for content factor '
                            f'({page_err or f"HTTP {page_status}"}).',
                'detail': {'http_status': page_status}}, url))

    _progress('light:scoring', 80)

    gates = build_gates(bev, robots_out)
    audit_ctx = {'bots_eye_view': bev, 'gates': gates}
    scoring = compute_from_findings(findings, audit_ctx)
    narrative = _narrative(findings, scoring.get('inconclusive', False),
                           scoring.get('inconclusive_reason', ''))
    classification = _classify(bev, entities, url)

    domain = re.sub(r'^https?://', '', url).rstrip('/').split('/')[0]
    audit: Dict[str, Any] = {
        'audit_id': audit_id,
        'url': url,
        'domain': domain,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'duration_seconds': round(time.time() - started, 1),
        'profile': 'light',
        'target': target,
        'session_ref': session_ref,
        'classification': classification,
        'context': {'competitors': [], 'test_queries': []},
        'gates': gates,
        'scoring': scoring,
        'findings': findings,
        'narrative': narrative,
        'bots_eye_view': bev,
        'performance': {},   # Playwright render is a full-profile stage
        'metadata': {
            'version': LIGHT_VERSION,
            'profile': 'light',
            'target': target,
            'session_ref': session_ref,
            'mode': 'light-deterministic',
            'llm_calls': 0,
            'cost_usd': 0.0,
            'factors': LIGHT_FACTORS,
            'checks_run': len(findings),
            'city': city,
        },
    }
    validate_audit(audit)

    # Artifacts — JSON always; Markdown best-effort via the shared renderer.
    try:
        out_dir = Path(output_dir).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        base = out_dir / f'{domain.replace(".", "-")}-{audit_id[:8]}'
        json_path = base.with_suffix('.json')
        json_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False))
        audit['json_path'] = str(json_path)
        try:
            from audit_pipeline import render_markdown_report
            md_compat = {**audit,
                         'scripts_output': {'bots_eye_view': bev,
                                            'all_checks': {}},
                         'brain_stats': {}}
            md_path = base.with_suffix('.md')
            md_path.write_text(render_markdown_report(md_compat))
            audit['md_path'] = str(md_path)
        except Exception as e:  # noqa: BLE001
            log.debug('light md render skipped: %s', e)
            audit['md_path'] = None
        audit['pdf_path'] = None   # 1-page PDF is a full-profile artifact
    except Exception as e:  # noqa: BLE001
        log.warning('light artifact write failed: %s', e)

    _progress('light:persist', 92)

    if persist:
        try:
            from tools import persist_audit
            audit['metadata']['persistence'] = persist_audit(audit)
        except Exception as e:  # noqa: BLE001
            audit['metadata']['persistence'] = {'persisted': False,
                                                'error': str(e)}
        try:
            from persistence import post_to_answermonk
            audit['metadata']['answermonk_sync'] = post_to_answermonk(audit)
        except Exception as e:  # noqa: BLE001
            audit['metadata']['answermonk_sync'] = {'posted': False,
                                                    'error': str(e)}

    _progress('light:done', 99)
    return audit
