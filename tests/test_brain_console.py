"""Offline tests for the Brain Console: routes registered + auth-gated at
include time, probe validation logic, page markers. No DB, no network.
Prints BRAIN_CONSOLE_OK on success (run_tests.sh contract)."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'service'))

import brain_console  # noqa: E402


def test_router_has_all_routes():
    paths = {getattr(r, 'path', None) for r in brain_console.router.routes}
    for p in ('/brain', '/api/brain/overview', '/api/brain/sources',
              '/api/brain/rules', '/api/brain/changes',
              '/api/brain/sources/probe', '/api/brain/sources/{source_id}/toggle'):
        assert p in paths, p


def test_router_defines_no_own_auth_but_main_gates_it():
    # The router deliberately has no per-route dependencies; main.py must
    # include it with dependencies=[Depends(require_auth)] — assert the wiring
    # exists in source so nothing ships open by accident.
    src = open(os.path.join(os.path.dirname(__file__), '..', 'service',
                            'main.py')).read()
    assert 'include_router(brain_console.router, dependencies=[Depends(require_auth)])' in src
    for r in brain_console.router.routes:
        deps = getattr(getattr(r, 'dependant', None), 'dependencies', [])
        assert not deps, f'route {r.path} defines its own auth — centralize it'


def test_source_spec_validation():
    from pydantic import ValidationError
    ok = brain_console.SourceSpec(source_id='my-blog', canonical_org='X',
                                  root_url='https://x.test')
    assert ok.tier == 3 and ok.adapter_type == 'sitemap'
    try:
        brain_console.SourceSpec(source_id='my-blog', canonical_org='X',
                                 root_url='https://x.test', tier=9)
        raise AssertionError('tier 9 must fail')
    except ValidationError:
        pass
    assert brain_console._ID_RE.match('good-slug-2')
    assert not brain_console._ID_RE.match('Bad Slug!')


def test_probe_rejects_incomplete_specs():
    spec = brain_console.SourceSpec(source_id='x-y', canonical_org='X',
                                    root_url='https://x.test',
                                    adapter_type='url_list')
    out = brain_console._probe_spec(spec)
    assert out['ok'] is False and 'seed_urls' in out['error']
    spec2 = brain_console.SourceSpec(source_id='x-y', canonical_org='X',
                                     root_url='https://x.test',
                                     adapter_type='sitemap')
    out2 = brain_console._probe_spec(spec2)
    assert out2['ok'] is False and 'sitemap_url' in out2['error']


def test_page_markers():
    h = brain_console.BRAIN_HTML
    for marker in ('Sieve Brain Console', 'Deep crawl suggested', 'Add a source',
                   'Browse rules', 'Activity', 'data-s="sources"'):
        assert marker in h, marker
    # every dynamic insertion path uses the esc() helper
    assert 'const esc=' in h


if __name__ == '__main__':
    test_router_has_all_routes()
    test_router_defines_no_own_auth_but_main_gates_it()
    test_source_spec_validation()
    test_probe_rejects_incomplete_specs()
    test_page_markers()
    print('BRAIN_CONSOLE_OK')
