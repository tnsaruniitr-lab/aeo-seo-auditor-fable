#!/usr/bin/env python3
"""
deterministic_checks_extras.py — Additive helpers for deterministic_checks.py.

This module does NOT replace the original deterministic_checks.py (which
contains 9 established checks that are independently verified).

Instead it exposes NEW helper functions that plug into the original via
a simple import:

    # In the original deterministic_checks.py, add near the top:
    try:
        from deterministic_checks_extras import detect_hreflang, looks_like_question
    except ImportError:
        pass

Currently provides:

1. detect_hreflang(html)
   Detects hreflang tags in BOTH top-level <link rel="alternate"> tags
   AND Next.js streaming data (self.__next_f.push chunks). Original code
   only saw top-level tags and reported 0 hreflang on Next.js App Router
   sites that stream metadata via RSC chunks.

2. looks_like_question(text)
   Re-export from _bev_analyze for easy reuse.

3. safe_url_for_shell(url)
   Escapes a URL safely for use in contexts where it might be passed
   to a shell, to defuse the URL-interpolation bug class.

Dependencies: python3 (3.8+). stdlib only.
"""

import re
import shlex
import urllib.parse
from typing import Dict, Set


try:
    from _bev_analyze import looks_like_question  # noqa: F401
except ImportError:
    # Minimal fallback if _bev_analyze isn't on the path
    def looks_like_question(text: str) -> bool:
        t = (text or '').strip().lower()
        if '?' in t:
            return True
        if t.startswith(('faq', 'q:', 'question')):
            return True
        for kw in ('how ', 'what ', 'when ', 'where ', 'why ', 'which ', 'who ',
                   'can ', 'could ', 'do ', 'does ', 'did ', 'is ', 'are ',
                   'was ', 'were ', 'will ', 'would ', 'should '):
            if t.startswith(kw):
                return True
        return False


def detect_hreflang(html: str) -> Dict:
    """
    Detect hreflang tags in HTML, including Next.js streaming data.

    Returns:
        {
          'total_count': int,
          'toplevel_count': int,
          'streamed_count': int,
          'locales': [sorted list],
          'status': 'pass' | 'warn' | 'fail' | 'na',
          'evidence': str,
        }
    """
    if not html:
        return {
            'total_count': 0, 'toplevel_count': 0, 'streamed_count': 0,
            'locales': [], 'status': 'na',
            'evidence': 'Empty HTML input — hreflang not assessable.'
        }

    # Top-level: <link rel="alternate" hreflang="en-AE" href="...">
    # Handle attribute order variations via two passes.
    toplevel_langs: Set[str] = set()
    for m in re.finditer(
        r'<link[^>]*hreflang=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        toplevel_langs.add(m.group(1).lower())
    # Catch the reversed-attribute order case too
    for m in re.finditer(
        r'<link[^>]*rel=["\']alternate["\'][^>]*hreflang=["\']([^"\']+)["\']',
        html, re.IGNORECASE
    ):
        toplevel_langs.add(m.group(1).lower())

    # Streaming: Next.js RSC payloads carry hreflang as a JSON key, e.g.
    # "hrefLang":"en-AE" or escaped \"hrefLang\":\"en-AE\". Scan the WHOLE
    # document case-insensitively — extracting self.__next_f.push(...)
    # chunks first truncated each chunk at its first ')' and missed
    # lowercase "hreflang" keys. The JSON quoted-key syntax cannot appear
    # inside a <link> tag (attributes are hreflang="..."), so the streamed
    # count stays distinct from the top-level count.
    # Only count streamed hits on pages that actually carry Next.js payloads
    # — any inline JS config with an "hreflang" key would otherwise flip a
    # monolingual site from 'na' to a false warn.
    streamed_langs: Set[str] = set()
    if 'self.__next_f' in html or '__NEXT_DATA__' in html:
        for m in re.finditer(
            r'\\*"hreflang\\*"\s*:\s*\\*"([a-zA-Z\-]+)\\*"',
            html, re.IGNORECASE
        ):
            streamed_langs.add(m.group(1).lower())

    total_langs = toplevel_langs | streamed_langs
    total_count = len(total_langs)
    toplevel_count = len(toplevel_langs)
    streamed_count = len(streamed_langs)

    if total_count == 0:
        # Zero hreflang is NOT a failure: hreflang is only required for
        # multi-locale sites. Failing here flagged every monolingual site.
        return {
            'total_count': 0,
            'toplevel_count': 0,
            'streamed_count': 0,
            'locales': [],
            'status': 'na',
            'evidence': (
                'no hreflang declared — only required for multi-locale sites'
            )
        }

    locales = sorted(total_langs)

    if toplevel_count == 0 and streamed_count > 0:
        return {
            'total_count': total_count,
            'toplevel_count': 0,
            'streamed_count': streamed_count,
            'locales': locales,
            'status': 'warn',
            'evidence': (
                f'{streamed_count} hreflang locales found only in Next.js '
                f'streaming data, not as top-level <link> tags. Hydrated '
                f'clients see them; some bots may not. Locales: {locales}'
            )
        }

    if total_count < 2:
        return {
            'total_count': total_count,
            'toplevel_count': toplevel_count,
            'streamed_count': streamed_count,
            'locales': locales,
            'status': 'warn',
            'evidence': (
                f'Only {total_count} hreflang locale defined — needs at '
                f'least 2 + x-default for multi-region.'
            )
        }

    return {
        'total_count': total_count,
        'toplevel_count': toplevel_count,
        'streamed_count': streamed_count,
        'locales': locales,
        'status': 'pass',
        'evidence': f'{total_count} hreflang locales detected: {locales}.'
    }


def safe_url_for_shell(url: str) -> str:
    """
    Quote a URL for safe inclusion in shell command strings.
    Use shlex.quote which handles every edge case (spaces, $, `, \\, etc.).

    Prefer passing URLs as separate argv entries (no shell interpolation)
    whenever possible — this helper is for cases where the code path
    genuinely needs a single string.
    """
    return shlex.quote(url)


def safe_url_components(url: str) -> Dict[str, str]:
    """
    Parse a URL into components using urllib — exposes path + query
    separately so they can be substituted into commands without manual
    string surgery.
    """
    parsed = urllib.parse.urlparse(url)
    return {
        'scheme': parsed.scheme,
        'netloc': parsed.netloc,
        'path': parsed.path or '/',
        'query': parsed.query,
        'fragment': parsed.fragment,
        'origin': f'{parsed.scheme}://{parsed.netloc}',
    }


# ----------------------------------------------------------------------
# Self-test
# ----------------------------------------------------------------------

def _selftest():
    """Quick self-check against known fixtures."""
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    fix_dir = os.path.join(here, '..', 'tests', 'fixtures')

    # Test hreflang detection on Next.js streaming fixture
    path = os.path.join(fix_dir, 'nextjs_streaming_hreflang.html')
    if os.path.exists(path):
        with open(path) as f:
            html = f.read()
        result = detect_hreflang(html)
        print(f'nextjs_streaming_hreflang.html:')
        print(f'  total={result["total_count"]} '
              f'toplevel={result["toplevel_count"]} '
              f'streamed={result["streamed_count"]}')
        print(f'  locales={result["locales"]}')
        print(f'  status={result["status"]}')
        assert result['streamed_count'] == 9, f'expected 9, got {result["streamed_count"]}'
        print(f'  ✓ 9 streamed locales detected')

    # Test hreflang on SSR full landing fixture
    path = os.path.join(fix_dir, 'ssr_full_landing.html')
    if os.path.exists(path):
        with open(path) as f:
            html = f.read()
        result = detect_hreflang(html)
        print(f'ssr_full_landing.html:')
        print(f'  total={result["total_count"]} '
              f'toplevel={result["toplevel_count"]} '
              f'streamed={result["streamed_count"]}')
        assert result['toplevel_count'] == 2, (
            f'expected 2 toplevel, got {result["toplevel_count"]}'
        )
        print(f'  ✓ 2 top-level locales detected')

    # Test URL component parsing
    url_with_query = 'https://example.com/search?q=hello world&lang=en'
    comp = safe_url_components(url_with_query)
    assert comp['path'] == '/search'
    assert 'q=hello' in comp['query']
    print(f'safe_url_components: ✓ parsed path={comp["path"]} query={comp["query"][:40]}')

    # Test shell quoting
    tricky = 'https://example.com/path?a=1&b=$(rm -rf /)'
    quoted = safe_url_for_shell(tricky)
    # Quoted version must contain the dangerous chars only within single quotes
    assert '$(rm' not in quoted.split("'")[0], 'unquoted shell injection leaked'
    print(f'safe_url_for_shell: ✓ shell-dangerous URL quoted safely')

    print('\nAll self-tests passed.')


if __name__ == '__main__':
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == '--selftest':
        _selftest()
    else:
        print('deterministic_checks_v2 is a library module. Use --selftest.')
