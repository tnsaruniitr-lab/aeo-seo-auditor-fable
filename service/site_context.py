"""
site_context.py — optional site-wide crawl context for a single-page audit
(roadmap 1.4, AnswerMonk seam).

AnswerMonk may attach a `siteContext` object to POST /api/audit/start when it
holds a completed site-wide crawl for the audited domain:

    { orphan?: bool, clickDepth?: int, duplicateOf?: str, inLinks?: int }

This module owns the two seam responsibilities, stdlib-only so the offline
test harness can exercise them directly:

  - sanitize_site_context(raw): lenient validation. Accepts camelCase and
    snake_case keys, coerces numbers, drops junk. Returns a canonical dict
    (camelCase keys) or None when nothing usable remains — None means the
    request behaves exactly as before (fully backward compatible).
  - site_context_block(ctx): the CONTEXT paragraph appended to the agent's
    initial user message. The signals come from a real crawl, so they are
    tagged evidence: "measured" — but they are scoped to NARRATIVE severity
    only. The block explicitly forbids changing check statuses, because all
    scoring math stays deterministic in scoring.py (invariant: the LLM
    classifies, Python grades).

Nothing here touches scoring: sanitized context flows into the prompt and
into audit['metadata']['site_context'] only.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

# duplicateOf is rendered into the agent prompt — cap it and strip newlines so
# a hostile caller can't smuggle multi-line prompt instructions through it.
_MAX_DUPLICATE_OF_LEN = 500

# Metadata tag stored alongside the raw context on the audit record.
EVIDENCE_TAG = 'measured'          # came from a real site-wide crawl
SCOPE_TAG = 'narrative-only'       # must never alter check statuses / scoring


def _coerce_count(value: Any) -> Optional[int]:
    """Non-negative int from an int/float/numeric-string; None for junk.
    bool is excluded explicitly (bool is an int subclass in Python)."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        n = value
    elif isinstance(value, str):
        try:
            n = float(value.strip())
        except (ValueError, AttributeError):
            return None
    else:
        return None
    if n != n or n in (float('inf'), float('-inf')) or n < 0:  # NaN / inf / negative
        return None
    return int(round(n))


def sanitize_site_context(raw: Any) -> Optional[Dict[str, Any]]:
    """Leniently validate a siteContext payload. Returns a canonical dict with
    only the recognized fields (orphan, clickDepth, duplicateOf, inLinks), or
    None when the input is absent, not a dict, or carries nothing usable."""
    if not isinstance(raw, dict):
        return None
    ctx: Dict[str, Any] = {}

    orphan = raw.get('orphan', raw.get('is_orphan', raw.get('isOrphan')))
    if isinstance(orphan, bool):
        ctx['orphan'] = orphan

    for key in ('clickDepth', 'click_depth', 'depth'):
        if key in raw:
            n = _coerce_count(raw.get(key))
            if n is not None:
                ctx['clickDepth'] = n
                break

    for key in ('duplicateOf', 'duplicate_of'):
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            cleaned = ' '.join(v.split())  # collapse whitespace incl. newlines
            ctx['duplicateOf'] = cleaned[:_MAX_DUPLICATE_OF_LEN]
            break

    for key in ('inLinks', 'in_links', 'inlinks'):
        if key in raw:
            n = _coerce_count(raw.get(key))
            if n is not None:
                ctx['inLinks'] = n
                break

    return ctx or None


def site_context_block(ctx: Optional[Dict[str, Any]]) -> str:
    """CONTEXT paragraph for the agent's initial user message. Empty string
    when there is no usable context (prompt unchanged — old behavior)."""
    if not ctx:
        return ''
    lines = []
    if ctx.get('orphan') is True:
        lines.append('- This page is an ORPHAN: the site crawl found no '
                      'internal links pointing to it.')
    elif ctx.get('orphan') is False:
        lines.append('- This page is reachable through internal links '
                      '(not an orphan).')
    if 'clickDepth' in ctx:
        lines.append(f"- Click depth from the homepage: {ctx['clickDepth']}.")
    if 'inLinks' in ctx:
        lines.append(f"- Internal in-links pointing at this page: {ctx['inLinks']}.")
    if 'duplicateOf' in ctx:
        lines.append(f"- Near-duplicate of another page on this site: {ctx['duplicateOf']}")
    if not lines:
        return ''
    return (
        '\n\nCONTEXT — site context from a site-wide crawl of this domain '
        '(evidence: measured — these signals were observed by a real crawl, '
        'not inferred):\n'
        + '\n'.join(lines) + '\n'
        'Use these signals ONLY to sharpen the narrative, severity reasoning '
        'and recommendations (e.g. an orphaned page or click depth 5 makes '
        'discovery-related issues more severe in the write-up), and attribute '
        'them to the site-wide crawl (measured) wherever you cite them. Do '
        'NOT change any check status (pass / warn / fail / na) or invent '
        'findings from this context alone — the score is computed '
        'deterministically from check statuses and must not be affected.'
    )


def metadata_entry(ctx: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """audit['metadata']['site_context'] value: the sanitized context tagged
    with its evidence tier and scope. None when there is no context."""
    if not ctx:
        return None
    return {'context': dict(ctx), 'evidence': EVIDENCE_TAG, 'scope': SCOPE_TAG}


# ---------------------------------------------------------------------------
# SELFTEST — stdlib-only, run by tests/run_tests.sh via tests/test_site_context.py
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    # sanitize: canonical camelCase in
    ctx = sanitize_site_context({'orphan': True, 'clickDepth': 5,
                                 'duplicateOf': 'https://x.com/a', 'inLinks': 2})
    assert ctx == {'orphan': True, 'clickDepth': 5,
                   'duplicateOf': 'https://x.com/a', 'inLinks': 2}, ctx

    # sanitize: snake_case + numeric strings + junk dropped
    ctx = sanitize_site_context({'is_orphan': False, 'click_depth': '3',
                                 'in_links': 7.2, 'duplicate_of': '  a\nb  ',
                                 'unknown': 'ignored'})
    assert ctx == {'orphan': False, 'clickDepth': 3, 'inLinks': 7,
                   'duplicateOf': 'a b'}, ctx

    # sanitize: absent / non-dict / all-junk → None (backward compatible)
    assert sanitize_site_context(None) is None
    assert sanitize_site_context('orphan') is None
    assert sanitize_site_context([1, 2]) is None
    assert sanitize_site_context({}) is None
    assert sanitize_site_context({'orphan': 'yes', 'clickDepth': -1,
                                  'inLinks': True, 'duplicateOf': ''}) is None

    # sanitize: oversized duplicateOf capped
    ctx = sanitize_site_context({'duplicateOf': 'u' * 2000})
    assert ctx is not None and len(ctx['duplicateOf']) == _MAX_DUPLICATE_OF_LEN

    # prompt block: present context renders measured-tagged, narrative-scoped
    block = site_context_block({'orphan': True, 'clickDepth': 5})
    assert 'CONTEXT' in block and 'ORPHAN' in block and 'Click depth from the homepage: 5.' in block
    assert 'measured' in block and 'NOT change any check status' in block
    # prompt block: no context → empty string (prompt byte-identical to before)
    assert site_context_block(None) == ''
    assert site_context_block({}) == ''

    # metadata tagging
    md = metadata_entry({'orphan': True})
    assert md == {'context': {'orphan': True}, 'evidence': 'measured',
                  'scope': 'narrative-only'}, md
    assert metadata_entry(None) is None

    print('SITE_CONTEXT_OK sanitize+block+metadata verified')
