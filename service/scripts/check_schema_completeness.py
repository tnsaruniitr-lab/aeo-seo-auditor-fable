#!/usr/bin/env python3
"""
check_schema_completeness.py — Per-@type required/recommended field validator

Parses all JSON-LD schema blocks from the page and validates each entity against
Schema.org required and Google-recommended field lists.

Catches the "schema present but incomplete" pattern — e.g. a Product schema
without `offers`, Organization without `sameAs`, MedicalBusiness without `address`.

Usage:
  python3 scripts/check_schema_completeness.py <URL>

Output: JSON to stdout with per-entity validation.

Deterministic: same HTML input → identical JSON output.

Coverage — types validated:
  - Organization, LocalBusiness, MedicalBusiness (subtypes of Organization)
  - Person
  - WebSite, WebPage
  - Article, BlogPosting, NewsArticle
  - Product
  - Offer, AggregateOffer
  - FAQPage
  - HowTo
  - Review, AggregateRating
  - Recipe
  - Event
  - VideoObject, ImageObject
  - BreadcrumbList
  - MedicalProcedure, MedicalTherapy, Drug
  - SoftwareApplication, MobileApplication
  - Service

Field classifications:
  - REQUIRED: mandatory per Schema.org spec — absence disqualifies from rich results
  - GOOGLE_REQUIRED: additional fields Google requires for rich results
  - RECOMMENDED: improves eligibility but not strictly required
"""

import sys
import re
import json
import pathlib
import urllib.request
from urllib.error import URLError, HTTPError

# SSRF guard (canonical impl at service/safety.py, one dir up). fetch_html
# follows redirects; a public URL can 30x to an internal host / metadata IP.
# Validate the initial URL and every redirect hop before reading any body.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
try:
    from safety import check_url_safe
except Exception:
    def check_url_safe(url, resolve=True):  # stdlib fallback keeps scripts standalone
        return True, None   # (only used if safety.py is somehow absent)


# ──────────────────────────────────────────────────────────────────────────
# Field specifications per @type
# Source: schema.org/[Type] + developers.google.com/search/docs/appearance/structured-data/*
# ──────────────────────────────────────────────────────────────────────────

FIELD_SPECS = {
    # Thing (base) — name, url are universally recommended
    'Organization': {
        'required': ['name', 'url'],
        'google_required': [],
        'recommended': ['logo', 'sameAs', 'contactPoint', 'description'],
    },
    'LocalBusiness': {
        'required': ['name', 'url'],
        'google_required': ['address', 'telephone'],
        'recommended': ['openingHours', 'priceRange', 'geo', 'image', 'aggregateRating'],
    },
    'MedicalBusiness': {
        'required': ['name', 'url'],
        'google_required': ['address'],
        'recommended': ['telephone', 'medicalSpecialty', 'priceRange', 'aggregateRating', 'hasOfferCatalog'],
    },
    'MedicalClinic': {
        'required': ['name', 'url'],
        'google_required': ['address'],
        'recommended': ['telephone', 'medicalSpecialty', 'availableService'],
    },
    'MedicalOrganization': {
        'required': ['name', 'url'],
        'google_required': [],
        'recommended': ['medicalSpecialty', 'sameAs', 'contactPoint'],
    },
    'Person': {
        'required': ['name'],
        'google_required': [],
        'recommended': ['jobTitle', 'sameAs', 'url', 'worksFor', 'hasCredential'],
    },
    'WebSite': {
        'required': ['name', 'url'],
        'google_required': [],
        'recommended': ['publisher', 'potentialAction'],
    },
    'WebPage': {
        'required': ['name', 'url'],
        'google_required': [],
        'recommended': ['description', 'dateModified', 'isPartOf', 'primaryImageOfPage', 'inLanguage'],
    },
    'Article': {
        'required': ['headline', 'datePublished'],
        'google_required': ['author', 'image'],
        'recommended': ['dateModified', 'publisher', 'description', 'mainEntityOfPage'],
    },
    'BlogPosting': {
        'required': ['headline', 'datePublished'],
        'google_required': ['author', 'image'],
        'recommended': ['dateModified', 'publisher', 'description'],
    },
    'NewsArticle': {
        'required': ['headline', 'datePublished'],
        'google_required': ['author', 'image'],
        'recommended': ['dateModified', 'publisher', 'description'],
    },
    'Product': {
        'required': ['name'],
        'google_required': ['offers'],
        'recommended': ['image', 'description', 'brand', 'aggregateRating', 'review', 'sku'],
    },
    'Offer': {
        'required': [],
        'google_required': ['price', 'priceCurrency'],
        'recommended': ['availability', 'priceValidUntil', 'url', 'itemCondition'],
    },
    'AggregateOffer': {
        'required': ['lowPrice', 'priceCurrency', 'offerCount'],
        'google_required': [],
        'recommended': ['highPrice', 'offers'],
    },
    'FAQPage': {
        'required': ['mainEntity'],
        'google_required': [],
        'recommended': [],
        # Custom: mainEntity must be array of Question objects
        'custom_checks': ['faqpage_mainentity_is_array_of_questions'],
    },
    'Question': {
        'required': ['name', 'acceptedAnswer'],
        'google_required': [],
        'recommended': [],
    },
    'Answer': {
        'required': ['text'],
        'google_required': [],
        'recommended': [],
    },
    'HowTo': {
        'required': ['name', 'step'],
        'google_required': [],
        'recommended': ['description', 'image', 'totalTime', 'supply', 'tool', 'estimatedCost'],
    },
    'HowToStep': {
        'required': ['text'],
        'google_required': [],
        'recommended': ['name', 'image', 'url'],
    },
    'Review': {
        'required': ['author', 'reviewRating'],
        'google_required': ['itemReviewed'],
        'recommended': ['datePublished', 'reviewBody'],
    },
    'AggregateRating': {
        'required': ['ratingValue', 'ratingCount'],
        'google_required': [],
        'recommended': ['bestRating', 'worstRating', 'reviewCount'],
    },
    'Rating': {
        'required': ['ratingValue'],
        'google_required': [],
        'recommended': ['bestRating', 'worstRating'],
    },
    'Recipe': {
        'required': ['name', 'recipeIngredient'],
        'google_required': ['image', 'author'],
        'recommended': ['datePublished', 'description', 'cookTime', 'prepTime', 'totalTime', 'recipeYield', 'recipeInstructions', 'nutrition', 'aggregateRating'],
    },
    'Event': {
        'required': ['name', 'startDate', 'location'],
        'google_required': [],
        'recommended': ['endDate', 'eventStatus', 'eventAttendanceMode', 'image', 'description', 'offers', 'organizer'],
    },
    'VideoObject': {
        'required': ['name', 'description', 'thumbnailUrl', 'uploadDate'],
        'google_required': [],
        'recommended': ['contentUrl', 'embedUrl', 'duration', 'interactionStatistic'],
    },
    'ImageObject': {
        'required': ['url'],
        'google_required': [],
        'recommended': ['width', 'height', 'caption', 'creator', 'license'],
    },
    'BreadcrumbList': {
        'required': ['itemListElement'],
        'google_required': [],
        'recommended': [],
        'custom_checks': ['breadcrumblist_sequential_positions'],
    },
    'ListItem': {
        'required': ['position', 'name'],
        'google_required': [],
        'recommended': ['item'],
    },
    'MedicalProcedure': {
        'required': ['name'],
        'google_required': [],
        'recommended': ['description', 'howPerformed', 'preparation', 'procedureType', 'followup'],
    },
    'MedicalTherapy': {
        'required': ['name'],
        'google_required': [],
        'recommended': ['description', 'contraindication', 'seriousAdverseOutcome', 'indication'],
    },
    'Drug': {
        'required': ['name'],
        'google_required': [],
        'recommended': ['activeIngredient', 'dosageForm', 'prescribingInfo', 'warning', 'nonProprietaryName'],
    },
    'SoftwareApplication': {
        'required': ['name'],
        'google_required': ['offers', 'operatingSystem', 'applicationCategory'],
        'recommended': ['aggregateRating', 'description', 'screenshot', 'featureList', 'datePublished', 'dateModified', 'softwareVersion'],
    },
    'MobileApplication': {
        'required': ['name', 'operatingSystem'],
        'google_required': ['offers', 'applicationCategory'],
        'recommended': ['aggregateRating', 'description', 'screenshot'],
    },
    'Service': {
        'required': ['name'],
        'google_required': [],
        'recommended': ['description', 'provider', 'areaServed', 'serviceType', 'offers'],
    },
    'ContactPoint': {
        'required': ['contactType'],
        'google_required': [],
        'recommended': ['telephone', 'email', 'availableLanguage', 'areaServed'],
    },
    'PostalAddress': {
        'required': [],
        'google_required': ['streetAddress', 'addressLocality', 'addressCountry'],
        'recommended': ['postalCode', 'addressRegion'],
    },
    'SearchAction': {
        'required': ['target'],
        'google_required': ['query-input'],
        'recommended': [],
    },
}


class _SchemaBlockedRedirect(Exception):
    """Raised when a redirect hop fails the SSRF guard, so fetch_html returns
    (None, 0, 'redirect blocked ...') rather than following to an internal host."""
    def __init__(self, newurl, reason):
        super().__init__(reason)
        self.newurl = newurl
        self.reason = reason


class _Redirect308Handler(urllib.request.HTTPRedirectHandler):
    """urllib follows 308 Permanent Redirect only from Python 3.11; alias to 307
    so http→https 308s (Vercel/Framer hosts) don't abort the whole check.
    The base redirect_request also has a hardcoded (301,302,303,307) allowlist
    on Python < 3.11, so 308 must be presented to it as 307."""
    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_307

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # SSRF: validate every redirect hop before following it.
        ok, reason = check_url_safe(newurl)
        if not ok:
            raise _SchemaBlockedRedirect(newurl, reason)
        return super().redirect_request(
            req, fp, 307 if code == 308 else code, msg, headers, newurl)


def fetch_html(url, timeout=15):
    """Fetch HTML from URL. Returns (html, http_status, error)."""
    # SSRF pre-flight on the initial URL.
    ok, reason = check_url_safe(url)
    if not ok:
        return None, 0, f'blocked by SSRF guard: {reason}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (AuditBot/1.0)'})
    try:
        opener = urllib.request.build_opener(_Redirect308Handler())
        resp = opener.open(req, timeout=timeout)
        return resp.read().decode('utf-8', errors='replace'), resp.status, None
    except _SchemaBlockedRedirect as e:
        return None, 0, f'redirect blocked by SSRF guard: {e.reason}'
    except HTTPError as e:
        # Keep the real status code — a CDN 403 ("blocked") must stay
        # distinguishable from a network failure (status 0).
        return None, e.code, f'HTTP {e.code} {e.reason}'
    except (URLError, Exception) as e:
        # urllib may wrap the raised _SchemaBlockedRedirect in a URLError.
        if isinstance(getattr(e, 'reason', None), _SchemaBlockedRedirect):
            return None, 0, f'redirect blocked by SSRF guard: {e.reason.reason}'
        return None, 0, f'{type(e).__name__}: {e}'


def extract_schema_blocks(html):
    """Extract all JSON-LD blocks. Returns list of parsed dict/list objects."""
    if not html:
        return []
    blocks = []
    for m in re.finditer(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.IGNORECASE | re.DOTALL
    ):
        try:
            parsed = json.loads(m.group(1).strip())
            blocks.append(parsed)
        except json.JSONDecodeError as e:
            blocks.append({'__parse_error': str(e), '__raw_start': m.group(1)[:200]})
    return blocks


def flatten_entities(blocks):
    """
    Flatten all JSON-LD blocks into a single list of entity dicts.
    Handles: @graph, arrays, nested entities (founder, author, etc.).
    Returns list of entities with @type.
    """
    entities = []

    def walk(obj, depth=0):
        if depth > 5:  # avoid infinite recursion
            return
        if isinstance(obj, dict):
            if '@type' in obj:
                entities.append(obj)
            # Recurse into nested objects
            for key, val in obj.items():
                if key == '@graph':
                    # @graph is a structural wrapper, not nesting — recurse
                    # at the same depth so list-wrapped @graph blocks
                    # ([{"@graph": [...]}]) are reached too.
                    walk(val, depth)
                    continue
                if key.startswith('@'):
                    continue
                if isinstance(val, (dict, list)):
                    walk(val, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                walk(item, depth + 1)

    for block in blocks:
        if isinstance(block, dict) and '__parse_error' in block:
            continue
        walk(block)

    return entities


def normalize_type(type_value):
    """
    Normalize @type — can be string or array of strings. Return primary type string.
    For arrays, prefer the first element that HAS a validation spec
    (["Physiotherapy", "LocalBusiness"] → LocalBusiness); fall back to the
    first string element if none do. Non-string elements are skipped.
    """
    if isinstance(type_value, str):
        return type_value
    if isinstance(type_value, list) and len(type_value) > 0:
        str_types = [t for t in type_value if isinstance(t, str)]
        for t in str_types:
            if t in FIELD_SPECS:
                return t
        if str_types:
            return str_types[0]
    return 'Unknown'


def validate_entity(entity):
    """
    Validate a single entity against FIELD_SPECS.
    Returns {type, missing_required, missing_google_required, missing_recommended, validation_status}.
    """
    entity_type = normalize_type(entity.get('@type'))
    spec = FIELD_SPECS.get(entity_type)

    if not spec:
        return {
            'type': entity_type,
            'name': entity.get('name'),
            'validation_status': 'no_spec',
            'missing_required': [],
            'missing_google_required': [],
            'missing_recommended': [],
            'has_id': bool(entity.get('@id')),
        }

    def field_present(field_name, entity):
        """Check if field is present and has a non-empty value."""
        val = entity.get(field_name)
        if val is None:
            return False
        if isinstance(val, str) and val.strip() == '':
            return False
        if isinstance(val, list) and len(val) == 0:
            return False
        if isinstance(val, dict) and len(val) == 0:
            return False
        return True

    missing_required = [f for f in spec['required'] if not field_present(f, entity)]
    missing_google_req = [f for f in spec['google_required'] if not field_present(f, entity)]
    missing_recommended = [f for f in spec['recommended'] if not field_present(f, entity)]

    # Custom checks
    custom_issues = []
    if entity_type == 'FAQPage':
        main_entity = entity.get('mainEntity', [])
        if isinstance(main_entity, dict):
            # A single Question object is valid JSON-LD — treat as 1-item list
            main_entity = [main_entity]
        if not isinstance(main_entity, list):
            custom_issues.append('mainEntity is not an array')
        elif len(main_entity) == 0:
            custom_issues.append('mainEntity is empty')
        else:
            for i, q in enumerate(main_entity):
                q_types = q.get('@type') if isinstance(q, dict) else None
                if not isinstance(q_types, list):
                    q_types = [q_types]
                if not isinstance(q, dict):
                    custom_issues.append(f'mainEntity[{i}] is not an object')
                elif 'Question' not in q_types:
                    custom_issues.append(f'mainEntity[{i}] has @type={q.get("@type")}, expected Question')
                elif not q.get('name'):
                    custom_issues.append(f'mainEntity[{i}] missing name')
                else:
                    ans = q.get('acceptedAnswer')
                    if not ans:
                        custom_issues.append(f'mainEntity[{i}] missing acceptedAnswer')
                    elif isinstance(ans, dict) and not ans.get('text'):
                        custom_issues.append(f'mainEntity[{i}].acceptedAnswer missing text')

    if entity_type == 'BreadcrumbList':
        items = entity.get('itemListElement', [])
        if isinstance(items, list):
            expected_pos = 1
            for i, item in enumerate(items):
                if isinstance(item, dict):
                    pos = item.get('position')
                    # Coerce string positions ("1") — common and Google-tolerated
                    try:
                        pos = int(pos)
                    except (TypeError, ValueError):
                        pass
                    if pos != expected_pos:
                        custom_issues.append(f'itemListElement[{i}] position={item.get("position")}, expected {expected_pos}')
                    expected_pos += 1

    # Determine overall status
    if missing_required or missing_google_req or custom_issues:
        status = 'invalid'
    elif missing_recommended:
        status = 'incomplete'
    else:
        status = 'valid'

    return {
        'type': entity_type,
        'name': entity.get('name') if isinstance(entity.get('name'), str) else None,
        'id': entity.get('@id'),
        'has_id': bool(entity.get('@id')),
        'validation_status': status,
        'missing_required': missing_required,
        'missing_google_required': missing_google_req,
        'missing_recommended': missing_recommended,
        'custom_issues': custom_issues,
    }


def main():
    if len(sys.argv) < 2:
        print(json.dumps({'error': 'Usage: python3 check_schema_completeness.py <URL>'}))
        sys.exit(1)

    url = sys.argv[1]
    html, status, fetch_error = fetch_html(url)

    if not html:
        if status == 403:
            error_msg = 'blocked by server (HTTP 403 — likely bot challenge or WAF), could not audit schema'
        elif status >= 400:
            error_msg = f'could not fetch HTML (HTTP {status})'
        else:
            error_msg = 'could not fetch HTML'
        print(json.dumps({
            'url': url,
            'error': error_msg,
            'http_status': status,
            'fetch_error': fetch_error,
        }))
        sys.exit(0)

    blocks = extract_schema_blocks(html)
    entities = flatten_entities(blocks)

    validations = [validate_entity(e) for e in entities]

    # Parse errors
    parse_errors = [b for b in blocks if isinstance(b, dict) and '__parse_error' in b]

    # Summary
    total = len(validations)
    invalid = [v for v in validations if v['validation_status'] == 'invalid']
    incomplete = [v for v in validations if v['validation_status'] == 'incomplete']
    valid = [v for v in validations if v['validation_status'] == 'valid']
    no_spec = [v for v in validations if v['validation_status'] == 'no_spec']
    without_id = [v for v in validations if not v['has_id']]

    # Build checks dict
    checks = {}

    # Check 1: parsing succeeded for all schema blocks
    if parse_errors:
        checks['all_schema_blocks_parse'] = {
            'status': 'fail',
            'evidence': f'{len(parse_errors)} of {len(blocks)} JSON-LD block(s) failed to parse as valid JSON.',
            'detail': {'parse_error_count': len(parse_errors), 'errors': [p.get('__parse_error') for p in parse_errors]}
        }
    else:
        checks['all_schema_blocks_parse'] = {
            'status': 'pass',
            'evidence': f'All {len(blocks)} JSON-LD block(s) parse as valid JSON.',
            'detail': {'block_count': len(blocks)}
        }

    # Check 2: at least one schema entity present
    if total == 0:
        checks['schema_entities_present'] = {
            'status': 'fail',
            'evidence': 'No schema entities found on the page.',
            'detail': {}
        }
        print(json.dumps({
            'url': url,
            'schema_summary': {
                'total_entities': 0,
                'total_blocks': len(blocks),
            },
            'checks': checks,
        }, indent=2))
        sys.exit(0)
    else:
        checks['schema_entities_present'] = {
            'status': 'pass',
            'evidence': f'{total} schema entities found across {len(blocks)} JSON-LD block(s). Types: {sorted(set(v["type"] for v in validations))}.',
            'detail': {'entity_count': total, 'block_count': len(blocks)}
        }

    # Check 3: no invalid entities (missing REQUIRED fields or custom issues)
    if invalid:
        invalid_summary = []
        for v in invalid:
            issues = []
            if v['missing_required']:
                issues.append(f'missing required: {v["missing_required"]}')
            if v['missing_google_required']:
                issues.append(f'missing Google-required: {v["missing_google_required"]}')
            if v.get('custom_issues'):
                issues.append(f'custom: {v["custom_issues"][:3]}')
            invalid_summary.append(f'{v["type"]} ({", ".join(issues)})')

        checks['no_invalid_entities'] = {
            'status': 'fail',
            'evidence': f'{len(invalid)}/{total} entities invalid: {"; ".join(invalid_summary[:5])}',
            'detail': {'invalid_entities': invalid}
        }
    else:
        checks['no_invalid_entities'] = {
            'status': 'pass',
            'evidence': f'All {total} entities have required fields.',
            'detail': {}
        }

    # Check 4: entities have @id for cross-referencing
    if len(without_id) == total:
        checks['schema_id_coverage'] = {
            'status': 'fail',
            'evidence': f'0 of {total} entities have @id fragments.',
            'detail': {'without_id': [{'type': v['type'], 'name': v['name']} for v in without_id]}
        }
    elif len(without_id) > total / 2:
        checks['schema_id_coverage'] = {
            'status': 'warn',
            'evidence': f'{len(without_id)}/{total} entities lack @id fragments.',
            'detail': {'without_id': [{'type': v['type'], 'name': v['name']} for v in without_id]}
        }
    elif len(without_id) > 0:
        checks['schema_id_coverage'] = {
            'status': 'warn',
            'evidence': f'{len(without_id)}/{total} entities lack @id fragments.',
            'detail': {'without_id': [{'type': v['type'], 'name': v['name']} for v in without_id]}
        }
    else:
        checks['schema_id_coverage'] = {
            'status': 'pass',
            'evidence': f'All {total} entities have @id fragments.',
            'detail': {}
        }

    # Check 5: recommended fields coverage
    if incomplete:
        incomplete_summary = [f'{v["type"]} missing: {v["missing_recommended"][:5]}' for v in incomplete]
        checks['recommended_fields_coverage'] = {
            'status': 'warn',
            'evidence': f'{len(incomplete)}/{total} entities have required fields but missing recommended: {"; ".join(incomplete_summary[:3])}',
            'detail': {'incomplete_entities': incomplete}
        }
    else:
        checks['recommended_fields_coverage'] = {
            'status': 'pass' if valid else 'na',
            'evidence': f'{len(valid)}/{total} entities have all recommended fields.',
            'detail': {}
        }

    # Check 6: unknown types (no spec)
    if no_spec:
        unknown_types = sorted(set(v['type'] for v in no_spec))
        checks['known_schema_types'] = {
            'status': 'warn',
            'evidence': f'{len(no_spec)} entities have types not in validator spec: {unknown_types}. Not validated for completeness.',
            'detail': {'unknown_types': unknown_types}
        }

    # Return full output
    result = {
        'url': url,
        'schema_summary': {
            'total_blocks': len(blocks),
            'total_entities': total,
            'valid': len(valid),
            'incomplete': len(incomplete),
            'invalid': len(invalid),
            'unknown_types': len(no_spec),
            'entities_with_id': total - len(without_id),
            'parse_errors': len(parse_errors),
            'entity_types_found': sorted(set(v['type'] for v in validations)),
        },
        'validations': validations,
        'checks': checks,
    }

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
