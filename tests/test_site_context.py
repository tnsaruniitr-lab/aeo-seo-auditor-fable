#!/usr/bin/env python3
"""Test the site-context seam (roadmap 1.4, AnswerMonk -> auditor).

Covers:
  - lenient sanitation: camelCase + snake_case keys, numeric coercion, junk
    dropped, absent/non-dict/empty payloads degrade to None (old behavior)
  - prompt CONTEXT block: rendered only when context exists, tagged as
    measured evidence, explicitly scoped to narrative severity (no check
    status / scoring changes); empty string when absent (prompt unchanged)
  - metadata tagging: {context, evidence: "measured", scope: "narrative-only"}
  - request acceptance: StartAuditRequest accepts a body WITH siteContext
    (wire spelling), WITH site_context (pythonic), and WITHOUT the field —
    exercised against the real FastAPI model when pydantic/fastapi are
    installed (they are in CI, which pip-installs service/requirements.txt
    before this suite); skipped loudly in a bare-stdlib environment.

Run from the service dir (imports site_context/main directly):
    cd service && python3 ../tests/test_site_context.py
Prints SITE_CONTEXT_OK on success, exits non-zero on failure.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                '..', 'service'))

import site_context as sc

# ---------------------------------------------------------------------------
# 1) Sanitation — lenient in, canonical out
# ---------------------------------------------------------------------------
ctx = sc.sanitize_site_context({'orphan': True, 'clickDepth': 5,
                                'duplicateOf': 'https://x.com/a', 'inLinks': 2})
assert ctx == {'orphan': True, 'clickDepth': 5,
               'duplicateOf': 'https://x.com/a', 'inLinks': 2}, ctx

ctx = sc.sanitize_site_context({'is_orphan': False, 'click_depth': '3',
                                'in_links': 7.2, 'duplicate_of': ' a\nb ',
                                'unknown_key': ['ignored']})
assert ctx == {'orphan': False, 'clickDepth': 3, 'inLinks': 7,
               'duplicateOf': 'a b'}, ctx

# Absent / non-dict / all-junk payloads -> None (request behaves as before)
assert sc.sanitize_site_context(None) is None
assert sc.sanitize_site_context('orphan page') is None
assert sc.sanitize_site_context(42) is None
assert sc.sanitize_site_context({}) is None
assert sc.sanitize_site_context({'orphan': 'yes', 'clickDepth': -2,
                                 'inLinks': True, 'duplicateOf': '   '}) is None

# Oversized duplicateOf is capped (it is rendered into the agent prompt)
ctx = sc.sanitize_site_context({'duplicateOf': 'u' * 5000})
assert ctx is not None and len(ctx['duplicateOf']) == 500, ctx

# ---------------------------------------------------------------------------
# 2) Prompt CONTEXT block — measured tag, narrative-only scope
# ---------------------------------------------------------------------------
block = sc.site_context_block({'orphan': True, 'clickDepth': 5,
                               'inLinks': 0, 'duplicateOf': 'https://x.com/a'})
assert 'CONTEXT' in block and 'site-wide crawl' in block, block
assert 'ORPHAN' in block and 'Click depth from the homepage: 5.' in block
assert 'in-links pointing at this page: 0' in block
assert 'Near-duplicate of another page on this site: https://x.com/a' in block
assert 'measured' in block, 'context must be tagged as measured evidence'
assert 'NOT change any check status' in block, 'must stay narrative-only'
# Absent context -> empty string: the agent prompt is byte-identical to before
assert sc.site_context_block(None) == ''
assert sc.site_context_block({}) == ''

# ---------------------------------------------------------------------------
# 3) Metadata tagging for the audit record
# ---------------------------------------------------------------------------
md = sc.metadata_entry({'orphan': True, 'clickDepth': 5})
assert md == {'context': {'orphan': True, 'clickDepth': 5},
              'evidence': 'measured', 'scope': 'narrative-only'}, md
assert sc.metadata_entry(None) is None

# ---------------------------------------------------------------------------
# 4) Request acceptance at the real API model (needs pydantic + fastapi —
#    installed in CI; skip loudly on a bare-stdlib interpreter)
# ---------------------------------------------------------------------------
try:
    import pydantic  # noqa: F401
    import fastapi   # noqa: F401
    _HAVE_API_DEPS = True
except ImportError:
    _HAVE_API_DEPS = False

if _HAVE_API_DEPS:
    os.environ.setdefault('AUDIT_MODE', 'deterministic')
    # import main from the service dir (mirrors CI's import smoke test)
    os.chdir(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          '..', 'service'))
    import main

    # WITH siteContext — wire spelling AnswerMonk sends (camelCase)
    req = main.StartAuditRequest.model_validate(
        {'url': 'https://example.com/page',
         'siteContext': {'orphan': True, 'clickDepth': 5}})
    assert req.site_context == {'orphan': True, 'clickDepth': 5}, req.site_context
    assert sc.sanitize_site_context(req.site_context) == {'orphan': True,
                                                          'clickDepth': 5}

    # WITH site_context — pythonic spelling also accepted
    req = main.StartAuditRequest.model_validate(
        {'url': 'https://example.com/page', 'site_context': {'inLinks': 3}})
    assert req.site_context == {'inLinks': 3}, req.site_context

    # WITHOUT the field — pre-existing contract untouched
    req = main.StartAuditRequest.model_validate(
        {'url': 'https://example.com/page',
         'webhookUrl': 'https://am.example/api/auditor-webhook'})
    assert req.site_context is None
    assert req.webhookUrl == 'https://am.example/api/auditor-webhook'
    assert sc.sanitize_site_context(req.site_context) is None

    # Unknown-key payload is accepted (lenient) and sanitizes to None
    req = main.StartAuditRequest.model_validate(
        {'url': 'https://example.com/page', 'siteContext': {'weird': [1, 2]}})
    assert sc.sanitize_site_context(req.site_context) is None
    accept_note = 'model-accepts:with+without'
else:
    accept_note = 'model-check-skipped(no pydantic/fastapi)'

print('SITE_CONTEXT_OK sanitize+block+metadata verified; ' + accept_note)
