"""
mobile_parity.py — mobile render emulation config + mobile-vs-desktop
content-parity comparator (roadmap 2.2).

WHY THIS FILE EXISTS
--------------------
Google indexes the MOBILE rendering of a page (mobile-first indexing), but
the auditor's render pass was desktop-only: desktop UA, desktop viewport,
no touch. A site that hides sections, headings or whole content blocks on
small viewports was scored on content the indexer never sees. The brain
already carries the anti-pattern for this — AP#326 "Mobile version
containing less content than desktop", mapped to check A9 (viewport /
mobile-first indexing) in ruleset/brain-mappings.json — but nothing
measured it.

This module owns the pure, offline-testable pieces of the fix:

  - the mobile emulation profile (viewport 390x844, DPR 3, mobile UA,
    touch) that tools.render_page_js uses for its second pass,
  - the AUDIT_MOBILE_RENDER feature flag (default ON; '0'/'false'/'no'/
    'off' disables — the flag-off path restores the old single-pass
    behavior byte-for-byte except for the additive keys),
  - content_signals(html): deterministic extraction of the parity signals
    (rendered text length, heading set, title / first H1 / meta
    description) from a rendered HTML string,
  - parity_check(desktop_html, mobile_html): the deterministic parity
    verdict, emitted as sub-check `A9b_mobile_content_parity` with
    evidence_tier='measured' (sub-letter convention like A2b/A4b/C12b;
    query_brain resolves it to parent A9_viewport_meta, whose mapping is
    exactly AP#326).

HONEST CWV LABELING (also roadmap 2.2)
--------------------------------------
LCP / CLS collected by a single Playwright run are LAB numbers from one
run — not field data. CWV_LAB_LABEL is stamped next to them wherever they
surface. INP fundamentally requires real-user interaction data (CrUX);
a lab run cannot produce it, so we publish INP_FIELD_NOTE and NEVER a
fabricated INP value.

Nothing here touches scoring math: the parity check is one more finding
whose status feeds the existing per-section equal weighting. PCR_WEIGHTS
are untouched (test-asserted).

Stdlib only.
"""

from __future__ import annotations

import html as html_lib
import os
import re
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# Feature flag + emulation profile
# ---------------------------------------------------------------------------

MOBILE_FLAG_ENV = 'AUDIT_MOBILE_RENDER'
_FLAG_OFF_VALUES = frozenset({'0', 'false', 'no', 'off'})

# iPhone-class profile: ~390x844 CSS px, DPR 3, touch, mobile UA. The UA keeps
# the AEO-Auditor identification suffix for parity with the desktop pass.
MOBILE_USER_AGENT = (
    'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) '
    'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 '
    'Mobile/15E148 Safari/604.1 (compatible; AEO-Auditor/1.0; +Playwright)'
)
MOBILE_VIEWPORT = {'width': 390, 'height': 844}
MOBILE_DEVICE_SCALE_FACTOR = 3


def mobile_render_enabled(env: Optional[Dict[str, str]] = None) -> bool:
    """True unless AUDIT_MOBILE_RENDER is explicitly set to 0/false/no/off.
    Default ON — the flag exists to restore single-pass behavior instantly."""
    source = env if env is not None else os.environ
    raw = str(source.get(MOBILE_FLAG_ENV, '1') or '1').strip().lower()
    return raw not in _FLAG_OFF_VALUES


def mobile_context_kwargs() -> Dict[str, Any]:
    """Playwright new_context(**kwargs) for the mobile emulation pass."""
    return {
        'user_agent': MOBILE_USER_AGENT,
        'viewport': dict(MOBILE_VIEWPORT),
        'device_scale_factor': MOBILE_DEVICE_SCALE_FACTOR,
        'is_mobile': True,
        'has_touch': True,
    }


# ---------------------------------------------------------------------------
# Honest CWV labeling
# ---------------------------------------------------------------------------

# Stamped next to every lab LCP/CLS number this service reports. Lab values
# come from ONE headless run — they are directional, not CrUX field data.
CWV_LAB_LABEL = 'lab (single run)'

# INP cannot be measured in a lab run at all — it needs real-user interaction
# data (CrUX). We publish this note instead of a number, never a fabricated one.
INP_FIELD_NOTE = ('INP requires field data (CrUX real-user measurements) and '
                  'cannot be measured in a single lab run — no INP value is '
                  'reported.')


# ---------------------------------------------------------------------------
# Content-signal extraction (deterministic, regex/stdlib — same approach as
# the deterministic_checks.py strip_tags family)
# ---------------------------------------------------------------------------

PARITY_CHECK_ID = 'A9b_mobile_content_parity'

# Parity thresholds (mobile rendered-text chars / desktop rendered-text chars).
# <0.50 = the mobile render is missing the majority of the desktop content —
# the AP#326 failure under mobile-first indexing. 0.50–0.85 = meaningful gap
# worth a warn. Headings: losing more than half of a real heading set (>=3)
# on mobile is also a fail-grade structural gap.
TEXT_RATIO_FAIL = 0.50
TEXT_RATIO_WARN = 0.85
HEADING_LOSS_FAIL_FRACTION = 0.50
HEADING_MIN_SET_FOR_FAIL = 3


def _visible_text(html: str) -> str:
    """Tag-stripped visible text (script/style/noscript/template removed)."""
    if not html:
        return ''
    c = re.sub(r'<(script|style|noscript|template)[^>]*>.*?</\1\s*>', ' ',
               html, flags=re.DOTALL | re.IGNORECASE)
    c = re.sub(r'<!--.*?-->', ' ', c, flags=re.DOTALL)
    t = re.sub(r'<[^>]+>', ' ', c)
    t = html_lib.unescape(t)
    return re.sub(r'\s+', ' ', t).strip()


def _norm(s: Optional[str]) -> str:
    """Normalization for equality comparison: entities, whitespace, case."""
    if not s:
        return ''
    s = html_lib.unescape(s)
    return re.sub(r'\s+', ' ', s).strip().casefold()


def _headings(html: str) -> List[str]:
    """Normalized text of every h1–h6, document order, empties dropped."""
    out: List[str] = []
    for m in re.finditer(r'<h([1-6])\b[^>]*>(.*?)</h\1\s*>',
                         html or '', re.IGNORECASE | re.DOTALL):
        text = _norm(re.sub(r'<[^>]+>', ' ', m.group(2)))
        if text:
            out.append(text)
    return out


def _title(html: str) -> Optional[str]:
    m = re.search(r'<title[^>]*>([^<]*)</title>', html or '', re.IGNORECASE)
    return (re.sub(r'\s+', ' ', m.group(1)).strip() or None) if m else None


def _first_h1(html: str) -> Optional[str]:
    m = re.search(r'<h1\b[^>]*>(.*?)</h1\s*>', html or '',
                  re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    text = _norm(re.sub(r'<[^>]+>', ' ', m.group(1)))
    return text or None


_ATTR_PAT = r'''(?<![\w-]){name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))'''


def _meta_description(html: str) -> Optional[str]:
    for tag in re.finditer(r'<meta\b[^>]*>', html or '', re.IGNORECASE):
        tag_html = tag.group(0)
        name_m = re.search(_ATTR_PAT.format(name='name'), tag_html, re.IGNORECASE)
        if not name_m:
            continue
        name = next(g for g in name_m.groups() if g is not None).strip().lower()
        if name != 'description':
            continue
        content_m = re.search(_ATTR_PAT.format(name='content'), tag_html,
                              re.IGNORECASE)
        if not content_m:
            continue
        content = html_lib.unescape(
            next(g for g in content_m.groups() if g is not None)).strip()
        if content:
            return content
    return None


def content_signals(html: str) -> Dict[str, Any]:
    """The parity signals for one rendered pass. Pure function of the HTML."""
    text = _visible_text(html or '')
    headings = _headings(html or '')
    return {
        'text_chars': len(text),
        'word_count': len(text.split()) if text else 0,
        'headings': headings,
        'heading_count': len(headings),
        'title': _title(html or ''),
        'h1_first': _first_h1(html or ''),
        'meta_description': _meta_description(html or ''),
    }


# ---------------------------------------------------------------------------
# Parity verdict
# ---------------------------------------------------------------------------

def parity_na(reason: str) -> Dict[str, Any]:
    """The 'not assessable' parity result (flag off / mobile pass failed)."""
    return {
        'check_id': PARITY_CHECK_ID,
        'status': 'na',
        'evidence': f'Mobile parity not assessed: {reason}',
        'evidence_tier': 'measured',
        'detail': {'reason': reason},
    }


def parity_check(desktop_html: str, mobile_html: str) -> Dict[str, Any]:
    """Deterministic mobile-vs-desktop content-parity verdict.

    Compares rendered text length, the heading set, and the key page
    elements (title / first H1 / meta description) between the desktop and
    mobile render passes. Same inputs → same output; no LLM in the loop.
    """
    if not (desktop_html or '').strip():
        return parity_na('desktop pass produced no HTML')
    if not (mobile_html or '').strip():
        return parity_na('mobile pass produced no HTML')

    d = content_signals(desktop_html)
    m = content_signals(mobile_html)

    if d['text_chars'] == 0 and m['text_chars'] == 0:
        return parity_na('no visible text in either pass')

    ratio = (round(m['text_chars'] / d['text_chars'], 3)
             if d['text_chars'] > 0 else None)

    d_set = set(d['headings'])
    m_set = set(m['headings'])
    missing_on_mobile = sorted(d_set - m_set)
    extra_on_mobile = sorted(m_set - d_set)
    missing_fraction = (len(missing_on_mobile) / len(d_set)) if d_set else 0.0

    title_match = _norm(d['title']) == _norm(m['title'])
    h1_match = _norm(d['h1_first']) == _norm(m['h1_first'])
    meta_match = _norm(d['meta_description']) == _norm(m['meta_description'])

    detail = {
        'desktop': {k: d[k] for k in
                    ('text_chars', 'word_count', 'heading_count',
                     'title', 'h1_first')},
        'mobile': {k: m[k] for k in
                   ('text_chars', 'word_count', 'heading_count',
                    'title', 'h1_first')},
        'text_ratio_mobile_vs_desktop': ratio,
        'headings_missing_on_mobile': missing_on_mobile[:10],
        'headings_missing_count': len(missing_on_mobile),
        'headings_extra_on_mobile_count': len(extra_on_mobile),
        'title_match': title_match,
        'h1_match': h1_match,
        'meta_description_match': meta_match,
    }

    key_mismatches = [name for name, okay in
                      (('title', title_match), ('h1', h1_match),
                       ('meta description', meta_match)) if not okay]

    ratio_str = f'{ratio:.2f}' if ratio is not None else 'n/a (desktop empty)'
    base_evidence = (
        f'Rendered text mobile/desktop: {m["text_chars"]}/{d["text_chars"]} '
        f'chars (ratio {ratio_str}). Headings: desktop {len(d_set)} vs '
        f'mobile {len(m_set)} ({len(missing_on_mobile)} missing on mobile).'
    )

    severe_text_loss = ratio is not None and ratio < TEXT_RATIO_FAIL
    severe_heading_loss = (len(d_set) >= HEADING_MIN_SET_FOR_FAIL
                           and missing_fraction > HEADING_LOSS_FAIL_FRACTION)
    if severe_text_loss or severe_heading_loss:
        return {
            'check_id': PARITY_CHECK_ID,
            'status': 'fail',
            'evidence': (
                base_evidence + ' The mobile render is missing the majority '
                'of the desktop content — under mobile-first indexing Google '
                'indexes the MOBILE version, so this content is invisible to '
                'the index (anti-pattern: mobile version containing less '
                'content than desktop). Sample missing headings: '
                f'{missing_on_mobile[:3]}'),
            'evidence_tier': 'measured',
            'detail': detail,
        }

    moderate_text_gap = ratio is not None and ratio < TEXT_RATIO_WARN
    if moderate_text_gap or missing_on_mobile or key_mismatches:
        notes = []
        if moderate_text_gap:
            notes.append('mobile serves measurably less text than desktop')
        if missing_on_mobile:
            notes.append(
                f'{len(missing_on_mobile)} desktop heading(s) absent on '
                f'mobile (e.g. {missing_on_mobile[:2]})')
        if key_mismatches:
            notes.append('mobile differs on: ' + ', '.join(key_mismatches))
        return {
            'check_id': PARITY_CHECK_ID,
            'status': 'warn',
            'evidence': (base_evidence + ' Partial parity gap: '
                         + '; '.join(notes) + '.'),
            'evidence_tier': 'measured',
            'detail': detail,
        }

    return {
        'check_id': PARITY_CHECK_ID,
        'status': 'pass',
        'evidence': (base_evidence + ' Mobile and desktop renders are at '
                     'content parity (text volume, headings, and '
                     'title/H1/meta description all consistent).'),
        'evidence_tier': 'measured',
        'detail': detail,
    }
