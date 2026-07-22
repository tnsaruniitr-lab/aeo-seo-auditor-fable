# Check Definitions — 103 Checks (v1.3)

Source of truth for every audit check. Each check has:
- **ID**: Category letter + number (A1, B1, ... J4)
- **Name**: Short title
- **Method**: How to verify
- **Truth Badge**: HARD EVIDENCE / MEASURED / STATIC RULE / COMPARATIVE / HEURISTIC / MODEL JUDGMENT
- **Weight**: 1 (minor) / 2 (moderate) / 3 (critical)
- **Severity on fail**: critical / high / medium / low
- **Fix Type**: page_html / schema / content_restructure / sitewide_template / cms_constraint / offpage_entity / cannot_fix_from_page
- **Page types**: Which page types this applies to (all = universal)

## Truth Badge Definitions

| Badge | Meaning | Reliability |
|---|---|---|
| **HARD EVIDENCE** | Binary fact from HTML/headers/schema — exists or doesn't | 100% deterministic |
| **MEASURED** | Actual measurement from tools (timing, counts) | Deterministic but may vary by run |
| **STATIC RULE** | Fixed criteria backed by research data | Deterministic threshold, research-backed |
| **COMPARATIVE** | Relative to competitors — changes with SERP results | Deterministic comparison, variable inputs |
| **HEURISTIC** | Pattern matching — may have edge cases | Usually correct, some false positives |
| **MODEL JUDGMENT** | LLM interpretation — inherently variable | Directional, ~80% consistent between runs |

---

## Category A: Technical SEO (12 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| A1 | HTTPS enforcement | HARD EVIDENCE | URL scheme + HSTS header | 3 | critical | sitewide_template |
| A2 | Title tag present, 50-60 chars | HARD EVIDENCE | Extract `<title>`, count chars | 3 | high | page_html |
| A3 | Meta description present, 120-160 chars | HARD EVIDENCE | Extract meta description, count chars | 3 | high | page_html |
| A4 | Canonical tag present and self-referencing | HARD EVIDENCE | Extract `<link rel="canonical">`, compare to URL | 3 | critical | page_html |
| A5 | Robots meta allows indexing | HARD EVIDENCE | Check meta robots for noindex | 3 | critical | page_html |
| A6 | Exactly one H1 tag | HARD EVIDENCE | Count H1 elements | 2 | high | page_html |
| A7 | H1 contains primary keyword | HEURISTIC | Compare H1 text to title/query keywords | 2 | medium | content_restructure |
| A8 | HTML lang attribute present | HARD EVIDENCE | Check `<html lang>` | 1 | low | page_html |
| A9 | Viewport meta tag | HARD EVIDENCE | Check meta viewport | 2 | high | page_html |
| A10 | robots.txt allows crawling | HARD EVIDENCE | Parse robots.txt Disallow rules for page path | 3 | critical | sitewide_template |
| A11 | Sitemap referenced | HARD EVIDENCE | Check robots.txt Sitemap: + link rel sitemap | 2 | medium | sitewide_template |
| A12 | Page renders without JS dependency | MEASURED | WebFetch content volume vs Chrome (if available) | 2 | high | cms_constraint |

---

## Category B: Performance (10 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| B1 | TTFB < 800ms (AI), < 3s (general) | MEASURED | Chrome timing or WebFetch response | 3 | high | sitewide_template |
| B2 | No render-blocking resources | HARD EVIDENCE | Script without async/defer, stylesheet in head | 2 | medium | page_html |
| B3 | Images use modern formats | HARD EVIDENCE | Check img src extensions for webp/avif | 2 | medium | sitewide_template |
| B4 | Lazy loading on below-fold images | HARD EVIDENCE | Check loading="lazy" on images after first 2 | 1 | low | page_html |
| B5 | CSS/JS minified (no large inline) | HARD EVIDENCE | Check inline style/script blocks > 5KB | 1 | low | sitewide_template |
| B6 | DOM depth not excessive (< 15) | MEASURED | Chrome MCP DOM analysis | 1 | low | cms_constraint |
| B7 | Gzip/Brotli compression | HARD EVIDENCE | Content-Encoding header | 2 | medium | sitewide_template |
| B8 | Cache-Control present | HARD EVIDENCE | Cache-Control header with max-age | 1 | low | sitewide_template |
| B9 | No mixed content | HARD EVIDENCE | Scan for http:// in src/href on HTTPS page | 2 | high | page_html |
| B10 | Core Web Vitals indicators | MEASURED | Chrome MCP performance API (LCP, CLS, INP) | 3 | high | sitewide_template |
| B11 | Image dimensions specified | HARD EVIDENCE | Check img tags for width/height attributes | 2 | medium | page_html |

---

## Category C: On-Page SEO (13 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| C1 | Heading hierarchy logical | HARD EVIDENCE | Trace H1→H2→H3, flag skipped levels | 2 | medium | content_restructure |
| C2 | Primary keyword in first 100 words | HEURISTIC | Extract first 100 body words, check keyword | 3 | high | content_restructure |
| C3 | >= 3 internal links | HARD EVIDENCE | Count same-domain anchors | 2 | medium | content_restructure |
| C4 | Descriptive anchor text | HEURISTIC | Flag "click here", "read more", "here", "link" | 2 | medium | content_restructure |
| C5 | All images have alt text | HARD EVIDENCE | Count img without alt or alt="" | 2 | medium | page_html |
| C6 | Sufficient word count | COMPARATIVE | Word count vs page-type threshold AND competitor median | 2 | medium | content_restructure |
| C7 | No keyword stuffing (< 4%) | HEURISTIC | Primary keyword occurrences / total words | 2 | medium | content_restructure |
| C8 | Outbound links to authoritative sources | HARD EVIDENCE | Count external links in body | 1 | low | content_restructure |
| C9 | URL clean and descriptive | HARD EVIDENCE | Path length < 75, hyphens, no IDs | 1 | low | sitewide_template |
| C10 | Open Graph tags complete | HARD EVIDENCE | Check og:title, og:description, og:image, og:type | 2 | medium | page_html |
| C11 | Twitter Card tags present | HARD EVIDENCE | Check twitter:card, twitter:title | 1 | low | page_html |
| C12 | Visible publication/update date | HEURISTIC | Scan content for date patterns + check schema dates | 2 | medium | page_html |
| C14 | No broken external links | MEASURED | WebFetch each outbound link, check for 4xx/5xx | 2 | medium | content_restructure |

---

## Category D: Schema / Structured Data (13 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| D1 | JSON-LD block present | HARD EVIDENCE | Check for script type="application/ld+json" | 3 | high | schema |
| D2 | @context is https://schema.org | HARD EVIDENCE | Validate @context field | 2 | medium | schema |
| D3 | Page-appropriate @type | STATIC RULE | Check @type vs page classification (schema-validation.md) | 3 | high | schema |
| D4 | @id with unique fragment URL | HARD EVIDENCE | Check @id exists with URL#fragment | 2 | medium | schema |
| D5 | BreadcrumbList (non-homepage) | HARD EVIDENCE | Check for BreadcrumbList @type | 2 | medium | schema |
| D6 | Required fields per @type | STATIC RULE | Cross-ref vs schema-validation.md required list | 3 | high | schema |
| D7 | Recommended fields per @type | STATIC RULE | Cross-ref vs schema-validation.md recommended list | 2 | medium | schema |
| D8 | Organization/WebSite schema | HARD EVIDENCE | Check for Organization or WebSite @type | 2 | medium | schema |
| D9 | FAQPage if FAQ exists + no promo in answers | HARD EVIDENCE | If HTML has Q&A → check FAQPage schema + answer quality (no promotional, not truncated) | 2 | high | schema |
| D10 | Image fields use ImageObject | HARD EVIDENCE | Check schema image fields for @type ImageObject | 1 | low | schema |
| D11 | datePublished/dateModified ISO 8601 | HARD EVIDENCE | Validate date formats in schema | 2 | medium | schema |
| D12 | Author with Person type and name | HARD EVIDENCE | Check author field for Person @type + name | 2 | medium | schema |
| D13 | speakable property present | HARD EVIDENCE | Check for speakable with cssSelector | 2 | medium | schema |

---

## Category E: AEO — Discovery (10 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| E1 | PerplexityBot allowed | HARD EVIDENCE | robots.txt User-agent: PerplexityBot rules | 3 | critical | sitewide_template |
| E2 | BingPreview allowed | HARD EVIDENCE | robots.txt BingPreview rules | 3 | critical | sitewide_template |
| E3 | GoogleBot allowed | HARD EVIDENCE | robots.txt Googlebot rules | 3 | critical | sitewide_template |
| E4 | No nosnippet/max-snippet:0 | HARD EVIDENCE | Meta robots nosnippet check | 3 | critical | page_html |
| E5 | Content in raw HTML | MEASURED | WebFetch content volume vs Chrome rendered | 3 | high | cms_constraint |
| E6 | Content not behind JS accordions | HEURISTIC | Check details/summary HTML fallback presence | 2 | medium | page_html |
| E7 | IndexNow or ping mechanism | HARD EVIDENCE | Check for IndexNow key file | 1 | low | sitewide_template |
| E8 | Page in XML sitemap | HARD EVIDENCE | Cross-ref page URL vs sitemap URLs | 2 | medium | sitewide_template |
| E9 | Bing Webmaster verification | HARD EVIDENCE | Check msvalidate.01 meta tag | 1 | low | page_html |
| E10 | ClaudeBot/ChatGPT-User/Applebot allowed | HARD EVIDENCE | robots.txt user-agent rules | 2 | high | sitewide_template |
| E11 | Content not behind paywall/login | HEURISTIC | Check for login gates, paywall indicators, authentication requirements | 2 | high | cms_constraint |
| E12 | No NOARCHIVE on AI-targeted pages | HARD EVIDENCE | Check meta robots and X-Robots-Tag for noarchive directive | 2 | high | page_html |
| E13 | CCBot / LLM training crawler allowed | HARD EVIDENCE | Check robots.txt for CCBot Disallow or wildcard blocks | 2 | medium | sitewide_template |
| E14 | llms.txt present | HARD EVIDENCE | Fetch https://domain/llms.txt — pass requires HTTP 200 AND a non-HTML, text/markdown-shaped body (soft-200 HTML shell rejected; first ~100 chars recorded as evidence). Measured 2.4× citation lift | 2 | medium | sitewide_template |

---

## Category F: AEO — Extraction (12 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| F1 | First paragraph answers query | STATIC RULE | First 150 words must contain: entity name + category + function in "X is a Y that does Z" format | 3 | high | content_restructure |
| F2 | Quick-answer block near H1 | HEURISTIC | Check for summary/callout within 200 words of H1 | 3 | high | content_restructure |
| F3 | FAQ section >= 3 Q&A pairs | HARD EVIDENCE | Count Q&A pairs in HTML/schema | 3 | high | content_restructure |
| F4 | FAQ uses semantic markup | HARD EVIDENCE | Check details/summary or clear Q+A structure | 2 | medium | page_html |
| F5 | FAQ questions natural language | HEURISTIC | Questions start with How/What/When/Why/Is/Can | 2 | medium | content_restructure |
| F6 | Headings as questions/answers | HEURISTIC | H2/H3 text has question marks or direct-answer phrasing | 2 | medium | content_restructure |
| F7 | Named entities (not vague pronouns) | STATIC RULE | Count entity names vs "it","they","our","your" in first 300 words. Ratio > 3:1 = pass | 2 | medium | content_restructure |
| F8 | Specific facts (numbers, dates, prices) | HEURISTIC | Regex count: quantities, dates, currency, percentages | 2 | medium | content_restructure |
| F9 | Definition-first writing | STATIC RULE | First sentence matches "X is a Y" or "X is [article] Y that" pattern | 2 | medium | content_restructure |
| F10 | Summary/TL;DR at end | HEURISTIC | Scan last 20% of content for summary section | 2 | medium | content_restructure |
| F11 | Self-contained answer units | MODEL JUDGMENT | Each H2 section independently comprehensible if extracted alone | 2 | medium | content_restructure |
| F12 | Tables/lists for comparative data | HARD EVIDENCE | Count table + ol/ul elements in body | 1 | low | content_restructure |

---

## Category G: AEO — Trust (8 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| G1 | Author byline visible | HEURISTIC | Scan visible content for author name pattern | 2 | medium | content_restructure |
| G2 | Author schema with credentials/sameAs | HARD EVIDENCE | Person schema has sameAs or jobTitle | 2 | medium | schema |
| G3 | Outbound citations to authoritative sources | HARD EVIDENCE | Count links to .gov/.edu/academic/official domains | 3 | high | content_restructure |
| G4 | Publication date visible AND in schema | HARD EVIDENCE | Visible date on page + datePublished in schema | 2 | medium | page_html |
| G5 | dateModified visible AND in schema | HARD EVIDENCE | Visible "Updated" text + dateModified in schema | 2 | high | page_html |
| G6 | Organization schema with sameAs | HARD EVIDENCE | Organization @type has sameAs array | 2 | medium | schema |
| G7 | Privacy/terms accessible | HARD EVIDENCE | Footer links containing privacy/terms/legal | 1 | low | page_html |
| G8 | HTTPS valid | HARD EVIDENCE | Page loads over HTTPS | 2 | high | sitewide_template |
| G9 | Content freshness recency | HARD EVIDENCE + MODEL JUDGMENT | dateModified age: < 90d pass, 90-365d warn, > 365d fail. Cosmetic update detection via LLM | 3 | high | content_restructure |

---

## Category H: AEO — Selection (8 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| H1 | Content depth vs competitors | COMPARATIVE | Word count + topic coverage vs top 5 SERP | 3 | high | content_restructure |
| H2 | Unique data/research | MODEL JUDGMENT | Compare content for original vs derivative info | 3 | high | content_restructure |
| H3 | FAQ coverage vs competitors | COMPARATIVE | FAQ pair count vs competitor pages | 2 | medium | content_restructure |
| H4 | Schema completeness vs competitors | COMPARATIVE | Schema type count vs competitor pages | 2 | medium | schema |
| H5 | Fresher content than competitors | COMPARATIVE | dateModified recency vs competitors | 2 | high | page_html |
| H6 | E-E-A-T signals vs competitors | COMPARATIVE | Author, citations, credentials comparison | 2 | medium | offpage_entity |
| H7 | Appears in AI Overview | MEASURED | WebSearch result analysis for AI snippet | 3 | high | cannot_fix_from_page |
| H8 | Content matches query intent | MODEL JUDGMENT | Content-query alignment vs competitors | 3 | high | content_restructure |

---

## Category I: GEO (8 checks)

| ID | Name | Truth Badge | Dimension | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|---|
| I1 | Brand in category queries | MODEL JUDGMENT | Presence | WebSearch "best [category]" | 3 | high | offpage_entity |
| I2 | Knowledge panel/entity card | MEASURED | Presence | WebSearch brand name | 2 | medium | offpage_entity |
| I3 | AI description matches positioning | MODEL JUDGMENT | Accuracy | WebSearch vs site description | 3 | high | offpage_entity |
| I4 | No outdated/incorrect AI info | MODEL JUDGMENT | Accuracy | WebSearch vs current site | 2 | medium | offpage_entity |
| I5 | Brand sentiment positive/neutral | MODEL JUDGMENT | Favorability | WebSearch result sentiment | 2 | medium | offpage_entity |
| I6 | Brand recommended over competitors | MODEL JUDGMENT | Favorability | WebSearch category ranking | 2 | medium | offpage_entity |
| I7 | Consistent entity across sources | MODEL JUDGMENT | Accuracy | Cross-ref web search results | 2 | medium | offpage_entity |
| I8 | sameAs links to authoritative profiles | HARD EVIDENCE | Presence | JSON-LD sameAs + WebFetch verify | 2 | medium | schema |

---

## Category J: Entity Consistency (4 checks)

| ID | Name | Truth Badge | Method | Weight | Severity | Fix Type |
|---|---|---|---|---|---|---|
| J1 | Organization name consistent | HARD EVIDENCE | Cross-ref schema/OG/title/footer | 2 | medium | sitewide_template |
| J2 | Logo consistent | HARD EVIDENCE | Cross-ref schema/OG/favicon | 1 | low | sitewide_template |
| J3 | URL/domain consistent | HARD EVIDENCE | Cross-ref canonical/schema @id/OG url | 2 | medium | page_html |
| J4 | sameAs URLs resolve | MEASURED | WebFetch each sameAs URL | 2 | medium | schema |

---

## Summary by Truth Badge

| Truth Badge | Count | Percentage |
|---|---|---|
| HARD EVIDENCE | 59 | 57% |
| MEASURED | 9 | 9% |
| STATIC RULE | 6 | 6% |
| COMPARATIVE | 6 | 6% |
| HEURISTIC | 15 | 15% |
| MODEL JUDGMENT | 8 | 8% |
| **Total** | **103** | **100%** |

**83% of checks are deterministic or near-deterministic** (HARD EVIDENCE + MEASURED + STATIC RULE + COMPARATIVE + HEURISTIC). Only 8% are genuinely subjective (MODEL JUDGMENT).

Note: G9 counts as HARD EVIDENCE for date recency + MODEL JUDGMENT for cosmetic update detection.

## Summary by Fix Type

| Fix Type | Count | Who Does It | Timeline |
|---|---|---|---|
| page_html | 26 | Developer, 5-15 min per fix | Immediate |
| schema | 16 | Developer, 15-30 min per fix | Immediate |
| content_restructure | 29 | Content writer, 1-2 hours | Days |
| sitewide_template | 18 | Developer, 30-60 min | Days |
| cms_constraint | 4 | Platform decision | Varies |
| offpage_entity | 8 | Marketing team, 4-8 weeks | Months |
| cannot_fix_from_page | 2 | Org-level investment | Months-Quarters |
| **Total** | **103** | | |
