"""
validators.py — Portable HTML + JSON-LD validation library.

Self-contained Python module extracted from website-seo-aeo-auditor v3
(scripts/_bev_analyze.py + scripts/check_schema_completeness.py +
scripts/deterministic_checks_extras.py).

USAGE
    Drop this file into your project. No external dependencies.

    from validators import (
        visible_text, visible_word_count,
        faq_visible_count, looks_like_question,
        detect_spa_signals, classify_ssr,
        detect_hreflang,
        extract_schema_blocks, flatten_entities,
        validate_entity_fields, load_schema_specs,
        tier_calculator,
    )

PORTING NOTES
    - Pure stdlib Python (>=3.8). No pip dependencies.
    - If you're porting to TypeScript, the logic translates 1:1 —
      the functions are small and self-contained.
    - schema-specs.json is loaded externally so specs can be
      updated without touching this file.
    - brain-mappings.json is consumed by the caller, not here.

VERSION
    1.0 — extracted 2026-04-21 from the unified build.
"""

from __future__ import annotations

import html as html_lib
import html.parser
import json
import re
from typing import Dict, List, Optional, Set, Tuple


# ======================================================================
# VISIBLE TEXT EXTRACTION (stdlib html.parser, not regex)
# ======================================================================

class _VisibleTextExtractor(html.parser.HTMLParser):
    """Collects text from an HTML doc, skipping script/style/noscript/template."""
    SKIP_TAGS = {'script', 'style', 'noscript', 'template', 'head'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: List[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in self.SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag.lower() in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            self._parts.append(data)

    def get_text(self) -> str:
        return ' '.join(self._parts)


def visible_text(html_str: str, max_chars: int = 50_000) -> str:
    """Extract visible text from HTML via stdlib html.parser.

    Skips <script>, <style>, <noscript>, <template>, <head>.
    Falls back to regex tag-strip if the parser chokes on malformed HTML.
    Never raises.
    """
    if not html_str:
        return ''
    ex = _VisibleTextExtractor()
    try:
        ex.feed(html_str)
    except Exception:
        stripped = re.sub(r'<script[^>]*>.*?</script>', ' ', html_str, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r'<style[^>]*>.*?</style>', ' ', stripped, flags=re.DOTALL | re.IGNORECASE)
        stripped = re.sub(r'<[^>]+>', ' ', stripped)
        stripped = html_lib.unescape(stripped)
        return re.sub(r'\s+', ' ', stripped).strip()[:max_chars]
    text = ex.get_text()
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:max_chars]


def visible_word_count(html_str: str) -> int:
    """Count visible (human-readable) words in an HTML document."""
    text = visible_text(html_str)
    return len([w for w in text.split() if w.strip()])


# ======================================================================
# FAQ DETECTION WITH QUESTION-INTENT GATE
# ======================================================================

_QUESTION_WORDS = (
    'how ', 'what ', 'when ', 'where ', 'why ', 'which ', 'who ',
    'can ', 'could ', 'do ', 'does ', 'did ',
    'is ', 'are ', 'was ', 'were ',
    'will ', 'would ', 'should ', 'shall ', 'may ', 'might ',
    'have ', 'has ', 'had ',
)


def looks_like_question(text: str) -> bool:
    """Heuristic: does this text look like a user-facing question?

    True when the text:
    - contains a '?', OR
    - starts with 'faq', 'q:', or 'question', OR
    - starts with a question word (how, what, when, where, why, which,
      who, can, could, do, does, did, is, are, was, were, will, would,
      should, shall, may, might, have, has, had).
    """
    if not text:
        return False
    t = text.strip().lower()
    if '?' in t:
        return True
    if t.startswith(('faq', 'q:', 'question')):
        return True
    if any(t.startswith(kw) for kw in _QUESTION_WORDS):
        return True
    return False


def faq_visible_count(html_str: str) -> Tuple[int, str]:
    """Count visible FAQ pairs using multiple detection patterns with
    question-intent gating.

    Returns (count, detection_method).

    This is the fix for the 'country accordion counts as FAQ' bug —
    <details>/<summary> pairs only count when the summary looks like
    a question.
    """
    if not html_str:
        return 0, 'empty_html'

    # Pattern 1: <details><summary> WITH question-like summary text
    summaries = re.findall(
        r'<summary[^>]*>(.*?)</summary>', html_str, re.IGNORECASE | re.DOTALL
    )
    q_summaries = []
    for s in summaries:
        text = re.sub(r'<[^>]+>', ' ', s).strip()
        text = html_lib.unescape(text)
        if looks_like_question(text):
            q_summaries.append(s)
    if q_summaries:
        return len(q_summaries), 'details_summary_question'

    # Pattern 2: <dl><dt><dd> with question-like <dt> content
    dts = re.findall(r'<dt[^>]*>(.*?)</dt>', html_str, re.IGNORECASE | re.DOTALL)
    dds = re.findall(r'<dd[^>]*>', html_str, re.IGNORECASE)
    if len(dts) >= 3 and len(dds) >= 3:
        q_dts = [dt for dt in dts if looks_like_question(
            re.sub(r'<[^>]+>', ' ', html_lib.unescape(dt))
        )]
        if len(q_dts) >= len(dts) / 2:
            return min(len(q_dts), len(dds)), 'dl_dt_dd_question'

    # Pattern 3: data-slot="accordion-item" (shadcn/ui)
    accordion = re.findall(r'data-slot=["\']accordion-item["\']', html_str, re.IGNORECASE)
    if accordion:
        return len(accordion), 'data_slot_accordion'

    # Pattern 4: class="*accordion-item*" / "*faq-item*" / "*faq-entry*"
    class_items = re.findall(
        r'class=["\'][^"\']*(?:accordion-item|faq-item|faq-entry|faq-question)',
        html_str, re.IGNORECASE
    )
    if class_items:
        return len(class_items), 'class_accordion_item'

    # Pattern 5: H3 tags ending in ? — strong signal of FAQ headings
    h3_qs = re.findall(r'<h3[^>]*>\s*[^<]*\?\s*</h3>', html_str, re.IGNORECASE)
    if h3_qs and len(h3_qs) >= 3:
        return len(h3_qs), 'h3_question_headings'

    return 0, 'none_detected'


def faq_schema_count(html_str: str) -> int:
    """Count FAQ Question entities in FAQPage JSON-LD, if present."""
    blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_str, re.IGNORECASE | re.DOTALL
    )
    for b in blocks:
        try:
            data = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get('@type') == 'FAQPage':
                me = item.get('mainEntity', [])
                if isinstance(me, list):
                    return len(me)
            graph = item.get('@graph', [])
            if isinstance(graph, list):
                for g in graph:
                    if isinstance(g, dict) and g.get('@type') == 'FAQPage':
                        me = g.get('mainEntity', [])
                        if isinstance(me, list):
                            return len(me)
    return 0


# ======================================================================
# SPA FRAMEWORK DETECTION + SSR CLASSIFICATION
# ======================================================================

def detect_spa_signals(html_str: str) -> List[str]:
    """Return list of SPA framework hints detected in raw HTML."""
    signals = []
    if re.search(r'<app-root', html_str, re.IGNORECASE):
        signals.append('angular_app_root')
    if (re.search(r'<div[^>]*id=["\']__next["\']', html_str, re.IGNORECASE)
            or '__NEXT_DATA__' in html_str
            or 'self.__next_f' in html_str):
        signals.append('nextjs')
    if (re.search(r'<div[^>]*id=["\']root["\']', html_str, re.IGNORECASE)
            and 'react' in html_str.lower()):
        signals.append('react_root')
    if (('id="app"' in html_str or "id='app'" in html_str)
            and 'vue' in html_str.lower()):
        signals.append('vue_app')
    if re.search(r'<div[^>]*id=["\']__nuxt["\']', html_str, re.IGNORECASE):
        signals.append('nuxt')
    return signals


_UI_ACTION_H1_KEYWORDS = (
    'select', 'choose', 'pick', 'continue', 'enter your',
    'get started', 'sign in', 'log in', 'welcome',
)


def classify_ssr(
    visible_words: int,
    same_as_404: bool,
    spa_signals: List[str],
    h1_first: Optional[str] = None,
    html_snippet: Optional[str] = None,
) -> str:
    """Deterministic SSR/SPA classification.

    Returns one of:
      - 'spa_no_ssr'                  : identical shell for every URL. Dark to AI.
      - 'ssr_shell_js_hidden_content' : SSR renders only a modal/gate; real
                                        content is in the JS bundle.
      - 'js_dependent'                : <200 visible words + SPA signals.
      - 'minimal_content'             : <200 visible words, no SPA signals.
      - 'partial_ssr'                 : 200-500 words.
      - 'fully_accessible'            : >500 visible words in raw HTML.
    """
    if same_as_404:
        return 'spa_no_ssr'

    ui_action_h1 = False
    if h1_first:
        h1_lower = h1_first.lower()
        ui_action_h1 = any(kw in h1_lower for kw in _UI_ACTION_H1_KEYWORDS)

    has_next_streaming = bool(html_snippet and 'self.__next_f.push' in html_snippet)
    rich_bundle = has_next_streaming and html_snippet and len(html_snippet) > 40_000
    if visible_words < 200 and ui_action_h1 and rich_bundle:
        return 'ssr_shell_js_hidden_content'

    if visible_words < 200:
        if spa_signals:
            return 'js_dependent'
        return 'minimal_content'
    if visible_words < 500:
        return 'partial_ssr'
    return 'fully_accessible'


# ======================================================================
# HREFLANG DETECTION (top-level + Next.js streaming)
# ======================================================================

def detect_hreflang(html_str: str) -> Dict:
    """Detect hreflang tags in HTML, including Next.js streaming data.

    Returns {total_count, toplevel_count, streamed_count, locales,
             status ('pass'|'warn'|'fail'), evidence}.

    Critical fix: Next.js App Router streams hreflang metadata inside
    self.__next_f.push(...) chunks, not as top-level <link> tags. The
    naive grep misses these.
    """
    if not html_str:
        return {
            'total_count': 0, 'toplevel_count': 0, 'streamed_count': 0,
            'locales': [], 'status': 'fail', 'evidence': 'Empty HTML.'
        }

    toplevel_langs: Set[str] = set()
    for m in re.finditer(
        r'<link[^>]*hreflang=["\']([^"\']+)["\']',
        html_str, re.IGNORECASE
    ):
        toplevel_langs.add(m.group(1).lower())
    for m in re.finditer(
        r'<link[^>]*rel=["\']alternate["\'][^>]*hreflang=["\']([^"\']+)["\']',
        html_str, re.IGNORECASE
    ):
        toplevel_langs.add(m.group(1).lower())

    streamed_langs: Set[str] = set()
    push_chunks = re.findall(
        r'self\.__next_f\.push\((.*?)\)',
        html_str, re.IGNORECASE | re.DOTALL
    )
    for chunk in push_chunks:
        for m in re.finditer(
            r'\\*"hrefLang\\*"\s*:\s*\\*"([a-zA-Z\-]+)\\*"',
            chunk
        ):
            streamed_langs.add(m.group(1).lower())

    total_langs = toplevel_langs | streamed_langs
    total_count = len(total_langs)
    locales = sorted(total_langs)

    if total_count == 0:
        return {
            'total_count': 0, 'toplevel_count': 0, 'streamed_count': 0,
            'locales': [], 'status': 'fail',
            'evidence': 'No hreflang detected in <head> or Next.js streaming data.'
        }
    if len(toplevel_langs) == 0 and len(streamed_langs) > 0:
        return {
            'total_count': total_count, 'toplevel_count': 0,
            'streamed_count': len(streamed_langs), 'locales': locales,
            'status': 'warn',
            'evidence': f'{len(streamed_langs)} hreflang locales in Next.js '
                        f'streaming data only (not top-level <link>). Hydrated '
                        f'clients see them; some bots may not. Locales: {locales}'
        }
    if total_count < 2:
        return {
            'total_count': total_count, 'toplevel_count': len(toplevel_langs),
            'streamed_count': len(streamed_langs), 'locales': locales,
            'status': 'warn',
            'evidence': f'Only {total_count} hreflang locale — need 2+ + x-default.'
        }
    return {
        'total_count': total_count, 'toplevel_count': len(toplevel_langs),
        'streamed_count': len(streamed_langs), 'locales': locales,
        'status': 'pass',
        'evidence': f'{total_count} hreflang locales: {locales}'
    }


# ======================================================================
# JSON-LD SCHEMA EXTRACTION + VALIDATION
# ======================================================================

def extract_schema_blocks(html_str: str) -> List:
    """Extract all JSON-LD blocks from HTML. Returns parsed dict/list objects.

    Blocks that fail to parse are included as {'__parse_error': str, ...}.
    """
    if not html_str:
        return []
    blocks = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html_str, re.IGNORECASE | re.DOTALL
    ):
        try:
            parsed = json.loads(m.group(1).strip())
            blocks.append(parsed)
        except json.JSONDecodeError as e:
            blocks.append({'__parse_error': str(e), '__raw_start': m.group(1)[:200]})
    return blocks


def flatten_entities(blocks: List, max_depth: int = 5) -> List[Dict]:
    """Flatten JSON-LD blocks into a single list of entity dicts.

    Handles: @graph, arrays at any depth, nested entities (founder, author,
    medicalSpecialist, member, worksFor, etc.).
    """
    entities: List[Dict] = []

    def walk(obj, depth: int = 0):
        if depth > max_depth:
            return
        if isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)
            return
        if not isinstance(obj, dict):
            return
        if '@graph' in obj and isinstance(obj['@graph'], list):
            for g in obj['@graph']:
                walk(g, depth + 1)
        if obj.get('@type'):
            entities.append(obj)
        for key in ('author', 'publisher', 'founder', 'founders',
                    'employee', 'member', 'worksFor', 'mainEntity',
                    'itemListElement', 'itemReviewed', 'about'):
            if key in obj:
                walk(obj[key], depth + 1)

    for block in blocks:
        if isinstance(block, dict) and '__parse_error' in block:
            continue
        walk(block)

    return entities


def load_schema_specs(path: str) -> Dict:
    """Load schema-specs.json (the FIELD_SPECS + custom_validation_rules)."""
    with open(path) as f:
        return json.load(f)


def validate_entity_fields(
    entity: Dict,
    field_specs: Dict,
) -> Dict:
    """Check a single entity against its @type's field spec.

    field_specs is the 'field_specs' dict from schema-specs.json.

    Returns:
      {
        'type': str,
        'type_known': bool,
        'missing_required': [str],
        'missing_google_required': [str],
        'missing_recommended': [str],
        'has_id': bool,
      }
    """
    t = entity.get('@type')
    if isinstance(t, list):
        t = t[0]
    if not t:
        return {'type': None, 'type_known': False,
                'missing_required': [], 'missing_google_required': [],
                'missing_recommended': [], 'has_id': bool(entity.get('@id'))}

    spec = field_specs.get(t)
    if not spec:
        return {'type': t, 'type_known': False,
                'missing_required': [], 'missing_google_required': [],
                'missing_recommended': [], 'has_id': bool(entity.get('@id'))}

    missing_req = [f for f in spec.get('required', []) if not entity.get(f)]
    missing_goog = [f for f in spec.get('google_required', []) if not entity.get(f)]
    missing_rec = [f for f in spec.get('recommended', []) if not entity.get(f)]

    return {
        'type': t,
        'type_known': True,
        'missing_required': missing_req,
        'missing_google_required': missing_goog,
        'missing_recommended': missing_rec,
        'has_id': bool(entity.get('@id')),
    }


# ======================================================================
# TIER CALCULATOR (for evidence-cell tiering in authority/social audits)
# ======================================================================

def tier_calculator(evidence: Dict) -> str:
    """Compute HIGH / MID / LOW tier for an authority/social evidence cell.

    Rules:
      HIGH:  profile_url verified (2xx) AND primary_metric_value not null
             AND at least one sample_evidence with source_url AND
             latest_activity_date within last 180 days
      MID:   profile_url verified AND (primary_metric_value not null
             OR sample_evidence non-empty) but fails HIGH criteria
      LOW:   everything else

    evidence shape:
      {
        'profile_url': str | null,
        'profile_verified': bool,
        'primary_metric_value': number | null,
        'sample_evidence': [{text, source_url}],
        'latest_activity_date': str (ISO) | null,
      }
    """
    import datetime

    profile_verified = evidence.get('profile_verified', False)
    primary_metric = evidence.get('primary_metric_value')
    samples = evidence.get('sample_evidence') or []
    latest = evidence.get('latest_activity_date')

    has_metric = primary_metric is not None
    has_cited_sample = any(
        isinstance(s, dict) and s.get('text') and s.get('source_url')
        for s in samples
    )

    within_180d = False
    if latest:
        try:
            d = datetime.datetime.fromisoformat(latest.replace('Z', '+00:00'))
            delta = datetime.datetime.now(datetime.timezone.utc) - d
            within_180d = delta.days <= 180
        except (ValueError, AttributeError):
            within_180d = False

    if profile_verified and has_metric and has_cited_sample and within_180d:
        return 'HIGH'
    if profile_verified and (has_metric or has_cited_sample):
        return 'MID'
    return 'LOW'


# ======================================================================
# SELF-TEST
# ======================================================================

def _selftest():
    """Minimal smoke tests so consumers can verify imports."""
    assert visible_word_count('<p>Hello world</p>') == 2
    assert visible_word_count('<script>alert("x")</script><p>Real text here</p>') == 3
    assert looks_like_question('How do I book a trip?') is True
    assert looks_like_question('United Arab Emirates') is False
    assert looks_like_question('FAQ about pricing') is True
    assert classify_ssr(8, True, [], None, '') == 'spa_no_ssr'
    assert classify_ssr(100, False, ['nextjs'], None, '') == 'js_dependent'
    assert classify_ssr(600, False, [], None, '') == 'fully_accessible'

    hreflang = detect_hreflang(
        '<link rel="alternate" hreflang="en-US" href="https://example.com/en-us" />'
        '<link rel="alternate" hreflang="x-default" href="https://example.com" />'
    )
    assert hreflang['total_count'] == 2, hreflang

    blocks = extract_schema_blocks(
        '<script type="application/ld+json">'
        '{"@context":"https://schema.org","@type":"Article","headline":"x"}</script>'
    )
    assert len(blocks) == 1
    ents = flatten_entities(blocks)
    assert ents[0]['@type'] == 'Article'

    print('validators.py self-tests passed.')


if __name__ == '__main__':
    _selftest()
