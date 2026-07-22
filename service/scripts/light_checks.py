#!/usr/bin/env python3
"""
light_checks.py — Deterministic 8-factor checks for the LIGHT audit profile.

Every function here is a pure parse over already-fetched bytes: same input →
identical output, zero LLM, zero network. Network I/O lives in the callers
(light_profile.py / the CLI at the bottom), which reuse the service's existing
SSRF-guarded fetch stack (check_schema_completeness.fetch_html — urllib with
per-redirect-hop safety.check_url_safe validation). No new fetcher is written.

The 8 light-profile factors and their check ids:

    factor                              check id                         base
    1. AI-bot robots access             E1/E2/E3/E10/E13 + A10/A11   (existing,
                                        derived from check_robots_txt output)
    2. llms.txt presence                E14_llms_txt        (canonical id —
                                        same check id + verdict bands as the
                                        full profile's deterministic_checks
                                        implementation, shared classifier)
    3. raw-HTML depth (word count)      E5b_raw_html_depth                NEW
    4. LocalBusiness + Geo JSON-LD      D15_localbusiness_geo_schema      NEW
    5. city in <title>/H1               C13_city_in_title_h1              NEW
    6. FAQ content/schema               F3b_faq_content_present (NEW) +
                                        D9_faqpage_schema_vs_visible (existing
                                        measured base, computed here from the
                                        same _bev_analyze primitives)
    7. question-form headings           F6b_question_headings             NEW
    8. prices on page                   F8b_prices_visible                NEW

E5b/F3b/F6b/F8b/C13 are the DETERMINISTIC VARIANTS of factors that the full
(agent) profile classifies via LLM (E5/F3/F6/F8 + city semantics inside C
checks). They deliberately use NEW base codes so the full profile's
evidence-tier derivation is untouched: adding e.g. 'F6' to
scoring.MEASURED_CHECK_BASES would have re-stamped LLM-judged full-profile
findings as measured. Findings from the deterministic variants carry
factor_variant='deterministic'.

Every check returns the repo's standard check dict:
    {status: pass|warn|fail|na, severity, evidence, detail: {...}}

Stdlib only (imports sibling audit scripts for the shared HTML primitives).
"""

from __future__ import annotations

import html as html_lib
import json
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple

_HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))            # sibling scripts
sys.path.insert(0, str(_HERE.parent))     # service/ (safety.py)

# Shared, already-tested HTML primitives — reused, not reimplemented.
from _bev_analyze import (  # noqa: E402
    visible_text, visible_word_count, faq_visible_count, faq_schema_count,
    faq_schema_questions, looks_like_question, extract_first_h1,
    _norm_for_match,
)
from check_schema_completeness import (  # noqa: E402
    extract_schema_blocks, flatten_entities, normalize_type,
)
# E14 verdict bands — the SAME classifier the full profile's
# check_e14_llms_txt uses (single source of truth; light and full paths
# cannot drift on llms.txt semantics).
from deterministic_checks import classify_llms_txt_response  # noqa: E402


# ---------------------------------------------------------------------------
# Shared extraction helpers
# ---------------------------------------------------------------------------

def extract_title(html_str: str) -> Optional[str]:
    """Text of the first <title> tag, entity-decoded and whitespace-collapsed."""
    if not html_str:
        return None
    m = re.search(r'<title[^>]*>(.*?)</title>', html_str,
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    t = re.sub(r'<[^>]+>', ' ', m.group(1))
    t = html_lib.unescape(t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t or None


def extract_headings(html_str: str, levels: Tuple[str, ...] = ('h2', 'h3')) -> List[str]:
    """All heading texts for the given levels, in document order."""
    if not html_str:
        return []
    out: List[str] = []
    pattern = r'<(%s)[^>]*>(.*?)</\1>' % '|'.join(levels)
    for m in re.finditer(pattern, html_str, re.IGNORECASE | re.DOTALL):
        t = re.sub(r'<[^>]+>', ' ', m.group(2))
        t = html_lib.unescape(t)
        t = re.sub(r'\s+', ' ', t).strip()
        if t:
            out.append(t)
    return out


def page_entities(html_str: str) -> List[Dict[str, Any]]:
    """All JSON-LD entities on the page (reuses the schema validator's parser)."""
    return flatten_entities(extract_schema_blocks(html_str or ''))


# LocalBusiness and the subtypes the schema validator + winner-tech care about.
LOCALBUSINESS_TYPES = frozenset({
    'localbusiness', 'medicalbusiness', 'medicalclinic', 'dentist',
    'physician', 'physiotherapy', 'healthandbeautybusiness', 'daycare',
    'store', 'restaurant', 'cafeorcoffeeshop', 'foodestablishment',
    'legalservice', 'attorney', 'accountingservice', 'financialservice',
    'homeandconstructionbusiness', 'plumber', 'electrician', 'roofingcontractor',
    'hvacbusiness', 'movingcompany', 'realestateagent', 'travelagency',
    'automotivebusiness', 'autorepair', 'beautysalon', 'hairsalon', 'nailsalon',
    'daspa', 'dayspa', 'gym', 'exercisegym', 'sportsactivitylocation',
    'lodgingbusiness', 'hotel', 'professionalservice', 'veterinarycare',
    'childcare', 'emergencyservice', 'selfstorage', 'shoppingcenter',
    'petstore', 'librarysystem', 'radiostation', 'televisionstation',
})


def _types_of(entity: Dict[str, Any]) -> List[str]:
    t = entity.get('@type')
    ts = t if isinstance(t, list) else [t]
    return [x.strip().lower() for x in ts if isinstance(x, str)]


def find_localbusiness_entities(entities: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in entities
            if any(t in LOCALBUSINESS_TYPES for t in _types_of(e))]


def _geo_of(entity: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A usable geo block: GeoCoordinates with latitude+longitude."""
    geo = entity.get('geo')
    if isinstance(geo, list):
        geo = geo[0] if geo else None
    if not isinstance(geo, dict):
        return None
    lat = geo.get('latitude')
    lon = geo.get('longitude')
    if lat is None or lon is None:
        return None
    return {'latitude': lat, 'longitude': lon}


def derive_city(entities: List[Dict[str, Any]]) -> Optional[str]:
    """Best-effort target city from JSON-LD: addressLocality of a
    LocalBusiness-family entity first, then any PostalAddress."""
    def _locality(addr: Any) -> Optional[str]:
        if isinstance(addr, list):
            addr = addr[0] if addr else None
        if isinstance(addr, dict):
            loc = addr.get('addressLocality')
            if isinstance(loc, str) and loc.strip():
                return loc.strip()
        return None

    for e in find_localbusiness_entities(entities):
        loc = _locality(e.get('address'))
        if loc:
            return loc
    for e in entities:
        if 'postaladdress' in _types_of(e):
            loc = e.get('addressLocality')
            if isinstance(loc, str) and loc.strip():
                return loc.strip()
        loc = _locality(e.get('address'))
        if loc:
            return loc
    return None


# ---------------------------------------------------------------------------
# Factor 2 — llms.txt presence (E14, NEW)
# ---------------------------------------------------------------------------

_LLMS_SEVERITY = {'pass': 'info', 'fail': 'low', 'na': 'low'}


def check_llms_txt(status: Optional[int], body: Optional[str],
                   fetch_error: Optional[str] = None,
                   content_type: str = '') -> Dict[str, Any]:
    """E14_llms_txt — evaluate an already-fetched /llms.txt response.

    Emits the CANONICAL check id / verdict bands: delegates entirely to
    deterministic_checks.classify_llms_txt_response — the same classifier
    the full profile's check_e14_llms_txt runs — so both paths return the
    identical status for the same bytes (asserted in test_light_profile.py).

    Bands (canonical): 2xx text/markdown-shaped -> pass; 2xx HTML body
    (SPA catch-all soft-200) or 2xx empty or 4xx -> fail; 5xx and
    fetch-failed (status None/0) -> na (unknown — never asserted absent).
    """
    r = classify_llms_txt_response(status, body, content_type)
    r['severity'] = _LLMS_SEVERITY.get(r.get('status'), 'low')
    detail = r.get('detail')
    if isinstance(detail, dict) and fetch_error is not None:
        # only annotate FAILED fetches — success-path detail stays key-compatible
        # with the full profile's check_e14_llms_txt detail block
        detail['fetch_error'] = fetch_error
    return r


# ---------------------------------------------------------------------------
# Factor 3 — raw-HTML depth (E5b, deterministic variant of E5)
# ---------------------------------------------------------------------------

def check_raw_html_depth(html_str: str,
                         visible_words: Optional[int] = None) -> Dict[str, Any]:
    """E5b_raw_html_depth — server-rendered visible word count.

    Same thresholds the Bot's-Eye-View classifier uses (<200 thin,
    200–500 partial, >500 full), applied as a measured check verdict.

    DELIBERATE DIVERGENCE from the study bands: the 2026-07-21 study's
    Tier-0 gate fails raw-HTML pages under ~300 words and its Tier-1 depth
    LEVER only kicks in around ~2,000 words. E5b intentionally keeps the
    BEV SSR-classification bands instead — it answers "is the content
    server-rendered at all?", not "does the page meet the study's
    citation-depth lever?". A 'pass' here therefore does NOT mean
    study-depth met (see docs/LIGHT-PROFILE.md). detail.threshold_basis
    stamps this so downstream consumers can't misread it.
    """
    wc = visible_words if isinstance(visible_words, int) \
        else visible_word_count(html_str or '')
    detail = {'visible_words': wc, 'thresholds': {'thin': 200, 'partial': 500},
              'threshold_basis': 'bev_ssr_classification',
              'study_depth_lever_words': 2000}
    if wc >= 500:
        return {'status': 'pass', 'severity': 'info',
                'evidence': f'{wc} visible words in raw HTML — content is '
                            f'server-rendered at full depth (>500).',
                'detail': detail}
    if wc >= 200:
        return {'status': 'warn', 'severity': 'medium',
                'evidence': f'{wc} visible words in raw HTML — partial SSR '
                            f'(200–500); AI crawlers see a reduced page.',
                'detail': detail}
    return {'status': 'fail', 'severity': 'high',
            'evidence': f'Only {wc} visible words in raw HTML (<200) — the '
                        f'page is effectively dark to non-JS crawlers.',
            'detail': detail}


# ---------------------------------------------------------------------------
# Factor 4 — LocalBusiness + Geo JSON-LD (D15, NEW)
# ---------------------------------------------------------------------------

def check_localbusiness_geo(entities: List[Dict[str, Any]]) -> Dict[str, Any]:
    """D15_localbusiness_geo_schema — a LocalBusiness-family entity exists AND
    carries geo (GeoCoordinates lat/lon); address-only downgrades to warn."""
    lbs = find_localbusiness_entities(entities)
    detail: Dict[str, Any] = {
        'localbusiness_entities': [normalize_type(e.get('@type')) for e in lbs],
        'total_entities': len(entities),
    }
    if not lbs:
        return {'status': 'fail', 'severity': 'medium',
                'evidence': f'No LocalBusiness-typed JSON-LD entity found '
                            f'({len(entities)} entities on page).',
                'detail': detail}
    for e in lbs:
        geo = _geo_of(e)
        if geo:
            detail['geo'] = geo
            return {'status': 'pass', 'severity': 'info',
                    'evidence': f'{normalize_type(e.get("@type"))} entity with '
                                f'GeoCoordinates ({geo["latitude"]}, '
                                f'{geo["longitude"]}).',
                    'detail': detail}
    has_address = any(e.get('address') for e in lbs)
    detail['has_address'] = has_address
    if has_address:
        return {'status': 'warn', 'severity': 'low',
                'evidence': 'LocalBusiness entity present with a postal address '
                            'but no geo (GeoCoordinates latitude/longitude).',
                'detail': detail}
    return {'status': 'fail', 'severity': 'medium',
            'evidence': 'LocalBusiness entity present but has neither geo '
                        'coordinates nor a postal address.',
            'detail': detail}


# ---------------------------------------------------------------------------
# Factor 5 — city in <title>/H1 (C13, NEW)
# ---------------------------------------------------------------------------

def check_city_in_title_h1(html_str: str,
                           city: Optional[str] = None,
                           entities: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """C13_city_in_title_h1 — the target city appears in the title tag or the
    first H1. City comes from the caller (API param) or is derived from the
    page's own JSON-LD address; without either the check is n/a."""
    ents = entities if entities is not None else page_entities(html_str)
    city_source = 'request' if city else None
    if not city:
        city = derive_city(ents)
        city_source = 'schema_address' if city else None
    title = extract_title(html_str)
    h1 = extract_first_h1(html_str or '')
    detail = {'city': city, 'city_source': city_source,
              'title': title, 'h1': h1}
    if not city:
        return {'status': 'na', 'severity': 'info',
                'evidence': 'No target city supplied and none derivable from '
                            'JSON-LD address — city-in-title/H1 not evaluable.',
                'detail': detail}
    c = _norm_for_match(city)
    in_title = bool(title) and c in _norm_for_match(title)
    in_h1 = bool(h1) and c in _norm_for_match(h1)
    detail.update({'in_title': in_title, 'in_h1': in_h1})
    if in_title or in_h1:
        where = ' and '.join(w for w, hit in
                             (('title', in_title), ('H1', in_h1)) if hit)
        return {'status': 'pass', 'severity': 'info',
                'evidence': f'City "{city}" appears in {where}.',
                'detail': detail}
    body_hit = c in _norm_for_match(visible_text(html_str or '', max_chars=20_000))
    detail['in_body_intro'] = body_hit
    if body_hit:
        return {'status': 'warn', 'severity': 'medium',
                'evidence': f'City "{city}" appears in the page text but not '
                            f'in the title tag or H1.',
                'detail': detail}
    return {'status': 'fail', 'severity': 'medium',
            'evidence': f'City "{city}" appears neither in the title tag, the '
                        f'H1, nor the early page text.',
            'detail': detail}


# ---------------------------------------------------------------------------
# Factor 6 — FAQ content/schema (F3b NEW + D9 existing measured base)
# ---------------------------------------------------------------------------

def check_faq_presence(html_str: str) -> Dict[str, Any]:
    """F3b_faq_content_present — visible FAQ widget and/or FAQPage JSON-LD."""
    vis, method = faq_visible_count(html_str or '')
    schema_n = faq_schema_count(html_str or '')
    detail = {'faq_visible': vis, 'detection_method': method,
              'faq_schema_pairs': schema_n}
    if vis > 0 and schema_n > 0:
        return {'status': 'pass', 'severity': 'info',
                'evidence': f'FAQ present: {vis} visible pair(s) ({method}) '
                            f'and {schema_n} pair(s) in FAQPage JSON-LD.',
                'detail': detail}
    if vis > 0:
        return {'status': 'warn', 'severity': 'low',
                'evidence': f'{vis} visible FAQ pair(s) ({method}) but no '
                            f'FAQPage JSON-LD.',
                'detail': detail}
    if schema_n > 0:
        return {'status': 'warn', 'severity': 'medium',
                'evidence': f'{schema_n} FAQ pair(s) in JSON-LD but no visible '
                            f'FAQ widget detected.',
                'detail': detail}
    return {'status': 'fail', 'severity': 'medium',
            'evidence': 'No FAQ content found — neither a visible FAQ section '
                        'nor FAQPage JSON-LD.',
            'detail': detail}


def check_faq_schema_integrity(html_str: str) -> Dict[str, Any]:
    """D9_faqpage_schema_vs_visible — every JSON-LD FAQ question text must be
    present in the visible HTML (Google's FAQPage policy). Same primitives as
    the Bot's-Eye-View integrity verdict."""
    schema_qs = faq_schema_questions(html_str or '')
    vis, method = faq_visible_count(html_str or '')
    detail: Dict[str, Any] = {'faq_schema_pairs': len(schema_qs),
                              'faq_visible': vis, 'detection_method': method}
    if not schema_qs and vis == 0:
        return {'status': 'na', 'severity': 'info',
                'evidence': 'No FAQ schema and no visible FAQ — integrity n/a.',
                'detail': detail}
    if not schema_qs:
        return {'status': 'warn', 'severity': 'low',
                'evidence': f'{vis} visible FAQ pair(s) but no FAQPage JSON-LD '
                            f'to cross-check (markup opportunity).',
                'detail': detail}
    vis_norm = _norm_for_match(visible_text(html_str or '', max_chars=300_000))
    matched = sum(1 for q in schema_qs
                  if _norm_for_match(q) and _norm_for_match(q) in vis_norm)
    detail['schema_questions_visible'] = matched
    if matched >= len(schema_qs):
        return {'status': 'pass', 'severity': 'info',
                'evidence': f'All {len(schema_qs)} FAQPage schema questions '
                            f'appear in the visible HTML.',
                'detail': detail}
    if matched >= (len(schema_qs) + 1) // 2:
        return {'status': 'warn', 'severity': 'medium',
                'evidence': f'{matched}/{len(schema_qs)} FAQPage schema '
                            f'questions found in visible HTML — partial match.',
                'detail': detail}
    return {'status': 'fail', 'severity': 'high',
            'evidence': f'FAQ schema/HTML mismatch: {len(schema_qs)} pairs in '
                        f'JSON-LD, only {matched} question text(s) visible.',
            'detail': detail}


# ---------------------------------------------------------------------------
# Factor 7 — question-form headings (F6b, deterministic variant of F6)
# ---------------------------------------------------------------------------

def check_question_headings(html_str: str) -> Dict[str, Any]:
    """F6b_question_headings — H2/H3 headings phrased as questions
    (same question heuristic the FAQ gate uses: looks_like_question).

    Pass rule follows the study definition (AEO playbook 2026-07-21, the
    15-weight lever): >= 2 interrogative headings — the cited-winner median
    was exactly 2 (e.g. qashio.com: 7 H2s, 2 question headings, cited 85x).
    NOTE: the study counted H1+H2; this check counts H2/H3 (H1 is a single
    page title slot, H2/H3 is where answer-shaped structure lives). The
    ratio>=0.3 branch is an additional pass route for short pages.
    Exactly 1 question heading is a warn; 0 is a fail."""
    headings = extract_headings(html_str, ('h2', 'h3'))
    q = [h for h in headings if looks_like_question(h)]
    detail = {'headings_total': len(headings), 'question_headings': len(q),
              'samples': q[:5]}
    if not headings:
        return {'status': 'warn', 'severity': 'low',
                'evidence': 'No H2/H3 headings found — no answer-shaped '
                            'question structure to evaluate.',
                'detail': detail}
    ratio = len(q) / len(headings)
    detail['ratio'] = round(ratio, 2)
    if len(q) >= 2 or ratio >= 0.3:
        return {'status': 'pass', 'severity': 'info',
                'evidence': f'{len(q)}/{len(headings)} H2/H3 headings are '
                            f'question-form (study pass rule: >=2; e.g. '
                            f'"{q[0][:80]}").',
                'detail': detail}
    if q:
        return {'status': 'warn', 'severity': 'low',
                'evidence': f'Only {len(q)}/{len(headings)} H2/H3 heading(s) '
                            f'are question-form (study pass rule is >=2).',
                'detail': detail}
    return {'status': 'fail', 'severity': 'medium',
            'evidence': f'0 of {len(headings)} H2/H3 headings are phrased as '
                        f'questions — content is not answer-shaped.',
            'detail': detail}


# ---------------------------------------------------------------------------
# Factor 8 — prices on page (F8b, deterministic variant of F8)
# ---------------------------------------------------------------------------

# Currency amount adjacent to a symbol/code, either order, EU or US decimals.
_PRICE_RE = re.compile(
    r'(?:(?:€|\$|£|CHF|USD|EUR|GBP|AED)\s?\d{1,6}(?:[.,]\d{1,2})?)'
    r'|(?:\d{1,6}(?:[.,]\d{1,2})?\s?(?:€|\$|£|CHF|USD|EUR|GBP|AED)\b)'
    r'|(?:\b(?:ab|from|starting at|nur)\s+\d{1,6}(?:[.,]\d{1,2})?\s?(?:€|\$|£|,?-?\s?(?:Euro|Dollar)))',
    re.IGNORECASE,
)

_PRICE_SCHEMA_FIELDS = ('price', 'priceRange', 'lowPrice', 'highPrice',
                        'priceSpecification')


def _schema_price_signals(entities: List[Dict[str, Any]]) -> List[str]:
    hits = []
    for e in entities:
        for f in _PRICE_SCHEMA_FIELDS:
            v = e.get(f)
            if v not in (None, '', [], {}):
                hits.append(f'{normalize_type(e.get("@type"))}.{f}')
    return hits


def check_prices_on_page(html_str: str,
                         entities: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """F8b_prices_visible — concrete price signals in the visible text, with
    Offer/priceRange JSON-LD as a cross-check (schema-only → warn)."""
    text = visible_text(html_str or '', max_chars=300_000)
    matches = _PRICE_RE.findall(text)
    ents = entities if entities is not None else page_entities(html_str)
    schema_hits = _schema_price_signals(ents)
    detail = {'visible_price_matches': len(matches),
              'samples': [m if isinstance(m, str) else ''.join(m)
                          for m in matches[:5]],
              'schema_price_fields': schema_hits}
    if matches:
        return {'status': 'pass', 'severity': 'info',
                'evidence': f'{len(matches)} visible price signal(s) on the '
                            f'page (e.g. "{detail["samples"][0]}")'
                            + (f'; schema price fields: {schema_hits[:3]}.'
                               if schema_hits else '.'),
                'detail': detail}
    if schema_hits:
        return {'status': 'warn', 'severity': 'medium',
                'evidence': f'Price data only in JSON-LD ({schema_hits[:3]}) — '
                            f'no visible price on the page for AI extraction.',
                'detail': detail}
    return {'status': 'fail', 'severity': 'medium',
            'evidence': 'No price signals found — neither visible currency '
                        'amounts nor Offer/priceRange schema.',
            'detail': detail}


# ---------------------------------------------------------------------------
# CLI — fetch (via the existing SSRF-guarded fetcher) + run the page-byte checks
# ---------------------------------------------------------------------------

def run_page_checks(html_str: str, *, city: Optional[str] = None,
                    visible_words: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """All page-byte factor checks over one HTML document, keyed by check id."""
    ents = page_entities(html_str)
    return {
        'E5b_raw_html_depth': check_raw_html_depth(html_str, visible_words),
        'D15_localbusiness_geo_schema': check_localbusiness_geo(ents),
        'C13_city_in_title_h1': check_city_in_title_h1(html_str, city, ents),
        'F3b_faq_content_present': check_faq_presence(html_str),
        'D9_faqpage_schema_vs_visible': check_faq_schema_integrity(html_str),
        'F6b_question_headings': check_question_headings(html_str),
        'F8b_prices_visible': check_prices_on_page(html_str, ents),
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'usage: python3 light_checks.py <URL> [city]'}))
        sys.exit(1)
    from urllib.parse import urlparse
    from check_schema_completeness import fetch_html
    url = sys.argv[1]
    city = sys.argv[2] if len(sys.argv) > 2 else None
    html_str, status, err = fetch_html(url)
    if not html_str:
        print(json.dumps({'url': url, 'error': err or f'HTTP {status}',
                          'http_status': status, 'checks': {}}))
        sys.exit(0)
    checks = run_page_checks(html_str, city=city)
    p = urlparse(url)
    llms_body, llms_status, llms_err = fetch_html(
        f'{p.scheme}://{p.netloc}/llms.txt')
    checks['E14_llms_txt'] = check_llms_txt(llms_status, llms_body, llms_err)
    print(json.dumps({'url': url, 'http_status': status, 'checks': checks},
                     indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
