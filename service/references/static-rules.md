# Static Rules — The Auditor's Core Brain

Version: 1.3
Last updated: 2026-04-13
Changes: v1.3 — Added E13 (CCBot / LLM training crawler access). Source-first citation format. (103 checks total)

Every check has:
- **Precise pass/fail/warn criteria** — deterministic where possible
- **Research basis** — which study or data point backs the threshold
- **Fix template** — skeleton of the before/after output
- **Brain mapping** — Sieve entry IDs (curated in brain-mappings.md)

---

## Category A: Technical SEO

### A1: HTTPS Enforcement
- **Pass:** URL scheme is `https://` AND response includes `Strict-Transport-Security` header
- **Warn:** URL scheme is `https://` but no HSTS header
- **Fail:** URL scheme is `http://` or page loads over HTTP
- **Research:** Google confirmed HTTPS as ranking signal (2014). HSTS prevents downgrade attacks. All AI crawlers require HTTPS for trust signals.
- **Fix template:** Redirect HTTP → HTTPS at server level. Add header: `Strict-Transport-Security: max-age=31536000; includeSubDomains`

### A2: Title Tag Present, 50-60 Characters
- **Pass:** `<title>` tag exists AND character count 50-60 (inclusive)
- **Warn:** Title exists but 40-49 or 61-70 characters
- **Fail:** No `<title>` tag OR title empty OR title < 40 characters OR title > 70 characters
- **Research:** Google truncates at ~600px (~60 chars). 2026 consensus: 50-60 is optimal. Titles < 50 are usually too generic to differentiate in SERPs. API leak confirmed `titlematchScore`. Pixel width (600px) is the real limit, not character count.
- **Fix template:** `<title>[Primary keyword] — [Brand] | [Differentiator]</title>` (aim for 50-55 chars)

### A3: Meta Description Present, 120-160 Characters
- **Pass:** `<meta name="description">` exists AND content 120-160 characters
- **Warn:** Description exists but < 120 or 161-180 characters
- **Fail:** No meta description OR empty OR > 180 characters
- **Research:** Not a ranking signal (confirmed by Google). Affects CTR — pages with custom descriptions get ~25% higher CTR than auto-generated snippets. Google may rewrite descriptions regardless.
- **Fix template:** `<meta name="description" content="[Entity] is a [category] that [key benefit]. [Proof point or CTA]. [Differentiator].">`

### A4: Canonical Tag Present and Self-Referencing
- **Pass:** `<link rel="canonical" href="[current URL]">` exists AND href matches current page URL (normalized — trailing slashes, www/non-www)
- **Warn:** Canonical exists but points to a different URL on same domain (intentional consolidation possible)
- **Fail:** No canonical tag OR canonical points to different domain OR canonical is empty
- **Research:** Google API leak confirmed `siteAuthority` — split domains fragment this signal. Canonical is the primary signal for URL consolidation. Missing canonical on redirecting domains (e.g., trypsagent.com → jointryps.com) is a critical failure.
- **Fix template:** `<link rel="canonical" href="https://[domain]/[path]/">`

### A5: Robots Meta Allows Indexing
- **Pass:** No `<meta name="robots">` tag (default = index) OR tag contains `index` or `all`
- **Fail:** `<meta name="robots" content="noindex">` or `content="none"`
- **Note:** This is a GATE CHECK — if this fails, Gate 1 triggers and dominates the report
- **Research:** Google Rule #1166 (0.99): "Noindex directive excludes page from Google index regardless of other signals." Also excludes from AI Overviews.
- **Fix template:** Remove `noindex` or change to `<meta name="robots" content="index, follow, max-snippet:-1, max-image-preview:large">`

### A6: Exactly One H1 Tag
- **Pass:** Exactly 1 `<h1>` element in the page
- **Warn:** 0 H1 tags (missing) or 2 H1 tags (might be intentional in HTML5 sectioning)
- **Fail:** 3+ H1 tags
- **Research:** Google has said multiple H1s are "fine" but single H1 is best practice for clarity. The H1 signals the primary topic to search engines and AI extractors.
- **Fix template:** Ensure single `<h1>[Primary page topic]</h1>` early in content

### A7: H1 Contains Primary Keyword
- **Pass:** H1 text contains the primary keyword or a close semantic variant from the target query
- **Warn:** H1 is related but doesn't contain the keyword (e.g., branded tagline)
- **Fail:** H1 is completely unrelated to target query/page topic
- **Research:** API leak: `titlematchScore` tracks title-query relevance. H1 serves similar function for on-page topic signaling. AI extractors use H1 as section anchor.
- **Fix template:** Rewrite H1 to include primary keyword naturally: `<h1>[Keyword-rich descriptive heading]</h1>`

### A8: HTML Lang Attribute
- **Pass:** `<html lang="[valid code]">` present (e.g., `lang="en"`)
- **Fail:** No lang attribute on html tag
- **Research:** Minor signal. Helps search engines and AI systems determine content language. Required for accessibility (WCAG). Google uses visible content for language detection (not lang attr), but absence is a quality gap.
- **Fix template:** `<html lang="en">`

### A9: Viewport Meta Tag
- **Pass:** `<meta name="viewport" content="width=device-width, initial-scale=1">` present
- **Fail:** No viewport meta tag
- **Research:** Required for mobile-first indexing. Google crawls mobile version primarily. Without viewport, page renders as desktop on mobile — poor experience, potential ranking suppression.
- **Fix template:** `<meta name="viewport" content="width=device-width, initial-scale=1">`

### A10: robots.txt Allows Crawling
- **Pass:** robots.txt has no Disallow rule covering the audited page path for Googlebot or wildcard
- **Warn:** robots.txt returns 404 (no file — treated as fully permissive by crawlers, but missing is not intentional)
- **Fail:** robots.txt Disallows the audited page path for Googlebot or wildcard User-agent
- **Note:** This is a GATE CHECK — blocking Googlebot triggers Gate 1
- **Research:** Googlebot Disallow = page will not be indexed. Different from noindex (Disallow prevents crawling; noindex prevents indexing after crawling).
- **Fix template:** Remove the Disallow line or add specific Allow for the page path

### A11: Sitemap Referenced
- **Pass:** robots.txt contains `Sitemap: [URL]` directive OR `<link rel="sitemap" href="...">` in HTML head
- **Warn:** Neither present but sitemap exists at /sitemap.xml
- **Fail:** No sitemap reference anywhere AND no sitemap.xml found
- **Research:** Sitemaps help crawlers discover pages efficiently. Google ignores `priority` and `changefreq` (confirmed Rule #562, 0.99). lastmod is the only useful sitemap signal.
- **Fix template:** Add to robots.txt: `Sitemap: https://[domain]/sitemap.xml`

### A12: Page Renders Without JS Dependency
- **Pass:** WebFetch returns > 500 words of body text (content is in raw HTML)
- **Warn:** WebFetch returns 200-500 words (partial JS dependency)
- **Fail:** WebFetch returns < 200 words of body text (heavily JS-dependent)
- **Note:** This is a GATE CHECK (Gate 2) when combined with Chrome MCP unavailability
- **Research:** GPTBot, PerplexityBot, ClaudeBot do NOT execute JavaScript (Vercel 500M request study: zero JS execution by GPTBot). Content behind JS is invisible to AI crawlers. Google renders JS but with delay.
- **Fix template:** Implement server-side rendering (SSR) or static site generation (SSG)

---

## Category B: Performance

### B1: TTFB Under Threshold
- **Pass (with Lighthouse):** TTFB < 800ms measured
- **Warn (with Lighthouse):** TTFB 800ms-2000ms
- **Fail (with Lighthouse):** TTFB > 2000ms
- **Pass (without Lighthouse):** WebFetch responds within expected timeframe (no timeout)
- **Fail (without Lighthouse):** WebFetch times out or shows significant delay
- **Research:** AI crawlers make one request and move on. TTFB > 800ms risks AI crawler abandonment. Target < 200ms for optimal AI crawler access. Traditional SEO: TTFB > 3s is "Poor" per Google.
- **Fix template:** Optimize server response — caching, CDN, database query optimization, reduce server-side processing

### B2: No Render-Blocking Resources
- **Pass:** No `<script>` tags without `async` or `defer` in `<head>`, no `<link rel="stylesheet">` without `media` queries in `<head>` that block initial render
- **Warn:** 1-2 render-blocking resources
- **Fail:** 3+ render-blocking scripts or stylesheets in head
- **Research:** Render-blocking resources delay LCP. Minor ranking signal through CWV. More important for user experience than rankings.
- **Fix template:** Add `async` or `defer` to script tags. Use `media="print"` with `onload` swap for non-critical CSS.

### B3: Images Use Modern Formats
- **Pass:** All `<img>` src attributes use .webp, .avif, or .svg
- **Warn:** Mix of modern and legacy formats (.jpg, .png)
- **Fail:** All images are legacy formats with no `<picture>` fallback
- **Research:** WebP/AVIF reduce image weight 25-50% vs JPEG/PNG. Reduces page weight, improves LCP. Not a direct ranking signal but contributes to CWV thresholds.
- **Fix template:** Convert images to WebP. Use `<picture>` element for fallback: `<picture><source srcset="img.webp" type="image/webp"><img src="img.jpg"></picture>`

### B4: Lazy Loading on Below-Fold Images
- **Pass:** Images after the first 2 have `loading="lazy"` attribute
- **Warn:** Some below-fold images lack lazy loading
- **Fail:** No lazy loading on any images (and page has 5+ images)
- **N/A:** Page has fewer than 3 images
- **Research:** Lazy loading reduces initial page weight. Do NOT lazy-load above-fold images (hurts LCP). Native `loading="lazy"` supported by all modern browsers.
- **Fix template:** Add `loading="lazy"` to all img tags except the first 1-2 (hero/above-fold)

### B5: No Large Inline CSS/JS Blocks
- **Pass:** No inline `<style>` or `<script>` blocks > 5KB
- **Warn:** 1 inline block > 5KB
- **Fail:** Multiple inline blocks > 5KB or any > 20KB
- **Research:** Large inline blocks increase HTML document size, slow TTFB for the HTML response. Should be external and cacheable.
- **Fix template:** Move inline CSS/JS to external files with proper caching headers

### B6: DOM Depth Not Excessive
- **Pass:** Max DOM nesting depth < 15 levels
- **Warn:** Depth 15-20 levels
- **Fail:** Depth > 20 levels
- **N/A:** Cannot measure without Chrome MCP
- **Research:** Deep DOM increases rendering cost. Google Lighthouse flags depth > 32 as excessive. Practical impact on rankings: negligible. UX impact: measurable on low-end devices.
- **Fix template:** Flatten nested divs. Reduce wrapper elements. Use CSS Grid/Flexbox instead of nested containers.

### B7: Compression Enabled
- **Pass:** Response includes `Content-Encoding: gzip` or `Content-Encoding: br` (Brotli)
- **Fail:** No Content-Encoding header or `Content-Encoding: identity`
- **Research:** Compression reduces transfer size 60-80%. AI crawlers making 3.6x more requests than traditional crawlers — uncompressed responses increase server load and risk timeouts at scale.
- **Fix template:** Enable Gzip/Brotli at server/CDN level. Most hosting platforms support this via configuration, not code changes.

### B8: Cache-Control Headers
- **Pass:** Response includes `Cache-Control` header with `max-age` > 0
- **Warn:** Cache-Control present but max-age = 0 or no-cache
- **Fail:** No Cache-Control header at all
- **Research:** Caching reduces repeat-visit load times and server load. Not a ranking signal. UX and infrastructure benefit.
- **Fix template:** Add `Cache-Control: public, max-age=3600` (or longer for static assets)

### B9: No Mixed Content
- **Pass:** Zero instances of `http://` in `src`, `href`, `action` attributes on an HTTPS page
- **Fail:** Any `http://` resource loaded on HTTPS page
- **Research:** Mixed content triggers browser warnings. Chrome blocks mixed active content (scripts). Erodes trust signals. Google considers HTTPS implementation quality.
- **Fix template:** Replace all `http://` references with `https://` or protocol-relative `//`

### B10: Core Web Vitals Indicators
- **Pass (with Lighthouse):** LCP <= 2.5s AND CLS <= 0.1 AND INP <= 200ms (all "Good")
- **Warn (with Lighthouse):** Any metric in "Needs Improvement" range
- **Fail (with Lighthouse):** Any metric in "Poor" range (LCP > 4s OR CLS > 0.25 OR INP > 500ms)
- **N/A (without Lighthouse):** Cannot measure — note in report
- **Research:** CWV is a threshold signal, not a gradient. Benefit caps at "Good" — no ranking boost beyond. Pages at #1 only 10% more likely to pass CWV than #9 (Dollar Pocket 10M study). INP replaced FID March 2024. Measured at 75th percentile of CrUX field data, not Lighthouse lab data.
- **Fix template:** Depends on failing metric. LCP: optimize largest image/text block. CLS: add dimensions to images/embeds. INP: reduce JavaScript execution time.

### B11: Image Dimensions Specified (NEW — v1.1)
- **Pass:** All `<img>` tags have explicit `width` and `height` attributes (or CSS equivalent)
- **Warn:** 1-3 images missing dimensions
- **Fail:** 4+ images without width/height (or > 50% of images)
- **N/A:** Page has fewer than 2 images
- **Research:** Images without explicit dimensions are the #1 cause of CLS (Cumulative Layout Shift). Browser cannot reserve space until image loads, causing layout shift. Lighthouse flags this as a CLS contributor. All three major tools (Screaming Frog, Ahrefs, Lighthouse) check this.
- **Fix template:** Add `width` and `height` attributes matching the image's intrinsic dimensions: `<img src="..." alt="..." width="800" height="450" loading="lazy">`

---

## Category C: On-Page SEO

### C1: Heading Hierarchy Logical
- **Pass:** Headings follow H1 → H2 → H3 sequence with no skipped levels (no H1 → H3)
- **Warn:** One skipped level (e.g., H2 → H4)
- **Fail:** Multiple skipped levels or no logical structure
- **Research:** Proper heading hierarchy helps screen readers and search engine content parsing. AI extractors use headings to identify section boundaries and topics.
- **Fix template:** Restructure headings to follow sequential order. Each H3 must be under an H2. Each H4 under an H3.

### C2: Primary Keyword in First 100 Words
- **Pass:** Primary keyword (from title or target query) appears in the first 100 words of body text
- **Warn:** Close semantic variant appears but not exact keyword
- **Fail:** Neither keyword nor variant in first 100 words
- **Research:** Google uses passage-level indexing — first 100 words strongly signal page topic. AI extractors weight opening content heavily for answer selection.
- **Fix template:** Incorporate primary keyword naturally in the opening paragraph

### C3: Three or More Internal Links
- **Pass:** 3+ `<a href>` links pointing to same-domain pages in body content (not nav/footer)
- **Warn:** 1-2 internal links
- **Fail:** Zero internal links in body content
- **Research:** Internal links distribute PageRank, help crawlers discover pages, and signal topical relationships. Pages with no internal links are potential orphans.
- **Fix template:** Add contextual internal links to related pages within body content

### C4: Descriptive Anchor Text
- **Pass:** Zero anchors with text "click here", "read more", "here", "link", "this" as standalone text
- **Warn:** 1-2 non-descriptive anchors
- **Fail:** 3+ non-descriptive anchors
- **Research:** Google uses anchor text to understand linked page context. "Click here" provides zero context. Descriptive anchors help both users and search engines.
- **Fix template:** Replace "Click here to learn more" with "[Topic name] guide" or "[Descriptive phrase]"

### C5: All Images Have Alt Text
- **Pass:** All `<img>` tags have non-empty `alt` attribute (decorative images may use `alt=""`)
- **Warn:** 1-3 images missing alt text
- **Fail:** 4+ images missing alt text or > 50% of images lack alt
- **Research:** Alt text is required for accessibility (WCAG 2.1). Used by Google Image Search for indexing. AI crawlers may use alt text for content understanding.
- **Fix template:** Add descriptive `alt="[What the image shows]"` to each img tag

### C6: Sufficient Word Count
- **Pass:** Word count meets page-type threshold AND is within 50% of competitor median
- **Warn:** Meets page-type threshold but significantly below competitor median (> 50% less)
- **Fail:** Below page-type minimum threshold
- **Page-type thresholds:** Homepage 200+, Landing 500+, Blog/Article 800+, Product 300+, Service 500+, HowTo 600+
- **Research:** Passage-level ranking means word count alone doesn't win. But insufficient content means fewer passages to rank for. Competitor median provides real-world calibration. A 1,500-word page is fine absolutely but weak if competitors average 4,500.
- **Fix template:** Expand content to cover topics competitors address that you don't. Don't pad — add substance.

### C7: No Keyword Stuffing
- **Pass:** Primary keyword density < 3%
- **Warn:** Density 3-4%
- **Fail:** Density > 4%
- **N/A:** Applies to blog, article, howto only. Not scored for homepage/landing/product.
- **Research:** Outdated as a primary concern — Google's semantic understanding handles synonyms. But density > 4% is still a negative signal. GEO paper: keyword stuffing had NEGATIVE impact on visibility.
- **Fix template:** Replace some keyword instances with synonyms or related terms

### C8: Outbound Links to Authoritative Sources
- **Pass:** 2+ outbound links to authoritative external sources (.gov, .edu, academic, official documentation, recognized industry sources) in body content
- **Warn:** 1 outbound link
- **Fail:** Zero outbound links in body content
- **Research:** GEO paper (KDD 2024): "Cite Sources" strategy = 30-40% visibility boost — the highest-impact GEO strategy. Outbound citations signal trust and enable AI cross-reference verification. All AI engines cross-check claims against multiple sources.
- **Fix template:** Add 2-3 outbound links to studies, official documentation, or authoritative sources that support claims made in the content

### C9: Clean URL Structure
- **Pass:** URL path < 75 characters, uses hyphens, lowercase, no special characters or session IDs
- **Warn:** Path 75-100 characters or has underscores
- **Fail:** Path > 100 characters, contains query parameters as primary URL, or non-readable IDs
- **Research:** Minor signal. Clean URLs are better for user comprehension and sharing. Google handles ugly URLs fine technically.
- **Fix template:** Restructure URL to: `https://domain.com/[category]/[descriptive-slug]`

### C10: Open Graph Tags Complete
- **Pass:** All four present: `og:title`, `og:description`, `og:image`, `og:type`
- **Warn:** 2-3 of four present
- **Fail:** 0-1 of four present
- **Research:** Not a ranking signal. Critical for social sharing appearance. Affects CTR from social platforms. AI systems may reference OG data for entity understanding.
- **Fix template:** Add: `<meta property="og:title" content="[Title]">` + description + image + type

### C11: Twitter Card Tags
- **Pass:** `twitter:card` present (preferably `summary_large_image`)
- **Warn:** Twitter card present but using `summary` instead of `summary_large_image`
- **Fail:** No Twitter card tags
- **Research:** Minor. Affects appearance when shared on X/Twitter. Not a ranking signal.
- **Fix template:** `<meta name="twitter:card" content="summary_large_image">`

### C12: Visible Publication/Update Date
- **Pass:** Date visible on page (in content, not just schema) AND datePublished or dateModified in schema
- **Warn:** Date in schema but not visible on page, or visible but not in schema
- **Fail:** No date anywhere (neither visible nor schema)
- **Research:** Google API leak: three date signals (`bylineDate`, `syntacticDate`, `semanticDate`). Perplexity: content within 30 days gets 3.2x more citations. 83% of commercial AI citations from pages < 12 months old.
- **Fix template:** Add visible "Published: [date]" or "Last updated: [date]" near top of content. Add datePublished/dateModified to schema.

### C14: No Broken External Links (NEW — v1.1)
- **Pass:** All outbound links in body content return 200 status
- **Warn:** 1 broken outbound link (4xx/5xx)
- **Fail:** 2+ broken outbound links
- **N/A:** No outbound links (page fails C8 instead)
- **Research:** Broken outbound links erode trust signals. If the page cites a source that no longer exists, the citation is worthless for AI cross-reference verification. All three major tools (Screaming Frog, Ahrefs, Lighthouse) check broken links. GEO paper: "Cite Sources" is the #1 visibility strategy — but only if the citations actually work.
- **Fix template:** Replace broken links with current URLs to the same or equivalent authoritative sources. If the source no longer exists, find an alternative or remove the citation.

---

## Category D: Schema / Structured Data

### D1: JSON-LD Block Present
- **Pass:** At least one `<script type="application/ld+json">` block exists with valid JSON
- **Fail:** No JSON-LD blocks found
- **Research:** BrightEdge: 73% higher AI Overview selection with schema. 3.2x more AI answer appearances with FAQPage. BUT generic/minimally populated schema underperforms no schema (41.6% vs 59.8%) — quality matters.
- **Fix template:** Add appropriate JSON-LD block for page type (see schema-validation.md for required fields)

### D2: @context Is https://schema.org
- **Pass:** @context field is `"https://schema.org"` (HTTPS, not HTTP)
- **Fail:** @context is `"http://schema.org"` or missing or wrong
- **Research:** schema.org is the standard. Google requires `https://` prefix. HTTP prefix may work but is technically incorrect.
- **Fix template:** Change `"@context": "http://schema.org"` to `"@context": "https://schema.org"`

### D3: Page-Appropriate @type Present
- **Pass:** @type matches page classification (Article/BlogPosting for blog, Product for product, etc.)
- **Warn:** @type present but may not be the best match (e.g., WebPage instead of Article for a blog)
- **Fail:** No relevant @type for page classification, or wrong @type
- **Research:** @type tells search engines and AI systems what this content IS. Wrong @type = wrong interpretation. See schema-validation.md for type-to-page mapping.
- **Fix template:** Add or correct @type to match page classification

### D4: @id With Unique Fragment URL
- **Pass:** Schema has `@id` field using `{page URL}#{type}` pattern (e.g., `"https://example.com/#organization"`)
- **Warn:** @id present but not using fragment pattern
- **Fail:** No @id on main schema entities
- **Research:** @id enables entity linking across schema blocks. Google uses @id to connect related entities (Organization ↔ Person ↔ Article). Particularly important for E-E-A-T entity graphs.
- **Fix template:** Add `"@id": "https://[domain]/[path]/#[type]"` to each schema block

### D5: BreadcrumbList Present (Non-Homepage)
- **Pass:** BreadcrumbList schema with valid itemListElement array
- **Fail:** No BreadcrumbList (on non-homepage pages)
- **N/A:** Homepage (breadcrumbs not expected)
- **Research:** BreadcrumbList helps search engines understand site hierarchy. Enables breadcrumb rich results in SERPs.
- **Fix template:** See schema-validation.md for BreadcrumbList required fields

### D6: Required Fields Present Per @type
- **Pass:** All required fields present per schema-validation.md for each @type found
- **Warn:** 1-2 required fields missing
- **Fail:** 3+ required fields missing or critical fields (name, @type) missing
- **Research:** Incomplete schema may fail Google Rich Results validation. Generic/minimal schema underperforms no schema (41.6% vs 59.8% citation rate).
- **Fix template:** Add missing required fields. See schema-validation.md for complete field list per @type.

### D7: Recommended Fields Present Per @type
- **Pass:** 80%+ of recommended fields present
- **Warn:** 50-79% of recommended fields present
- **Fail:** < 50% of recommended fields present
- **Research:** Attribute-rich schema earns 61.7% citation rate. Richness signals quality to AI systems.
- **Fix template:** Add recommended fields per schema-validation.md

### D8: Organization/WebSite Schema Present
- **Pass:** Organization OR WebSite schema exists on the page
- **Warn:** Neither present on a non-homepage page (may exist on homepage only)
- **Fail:** Neither present on the homepage
- **Research:** Organization schema establishes entity identity. WebSite schema enables sitelinks search. Both important for brand entity recognition by AI systems.
- **Fix template:** Add Organization schema with name, url, logo, description, sameAs

### D9: FAQPage Schema If FAQ Content Exists (REFINED — v1.2)
- **Pass:** HTML has Q&A content AND FAQPage schema exists matching it AND answer text contains no promotional/advertising content
- **Warn:** Q&A content exists but no FAQPage schema
- **Fail (mismatch):** FAQPage schema exists but doesn't match visible Q&A content
- **Fail (promotional):** FAQ answer text contains promotional language, CTAs, pricing pitches, or advertising ("Buy now", "Sign up today", "Best in class")
- **Fail (truncated):** FAQ answers are truncated, incomplete, or too vague to be useful
- **N/A:** No Q&A content on page
- **Research:** FAQPage = 3.2x citation increase in AI answers (BrightEdge). Sieve Rule #1603 (Schema.org, 0.97): "FAQPage Must Not Contain Promotional Content in Answers" — Google may demote or reject markup with promotional answer text. Sieve Rule #1602 (Schema.org, 0.96): "FAQPage Answer Text Must Not Be Truncated." Sieve AP#815 (high): "Including promotional or advertising content in FAQPage answer text." Sieve AP#816 (medium): "Writing truncated or incomplete FAQ answers."
- **Fix template:** Add FAQPage schema with mainEntity array matching visible Q&A pairs. Ensure each answer is: (a) factual and informative, not promotional, (b) complete — minimum 1-2 sentences, (c) self-contained — understandable without reading the rest of the page

### D10: Image Fields Use ImageObject
- **Pass:** All schema `image` fields use `{"@type": "ImageObject", "url": "..."}` format
- **Warn:** Mix of ImageObject and bare URL strings
- **Fail:** All image fields are bare URL strings
- **Research:** ImageObject allows width, height, caption — richer data for search engines. Minor quality signal.
- **Fix template:** Convert `"image": "https://..."` to `"image": {"@type": "ImageObject", "url": "https://...", "width": X, "height": Y}`

### D11: datePublished/dateModified in ISO 8601
- **Pass:** Both datePublished and dateModified present in ISO 8601 format (YYYY-MM-DD or full ISO)
- **Warn:** datePublished present but dateModified missing
- **Fail:** Neither date present in schema
- **N/A:** Homepage, product pages (dates less critical)
- **Research:** Google API leak: three date signals. Perplexity weights freshness heavily (3.2x for < 30 days). dateModified is the strongest freshness signal for AI engines.
- **Fix template:** Add `"datePublished": "YYYY-MM-DD"` and `"dateModified": "YYYY-MM-DD"` (use actual dates, not placeholder)

### D12: Author With Person Type and Name
- **Pass:** `"author": {"@type": "Person", "name": "[Full Name]"}` present
- **Warn:** Author field exists but as plain text string, not Person object
- **Fail:** No author field in schema
- **N/A:** Homepage, product pages
- **Research:** 96% of AI Overview citations come from E-E-A-T sources. Author entity is a primary E-E-A-T signal. API leak confirmed `author` fields and `isAuthor` booleans stored per document.
- **Fix template:** Add `"author": {"@type": "Person", "name": "[Name]", "jobTitle": "[Title]", "sameAs": ["[LinkedIn URL]"]}`

### D13: Speakable Property Present
- **Pass:** `"speakable": {"@type": "SpeakableSpecification", "cssSelector": [...]}` present on Article/WebPage
- **Fail:** No speakable property
- **N/A:** Non-article pages
- **Research:** Speakable markup tells voice assistants and AI extractors which sections are best for audio/answer extraction. Must target answer content, NOT navigation or CTAs.
- **Fix template:** Add `"speakable": {"@type": "SpeakableSpecification", "cssSelector": [".summary", ".faq-answer", ".key-takeaway"]}`

---

## Category E: AEO — Discovery

### E1: PerplexityBot Allowed
- **Pass:** robots.txt has no Disallow for PerplexityBot, OR explicit Allow
- **Fail:** robots.txt blocks PerplexityBot
- **Research:** Sieve Rule #1487 (0.99): "Blocking PerplexityBot is the most common reason for Perplexity invisibility." Note: Cloudflare documented Perplexity using stealth crawlers to bypass blocks — blocking may not fully work regardless.
- **Fix template:** Add to robots.txt: `User-agent: PerplexityBot\nAllow: /`

### E2: BingPreview Allowed
- **Pass:** robots.txt has no Disallow for BingPreview
- **Fail:** robots.txt blocks BingPreview
- **Research:** Sieve Anti-pattern #803: "Perplexity uses Bing's index as primary data source. Blocking BingPreview prevents Bing from crawling, which prevents Perplexity from indexing."
- **Fix template:** Add to robots.txt: `User-agent: BingPreview\nAllow: /`

### E3: GoogleBot Allowed
- **Pass:** robots.txt has no Disallow for Googlebot on audited page path
- **Fail:** Googlebot blocked
- **Note:** GATE CHECK — Googlebot block = no Google indexing = no AI Overviews
- **Research:** Blocking Googlebot removes page from all Google surfaces including AI Overviews. Cannot block Googlebot for AI Overviews only — it's all or nothing.
- **Fix template:** Remove Googlebot Disallow rule

### E4: No nosnippet/max-snippet:0
- **Pass:** No nosnippet or max-snippet:0 in meta robots
- **Fail:** `nosnippet` or `max-snippet:0` present
- **Research:** Sieve Rule #1441 (0.99): "Page with nosnippet or max-snippet:0 is excluded from AI Overviews selection." These directives prevent Google from generating any snippet, including AI Overviews.
- **Fix template:** Remove nosnippet. Change to `max-snippet:-1` (unlimited) if snippet control needed.

### E5: Content in Raw HTML
- **Pass:** WebFetch extracts > 500 words of body text (SSR/static content)
- **Warn:** WebFetch extracts 200-500 words (partial JS dependency)
- **Fail:** WebFetch extracts < 200 words (JS SPA — content invisible to AI crawlers)
- **Research:** Sieve Rule #1481 (0.99): "Perplexity's crawler cannot access JS-rendered content." Vercel study: zero JS execution by GPTBot across 500M+ requests. This is the #1 AI visibility blocker.
- **Fix template:** Implement SSR or SSG. Ensure all critical content is in initial HTML response.

### E6: Content Not Behind JS Accordions
- **Pass:** FAQ/accordion content is in initial HTML (just visually collapsed via CSS)
- **Warn:** Accordion content appears to be in HTML but uses JS toggle (may be in DOM)
- **Fail:** Accordion content loaded via JavaScript on click (not in initial HTML)
- **Research:** Sieve Anti-pattern #804 (high): "Perplexity does not execute JavaScript, click interactive elements, expand accordions." Content must be in raw HTML to be extractable.
- **Fix template:** Use CSS-only accordions (`<details>/<summary>`) or render content in HTML with CSS visibility toggle

### E7: IndexNow or Ping Mechanism
- **Pass:** IndexNow key file exists at `/[key].txt` or IndexNow API integration detected
- **Fail:** No IndexNow mechanism found
- **Research:** IndexNow notifies search engines of content changes instantly. Supported by Bing (powers Copilot), Yandex, Seznam. Not supported by Google (uses its own crawl scheduling). Sieve Anti-pattern #763: "Waiting for standard crawl cycles instead of IndexNow" for time-sensitive content.
- **Fix template:** Implement IndexNow API integration. Generate key and host at `https://[domain]/[key].txt`

### E8: Page in XML Sitemap
- **Pass:** Audited page URL appears in the sitemap
- **Fail:** Page not found in sitemap
- **Research:** Sitemap inclusion helps crawlers discover and prioritize pages. Missing = page may be discovered through links but not prioritized. lastmod in sitemap is the only useful signal (Google ignores priority/changefreq).
- **Fix template:** Add page URL to sitemap.xml with accurate lastmod date

### E9: Bing Webmaster Verification
- **Pass:** `<meta name="msvalidate.01" content="[key]">` present OR BingSiteAuth.xml exists
- **Fail:** No Bing verification detected
- **Research:** Bing Webmaster Tools provides AI Performance Report (Feb 2026) showing Copilot citation data. Verification enables this diagnostic data. Also signals site ownership to Bing's index.
- **Fix template:** Add `<meta name="msvalidate.01" content="[your key]">` from Bing Webmaster Tools

### E10: ClaudeBot/ChatGPT-User/Applebot Allowed
- **Pass:** robots.txt has no Disallow for ClaudeBot, ChatGPT-User, OAI-SearchBot, Applebot
- **Warn:** Some but not all are allowed
- **Fail:** Multiple AI crawlers blocked
- **Research:** ChatGPT-User NO LONGER respects robots.txt (2025 policy change). Blocking GPTBot does NOT block OAI-SearchBot. Each OpenAI/Anthropic/Apple bot is independently controllable. ChatGPT = 87.4% of all AI referral traffic.
- **Fix template:** Ensure all AI crawlers are allowed. Note: blocking ChatGPT-User is ineffective since 2025.

### E11: Content Not Behind Paywall/Login (NEW — v1.1)
- **Pass:** All primary content is accessible without login, paywall, or authentication
- **Warn:** Some content gated but primary answer content is accessible
- **Fail:** Core content requires login or payment to access
- **Research:** Sieve AP #810 (high): "Placing primary content behind authentication gates" — AI crawlers cannot authenticate. Login-gated content is completely invisible to GPTBot, PerplexityBot, ClaudeBot, and all AI crawlers. Even Google has limited ability to index paywalled content (requires structured paywall markup + First Click Free or flexible sampling).
- **Fix template:** Make core informational content freely accessible. Use paywall only for premium/extended content. If paywall required, implement `isAccessibleForFree` schema property and Google's paywall structured data.

### E12: No NOARCHIVE on AI-Targeted Pages (NEW — v1.2)
- **Pass:** No `noarchive` directive in meta robots tag or HTTP X-Robots-Tag header
- **Warn:** `noarchive` present but page is not a primary content/service page (may be intentional on sensitive pages)
- **Fail:** `noarchive` present on a page intended for AI citation (service page, blog, product page)
- **Research:** Sieve Rule #1424 (Bing, 0.98): "NOARCHIVE blocks Copilot citation." The noarchive directive prevents Bing from caching the page, which severely limits Copilot to URL-only citation — no rich content extraction, no answer blocks. Sieve AP#766 (high): "Applying NOARCHIVE Meta Tag to Content Intended for Copilot Citation." Note: noarchive does NOT affect Google AI Overviews (confirmed by Sieve Rule #1468, 0.93).
- **Fix template:** Remove `noarchive` from meta robots. Change `<meta name="robots" content="noarchive">` to `<meta name="robots" content="index, follow, max-snippet:-1">`. If noarchive is set via HTTP header (X-Robots-Tag: noarchive), remove it from server config.

### E13: CCBot / LLM Training Crawler Access (NEW — v1.3)
- **Pass:** robots.txt has no Disallow for CCBot (Common Crawl), OR explicitly allows CCBot
- **Warn:** robots.txt uses wildcard `Disallow: /` that covers CCBot but no explicit exception
- **Fail:** robots.txt explicitly blocks CCBot (`User-agent: CCBot / Disallow: /`) OR the site is excluded from Common Crawl index
- **Research:** Common Crawl (CCBot) is the underlying dataset used to train GPT-4, Claude, Llama, and most foundation LLMs. Blocking CCBot means your brand won't exist in the training data of future AI models. Sieve Rule #2016 (amsive.com, 0.95): "AI Crawler Robots.txt Access Rule — If robots.txt blocks CCBot from accessing the site, the site is excluded from Common Crawl dataset." Unlike real-time AI crawlers (GPTBot, PerplexityBot), CCBot access affects how future AI models KNOW about your brand, not just how current ones retrieve it. **Both matter**: CCBot affects training-time brand recognition; GPTBot/PerplexityBot/ClaudeBot affect inference-time citation.
- **Fix template:** Add to robots.txt: `User-agent: CCBot\nAllow: /`. If wildcard `Disallow: /` is set, add explicit `Allow: /` for CCBot. Note: CCBot respects robots.txt, unlike some other AI crawlers.

---

## Category F: AEO — Extraction

### F1: First Paragraph Answers Query
- **Pass:** First 150 words contain ALL THREE: (a) entity/brand name, (b) category/type word, (c) primary function — in declarative "X is a Y that does Z" format
- **Warn:** Entity name present but category or function definition missing
- **Fail:** First 150 words contain no declarative entity definition (opens with tagline, story, or preamble)
- **Research:** AI Overviews: self-contained 134-167 word blocks score 4.2x higher citation. Perplexity L3 reranker scores entity clarity. GEO paper: fluency + clarity = 15-30% visibility boost.
- **Fix template:** "[Entity] is a [category] [for audience] that [primary function]. [Key differentiator]. [Proof point or quantitative claim]."

### F2: Quick-Answer Block Near H1
- **Pass:** Within 200 words of H1, there is a summary, callout, or TL;DR block that directly answers the primary query
- **Warn:** Answer exists but not within first 200 words after H1
- **Fail:** No quick-answer element anywhere near the top of content
- **Research:** AI extractors scan top-of-page content first. A clearly delineated answer block (styled differently, in a callout/box) signals "this is the answer" to both AI and users.
- **Fix template:** Add a summary block immediately after H1: `<div class="quick-answer">[2-3 sentence direct answer to the primary query]</div>`

### F3: FAQ Section With 3+ Q&A Pairs
- **Pass:** Page has identifiable FAQ section with 3+ question-answer pairs
- **Warn:** 1-2 Q&A pairs
- **Fail:** No FAQ or Q&A content
- **Research:** FAQPage schema = 3.2x citation increase. FAQ content is the most directly extractable format for AI answer engines. Questions map directly to user queries.
- **Fix template:** Add FAQ section with 5-8 Q&A pairs. Derive questions from target queries + "People Also Ask" patterns.

### F4: FAQ Uses Semantic Markup
- **Pass:** FAQ uses `<details>/<summary>`, `<dl>/<dt>/<dd>`, or clearly structured Q/A HTML
- **Warn:** FAQ exists but uses generic divs without semantic structure
- **Fail:** FAQ content with no semantic markup or accessibility
- **Research:** Semantic markup helps AI parsers identify question boundaries and answer boundaries. `<details>/<summary>` is native HTML — no JS needed.
- **Fix template:** Wrap FAQ in `<details><summary>[Question]</summary><p>[Answer]</p></details>`

### F5: FAQ Questions Use Natural Language
- **Pass:** 80%+ of FAQ questions start with How/What/When/Why/Is/Can/Does/Where/Which
- **Warn:** 50-79% use natural language phrasing
- **Fail:** < 50% (questions are statements or branded phrases)
- **Research:** AI query matching works best with natural language questions. "How does TRYPS work?" matches user queries better than "TRYPS Features" as an FAQ heading.
- **Fix template:** Rephrase FAQ headings as natural questions: "Features" → "What features does [brand] offer?"

### F6: Headings Phrased as Questions or Answers
- **Pass:** 50%+ of H2/H3 headings contain question marks OR are direct-answer phrases
- **Warn:** 25-49% question/answer phrasing
- **Fail:** < 25% (most headings are branded or abstract labels)
- **Research:** Question-style H2s create natural query-answer mapping. AI systems match user queries against heading text to locate relevant sections.
- **Fix template:** Convert "Our Process" → "How does [brand] work?" or "Simple Process" → "3 Steps to Plan Your Group Trip"

### F7: Named Entities (Not Vague Pronouns)
- **Pass:** In first 300 words, specific entity names outnumber vague pronouns ("it", "they", "we", "our", "your", "this") by 3:1 ratio or better
- **Warn:** Ratio 1:1 to 3:1
- **Fail:** Pronouns outnumber entity names
- **Research:** AI extractors need entity clarity to understand WHAT the content is about. "Our platform helps your team" is ambiguous. "[Brand] helps [specific audience]" is extractable. Perplexity L3 scores entity clarity.
- **Fix template:** Replace vague references with specific names. "We help you" → "[Brand] helps [audience]". "Our tool" → "[Product name]".

### F8: Specific Facts (Numbers, Dates, Prices)
- **Pass:** 5+ quantitative claims in content (numbers, percentages, dates, prices, durations)
- **Warn:** 2-4 quantitative claims
- **Fail:** 0-1 quantitative claims (all qualitative/vague)
- **Research:** GEO paper: "Statistics Addition" = 30-40% visibility boost for law/government, strong for all domains. AI systems prefer citable facts over vague claims. Sieve Anti-pattern #797: "Using Vague Marketing Language Instead of Verifiable Claims."
- **Fix template:** Replace vague claims with specific data. "Saves time" → "Saves an average of 3 hours per trip." "Many users" → "Used by 500+ groups."

### F9: Definition-First Writing Style
- **Pass:** First sentence of the page matches "X is a Y" or "X is [article] Y that [verb]" pattern
- **Warn:** First sentence contains the entity name but in non-definitional format
- **Fail:** First sentence is a tagline, question, or narrative opener with no entity definition
- **Research:** Definition-first opening creates an extractable entity description. AI Overview citation blocks start with entity definitions. Perplexity L3 reranker scores entity clarity.
- **Fix template:** Start with: "[Entity] is a [category noun] that/for [function/audience]."

### F10: Summary/TL;DR at End
- **Pass:** Last 20% of content contains a summary section (heading includes "summary", "takeaway", "conclusion", "TL;DR", "key points")
- **Warn:** Closing section exists but not clearly labeled as summary
- **Fail:** Content ends without any summary or recap
- **Research:** End-of-article summaries provide a second extraction point for AI systems. Users who scroll to the end get a recap. Creates bookend with the opening answer block.
- **Fix template:** Add section: `## Key Takeaways` with 3-5 bullet points summarizing the core information

### F11: Self-Contained Answer Units
- **Pass:** Each H2 section can be understood independently if extracted alone — contains enough context to be a standalone answer
- **Warn:** Most sections are self-contained but 1-2 depend on prior sections for context
- **Fail:** Sections heavily reference prior content ("as mentioned above", "building on the previous point") and cannot stand alone
- **Research:** AI engines extract SECTIONS, not full pages. Each section is a potential citation candidate. Self-contained = citable. Context-dependent = not citable.
- **Fix template:** Start each section with a brief contextual sentence that doesn't assume the reader saw previous sections

### F12: Tables/Lists for Comparative Data
- **Pass:** Page uses `<table>` or structured `<ul>/<ol>` for presenting multi-item or comparative data
- **Warn:** Lists exist but data that should be in a table is in paragraph form
- **Fail:** No tables or structured lists, and content contains comparative or multi-item data
- **N/A:** Content doesn't contain comparative data
- **Research:** 40-61% of AI Overviews use list/bullet formats. Tables are highly extractable by all AI engines. Comparison tables map directly to "[Product] vs [Product]" queries.
- **Fix template:** Convert comparison paragraphs to `<table>` with clear column headers

---

## Category G: AEO — Trust

### G1: Author Byline Visible
- **Pass:** Author name visible in page content (not just schema)
- **Fail:** No visible author attribution
- **N/A:** Homepage, product pages
- **Research:** 96% of AI Overview citations from E-E-A-T sources. Visible authorship is a primary trust signal. Schema-only author (invisible to users) is weaker than visible byline.
- **Fix template:** Add visible "By [Author Name]" near the title/date

### G2: Author Schema With Credentials/sameAs
- **Pass:** Person schema has `sameAs` array with 1+ external profile URLs AND (`jobTitle` OR `description`)
- **Warn:** Person schema exists but missing sameAs or credentials
- **Fail:** No Person schema for author, or Person with name only
- **N/A:** Pages without authorship expectation
- **Research:** Author entity verification through sameAs links enables AI systems to confirm expertise. Empty Person schema (name only) is an unverifiable claim.
- **Fix template:** Add `"sameAs": ["https://linkedin.com/in/...", "https://twitter.com/..."], "jobTitle": "[Title]"`

### G3: Outbound Citations to Primary Sources
- **Pass:** 3+ outbound links to authoritative external domains (.gov, .edu, academic journals, official documentation, recognized data sources)
- **Warn:** 1-2 outbound citations
- **Fail:** Zero outbound links to external authoritative sources
- **Research:** GEO paper: "Cite Sources" = 30-40% visibility boost (highest-impact strategy, universally effective). All AI engines cross-reference claims. Outbound citations enable corroboration.
- **Fix template:** Add inline citations: `According to [Source Name](https://...), [factual claim].`

### G4: Publication Date Visible AND in Schema
- **Pass:** Date visible on page (text like "Published: Jan 15, 2026") AND datePublished in schema
- **Warn:** Date in one location but not both
- **Fail:** No date anywhere
- **N/A:** Homepage, evergreen product pages
- **Research:** Google API leak: three date signals. Visible date provides transparency to users. Schema date is machine-readable for AI systems. Both together are strongest.
- **Fix template:** Add visible date near title AND `"datePublished": "2026-01-15"` in schema

### G5: dateModified Visible AND in Schema
- **Pass:** "Updated" or "Last modified" visible on page AND dateModified in schema with date different from datePublished
- **Warn:** dateModified in schema only, not visible
- **Fail:** No dateModified anywhere
- **Research:** Perplexity: content within 30 days gets 3.2x more citations. 83% of commercial AI citations from pages < 12 months old. dateModified is THE freshness signal for AI engines.
- **Fix template:** Add visible "Last updated: [date]" AND `"dateModified": "2026-04-10"` in schema. Update both when content changes.

### G6: Organization Schema With sameAs
- **Pass:** Organization schema has `sameAs` array with 2+ social profile URLs that resolve
- **Warn:** Organization has sameAs with 1 URL
- **Fail:** Organization schema has no sameAs, or no Organization schema
- **Research:** sameAs links establish entity identity across platforms. AI systems use sameAs for entity disambiguation. Missing sameAs = weaker entity recognition.
- **Fix template:** Add `"sameAs": ["https://linkedin.com/company/...", "https://twitter.com/...", "https://github.com/..."]`

### G7: Privacy/Terms Accessible
- **Pass:** Footer contains links with text matching "privacy", "terms", "legal", or "cookie"
- **Fail:** No legal links in footer
- **Research:** Minor trust signal. Quality rater guidelines check for transparency indicators. Absence suggests less trustworthy site.
- **Fix template:** Add footer links to privacy policy and terms of service pages

### G8: HTTPS Valid
- **Pass:** Page loads over HTTPS without certificate errors
- **Fail:** Certificate error, expired cert, or HTTP-only
- **Research:** HTTPS is a confirmed ranking signal (since 2014). All major browsers warn users on HTTP sites. AI crawlers deprioritize non-HTTPS sources.
- **Fix template:** Install valid SSL certificate. Configure server for HTTPS.

### G9: Content Freshness Recency (NEW — v1.1)
- **Step 1 — Date exists?**
  - No datePublished AND no dateModified AND no visible date → FAIL "No freshness signal. AI engines cannot determine if this content is current." (This overlaps with G4/G5 but specifically checks for recency assessment capability)
- **Step 2 — How old?**
  - **Pass:** dateModified or datePublished is < 90 days old
  - **Warn:** Date is 90-365 days old
  - **Fail:** Date is > 365 days old
- **Cosmetic update detection:** If dateModified is recent (< 30 days) but content references outdated data (years, statistics, "in 2024" language, discontinued products), flag as MODEL JUDGMENT warning: "Possible cosmetic timestamp update without substantive content changes (Sieve AP#799)."
- **Research:** 50% of AI-cited content is < 13 weeks old (2026 web data). Perplexity: 3.2x citation boost for content < 30 days. 83% of commercial AI citations from pages < 12 months. Content freshness is a primary signal for ALL AI answer engines, not just a nice-to-have.
- **Fix template:**
  - If no date: Add `"dateModified": "YYYY-MM-DD"` to schema + visible "Last updated: [date]" on page → fix type: PAGE HTML FIX
  - If date is stale: Actually update the content — refresh statistics, update examples, add new information — THEN update dateModified → fix type: CONTENT RESTRUCTURE
  - Never just change the date without changing content (Sieve AP#799: "Cosmetic timestamp updates" — risk: medium)

---

## Category H: AEO — Selection (Competitor-Relative)

All H checks are scored RELATIVE to competitors. There are no absolute thresholds —
the page is compared against the competitor set from Phase 3.

### H1: Content Depth vs Competitors
- **Pass:** Word count + topic coverage at or above competitor median
- **Warn:** Within 30% below competitor median
- **Fail:** More than 30% below competitor median
- **Research:** Semantic completeness scoring — AI Overviews evaluates whether a source provides sufficient context. Deeper content has more passages for passage-level ranking.
- **Fix template:** Expand content to cover topics competitors address. Identify missing subtopics from competitor H2/H3 headings.

### H2: Unique Data/Research
- **Pass:** Page contains original data (surveys, case studies with metrics, proprietary research, first-person experience) not found on competitor pages
- **Warn:** Some unique perspective but no original data
- **Fail:** Content is entirely derivative — restates what competitors say without adding new information
- **Research:** AI systems prefer authoritative sources. Original research = highest authority signal. Derivative content competes poorly against sources with original data.
- **Fix template:** Add original data point, case study result, or first-person experience that competitors don't have

### H3: FAQ Coverage vs Competitors
- **Pass:** FAQ pair count at or above competitor median
- **Warn:** Below median but within 50%
- **Fail:** Below 50% of competitor median, or no FAQ when competitors have 5+
- **Research:** FAQPage = 3.2x citation boost. More FAQ coverage = more query-answer match opportunities.
- **Fix template:** Add FAQ pairs to match or exceed competitor median. Source questions from "People Also Ask" for target queries.

### H4: Schema Completeness vs Competitors
- **Pass:** Schema type count and field completeness at or above competitor median
- **Warn:** Slightly below median
- **Fail:** Significantly less schema than competitors
- **Research:** Rich schema = 61.7% citation rate. Schema completeness is a measurable competitive advantage for AI citation.
- **Fix template:** Add schema types that competitors have and you don't

### H5: Fresher Content Than Competitors
- **Pass:** dateModified more recent than competitor median
- **Warn:** Within 30 days of competitor median
- **Fail:** dateModified older than all competitors, or no dateModified when competitors have it
- **Research:** Perplexity: 3.2x citation boost for content < 30 days. Freshness is relative — fresher than competitors = advantage.
- **Fix template:** Update content and dateModified. Add new information, refresh statistics, update references.

### H6: E-E-A-T Signals vs Competitors
- **Pass:** Equal or more author/credential/citation signals than competitor median
- **Warn:** Slightly fewer signals
- **Fail:** Significantly fewer (no author vs competitors with authors, no citations vs competitors with 3+)
- **Research:** 96% of AI Overview citations from E-E-A-T sources. E-E-A-T is competitive — relative strength matters.
- **Fix template:** Add the specific E-E-A-T signals competitors have that you lack (author, credentials, citations, dates)

### H7: Appears in AI Overview
- **Pass:** Page/domain appears in WebSearch AI-generated results for target query
- **Warn:** Domain appears but for different page
- **Fail:** Not present in any AI results for target query
- **Research:** AI Overview organic overlap dropped to 38% (Jan 2026). Appearance is not guaranteed by organic ranking. This check MEASURES actual presence.
- **Fix template:** This cannot be directly fixed from this page alone. Requires: E-E-A-T improvements, content depth, schema completeness, freshness — the cumulative effect of fixing other checks.

### H8: Content Matches Query Intent
- **Pass:** Content format and depth match the query intent type (informational query → comprehensive guide, transactional → product details with pricing)
- **Warn:** Partial intent match (e.g., informational query but content is too promotional)
- **Fail:** Intent mismatch (e.g., target query is informational but page is purely a sales landing page)
- **Research:** Google intent classification is the primary ranking filter. Wrong format for intent = won't rank. API leak: NavBoost tracks "bad clicks" from intent mismatches.
- **Fix template:** Identify the dominant intent for the target query (informational/transactional/navigational). Restructure content to match that intent.

---

## Category I: GEO (All MODEL JUDGMENT — Directional)

All GEO checks produce directional assessments, not hard evidence.
Report with appropriate caveats.

### I1: Brand in Category Queries
- **Pass:** Brand name appears in WebSearch results for "best [category]" or "[category] tools/apps"
- **Fail:** Brand not mentioned in any category search results
- **Assessment method:** Run WebSearch for category query. Check if brand name appears in any result title, description, or snippet.
- **Research:** GEO paper: brand presence in category contexts is the foundation of generative engine visibility. If AI systems don't see the brand in category content, they can't recommend it.
- **Fix template:** OFF-PAGE WORK — get listed on review sites (G2, Product Hunt, Capterra), create "best [category]" content on your own blog, earn mentions in industry listicles.

### I2: Knowledge Panel/Entity Card
- **Pass:** Searching brand name produces a knowledge panel, entity card, or structured brand information
- **Fail:** No entity recognition — just regular organic results
- **Assessment method:** WebSearch for brand name. Check for structured information beyond organic links.
- **Research:** Knowledge panel indicates Google recognizes the brand as an entity. Wikipedia/Wikidata presence strengthens this. Entity recognition by Google correlates with AI system awareness.
- **Fix template:** OFF-PAGE WORK — build structured entity presence: Wikipedia article (if notable), Wikidata entry, Google Business Profile, consistent NAP across directories.

### I3: AI Description Matches Positioning
- **Pass:** How search results/snippets describe the brand aligns with the brand's own positioning on its website
- **Warn:** Partial alignment but some outdated or inaccurate framing
- **Fail:** Search results describe the brand differently from how it describes itself, or brand has no consistent self-description
- **Assessment method:** Compare WebSearch brand descriptions against homepage/about page brand statement.
- **Research:** AI systems synthesize brand descriptions from multiple sources. If sources disagree, the AI description becomes inaccurate or generic.
- **Fix template:** Create one canonical brand sentence used identically across: homepage, about page, Organization schema description, social profiles, directory listings.

### I4: No Outdated/Incorrect AI Info
- **Pass:** Search results contain current, accurate information about the brand
- **Warn:** Minor inaccuracies or outdated details
- **Fail:** Search results contain significantly wrong information (old pricing, discontinued features, wrong category)
- **Assessment method:** Cross-reference search result descriptions with current site content.
- **Research:** Outdated info in search results means AI systems will generate outdated brand descriptions. Freshness of ALL sources (not just your site) matters.
- **Fix template:** Update your own site content, then update third-party profiles, then request Google review of knowledge panel if applicable.

### I5: Brand Sentiment Positive/Neutral
- **Pass:** Search results for brand name show positive or neutral sentiment (good reviews, positive press, favorable comparisons)
- **Warn:** Mixed sentiment
- **Fail:** Negative sentiment dominates (bad reviews, complaints, unfavorable comparisons)
- **Assessment method:** WebSearch brand name + "reviews". Assess overall sentiment of results.
- **Research:** AI systems reflect the web's consensus about a brand. Negative review dominance in search results translates to negative AI descriptions.
- **Fix template:** OFF-PAGE WORK — review management strategy, customer success outreach, address negative reviews professionally, build positive testimonial content.

### I6: Brand Recommended Over Competitors
- **Pass:** Brand appears in top results for category queries AND is positioned favorably (recommended, highly rated)
- **Warn:** Brand mentioned but not recommended or positioned neutrally
- **Fail:** Brand not mentioned, or mentioned unfavorably compared to competitors
- **Assessment method:** WebSearch "best [category]" results. Check brand's position and framing.
- **Research:** GEO: brand positioning in category content directly influences AI recommendations. Owned comparison content ("Brand vs Competitor") helps control narrative.
- **Fix template:** OFF-PAGE WORK — create "[Brand] vs [Competitor]" content on your blog, build use-case-specific landing pages, earn placement in category listicles.

### I7: Consistent Entity Data Across Sources
- **Pass:** Brand name, description, and category are consistent across 3+ search result sources
- **Warn:** Minor inconsistencies across sources
- **Fail:** Major inconsistencies — different name variants, conflicting descriptions, wrong categories
- **Assessment method:** Cross-reference brand information across multiple search result entries.
- **Research:** Entity consistency across the web strengthens AI's confidence in brand information. Inconsistency = AI picks randomly or generates a blended (often wrong) description.
- **Fix template:** Audit and update all third-party profiles to use identical brand name, description, and category. Start with highest-authority sources first.

### I8: sameAs Links to Authoritative Profiles
- **Pass:** Organization schema has sameAs array with 2+ URLs that resolve to real, active profiles
- **Warn:** sameAs URLs present but some don't resolve or are inactive
- **Fail:** No sameAs in Organization schema
- **Assessment method:** Parse Organization schema sameAs. WebFetch each URL to verify it resolves.
- **Research:** sameAs is the structured data mechanism for entity disambiguation. Active, verified external profiles strengthen entity recognition across all AI systems.
- **Fix template:** Add active social/professional profile URLs to Organization schema sameAs array

---

## Category J: Entity Consistency

### J1: Organization Name Consistent
- **Pass:** Brand name is identical in: Organization schema name, og:site_name, title tag brand portion, footer copyright/brand text
- **Warn:** 1 source differs slightly (e.g., "TRYPS" vs "TRYPS App" vs "Tryps")
- **Fail:** 2+ sources differ, or significantly different names across sources
- **Research:** Entity consistency helps AI systems unify brand identity. Inconsistent naming = fragmented entity recognition. Google and AI systems may treat "TRYPS" and "TRYPS App" as different entities.
- **Fix template:** Standardize brand name across all on-page references. Pick one canonical name and use it everywhere.

### J2: Logo Consistent
- **Pass:** Logo/image referenced in Organization schema matches og:image or favicon references
- **Warn:** Different images across schema and OG
- **Fail:** No logo in schema, or completely different images across sources
- **Research:** Visual brand consistency reinforces entity identity. Minor signal but easy to fix.
- **Fix template:** Use the same logo URL in Organization schema logo field and og:image

### J3: URL/Domain Consistent
- **Pass:** Canonical URL, schema @id, og:url all reference the same domain and path
- **Fail:** Different domains or paths across these references (e.g., trypsagent.com in one, jointryps.com in another)
- **Research:** Domain inconsistency fragments siteAuthority (confirmed in API leak). Multiple domains without proper canonical consolidation split signals.
- **Fix template:** Standardize on one domain. Set canonical. Ensure schema @id and og:url match canonical URL.

### J4: sameAs URLs Resolve
- **Pass:** All sameAs URLs in Organization schema return 200 status and contain brand references
- **Warn:** Some sameAs URLs resolve but don't clearly link back to the brand
- **Fail:** sameAs URLs return 404, redirect to unrelated pages, or don't resolve
- **Research:** Dead sameAs links signal neglected entity management. AI systems that follow sameAs to verify identity will find dead ends — weakening trust.
- **Fix template:** Remove dead sameAs URLs. Replace with active, verified profiles. Ensure each profile clearly references the brand.
