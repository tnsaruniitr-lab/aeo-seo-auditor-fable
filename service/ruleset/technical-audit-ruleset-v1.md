# Technical Audit Ruleset — Portable Export v1.0

**Date:** 2026-04-21
**Source:** website-seo-aeo-auditor v3 (unified build at `~/Desktop/aeo-seo-auditor/skill-unified/`)
**Purpose:** Drop-in ruleset for building a blog auditor, content QA tool, or any project that needs the same SEO/AEO/GEO technical knowledge without reinventing it.

This directory is the **portable extract** of the knowledge, field specs, and validators that power the website auditor. Designed to be cloned/downloaded and consumed by another project (e.g., a blog-specific auditor, a content ops pipeline, a pre-publish QA tool).

---

## What's in this export

Four files. Each is self-contained and reusable.

| File | Size | Format | Purpose |
|---|---|---|---|
| `technical-audit-ruleset-v1.md` | this file | Markdown | Hub doc — integration guide, truth badges, citation tiers, how to consume the other three files |
| `schema-specs.json` | ~10KB | JSON | Per-`@type` Schema.org field requirements (37 types) + custom validation rules |
| `brain-mappings.json` | ~15KB | JSON | Check-ID → Sieve rule + anti-pattern IDs (the evidence layer) |
| `validators.py` | ~14KB | Python (stdlib only) | Pure-function library: HTML parsing, FAQ detection, SSR classification, hreflang detection, schema extraction, tier calculator |

Total ~40KB. Zero dependencies.

---

## Quick start — port this into a blog auditor in 10 minutes

### Option A — Python consumer

```python
# In your blog auditor project
from validators import (
    visible_text, visible_word_count,
    faq_visible_count, looks_like_question,
    detect_spa_signals, classify_ssr,
    detect_hreflang,
    extract_schema_blocks, flatten_entities,
    validate_entity_fields, load_schema_specs,
    tier_calculator,
)
import json

# Load specs and brain mappings once at startup
schema_specs = load_schema_specs('schema-specs.json')['field_specs']
with open('brain-mappings.json') as f:
    brain_mappings = json.load(f)

# Audit a blog post
html = fetch_blog_html(url)
blocks = extract_schema_blocks(html)
entities = flatten_entities(blocks)

for entity in entities:
    result = validate_entity_fields(entity, schema_specs)
    if result['missing_google_required']:
        # Look up brain evidence for this failure
        check_id = 'D6_required_fields'
        mapping = brain_mappings['mappings'].get(check_id, {})
        rule_ids = mapping.get('rules', [])
        # ... fetch rule text from Sieve, attach as citation
```

### Option B — TypeScript / Node.js consumer

The JSON files are directly consumable. Port `validators.py` to TS — it's pure functions with no Python-specific magic:

```typescript
import schemaSpecs from './schema-specs.json';
import brainMappings from './brain-mappings.json';
// Port the functions from validators.py — 1:1 translation,
// regex patterns identical, same return shapes.
```

### Option C — LLM-as-auditor consumer (fastest to build)

Paste all three files as system context for your LLM-driven blog auditor:

```
System prompt:
"You are a blog auditor. Use the attached schema-specs.json to validate
JSON-LD, the brain-mappings.json to cite evidence for findings, and the
validators.py file as the reference logic for HTML parsing rules.
Return findings with truth_badge + tier + rule_ids matched."
```

The LLM handles HTML inspection; the files give it the validation criteria
and evidence database to cite against.

---

## The three knowledge layers

### Layer 1 — Schema.org per-`@type` field specs (schema-specs.json)

37 types covered: Organization, LocalBusiness, MedicalBusiness, Person, WebSite, WebPage, Article, BlogPosting, NewsArticle, Product, Offer, AggregateOffer, FAQPage, Question, Answer, HowTo, HowToStep, Review, AggregateRating, Rating, Recipe, Event, VideoObject, ImageObject, BreadcrumbList, ListItem, MedicalProcedure, MedicalTherapy, Drug, SoftwareApplication, MobileApplication, Service, ContactPoint, PostalAddress, SearchAction, MedicalClinic, MedicalOrganization.

Each type has three field tiers:
- **required** — Schema.org spec requirements. Missing these = the schema is invalid per Schema.org.
- **google_required** — Google Search Central structured data requirements. Tighter than Schema.org in some cases (e.g., Article requires `author` + `image` per Google, not per Schema.org).
- **recommended** — Best-practice fields. Missing these shouldn't fail a check; they should surface as warnings.

Plus 7 **custom validation rules** that go beyond per-field presence:

| Rule | Applies to | Check |
|---|---|---|
| `faqpage_mainentity_is_array_of_questions` | FAQPage | mainEntity must be array; every item has @type=Question + name + acceptedAnswer.text |
| `faqpage_schema_matches_visible_count` | FAQPage | Number of Question entities must equal visible FAQ pairs on the page |
| `breadcrumblist_sequential_positions_from_one` | BreadcrumbList | positions must be 1, 2, 3, ... sequential |
| `every_entity_has_stable_id` | all | Every entity should carry `@id` for cross-page entity graph |
| `datemodified_not_current_timestamp` | Article/BlogPosting/NewsArticle/WebPage | dateModified must not equal Date.now() within 60s |
| `datemodified_iso_8601_with_time` | Article/BlogPosting/NewsArticle/WebPage | Full ISO 8601 with time, not date-only |
| `person_ymyl_has_credential_or_sameas` | Person | On YMYL pages, Person must have hasCredential or sameAs |

### Layer 2 — Sieve brain mappings (brain-mappings.json)

~40 check-ID mappings that link each deterministic check to a set of Sieve rule IDs and anti-pattern IDs. The auditor uses these to attach authoritative citations to every finding.

Example mapping shape:

```json
"F1_first_paragraph_answers_query": {
  "category": "aeo_extraction",
  "rules": [1448, 1471, 1472],
  "anti_patterns": [3, 774, 797, 4698],
  "notes": "Rule 1448 (Google, 0.95) 'Answer-first structure for AI Overview citation.' Rule 1471 (Perplexity, 0.97) 'Lead with Direct Answer.' AP#4698 (Backlinko, high) 'Burying the Answer.'"
}
```

At audit time: for each failing check, look up its mapping, fetch the rule and AP text from your Sieve database, attach as evidence in the finding. If the mapping has no entries, fall back to static criteria alone.

### Layer 3 — Validators library (validators.py)

Pure functions, no state. The building blocks that turn raw HTML into auditable observations:

| Function | What it does |
|---|---|
| `visible_text(html)` | Returns visible body text (skips script/style/noscript/head). Stdlib html.parser — handles malformed HTML gracefully. |
| `visible_word_count(html)` | Count visible words. Used for thin-content detection. |
| `faq_visible_count(html)` | Count visible FAQ pairs using 5 detection patterns with question-intent gating. Does NOT count country expanders, nav toggles, etc. |
| `looks_like_question(text)` | Heuristic for whether text looks like a user-facing question. |
| `detect_spa_signals(html)` | Detect SPA framework markers (Angular app-root, Next.js __next_f, React root, Vue, Nuxt). |
| `classify_ssr(...)` | 6-class SSR/SPA classification. Returns `spa_no_ssr` / `ssr_shell_js_hidden_content` / `js_dependent` / `minimal_content` / `partial_ssr` / `fully_accessible`. |
| `detect_hreflang(html)` | Detect hreflang in both top-level `<link>` tags AND Next.js streaming data. Catches the false-negative on App Router sites. |
| `extract_schema_blocks(html)` | Extract + parse all JSON-LD blocks. Returns parsed objects; unparseable blocks marked with `__parse_error`. |
| `flatten_entities(blocks)` | Flatten nested + @graph entities into a single list of entities (max depth 5). |
| `validate_entity_fields(entity, specs)` | Compare one entity against its @type's required/google_required/recommended fields. |
| `tier_calculator(evidence)` | Compute HIGH / MID / LOW tier for authority/social evidence cells. |

---

## Truth badges — every finding gets one

Whenever your blog auditor produces a finding, tag it with the appropriate truth badge so consumers know how much to trust it:

| Badge | Meaning | Example |
|---|---|---|
| `HARD_EVIDENCE` | Direct observable fact from curl response or page HTML. Binary. | HTTP status, tag presence/absence, exact char count. |
| `MEASURED` | Quantitative value from deterministic measurement. | TTFB median, visible word count, schema entity count. |
| `STATIC_RULE` | Pass/fail against a pre-defined criterion. No brain lookup. | "Meta description > 160 chars = warn" |
| `COMPARATIVE` | Finding based on competitor comparison. | "Competitor has 10 FAQs; you have 0." |
| `HEURISTIC` | Directional assessment, likely but not provably correct. | "H1 doesn't describe brand value prop." |
| `MODEL_JUDGMENT` | LLM-authored quality assessment. Subjective. | "Intro paragraph buries the lede." |

**The rule:** never mix tiers in the top-line score. Scores should be computed only from HARD_EVIDENCE + MEASURED + STATIC_RULE. HEURISTIC and MODEL_JUDGMENT findings appear as separate directional signals in the report.

---

## Citation tier system — source authority for findings

Every Sieve rule or external citation gets a tier icon based on source authority:

| Tier | Icon | Sources | Framing |
|---|---|---|---|
| Tier 1 — Primary | 🥇 | Google, Schema.org, Perplexity, Bing, Microsoft, W3C, Apple | "Per [org]'s official documentation" |
| Tier 2 — Research | 🥈 | Backlinko, Ahrefs, Semrush, Princeton/arXiv, Vercel, BrightEdge | "Per [org]'s research study" |
| Tier 3 — Industry | 🥉 | Search Engine Land, Search Engine Journal, Moz, HubSpot | "Per industry analysis at [org]" |
| Tier 4 — Specialized | 📎 | amsive.com, almcorp.com, cxl.com, seerinteractive.com, Y Combinator | "According to [org]" |

When multiple sources back one finding, surface them grouped by tier (Tier 1 first). Maximum 3 citations per finding.

---

## What's NOT in this export (and why)

Deliberate exclusions — these belong in the website auditor only, not a blog auditor:

- **Sitemap validation logic** — site-level, not per-post
- **robots.txt + 16-bot crawl policy** — site-level
- **TTFB / Core Web Vitals measurement** — site-level infrastructure
- **Per-bot UA cloaking detection** — site-level
- **Competitor SERP discovery + crawl** — different scope for blog comparison
- **Orchestrator with 5 parallel subprocess calls** — overkill for a single post
- **Supabase `website_audits` persistence layer** — different data model for blog tools

If you genuinely need any of these in your blog auditor, they're available in the full website auditor at the same source repo. Copy only what you need.

---

## Update cadence

This export is a snapshot of v3 at 2026-04-21. The authoritative versions live in:
- `~/Desktop/aeo-seo-auditor/skill-unified/` (local)
- `https://github.com/tnsaruniitr-lab/aeo-seo-auditor/tree/main/skill-unified` (pushed)

When the website auditor updates its schema specs or brain mappings:
1. Re-run the export (regenerate these files from the source)
2. Bump the version number
3. Update consumers

Don't let consumers diverge from the source — that's how the "each team has a different view of what a valid FAQPage looks like" pain starts.

---

## Blog-auditor-specific additions (add these on top of this export)

The website auditor doesn't cover these; your blog auditor should:

### Content quality dimensions (Layer 4 — new, blog-specific)

| Dimension | What to measure |
|---|---|
| Intro hook strength | Does the first 150 words compel the reader to continue? |
| Entity definition compliance | Does paragraph 1 match "X is a Y that does Z" for AI extraction? |
| Section standalone-ness | Can each H2 section be quoted alone as a complete answer? |
| Specificity density | Named entities + numbers + dated facts per 500 words |
| Original-data presence | First-party claim or data point, or only derivative? |
| Answerability | How many discrete Q/A pairs can be extracted from the post? |
| Internal link relevance | Strategic linking to other posts, not just pile-on |
| Citation graph | Links to authoritative external sources |
| Freshness of facts | Are dated claims still true? |
| Voice consistency with brand | Brand-specific rubric (e.g., tryps-blog-auditor has TRYPS voice rules) |

These are LLM-judgment-heavy. Score them as MODEL_JUDGMENT tier and keep them separate from the technical score.

### BlogPosting-specific tightening

The blog auditor should be stricter than the website auditor on:
- `Article` vs `BlogPosting` — prefer BlogPosting for blog content
- `mainEntityOfPage` — should match canonical URL exactly
- `articleBody` — should be populated for long-form posts
- `wordCount` — should match actual visible word count
- `headline` — should match `<title>` minus site name suffix (not be a duplicate of h1 verbatim)
- `alternativeHeadline` — optional but useful for A/B testing

---

## Integration cookbook

### Recipe 1 — Validate a blog post's schema

```python
from validators import extract_schema_blocks, flatten_entities, validate_entity_fields, load_schema_specs

specs = load_schema_specs('schema-specs.json')['field_specs']
custom_rules = load_schema_specs('schema-specs.json')['custom_validation_rules']

html = fetch_blog(url)
entities = flatten_entities(extract_schema_blocks(html))

findings = []
for entity in entities:
    result = validate_entity_fields(entity, specs)
    if result['missing_required']:
        findings.append({
            'type': entity['@type'],
            'issue': f"Missing required fields: {result['missing_required']}",
            'severity': 'high',
            'truth_badge': 'HARD_EVIDENCE',
        })
    if not result['has_id']:
        findings.append({
            'type': entity['@type'],
            'issue': f"Missing @id fragment (required for entity graph)",
            'severity': 'medium',
            'truth_badge': 'HARD_EVIDENCE',
        })
```

### Recipe 2 — FAQ schema/visible match

```python
from validators import faq_visible_count, faq_schema_count

visible_count, method = faq_visible_count(html)
schema_count = faq_schema_count(html)

if schema_count != visible_count:
    findings.append({
        'issue': f'FAQ mismatch: {schema_count} in schema, {visible_count} visible (via {method})',
        'severity': 'high',
        'truth_badge': 'HARD_EVIDENCE',
        'rule_ids': [1495, 1496, 7375],
        'anti_pattern_ids': [813, 876],
        'source': '🥇 Schema.org — "Schema Markup Must Match Visible Content"',
    })
```

### Recipe 3 — Attach brain evidence to a finding

```python
import json

with open('brain-mappings.json') as f:
    mappings = json.load(f)['mappings']

def attach_evidence(finding, check_id):
    mapping = mappings.get(check_id, {})
    finding['rule_ids'] = mapping.get('rules', [])
    finding['anti_pattern_ids'] = mapping.get('anti_patterns', [])
    finding['notes'] = mapping.get('notes', '')
    return finding

finding = attach_evidence({
    'issue': 'dateModified matches current timestamp (cosmetic)',
    'truth_badge': 'HARD_EVIDENCE',
}, 'D11_datepublished_datemodified')
# → adds rule_ids: [1474], anti_pattern_ids: [799]
```

### Recipe 4 — Tier an authority/social evidence cell

```python
from validators import tier_calculator

cell = {
    'profile_url': 'https://g2.com/products/trypsagent',
    'profile_verified': True,
    'primary_metric_value': 47,
    'sample_evidence': [{'text': '...', 'source_url': 'https://...'}],
    'latest_activity_date': '2026-04-15T10:00:00+00:00',
}
tier = tier_calculator(cell)  # 'HIGH' / 'MID' / 'LOW'
```

---

## Known limits of this ruleset

Things this doesn't cover — worth flagging so consumers don't assume:

1. **No brand-voice rules.** Every brand is different. Add your own rubric layer.
2. **No domain-authority scoring.** Requires external APIs (Ahrefs, Moz). Not in scope.
3. **No live AI engine citation testing.** Would require ChatGPT/Claude/Perplexity API calls per query. Separate module.
4. **No multi-locale content-quality tests.** Focuses on English. Add locale-specific adjustments.
5. **No YMYL calibration by sub-category.** "Medical" is one bucket; real YMYL has dozens of niches. Tune per brand.
6. **Tier calculator assumes at least one verified URL.** Without that, everything is LOW. That's intentional — unverified = uncited.

---

## Versioning

- **v1.0** — initial export, 2026-04-21
  - Schema specs for 37 types
  - Brain mappings for ~40 checks
  - Validators library (12 functions)
  - Integration guide

Plan to bump minor version when specs or mappings are added. Bump major version for breaking changes to function signatures or schema field semantics.

---

## License and usage

This ruleset is a knowledge extract, not application code. It's safe to:
- Commit into your own project
- Modify for your brand/vertical
- Redistribute internally

If you update the ruleset and discover new patterns worth sharing, push them back to the source repo so both the website auditor and downstream blog auditors benefit.

---

## Support

The source-of-truth live skill is at `~/Documents/New project/.claude/skills/website-seo-aeo-auditor/` (symlinked to the unified build). When you audit a blog post and notice a rule that's missing from this export, that's the place to add it first; then re-export.

The public repo with full history + test suite: https://github.com/tnsaruniitr-lab/aeo-seo-auditor

---

*Generated 2026-04-21 from website-seo-aeo-auditor v3 (unified build).*
