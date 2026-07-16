"""
scoring.py — Single source of truth for weights, grades, and score computation.

WHY THIS FILE EXISTS
--------------------
Before this module, the production agent path let the LLM compute the headline
score, grade, PCR, BAP, and every section score "in its head", and the service
persisted those numbers verbatim. That made the single most load-bearing number
in the product non-deterministic and unauditable: the same URL could score
differently on re-run, and a malformed model output could inject a non-numeric
"score" straight into the public share page.

The fix is a hard separation of concerns:
    - The LLM CLASSIFIES each check (pass / warn / fail / na).
    - Python GRADES deterministically from those classifications.

`recompute_scores()` reads `audit["findings"]` (the per-check statuses) and
overwrites `audit["scoring"]` with numbers computed here. `validate_audit()`
then clamps/enum-guards every field so nothing outside a known-good range or
enum can ever reach persistence or rendering.

There is exactly ONE weight table and ONE grade table, and they live here.
Any doc, prompt, or renderer references these — never its own copy.

Stdlib only.
"""

from __future__ import annotations

import math
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# Phase 4 — rule-weighted scoring (mode D). A section score is a weighted mean
# of its checks' status values; a check backed by a VERIFIED binding to a
# high-confidence authoritative rule (or high-risk anti-pattern) can count more.
# INERT BY DEFAULT: SIEVE_RULE_WEIGHT_LAMBDA=0 => every weight is 1.0 => the
# score is byte-identical to the unweighted formula. An operator raises LAMBDA
# only after calibrating against a labelled set. The weight is bounded to
# [1.0, 1.5] and rides only DB-pinned attributes (confidence / risk), so it can
# never flip a status or move a grade cutoff — it re-weights WITHIN a section.
RULE_WEIGHT_LAMBDA = float(os.getenv('SIEVE_RULE_WEIGHT_LAMBDA', '0.0'))
_RULE_WEIGHT_MAX = 1.5
_RISK_SEVERITY = {'high': 1.0, 'medium': 0.6, 'low': 0.3}
_STATUS_VAL = {'pass': 1.0, 'warn': 0.5, 'fail': 0.0}


def _binding_severity(bound_rule: Dict[str, Any]) -> float:
    """0..1 severity of a verified binding: rule confidence, or AP risk level."""
    if bound_rule.get('kind') == 'ap':
        return _RISK_SEVERITY.get(str(bound_rule.get('risk_level') or '').lower(), 0.6)
    try:
        return max(0.0, min(1.0, float(bound_rule.get('confidence_score') or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _check_weight(f: Dict[str, Any]) -> float:
    """Per-check scoring weight. 1.0 unless a VERIFIED binding raises it, and
    only when LAMBDA is set. Clamped to [1.0, _RULE_WEIGHT_MAX]."""
    if RULE_WEIGHT_LAMBDA <= 0:
        return 1.0
    br = f.get('bound_rule')
    if not isinstance(br, dict) or br.get('binding_verified') is not True:
        return 1.0
    w = 1.0 + RULE_WEIGHT_LAMBDA * _binding_severity(br)
    return max(1.0, min(_RULE_WEIGHT_MAX, w))

# ---------------------------------------------------------------------------
# CANONICAL TABLES — the only copies in the codebase
# ---------------------------------------------------------------------------

# Section letter -> canonical section_scores key used everywhere downstream.
SECTION_KEYS: Dict[str, str] = {
    'A': 'A_technical',
    'B': 'B_performance',
    'C': 'C_onpage',
    'D': 'D_schema',
    'E': 'E_aeo_discovery',
    'F': 'F_aeo_extraction',
    'G': 'G_aeo_trust',
    'H': 'H_aeo_selection',
    'I': 'I_geo',
    'J': 'J_entity',
}

# Page Citation Readiness (PCR) section weights. Canonical per scoring-rubric.md.
# Sums to exactly 1.00. Section I (GEO) is intentionally EXCLUDED from PCR — it
# feeds Brand AI Presence (BAP) instead. PCR is the deterministic, page-fixable
# headline number; BAP is directional and reported separately.
PCR_WEIGHTS: Dict[str, float] = {
    'A_technical': 0.16,
    'B_performance': 0.10,
    'C_onpage': 0.13,
    'D_schema': 0.16,
    'E_aeo_discovery': 0.13,
    'F_aeo_extraction': 0.13,
    'G_aeo_trust': 0.08,
    'H_aeo_selection': 0.08,
    'J_entity': 0.03,
}
assert abs(sum(PCR_WEIGHTS.values()) - 1.0) < 1e-9, 'PCR weights must sum to 1.0'

# Brand AI Presence (BAP) sub-weights, grouped by check id. Sums to 1.00.
# BAP is a directional signal derived from GEO (section I) checks.
BAP_GROUPS: Dict[str, Tuple[List[str], float]] = {
    'presence':     (['I1', 'I2', 'I8'], 0.40),
    'accuracy':     (['I3', 'I4', 'I7'], 0.35),
    'favorability': (['I5', 'I6'],       0.25),
}

# The ONE grade table. Monotonic, 9 grades + INCONCLUSIVE. Grade is derived from
# PCR (the deterministic number), never from the blended/directional figure.
# (min_inclusive_score, grade) — evaluated top-down.
GRADE_TABLE: List[Tuple[float, str]] = [
    (95.0, 'A+'),
    (85.0, 'A'),
    (80.0, 'B+'),
    (75.0, 'B'),
    (68.0, 'C+'),
    (60.0, 'C'),
    (53.0, 'D+'),
    (45.0, 'D'),
    (0.0,  'F'),
]

VALID_GRADES = frozenset([g for _, g in GRADE_TABLE] + ['INCONCLUSIVE'])
VALID_STATUSES = frozenset({'pass', 'warn', 'fail', 'na'})

# ---------------------------------------------------------------------------
# EVIDENCE TIERS (roadmap 0.1) — additive metadata on every finding.
#   'measured'   = the verdict was computed by deterministic Python over real
#                  page bytes (the script suite / runtime checks).
#   'llm-judged' = the verdict came from LLM classification.
# The tier is derived from the check id: these base codes have deterministic
# implementations in the script suite (deterministic_checks.py,
# check_robots_txt.py, check_sitemap.py). Everything else is LLM-classified.
# ---------------------------------------------------------------------------
EVIDENCE_MEASURED = 'measured'
EVIDENCE_LLM = 'llm-judged'
VALID_EVIDENCE_TIERS = frozenset({EVIDENCE_MEASURED, EVIDENCE_LLM})

MEASURED_CHECK_BASES = frozenset({
    # deterministic_checks.py
    'A1',    # https_enforcement (redirect + HSTS)
    'A2b',   # title_uniqueness_sample
    'A3',    # meta_description
    'A4b',   # canonical_redirect_chain
    'A5',    # robots_meta_indexing (+ robots.txt contradiction)
    'A7b',   # h1_nested_in_heading
    'B1',    # ttfb_median_5_samples
    'B9',    # no_mixed_content
    'C10',   # open_graph_tags
    'C12b',  # datemodified_staleness
    'D4',    # schema_id_coverage
    'D9',    # faqpage_schema_vs_visible
    'D12',   # person_schema_with_credentials
    'D14',   # hreflang_coverage
    'E4',    # no_nosnippet_noarchive
    'E12',   # no_noarchive
    'G1',    # author_byline (visible pattern / markup / schema author)
    'G2',    # author_schema_credentials (Article author → Person/Org linkage)
    'G7b',   # about_contact_discoverability (sub-check of G7)
    'G7c',   # editorial_policy_link (sub-check of G7)
    'J2',    # brand_name_consistency
    # tools.render_page_js (runtime deterministic — mobile emulation pass)
    'A9b',   # mobile_content_parity (desktop vs mobile render diff)
    # check_robots_txt.py (deterministic robots.txt parse per RFC 9309)
    'A10',   # robots_txt_crawling / target_path_not_disallowed
    'A11',   # sitemap_referenced / robots_declares_sitemap
    'E1',    # perplexitybot_allowed
    'E2',    # bingpreview_allowed
    'E3',    # googlebot_allowed
    'E10',   # claudebot_chatgpt_applebot
    'E13',   # ccbot_llm_training_access
    # check_sitemap.py
    'E8',    # page_in_sitemap / target_url_in_sitemap
})

_CHECK_BASE_RE = re.compile(r'^([A-J]\d{1,2}[a-z]?)(?:_|$)')


def evidence_tier_for(check_id: Any) -> str:
    """Deterministic evidence tier for a check id. Strips any 'source:' prefix
    (e.g. 'det_checks:B1_ttfb'), extracts the base code (letter+number+optional
    sub-letter) and looks it up in MEASURED_CHECK_BASES."""
    cid = str(check_id or '').split(':', 1)[-1].strip()
    m = _CHECK_BASE_RE.match(cid)
    if m and m.group(1) in MEASURED_CHECK_BASES:
        return EVIDENCE_MEASURED
    return EVIDENCE_LLM

# Bot's-Eye-View classifications meaning the probe never reached real content.
# A page in one of these states must NOT be scored — the redirect-incident
# failure mode where a healthy-but-misprobed page scored an F.
TRANSPORT_INCONCLUSIVE = frozenset({
    'unresolved_redirect', 'bot_blocked', 'http_error', 'fetch_failed',
})


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def grade_for(score: Optional[float]) -> str:
    """Deterministic letter grade for a 0-100 PCR score."""
    if score is None:
        return 'INCONCLUSIVE'
    s = _clamp(score)
    for threshold, grade in GRADE_TABLE:
        if s >= threshold:
            return grade
    return 'F'


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _num_or_none(x: Any) -> Optional[float]:
    """Coerce a value to a finite float in [0,100], or None. Never raises.
    This is the choke point that stops a non-numeric model 'score' (the stored
    XSS / score-forgery vector) from ever surviving into persistence."""
    if x is None:
        return None
    if isinstance(x, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(x, (int, float)):
        return _clamp(float(x)) if math.isfinite(float(x)) else None
    if isinstance(x, str):
        m = re.search(r'-?\d+(?:\.\d+)?', x)
        if not m:
            return None
        try:
            return _clamp(float(m.group(0)))
        except ValueError:
            return None
    return None


def _section_of(finding: Dict[str, Any]) -> Optional[str]:
    """Determine the A-J section letter for a finding, from its explicit
    `section` field or by parsing the leading letter of its check_id."""
    sec = str(finding.get('section') or '').strip().upper()
    if sec and sec[0] in SECTION_KEYS:
        return sec[0]
    cid = str(finding.get('check_id') or '').split(':', 1)[-1].strip()
    m = re.match(r'^([A-Z])', cid)
    if m and m.group(1) in SECTION_KEYS:
        return m.group(1)
    return None


def _transport_inconclusive(audit: Dict[str, Any]) -> Tuple[bool, str]:
    """True when the probe never reached page content, so no numeric grade is
    defensible. Reads the Bot's-Eye-View classification wherever it lives."""
    bev = audit.get('bots_eye_view') or {}
    classification = (
        bev.get('classification')
        or (bev.get('summary') or {}).get('classification')
        or ''
    )
    classification = str(classification).strip().lower()
    if classification in TRANSPORT_INCONCLUSIVE:
        return True, f"probe classification '{classification}'"
    # Also honor an explicit gate the model may have set.
    gates = audit.get('gates') or {}
    if str(gates.get('page_existence', '')).lower() == 'fail':
        return True, 'gate: page does not exist (4xx/5xx/parking)'
    return False, ''


# ---------------------------------------------------------------------------
# CORE: recompute scores from the model's per-check statuses
# ---------------------------------------------------------------------------

def compute_from_findings(findings: List[Dict[str, Any]],
                          audit: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Compute the full scoring block deterministically from per-check statuses.

    Within a section every applicable check is weighted equally: pass=1.0,
    warn=0.5, fail=0.0, na excluded. Sections are combined by PCR_WEIGHTS.
    Returns a scoring dict; never raises.
    """
    counts = {L: {'pass': 0, 'warn': 0, 'fail': 0, 'na': 0} for L in SECTION_KEYS}
    # Weighted accumulators (mode D). With every weight 1.0 these reduce exactly
    # to (pass + 0.5*warn)/applicable, so the default is byte-identical.
    wsum = {L: 0.0 for L in SECTION_KEYS}
    wval = {L: 0.0 for L in SECTION_KEYS}
    tier_counts = {EVIDENCE_MEASURED: 0, EVIDENCE_LLM: 0}
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        # Evidence-tier rollup (additive metadata): honor an explicit tier the
        # producer stamped, else derive deterministically from the check id.
        et = f.get('evidence_tier')
        if et not in VALID_EVIDENCE_TIERS:
            et = evidence_tier_for(f.get('check_id'))
        tier_counts[et] += 1
        sec = _section_of(f)
        if sec is None:
            continue
        st = str(f.get('status', 'na')).strip().lower()
        if st not in VALID_STATUSES:
            st = 'na'
        counts[sec][st] += 1
        if st in _STATUS_VAL:                       # applicable (na excluded)
            w = _check_weight(f)
            wsum[sec] += w
            wval[sec] += w * _STATUS_VAL[st]

    section_scores: Dict[str, Optional[float]] = {}
    for L, key in SECTION_KEYS.items():
        section_scores[key] = (
            None if wsum[L] == 0
            else round(wval[L] / wsum[L] * 100, 1)
        )

    # Transport gate — no numeric score if the probe never reached content.
    inconclusive = False
    reason = ''
    if audit is not None:
        inconclusive, reason = _transport_inconclusive(audit)

    # PCR — weighted over applicable, non-GEO sections; renormalized so a section
    # with no applicable checks doesn't drag the denominator.
    weighted = 0.0
    weight_sum = 0.0
    for key, w in PCR_WEIGHTS.items():
        v = section_scores.get(key)
        if v is not None:
            weighted += v * w
            weight_sum += w
    pcr = round(weighted / weight_sum, 1) if weight_sum > 0 else None

    # BAP — directional GEO signal, with a coverage-based confidence.
    bap, bap_conf = _compute_bap(findings or [])

    if inconclusive or pcr is None:
        return {
            'section_scores': section_scores,
            'section_counts': counts,
            'evidence_tiers': tier_counts,
            'page_citation_readiness': None,
            'brand_ai_presence': bap,
            'brand_ai_presence_confidence': bap_conf,
            'overall_score': None,
            'overall_grade': 'INCONCLUSIVE',
            'grade_basis': 'page_citation_readiness',
            'inconclusive': True,
            'inconclusive_reason': reason or 'no applicable checks (content not reached)',
            'computed_by': 'runtime-deterministic',
        }

    grade = grade_for(pcr)
    # Informational only. The letter grade is driven by PCR (deterministic);
    # BAP is folded here purely as a directional combined view, never into the grade.
    combined = round(pcr * 0.80 + (bap if bap is not None else pcr) * 0.20, 1)

    return {
        'section_scores': section_scores,
        'section_counts': counts,
        'evidence_tiers': tier_counts,
        'page_citation_readiness': pcr,
        'brand_ai_presence': bap,
        'brand_ai_presence_confidence': bap_conf,
        'combined_directional_score': combined,
        'overall_score': pcr,
        'overall_grade': grade,
        'grade_basis': 'page_citation_readiness',
        'inconclusive': False,
        'computed_by': 'runtime-deterministic',
    }


def _compute_bap(findings: List[Dict[str, Any]]) -> Tuple[Optional[float], str]:
    """Brand AI Presence from GEO (section I) checks. Directional — returns a
    confidence tag reflecting how many I-checks were actually applicable."""
    by_id: Dict[str, str] = {}
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        cid = str(f.get('check_id') or '').split(':', 1)[-1].strip()
        m = re.match(r'^(I\d+)', cid)
        if m:
            by_id[m.group(1)] = str(f.get('status', 'na')).strip().lower()

    def _group_score(ids: List[str]) -> Optional[float]:
        vals = []
        for i in ids:
            st = by_id.get(i)
            if st == 'pass':
                vals.append(1.0)
            elif st == 'warn':
                vals.append(0.5)
            elif st == 'fail':
                vals.append(0.0)
        return (sum(vals) / len(vals) * 100) if vals else None

    weighted = 0.0
    weight_sum = 0.0
    applicable_checks = 0
    for _group, (ids, w) in BAP_GROUPS.items():
        gs = _group_score(ids)
        if gs is not None:
            weighted += gs * w
            weight_sum += w
            applicable_checks += sum(1 for i in ids if i in by_id)

    if weight_sum == 0:
        return None, 'none'
    bap = round(weighted / weight_sum, 1)
    # Confidence by coverage: BAP rides on live SERP/model judgment and is noisy.
    conf = 'low' if applicable_checks < 3 else ('medium' if applicable_checks < 6 else 'high')
    return bap, conf


# ---------------------------------------------------------------------------
# PUBLIC: recompute + validate (call both before persist/render)
# ---------------------------------------------------------------------------

def recompute_scores(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Overwrite audit['scoring'] with deterministically-computed numbers.

    The model's own scoring (if any) is preserved under
    scoring['model_reported'] for debugging/telemetry, then replaced.
    """
    if not isinstance(audit, dict):
        return audit
    findings = audit.get('findings') if isinstance(audit.get('findings'), list) else []
    model_reported = audit.get('scoring') if isinstance(audit.get('scoring'), dict) else None

    computed = compute_from_findings(findings, audit)
    if model_reported is not None:
        computed['model_reported'] = {
            k: model_reported.get(k)
            for k in ('overall_score', 'overall_grade', 'page_citation_readiness',
                      'brand_ai_presence')
            if k in model_reported
        }
    audit['scoring'] = computed
    return audit


def validate_audit(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Clamp/enum-guard every score-bearing field so nothing malformed can reach
    persistence or the public renderer. Idempotent; never raises.

    This is the server-side backstop for SEC-4: even if recompute_scores were
    bypassed, a non-numeric section score or an out-of-enum grade is neutralized
    here rather than being interpolated into public HTML.
    """
    if not isinstance(audit, dict):
        return audit
    scoring = audit.get('scoring')
    if not isinstance(scoring, dict):
        audit['scoring'] = compute_from_findings(
            audit.get('findings') if isinstance(audit.get('findings'), list) else [],
            audit,
        )
        return audit

    ss = scoring.get('section_scores')
    if isinstance(ss, dict):
        scoring['section_scores'] = {
            k: _num_or_none(v) for k, v in ss.items()
        }

    for key in ('page_citation_readiness', 'brand_ai_presence',
                'combined_directional_score', 'overall_score',
                'seo_score', 'aeo_score', 'citation_readiness'):
        if key in scoring:
            scoring[key] = _num_or_none(scoring[key])

    grade = str(scoring.get('overall_grade') or '').strip()
    if grade not in VALID_GRADES:
        # Re-derive from PCR rather than trusting an out-of-enum string.
        scoring['overall_grade'] = grade_for(scoring.get('overall_score'))

    # Guard finding statuses too — the renderer keys icons off these.
    # Also stamp the evidence tier (roadmap 0.1): additive — an explicit valid
    # tier set by a deterministic producer is preserved; anything missing or
    # out-of-enum is (re)derived from the check id.
    findings = audit.get('findings')
    if isinstance(findings, list):
        for f in findings:
            if isinstance(f, dict):
                st = str(f.get('status', 'na')).strip().lower()
                f['status'] = st if st in VALID_STATUSES else 'na'
                if f.get('evidence_tier') not in VALID_EVIDENCE_TIERS:
                    f['evidence_tier'] = evidence_tier_for(f.get('check_id'))
    return audit


def finalize_scoring(audit: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience: recompute then validate. This is what callers should use
    on the agent's raw audit before rendering/persisting."""
    return validate_audit(recompute_scores(audit))
