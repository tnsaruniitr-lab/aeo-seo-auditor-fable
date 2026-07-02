#!/usr/bin/env python3
"""
check_sitemap_v2.py — Fixed sitemap validator.

Replaces the original scripts/check_sitemap.py. Fixes:

1. Real XML parsing via xml.etree.ElementTree (stdlib).
   Original used regex; corrupted URLs with &amp; entities, CDATA,
   and namespace variations.

2. HEAD with GET fallback for sample URL probing.
   Original used HEAD-only (curl -I); falsely failed URLs on servers
   that return 405/501 for HEAD but 200 for GET (Cloudflare, nginx
   with specific configs).

3. Safe URL quoting when interpolating into shell/curl commands.
   Prevents URL fragments like `?q=hello&world` from being mangled.

4. Graceful handling of empty/malformed sitemap responses.
   Original raised uncaught exceptions.

Interface preserved: same CLI (`python3 check_sitemap_v2.py <URL>`),
same JSON output schema as check_sitemap.py.

Dependencies: curl, python3 (3.8+). stdlib only.
"""

import gzip
import zlib
import hashlib
import json
import pathlib
import re
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

# SSRF guard (canonical impl at service/safety.py, one dir up). Sitemap and
# sample-URL probes below use `curl -L`, which follows redirects — a submitted
# URL (or a sitemap <loc>) pointing at an internal host / metadata IP must be
# refused before curl runs.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
try:
    from safety import check_url_safe
except Exception:
    def check_url_safe(url, resolve=True):  # stdlib fallback keeps scripts standalone
        return True, None   # (only used if safety.py is somehow absent)

SITEMAP_NAMESPACE = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
CURL_TIMEOUT = 15
USER_AGENT = 'Mozilla/5.0 (compatible; SEO-AEO-Auditor/2.0)'
MAX_INDEX_DEPTH = 2
MAX_SUBSITEMAPS_PER_INDEX = 20
SAMPLE_SIZE = 10


def curl_fetch(url: str, timeout: int = CURL_TIMEOUT) -> Tuple[int, str, str]:
    """
    Fetch URL via curl. Returns (http_code, body, error).
    URL is passed as a separate argv entry — no shell interpolation.

    Captures bytes (not text) so gzipped sitemaps (.xml.gz, or gzip bodies
    served without Content-Encoding) are gunzipped instead of crashing the
    UTF-8 decode. `--compressed` handles Content-Encoding: gzip transfers.
    """
    # SSRF: refuse internal/metadata targets before spawning curl.
    ok, reason = check_url_safe(url)
    if not ok:
        return 0, '', f'blocked by SSRF guard: {reason}'
    try:
        result = subprocess.run(
            ['curl', '-sS', '-L', '--max-redirs', '5',
             '--max-time', str(timeout),
             '--compressed',
             '-A', USER_AGENT,
             '-w', '\n---HTTP_CODE---\n%{http_code}',
             url],
            capture_output=True, timeout=timeout + 5
        )
        output = result.stdout
        marker = b'\n---HTTP_CODE---\n'
        if marker in output:
            body_bytes, code_str = output.rsplit(marker, 1)
            try:
                code = int(code_str.strip().decode('ascii', errors='replace'))
            except ValueError:
                code = 0
        else:
            body_bytes = output
            code = 0
        # Gunzip raw gzip payloads (curl --compressed does not decode
        # bodies served without a Content-Encoding header).
        if body_bytes[:2] == b'\x1f\x8b' or url.lower().split('?')[0].endswith('.gz'):
            try:
                body_bytes = gzip.decompress(body_bytes)
            except (OSError, EOFError, zlib.error):
                pass  # not gzipped / truncated download — keep the raw bytes
        body = body_bytes.decode('utf-8', errors='replace')
        stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
        return code, body, stderr
    except subprocess.TimeoutExpired:
        return 0, '', 'timeout'
    except FileNotFoundError:
        return 0, '', 'curl not installed'
    except Exception as e:
        return 0, '', f'{type(e).__name__}: {e}'


def probe_url(url: str, timeout: int = 10) -> Tuple[int, str]:
    """
    Probe a URL's reachability. HEAD first; fall back to GET with Range
    if HEAD returns 405/501. Returns (http_code, method_used).

    This is the fix for the HEAD-only bug that falsely flagged URLs
    on servers that don't allow HEAD but accept GET.
    """
    # SSRF: refuse probing internal/metadata targets (sitemap <loc> entries
    # are attacker-influenceable). Reported as an unreachable probe.
    ok, _reason = check_url_safe(url)
    if not ok:
        return 0, 'blocked_ssrf'
    # First attempt: HEAD
    try:
        r = subprocess.run(
            ['curl', '-sS', '-I', '-L', '--max-redirs', '3',
             '--max-time', str(timeout),
             '-A', USER_AGENT,
             '-o', '/dev/null',
             '-w', '%{http_code}',
             url],
            capture_output=True, text=True, timeout=timeout + 3
        )
        head_code = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    except (subprocess.TimeoutExpired, ValueError):
        head_code = 0

    # If HEAD said 405 Method Not Allowed or 501 Not Implemented,
    # try GET with a byte range to minimize transfer.
    if head_code in (405, 501):
        try:
            r = subprocess.run(
                ['curl', '-sS', '-L', '--max-redirs', '3',
                 '--max-time', str(timeout),
                 '-A', USER_AGENT,
                 '-H', 'Range: bytes=0-1023',
                 '-o', '/dev/null',
                 '-w', '%{http_code}',
                 url],
                capture_output=True, text=True, timeout=timeout + 3
            )
            get_code = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
            return get_code, 'GET_range'
        except (subprocess.TimeoutExpired, ValueError):
            return 0, 'GET_failed'

    # If HEAD succeeded (2xx/3xx) or gave a definitive error (4xx other than
    # 405, or 5xx other than 501), trust it.
    return head_code, 'HEAD'


def parse_sitemap_xml(xml_body: str) -> Optional[Dict]:
    """
    Parse sitemap XML using stdlib xml.etree.ElementTree.
    Returns {'type': 'index' | 'urlset', 'entries': [...]} or None on failure.

    Handles: &amp; entities, CDATA sections, namespaces, comments.
    Does NOT use regex on XML (the original bug).
    """
    if not xml_body or not xml_body.strip():
        return None

    try:
        root = ET.fromstring(xml_body)
    except ET.ParseError as e:
        return {'parse_error': f'{type(e).__name__}: {e}'}

    # Strip namespace from tag name for simpler inspection
    tag = root.tag.split('}')[-1] if '}' in root.tag else root.tag

    # Try with namespace first, fall back to no namespace
    def find_all(element, child_tag):
        with_ns = element.findall(f'sm:{child_tag}', SITEMAP_NAMESPACE)
        if with_ns:
            return with_ns
        return element.findall(child_tag)

    def get_text(element, child_tag):
        child = element.find(f'sm:{child_tag}', SITEMAP_NAMESPACE)
        if child is None:
            child = element.find(child_tag)
        return child.text.strip() if child is not None and child.text else None

    if tag == 'sitemapindex':
        entries = []
        for sm in find_all(root, 'sitemap'):
            loc = get_text(sm, 'loc')
            lastmod = get_text(sm, 'lastmod')
            if loc:
                entries.append({'loc': loc, 'lastmod': lastmod})
        return {'type': 'index', 'entries': entries}

    elif tag == 'urlset':
        entries = []
        for url_el in find_all(root, 'url'):
            loc = get_text(url_el, 'loc')
            lastmod = get_text(url_el, 'lastmod')
            changefreq = get_text(url_el, 'changefreq')
            priority = get_text(url_el, 'priority')
            if loc:
                entries.append({
                    'loc': loc,
                    'lastmod': lastmod,
                    'changefreq': changefreq,
                    'priority': priority
                })
        return {'type': 'urlset', 'entries': entries}

    else:
        return {'parse_error': f'unexpected root tag: {tag}'}


def discover_sitemap_urls(base_url: str) -> Tuple[List[str], str]:
    """
    Try 3 discovery paths in order:
    1. robots.txt Sitemap: directives (ALL of them, not just the first)
    2. /sitemap.xml
    3. /sitemap_index.xml
    Returns (sitemap_urls, discovered_via) or ([], reason).
    """
    parsed = urllib.parse.urlparse(base_url)
    origin = f'{parsed.scheme}://{parsed.netloc}'

    # 1. robots.txt — collect every Sitemap: directive
    robots_url = f'{origin}/robots.txt'
    code, body, _ = curl_fetch(robots_url)
    if 200 <= code < 300 and body:
        sm_urls = []
        for line in body.splitlines():
            line = line.strip()
            if line.lower().startswith('sitemap:'):
                sm_url = line.split(':', 1)[1].strip()
                if sm_url and sm_url not in sm_urls:
                    sm_urls.append(sm_url)
        if sm_urls:
            return sm_urls, 'robots_txt_directive'

    # 2. /sitemap.xml
    sm_url = f'{origin}/sitemap.xml'
    code, _, _ = curl_fetch(sm_url)
    if 200 <= code < 300:
        return [sm_url], 'default_sitemap_xml'

    # 3. /sitemap_index.xml
    sm_url = f'{origin}/sitemap_index.xml'
    code, _, _ = curl_fetch(sm_url)
    if 200 <= code < 300:
        return [sm_url], 'default_sitemap_index_xml'

    return [], 'not_discovered'


def traverse_sitemap(
    sitemap_url: str, depth: int = 0, seen: Optional[set] = None,
    stats: Optional[Dict] = None
) -> Tuple[List[Dict], List[str], Dict]:
    """
    Recursively fetch and parse sitemap + sitemap indexes.
    Returns (all_url_entries, errors, stats).

    stats records per-FILE URL counts (the 50K limit is per sitemap file,
    not per aggregated total) and whether traversal was truncated by the
    MAX_SUBSITEMAPS_PER_INDEX / MAX_INDEX_DEPTH bounds.
    """
    if seen is None:
        seen = set()
    if stats is None:
        stats = {'file_url_counts': {}, 'truncated': False}
    if sitemap_url in seen:
        return [], [], stats
    if depth > MAX_INDEX_DEPTH:
        stats['truncated'] = True
        return [], [], stats
    seen.add(sitemap_url)

    errors = []
    all_entries = []

    code, body, err = curl_fetch(sitemap_url)
    if code == 0 or not body:
        errors.append(f'fetch failed for {sitemap_url}: {err or f"HTTP {code}"}')
        return [], errors, stats
    if code >= 400:
        errors.append(f'HTTP {code} for {sitemap_url}')
        return [], errors, stats

    parsed = parse_sitemap_xml(body)
    if parsed is None:
        errors.append(f'empty/invalid XML at {sitemap_url}')
        return [], errors, stats
    if 'parse_error' in parsed:
        errors.append(f'parse error at {sitemap_url}: {parsed["parse_error"]}')
        return [], errors, stats

    if parsed['type'] == 'urlset':
        stats['file_url_counts'][sitemap_url] = len(parsed['entries'])
        return parsed['entries'], errors, stats

    elif parsed['type'] == 'index':
        # Recurse into sub-sitemaps (bounded)
        sub_entries = parsed['entries'][:MAX_SUBSITEMAPS_PER_INDEX]
        if len(parsed['entries']) > MAX_SUBSITEMAPS_PER_INDEX:
            stats['truncated'] = True
        for sub in sub_entries:
            sub_url = sub['loc']
            child_entries, child_errors, stats = traverse_sitemap(
                sub_url, depth + 1, seen, stats)
            all_entries.extend(child_entries)
            errors.extend(child_errors)

    return all_entries, errors, stats


def deterministic_sample(
    entries: List[Dict], target_url: str, sample_size: int = SAMPLE_SIZE
) -> List[Dict]:
    """
    Deterministic sampling: for a given target_url, always returns the same
    sample entries from a given entries list. Uses MD5 hash for stable order.
    """
    if len(entries) <= sample_size:
        return entries

    seed = target_url.encode()
    scored = []
    for entry in entries:
        h = hashlib.md5(seed + entry['loc'].encode()).hexdigest()
        scored.append((h, entry))
    scored.sort(key=lambda x: x[0])
    return [e for _, e in scored[:sample_size]]


def normalize_url_for_compare(url: str) -> str:
    """
    Normalize a URL for sitemap-membership comparison: strip scheme,
    leading 'www.', and trailing slash. https://www.x.com/a/ and
    http://x.com/a compare equal.
    """
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower()
    if host.startswith('www.'):
        host = host[4:]
    path = p.path.rstrip('/')
    query = f'?{p.query}' if p.query else ''
    return f'{host}{path}{query}'


def check_sitemap(target_url: str) -> Dict:
    """Main entry: run all sitemap checks against target_url."""
    checks = {}

    sitemap_urls, discovered_via = discover_sitemap_urls(target_url)

    if not sitemap_urls:
        for check_id in ('sitemap_reachable', 'target_url_in_sitemap',
                         'no_cross_domain_sitemap_entries',
                         'sampled_urls_return_200', 'lastmod_coverage',
                         'sitemap_size_compliance'):
            checks[check_id] = {
                'status': 'fail',
                'severity': 'high',
                'evidence': 'Sitemap could not be discovered via robots.txt, /sitemap.xml, or /sitemap_index.xml.'
            }
        return {
            'sitemap': {'found': False, 'discovered_via': discovered_via},
            'checks': checks
        }

    # Traverse every discovered sitemap; the seen-set, depth and
    # per-index bounds are shared (global) across all of them.
    sitemap_url = sitemap_urls[0]
    truncated = False
    if len(sitemap_urls) > MAX_SUBSITEMAPS_PER_INDEX:
        sitemap_urls = sitemap_urls[:MAX_SUBSITEMAPS_PER_INDEX]
        truncated = True
    entries = []
    errors = []
    seen = set()
    stats = {'file_url_counts': {}, 'truncated': False}
    for sm_url in sitemap_urls:
        sm_entries, sm_errors, stats = traverse_sitemap(sm_url, 0, seen, stats)
        entries.extend(sm_entries)
        errors.extend(sm_errors)
    truncated = truncated or stats['truncated']

    checks['sitemap_reachable'] = {
        'status': 'pass' if entries and not errors else
                  'warn' if entries else 'fail',
        'severity': 'high',
        'evidence': (
            f'Sitemap located via {discovered_via}: {sitemap_url}'
            + (f' (+{len(sitemap_urls) - 1} more declared)'
               if len(sitemap_urls) > 1 else '')
            + f'. {len(entries)} URLs parsed.'
            + (f' Warnings: {"; ".join(errors[:3])}' if errors else '')
        )
    }

    target_parsed = urllib.parse.urlparse(target_url)
    target_origin = f'{target_parsed.scheme}://{target_parsed.netloc}'

    # target_url_in_sitemap — normalize BOTH sides (scheme, leading
    # 'www.', trailing slash) so https://www.x.com/ matches https://x.com/
    target_norm = normalize_url_for_compare(target_url)
    matching = next(
        (e for e in entries if normalize_url_for_compare(e['loc']) == target_norm),
        None
    )
    if matching:
        evidence = (
            f'Target URL {target_url} found in sitemap'
            + (' (normalized (trailing slash, www, or protocol differs))'
               if matching['loc'] != target_url else '')
            + (f'. lastmod: {matching["lastmod"]}' if matching['lastmod'] else '')
        )
        checks['target_url_in_sitemap'] = {
            'status': 'pass', 'severity': 'high', 'evidence': evidence
        }
    elif truncated:
        checks['target_url_in_sitemap'] = {
            'status': 'warn', 'severity': 'high',
            'evidence': f'Target URL {target_url} not found in the {len(entries)} '
                        f'URLs traversed, but the search was truncated '
                        f'(sub-sitemap/depth bounds hit) — the URL may be in an '
                        f'untraversed sitemap.'
        }
    else:
        checks['target_url_in_sitemap'] = {
            'status': 'fail', 'severity': 'high',
            'evidence': f'Target URL {target_url} not found in sitemap ({len(entries)} URLs checked).'
        }

    # no_cross_domain_sitemap_entries
    cross_domain = []
    for entry in entries[:500]:  # sample for performance
        p = urllib.parse.urlparse(entry['loc'])
        entry_origin = f'{p.scheme}://{p.netloc}'
        if entry_origin != target_origin:
            cross_domain.append(entry['loc'])
    checks['no_cross_domain_sitemap_entries'] = {
        'status': 'pass' if not cross_domain else 'warn',
        'severity': 'medium',
        'evidence': (
            f'All sitemap entries point to the expected origin ({target_origin}).'
            if not cross_domain
            else f'{len(cross_domain)} URLs point to different origins. '
                 f'Examples: {cross_domain[:3]}'
        )
    }

    # sampled_urls_return_200 — uses HEAD-then-GET-fallback.
    # Graded by PROPORTION of reachable sampled URLs, not a single failure: a
    # lone dead URL (a stale sitemap entry) in an otherwise-healthy sample is
    # a warning, not a whole-check fail. 403s are treated as "blocked"
    # (bot-challenge), which keep warn semantics and are excluded from the
    # reachable count without counting as dead.
    sample = deterministic_sample(entries, target_url, sample_size=5)
    sample_results = []
    dead = []
    blocked = []
    reachable = 0
    for entry in sample:
        code, method = probe_url(entry['loc'])
        sample_results.append({'url': entry['loc'], 'code': code, 'method': method})
        if code == 403:
            # 403 usually means a bot challenge / WAF, not a dead URL
            blocked.append((entry['loc'], code))
        elif 200 <= code < 400:
            reachable += 1
        else:
            dead.append((entry['loc'], code))

    total_sampled = len(sample)
    reachable_ratio = (reachable / total_sampled) if total_sampled else 0.0
    ratio_pct = round(reachable_ratio * 100)

    # Proportional grade. Blocked-only samples (no dead URLs) never fail —
    # a WAF challenge is inconclusive, not a broken sitemap.
    if dead:
        if reachable_ratio >= 0.9:
            status = 'pass'
        elif reachable_ratio >= 0.7:
            status = 'warn'
        else:
            status = 'fail'
    elif blocked:
        status = 'warn'
    else:
        status = 'pass'

    if dead:
        sample_evidence = (
            f'{reachable} of {total_sampled} sampled URLs reachable ({ratio_pct}%). '
            f'{len(dead)} returned an error status (outside 200-399): {dead[:3]}'
            + (f'. {len(blocked)} additionally blocked with HTTP 403 '
               f'(likely bot challenge): {blocked[:3]}' if blocked else '')
        )
    elif blocked:
        sample_evidence = (
            f'{reachable} of {total_sampled} sampled URLs reachable ({ratio_pct}%); '
            f'{len(blocked)} returned HTTP 403 — blocked (likely bot challenge), '
            f'not necessarily dead: {blocked[:3]}'
        )
    else:
        sample_evidence = (
            f'All {total_sampled} sampled URLs reachable ({ratio_pct}%) '
            f'(HTTP 200-399).'
        )
    checks['sampled_urls_return_200'] = {
        'status': status,
        'severity': 'high',
        'evidence': sample_evidence,
        'detail': {
            'reachable': reachable,
            'total_sampled': total_sampled,
            'reachable_ratio': round(reachable_ratio, 3),
            'dead_urls': dead[:10],
            'blocked_urls': blocked[:10],
            'sample_results': sample_results,
        }
    }

    # lastmod_coverage
    with_lastmod = sum(1 for e in entries if e.get('lastmod'))
    coverage = (with_lastmod / len(entries) * 100) if entries else 0
    checks['lastmod_coverage'] = {
        'status': 'pass' if coverage >= 80 else
                  'warn' if coverage >= 40 else 'fail',
        'severity': 'medium',
        'evidence': f'{with_lastmod}/{len(entries)} URLs ({coverage:.0f}%) have lastmod dates.'
    }

    # sitemap_size_compliance (Google limits: 50K URLs PER FILE, 50MB)
    oversized_files = sorted(
        url for url, count in stats['file_url_counts'].items() if count > 50_000
    )
    file_count = len(stats['file_url_counts'])
    checks['sitemap_size_compliance'] = {
        'status': 'pass' if not oversized_files else 'warn',
        'severity': 'low',
        'evidence': (
            f'No sitemap file exceeds 50,000 URLs ({file_count} file(s), '
            f'{len(entries)} URLs total; Google limit is per file).'
            if not oversized_files
            else f'{len(oversized_files)} sitemap file(s) exceed the 50,000-URL '
                 f'per-file limit: {oversized_files[:3]} — split into multiple sitemaps.'
        )
    }

    return {
        'sitemap': {
            'found': True,
            'sitemap_url': sitemap_url,
            'sitemap_urls': sitemap_urls,
            'discovered_via': discovered_via,
            'total_urls_indexed': len(entries),
            'truncated': truncated,
            'traversal_errors': errors[:5] if errors else []
        },
        'checks': checks
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'missing URL', 'usage': 'python3 check_sitemap_v2.py <URL>'}))
        sys.exit(1)

    result = check_sitemap(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
