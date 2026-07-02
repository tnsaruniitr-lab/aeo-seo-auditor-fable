"""
audit_pipeline.py — End-to-end audit pipeline.

Orchestrates a complete website audit from a URL input:
    1. Run deterministic scripts (curl, schema parse, etc.)
    2. Load brain snapshots
    3. For each failed check, run ranker → top 3 citations
    4. Single Anthropic Sonnet 4.6 call → structured narrative
    5. Render Markdown report + 1-page PDF summary
    6. Return all artifacts

USAGE
    from audit_pipeline import run_audit

    result = run_audit(url="https://example.com", output_dir="./audits/")
    # result = { audit_id, score, grade, json_path, md_path, pdf_path, ... }

DEPENDENCIES
    - auditor-ruleset-export/ranker.py (deterministic citation selector)
    - auditor-ruleset-export/*.json (4 brain snapshots)
    - skill-unified/scripts/run_deterministic.sh (the existing 23-check engine)
    - ANTHROPIC_API_KEY environment variable
    - anthropic SDK (pip install anthropic)
    - Optional: Chrome installed at standard path for PDF rendering

RUNTIME
    Synchronous. ~60-120 seconds end to end:
        - 30s: deterministic scripts
        - 30s: Sonnet API call
        - 5s: PDF render
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

# Bot's-Eye-View classifications that mean the probe never reached real
# page content. A page in one of these states must NOT be scored or have
# content findings written about it — the redirect-incident failure mode.
TRANSPORT_CLASSES = frozenset({
    'unresolved_redirect', 'bot_blocked', 'http_error', 'fetch_failed',
})


def bev_summary(scripts_output: Dict) -> Dict:
    """Read the Bot's-Eye-View analyzer's ACTUAL output contract.

    The analyzer (_bev_analyze.py) emits a `summary` block plus per-UA
    `probes`; it has never emitted `page_identity` / `content_visible_to_bots`
    / `response_per_bot`. Reading those phantom keys silently yielded empty
    data (and a false "FAQ visible: 0" in every report). This helper is the
    single source of truth for the real fields.
    """
    bev = scripts_output.get('bots_eye_view') or {}
    summary = bev.get('summary') or {}
    probes = bev.get('probes') or {}
    default_probe = probes.get('default') or {}
    return {
        'classification': bev.get('classification'),
        'is_transport_inconclusive': bev.get('classification') in TRANSPORT_CLASSES,
        'visible_words': summary.get('visible_words_default'),
        'h1_first': default_probe.get('h1_first'),
        'http_code': summary.get('http_code_default'),
        'final_url': summary.get('final_url'),
        'redirects_followed': summary.get('redirects_followed'),
        'faq_visible': summary.get('faq_visible'),
        'faq_schema': summary.get('faq_schema'),
        'faq_schema_questions_visible': summary.get('faq_schema_questions_visible'),
        'faq_integrity': summary.get('faq_integrity'),
        'same_html_as_404_url': summary.get('same_html_as_404_url'),
        'soft_404_redirect': summary.get('soft_404_redirect'),
        'cloaking_detected': summary.get('cloaking_detected'),
        'bot_blocking_detected': summary.get('bot_blocking_detected'),
        'critical_issues': summary.get('critical_issues') or [],
        'spa_signals': summary.get('spa_signals') or [],
    }

# Add the ruleset export to import path
THIS_DIR = Path(__file__).resolve().parent
RULESET_DIR = THIS_DIR / 'ruleset'
SCRIPTS_DIR = THIS_DIR / 'scripts'

sys.path.insert(0, str(RULESET_DIR))
from ranker import BrainIndex, select_citations, format_citation, get_tier_rank, TIER_ICONS  # noqa


# ----------------------------------------------------------------------
# CONFIGURATION
# ----------------------------------------------------------------------

ANTHROPIC_MODEL = 'claude-sonnet-4-6'
ANTHROPIC_MAX_TOKENS = 8000
DETERMINISTIC_SCRIPT = SCRIPTS_DIR / 'run_deterministic.sh'


def _resolve_chrome() -> Optional[str]:
    """Find a Chrome/Chromium binary for headless PDF rendering.

    Honors the CHROME_PATH env var first (the Dockerfile sets it to
    /usr/bin/chromium on Railway/Linux), then falls back to the usual Linux and
    macOS locations. Returns None if none exist — the old code hardcoded a macOS
    path, so every /pdf 404'd in production (SD-6)."""
    candidates = [
        os.getenv('CHROME_PATH'),
        '/usr/bin/chromium',
        '/usr/bin/chromium-browser',
        '/usr/bin/google-chrome',
        '/usr/bin/google-chrome-stable',
        '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
        '/Applications/Chromium.app/Contents/MacOS/Chromium',
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    return None


CHROME_PATH = _resolve_chrome()


# ----------------------------------------------------------------------
# STEP 1: DETERMINISTIC LAYER
# ----------------------------------------------------------------------

def run_deterministic_scripts(url: str, timeout: int = 180) -> Dict:
    """Invoke run_deterministic.sh and return the parsed JSON output.

    Returns the same structure today's audits use:
        {
          'bots_eye_view': {...},
          'all_checks': {check_id: {status, evidence, ...}},
          'overall_summary': {...},
          'sitemap_analysis': {...},
          'robots_txt_analysis': {...},
          'schema_completeness': {...},
        }

    On script failure, returns a minimal error dict.
    """
    if not DETERMINISTIC_SCRIPT.exists():
        return {
            'error': f'Script not found: {DETERMINISTIC_SCRIPT}',
            'all_checks': {},
            'overall_summary': {'pass': 0, 'fail': 0, 'warn': 0, 'na': 0,
                                'all_critical_issues': []},
        }

    try:
        result = subprocess.run(
            ['bash', str(DETERMINISTIC_SCRIPT), url],
            capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return {'error': 'deterministic scripts timed out',
                'all_checks': {}, 'overall_summary': {}}

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return {
            'error': f'JSON parse: {e}',
            'stdout_first_500': result.stdout[:500],
            'stderr_first_500': result.stderr[:500],
            'all_checks': {},
            'overall_summary': {},
        }


# ----------------------------------------------------------------------
# STEP 2: PAGE CLASSIFICATION (light)
# ----------------------------------------------------------------------

def classify_page_from_scripts(scripts_output: Dict, url: str) -> Dict:
    """Infer page_type + industry from script signals.

    This is a lightweight classifier — uses deterministic signals only.
    A heavier classifier (LLM-based) could be added later.
    """
    bev = bev_summary(scripts_output)
    schema = scripts_output.get('schema_completeness', {})

    # The analyzer emits no page title; H1 lives on the default probe.
    h1 = (bev.get('h1_first') or '').lower()
    title = h1  # best available proxy for keyword signals
    schema_types = []
    for ent in schema.get('entities', []):
        t = ent.get('type', '')
        if isinstance(t, str):
            schema_types.append(t.lower())

    # URL-based signal: root = no path beyond '/'. Path-based beats a TLD
    # suffix list (which missed .dev/.xyz and broke on ports/query strings).
    url_lower = url.lower()
    try:
        from urllib.parse import urlparse
        _path = urlparse(url_lower).path or '/'
        is_root = _path in ('', '/')
    except Exception:
        is_root = url_lower.rstrip('/').count('/') <= 2

    # Page type heuristic
    if 'blogposting' in schema_types or 'article' in schema_types or '/blog/' in url_lower:
        page_type = 'blog'
    elif 'product' in schema_types or '/product' in url_lower or '/shop' in url_lower:
        page_type = 'product'
    elif 'localbusiness' in schema_types or 'medicalbusiness' in schema_types \
         or 'medicalclinic' in schema_types:
        page_type = 'local_business'
    elif 'softwareapplication' in schema_types or 'mobileapplication' in schema_types:
        page_type = 'software_application'
    elif is_root:
        page_type = 'homepage'
    else:
        page_type = 'service'

    # Industry heuristic
    healthcare_kw = ['health', 'medical', 'pflege', 'doctor', 'nurse',
                     'clinic', 'hospital', 'gesundheit', 'arzt']
    if any(kw in title or kw in h1 for kw in healthcare_kw) \
       or any(kw in s for s in schema_types for kw in ['medical']):
        industry = 'healthcare'
    elif 'softwareapplication' in schema_types or 'mobileapplication' in schema_types:
        industry = 'saas'
    elif 'product' in schema_types:
        industry = 'ecommerce'
    else:
        industry = 'other'

    return {
        'page_type': page_type,
        'industry': industry,
        'confidence': 'medium',  # heuristic
        'signals': {
            'title': title[:100],
            'schema_types': schema_types[:5],
            'is_root_url': is_root,
        }
    }


# ----------------------------------------------------------------------
# STEP 3: ATTACH CITATIONS TO FAILED CHECKS
# ----------------------------------------------------------------------

def enrich_findings_with_citations(
    scripts_output: Dict,
    brain: BrainIndex,
    page_type: str,
    industry: str,
) -> List[Dict]:
    """Walk all_checks; for each failed/warned check, attach top 3 citations.

    Returns a list of finding dicts, each with:
        {check_id, status, severity, evidence, citations: [...]}
    """
    findings = []
    all_checks = scripts_output.get('all_checks', {})

    for full_check_id, check_data in all_checks.items():
        status = check_data.get('status')
        if status not in ('fail', 'warn'):
            continue

        # Strip prefix (e.g. 'det_checks:D9_xxx' → 'D9_xxx')
        clean_check_id = full_check_id.split(':', 1)[-1]

        citations = select_citations(
            brain=brain,
            check_id=clean_check_id,
            page_type=page_type,
            industry=industry,
            max_citations=3,
        )

        findings.append({
            'check_id': clean_check_id,
            'full_check_id': full_check_id,
            'status': status,
            'severity': check_data.get('severity', 'medium'),
            'evidence': check_data.get('evidence', ''),
            'citations': citations,
        })

    # Sort findings: critical → high → medium → low; fail before warn
    severity_rank = {'critical': 0, 'high': 1, 'medium': 2, 'low': 3, 'info': 4}
    status_rank = {'fail': 0, 'warn': 1}
    findings.sort(key=lambda f: (
        status_rank.get(f['status'], 9),
        severity_rank.get(f.get('severity', 'medium'), 5),
    ))

    return findings


# ----------------------------------------------------------------------
# STEP 4: SONNET API CALL FOR NARRATIVE
# ----------------------------------------------------------------------

def call_sonnet_for_narrative(
    url: str,
    classification: Dict,
    scripts_output: Dict,
    findings: List[Dict],
) -> Dict:
    """Single Anthropic Sonnet 4.6 call to compose the audit narrative.

    Input is fully structured; output is a tight JSON schema with named fields.

    Returns:
        {
          'executive_diagnosis': str,
          'why_not_cited': [{title, badge, body, citations}, ...],
          'top_5_fixes': [{rank, title, impact, effort, type, before, after, why}, ...],
          'quick_wins': [str, ...],
          'summary_what_to_do': str,
          'tokens_used': int,
        }
    """
    bev = bev_summary(scripts_output)

    # Transport-inconclusive probe: the page content was never reached, so
    # there is nothing to diagnose. Return a fixed, honest narrative WITHOUT
    # calling the model (and regardless of whether the API key is set) instead
    # of asking it to invent reasons a page "isn't cited".
    if bev['is_transport_inconclusive']:
        cls = bev['classification']
        reason = {
            'unresolved_redirect': (
                f"The page did not resolve to final content — it is still redirecting "
                f"(HTTP {bev.get('http_code')}) after following redirects. "
                f"Re-run the audit against the final URL ({bev.get('final_url') or 'unknown'})."),
            'bot_blocked': (
                f"The site returned HTTP {bev.get('http_code')} to the audit client, "
                f"blocking access. Crawler-specific results may differ — see the probe table."),
            'http_error': (
                f"The page returned HTTP {bev.get('http_code')}; the analyzed response was an "
                f"error page, not the live content."),
            'fetch_failed': (
                "The page could not be fetched (connection error or timeout); no content "
                "could be analyzed."),
        }.get(cls, "The page probe was inconclusive; no content could be analyzed.")
        return {
            'executive_diagnosis': (
                f"Audit inconclusive — {reason} No content, schema, or AEO findings can be "
                f"reported until the page is reachable."),
            'why_not_cited': [],
            'top_5_fixes': [],
            'quick_wins': [],
            'summary_what_to_do': reason,
            'inconclusive': True,
            'tokens_used': 0,
        }

    try:
        from anthropic import Anthropic
    except ImportError:
        return {
            'error': 'anthropic SDK not installed. Run: pip install anthropic',
            'executive_diagnosis': 'LLM layer unavailable.',
            'why_not_cited': [],
            'top_5_fixes': [],
            'quick_wins': [],
            'summary_what_to_do': '',
            'tokens_used': 0,
        }

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        return {
            'error': 'ANTHROPIC_API_KEY not set in environment',
            'executive_diagnosis': '',
            'why_not_cited': [],
            'top_5_fixes': [],
            'quick_wins': [],
            'summary_what_to_do': '',
            'tokens_used': 0,
        }

    client = Anthropic(api_key=api_key)

    # Compact context payload for the LLM — read the analyzer's REAL keys.
    context = {
        'url': url,
        'classification': classification,
        'visible_words': bev['visible_words'],
        'classification_label': bev['classification'],
        'h1': bev['h1_first'],
        'faq': {
            'visible': bev['faq_visible'],
            'schema': bev['faq_schema'],
            'schema_questions_visible': bev['faq_schema_questions_visible'],
            'integrity': bev['faq_integrity'],
        },
        'bot_blocking_detected': bev['bot_blocking_detected'],
        'cloaking_detected': bev['cloaking_detected'],
        'probe_critical_issues': bev['critical_issues'],
        'overall_summary': scripts_output.get('overall_summary', {}),
        'top_findings': [
            {
                'check_id': f['check_id'],
                'status': f['status'],
                'severity': f['severity'],
                'evidence': f['evidence'][:300],
                'citations': [
                    {
                        'kind': c['kind'],
                        'id': c['id'],
                        'tier': c['tier'],
                        'tier_icon': c['tier_icon'],
                        'name': c.get('name') or c.get('title', ''),
                        'source_org': c.get('source_org'),
                        'source_url': c.get('source_url'),
                        'confidence_score': c.get('confidence_score'),
                        'risk_level': c.get('risk_level'),
                        'if_condition': c.get('if_condition', '')[:200],
                        'then_action': c.get('then_action', '')[:200],
                        'description': c.get('description', '')[:200],
                    }
                    for c in f.get('citations', [])
                ],
            }
            for f in findings[:25]  # cap context size
        ],
    }

    system_prompt = """You are a website SEO + AEO + GEO auditor producing a final \
structured audit report.

You will receive structured findings (deterministic, already analyzed) and selected \
citation rules (deterministic, already chosen). Your ONLY job is to compose narrative \
prose that explains them clearly. Do NOT pick which rules to cite — that's already \
been done. Use the citations exactly as given.

Output MUST be valid JSON matching this exact schema:

{
  "executive_diagnosis": "1-2 sentence top-level diagnosis",
  "why_not_cited": [
    {
      "title": "short title",
      "badge": "HARD EVIDENCE | STATIC RULE | MEASURED | HEURISTIC | MODEL JUDGMENT",
      "body": "1-paragraph explanation",
      "citation_indexes": [0, 1]
    }
  ],
  "top_5_fixes": [
    {
      "rank": 1,
      "title": "Fix title",
      "impact": "Critical | High | Medium",
      "effort": "Trivial | Easy | Moderate | Heavy",
      "type": "PAGE HTML FIX | SCHEMA FIX | CONTENT RESTRUCTURE | SITEWIDE TEMPLATE FIX | OFF-PAGE | ...",
      "before": "what's currently broken — be specific",
      "after": "exact recommended state — include code blocks if relevant",
      "why": "1-paragraph explanation invoking specific citations"
    }
  ],
  "quick_wins": ["one-line fix descriptions"],
  "summary_what_to_do": "1-paragraph honest framing — what to do this week"
}

GUIDELINES:
- Up to 3 entries in "why_not_cited" — ONLY reasons supported by the supplied \
findings. If findings are thin, return fewer (even zero). Never invent a reason \
that the findings do not support.
- Up to 5 entries in "top_5_fixes", each tied to a specific finding (fewer if \
findings are limited)
- "citation_indexes" refer to position in top_findings[i].citations array
- Be specific to the brand and page type. No generic advice.
- Quote citation source names verbatim (e.g., "Per Schema.org's official documentation")
- Use truth badge that matches: HARD EVIDENCE for binary tag presence, MEASURED for \
metrics, STATIC RULE for static-rule checks, HEURISTIC/MODEL JUDGMENT for inferences.
"""

    user_prompt = f"""Audit context:

```json
{json.dumps(context, indent=2, ensure_ascii=False)[:20000]}
```

Compose the audit narrative as JSON per the schema. Return ONLY the JSON object — \
no preamble, no markdown fences."""

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=ANTHROPIC_MAX_TOKENS,
            system=system_prompt,
            messages=[{'role': 'user', 'content': user_prompt}],
        )
        text = response.content[0].text.strip()
        # Strip markdown fences if present
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        narrative = json.loads(text)
        narrative['tokens_used'] = response.usage.input_tokens + response.usage.output_tokens
        return narrative
    except json.JSONDecodeError as e:
        return {
            'error': f'LLM returned non-JSON: {e}',
            'raw': text[:1000] if 'text' in locals() else '',
            'executive_diagnosis': '',
            'why_not_cited': [],
            'top_5_fixes': [],
            'quick_wins': [],
            'summary_what_to_do': '',
            'tokens_used': 0,
        }
    except Exception as e:
        return {
            'error': f'{type(e).__name__}: {e}',
            'executive_diagnosis': '',
            'why_not_cited': [],
            'top_5_fixes': [],
            'quick_wins': [],
            'summary_what_to_do': '',
            'tokens_used': 0,
        }


# ----------------------------------------------------------------------
# STEP 5: SCORING (sectional + overall)
# ----------------------------------------------------------------------

def compute_section_scores(scripts_output: Dict) -> Dict:
    """Compute per-section scores + overall grade for the deterministic path.

    Delegates to scoring.py — the single source of truth for weights and grade
    cutoffs — so this legacy path can never disagree with the agent path (the
    old copy here used a different weight table summing to 0.95 and a different
    grade table, which was finding SCORE-2 / ENG-4). Converts the all_checks
    map into the findings shape scoring.compute_from_findings expects.
    """
    from scoring import compute_from_findings

    findings = []
    for full_id, check in scripts_output.get('all_checks', {}).items():
        clean = full_id.split(':', 1)[-1]
        findings.append({
            'check_id': clean,
            'status': (check or {}).get('status', 'na'),
        })

    # Provide the Bot's-Eye-View context so scoring's transport gate can fire.
    audit_ctx = {'bots_eye_view': scripts_output.get('bots_eye_view', {})}
    return compute_from_findings(findings, audit_ctx)


# ----------------------------------------------------------------------
# STEP 6: RENDER MARKDOWN + PDF
# ----------------------------------------------------------------------

def render_markdown_report(audit: Dict) -> str:
    """Render the full audit as Markdown matching the existing audit-reports/ format."""
    url = audit['url']
    domain = audit['domain']
    classification = audit['classification']
    scoring = audit['scoring']
    narrative = audit['narrative']
    findings = audit['findings']
    bev = bev_summary(audit['scripts_output'])

    md = []
    md.append(f"# SEO + AEO + GEO Audit Report\n")
    md.append(f"**URL:** {url}")
    md.append(f"**Domain:** {domain}")
    md.append(f"**Page Type:** {classification['page_type']} ({classification['confidence']} confidence)")
    md.append(f"**Industry:** {classification['industry']}")
    md.append(f"**Date:** {audit['date']}")
    md.append(f"**Audit ID:** `{audit['audit_id']}`")
    md.append(f"**Audit Version:** 4.0 (standalone, deterministic-script-backed)")
    md.append("")
    md.append(f"---\n")

    md.append("## Executive Diagnosis\n")
    md.append(narrative.get('executive_diagnosis', '(not available)'))
    md.append("")

    md.append("## Scores\n")
    if scoring.get('overall_score') is None:
        md.append(f"### Page Citation Readiness: INCONCLUSIVE\n")
        md.append(f"> The page probe was inconclusive "
                  f"({scoring.get('inconclusive_reason', 'content not reached')}); "
                  f"no score can be assigned until the page is reachable.\n")
    else:
        md.append(f"### Page Citation Readiness: {scoring['overall_score']}% ({scoring['overall_grade']})\n")
    md.append("| Section | Score |")
    md.append("|---|---|")
    for k, v in scoring['section_scores'].items():
        if k.startswith(('section', 'overall', 'page_citation', 'brand_ai')):
            continue
        if v is None:
            md.append(f"| {k} | N/A |")
        else:
            md.append(f"| {k} | {v}% |")
    md.append("")

    md.append("## Why This Page Isn't Being Cited\n")
    for i, item in enumerate(narrative.get('why_not_cited', []), 1):
        md.append(f"### {i}. {item.get('title', '')} [{item.get('badge', '')}]")
        md.append(item.get('body', ''))
        md.append("")

    def _cell(v):
        return 'n/a' if v is None else v

    md.append("## Bot's Eye View\n")
    md.append("| Metric | Value | Source |")
    md.append("|---|---|---|")
    md.append(f"| Classification | {_cell(bev['classification'])} | script |")
    md.append(f"| HTTP status (default UA) | {_cell(bev['http_code'])} | curl |")
    if bev['redirects_followed']:
        md.append(f"| Final URL after redirects | {_cell(bev['final_url'])} | curl |")
    md.append(f"| Visible word count | {_cell(bev['visible_words'])} | curl |")
    md.append(f"| H1 | {_cell(bev['h1_first'])} | curl |")
    md.append(f"| FAQ visible (widget) | {_cell(bev['faq_visible'])} | script |")
    md.append(f"| FAQ in schema | {_cell(bev['faq_schema'])} | script |")
    md.append(f"| FAQ schema questions in HTML | {_cell(bev['faq_schema_questions_visible'])} | script |")
    md.append(f"| FAQ integrity | {_cell(bev['faq_integrity'])} | script |")
    md.append(f"| Same HTML as 404 URL | {_cell(bev['same_html_as_404_url'])} | script |")
    md.append(f"| Bot UA blocking detected | {_cell(bev['bot_blocking_detected'])} | script |")
    md.append(f"| Cloaking detected | {_cell(bev['cloaking_detected'])} | script |")
    if bev['critical_issues']:
        md.append("")
        md.append("**Probe critical issues:**")
        for ci in bev['critical_issues']:
            md.append(f"- {ci}")
    md.append("")

    md.append("## Top 5 Fixes\n")
    for fix in narrative.get('top_5_fixes', []):
        md.append(f"### Fix #{fix.get('rank')}: {fix.get('title', '')}")
        md.append(f"**Impact:** {fix.get('impact')} | **Effort:** {fix.get('effort')} | **Type:** {fix.get('type')}\n")
        md.append(f"**BEFORE:**\n{fix.get('before', '')}\n")
        md.append(f"**AFTER:**\n{fix.get('after', '')}\n")
        md.append(f"**WHY:** {fix.get('why', '')}\n")

    md.append("## Quick Wins\n")
    for qw in narrative.get('quick_wins', []):
        md.append(f"- {qw}")
    md.append("")

    md.append("## Per-Section Findings (Layer 2)\n")
    md.append("| Status | Check ID | Severity | Evidence |")
    md.append("|---|---|---|---|")
    for f in findings[:50]:
        icon = {'fail': '✗', 'warn': '⚠'}.get(f['status'], '?')
        md.append(f"| {icon} | {f['check_id']} | {f['severity']} | {f['evidence'][:140]} |")
    md.append("")

    md.append("## Brain Intelligence Applied\n")
    seen_ids = set()
    by_tier = {1: [], 2: [], 3: [], 4: [], 5: []}
    for f in findings:
        for c in f.get('citations', []):
            if c['id'] in seen_ids:
                continue
            seen_ids.add(c['id'])
            by_tier[c['tier']].append(c)
    for tier in [1, 2, 3, 4, 5]:
        if not by_tier[tier]:
            continue
        icon = TIER_ICONS[tier]
        md.append(f"### {icon} Tier {tier} sources\n")
        for c in by_tier[tier][:10]:
            org = c.get('source_org', 'unknown')
            name = c.get('name') or c.get('title', '(no name)')
            kind_label = 'Rule' if c['kind'] == 'rule' else 'AP'
            md.append(f"- **{org}** — \"{name}\" [Sieve {kind_label} #{c['id']}]")
        md.append("")

    md.append("## Audit Metadata\n")
    md.append(f"- Version: 4.0 (standalone)")
    md.append(f"- Brain entries: {audit['brain_stats']}")
    md.append(f"- Anthropic model: {ANTHROPIC_MODEL}")
    md.append(f"- Tokens used: {narrative.get('tokens_used', 0)}")
    md.append(f"- Total checks: {sum(c.get('pass', 0) + c.get('fail', 0) + c.get('warn', 0) + c.get('na', 0) for c in scoring['section_counts'].values())}")
    md.append("")

    md.append(f"---")
    md.append(f"*Generated by aeo-seo-auditor standalone v4.0*")

    return '\n'.join(md)


def render_pdf_summary(audit: Dict, output_path: Path) -> Optional[Path]:
    """Render a 1-page PDF executive summary using Chrome headless.

    Returns path to PDF, or None if no renderer is available.
    """
    html_path = output_path.with_suffix('.html')
    narrative = audit['narrative']
    scoring = audit['scoring']

    def esc(v):
        return html.escape(str(v if v is not None else ''))

    issues_rows = ''
    for f in audit['findings'][:8]:
        sev = f.get('severity', 'medium')
        sev_class = {'critical': 'crit', 'high': 'high',
                     'medium': 'med', 'low': 'low'}.get(sev, 'low')
        issues_rows += (
            f'<tr><td class="num">{esc(f.get("check_id"))}</td>'
            f'<td>{esc((f.get("evidence") or "")[:180])}</td>'
            f'<td class="tag {sev_class}">{esc(sev)}</td></tr>'
        )

    fixes_rows = ''
    for fix in narrative.get('top_5_fixes', [])[:5]:
        fixes_rows += (
            f'<li><strong>{esc(fix.get("title"))}</strong> '
            f'<em>({esc(fix.get("impact"))} impact, {esc(fix.get("effort"))} effort)</em></li>'
        )

    html_doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{audit["domain"]} Audit Summary</title>
<style>
@page {{ size: A4; margin: 10mm 12mm; }}
body {{ font-family: -apple-system, "Helvetica Neue", Arial, sans-serif;
        font-size: 9pt; line-height: 1.32; color: #111; margin: 0; padding: 0; }}
h1 {{ font-size: 16pt; margin: 0 0 2px 0; color: #0a2540; }}
.subtitle {{ font-size: 9pt; color: #555; margin-bottom: 6px; }}
.score-band {{ background: #fef2f2; border-left: 3px solid #dc2626;
               padding: 6px 10px; margin: 0 0 8px 0; font-size: 10pt;
               font-weight: 600; color: #7f1d1d; }}
.score-band span.score {{ font-size: 14pt; color: #dc2626; margin-right: 6px; }}
h2 {{ font-size: 10pt; margin: 9px 0 4px 0; padding-bottom: 2px;
      border-bottom: 1.5px solid #0a2540; color: #0a2540;
      letter-spacing: 0.2px; text-transform: uppercase; }}
table {{ width: 100%; border-collapse: collapse; margin: 3px 0 6px 0; font-size: 8.5pt; }}
th, td {{ padding: 3px 5px; text-align: left; border-bottom: 1px solid #e5e7eb;
         vertical-align: top; }}
th {{ background: #f8fafc; font-weight: 600; font-size: 8pt;
      text-transform: uppercase; color: #475569; }}
td.num {{ width: 80px; font-family: monospace; color: #64748b; font-weight: 600; }}
td.tag {{ width: 60px; font-size: 7.5pt; font-weight: 600; text-transform: uppercase; }}
.crit {{ color: #dc2626; }} .high {{ color: #ea580c; }}
.med  {{ color: #ca8a04; }} .low  {{ color: #64748b; }}
ol {{ margin: 2px 0 4px 14px; padding: 0; }}
li {{ margin: 1px 0; }}
.footer {{ margin-top: 8px; font-size: 7.5pt; color: #94a3b8;
           text-align: center; border-top: 1px solid #e5e7eb; padding-top: 4px; }}
</style></head><body>
<h1>{esc(audit["domain"])} — AI Visibility Audit</h1>
<div class="subtitle">{esc(audit["classification"]["page_type"])} · {esc(audit["date"])} · Audit ID: <code>{esc(audit["audit_id"][:8])}</code></div>
<div class="score-band">
  <span class="score">{'INCONCLUSIVE' if scoring.get('overall_score') is None else esc(scoring['overall_score']) + ' / 100'}</span> · Grade {esc(scoring["overall_grade"])}
  &nbsp;|&nbsp; <span style="color:#334155">{esc(narrative.get("executive_diagnosis","")[:200])}</span>
</div>
<h2>Top issues</h2>
<table><thead><tr><th>Check</th><th>Issue</th><th>Severity</th></tr></thead>
<tbody>{issues_rows}</tbody></table>
<h2>Top 5 fixes</h2>
<ol>{fixes_rows}</ol>
<h2>Honest framing</h2>
<p>{esc(narrative.get("summary_what_to_do","")[:600])}</p>
<div class="footer">Generated by aeo-seo-auditor standalone v4.0 · model: {ANTHROPIC_MODEL}</div>
</body></html>"""

    html_path.write_text(html_doc)
    pdf_path = output_path.with_suffix('.pdf')

    # Path 1: a system Chrome/Chromium binary (fast, no extra deps).
    if CHROME_PATH:
        try:
            subprocess.run([
                CHROME_PATH, '--headless=new', '--disable-gpu', '--no-sandbox',
                f'--print-to-pdf={pdf_path}',
                '--no-pdf-header-footer',
                f'file://{html_path}'
            ], capture_output=True, timeout=30)
            if pdf_path.exists() and pdf_path.stat().st_size > 0:
                return pdf_path
        except subprocess.TimeoutExpired:
            pass
        except Exception:
            pass

    # Path 2: Playwright's bundled Chromium (already a project dependency).
    # This makes PDF work even where no system Chrome exists.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
            page = browser.new_page()
            page.goto(f'file://{html_path}', wait_until='load', timeout=30000)
            page.pdf(path=str(pdf_path), format='A4', print_background=True)
            browser.close()
        if pdf_path.exists() and pdf_path.stat().st_size > 0:
            return pdf_path
    except Exception:
        pass

    return None


# ----------------------------------------------------------------------
# MAIN ENTRY POINT
# ----------------------------------------------------------------------

def run_audit(url: str, output_dir: str = './audits/') -> Dict:
    """Run a complete audit. Returns audit dict + paths to artifacts."""
    audit_id = str(uuid.uuid4())
    started = time.time()
    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f'[1/6] Running deterministic scripts on {url}...', flush=True)
    scripts_output = run_deterministic_scripts(url)
    if 'error' in scripts_output:
        return {'audit_id': audit_id, 'url': url, 'error': scripts_output['error']}

    print(f'[2/6] Loading brain snapshots...', flush=True)
    brain = BrainIndex.from_export_dir(str(RULESET_DIR))
    brain_stats = brain.stats()

    print(f'[3/6] Classifying page...', flush=True)
    classification = classify_page_from_scripts(scripts_output, url)

    print(f'[4/6] Selecting citations for failed checks...', flush=True)
    findings = enrich_findings_with_citations(
        scripts_output, brain,
        classification['page_type'], classification['industry']
    )

    print(f'[5/6] Calling Anthropic Sonnet 4.6 for narrative...', flush=True)
    narrative = call_sonnet_for_narrative(url, classification, scripts_output, findings)

    print(f'[6/6] Computing scores + rendering report...', flush=True)
    scoring = compute_section_scores(scripts_output)

    domain = re.sub(r'^https?://', '', url).rstrip('/').split('/')[0]
    audit = {
        'audit_id': audit_id,
        'url': url,
        'domain': domain,
        'date': datetime.now(timezone.utc).strftime('%Y-%m-%d'),
        'duration_seconds': round(time.time() - started, 1),
        'classification': classification,
        'scoring': scoring,
        'findings': findings,
        'narrative': narrative,
        'scripts_output': scripts_output,
        'brain_stats': brain_stats,
    }

    slug = domain.replace('.', '-')
    base_path = out_dir / f'{slug}-{audit_id[:8]}'

    # Render JSON
    json_path = base_path.with_suffix('.json')
    json_path.write_text(json.dumps(audit, indent=2, ensure_ascii=False))

    # Render Markdown
    md_path = base_path.with_suffix('.md')
    md_path.write_text(render_markdown_report(audit))

    # Render PDF (best effort)
    pdf_path = render_pdf_summary(audit, base_path)

    audit['json_path'] = str(json_path)
    audit['md_path'] = str(md_path)
    audit['pdf_path'] = str(pdf_path) if pdf_path else None

    print(f'\n✓ Audit complete in {audit["duration_seconds"]}s')
    print(f'  JSON: {json_path}')
    print(f'  Markdown: {md_path}')
    if pdf_path:
        print(f'  PDF:  {pdf_path}')
    return audit


if __name__ == '__main__':
    import argparse
    p = argparse.ArgumentParser(description='Standalone audit pipeline')
    p.add_argument('url', help='URL to audit')
    p.add_argument('--output', '-o', default='./audits/', help='Output directory')
    args = p.parse_args()
    result = run_audit(args.url, args.output)
    if 'error' in result:
        print(f'ERROR: {result["error"]}', file=sys.stderr)
        sys.exit(1)
