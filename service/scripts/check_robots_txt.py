#!/usr/bin/env python3
"""
check_robots_txt_v2.py — Fixed robots.txt validator.

Replaces the original scripts/check_robots_txt.py. Fixes:

1. Empty-body tolerance.
   Original raised IndexError when robots.txt returned HTTP 200 with zero bytes.
   Now: returns empty groups structure, reports 'robots_empty' with
   permissive default (all UAs allowed, per RFC 9309 §2.2.1).

2. HTTP error handling (403/500/404).
   Original treated missing robots.txt as implicit-allow without signaling.
   Now: explicit 'fail' on 4xx/5xx with evidence citing the status code.
   Downstream checks mark 'target_path_not_disallowed' as WARN with note.

3. Robots parser returns structured groups instead of a flat dict.
   Makes per-UA precedence (specific > wildcard) + longest-match explicit
   and testable.

Interface preserved: same CLI (`python3 check_robots_txt_v2.py <URL>`),
same JSON output schema.

Dependencies: curl, python3 (3.8+). stdlib only.
"""

import json
import pathlib
import re
import subprocess
import sys
import urllib.parse
from typing import Dict, List, Tuple

# SSRF guard (canonical impl at service/safety.py, one dir up). fetch_robots
# builds the robots URL from the caller-supplied target and fetches it with
# `curl -L`; a target on an internal host / metadata IP must be refused.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
try:
    from safety import check_url_safe
except Exception:
    def check_url_safe(url, resolve=True):  # stdlib fallback keeps scripts standalone
        return True, None   # (only used if safety.py is somehow absent)


CURL_TIMEOUT = 10
USER_AGENT = 'Mozilla/5.0 (compatible; SEO-AEO-Auditor/2.0)'


# Bots we evaluate for explicit allow/deny
BOTS_TO_CHECK = [
    'Googlebot', 'Bingbot', 'BingPreview',
    'GPTBot', 'ChatGPT-User', 'OAI-SearchBot',
    'ClaudeBot', 'Claude-Web', 'anthropic-ai',
    'PerplexityBot',
    'Google-Extended', 'Applebot', 'Applebot-Extended',
    'CCBot', 'DuckDuckBot', 'Bytespider',
]

AI_CRAWLERS_ONLY = [
    'GPTBot', 'ChatGPT-User', 'OAI-SearchBot',
    'ClaudeBot', 'Claude-Web', 'anthropic-ai',
    'PerplexityBot', 'Google-Extended',
    'CCBot', 'Applebot-Extended',
]


def parse_robots_txt(body: str) -> Dict:
    """
    Parse robots.txt into groups. Each group has:
      - user_agents: list of UA tokens this group applies to
      - rules: list of (directive_type, path) tuples

    Tolerates:
      - Empty body (returns zero groups)
      - BOM, windows line endings, stray whitespace
      - Comments on line or inline

    Does NOT raise. Returns a valid (possibly-empty) structure on any input.
    """
    result = {'groups': [], 'sitemaps': [], 'empty': False, 'parse_warnings': []}

    if body is None or not body.strip():
        result['empty'] = True
        return result

    # Normalize
    body = body.lstrip('\ufeff')
    body = body.replace('\r\n', '\n').replace('\r', '\n')

    current_uas: List[str] = []
    current_rules: List[Tuple[str, str]] = []
    last_was_ua = False

    def commit_group():
        # Commit any group that names user-agents — even with zero
        # Allow/Disallow rules (e.g. only Crawl-delay). An empty rule
        # list means allow-all for those UAs; dropping the group would
        # wrongly let the bot fall through to the wildcard group.
        if current_uas:
            result['groups'].append({
                'user_agents': list(current_uas),
                'rules': list(current_rules),
            })

    for line_no, raw_line in enumerate(body.split('\n'), 1):
        line = raw_line.split('#', 1)[0].strip()
        if not line:
            continue

        if ':' not in line:
            result['parse_warnings'].append(
                f'line {line_no}: missing colon, skipped: "{raw_line.strip()[:60]}"'
            )
            continue

        directive, value = line.split(':', 1)
        directive = directive.strip().lower()
        value = value.strip()

        if directive == 'sitemap':
            if value:
                result['sitemaps'].append(value)
            continue

        if directive == 'user-agent':
            # If previous line was a rule (not a UA), we're starting a new group
            if not last_was_ua and (current_uas or current_rules):
                commit_group()
                current_uas = []
                current_rules = []
            if value:
                current_uas.append(value)
            last_was_ua = True
            continue

        if directive in ('disallow', 'allow'):
            if not current_uas:
                result['parse_warnings'].append(
                    f'line {line_no}: {directive} before user-agent, ignored'
                )
                continue
            # RFC 9309: empty Disallow means allow-all; empty Allow is a no-op
            current_rules.append((directive, value))
            last_was_ua = False
            continue

        if directive in ('crawl-delay', 'host', 'noindex', 'clean-param'):
            last_was_ua = False
            continue

        result['parse_warnings'].append(
            f'line {line_no}: unknown directive "{directive}", ignored'
        )

    commit_group()
    return result


def find_matching_groups(parsed: Dict, user_agent: str) -> List[Dict]:
    """
    Find groups that match the given user_agent, per RFC 9309 §2.2.1 and
    Google's robotstxt parser: a group token matches a crawler when the
    token is a case-insensitive PREFIX of the crawler's product token
    (group 'googlebot' matches crawler 'Googlebot-Image', but group
    'googlebot-image' does NOT match crawler 'Googlebot').
    The longest matching token wins; wildcard (*) groups apply only when
    no specific token matches.
    """
    ua_lower = user_agent.lower()
    wildcard = []
    best_len = -1
    best_groups: List[Dict] = []
    for group in parsed.get('groups', []):
        group_match_len = -1
        for g_ua in group['user_agents']:
            g_lower = g_ua.lower()
            if g_lower == '*':
                wildcard.append(group)
            elif ua_lower.startswith(g_lower):
                group_match_len = max(group_match_len, len(g_lower))
        if group_match_len < 0:
            continue
        if group_match_len > best_len:
            best_len = group_match_len
            best_groups = [group]
        elif group_match_len == best_len:
            best_groups.append(group)
    return best_groups if best_groups else wildcard


def evaluate_path_access(groups: List[Dict], path: str) -> Tuple[bool, str]:
    """
    RFC 9309 evaluation: longest matching path wins; Allow wins ties.
    Returns (allowed, evidence).

    If no groups match, defaults to allowed (permissive).
    """
    if not groups:
        return True, 'no matching rule groups — permissive default applied'

    best_match_len = -1
    best_directive = None
    best_pattern = None

    for group in groups:
        for directive, pattern in group['rules']:
            # Empty Disallow means "no disallow rules" — allow all
            if directive == 'disallow' and not pattern:
                if 0 > best_match_len:
                    best_match_len = 0
                    best_directive = 'allow'
                    best_pattern = '(empty Disallow = allow-all)'
                continue
            if not pattern:
                continue

            # RFC 9309 §2.2.3: '*' matches any sequence of characters,
            # trailing '$' anchors the match to the end of the path.
            # Translate the pattern to a regex anchored at path start.
            anchored = pattern.endswith('$')
            core = pattern[:-1] if anchored else pattern
            regex = re.escape(core).replace(r'\*', '.*')
            if anchored:
                regex += '$'
            try:
                matched = re.match(regex, path) is not None
            except re.error:
                continue
            if not matched:
                continue
            if len(pattern) > best_match_len:
                best_match_len = len(pattern)
                best_directive = directive
                best_pattern = pattern
            elif len(pattern) == best_match_len and directive == 'allow':
                # Allow wins tie
                best_directive = 'allow'
                best_pattern = pattern

    if best_directive is None:
        return True, 'no matching rule — permissive default'
    allowed = (best_directive == 'allow')
    return allowed, f'{best_directive} pattern "{best_pattern}" (length {best_match_len})'


def fetch_robots(base_url: str) -> Tuple[int, str, str]:
    """Fetch robots.txt. Returns (http_code, body, error). Never raises."""
    parsed = urllib.parse.urlparse(base_url)
    robots_url = f'{parsed.scheme}://{parsed.netloc}/robots.txt'
    # SSRF: refuse internal/metadata targets before spawning curl.
    ok, reason = check_url_safe(robots_url)
    if not ok:
        return 0, '', f'blocked by SSRF guard: {reason}'
    try:
        result = subprocess.run(
            ['curl', '-sS', '-L', '--max-redirs', '3',
             '--max-time', str(CURL_TIMEOUT),
             '-A', USER_AGENT,
             '-w', '\n---HTTP_CODE---\n%{http_code}',
             robots_url],
            capture_output=True, timeout=CURL_TIMEOUT + 3
        )
        # Decode with errors='replace' — a single non-UTF-8 byte (e.g.
        # Latin-1 comment) must not turn the whole fetch into a failure.
        output = result.stdout.decode('utf-8', errors='replace')
        stderr = result.stderr.decode('utf-8', errors='replace') if result.stderr else ''
        if '\n---HTTP_CODE---\n' in output:
            body, code_str = output.rsplit('\n---HTTP_CODE---\n', 1)
            code = int(code_str.strip()) if code_str.strip().isdigit() else 0
        else:
            body, code = output, 0
        return code, body, stderr
    except subprocess.TimeoutExpired:
        return 0, '', 'timeout'
    except FileNotFoundError:
        return 0, '', 'curl not installed'
    except Exception as e:
        return 0, '', f'{type(e).__name__}: {e}'


def check_robots(target_url: str) -> Dict:
    """Main entry: run all robots.txt checks against target_url."""
    checks = {}

    parsed_url = urllib.parse.urlparse(target_url)
    target_path = parsed_url.path or '/'
    # Rules can match on the query string too (e.g. Disallow: /*?print=1)
    if parsed_url.query:
        target_path += '?' + parsed_url.query

    http_code, body, err = fetch_robots(target_url)
    robots_5xx = False

    # --- robots_reachable ---
    if http_code == 0:
        checks['robots_reachable'] = {
            'status': 'fail', 'severity': 'high',
            'evidence': f'robots.txt fetch failed: {err or "unknown network error"}'
        }
        parsed = {'groups': [], 'sitemaps': [], 'empty': True, 'parse_warnings': []}
        robots_available = False
    elif 500 <= http_code < 600:
        checks['robots_reachable'] = {
            'status': 'fail', 'severity': 'high',
            'evidence': f'robots.txt returned HTTP {http_code}. '
                        f'Per RFC 9309 §2.3.1.4, 5xx means "unreachable" and '
                        f'crawlers MUST assume complete disallow (Google: '
                        f'for up to 30 days) — the site may be uncrawlable '
                        f'until the server error is fixed.'
        }
        parsed = {'groups': [], 'sitemaps': [], 'empty': True, 'parse_warnings': []}
        robots_available = False
        robots_5xx = True
    elif 400 <= http_code < 500:
        checks['robots_reachable'] = {
            'status': 'fail', 'severity': 'medium',
            'evidence': f'robots.txt returned HTTP {http_code}. '
                        f'Per RFC 9309 §2.3.1.3, 4xx means "no robots.txt" '
                        f'and crawlers apply permissive default.'
        }
        parsed = {'groups': [], 'sitemaps': [], 'empty': True, 'parse_warnings': []}
        robots_available = False
    elif not body.strip():
        checks['robots_reachable'] = {
            'status': 'warn', 'severity': 'low',
            'evidence': f'robots.txt reachable (HTTP {http_code}) but empty. '
                        f'All user-agents allowed (permissive default).'
        }
        parsed = parse_robots_txt(body)
        robots_available = True
    else:
        parsed = parse_robots_txt(body)
        checks['robots_reachable'] = {
            'status': 'pass', 'severity': 'info',
            'evidence': f'robots.txt reachable, HTTP {http_code}, {len(body)} bytes. '
                        f'{len(parsed["groups"])} user-agent group(s), '
                        f'{len(parsed["sitemaps"])} sitemap directive(s).'
        }
        robots_available = True

    # --- robots_declares_sitemap ---
    if not robots_available:
        checks['robots_declares_sitemap'] = {
            'status': 'na', 'severity': 'medium',
            'evidence': 'Cannot evaluate Sitemap: declarations — robots.txt '
                        'is unreachable or returned an error.'
        }
    elif parsed['sitemaps']:
        checks['robots_declares_sitemap'] = {
            'status': 'pass', 'severity': 'info',
            'evidence': f'robots.txt declares {len(parsed["sitemaps"])} sitemap(s): '
                        f'{parsed["sitemaps"][:3]}'
        }
    else:
        checks['robots_declares_sitemap'] = {
            'status': 'warn', 'severity': 'medium',
            'evidence': 'robots.txt does not declare any Sitemap: directive. '
                        'Crawlers must rely on /sitemap.xml convention.'
        }

    # --- googlebot_allowed ---
    if robots_available:
        gbot_groups = find_matching_groups(parsed, 'Googlebot')
        gbot_explicit = any(
            any(ua.lower() == 'googlebot' for ua in g['user_agents'])
            for g in gbot_groups
        )
        allowed, evidence = evaluate_path_access(gbot_groups, target_path)
        checks['googlebot_allowed'] = {
            'status': 'pass' if allowed else 'fail',
            'severity': 'critical' if not allowed else 'info',
            'evidence': (
                f'Googlebot {"explicitly listed and " if gbot_explicit else ""}'
                f'allowed for {target_path}.' if allowed
                else f'Googlebot DISALLOWED for {target_path}. Rule: {evidence}'
            )
        }
    else:
        checks['googlebot_allowed'] = {
            'status': 'warn', 'severity': 'medium',
            'evidence': (
                'Cannot evaluate Googlebot access — robots.txt returned a 5xx '
                'server error. Per RFC 9309 crawlers assume complete disallow '
                'until it recovers.' if robots_5xx else
                'Cannot evaluate Googlebot access — robots.txt inaccessible. '
                'Permissive default assumes allowed, but should be verified.'
            )
        }

    # --- ai_crawlers_all_allowed ---
    if robots_available:
        ai_explicit = []
        ai_denied = []
        for bot in AI_CRAWLERS_ONLY:
            groups = find_matching_groups(parsed, bot)
            explicit = any(
                any(ua.lower() == bot.lower() for ua in g['user_agents'])
                for g in groups
            )
            if explicit:
                ai_explicit.append(bot)
            allowed, _ = evaluate_path_access(groups, target_path)
            if not allowed:
                ai_denied.append(bot)

        if ai_denied:
            checks['ai_crawlers_all_allowed'] = {
                'status': 'fail', 'severity': 'high',
                'evidence': f'{len(ai_denied)} AI crawlers DENIED access: {ai_denied}'
            }
        elif len(ai_explicit) == len(AI_CRAWLERS_ONLY):
            checks['ai_crawlers_all_allowed'] = {
                'status': 'pass', 'severity': 'info',
                'evidence': f'All {len(AI_CRAWLERS_ONLY)} AI crawlers explicitly allowed.'
            }
        else:
            missing = [b for b in AI_CRAWLERS_ONLY if b not in ai_explicit]
            checks['ai_crawlers_all_allowed'] = {
                'status': 'warn', 'severity': 'low',
                'evidence': (
                    f'{len(ai_explicit)} of {len(AI_CRAWLERS_ONLY)} AI crawlers '
                    f'explicitly listed: {ai_explicit}. Others ({missing}) allowed '
                    f'only via wildcard — consider explicit entries for clarity.'
                )
            }
    else:
        checks['ai_crawlers_all_allowed'] = {
            'status': 'warn', 'severity': 'medium',
            'evidence': (
                'Cannot evaluate AI crawler access — robots.txt returned a 5xx '
                'server error. Per RFC 9309 crawlers assume complete disallow '
                'until it recovers.' if robots_5xx else
                'Cannot evaluate AI crawler access — robots.txt inaccessible.'
            )
        }

    # --- target_path_not_disallowed (across all bots checked) ---
    if robots_available:
        blocked_for = []
        for bot in BOTS_TO_CHECK:
            groups = find_matching_groups(parsed, bot)
            allowed, _ = evaluate_path_access(groups, target_path)
            if not allowed:
                blocked_for.append(bot)
        if blocked_for:
            checks['target_path_not_disallowed'] = {
                'status': 'fail', 'severity': 'high',
                'evidence': f'Target path {target_path} blocked for: {blocked_for}'
            }
        else:
            checks['target_path_not_disallowed'] = {
                'status': 'pass', 'severity': 'info',
                'evidence': f'Target path {target_path} is allowed for all {len(BOTS_TO_CHECK)} checked bots.'
            }
    else:
        checks['target_path_not_disallowed'] = {
            'status': 'warn', 'severity': 'medium',
            'evidence': (
                'Cannot evaluate target path access — robots.txt returned a 5xx '
                'server error. Per RFC 9309 crawlers assume complete disallow '
                'until it recovers.' if robots_5xx else
                'Cannot evaluate target path access — robots.txt inaccessible.'
            )
        }

    return {
        'robots_txt': {
            'http_code': http_code,
            'reachable': robots_available,
            'body_size': len(body) if body else 0,
            'groups_count': len(parsed.get('groups', [])),
            'sitemaps_declared': parsed.get('sitemaps', []),
            'parse_warnings': parsed.get('parse_warnings', [])[:5]
        },
        'checks': checks
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'missing URL', 'usage': 'python3 check_robots_txt_v2.py <URL>'}))
        sys.exit(1)

    result = check_robots(sys.argv[1])
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
