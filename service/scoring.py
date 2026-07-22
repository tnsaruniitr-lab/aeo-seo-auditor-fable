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

# Section letters PCR actually scores (I/GEO feeds BAP instead). The shadow's
# coverage counters are restricted to these so 'findings_total' never claims
# findings the PCR math (classic or shadow) can't count.
_PCR_SECTIONS = frozenset(L for L, key in SECTION_KEYS.items()
                          if key in PCR_WEIGHTS)

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
    'E14',   # llms_txt (deterministic_checks.py — domain-level /llms.txt probe)
    # check_sitemap.py
    'E8',    # page_in_sitemap / target_url_in_sitemap
    # scripts/light_checks.py (LIGHT profile deterministic factor checks).
    # NEW base codes on purpose: the LLM-classified cousins (E5/F3/F6/F8,
    # C-family city semantics) keep their llm-judged tier on the full path —
    # adding e.g. 'F6' here would have re-stamped model verdicts as measured.
    'C13',   # city_in_title_h1
    'D15',   # localbusiness_geo_schema
    'E5b',   # raw_html_depth (deterministic variant of E5)
    # (E14 llms_txt already listed above — the light profile emits the same
    #  canonical id E14_llms_txt with deterministic_checks.py's verdict bands)
    'F3b',   # faq_content_present (deterministic variant of F3)
    'F6b',   # question_headings (deterministic variant of F6)
    'F8b',   # prices_visible (deterministic variant of F8)
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


# ---------------------------------------------------------------------------
# OBSERVED-PROOF METHOD (contract §5) — what kind of observation an attached
# observed{} block represents. H*/I* checks observe competitors and AI-engine
# results, not the customer's page — they must never be labeled on-page.
# ---------------------------------------------------------------------------
OBSERVED_ON_PAGE = 'measured-on-page'
OBSERVED_OFF_PAGE = 'observed-off-page'
OBSERVED_COMPETITOR = 'observed-competitor'
OBSERVED_MODEL = 'model-judgment'
_OFF_PAGE_SECTIONS = frozenset({'H', 'I'})
# H-family (AEO selection) checks are comparative: their deterministic detail
# derives from the competitor crawl. Only H earns 'observed-competitor' — the
# I family observes AI-engine presence, not competitor pages.
_COMPETITOR_SECTION = 'H'

# Methods that represent a REAL observation (something the runtime measured or
# looked at), as opposed to a model judgment. The shadow score counts a finding
# whose observed block carries one of these.
REAL_OBSERVED_METHODS = frozenset({OBSERVED_ON_PAGE, OBSERVED_OFF_PAGE,
                                   OBSERVED_COMPETITOR})


def _detail_has_competitor_data(detail: Any) -> bool:
    """True when a deterministic detail block ACTUALLY carries competitor-crawl
    data: any competitor-named key with a non-empty value. competitors_crawled=0
    or competitors=[] is not competitor data — the comparison had no basis."""
    if not isinstance(detail, dict):
        return False
    return any('competitor' in str(k).lower() and bool(v)
               for k, v in detail.items())


def observed_method_for(check_id: Any, detail: Any = None) -> str:
    """Deterministic observed.method for a check id: off-page families first
    (H competitive, I GEO presence), then measured-on-page for the
    deterministic script suite, model-judgment for everything else. An
    H-family check whose deterministic `detail` actually contains competitor
    data refines to 'observed-competitor'; plain off-page stays
    'observed-off-page'."""
    cid = str(check_id or '').split(':', 1)[-1].strip()
    fam = cid[:1].upper()
    if fam in _OFF_PAGE_SECTIONS:
        if fam == _COMPETITOR_SECTION and _detail_has_competitor_data(detail):
            return OBSERVED_COMPETITOR
        return OBSERVED_OFF_PAGE
    if evidence_tier_for(cid) == EVIDENCE_MEASURED:
        return OBSERVED_ON_PAGE
    return OBSERVED_MODEL


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


def _evidence_backed(f: Dict[str, Any]) -> bool:
    """SHADOW gate: does this finding's verdict ride on evidence the runtime
    actually holds? True ONLY for an attached observed block whose method is a
    real observation (never model-judgment).

    Deliberately ignores `evidence_tier`: on the agent path the model authors
    the findings, and both an explicit tier stamp and a measured-family
    check_id are model-forgeable — the observed block is not. On the agent
    path agent._join_observed strips every model-emitted observed block and
    attaches one iff a deterministic producer really measured the check (with
    a fail-closed strip when the join dies); on the legacy pipeline path
    compute_section_scores stamps its own blocks over Python verdicts. Either
    way `observed` is producer-owned, so it is the one signal the shadow may
    trust."""
    obs = f.get('observed')
    return isinstance(obs, dict) and obs.get('method') in REAL_OBSERVED_METHODS


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
    # SHADOW (evidence-weighted) accumulators — same section/weight math as the
    # classic score, counting ONLY evidence-backed findings (_evidence_backed).
    # Purely additive: the classic accumulators above are untouched.
    ev_wsum = {L: 0.0 for L in SECTION_KEYS}
    ev_wval = {L: 0.0 for L in SECTION_KEYS}
    ev_counted = 0
    applicable_total = 0
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
            if sec in _PCR_SECTIONS:
                # Shadow coverage counts only PCR-scored sections — I (GEO)
                # findings feed BAP, not PCR, so neither counter claims them.
                applicable_total += 1
                if _evidence_backed(f):
                    ev_counted += 1
                    ev_wsum[sec] += w
                    ev_wval[sec] += w * _STATUS_VAL[st]

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
            'shadow': None,
            'shadow_reason': 'classic score inconclusive — shadow suppressed',
            'computed_by': 'runtime-deterministic',
        }

    # SHADOW (evidence-weighted) PCR — identical section/weight math over the
    # evidence-backed accumulators. A section with zero evidence-backed
    # findings is excluded + renormalized exactly like the classic
    # empty-section rule; with no such section at all the shadow is null with
    # a reason (never a fabricated number). Shadow means shadow: nothing here
    # feeds the classic score or grade.
    ev_section_scores = {
        key: (None if ev_wsum[L] == 0
              else round(ev_wval[L] / ev_wsum[L] * 100, 1))
        for L, key in SECTION_KEYS.items()
    }
    ev_weighted = 0.0
    ev_weight_sum = 0.0
    sections_with_data = 0
    for key, w in PCR_WEIGHTS.items():
        v = ev_section_scores.get(key)
        if v is not None:
            ev_weighted += v * w
            ev_weight_sum += w
            sections_with_data += 1
    if ev_weight_sum > 0:
        pcr_evidence = round(ev_weighted / ev_weight_sum, 1)
        shadow: Optional[Dict[str, Any]] = {
            'pcr_evidence': pcr_evidence,
            'grade_evidence': grade_for(pcr_evidence),
            'delta_vs_classic': round(pcr_evidence - pcr, 1),
            'coverage': {
                # findings_total = applicable, sectioned findings in the
                # PCR-scored sections (I/GEO feeds BAP, not PCR, so it is
                # excluded); findings_counted = the evidence-backed subset
                # the shadow retains.
                'findings_counted': ev_counted,
                'findings_total': applicable_total,
                'sections_with_data': sections_with_data,
            },
        }
        shadow_reason = None
    else:
        shadow = None
        shadow_reason = 'no evidence-backed findings in any scored section'

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
        'shadow': shadow,
        **({'shadow_reason': shadow_reason} if shadow_reason else {}),
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
# CITE-READINESS (Phase 2) — first consumer of the study-calibrated scoring
# model (service/ruleset/aeo-scoring-model.json, reverse-engineered from the
# 1000-probe citation experiment). Computes the on-site cite-readiness
# sub-score: tier-0 gates zero it, tier-1 factors weight it, tier-2
# non-factors are excluded entirely. Ships ALONGSIDE the classic PCR — the
# overall score/grade continue to ride PCR unchanged (no cutover).
# ---------------------------------------------------------------------------

_SCORING_MODEL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'ruleset', 'aeo-scoring-model.json')
# Module-level cache: False = not loaded yet; None = load failed (absence
# tolerated — citeReadiness goes null); dict = the parsed model.
_SCORING_MODEL_CACHE: Any = False


def load_scoring_model() -> Optional[Dict[str, Any]]:
    """Load + cache aeo-scoring-model.json. Absence/corruption tolerated:
    returns None (citeReadiness renders null), never raises."""
    global _SCORING_MODEL_CACHE
    if _SCORING_MODEL_CACHE is False:
        try:
            import json as _json
            with open(_SCORING_MODEL_PATH) as fh:
                model = _json.load(fh)
            _SCORING_MODEL_CACHE = model if isinstance(model, dict) else None
        except Exception:
            _SCORING_MODEL_CACHE = None
    return _SCORING_MODEL_CACHE


# Factor id -> auditor check BASES whose findings evidence that factor.
# Sourced from the measured factor→check-ID map in
# docs/AEO-PLAYBOOK-measured-2026-07-20.md §1 (the config carries weights and
# lifts; this table carries the check vocabulary those weights bind to).
CITE_FACTOR_CHECK_BASES: Dict[str, Tuple[str, ...]] = {
    'faq_schema':         ('D9', 'F4', 'F3'),
    'depth_structure':    ('C6', 'C1', 'F12'),
    'question_headings':  ('F6',),
    'article_schema':     ('D3', 'D11'),
    'llms_txt':           ('E14',),
    'answer_shaped_intro': ('F1', 'F2'),
    'byline_eeat':        ('G1', 'G2'),
    'delivery_hardening': ('B7', 'B8'),
}

VALID_GATE_STATUSES = frozenset({'pass', 'fail', 'unknown'})
VALID_BUSINESS_TYPES = frozenset({'product', 'local_service'})

# STATUS-DERIVATION MAPPING (deterministic, documented per Phase-2 contract):
#   Per factor, take every finding whose check-id BASE is in the factor's
#   CITE_FACTOR_CHECK_BASES entry and whose status is applicable
#   (pass/warn/fail — 'na' and missing checks are excluded).
#     presence_fraction = mean of {pass: 1.0, warn: 0.5, fail: 0.0}
#                         over those applicable constituent checks
#     factor status     = 'pass' when presence_fraction == 1.0 (all pass)
#                         'fail' when presence_fraction == 0.0 (all fail)
#                         'warn' otherwise (mixed / any warn)
#                         'na'   when NO applicable constituent check exists
#     factor points     = weight × presence_fraction
#   Renormalization: score = Σ(weight × presence_fraction) over applicable
#   factors ÷ Σ(weight) over applicable factors × 100. A factor with no
#   applicable constituent check ('na') is excluded from BOTH sums — the
#   finding-driven form of "page-type applicability" (checks the page type
#   never triggers are absent/na, so their factors drop out of the
#   denominator instead of dragging the score).


def _cite_gates(audit: Dict[str, Any],
                model: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Evaluate the tier-0 gates. Each gate is pass / fail / unknown:
    only an explicit FAIL zeroes the score — 'unknown' (missing probe or
    robots data) fails OPEN so a degraded scripts run can never fake a
    zero. This zero-path is DISTINCT from TRANSPORT_INCONCLUSIVE Gate 0:
    an unreached page is handled before this function is called and stays
    INCONCLUSIVE (citeReadiness null), never 0."""
    bev = audit.get('bots_eye_view') if isinstance(
        audit.get('bots_eye_view'), dict) else {}
    summary = bev.get('summary') if isinstance(bev.get('summary'), dict) else {}
    probes = bev.get('probes') if isinstance(bev.get('probes'), dict) else {}
    gates: List[Dict[str, Any]] = []

    # --- ai_bot_allowed — via the robots outputs (runtime-attached
    # audit['robots_ai_access'], produced by check_robots_txt.py's
    # evaluate_tier0_bot_access over GPTBot/ClaudeBot/Google-Extended/
    # PerplexityBot).
    access = audit.get('robots_ai_access')
    bots = access.get('bots') if isinstance(access, dict) else None
    if isinstance(bots, dict) and bots:
        blocked = sorted(b for b, v in bots.items()
                         if isinstance(v, dict) and v.get('allowed') is False)
        known = [v for v in bots.values()
                 if isinstance(v, dict) and v.get('allowed') is not None]
        if blocked:
            gates.append({'id': 'ai_bot_allowed', 'status': 'fail',
                          'evidence': f'robots.txt disallows the audited path '
                                      f'for: {", ".join(blocked)} '
                                      f'(measured: 0% of cited winners '
                                      f'blocked all AI bots)'})
        elif known:
            gates.append({'id': 'ai_bot_allowed', 'status': 'pass',
                          'evidence': f'all {len(known)} evaluated AI bots '
                                      f'allowed for the audited path '
                                      f'(basis: {access.get("basis")})'})
        else:
            gates.append({'id': 'ai_bot_allowed', 'status': 'unknown',
                          'evidence': f'robots.txt access unknown '
                                      f'(basis: {access.get("basis")})'})
    else:
        gates.append({'id': 'ai_bot_allowed', 'status': 'unknown',
                      'evidence': 'no robots ai_bot_access data attached '
                                  'to this audit'})

    # --- server_rendered — raw-HTML visible words >= 300, via
    # bots_eye_view (raw fetch, no JS execution).
    words = summary.get('visible_words_default')
    if isinstance(words, bool) or not isinstance(words, (int, float)):
        gates.append({'id': 'server_rendered', 'status': 'unknown',
                      'evidence': 'bots_eye_view visible_words_default '
                                  'not available'})
    elif words >= 300:
        gates.append({'id': 'server_rendered', 'status': 'pass',
                      'evidence': f'{int(words)} words visible in raw HTML '
                                  f'(threshold 300)'})
    else:
        gates.append({'id': 'server_rendered', 'status': 'fail',
                      'evidence': f'only {int(words)} words visible in raw '
                                  f'HTML (< 300) — content likely '
                                  f'JS-rendered; 0% of cited winners were '
                                  f'JS-blocked'})

    # --- reachable_200 — HTTP 2xx to the GPTBot-class UA probe (redirects
    # followed). Falls back to the default-UA code when the AI probes are
    # absent. Note the asymmetry with Gate 0: a page whose DEFAULT probe
    # never reached content is transport-inconclusive (null, handled
    # upstream); a page fine for browsers but 4xx to the AI UA is a real,
    # measured citation-killer and zeroes here.
    ai_codes = []
    for name in ('gpt', 'claude', 'perp'):
        p = probes.get(name)
        code = p.get('http_code') if isinstance(p, dict) else None
        if isinstance(code, (int, float)) and not isinstance(code, bool) \
                and code > 0:
            ai_codes.append((name, int(code)))
    if ai_codes:
        bad = [(n, c) for n, c in ai_codes if not (200 <= c < 300)]
        if bad:
            gates.append({'id': 'reachable_200', 'status': 'fail',
                          'evidence': 'AI-UA probe(s) not 2xx: ' + ', '.join(
                              f'{n}={c}' for n, c in bad)})
        else:
            gates.append({'id': 'reachable_200', 'status': 'pass',
                          'evidence': 'AI-UA probes 2xx: ' + ', '.join(
                              f'{n}={c}' for n, c in ai_codes)})
    else:
        code = summary.get('http_code_default')
        if isinstance(code, (int, float)) and not isinstance(code, bool) \
                and code > 0:
            ok = 200 <= int(code) < 300
            gates.append({'id': 'reachable_200',
                          'status': 'pass' if ok else 'fail',
                          'evidence': f'no AI-UA probes recorded; default UA '
                                      f'HTTP {int(code)}'})
        else:
            gates.append({'id': 'reachable_200', 'status': 'unknown',
                          'evidence': 'no probe HTTP codes available'})

    # --- https — scheme of the final served URL. Read from the A1 finding's
    # runtime-observed detail first (the script's measured final_url), then
    # the BEV summary, then the audit URL. A1 'fail' for a missing http→https
    # redirect (insecure duplicate) does NOT fail this gate — the config rule
    # is scheme == https on the served page.
    final_url = None
    for f in (audit.get('findings') or []):
        if not isinstance(f, dict):
            continue
        cid = str(f.get('check_id') or '').split(':', 1)[-1]
        m = _CHECK_BASE_RE.match(cid)
        if m and m.group(1) == 'A1':
            obs = f.get('observed')
            detail = obs.get('detail') if isinstance(obs, dict) else None
            if isinstance(detail, dict) and detail.get('final_url'):
                final_url = str(detail['final_url'])
            break
    final_url = final_url or summary.get('final_url') or audit.get('url')
    fu = str(final_url or '').strip().lower()
    if fu.startswith('https://'):
        gates.append({'id': 'https', 'status': 'pass',
                      'evidence': f'served over https ({final_url})'})
    elif fu.startswith('http://'):
        gates.append({'id': 'https', 'status': 'fail',
                      'evidence': f'served over plain http ({final_url}) — '
                                  f'100% of cited winners were https'})
    else:
        gates.append({'id': 'https', 'status': 'unknown',
                      'evidence': 'final URL scheme not determinable'})
    return gates


def _cite_business_type(audit: Dict[str, Any],
                        model: Dict[str, Any]) -> Tuple[str, Optional[str], str]:
    """Category dispatch per the config's category_dispatch, read from the
    persisted LLM Phase-4 classification. Returns
    (business_type, classification_confidence, dispatch_basis).

    Mapping (documented decision): page_type 'local_business' or 'service'
    -> 'local_service'; every other page_type -> 'product'. Industry is
    recorded on the audit but does not yet move the dispatch — the config
    only distinguishes these two categories (vertical overlays are the
    Phase-3 pinned-menu work, REPORT-SPEC-v3 data decision 2). Defaults to
    'product' when the classification is absent or confidence is 'low'."""
    cls = audit.get('classification')
    cls = cls if isinstance(cls, dict) else {}
    conf = str(cls.get('confidence') or '').strip().lower() or None
    if conf not in ('high', 'medium', 'low'):
        conf = None
    page_type = str(cls.get('page_type') or '').strip().lower()
    dispatch = model.get('category_dispatch')
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    if not page_type or conf in (None, 'low'):
        return 'product', conf, 'default (classification absent or low-confidence)'
    bt = 'local_service' if page_type in ('local_business', 'service') \
        else 'product'
    if bt not in dispatch:      # config renamed a category — fall back safe
        return 'product', conf, f'default (category "{bt}" not in config)'
    return bt, conf, f'classified page_type={page_type} ({conf} confidence)'


def compute_cite_readiness(audit: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """On-site cite-readiness per aeo-scoring-model.json (its first consumer).

    Returns the citeReadiness object, or None when the calibration config is
    unavailable or the audit is transport-inconclusive:

        {score, gates[], factors[], business_type, own_site_weight,
         classification_confidence, calibration_version}

    Semantics:
      - TRANSPORT_INCONCLUSIVE (Gate 0) -> None. An unreachable page stays
        INCONCLUSIVE/null — this is DISTINCT from the tier-0 zero-path.
      - Any tier-0 gate FAIL -> score 0 with the failing gates[] detail
        (factors are still reported: they carry the forfeited points).
      - Otherwise score = renormalized tier-1 weighted presence, 0..100
        (see the STATUS-DERIVATION MAPPING comment above). Tier-2
        non-factors in the config are excluded from scoring entirely.
      - score is None (with gates/factors intact) when no tier-1 factor has
        any applicable check — nothing measurable to score.
    Never raises."""
    model = load_scoring_model()
    if model is None or not isinstance(audit, dict):
        return None
    inconclusive, _reason = _transport_inconclusive(audit)
    if inconclusive:
        return None

    # Index findings by check-id base (source prefixes stripped).
    by_base: Dict[str, List[Dict[str, Any]]] = {}
    for f in (audit.get('findings') or []):
        if not isinstance(f, dict):
            continue
        cid = str(f.get('check_id') or '').split(':', 1)[-1].strip()
        m = _CHECK_BASE_RE.match(cid)
        if m:
            by_base.setdefault(m.group(1), []).append(f)

    gates = _cite_gates(audit, model)
    gate_failed = any(g.get('status') == 'fail' for g in gates)

    factors: List[Dict[str, Any]] = []
    weighted = 0.0
    weight_sum = 0.0
    for spec in (model.get('tier1_factors') or []):
        if not isinstance(spec, dict):
            continue
        fid = str(spec.get('id') or '')
        try:
            weight = float(spec.get('weight') or 0.0)
        except (TypeError, ValueError):
            weight = 0.0
        lift = spec.get('measured_lift')
        bases = CITE_FACTOR_CHECK_BASES.get(fid, ())
        vals: List[float] = []
        check_ids: List[str] = []
        evid_parts: List[str] = []
        for base in bases:
            for f in by_base.get(base, []):
                st = str(f.get('status') or '').strip().lower()
                if st not in _STATUS_VAL:
                    continue        # na / invalid -> not applicable
                vals.append(_STATUS_VAL[st])
                cid = str(f.get('check_id') or '').split(':', 1)[-1].strip()
                if cid and cid not in check_ids:
                    check_ids.append(cid)
                ev = f.get('evidence')
                if isinstance(ev, str) and ev.strip() and len(evid_parts) < 4:
                    evid_parts.append(f'[{cid}] ' + ev.strip()[:160])
        if not vals:
            factors.append({'id': fid, 'weight': weight, 'status': 'na',
                            'points': 0.0, 'check_ids': [],
                            'evidence': 'no applicable check on this page '
                                        '(excluded from the denominator)',
                            'lift': lift})
            continue
        fraction = sum(vals) / len(vals)
        status = 'pass' if fraction >= 1.0 else (
            'fail' if fraction <= 0.0 else 'warn')
        factors.append({
            'id': fid,
            'weight': weight,
            'status': status,
            'points': round(weight * fraction, 1),
            'check_ids': check_ids[:8],
            'evidence': ' | '.join(evid_parts)[:400],
            'lift': lift,
        })
        weighted += weight * fraction
        weight_sum += weight

    business_type, conf, dispatch_basis = _cite_business_type(audit, model)
    dispatch = model.get('category_dispatch')
    dispatch = dispatch if isinstance(dispatch, dict) else {}
    own_site_weight = None
    bt_cfg = dispatch.get(business_type)
    if isinstance(bt_cfg, dict):
        try:
            own_site_weight = float(bt_cfg.get('own_site_weight'))
        except (TypeError, ValueError):
            own_site_weight = None

    if gate_failed:
        score: Optional[float] = 0.0
    elif weight_sum > 0:
        score = round(_clamp(weighted / weight_sum * 100.0), 1)
    else:
        score = None    # nothing measurable — nullable, not zero

    out: Dict[str, Any] = {
        'score': score,
        'gates': gates,
        'factors': factors,
        'business_type': business_type,
        'own_site_weight': own_site_weight,
        'classification_confidence': conf,
        'dispatch_basis': dispatch_basis,
        'calibration_version': str(model.get('version') or '')[:40] or None,
    }
    if gate_failed:
        out['zeroed_by_gates'] = sorted(
            g['id'] for g in gates if g.get('status') == 'fail')
    elif score is None:
        out['reason'] = 'no tier-1 factor has an applicable check'
    return out


def clamp_cite_readiness(cr: Any) -> Optional[Dict[str, Any]]:
    """Clamp/enum-guard ONE citeReadiness block (mirror of clamp_shadow).
    Both copies — scoring['cite_readiness'] and the metadata.cite_readiness
    mirror reloaded audits fall back to — must pass this before any renderer
    or API forwards them. Mutates in place; returns the dict or None."""
    if not isinstance(cr, dict):
        return None
    cr['score'] = _num_or_none(cr.get('score'))
    gates = []
    for g in (cr.get('gates') or [])[:8] if isinstance(cr.get('gates'), list) else []:
        if not isinstance(g, dict):
            continue
        st = str(g.get('status') or '').strip().lower()
        gates.append({
            'id': str(g.get('id') or '')[:64],
            'status': st if st in VALID_GATE_STATUSES else 'unknown',
            'evidence': str(g.get('evidence') or '')[:300],
        })
    cr['gates'] = gates
    factors = []
    for f in (cr.get('factors') or [])[:16] if isinstance(cr.get('factors'), list) else []:
        if not isinstance(f, dict):
            continue
        st = str(f.get('status') or '').strip().lower()
        w = f.get('weight')
        p = f.get('points')
        lift = f.get('lift')
        ok_num = lambda x: (isinstance(x, (int, float))       # noqa: E731
                            and not isinstance(x, bool)
                            and math.isfinite(float(x)))
        factors.append({
            'id': str(f.get('id') or '')[:64],
            'weight': round(_clamp(float(w)), 1) if ok_num(w) else 0.0,
            'status': st if st in VALID_STATUSES else 'na',
            'points': round(_clamp(float(p)), 1) if ok_num(p) else 0.0,
            'check_ids': [str(c)[:64] for c in (f.get('check_ids') or [])[:8]
                          if isinstance(c, str)],
            'evidence': str(f.get('evidence') or '')[:400],
            'lift': round(float(lift), 2) if ok_num(lift) else None,
        })
    cr['factors'] = factors
    bt = str(cr.get('business_type') or '').strip()
    cr['business_type'] = bt if bt in VALID_BUSINESS_TYPES else 'product'
    conf = str(cr.get('classification_confidence') or '').strip().lower()
    cr['classification_confidence'] = conf if conf in ('high', 'medium', 'low') else None
    osw = cr.get('own_site_weight')
    if isinstance(osw, bool) or not isinstance(osw, (int, float)) \
            or not math.isfinite(float(osw)):
        cr['own_site_weight'] = None
    else:
        cr['own_site_weight'] = round(max(0.0, min(1.0, float(osw))), 2)
    cv = cr.get('calibration_version')
    cr['calibration_version'] = str(cv)[:40] if cv else None
    if 'zeroed_by_gates' in cr:
        cr['zeroed_by_gates'] = [str(g)[:64] for g in (cr.get('zeroed_by_gates') or [])[:8]
                                 if isinstance(g, str)]
    return cr


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

    # CITE-READINESS (Phase 2) — additive, nullable companion object beside
    # the classic PCR (which keeps driving overall score/grade unchanged).
    # Transport-inconclusive audits get null with a reason: the Gate-0
    # INCONCLUSIVE path must stay DISTINCT from the tier-0 gate zero-path
    # (an unreached page is not a zero-scored page).
    if computed.get('inconclusive'):
        computed['cite_readiness'] = None
        computed['cite_readiness_reason'] = (
            'transport inconclusive — cite-readiness suppressed '
            '(stays null, never 0)')
    else:
        try:
            cr = compute_cite_readiness(audit)
        except Exception:
            cr = None
        computed['cite_readiness'] = cr
        if cr is None:
            computed['cite_readiness_reason'] = (
                'scoring model unavailable or audit not scorable')

    audit['scoring'] = computed
    return audit


def clamp_shadow(shadow: Any) -> Optional[Dict[str, Any]]:
    """Clamp/enum-guard ONE shadow block; returns the sanitized dict or None.

    The shadow lives in two copies — scoring['shadow'] (validated in
    validate_audit) and the metadata.scoring_shadow mirror that reloaded
    audits fall back to — and BOTH must pass this single clamp before any
    renderer or API forwards them. Kept separate from _num_or_none because
    delta_vs_classic is legitimately NEGATIVE ([-100, 100] clamp), and
    coverage counters must come out as non-negative ints or not at all.
    Mutates in place (pass a copy if the source must stay pristine); never
    raises."""
    if not isinstance(shadow, dict):
        return None
    shadow['pcr_evidence'] = _num_or_none(shadow.get('pcr_evidence'))
    sg = str(shadow.get('grade_evidence') or '').strip()
    if sg not in VALID_GRADES:
        shadow['grade_evidence'] = grade_for(shadow.get('pcr_evidence'))
    d = shadow.get('delta_vs_classic')
    if isinstance(d, bool) or not isinstance(d, (int, float)) \
            or not math.isfinite(float(d)):
        shadow['delta_vs_classic'] = None
    else:
        shadow['delta_vs_classic'] = round(max(-100.0, min(100.0, float(d))), 1)
    if 'coverage' in shadow:
        cov = shadow.get('coverage')
        clean = {}
        if isinstance(cov, dict):
            for k in ('findings_counted', 'findings_total', 'sections_with_data'):
                v = cov.get(k)
                if isinstance(v, bool) or not isinstance(v, (int, float)) \
                        or not math.isfinite(float(v)):
                    continue
                clean[k] = max(0, int(v))
        shadow['coverage'] = clean or None
    return shadow


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

    # SHADOW backstop — same clamp discipline as the classic fields so a
    # persisted-then-edited shadow can't reach the renderer malformed.
    if scoring.get('shadow') is not None:
        scoring['shadow'] = clamp_shadow(scoring.get('shadow'))

    # CITE-READINESS backstop (Phase 2) — same discipline: the object is
    # clamped/enum-guarded wherever it appears, or nulled if malformed.
    if scoring.get('cite_readiness') is not None:
        scoring['cite_readiness'] = clamp_cite_readiness(
            scoring.get('cite_readiness'))

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
