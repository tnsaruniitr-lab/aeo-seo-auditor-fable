#!/usr/bin/env python3
"""
deterministic_checks.py — Phase 2 targeted deterministic checks

The 8 checks below are the ones where Claude produced wrong answers in past audits.
Each is a pure function: same HTML input → same output. No LLM in the loop.

Usage:
  bash scripts/run_all.sh <URL>     (invoked via the orchestrator)
  python3 scripts/deterministic_checks.py <URL>    (standalone)

Output: JSON to stdout with results for all 8 checks.

Checks implemented:
  D9.  faqpage_schema_vs_visible_match    — Catches the Valeo-style mismatch (Claude missed this with bad grep)
  A7b. h1_nested_in_heading_invalid       — Catches H1 inside H2 (Valeo weight loss)
  J2.  brand_name_consistency             — Flags mixed casing / character substitution (Weg0vy vs Wegovy)
  A4b. canonical_redirect_chain           — Flags canonical pointing to a redirected URL (Valeo trailing slash)
  B1.  ttfb_median_5_samples              — 5-sample TTFB instead of 1 (Valeo variance problem)
  D4.  schema_id_coverage                 — Every schema entity should have @id
  C12b. date_modified_is_stale            — Flags dateModified that hasn't changed despite other edits (AnswerMonk stale stamp)
  A2b. title_uniqueness_sample            — Samples 3 URLs from sitemap, checks titles differ (catches SPA placeholder titles)

Each check returns:
  {
    "status": "pass" | "fail" | "warn" | "na",
    "evidence": "<exact bytes or measurement>",
    "detail": { ... structured data for the narrative report ... }
  }
"""

import sys
import re
import json
import os
import pathlib
import subprocess
import time
import urllib.request
import urllib.parse
from urllib.error import URLError, HTTPError
from html import unescape as html_unescape

# SSRF guard. The canonical implementation lives at service/safety.py (one
# directory up). A public URL can 30x-redirect to an internal host or the
# cloud-metadata endpoint (169.254.169.254); fetch() validates the initial
# URL and every redirect hop against this before reading any body.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
try:
    from safety import check_url_safe
except Exception:
    def check_url_safe(url, resolve=True):  # stdlib fallback keeps scripts standalone
        return True, None   # (only used if safety.py is somehow absent)

# Share the question-intent-gated FAQ detector with the BEV layer so Phase 2
# counts the same thing Phase 1 does. Prior code had a duplicate pattern list
# that counted every <details>/<summary> pair, including country accordions.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
from _bev_analyze import faq_visible_count as _faq_visible_count  # noqa: E402
from _bev_analyze import visible_text as _visible_text  # noqa: E402
from _bev_analyze import _norm_for_match  # noqa: E402
# Hreflang detector that also walks Next.js streaming chunks (RSC payloads).
# Prior to wiring this in, App Router sites with locales declared inside
# self.__next_f.push(...) chunks reported zero hreflang in the audit output.
from deterministic_checks_extras import detect_hreflang as _detect_hreflang  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────
# Utilities
# ──────────────────────────────────────────────────────────────────────────

USER_AGENT = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (AuditBot)'

# Fixed slug for the guaranteed-404 probe. A timestamped slug made the same
# input produce different JSON on every run (and different probe URLs in
# evidence), breaking reproducibility.
NONEXISTENT_PROBE_SLUG = 'auditbot-nonexistent-probe-3f9c2a'


def decode_body(raw, headers):
    """Decode a response body. Prefer the Content-Type charset, then a
    <meta charset> sniff, then UTF-8. Always errors='replace' so a bad
    declaration can't crash the fetch."""
    charset = None
    if headers is not None:
        try:
            charset = headers.get_content_charset()
        except AttributeError:
            charset = None
    if not charset:
        m = re.search(
            rb'<meta[^>]+charset=["\']?\s*([a-zA-Z0-9_\-]+)',
            raw[:4096], re.IGNORECASE
        )
        if m:
            charset = m.group(1).decode('ascii', errors='replace')
    for cs in (charset, 'utf-8'):
        if not cs:
            continue
        try:
            return raw.decode(cs, errors='replace')
        except (LookupError, ValueError):
            continue
    return raw.decode('utf-8', errors='replace')


class _BlockedRedirect(Exception):
    """Raised inside a redirect handler when a hop fails the SSRF guard.

    A distinct type (not HTTPError) so fetch() returns the documented
    ('', final_url, 0, {}, redirect_chain) contract with status 0 — the hop
    was refused, no body was read, so there is no real HTTP status to report.
    """
    def __init__(self, newurl, reason):
        super().__init__(reason)
        self.newurl = newurl
        self.reason = reason


def fetch(url, timeout=15, allow_redirects=True, user_agent=USER_AGENT):
    """Fetch URL with controlled behavior. Returns (html, final_url, status, headers, redirect_chain)."""
    redirect_chain = []

    # SSRF pre-flight: refuse the initial URL if it is not safe to fetch
    # server-side (internal host, metadata IP, non-http scheme, credentials).
    ok, reason = check_url_safe(url)
    if not ok:
        redirect_chain.append({'from': url, 'to': url, 'status': 0,
                               'blocked': True, 'note': f'SSRF guard: {reason}'})
        return '', url, 0, {}, redirect_chain

    req = urllib.request.Request(url, headers={'User-Agent': user_agent})
    try:
        # Build opener that records redirects.
        # http_error_308 alias: urllib only learned to follow 308 Permanent
        # Redirect in Python 3.11; without this, every 308 (e.g. http→https
        # on Vercel/Framer hosts) raises HTTPError and zero checks run.
        class RecordingHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                # SSRF: validate EVERY redirect hop before following it. A
                # public URL can 30x to http://169.254.169.254/ or an RFC1918
                # host; without this the body would be read back to the caller.
                ok, reason = check_url_safe(newurl)
                if not ok:
                    redirect_chain.append({
                        'from': req.full_url, 'to': newurl, 'status': code,
                        'blocked': True, 'note': f'SSRF guard: {reason}',
                    })
                    # Refuse the hop. _BlockedRedirect propagates out of
                    # opener.open() to fetch()'s except clause below, which
                    # returns ('', final_url, 0, {}, redirect_chain) — the
                    # blocked hop is the last redirect_chain entry.
                    raise _BlockedRedirect(newurl, reason)
                redirect_chain.append({'from': req.full_url, 'to': newurl, 'status': code})
                # Base redirect_request has a hardcoded (301,302,303,307)
                # allowlist on Python < 3.11 — present 308 as 307 to it.
                return super().redirect_request(
                    req, fp, 307 if code == 308 else code, msg, headers, newurl)
            http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307

        if allow_redirects:
            opener = urllib.request.build_opener(RecordingHandler())
        else:
            # No redirects — catch the redirect response
            class NoFollowHandler(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
                http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307
            opener = urllib.request.build_opener(NoFollowHandler())

        resp = opener.open(req, timeout=timeout)
        html = decode_body(resp.read(), resp.headers)
        return html, resp.url, resp.status, dict(resp.headers), redirect_chain
    except _BlockedRedirect:
        # SSRF-blocked hop: no body, status 0. The blocked hop is already the
        # last redirect_chain entry (final_url points at the refused target).
        final_url = redirect_chain[-1]['to'] if redirect_chain else url
        return '', final_url, 0, {}, redirect_chain
    except HTTPError as e:
        try:
            body = decode_body(e.read(), e.headers)
        except Exception:
            body = ''
        # The error may have occurred after one or more redirects — report
        # the last hop of the chain as final_url, not the original URL.
        final_url = redirect_chain[-1]['to'] if redirect_chain else url
        return body, final_url, e.code, dict(e.headers) if e.headers else {}, redirect_chain
    except Exception as e:
        # urllib may wrap the raised _BlockedRedirect in a URLError; unwrap so
        # the blocked hop still yields the status-0 SSRF contract.
        if isinstance(getattr(e, 'reason', None), _BlockedRedirect):
            final_url = redirect_chain[-1]['to'] if redirect_chain else url
            return '', final_url, 0, {}, redirect_chain
        final_url = redirect_chain[-1]['to'] if redirect_chain else url
        return '', final_url, 0, {}, redirect_chain


def strip_tags(html):
    if not html:
        return ''
    c = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.DOTALL | re.IGNORECASE)
    c = re.sub(r'<style[^>]*>.*?</style>', ' ', c, flags=re.DOTALL | re.IGNORECASE)
    c = re.sub(r'<!--.*?-->', ' ', c, flags=re.DOTALL)
    t = re.sub(r'<[^>]+>', ' ', c)
    # Decode entities instead of deleting them: "AT&amp;T" must become
    # "AT&T", not "AT T".
    t = html_unescape(t)
    return re.sub(r'\s+', ' ', t).strip()


def extract_jsonld_blocks(html):
    """Return the raw inner text of every <script type="application/ld+json">
    block. Single shared pattern for all JSON-LD consumers — D9 previously
    used a stricter regex than the other checks and could disagree with them
    on the same page."""
    return [
        m.group(1)
        for m in re.finditer(
            r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
            html, re.IGNORECASE | re.DOTALL
        )
    ]


def term_pattern(term):
    """Whole-word regex for a term. Plain \\b never matches next to a
    non-word character, so the boundary is only applied where the term's
    edge is a word character (e.g. brands ending in '!' or '+')."""
    prefix = r'\b' if re.match(r'\w', term) else ''
    suffix = r'\b' if re.search(r'\w$', term) else ''
    return prefix + re.escape(term) + suffix


# ──────────────────────────────────────────────────────────────────────────
# CHECK D9: FAQPage schema vs visible match
# ──────────────────────────────────────────────────────────────────────────

def check_d9_faq_schema_match(html):
    """
    Count visible FAQ pairs using ALL detection patterns (not just <details>/<summary>).
    Compare to FAQPage schema mainEntity count.
    Returns pass only if they match, fail if there's a mismatch.
    """
    # Use the shared, question-intent-gated detector. Counts only
    # <details>/<summary> pairs whose summary text actually looks like a
    # question, plus the other accordion/FAQ-class signals. Returns
    # ('none_detected' | 'empty_html' | <pattern_name>).
    visible_count, detection_method = _faq_visible_count(html)

    # FAQPage schema count
    faq_schema_count = 0
    schema_questions = []
    for b in extract_jsonld_blocks(html):
        try:
            data = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            candidates = [item]
            if isinstance(item.get('@graph'), list):
                candidates.extend(item['@graph'])
            for c in candidates:
                if not isinstance(c, dict):
                    continue
                # '@type' may be a string or a list of types
                types = c.get('@type')
                types = types if isinstance(types, list) else [types]
                if 'FAQPage' not in types:
                    continue
                # mainEntity may be a single dict instead of a list
                me = c.get('mainEntity', [])
                if isinstance(me, dict):
                    me = [me]
                if isinstance(me, list):
                    # Accumulate across multiple FAQPage blocks instead of
                    # letting the last block overwrite earlier counts.
                    faq_schema_count += len(me)
                    schema_questions.extend(q.get('name', '') for q in me if isinstance(q, dict))

    # Determine status
    if visible_count == 0 and faq_schema_count == 0:
        return {
            'status': 'na',
            'evidence': 'No FAQ content detected on the page in schema or visible HTML.',
            'detail': {
                'visible_count': 0,
                'schema_count': 0,
                'detection_method': 'none',
            }
        }

    if visible_count == faq_schema_count and visible_count > 0:
        # Counts match — but equal COUNTS do not prove the visible questions
        # are the SAME questions the schema marks up (the Valeo-style case:
        # 6 visible Q&As, 6 in schema, but different text). Google's policy
        # requires the marked-up questions to be the ones displayed, so verify
        # the schema question texts actually appear in the visible HTML.
        #
        # Conservative: only downgrade a count-match to 'warn' when the schema
        # question texts are extractable. If we can't extract them, we can't
        # reliably prove divergence, so we keep the historical pass (no new
        # false negatives).
        extractable_qs = [q for q in schema_questions
                          if isinstance(q, str) and _norm_for_match(q)]
        if extractable_qs:
            vis_norm = _norm_for_match(_visible_text(html, max_chars=300_000))
            matched_qs = [q for q in extractable_qs
                          if _norm_for_match(q) in vis_norm]
            match_count = len(matched_qs)
            total_qs = len(extractable_qs)
            missing_qs = [q for q in extractable_qs
                          if _norm_for_match(q) not in vis_norm]
            # Exact-or-high overlap → the counts genuinely correspond.
            if match_count == total_qs:
                return {
                    'status': 'pass',
                    'evidence': (
                        f'{visible_count} visible FAQ pairs match {faq_schema_count} '
                        f'in FAQPage schema; all {total_qs} schema question texts '
                        f'found in visible HTML.'),
                    'detail': {
                        'visible_count': visible_count,
                        'schema_count': faq_schema_count,
                        'detection_method': detection_method,
                        'schema_questions': schema_questions,
                        'schema_questions_matched_in_visible': match_count,
                        'schema_questions_total': total_qs,
                    }
                }
            # Counts equal but the question SETS diverge — the mismatch the
            # count-only check used to miss. Warn (not fail: counts still line
            # up, and visible-text extraction has known blind spots).
            return {
                'status': 'warn',
                'evidence': (
                    f'FAQ counts match ({visible_count} visible vs {faq_schema_count} '
                    f'schema) but the question sets diverge: only {match_count} of '
                    f'{total_qs} schema question texts appear in the visible HTML. '
                    f'Missing from visible: {missing_qs[:3]}'),
                'detail': {
                    'visible_count': visible_count,
                    'schema_count': faq_schema_count,
                    'detection_method': detection_method,
                    'schema_questions': schema_questions,
                    'schema_questions_matched_in_visible': match_count,
                    'schema_questions_total': total_qs,
                    'schema_questions_missing_from_visible': missing_qs[:10],
                    'mismatch_signal': 'partial_text_match',
                }
            }
        # Schema question texts not extractable — keep the historical
        # count-based pass to avoid introducing false negatives.
        return {
            'status': 'pass',
            'evidence': f'{visible_count} visible FAQ pairs match {faq_schema_count} in FAQPage schema.',
            'detail': {
                'visible_count': visible_count,
                'schema_count': faq_schema_count,
                'detection_method': detection_method,
                'schema_questions': schema_questions,
            }
        }

    # Ground-truth fallback before declaring a mismatch: widget-pattern
    # detection misses builders (Framer, custom markup) whose FAQ text is
    # plainly visible. Google's policy tests whether the marked-up text is
    # displayed — so check the schema questions against the visible text.
    questions_visible = 0
    if faq_schema_count > visible_count and schema_questions:
        vis_norm = _norm_for_match(_visible_text(html, max_chars=300_000))
        questions_visible = sum(
            1 for q in schema_questions
            if isinstance(q, str) and _norm_for_match(q)
            and _norm_for_match(q) in vis_norm
        )

    if faq_schema_count > 0 and questions_visible >= faq_schema_count:
        return {
            'status': 'pass',
            'evidence': (
                f'All {faq_schema_count} FAQPage schema questions found in visible '
                f'HTML text (no FAQ widget pattern matched — text-match fallback).'),
            'detail': {
                'visible_count': visible_count,
                'schema_count': faq_schema_count,
                'schema_questions_visible': questions_visible,
                'detection_method': 'schema_question_text_match',
                'schema_questions': schema_questions,
            }
        }

    if faq_schema_count > 0 and questions_visible >= (faq_schema_count + 1) // 2:
        return {
            'status': 'warn',
            'evidence': (
                f'{questions_visible} of {faq_schema_count} FAQPage schema questions '
                f'found in visible HTML text; the rest are in schema only.'),
            'detail': {
                'visible_count': visible_count,
                'schema_count': faq_schema_count,
                'schema_questions_visible': questions_visible,
                'detection_method': 'schema_question_text_match',
                'schema_questions': schema_questions,
            }
        }

    # Mismatch
    direction = 'schema has more than visible' if faq_schema_count > visible_count else 'visible has more than schema'
    return {
        'status': 'fail',
        'evidence': f'FAQPage schema claims {faq_schema_count} Q&A pairs; visible HTML has {visible_count} (detected via {detection_method}). {direction}.',
        'detail': {
            'visible_count': visible_count,
            'schema_count': faq_schema_count,
            'schema_questions_visible': questions_visible,
            'detection_method': detection_method,
            'mismatch_direction': direction,
            'schema_questions': schema_questions,
        }
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK A7b: H1 nested inside another heading (invalid HTML)
# ──────────────────────────────────────────────────────────────────────────

def check_a7b_h1_nesting(html):
    """
    Detect H1 tags nested inside H2/H3/H4 elements.
    This is invalid HTML per W3C.
    Catches the Valeo "Frequently asked questions" case.
    """
    violations = []
    for parent_tag in ['h2', 'h3', 'h4', 'h5', 'h6']:
        # Find <hN>...</hN> blocks and check for <h1> inside
        for m in re.finditer(
            rf'<{parent_tag}[^>]*>(.*?)</{parent_tag}>',
            html, re.IGNORECASE | re.DOTALL
        ):
            inner = m.group(1)
            h1_open = re.search(r'<h1[^>]*>', inner, re.IGNORECASE)
            if h1_open:
                # HTML auto-close semantics: a heading start tag implicitly
                # closes any open heading. If another heading starts between
                # the parent's open tag and the <h1>, the parent was already
                # closed (unclosed <hN> spanning to a later </hN>) and the H1
                # is not actually nested — don't flag it.
                before_h1 = inner[:h1_open.start()]
                if re.search(r'<h[1-6][\s>/]', before_h1, re.IGNORECASE):
                    continue
                # Extract the H1 content for evidence
                h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', inner, re.IGNORECASE | re.DOTALL)
                h1_text = ''
                if h1_match:
                    h1_text = re.sub(r'<[^>]*>', ' ', h1_match.group(1))
                    h1_text = re.sub(r'\s+', ' ', h1_text).strip()[:80]
                violations.append({
                    'parent_tag': parent_tag,
                    'parent_position': m.start(),
                    'h1_text': h1_text,
                })

    if not violations:
        return {
            'status': 'pass',
            'evidence': 'No H1 tags nested inside other heading elements.',
            'detail': {'violations': []}
        }

    parent_tags_list = ", ".join([f"h1 in {v['parent_tag']}" for v in violations])
    return {
        'status': 'fail',
        'evidence': f'Found {len(violations)} H1 tag(s) invalidly nested inside heading elements: {parent_tags_list}.',
        'detail': {'violations': violations}
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK J2: Brand name consistency (catches character substitution / mixed casing)
# ──────────────────────────────────────────────────────────────────────────

def check_j2_brand_name_consistency(html, brand_name=None):
    """
    Detect when the same brand name has mixed variants on the page.
    Catches Weg0vy vs Wegovy (zero substituted for o).

    If brand_name is None, auto-detect from <title> or Organization schema.
    Scans for common character substitutions: o→0, l→1, i→1/l, e→3, a→4, s→5, b→6, z→2.
    """
    # Auto-detect brand name from Organization schema
    candidates = set()
    if brand_name:
        candidates.add(brand_name)
    else:
        for b in extract_jsonld_blocks(html):
            try:
                data = json.loads(b.strip())
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                _t = item.get('@type') if isinstance(item, dict) else None
                _types = _t if isinstance(_t, list) else [_t]
                if isinstance(item, dict) and any(
                        x in ('Organization', 'MedicalBusiness', 'LocalBusiness', 'SoftwareApplication')
                        for x in _types if isinstance(x, str)):
                    n = item.get('name')
                    # name can be a non-string JSON-LD value like
                    # {"@value": "Acme"} — only strings are usable here
                    if isinstance(n, str) and len(n) >= 4:
                        candidates.add(n)

    # Also look for common product/drug names that get obfuscated in this industry
    # These are well-known GLP-1 weight-loss drugs and other medical products commonly obfuscated
    known_terms = ['Wegovy', 'Mounjaro', 'Ozempic', 'Rybelsus', 'Saxenda', 'Trulicity']

    # Substitution map (lowercase)
    subst_map = {'o': '0', 'l': '1', 'i': '1', 'e': '3', 'a': '4', 's': '5', 'b': '6', 'z': '2'}

    # Scan tag-stripped visible text, not raw HTML — minified JS, nonces and
    # asset hashes produce accidental hits that are not real page content.
    visible_text = strip_tags(html)

    mixed_variants = []
    # sorted() so set iteration order can't reorder the output between runs
    for term in sorted(candidates) + known_terms:
        if not term or len(term) < 4:
            continue
        # Real count
        real_count = len(re.findall(term_pattern(term), visible_text, re.IGNORECASE))
        # Generate substituted variants and count them
        for idx, ch in enumerate(term.lower()):
            if ch in subst_map:
                substituted = term[:idx] + subst_map[ch] + term[idx+1:]
                sub_count = len(re.findall(term_pattern(substituted), visible_text, re.IGNORECASE))
                # Require corroboration before failing: a single substituted
                # hit with the real spelling absent is more likely noise.
                if sub_count >= 2 or (sub_count > 0 and real_count > 0):
                    mixed_variants.append({
                        'real': term,
                        'real_count': real_count,
                        'substituted': substituted,
                        'substituted_count': sub_count,
                    })

    if not mixed_variants:
        return {
            'status': 'pass',
            'evidence': 'No character-substitution variants of brand/product names detected.',
            'detail': {'variants_found': []}
        }

    # Each variant found is an issue
    return {
        'status': 'fail',
        'evidence': f'Found {len(mixed_variants)} brand/product name with mixed spelling: ' +
                    '; '.join([f'{v["real"]} ({v["real_count"]}x) vs {v["substituted"]} ({v["substituted_count"]}x)' for v in mixed_variants]),
        'detail': {'variants_found': mixed_variants}
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK A4b: Canonical URL redirect chain
# ──────────────────────────────────────────────────────────────────────────

def check_a4b_canonical_redirect_chain(html, current_url):
    """
    Extract the canonical URL from the HTML. If it differs from current_url,
    fetch the canonical URL without following redirects and see if it 3xx's.

    Flags the Valeo-style case: page at /foo, canonical says /foo/, which 308s to /foo.
    """
    # Extract canonical: walk <link> tags and read rel/href attributes
    # order- and quote-agnostically (rel after href, single/double/no quotes)
    attr_pat = r'''(?<![\w-]){name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))'''
    canonical = None
    for tag in re.finditer(r'<link\b[^>]*>', html, re.IGNORECASE):
        tag_html = tag.group(0)
        rel_m = re.search(attr_pat.format(name='rel'), tag_html, re.IGNORECASE)
        href_m = re.search(attr_pat.format(name='href'), tag_html, re.IGNORECASE)
        if not rel_m or not href_m:
            continue
        rel_val = next(g for g in rel_m.groups() if g is not None)
        if 'canonical' not in rel_val.lower().split():
            continue
        canonical = html_unescape(next(g for g in href_m.groups() if g is not None).strip())
        break

    if canonical is None:
        return {
            'status': 'warn',
            'evidence': 'No canonical tag found on the page.',
            'detail': {}
        }
    if not canonical:
        return {
            'status': 'warn',
            'evidence': 'Canonical tag present but its href is empty.',
            'detail': {'canonical_url': '', 'current_url': current_url}
        }

    # Resolve relative canonicals (e.g. '/pricing') against the served URL,
    # then drop the fragment. This is the URL we probe.
    canonical_resolved = urllib.parse.urldefrag(
        urllib.parse.urljoin(current_url, canonical)
    )[0]

    # Normalize for comparison: resolve against the served URL, strip the
    # fragment, lowercase scheme + host. The query string is KEPT — a
    # canonical that differs only by query points at a different URL.
    def normalize(u):
        resolved = urllib.parse.urljoin(current_url, u)
        parts = urllib.parse.urlsplit(resolved)
        return urllib.parse.urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, '')
        )

    canonical_norm = normalize(canonical)
    current_norm = normalize(current_url)

    # Case 1: Self-referencing (strictly equal including trailing slash)
    if canonical_norm == current_norm:
        return {
            'status': 'pass',
            'evidence': f'Canonical ({canonical}) exactly matches served URL.',
            'detail': {
                'canonical_url': canonical,
                'current_url': current_url,
                'identical': True,
            }
        }

    # Case 2: Canonical differs. Probe the canonical URL to see if it 3xx's.
    # Test without following redirects
    _, _, status, headers, redirects = fetch(canonical_resolved, allow_redirects=False, timeout=10)

    # Also test with redirects to get final URL
    _, final_url, final_status, _, full_chain = fetch(canonical_resolved, allow_redirects=True, timeout=10)

    is_redirect = status in (301, 302, 303, 307, 308)
    redirect_target = headers.get('Location') or headers.get('location') if is_redirect else None

    # Worst case: canonical points somewhere that redirects back to current URL
    if is_redirect and final_url and normalize(final_url) == current_norm:
        return {
            'status': 'fail',
            'evidence': f'Canonical points to {canonical} which {status}-redirects back to current URL {current_url}. This is a canonical loop.',
            'detail': {
                'canonical_url': canonical,
                'current_url': current_url,
                'canonical_status': status,
                'canonical_redirect_target': redirect_target,
                'final_url_after_redirects': final_url,
                'loop_detected': True,
            }
        }

    if is_redirect:
        return {
            'status': 'fail',
            'evidence': f'Canonical ({canonical}) returns {status} redirect to {redirect_target}. Canonical should point to a 200 page.',
            'detail': {
                'canonical_url': canonical,
                'canonical_status': status,
                'canonical_redirect_target': redirect_target,
                'final_url_after_redirects': final_url,
            }
        }

    # Canonical differs from current but resolves to a 200 — could be intentional
    # (e.g., filter variants canonicalizing to a main page)
    return {
        'status': 'warn',
        'evidence': f'Canonical ({canonical}) differs from served URL ({current_url}) but resolves to status {status}. Verify this is intentional.',
        'detail': {
            'canonical_url': canonical,
            'current_url': current_url,
            'canonical_status': status,
        }
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK B1: TTFB 5-sample median
# ──────────────────────────────────────────────────────────────────────────

def check_b1_ttfb_median(url, samples=5):
    """
    Sample TTFB 5 times and report median + p95.
    Fixes the single-sample variance problem I hit on Valeo weight loss.
    """
    ttfbs = []
    discarded_non_2xx = 0
    for i in range(samples):
        try:
            # Use curl for accurate TTFB measurement. No cache-busting query
            # param — the 'Cache-Control: no-cache' header asks for
            # revalidation while still measuring the CDN-cached reality users
            # hit. '-L' follows redirects so a fast 301 doesn't fake the TTFB.
            result = subprocess.run(
                ['curl', '-sS', '-L', '-o', '/dev/null', '-w',
                 '%{time_starttransfer} %{http_code}',
                 '--max-time', '15',
                 '-H', 'Cache-Control: no-cache',
                 url],
                capture_output=True, text=True, timeout=20
            )
            parts = result.stdout.strip().split()
            if len(parts) >= 2:
                ttfb_sec = float(parts[0])
                http_code = int(parts[1])
                if 200 <= http_code < 300:
                    ttfbs.append(ttfb_sec * 1000)  # convert to ms
                else:
                    # Error/challenge responses don't measure the real page
                    discarded_non_2xx += 1
            time.sleep(0.5)
        except Exception:
            continue

    if not ttfbs:
        evidence = 'Could not collect TTFB samples (network or tool failure).'
        if discarded_non_2xx:
            evidence = (f'Could not collect TTFB samples: all {discarded_non_2xx} '
                        f'responses were non-2xx.')
        return {
            'status': 'na',
            'evidence': evidence,
            'detail': {'discarded_non_2xx': discarded_non_2xx}
        }

    ttfbs.sort()
    median_ttfb = ttfbs[len(ttfbs) // 2]
    p95_ttfb = ttfbs[min(int(len(ttfbs) * 0.95), len(ttfbs) - 1)]
    max_ttfb = max(ttfbs)
    min_ttfb = min(ttfbs)

    # Google Core Web Vitals thresholds
    if median_ttfb < 800:
        status = 'pass'
        verdict = 'Good (<800ms)'
    elif median_ttfb < 1800:
        status = 'warn'
        verdict = 'Needs Improvement (800-1800ms)'
    else:
        status = 'fail'
        verdict = 'Poor (>1800ms)'

    evidence = f'TTFB median: {median_ttfb:.0f}ms ({verdict}). Samples: {[int(t) for t in ttfbs]}'
    if discarded_non_2xx:
        evidence += f' ({discarded_non_2xx} non-2xx sample(s) discarded)'

    return {
        'status': status,
        'evidence': evidence,
        'detail': {
            'samples_ms': [round(t, 0) for t in ttfbs],
            'median_ms': round(median_ttfb, 0),
            'p95_ms': round(p95_ttfb, 0),
            'min_ms': round(min_ttfb, 0),
            'max_ms': round(max_ttfb, 0),
            # Kept empty for output-shape compatibility: the curl write-out
            # that fed this ('%{header_x-envoy-...}') was never a valid
            # variable, so the field was always-empty noise.
            'origin_times_ms': [],
            'verdict': verdict,
            'sample_count': len(ttfbs),
            'discarded_non_2xx': discarded_non_2xx,
        }
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK D4: Schema @id coverage
# ──────────────────────────────────────────────────────────────────────────

def check_d4_schema_id_coverage(html):
    """
    Every schema entity on a production page should have @id for cross-referencing.
    Flags the TRYPS/AnswerMonk pattern of entities without @id.
    """
    entities = []
    for b in extract_jsonld_blocks(html):
        try:
            data = json.loads(b.strip())
        except json.JSONDecodeError:
            continue

        # Handle @graph, arrays, and single objects
        items_to_check = []
        if isinstance(data, dict):
            if isinstance(data.get('@graph'), list):
                items_to_check.extend(data['@graph'])
            else:
                items_to_check.append(data)
        elif isinstance(data, list):
            items_to_check.extend(data)

        for item in items_to_check:
            if not isinstance(item, dict):
                continue
            t = item.get('@type')
            if t:
                entities.append({
                    'type': t if isinstance(t, str) else str(t),
                    'has_id': bool(item.get('@id')),
                    'id': item.get('@id'),
                    'name': item.get('name', '')[:80] if isinstance(item.get('name'), str) else '',
                })

    if not entities:
        return {
            'status': 'na',
            'evidence': 'No schema entities found on the page.',
            'detail': {'entities': []}
        }

    missing_id = [e for e in entities if not e['has_id']]
    total = len(entities)
    with_id = total - len(missing_id)

    if len(missing_id) == 0:
        return {
            'status': 'pass',
            'evidence': f'All {total} schema entities have @id fragments.',
            'detail': {'entities': entities, 'with_id': with_id, 'total': total}
        }

    return {
        'status': 'fail' if len(missing_id) >= total / 2 else 'warn',
        'evidence': f'{len(missing_id)} of {total} schema entities lack @id. Missing on: {", ".join([e["type"] for e in missing_id])}.',
        'detail': {
            'entities': entities,
            'entities_missing_id': missing_id,
            'with_id': with_id,
            'total': total,
        }
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK C12b: dateModified staleness detection
# ──────────────────────────────────────────────────────────────────────────

def check_c12b_datemodified_staleness(html):
    """
    Extract dateModified from schema. Check if it's:
    (a) Missing — warn
    (b) Present and > 13 weeks old — flag as stale
    (c) Present and exactly matches the current timestamp — flag as cosmetic (Date.now() pattern)
    (d) Present and reasonable
    """
    import datetime

    dates_found = []
    for b in extract_jsonld_blocks(html):
        try:
            data = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get('@graph'), list):
            items.extend(data['@graph'])
        for item in items:
            if not isinstance(item, dict):
                continue
            dm = item.get('dateModified')
            if dm and isinstance(dm, str):
                dates_found.append({
                    'type': item.get('@type'),
                    'dateModified': dm,
                })

    if not dates_found:
        return {
            'status': 'warn',
            'evidence': 'No dateModified found in any schema entity.',
            'detail': {'dates_found': []}
        }

    now = datetime.datetime.now(datetime.timezone.utc)
    analyses = []
    for d in dates_found:
        dm_str = d['dateModified']
        try:
            # Try ISO format with various endings
            parsed = None
            for fmt_input in [dm_str, dm_str.replace('Z', '+00:00')]:
                try:
                    parsed = datetime.datetime.fromisoformat(fmt_input)
                    break
                except ValueError:
                    continue
            if parsed and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            if not parsed:
                analyses.append({**d, 'analysis': 'unparseable'})
                continue

            age = now - parsed
            age_seconds = age.total_seconds()
            age_days = age_seconds / 86400

            # Detect cosmetic pattern: dateModified within 60s of fetch time.
            # Date-only stamps (no time component) parse to midnight UTC and
            # are not a Date.now() pattern — exempt them from this window.
            date_only = bool(re.fullmatch(r'\d{4}-\d{2}-\d{2}', dm_str.strip()))
            if 0 <= age_seconds < 60 and not date_only:
                analyses.append({**d, 'analysis': 'cosmetic_near_now', 'age_days': round(age_days, 3)})
            elif age_seconds < 0:
                analyses.append({**d, 'analysis': 'future_date', 'age_days': round(age_days, 3)})
            elif age_days > 91:  # ~13 weeks
                analyses.append({**d, 'analysis': 'stale_over_13_weeks', 'age_days': round(age_days, 1)})
            else:
                analyses.append({**d, 'analysis': 'fresh', 'age_days': round(age_days, 1)})
        except Exception:
            analyses.append({**d, 'analysis': 'unparseable'})

    # Determine overall status
    # If any date is cosmetic (== now), fail
    # If all dates are stale (>13 weeks), warn
    # Pass only when at least one date is genuinely fresh
    # Unparseable-only or future-only sets warn with accurate evidence
    cosmetic = [a for a in analyses if a['analysis'] == 'cosmetic_near_now']
    stale = [a for a in analyses if a['analysis'] == 'stale_over_13_weeks']
    fresh = [a for a in analyses if a['analysis'] == 'fresh']
    future = [a for a in analyses if a['analysis'] == 'future_date']
    unparseable = [a for a in analyses if a['analysis'] == 'unparseable']

    if cosmetic:
        return {
            'status': 'fail',
            'evidence': f'{len(cosmetic)} dateModified field(s) match the current timestamp to within 60s — likely cosmetic/dynamic rendering. AP #799.',
            'detail': {'analyses': analyses}
        }

    if stale and not fresh:
        ages = [a['age_days'] for a in stale]
        return {
            'status': 'warn',
            'evidence': f'All dateModified fields are stale (>13 weeks old): ages {ages} days. 50% of AI-cited content is <13 weeks old.',
            'detail': {'analyses': analyses}
        }

    if fresh:
        return {
            'status': 'pass',
            'evidence': f'{len(fresh)} dateModified field(s) are fresh (<13 weeks old).',
            'detail': {'analyses': analyses}
        }

    # No fresh, stale or cosmetic dates left — only future and/or
    # unparseable values. Don't let these fall through to a pass.
    parts = []
    if future:
        parts.append(f'{len(future)} in the future ({[a["dateModified"] for a in future]})')
    if unparseable:
        parts.append(f'{len(unparseable)} unparseable ({[a["dateModified"] for a in unparseable]})')
    return {
        'status': 'warn',
        'evidence': 'No valid past dateModified found: ' + '; '.join(parts) + '.',
        'detail': {'analyses': analyses}
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK A2b: Title uniqueness sample (catches SPA placeholder titles)
# ──────────────────────────────────────────────────────────────────────────

def check_a2b_title_uniqueness(url, sample_size=3):
    """
    Fetch the page + a 404 URL + another sitemap URL. Compare titles.
    Statuses are captured so WAF/challenge/error bodies don't masquerade as
    page titles: the SPA-shell claim is only made when the main URL is 2xx,
    and a 200 on the 404-probe is reported as a soft-404.
    """
    origin = re.match(r'(https?://[^/]+)', url).group(1) if re.match(r'(https?://[^/]+)', url) else None
    if not origin:
        return {'status': 'na', 'evidence': 'Could not parse origin from URL.', 'detail': {}}

    # Fetch current page
    html1, _, status1, _, _ = fetch(url)
    title1 = extract_title_from_html(html1)

    if not (200 <= status1 < 300):
        return {
            'status': 'na',
            'evidence': f'Main URL returned HTTP {status1} — title uniqueness not assessable on an error/challenge response.',
            'detail': {'titles': {url: title1}, 'statuses': {url: status1}}
        }

    # Fetch a guaranteed-404 URL (fixed probe slug keeps output reproducible)
    ne_url = f'{origin}/{NONEXISTENT_PROBE_SLUG}'
    html2, _, status2, _, _ = fetch(ne_url)
    title2 = extract_title_from_html(html2)
    probe_2xx = 200 <= status2 < 300

    # Try to fetch another URL from sitemap.xml
    sitemap_url = f'{origin}/sitemap.xml'
    sitemap_html, _, sitemap_status, _, _ = fetch(sitemap_url)
    other_title = None
    other_url = None
    other_status = None
    if sitemap_status == 200 and sitemap_html:
        # Pick a URL that's different from the current one
        locs = re.findall(r'<loc>([^<]+)</loc>', sitemap_html)
        for loc in locs:
            if loc != url and not loc.endswith('/sitemap.xml'):
                other_url = loc
                html3, _, other_status, _, _ = fetch(loc)
                if 200 <= other_status < 300:
                    other_title = extract_title_from_html(html3)
                break

    # Compare
    titles_collected = {
        url: title1,
        ne_url: title2,
    }
    statuses = {url: status1, ne_url: status2}
    if other_url is not None:
        statuses[other_url] = other_status
    if other_url and other_title:
        titles_collected[other_url] = other_title

    unique_titles = set(t for t in titles_collected.values() if t)
    if len(titles_collected) < 2:
        return {
            'status': 'na',
            'evidence': 'Could not collect enough titles to compare.',
            'detail': {'titles': titles_collected, 'statuses': statuses}
        }

    # If every tested URL returns the same title → shared shell pattern.
    # Compare on REAL-page titles only: the 404 probe may legitimately lack
    # a title, and its absence must not suppress this detection.
    real_titles = [t for u, t in titles_collected.items() if u != ne_url and t]
    if len(unique_titles) == 1 and len(real_titles) >= 2:
        shared = list(unique_titles)[0]
        if probe_2xx:
            # Main URL is 2xx (checked above), so the SPA-shell claim is safe
            return {
                'status': 'fail',
                'evidence': f'All {len(titles_collected)} tested URLs return the same title "{shared}", and the nonexistent-URL probe returned HTTP {status2} (soft-404). This indicates a client-side SPA shell without per-page SSR.',
                'detail': {'titles': titles_collected, 'statuses': statuses, 'unique_count': 1, 'soft_404': True}
            }
        return {
            'status': 'fail',
            'evidence': f'All {len(titles_collected)} tested URLs return the same title "{shared}" (404 probe correctly returned HTTP {status2}). Pages share a global placeholder title.',
            'detail': {'titles': titles_collected, 'statuses': statuses, 'unique_count': 1, 'soft_404': False}
        }

    # If the 404-probe title matches the real page title → still a red flag
    if title2 and title1 and title2 == title1:
        if probe_2xx:
            return {
                'status': 'fail',
                'evidence': f'Nonexistent URL returned HTTP {status2} with the same title "{title1}" as the real page — a soft-404. Nonexistent URLs should return 404.',
                'detail': {'titles': titles_collected, 'statuses': statuses, 'unique_count': len(unique_titles), 'soft_404': True}
            }
        return {
            'status': 'fail',
            'evidence': f'404 page reuses the real page title "{title1}" (probe returned HTTP {status2}). Title appears to be a global template value.',
            'detail': {'titles': titles_collected, 'statuses': statuses, 'unique_count': len(unique_titles), 'soft_404': False}
        }

    return {
        'status': 'pass',
        'evidence': f'{len(titles_collected)} URLs tested, {len(unique_titles)} unique titles. Per-page titles appear to be rendered.',
        'detail': {'titles': titles_collected, 'statuses': statuses, 'unique_count': len(unique_titles)}
    }


def extract_title_from_html(html):
    m = re.search(r'<title[^>]*>([^<]*)</title>', html, re.IGNORECASE)
    if m:
        return re.sub(r'\s+', ' ', m.group(1)).strip()
    return None


# ──────────────────────────────────────────────────────────────────────────
# CHECK D12: Person/author schema presence
# ──────────────────────────────────────────────────────────────────────────

def check_d12_person_schema(html):
    """
    Check if any Person schema exists with credentials.
    For YMYL pages (medical, financial), this is a required E-E-A-T signal.
    """
    def node_types(node):
        # '@type' may be a string or a list of types
        t = node.get('@type')
        if isinstance(t, list):
            return [x for x in t if isinstance(x, str)]
        return [t] if isinstance(t, str) else []

    persons = []
    for b in extract_jsonld_blocks(html):
        try:
            data = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and isinstance(data.get('@graph'), list):
            items.extend(data['@graph'])
        # Also check nested Person in founder/author fields
        for item in items:
            if not isinstance(item, dict):
                continue
            if 'Person' in node_types(item):
                persons.append({
                    'name': item.get('name'),
                    'jobTitle': item.get('jobTitle'),
                    'hasCredential': bool(item.get('hasCredential')),
                    'sameAs_count': len(item.get('sameAs', [])) if isinstance(item.get('sameAs'), list) else 0,
                })
            # Nested as founder/author — value may be a single Person dict
            # or a LIST of Person dicts; normalize to a list
            for field in ['founder', 'author', 'medicalSpecialist']:
                nested = item.get(field)
                nested_list = nested if isinstance(nested, list) else [nested]
                for n in nested_list:
                    if isinstance(n, dict) and 'Person' in node_types(n):
                        persons.append({
                            'name': n.get('name'),
                            'jobTitle': n.get('jobTitle'),
                            'hasCredential': bool(n.get('hasCredential')),
                            'sameAs_count': len(n.get('sameAs', [])) if isinstance(n.get('sameAs'), list) else 0,
                            'found_via': field,
                        })

    if not persons:
        return {
            'status': 'fail',
            'evidence': 'No Person schema found in any JSON-LD block.',
            'detail': {'persons_found': []}
        }

    with_creds = [p for p in persons if p.get('hasCredential')]

    if with_creds:
        return {
            'status': 'pass',
            'evidence': f'{len(persons)} Person entities found, {len(with_creds)} with hasCredential.',
            'detail': {'persons_found': persons, 'with_credentials': with_creds}
        }

    return {
        'status': 'warn',
        'evidence': f'{len(persons)} Person entities found but none have hasCredential. Add for E-E-A-T (especially YMYL).',
        'detail': {'persons_found': persons}
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK D14: hreflang coverage (incl. Next.js streaming chunks)
# ──────────────────────────────────────────────────────────────────────────

def check_d14_hreflang_coverage(html):
    """
    Detect hreflang tags in the <head> AND inside Next.js streaming chunks
    (self.__next_f.push). Catches the App Router false-negative where
    locales declared in RSC payloads were invisible to the audit.

    Wraps `_detect_hreflang` (in deterministic_checks_extras.py) so that
    Phase 2 output carries an `hreflang_coverage` check entry like every
    other deterministic check.
    """
    r = _detect_hreflang(html)
    return {
        'status': r.get('status', 'na'),
        'evidence': r.get('evidence', ''),
        'detail': {
            'total_count': r.get('total_count', 0),
            'toplevel_count': r.get('toplevel_count', 0),
            'streamed_count': r.get('streamed_count', 0),
            'locales': r.get('locales', []),
        },
    }


# ──────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────────────────────────────────

def run_all_checks(url):
    """Run all deterministic checks and return consolidated JSON."""
    # Fetch the page once
    html, final_url, status_code, _, _ = fetch(url)

    if not html:
        return {
            'url': url,
            'error': 'Could not fetch page',
            'http_status': status_code,
            'checks': {}
        }

    results = {
        'url': url,
        'final_url_after_redirects': final_url,
        'http_status': status_code,
        'checks': {}
    }

    # (check_id, function, args) — HTML-only checks first, then network checks
    checks_to_run = [
        ('D9_faqpage_schema_vs_visible_match', check_d9_faq_schema_match, (html,)),
        ('A7b_h1_nested_in_heading', check_a7b_h1_nesting, (html,)),
        ('J2_brand_name_consistency', check_j2_brand_name_consistency, (html,)),
        ('D4_schema_id_coverage', check_d4_schema_id_coverage, (html,)),
        ('C12b_datemodified_staleness', check_c12b_datemodified_staleness, (html,)),
        ('D12_person_schema_with_credentials', check_d12_person_schema, (html,)),
        ('D14_hreflang_coverage', check_d14_hreflang_coverage, (html,)),
        ('A4b_canonical_redirect_chain', check_a4b_canonical_redirect_chain, (html, final_url)),
        ('B1_ttfb_median_5_samples', check_b1_ttfb_median, (url,)),
        ('A2b_title_uniqueness_sample', check_a2b_title_uniqueness, (url,)),
    ]

    # Error/challenge bodies (403/404/500, WAF interstitials) are not the
    # page — running content checks on them produces false claims in
    # reports. fetch() follows redirects, so a final 3xx also means real
    # content was never reached; treat it the same way.
    if status_code == 0 or status_code >= 300:
        results['content_checks_skipped'] = True
        evidence = f'page returned HTTP {status_code} — content checks not applicable'
        for check_id, _fn, _args in checks_to_run:
            results['checks'][check_id] = {
                'status': 'na',
                'evidence': evidence,
                'detail': {'http_status': status_code},
            }
    else:
        for check_id, fn, args in checks_to_run:
            try:
                results['checks'][check_id] = fn(*args)
            except Exception as e:
                # Exception isolation: one crashing check must not kill
                # the whole run.
                results['checks'][check_id] = {
                    'status': 'na',
                    'evidence': f'check crashed: {type(e).__name__}: {e}',
                    'detail': {},
                }

    # Summary counts
    statuses = [c['status'] for c in results['checks'].values()]
    results['summary'] = {
        'total_checks': len(statuses),
        'pass': statuses.count('pass'),
        'fail': statuses.count('fail'),
        'warn': statuses.count('warn'),
        'na': statuses.count('na'),
    }

    return results


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: python3 deterministic_checks.py <URL>'}))
        sys.exit(1)

    url = sys.argv[1]
    out = run_all_checks(url)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
