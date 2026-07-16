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

Wired checks (roadmap 0.2 — previously defined in check-definitions.md but
never executed; all pure page-data checks, canonical ids from
brain-mappings.json):
  A5.  robots_meta_indexing               — noindex via meta robots AND X-Robots-Tag, plus the robots.txt-vs-noindex contradiction
  A1.  https_enforcement                  — http→https redirect, HSTS header presence
  B9.  no_mixed_content                   — http:// subresources on an HTTPS page (active vs passive)
  A3.  meta_description                   — presence, 120–160 length band, duplicate-of-title
  C10. open_graph_tags                    — og:title / og:description / og:image presence
  E4.  no_nosnippet_noarchive             — nosnippet / max-snippet:0 / data-nosnippet directives
  E12. no_noarchive                       — noarchive directive (meta or header)

E-E-A-T deterministic subset (roadmap 2.4 — measured inputs feeding the
G-section LLM trust judgment; G1/G2 canonical ids, G7b/G7c sub-checks of G7):
  G1.  author_byline                      — visible byline pattern / byline markup / schema author
  G2.  author_schema_credentials          — Article author → Person/Org with sameAs|jobTitle|hasCredential
  G7b. about_contact_discoverability      — about/contact/impressum links (nav/footer membership recorded)
  G7c. editorial_policy_link              — editorial/review-policy link or publishingPrinciples schema

Every check result carries evidence_tier='measured' (roadmap 0.1) — the
verdict is computed by Python from real page bytes, never by the LLM.

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
# Shared helpers for the header/meta-based checks (A5, A1, B9, A3, C10,
# E4, E12 — the "defined but unexecuted" checks wired in roadmap item 0.2)
# ──────────────────────────────────────────────────────────────────────────

# Order- and quote-agnostic attribute extractor (same pattern A4b uses).
_ATTR_PAT = r'''(?<![\w-]){name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))'''


def _tag_attr(tag_html, name):
    """Extract one attribute value from a single tag's HTML, or None."""
    m = re.search(_ATTR_PAT.format(name=name), tag_html, re.IGNORECASE)
    if not m:
        return None
    return html_unescape(next(g for g in m.groups() if g is not None).strip())


def _meta_tags(html):
    """Yield the raw HTML of every <meta ...> tag."""
    for m in re.finditer(r'<meta\b[^>]*>', html or '', re.IGNORECASE):
        yield m.group(0)


def _get_header(headers, name):
    """Case-insensitive response-header lookup. Headers come from fetch() as a
    plain dict with original casing; None/empty-safe."""
    if not headers:
        return None
    target = name.lower()
    for k, v in headers.items():
        if str(k).lower() == target:
            return v
    return None


# X-Robots-Tag directives that legitimately contain a colon (value-carrying);
# everything else before a colon is a user-agent scope prefix to strip.
_VALUED_ROBOTS_DIRECTIVES = (
    'max-snippet', 'max-image-preview', 'max-video-preview', 'unavailable_after',
)


def _robots_directives(html, headers):
    """Collect robots directives from meta robots/googlebot/bingbot tags AND
    the X-Robots-Tag response header. Returns [(source, directive), ...] with
    directives lowercased (e.g. ('meta[name=robots]', 'noindex'))."""
    out = []
    for tag_html in _meta_tags(html):
        name = (_tag_attr(tag_html, 'name') or '').strip().lower()
        if name not in ('robots', 'googlebot', 'bingbot'):
            continue
        content = _tag_attr(tag_html, 'content') or ''
        for d in content.split(','):
            d = d.strip().lower()
            if d:
                out.append((f'meta[name={name}]', d))
    xrt = _get_header(headers, 'X-Robots-Tag') or ''
    for part in str(xrt).split(','):
        d = part.strip().lower()
        if not d:
            continue
        # UA-scoped form "googlebot: noindex" — strip the UA prefix, but keep
        # value-carrying directives like "max-snippet:0" intact.
        if ':' in d:
            left, right = d.split(':', 1)
            if left.strip() not in _VALUED_ROBOTS_DIRECTIVES:
                d = right.strip()
        if d:
            out.append(('x-robots-tag', d))
    return out


# ──────────────────────────────────────────────────────────────────────────
# CHECK A5: robots meta / X-Robots-Tag indexing + robots.txt contradiction
# ──────────────────────────────────────────────────────────────────────────

def check_a5_robots_meta_indexing(html, headers, page_url,
                                  robots_txt=None, robots_status=None):
    """A5 — Robots meta allows indexing.

    Checks noindex via BOTH the meta robots tag AND the X-Robots-Tag response
    header, and flags the robots.txt-vs-noindex contradiction the 2026 report
    warns about: a page blocked by robots.txt can never surface its noindex to
    crawlers, so the URL can stay indexed ("indexed, though blocked").

    `robots_txt`/`robots_status` are injectable for offline tests; when None,
    robots.txt is fetched from the page's origin (SSRF-guarded fetch()).
    """
    directives = _robots_directives(html, headers)
    # 'none' == noindex + nofollow per Google's robots meta spec.
    noindex_sources = sorted({src for src, d in directives
                              if d in ('noindex', 'none')})

    # robots.txt evaluation (for the contradiction check)
    robots_blocked = None
    robots_note = ''
    if robots_txt is None and robots_status is None:
        m = re.match(r'(https?://[^/]+)', str(page_url or ''))
        if m:
            body, _, st, _, _ = fetch(m.group(1) + '/robots.txt', timeout=10)
            robots_txt, robots_status = body, st
    if robots_txt is not None:
        st = robots_status if robots_status is not None else 200
        if st == 200:
            try:
                from check_robots_txt import (
                    parse_robots_txt, find_matching_groups, evaluate_path_access)
                parsed_rb = parse_robots_txt(robots_txt)
                pu = urllib.parse.urlparse(str(page_url or ''))
                path = pu.path or '/'
                if pu.query:
                    path += '?' + pu.query
                groups = find_matching_groups(parsed_rb, 'Googlebot')
                allowed, why = evaluate_path_access(groups, path)
                robots_blocked = not allowed
                robots_note = why
            except Exception as e:
                robots_note = f'robots.txt parse failed: {type(e).__name__}'
        elif 400 <= (st or 0) < 500:
            robots_blocked = False
            robots_note = f'robots.txt HTTP {st} — permissive default (no robots.txt)'
        else:
            robots_note = (f'robots.txt unavailable (HTTP {st}) — '
                           f'contradiction not assessable')

    detail = {
        'noindex_sources': noindex_sources,
        'directives': [f'{s}: {d}' for s, d in directives][:20],
        'robots_txt_blocked': robots_blocked,
        'robots_txt_note': robots_note,
    }

    if noindex_sources and robots_blocked:
        return {
            'status': 'fail',
            'severity': 'critical',
            'evidence': (
                f'Contradiction: the page carries noindex '
                f'({", ".join(noindex_sources)}) AND its path is blocked by '
                f'robots.txt ({robots_note}). Crawlers that obey robots.txt '
                f'never fetch the page, so they cannot see the noindex — the '
                f'URL can remain indexed ("indexed, though blocked by '
                f'robots.txt"). Unblock the path if the noindex is intentional.'),
            'detail': detail,
        }
    if noindex_sources:
        return {
            'status': 'fail',
            'severity': 'critical',
            'evidence': (
                f'Page opts out of indexing: noindex directive found via '
                f'{", ".join(noindex_sources)}. Search and answer engines '
                f'will drop this page from their indexes.'),
            'detail': detail,
        }
    evidence = ('No noindex directive in meta robots or X-Robots-Tag — '
                'the page allows indexing.')
    if robots_blocked:
        evidence += (f' Note: robots.txt blocks this path ({robots_note}) — '
                     f'crawl access is the binding constraint (see A10).')
    return {'status': 'pass', 'severity': 'critical',
            'evidence': evidence, 'detail': detail}


# ──────────────────────────────────────────────────────────────────────────
# CHECK A1: HTTPS enforcement (http→https redirect + HSTS)
# ──────────────────────────────────────────────────────────────────────────

def check_a1_https_enforcement(input_url, final_url, headers,
                               redirect_chain=None, http_probe=None):
    """A1 — HTTPS enforcement.

    Verifies: the page is served over HTTPS, the http:// variant redirects to
    https:// (probed, unless the initial fetch already demonstrated it), and
    the Strict-Transport-Security header is present.

    `http_probe` ({'final_url':..., 'status':...}) is injectable for offline
    tests; when None the http:// variant is fetched via the SSRF-guarded
    fetch().
    """
    fu = str(final_url or input_url or '')
    final_https = fu.lower().startswith('https://')
    hsts = _get_header(headers, 'Strict-Transport-Security')
    detail = {
        'final_url': fu,
        'hsts_present': bool(hsts),
        'hsts_value': str(hsts or '')[:120],
    }

    if not final_https:
        return {
            'status': 'fail',
            'severity': 'critical',
            'evidence': f'Page is served over HTTP, not HTTPS (final URL: {fu}).',
            'detail': detail,
        }

    redirect_enforced = None
    probe_note = ''
    if str(input_url or '').lower().startswith('http://'):
        # The initial fetch itself went http → https: redirect demonstrated.
        redirect_enforced = True
        probe_note = ('input URL was http:// and resolved to https:// '
                      '(redirect observed on the initial fetch)')
    else:
        if http_probe is None:
            http_url = 'http://' + re.sub(r'^https://', '', fu,
                                          flags=re.IGNORECASE)
            _, probe_final, probe_status, _, probe_chain = fetch(
                http_url, timeout=10)
            http_probe = {'final_url': probe_final, 'status': probe_status,
                          'hops': len(probe_chain)}
        detail['http_probe'] = http_probe
        pf = str(http_probe.get('final_url') or '').lower()
        ps = http_probe.get('status') or 0
        if ps == 0:
            redirect_enforced = None
            probe_note = ('http:// variant unreachable (port 80 closed or '
                          'timeout) — no insecure fallback exposed')
        elif pf.startswith('https://'):
            redirect_enforced = True
            probe_note = f'http:// variant redirects to https:// (final HTTP {ps})'
        else:
            redirect_enforced = False
            probe_note = (f'http:// variant serves HTTP {ps} at '
                          f'{http_probe.get("final_url")} without redirecting '
                          f'to https://')
    detail['redirect_enforced'] = redirect_enforced
    detail['probe_note'] = probe_note

    if redirect_enforced is False:
        return {
            'status': 'fail',
            'severity': 'critical',
            'evidence': (f'HTTPS not enforced: {probe_note}. The page has a '
                         f'live insecure duplicate.'),
            'detail': detail,
        }
    if hsts:
        return {
            'status': 'pass',
            'severity': 'critical',
            'evidence': (f'HTTPS enforced ({probe_note}) and '
                         f'Strict-Transport-Security is set '
                         f'({str(hsts)[:80]}).'),
            'detail': detail,
        }
    return {
        'status': 'warn',
        'severity': 'critical',
        'evidence': (f'Served over HTTPS ({probe_note}) but the '
                     f'Strict-Transport-Security (HSTS) header is missing — '
                     f'first visits over http:// are not protected.'),
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK B9: mixed content (http:// subresources on an HTTPS page)
# ──────────────────────────────────────────────────────────────────────────

# Subresource tags whose http:// references browsers BLOCK (active mixed
# content) vs merely warn about (passive).
_ACTIVE_MIXED_TAGS = frozenset({'script', 'iframe', 'embed', 'object', 'link'})
_FETCHABLE_LINK_RELS = frozenset({
    'stylesheet', 'icon', 'shortcut', 'apple-touch-icon', 'preload',
    'prefetch', 'modulepreload', 'manifest', 'mask-icon',
})


def check_b9_mixed_content(html, final_url):
    """B9 — no mixed content. Scans subresource tags (img, script, iframe,
    link[rel=stylesheet/icon/...], media) for literal http:// references on an
    HTTPS page. <a href> is navigation, not mixed content, and is ignored;
    likewise <link rel="canonical"/"alternate"> (pointers, not fetched)."""
    if not str(final_url or '').lower().startswith('https://'):
        return {
            'status': 'na',
            'evidence': 'Page is not served over HTTPS — mixed content not applicable.',
            'detail': {},
        }

    active, passive = [], []
    for m in re.finditer(
            r'<(img|script|iframe|embed|object|source|audio|video|track|link)\b[^>]*>',
            html or '', re.IGNORECASE):
        tag = m.group(1).lower()
        tag_html = m.group(0)
        if tag == 'link':
            rel_tokens = set((_tag_attr(tag_html, 'rel') or '').lower().split())
            if not (rel_tokens & _FETCHABLE_LINK_RELS):
                continue  # canonical/alternate/etc. are not fetched subresources
        urls = []
        for attr in ('src', 'href', 'data', 'poster'):
            v = _tag_attr(tag_html, attr)
            if v and v.lower().startswith('http://'):
                urls.append(v)
        for cand in (_tag_attr(tag_html, 'srcset') or '').split(','):
            u = cand.strip().split(' ')[0]
            if u.lower().startswith('http://'):
                urls.append(u)
        if not urls:
            continue
        (active if tag in _ACTIVE_MIXED_TAGS else passive).extend(urls)

    detail = {
        'active_count': len(active), 'passive_count': len(passive),
        'active_examples': active[:5], 'passive_examples': passive[:5],
    }
    if active:
        return {
            'status': 'fail',
            'severity': 'high',
            'evidence': (
                f'{len(active)} active mixed-content reference(s) (script/'
                f'iframe/stylesheet over http:// — browsers BLOCK these) '
                f'plus {len(passive)} passive. Examples: {active[:3]}'),
            'detail': detail,
        }
    if passive:
        return {
            'status': 'warn',
            'severity': 'high',
            'evidence': (
                f'{len(passive)} passive mixed-content reference(s) '
                f'(images/media loaded over http:// on an HTTPS page). '
                f'Examples: {passive[:3]}'),
            'detail': detail,
        }
    return {
        'status': 'pass',
        'severity': 'high',
        'evidence': 'No http:// subresources found on this HTTPS page.',
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK A3: meta description (presence, length band, duplicate-of-title)
# ──────────────────────────────────────────────────────────────────────────

def check_a3_meta_description(html):
    """A3 — meta description present, 120–160 chars, not a copy of <title>."""
    descs = []
    for tag_html in _meta_tags(html):
        name = (_tag_attr(tag_html, 'name') or '').strip().lower()
        if name == 'description':
            descs.append((_tag_attr(tag_html, 'content') or '').strip())
    non_empty = [d for d in descs if d]
    title = extract_title_from_html(html or '') or ''

    if not non_empty:
        return {
            'status': 'fail',
            'severity': 'high',
            'evidence': ('No meta description found (or its content is '
                         'empty). Search and answer engines will synthesize '
                         'their own snippet.'),
            'detail': {'tag_count': len(descs)},
        }

    desc = non_empty[0]
    length = len(desc)

    def _norm(s):
        return re.sub(r'\s+', ' ', s).strip().lower()

    duplicates_title = bool(title) and _norm(desc) == _norm(title)
    detail = {
        'length': length,
        'description': desc[:220],
        'duplicates_title': duplicates_title,
        'tag_count': len(descs),
    }
    multi_note = (f' ({len(non_empty)} description tags found — engines use '
                  f'the first)' if len(non_empty) > 1 else '')

    if duplicates_title:
        return {
            'status': 'warn',
            'severity': 'high',
            'evidence': (f'Meta description is an exact copy of the <title> '
                         f'("{desc[:80]}"). Write a distinct 120–160 char '
                         f'summary.{multi_note}'),
            'detail': detail,
        }
    if 120 <= length <= 160:
        return {
            'status': 'pass',
            'severity': 'high',
            'evidence': (f'Meta description present, {length} chars '
                         f'(target band 120–160).{multi_note}'),
            'detail': detail,
        }
    band = 'short' if length < 120 else 'long'
    return {
        'status': 'warn',
        'severity': 'high',
        'evidence': (f'Meta description present but too {band}: {length} '
                     f'chars (target band 120–160).{multi_note}'),
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK C10: Open Graph basics (og:title / og:description / og:image)
# ──────────────────────────────────────────────────────────────────────────

def check_c10_open_graph(html):
    """C10 — Open Graph basics. Requires og:title, og:description and
    og:image; reports og:type presence in detail."""
    og = {}
    for tag_html in _meta_tags(html):
        prop = (_tag_attr(tag_html, 'property')
                or _tag_attr(tag_html, 'name') or '').strip().lower()
        if not prop.startswith('og:'):
            continue
        content = (_tag_attr(tag_html, 'content') or '').strip()
        if content and prop not in og:
            og[prop] = content
    required = ('og:title', 'og:description', 'og:image')
    present = [k for k in required if og.get(k)]
    missing = [k for k in required if not og.get(k)]
    detail = {
        'present': present,
        'missing': missing,
        'og_type': og.get('og:type'),
        'all_og_keys': sorted(og.keys())[:15],
    }
    type_note = ('' if og.get('og:type')
                 else ' og:type is also missing (recommended).')
    if not present:
        return {
            'status': 'fail',
            'severity': 'medium',
            'evidence': ('No Open Graph tags found — shares and AI link '
                         'previews fall back to guessed title/image.'),
            'detail': detail,
        }
    if missing:
        return {
            'status': 'warn',
            'severity': 'medium',
            'evidence': (f'Open Graph incomplete: missing '
                         f'{", ".join(missing)}.{type_note}'),
            'detail': detail,
        }
    return {
        'status': 'pass',
        'severity': 'medium',
        'evidence': ('og:title, og:description and og:image all present.'
                     + type_note),
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK E4: no nosnippet / max-snippet:0 (snippets available to answer engines)
# ──────────────────────────────────────────────────────────────────────────

def check_e4_nosnippet_directives(html, headers):
    """E4 — no nosnippet/max-snippet:0. Reads meta robots AND X-Robots-Tag;
    also flags data-nosnippet attributes (partial snippet blocking)."""
    directives = _robots_directives(html, headers)
    nosnippet = [f'{s}: {d}' for s, d in directives if d == 'nosnippet']
    max0, max_small = [], []
    for s, d in directives:
        m = re.match(r'max-snippet\s*:\s*(-?\d+)', d)
        if not m:
            continue
        n = int(m.group(1))
        if n == 0:
            max0.append(f'{s}: {d}')
        elif 0 < n < 50:
            max_small.append(f'{s}: {d}')
    data_nosnippet = len(re.findall(r'\bdata-nosnippet\b', html or '',
                                    re.IGNORECASE))
    detail = {
        'nosnippet': nosnippet,
        'max_snippet_zero': max0,
        'max_snippet_small': max_small,
        'data_nosnippet_attrs': data_nosnippet,
    }
    if nosnippet or max0:
        return {
            'status': 'fail',
            'severity': 'critical',
            'evidence': (
                f'Snippets are disabled: {", ".join(nosnippet + max0)}. '
                f'Search and AI answer engines cannot quote this page — it '
                f'is effectively invisible in answers.'),
            'detail': detail,
        }
    if max_small or data_nosnippet:
        parts = []
        if max_small:
            parts.append(f'restrictive {", ".join(max_small)}')
        if data_nosnippet:
            parts.append(f'{data_nosnippet} data-nosnippet attribute(s)')
        return {
            'status': 'warn',
            'severity': 'critical',
            'evidence': (f'Partial snippet restrictions: {"; ".join(parts)}. '
                         f'Quoted answers may be truncated or exclude key '
                         f'content.'),
            'detail': detail,
        }
    return {
        'status': 'pass',
        'severity': 'critical',
        'evidence': ('No nosnippet/max-snippet:0 restrictions in meta robots '
                     'or X-Robots-Tag.'),
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# CHECK E12: no noarchive
# ──────────────────────────────────────────────────────────────────────────

def check_e12_noarchive(html, headers):
    """E12 — no noarchive directive (meta robots or X-Robots-Tag). Cached
    copies feed several answer engines; noarchive removes the page from them."""
    directives = _robots_directives(html, headers)
    hits = [f'{s}: {d}' for s, d in directives if d == 'noarchive']
    detail = {'noarchive': hits}
    if hits:
        return {
            'status': 'fail',
            'severity': 'high',
            'evidence': (f'noarchive directive present ({", ".join(hits)}) — '
                         f'cached/archived copies are disabled; engines that '
                         f'read from cache lose access to this page.'),
            'detail': detail,
        }
    return {
        'status': 'pass',
        'severity': 'high',
        'evidence': 'No noarchive directive in meta robots or X-Robots-Tag.',
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# E-E-A-T deterministic subset (roadmap 2.4). These measure the objective
# substrate of the G-section trust judgment — presence of bylines, About/
# Contact discoverability, editorial-policy links, and schema-author linkage.
# They FEED the LLM's G-section assessment as measured inputs; canonical ids
# from brain-mappings.json (G1, G2 exact; G7b/G7c sub-checks of G7 following
# the A2b/A4b/C12b convention — query_brain resolves them to G7).
# ──────────────────────────────────────────────────────────────────────────

def _iter_jsonld_items(html):
    """Yield every dict node from all JSON-LD blocks, descending into @graph.
    Shared walker for the G-section schema checks."""
    for b in extract_jsonld_blocks(html):
        try:
            data = json.loads(b.strip())
        except json.JSONDecodeError:
            continue
        stack = list(data) if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            yield item
            graph = item.get('@graph')
            if isinstance(graph, list):
                stack.extend(g for g in graph if isinstance(g, dict))


def _node_types(node):
    """'@type' may be a string or a list — normalize to a list of strings."""
    t = node.get('@type')
    if isinstance(t, list):
        return [x for x in t if isinstance(x, str)]
    return [t] if isinstance(t, str) else []


# Visible byline patterns. The bare "By <Name>" form requires TWO capitalized
# tokens (first + last name) to avoid matching prose like "by using". The
# explicit verb forms accept a single capitalized name. The verb/label part is
# case-insensitive via a scoped (?i:...) group; the NAME tokens stay
# case-SENSITIVE — that capitalization requirement is the false-positive gate.
_NAME_TOKEN = r"[A-Z][\w.'’-]+"
_BYLINE_EXPLICIT_RE = re.compile(
    r'\b(?i:written\s+by|authored\s+by|reviewed\s+by|'
    r'medically\s+reviewed\s+by|posted\s+by|verfasst\s+von|'
    r'geschrieben\s+von)[:\s]+('
    + _NAME_TOKEN + r'(?:\s+' + _NAME_TOKEN + r'){0,3})')
_BYLINE_BARE_RE = re.compile(
    r'\b[Bb]y[:\s]+(' + _NAME_TOKEN + r'(?:\s+' + _NAME_TOKEN + r'){1,3})\b')
_AUTHOR_LABEL_RE = re.compile(
    r'\b(?i:author|autor)\s*:\s*(' + _NAME_TOKEN
    + r'(?:\s+' + _NAME_TOKEN + r'){0,3})')

# Byline-intent markup: rel=author, class/id/itemprop author|byline.
_BYLINE_MARKUP_RE = re.compile(
    r'''(?:rel\s*=\s*["']?author|itemprop\s*=\s*["']?author|'''
    r'''(?:class|id)\s*=\s*["'][^"']*\b(?:byline|author)\b)''',
    re.IGNORECASE)


def _schema_author_names(html):
    """Author names declared anywhere in JSON-LD ('author' property values,
    string or Person/Organization node(s)). Sorted for determinism."""
    names = set()
    for item in _iter_jsonld_items(html):
        author = item.get('author')
        if author is None:
            continue
        vals = author if isinstance(author, list) else [author]
        for a in vals:
            if isinstance(a, str) and a.strip():
                names.add(a.strip())
            elif isinstance(a, dict):
                n = a.get('name')
                if isinstance(n, str) and n.strip():
                    names.add(n.strip())
    return sorted(names)


def check_g1_author_byline(html):
    """G1 — author byline visible (OR declared via schema author).

    Measures the objective substrate of the E-E-A-T authorship judgment:
    a visible byline pattern in the rendered text / byline-intent markup,
    and/or an author declared in JSON-LD. pass = visibly bylined;
    warn = schema-only (add a visible byline); fail = neither.
    """
    text = strip_tags(html)
    visible_hits = []
    for pat in (_BYLINE_EXPLICIT_RE, _AUTHOR_LABEL_RE, _BYLINE_BARE_RE):
        for match in pat.finditer(text):
            visible_hits.append(match.group(1).strip())
    # de-dupe, keep first-seen order for stable evidence
    seen = set()
    visible_hits = [h for h in visible_hits
                    if not (h in seen or seen.add(h))]
    markup_hit = bool(_BYLINE_MARKUP_RE.search(html or ''))
    schema_authors = _schema_author_names(html)

    detail = {
        'visible_byline_names': visible_hits[:5],
        'byline_markup_present': markup_hit,
        'schema_authors': schema_authors[:5],
    }

    if visible_hits or markup_hit:
        how = []
        if visible_hits:
            how.append(f'visible byline ({visible_hits[0]})')
        if markup_hit:
            how.append('byline/author markup (rel/class/itemprop)')
        if schema_authors:
            how.append(f'schema author ({schema_authors[0]})')
        return {
            'status': 'pass',
            'evidence': 'Author attribution present: ' + '; '.join(how) + '.',
            'detail': detail,
        }
    if schema_authors:
        return {
            'status': 'warn',
            'evidence': (
                f'Author declared in schema ({schema_authors[0]}) but no '
                f'visible byline pattern found on the page — answer engines '
                f'weight visible attribution; add a byline.'),
            'detail': detail,
        }
    return {
        'status': 'fail',
        'evidence': ('No author byline found: no visible byline pattern, no '
                     'byline markup, and no schema author. E-E-A-T authorship '
                     'is unverifiable for this page.'),
        'detail': detail,
    }


_ARTICLE_TYPES_RE = re.compile(r'Article$|^BlogPosting$|^Report$', )


def check_g2_schema_author_linkage(html):
    """G2 — schema-author linkage: Article author → Person/Organization
    with sameAs (or jobTitle/hasCredential, per the G2 definition).

    pass = an Article-family node's author resolves (inline or via @id) to a
    Person/Organization carrying sameAs / jobTitle / hasCredential.
    warn = author present but unlinked (bare string) or lacking those props.
    fail = Article-family schema present with NO author at all.
    na   = no Article-family node and no author property anywhere.
    """
    items = list(_iter_jsonld_items(html))
    by_id = {item['@id']: item for item in items
             if isinstance(item.get('@id'), str)}

    def resolve(author):
        """Resolve an author value to a node dict where possible."""
        if isinstance(author, dict):
            ref = author.get('@id')
            if ref and len(author) <= 2 and ref in by_id:
                return by_id[ref]     # {'@id': ...} (+optional @type) reference
            return author
        if isinstance(author, str):
            return by_id.get(author)  # bare '@id' string reference
        return None

    def credential_props(node):
        props = []
        same_as = node.get('sameAs')
        if (isinstance(same_as, str) and same_as.strip()) or \
                (isinstance(same_as, list) and any(
                    isinstance(u, str) and u.strip() for u in same_as)):
            props.append('sameAs')
        if isinstance(node.get('jobTitle'), str) and node['jobTitle'].strip():
            props.append('jobTitle')
        if node.get('hasCredential'):
            props.append('hasCredential')
        return props

    article_nodes = [i for i in items
                     if any(_ARTICLE_TYPES_RE.search(t)
                            for t in _node_types(i))]
    # Author properties on any node (covers WebPage.author etc.)
    author_bearing = [i for i in items if i.get('author') is not None]

    if not article_nodes and not author_bearing:
        return {
            'status': 'na',
            'evidence': ('No Article-family schema and no author property in '
                         'any JSON-LD node — schema-author linkage not '
                         'applicable.'),
            'detail': {'article_nodes': 0},
        }

    linked, unlinked = [], []
    source_nodes = article_nodes if article_nodes else author_bearing
    articles_missing_author = 0
    for node in source_nodes:
        author = node.get('author')
        if author is None:
            articles_missing_author += 1
            continue
        vals = author if isinstance(author, list) else [author]
        for a in vals:
            resolved = resolve(a)
            if isinstance(resolved, dict):
                types = _node_types(resolved)
                is_person_org = any(t in ('Person', 'Organization')
                                    for t in types)
                props = credential_props(resolved)
                name = resolved.get('name') if isinstance(
                    resolved.get('name'), str) else None
                entry = {'name': name, 'types': types, 'linked_props': props}
                if is_person_org and props:
                    linked.append(entry)
                else:
                    unlinked.append(entry)
            else:
                unlinked.append({'name': a if isinstance(a, str) else None,
                                 'types': [], 'linked_props': []})

    detail = {
        'article_nodes': len(article_nodes),
        'authors_linked': linked[:5],
        'authors_unlinked': unlinked[:5],
        'articles_missing_author': articles_missing_author,
    }

    if linked:
        first = linked[0]
        return {
            'status': 'pass',
            'evidence': (
                f'Schema author linked: {first["name"] or "(unnamed)"} '
                f'({"/".join(first["types"]) or "untyped"}) carries '
                f'{", ".join(first["linked_props"])}. '
                f'{len(linked)} linked author node(s) total.'),
            'detail': detail,
        }
    if unlinked:
        return {
            'status': 'warn',
            'evidence': (
                f'Author declared in schema but not linked to an identity: '
                f'{len(unlinked)} author value(s) are bare strings or '
                f'Person/Organization nodes without sameAs / jobTitle / '
                f'hasCredential. Add sameAs profile URLs to the author node.'),
            'detail': detail,
        }
    return {
        'status': 'fail',
        'evidence': (
            f'{len(article_nodes)} Article-family schema node(s) present but '
            f'none declares an author — add author → Person/Organization '
            f'with sameAs.'),
        'detail': detail,
    }


# About / Contact / editorial-policy link detection. Matched against BOTH the
# href and the anchor text (lowercased). Word-ish boundaries keep 'contact'
# from matching inside unrelated tokens.
_ABOUT_PAT = re.compile(
    r'(?:\babout(?:[-_/ ]?us)?\b|\bcompany\b|\bwho[-_ ]we[-_ ]are\b|'
    r'\bteam\b|\büber[-_ ]?uns\b|\bueber[-_ ]?uns\b)', re.IGNORECASE)
_CONTACT_PAT = re.compile(
    r'(?:\bcontact(?:[-_/ ]?us)?\b|\bkontakt\b|\bimpressum\b|\bimprint\b|'
    r'\bget[-_ ]in[-_ ]touch\b)', re.IGNORECASE)
_EDITORIAL_PAT = re.compile(
    r'(?:editorial[-_ ](?:policy|guidelines|standards|process)|'
    r'review[-_ ](?:policy|process|guidelines)|'
    r'content[-_ ](?:policy|standards|guidelines|integrity)|'
    r'corrections?[-_ ]policy|'
    r'fact[-_ ]?check(?:ing)?(?:[-_ ]policy)?|'
    r'publishing[-_ ]principles|ethics[-_ ](?:policy|statement))',
    re.IGNORECASE)

_ANCHOR_RE = re.compile(r'<a\b[^>]*>(.*?)</a\s*>', re.IGNORECASE | re.DOTALL)


def _page_anchors(html):
    """[(href, text, in_nav_or_footer), ...] for every anchor on the page.
    nav/footer/header membership is judged by position inside those blocks
    (regex block spans — good enough for discoverability evidence)."""
    spans = []
    for tag in ('nav', 'footer', 'header'):
        for m in re.finditer(rf'<{tag}\b[^>]*>.*?</{tag}\s*>',
                             html or '', re.IGNORECASE | re.DOTALL):
            spans.append((m.start(), m.end()))
    out = []
    for m in _ANCHOR_RE.finditer(html or ''):
        tag_open = m.group(0)
        href_m = re.search(_ATTR_PAT.format(name='href'), tag_open,
                           re.IGNORECASE)
        href = ''
        if href_m:
            href = html_unescape(
                next(g for g in href_m.groups() if g is not None).strip())
        text = re.sub(r'<[^>]+>', ' ', m.group(1))
        text = re.sub(r'\s+', ' ', html_unescape(text)).strip()
        in_chrome = any(s <= m.start() < e for s, e in spans)
        out.append((href, text, in_chrome))
    return out


def check_g7b_about_contact(html):
    """G7b — About/Contact discoverability: links to about / contact /
    impressum reachable from the page (nav/footer membership recorded).
    An identifiable operator is a core E-E-A-T trust signal.
    """
    about_links, contact_links = [], []
    for href, text, in_chrome in _page_anchors(html):
        hay_href, hay_text = href or '', text or ''
        entry = {'href': href[:200], 'text': text[:80],
                 'in_nav_or_footer': in_chrome}
        if _ABOUT_PAT.search(hay_href) or _ABOUT_PAT.search(hay_text):
            about_links.append(entry)
        # 'contact' can legitimately co-exist with about on one link; check both
        if _CONTACT_PAT.search(hay_href) or _CONTACT_PAT.search(hay_text):
            contact_links.append(entry)

    detail = {
        'about_links': about_links[:5],
        'contact_links': contact_links[:5],
        'about_in_nav_or_footer': any(l['in_nav_or_footer']
                                      for l in about_links),
        'contact_in_nav_or_footer': any(l['in_nav_or_footer']
                                        for l in contact_links),
    }

    if about_links and contact_links:
        return {
            'status': 'pass',
            'evidence': (
                f'About and Contact are discoverable: '
                f'{len(about_links)} about-link(s) '
                f'(e.g. {about_links[0]["href"] or about_links[0]["text"]}), '
                f'{len(contact_links)} contact/impressum link(s).'),
            'detail': detail,
        }
    if about_links or contact_links:
        missing = 'contact/impressum' if about_links else 'about'
        found = about_links[0] if about_links else contact_links[0]
        return {
            'status': 'warn',
            'evidence': (
                f'Only one operator-identity link found '
                f'({found["href"] or found["text"]}); no {missing} link '
                f'detected in the page (incl. nav/footer).'),
            'detail': detail,
        }
    return {
        'status': 'fail',
        'evidence': ('No About or Contact/Impressum links found anywhere on '
                     'the page — the operator behind the content is not '
                     'discoverable, a core E-E-A-T trust gap.'),
        'detail': detail,
    }


def check_g7c_editorial_policy(html):
    """G7c — editorial / review-policy link presence (or schema
    publishingPrinciples). Never fails: absence is a warn — the signal
    matters most for YMYL/publisher pages, which the LLM judges in context.
    """
    policy_links = []
    for href, text, in_chrome in _page_anchors(html):
        if _EDITORIAL_PAT.search(href or '') or _EDITORIAL_PAT.search(text or ''):
            policy_links.append({'href': href[:200], 'text': text[:80],
                                 'in_nav_or_footer': in_chrome})
    publishing_principles = None
    for item in _iter_jsonld_items(html):
        pp = item.get('publishingPrinciples')
        if isinstance(pp, str) and pp.strip():
            publishing_principles = pp.strip()
            break
        if isinstance(pp, dict):
            ref = pp.get('@id') or pp.get('url')
            if isinstance(ref, str) and ref.strip():
                publishing_principles = ref.strip()
                break

    detail = {
        'policy_links': policy_links[:5],
        'publishing_principles': publishing_principles,
    }

    if policy_links or publishing_principles:
        how = []
        if policy_links:
            how.append(f'link ({policy_links[0]["href"] or policy_links[0]["text"]})')
        if publishing_principles:
            how.append(f'schema publishingPrinciples ({publishing_principles})')
        return {
            'status': 'pass',
            'evidence': 'Editorial/review-policy signal present: '
                        + '; '.join(how) + '.',
            'detail': detail,
        }
    return {
        'status': 'warn',
        'evidence': ('No editorial/review-policy link or publishingPrinciples '
                     'schema found. Important for YMYL/publisher pages; '
                     'optional elsewhere.'),
        'detail': detail,
    }


# ──────────────────────────────────────────────────────────────────────────
# Main orchestrator
# ──────────────────────────────────────────────────────────────────────────

def run_all_checks(url):
    """Run all deterministic checks and return consolidated JSON."""
    # Fetch the page once — keep headers + redirect chain: A5/E4/E12 read
    # X-Robots-Tag, A1 reads HSTS + the http→https redirect evidence.
    html, final_url, status_code, headers, redirect_chain = fetch(url)

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
        # Wired checks (roadmap 0.2) — canonical ids from brain-mappings.json
        ('A3_meta_description', check_a3_meta_description, (html,)),
        ('C10_open_graph_tags', check_c10_open_graph, (html,)),
        # E-E-A-T deterministic subset (roadmap 2.4) — measured inputs that
        # feed the G-section trust judgment. G1/G2 canonical; G7b/G7c are
        # sub-checks of G7 (privacy/terms → operator-identity links).
        ('G1_author_byline', check_g1_author_byline, (html,)),
        ('G2_author_schema_credentials', check_g2_schema_author_linkage, (html,)),
        ('G7b_about_contact_discoverability', check_g7b_about_contact, (html,)),
        ('G7c_editorial_policy_link', check_g7c_editorial_policy, (html,)),
        ('E4_no_nosnippet_noarchive', check_e4_nosnippet_directives, (html, headers)),
        ('E12_no_noarchive', check_e12_noarchive, (html, headers)),
        ('B9_no_mixed_content', check_b9_mixed_content, (html, final_url)),
        # Network checks below (robots.txt fetch / http probe / samples)
        ('A5_robots_meta_indexing', check_a5_robots_meta_indexing,
         (html, headers, final_url)),
        ('A1_https_enforcement', check_a1_https_enforcement,
         (url, final_url, headers, redirect_chain)),
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

    # Evidence tier (roadmap 0.1): every verdict emitted here comes from
    # deterministic Python over real page bytes — stamp it 'measured' so the
    # report/compact rows can distinguish it from LLM-judged findings.
    for check_result in results['checks'].values():
        if isinstance(check_result, dict):
            check_result.setdefault('evidence_tier', 'measured')

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
