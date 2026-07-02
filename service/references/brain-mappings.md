# Brain Mappings — Check-to-Sieve Links

Version: 1.3
Last curated: 2026-04-13
Sieve project: aldraxqsqeywluohskhs
Sieve brain size: 7,794 rules + 4,533 anti-patterns + 5,827 principles (as of 2026-04-13)

## Usage

At audit time, for each failed/warned check:
1. Look up this file by check_id
2. Fetch Sieve entries by the listed IDs (direct SQL lookup, not keyword search)
3. Attach matching entries as evidence citations in the finding

If a check has no mapping → finding stands on static-rules.md criteria alone.
If Sieve is unreachable → all findings stand on static rules alone.

## Fallback

If a check has no curated mapping but fails, OPTIONALLY run a keyword search
against Sieve as transitional fallback. Phase out as mappings are completed.

---

## Category A: Technical SEO

### A1: HTTPS Enforcement
- rules: [1489]
- anti_patterns: [812]
- notes: Rule 1489 (0.95) "Enforce HTTPS across entire domain for AI citation trust." AP#812 "Operating key pages without HTTPS."

### A2: Title Tag
- rules: []
- anti_patterns: []
- notes: Basic HTML check. No brain entries needed.

### A3: Meta Description
- rules: []
- anti_patterns: []
- notes: Not a ranking signal. No brain entries.

### A4: Canonical Tag
- rules: []
- anti_patterns: [264]
- notes: AP#264 "Specifying conflicting canonical URLs across different methods" (high, seo).

### A5: Robots Meta Indexing
- rules: [1166, 1440]
- anti_patterns: [785, 793]
- notes: Rule 1166 (0.99) "Noindex excludes page." Rule 1440 (0.99) "AI Overviews indexing requirement." AP#785 "Blocking Googlebot." AP#793 "Applying nosnippet/noindex to AI Overview content."

### A6: H1 Tag
- rules: []
- anti_patterns: []
- notes: Basic HTML structure. No brain entries.

### A7: H1 Keyword
- rules: []
- anti_patterns: [773]
- notes: AP#773 "Using generic or vague heading labels that don't reflect block content" (medium).

### A8: Lang Attribute
- rules: []
- anti_patterns: []

### A9: Viewport Meta
- rules: []
- anti_patterns: [326]
- notes: AP#326 "Mobile version containing less content than desktop" (high, seo).

### A10: robots.txt Allows Crawling
- rules: [1442, 534, 1165]
- anti_patterns: [785]
- notes: Rule 1442 (0.98) "AI Overviews crawlability requirement." Rule 534 (0.99) "noindex in robots.txt not supported by Google." Rule 1165 (0.99) "Googlebot access blocked by robots.txt." AP#785 "Blocking Googlebot via robots.txt."

### A11: Sitemap Referenced
- rules: [563, 1482]
- anti_patterns: []
- notes: Rule 563 (0.99) "Absolute URLs required in sitemaps." Rule 1482 (0.90) "Submit sitemaps with accurate lastmod for Perplexity crawling."

### A12: JS Rendering Dependency
- rules: [1481, 1419, 1488]
- anti_patterns: [325, 804, 809]
- notes: Rule 1481 (0.99) "Serve content in raw HTML for Perplexity." Rule 1419 (0.98) "Critical content must be in crawlable HTML." Rule 1488 (0.97) "Render key content in static HTML." AP#325 "Relying solely on client-side rendering" (high, seo). AP#804 "Hiding key content behind JavaScript" (high). AP#809 "Rendering key content exclusively via client-side JS" (high).

---

## Category B: Performance

### B1: TTFB
- rules: []
- anti_patterns: []
- notes: Knowledge model covers TTFB importance for AI crawlers. No specific Sieve rules.

### B2-B9: Performance Checks
- rules: []
- anti_patterns: [277]
- notes: AP#277 "LCP exceeding 2.5s threshold" applies to B10. Other performance checks have no brain entries — knowledge model provides calibration.

### B11: Image Dimensions (NEW v1.1)
- rules: []
- anti_patterns: [277]
- notes: AP#277 indirectly related — missing dimensions cause CLS which affects LCP perception. No direct brain entry.

### B10: Core Web Vitals (STRENGTHENED v1.3)
- rules: [7176, 7190]
- anti_patterns: [277]
- notes: AP#277 "LCP exceeding 2.5s" (high, seo). v1.3: Rule #7176 (Backlinko, 0.95) "LCP 'Good' Threshold Under 2.5 Seconds." Rule #7190 (Backlinko, 0.95) "INP Threshold 200ms for Good Responsiveness." Knowledge model: CWV is threshold, not gradient.

---

## Category C: On-Page SEO

### C1: Heading Hierarchy
- rules: []
- anti_patterns: [773]
- notes: AP#773 "Using generic or vague heading labels."

### C2: Keyword in First 100 Words
- rules: [1448]
- anti_patterns: [774]
- notes: Rule 1448 (0.95) "Answer-first structure for AI Overview citation." AP#774 "Burying key answers after lengthy preamble."

### C3: Internal Links
- rules: []
- anti_patterns: [295]
- notes: AP#295 "Using non-anchor elements for navigation links" (high, seo) — Googlebot can't follow JS-driven nav.

### C4: Descriptive Anchor Text
- rules: []
- anti_patterns: []

### C5: Alt Text
- rules: []
- anti_patterns: []

### C6: Word Count
- rules: []
- anti_patterns: [800]
- notes: AP#800 "Publishing thin content that only summarises existing information" (high).

### C7: Keyword Stuffing
- rules: []
- anti_patterns: []
- notes: GEO paper: keyword stuffing has NEGATIVE visibility impact.

### C8: Outbound Citations
- rules: [1478]
- anti_patterns: []
- notes: Rule 1478 (0.92) "Cite primary authoritative sources inline for AI trust signals."

### C9-C11: URL, OG, Twitter
- rules: []
- anti_patterns: []
- notes: Standard HTML checks. No brain entries.

### C14: Broken External Links (NEW v1.1)
- rules: [1478]
- anti_patterns: []
- notes: Rule 1478 (0.92) "Cite primary authoritative sources inline" — broken citations undermine this. No direct AP but broken links are universally flagged by all major tools.

### C12: Visible Date
- rules: [1474]
- anti_patterns: [799]
- notes: Rule 1474 (0.95) "Signal content freshness with visible timestamps and substantive updates." AP#799 "Cosmetic timestamp updates without substantive content changes" (medium).

---

## Category D: Schema

### D1: JSON-LD Present (STRENGTHENED v1.3)
- rules: [3]
- anti_patterns: [4763]
- notes: Rule 3 (0.90) "Schema markup coverage rule." v1.3: AP#4763 (Backlinko, high) "Omitting Schema Markup" — explicitly identifies missing schema as a primary AEO failure pattern.

### D2: @context
- rules: []
- anti_patterns: []

### D3: Page-Appropriate @type (STRENGTHENED v1.2)
- rules: [3, 1696, 1682, 1635, 1636]
- anti_patterns: []
- notes: Rule 1696 (0.98) "Product schema must not be applied to category listing pages." Rule 1682 (Schema.org, 0.97) "Use LocalBusiness Instead of Bare Place for Commercial Entities." Rule 1635 (Schema.org, 0.95) "LocalBusiness: Use Most Specific Subtype." Rule 1636 (Schema.org, 0.97) "LocalBusiness Required Fields for Local Search Eligibility." (v1.2: added LocalBusiness rules for local_business page type)

### D4: @id Fragment
- rules: []
- anti_patterns: []

### D5: BreadcrumbList
- rules: [1555, 1556, 1557]
- anti_patterns: []
- notes: Rules 1555-1557 (all 0.99) cover BreadcrumbList itemListElement, position, and name requirements.

### D6: Required Fields
- rules: [1710, 1695, 1668, 1600, 1674, 1654, 1532]
- anti_patterns: []
- notes: Rule 1710 (0.99) "Product: name and offers mandatory." Rule 1695 (0.98) "Product requires name and offers for rich results." Rule 1668 (0.98) "Organization must include name and URL." Rule 1600 (0.98) "FAQPage mainEntity and acceptedAnswer required." Rule 1674 (0.99) "Person must include name." Rule 1654 (0.99) "Offer MUST include price, priceCurrency, availability." Rule 1532 (0.99) "AggregateRating must have ratingValue and count." (NEW mappings from validation v1.1)

### D7: Recommended Fields
- rules: [1678, 1679]
- anti_patterns: []
- notes: Rule 1678 (0.92) "Person affiliation and worksFor for authority signals." Rule 1679 (0.90) "Person hasCredential for expertise documentation."

### D8: Organization/WebSite
- rules: [1668]
- anti_patterns: [959]
- notes: Rule 1668 (0.98) Organization name+URL. AP#959 "Omitting required url property from schema entities."

### D9: FAQPage If FAQ Exists (REFINED v1.3 — visible-content rule added)
- rules: [1600, 1495, 1496, 1606, 1602, 1603, 7375]
- anti_patterns: [813, 814, 823, 873, 874, 876, 815, 816]
- notes: Rich rule coverage. Rules: FAQPage requirements (1600), visible content (1495), text matching (1496), JSON-LD implementation (1606). v1.2 additions: Rule 1602 (Schema.org, 0.96) "FAQPage Answer Text Must Not Be Truncated." Rule 1603 (Schema.org, 0.97) "FAQPage Must Not Contain Promotional Content." AP#815 "Including promotional content in FAQ answers" (high). AP#816 "Writing truncated FAQ answers" (medium). v1.3: Rule #7375 (Schema.org, 0.98) "Schema Markup Must Match Visible Content" — cross-cuts D9 and F4 (semantic markup matching). Anti-patterns cover misuse: hidden (813), crowdsourced (814, 874), QAPage confusion (823), promotional (873, 815), truncated (816), dynamically loaded (876).

### D10: ImageObject
- rules: []
- anti_patterns: []

### D11: datePublished/dateModified
- rules: [1474]
- anti_patterns: [799]
- notes: Rule 1474 (0.95) freshness timestamps. AP#799 cosmetic-only updates.

### D12: Author Person Schema
- rules: [1674, 1676, 1678, 1679]
- anti_patterns: [798]
- notes: Rule 1674 (0.99) Person must include name. Rule 1676 (0.98) Person author for E-E-A-T. Rule 1678 (0.92) affiliation/worksFor. Rule 1679 (0.90) hasCredential. AP#798 "Publishing anonymous content without authorship signals."

### D13: Speakable
- rules: [1746, 1747, 1748, 1749, 1517, 1518, 1519, 1520, 1521, 1750, 1751]
- anti_patterns: [945, 946, 827, 828, 829, 830, 831, 947]
- notes: Extensive coverage. Rules cover implementation requirements. Anti-patterns cover misuse: entire body (945), non-informational elements (946), visual-dependent content (829), hidden content (830), unsupported targeting (831).

---

## Category E: AEO Discovery

### E1: PerplexityBot Allowed
- rules: [1479, 1487]
- anti_patterns: [802, 808]
- notes: Rules 1479/1487 (both 0.99) "Allow PerplexityBot." AP#802 "Blocking PerplexityBot" (high). AP#808 "Blocking PerplexityBot or BingPreview."

### E2: BingPreview Allowed
- rules: [1480, 1487]
- anti_patterns: [803, 808]
- notes: Rule 1480 (0.98) "Allow BingPreview as prerequisite for Perplexity." Rule 1487 (0.99) covers both. AP#803 "Blocking BingPreview" (high).

### E3: GoogleBot Allowed
- rules: [1442, 1440]
- anti_patterns: [785]
- notes: Rule 1442 (0.98) AI Overviews crawlability. Rule 1440 (0.99) AI Overviews indexing. AP#785 "Blocking Googlebot."

### E4: No nosnippet / No NOARCHIVE
- rules: [1441, 1464, 1465, 1466, 1467, 1470, 1424]
- anti_patterns: [770, 786, 793, 794, 766, 767]
- notes: Extensive coverage. Rules cover snippet eligibility, max-snippet behavior, data-nosnippet granularity. Rule 1424 (Bing, 0.98) "NOARCHIVE blocks Copilot citation." AP#766 "Applying NOARCHIVE to Copilot-targeted content" (high). AP#767 "Applying NOCACHE to Copilot content" (high). (v1.2 additions)

### E5: Content in Raw HTML
- rules: [1481, 1419, 1444, 1488, 2015, 1426]
- anti_patterns: [325, 804, 809]
- notes: Core AEO rules. Rules cover raw HTML for Perplexity (1481), critical content in crawlable HTML (1419), AI Overviews visible text (1444), static HTML rendering (1488). Rule 2015 (amsive.com, 0.97) "AI Crawler JavaScript Avoidance Rule." Rule 1426 (Bing, 0.97) "JavaScript-Only Content Excluded from Bing Index." AP: client-side rendering (325), JS interactions (804), JS-only rendering (809). (v1.2: added 2015, 1426)

### E6: Content Not Behind Accordions
- rules: [1451]
- anti_patterns: [768, 783]
- notes: Rule 1451 (0.93) "Avoid hiding content behind interactive elements." AP#768 "Hiding key content behind interactive elements" (high). AP#783 "Hiding important content behind interactive elements" (high).

### E7: IndexNow
- rules: [1411, 1429]
- anti_patterns: [763, 765]
- notes: Rule 1411 (0.95) "IndexNow submission for fresh content AI citation." Rule 1429 (0.91) "IndexNow reduces citation lag." AP#763 "Waiting for standard crawl cycles" (high). AP#765 "Excessive repeated pings" (medium).

### E8: Page in Sitemap
- rules: [1482]
- anti_patterns: []
- notes: Rule 1482 (0.90) "Submit sitemaps with accurate lastmod for Perplexity crawling."

### E9: Bing Webmaster
- rules: [1425]
- anti_patterns: []
- notes: Rule 1425 (0.98) "Bing index is prerequisite for AI citation."

### E10: ClaudeBot/ChatGPT-User/Applebot
- rules: []
- anti_patterns: []
- notes: Knowledge model covers ChatGPT-User ignoring robots.txt (2025). No specific Sieve rules.

### E11: Content Behind Paywall/Login (NEW v1.1)
- rules: []
- anti_patterns: [810]
- notes: AP#810 "Placing primary content behind authentication gates" (high). AI crawlers cannot authenticate.

### E12: No NOARCHIVE on AI-Targeted Pages (NEW v1.2)
- rules: [1424]
- anti_patterns: [766, 767]
- notes: Rule 1424 (Bing, 0.98) "NOARCHIVE blocks Copilot citation — limits to URL-only, no rich content extraction." AP#766 "Applying NOARCHIVE to Copilot-targeted content" (high). AP#767 "Applying NOCACHE to Copilot content" (high). Note: NOARCHIVE does NOT affect Google AI Overviews (Rule #1468, 0.93).

### E13: CCBot / LLM Training Crawler Access (NEW v1.3)
- rules: [2016]
- anti_patterns: []
- notes: Rule #2016 (amsive.com, 0.95) "CCBot (Common Crawl) Access for LLM Training Recognition." CCBot is the underlying dataset used to train GPT-4, Claude, Llama. Unlike real-time AI crawlers (PerplexityBot, ChatGPT-User), CCBot access affects training-time brand recognition — brands absent from Common Crawl lack baseline familiarity when LLMs are asked about the category. No explicit anti-pattern but blocking CCBot is widely documented as a training-visibility risk.

---

## Category F: AEO Extraction

### F1: First Paragraph Answers Query (STRENGTHENED v1.3)
- rules: [1448, 1471, 1472]
- anti_patterns: [3, 774, 797, 4698]
- notes: Rule 1448 (Google, 0.95) "Answer-first structure for AI Overview citation." Rule 1471 (Perplexity, 0.97) "Lead with Direct Answer (Inverted Pyramid) for AI Citation." Rule 1472 (Perplexity, 0.97) "Use Specific Verifiable Facts to Enable Citation." AP#3 "AI Answer Evasion" (high). AP#774 "Burying key answers after preamble" (medium). AP#797 "Vague marketing language" (high). v1.3: AP#4698 (Backlinko, high) "Burying the Answer" — Backlinko's own anti-pattern for answer-first failure; strengthens cross-source consensus on F1. (v1.2: added 1471, 1472 — Perplexity-specific)

### F2: Quick-Answer Block
- rules: [1448, 1450]
- anti_patterns: [774, 777]
- notes: Rule 1450 (0.90) "Structured content formats for AI grounding." AP#777 "Publishing unbroken walls of text" (medium).

### F3: FAQ Section
- rules: [1477, 1500, 1600, 1606]
- anti_patterns: [816, 817]
- notes: Rule 1477 (0.93) "Add JSON-LD FAQ schema and semantic HTML5." Rule 1500 (0.93) "Write self-contained FAQ answers." Rule 1600 (0.98) FAQPage required fields. Rule 1606 (0.95) FAQPage JSON-LD implementation. AP#816 "Writing truncated FAQ answers" (medium). AP#817 "Duplicating FAQ across pages" (medium).

### F4: FAQ Semantic Markup
- rules: [1495, 1496]
- anti_patterns: [813, 876]
- notes: Rule 1495 (0.98) "FAQPage requires visible content." Rule 1496 (0.98) "Markup text must match visible content." AP#813 "Hidden/dynamically loaded FAQ." AP#876 "Marking up hidden Q&A."

### F5: FAQ Natural Language Questions
- rules: [1501, 1601]
- anti_patterns: []
- notes: Rule 1501 (0.91) "Align FAQ question phrasing with user query patterns." Rule 1601 (0.95) "FAQPage question name must be complete sentence."

### F6: Headings as Questions (STRENGTHENED v1.3)
- rules: []
- anti_patterns: [773, 4714]
- notes: AP#773 "Using generic heading labels." v1.3: AP#4714 (Backlinko, high) "Neglecting LLM-Friendly Formatting" — question-structured headings are core LLM-friendly formatting; this AP provides cross-source backing.

### F7: Named Entities (STRENGTHENED v1.3)
- rules: []
- anti_patterns: [2, 797, 4714]
- notes: AP#2 "Orphaned entity mentions" (high, entity). AP#797 "Vague marketing language." v1.3: AP#4714 (Backlinko, high) "Neglecting LLM-Friendly Formatting" — entity density is part of Backlinko's LLM-friendly format guidance.

### F8: Specific Facts
- rules: [1420]
- anti_patterns: [775, 797]
- notes: Rule 1420 (0.94) "Avoid unverifiable promotional language in AI-targeted content." AP#775 "Vague promotional language that cannot be grounded" (medium). AP#797 "Vague marketing language" (high).

### F9: Definition-First Writing
- rules: [1448]
- anti_patterns: [3]
- notes: Rule 1448 (0.95) "Answer-first structure." AP#3 "AI Answer Evasion."

### F10: Summary/TL;DR
- rules: []
- anti_patterns: []
- notes: No specific brain entries. Static rule covers this.

### F11: Self-Contained Answer Units (STRENGTHENED v1.3)
- rules: [1500]
- anti_patterns: [777, 805, 4602, 4623]
- notes: Rule 1500 (0.93) "Write self-contained FAQ answers for AI citation eligibility." AP#777 "Unbroken walls of text" (medium). AP#805 "Creating multi-intent pages" (medium). v1.3: AP#4602 (Backlinko, high) "Context-Dependent Sections" — content that requires surrounding paragraphs to make sense cannot be extracted as a citation unit. AP#4623 (Backlinko, high) "Human-Only Structure" — narrative flow without standalone paragraphs/blocks blocks LLM extraction.

### F12: Tables/Lists
- rules: [1450, 1434]
- anti_patterns: []
- notes: Rule 1450 (0.90) "Structured content formats for AI grounding." Rule 1434 (0.92) "AI Mode comparative content optimisation."

---

## Category G: AEO Trust

### G1: Author Byline
- rules: [1475]
- anti_patterns: [798]
- notes: Rule 1475 (0.92) "Associate content with named credentialed author." AP#798 "Publishing anonymous content" (medium).

### G2: Author Schema Credentials
- rules: [1676, 1678, 1679]
- anti_patterns: [798]
- notes: Rule 1676 (0.98) "Person author for E-E-A-T." Rule 1678 (0.92) "Person affiliation/worksFor." Rule 1679 (0.90) "Person hasCredential." AP#798 anonymous content.

### G3: Outbound Citations
- rules: [1478]
- anti_patterns: []
- notes: Rule 1478 (0.92) "Cite primary authoritative sources inline for AI trust signals."

### G4: Publication Date
- rules: [1474]
- anti_patterns: []
- notes: Rule 1474 (0.95) freshness timestamps.

### G5: dateModified
- rules: [1474]
- anti_patterns: [799]
- notes: Rule 1474 (0.95) freshness timestamps. AP#799 "Cosmetic timestamp updates" (medium).

### G6: Organization sameAs
- rules: [1668]
- anti_patterns: [959]
- notes: Rule 1668 (0.98) Organization name+URL. AP#959 "Omitting required url."

### G7: Privacy/Terms
- rules: []
- anti_patterns: []

### G8: HTTPS
- rules: [1489]
- anti_patterns: [812]

### G9: Content Freshness Recency (NEW v1.1)
- rules: [1474]
- anti_patterns: [799]
- notes: Rule 1474 (0.95) "Signal content freshness with visible timestamps." AP#799 "Cosmetic timestamp updates without substantive changes" (medium) — used for cosmetic update detection.

---

## Category H: AEO Selection (Competitor-Relative)

### H1: Content Depth
- rules: [1453]
- anti_patterns: [781, 800]
- notes: Rule 1453 (0.92) "Commodity content exclusion risk." AP#781 "Creating commodity content without unique insight" (high). AP#800 "Publishing thin content that only summarises" (high).

### H2: Unique Data/Research
- rules: [1453]
- anti_patterns: [781]
- notes: Same as H1 — commodity/uniqueness rules.

### H3: FAQ Coverage
- rules: [1477, 1500]
- anti_patterns: []
- notes: Rule 1477 (0.93) FAQ schema. Rule 1500 (0.93) self-contained answers.

### H4: Schema Completeness
- rules: [3]
- anti_patterns: []
- notes: Rule 3 (0.90) schema coverage rule.

### H5: Freshness
- rules: [1474]
- anti_patterns: [799]

### H6: E-E-A-T Signals
- rules: [1456, 1475, 1676]
- anti_patterns: [798]
- notes: Rule 1456 (0.98) "AI content must meet E-E-A-T standards." Rule 1475 (0.92) credentialed author. Rule 1676 (0.98) Person author for E-E-A-T. AP#798 anonymous content.

### H7: Appears in AI Overview
- rules: [1446]
- anti_patterns: []
- notes: Rule 1446 (0.95) "AI Overviews appearance cannot be guaranteed."

### H8: Query Intent Match
- rules: [1448]
- anti_patterns: [805]
- notes: Rule 1448 (0.95) answer-first. AP#805 "Multi-intent pages" (medium).

---

## Category I: GEO

### I1-I7: Brand Presence/Accuracy/Favorability (STRENGTHENED v1.3)
- rules: [564]
- anti_patterns: [807, 4607]
- notes: Rule 564 (Google, 0.95) "Business Profile Claim Required for Maps/Knowledge Panel Visibility" — maps directly to I2 (knowledge panel check). AP#807 "Prioritising backlink quantity over brand authority" (medium). v1.3: AP#4607 (Backlinko, high) "Sparse Off-Site Brand Mentions" — explicitly calls out that brands with few third-party mentions are invisible to LLMs at training time; validates GEO Presence severity calibration. GEO brain coverage improved (69 rules total, up from 19). These checks still rely primarily on knowledge model + WebSearch data. (v1.2: added Rule 564 for I2; v1.3: added Backlinko off-site mentions AP)

### I8: sameAs Links
- rules: [1668]
- anti_patterns: [959]
- notes: Rule 1668 (0.98) Organization name+URL. AP#959 "Omitting required url."

---

## Category J: Entity Consistency

### J1: Organization Name
- rules: [1668]
- anti_patterns: [2]
- notes: Rule 1668 Organization. AP#2 "Orphaned entity mentions."

### J2: Logo
- rules: []
- anti_patterns: []

### J3: URL/Domain
- rules: []
- anti_patterns: [264]
- notes: AP#264 "Conflicting canonical URLs."

### J4: sameAs Resolve
- rules: []
- anti_patterns: [959]
- notes: AP#959 "Omitting required url from schema entities."

---

## Coverage Summary (v1.3 — 103 checks)

| Category | Checks With Mappings | Checks Without | Coverage |
|---|---|---|---|
| A: Technical SEO | 8/12 | A2, A3, A6, A8 | 67% |
| B: Performance | 3/11 | Most (B2-B9) | 27% |
| C: On-Page SEO | 7/13 | C4, C5, C7, C9, C10, C11 | 54% |
| D: Schema | 11/13 | D2, D4 | 85% |
| E: AEO Discovery | 13/13 | — | **100%** |
| F: AEO Extraction | 11/12 | F10 | 92% |
| G: AEO Trust | 8/9 | G7 | 89% |
| H: AEO Selection | 8/8 | — | 100% |
| I: GEO | 2/8 | I1-I7 mostly | 25% |
| J: Entity | 3/4 | J2 | 75% |
| **Total** | **74/103** | **29** | **72%** |

72% of checks have curated Sieve brain mappings. v1.3 changes:
- **E13 (CCBot / LLM Training Crawler Access) added** with mapping to Rule #2016 (amsive.com, 0.95)
- E AEO Discovery coverage now at **13/13 = 100%**
- B10 strengthened with Backlinko LCP/INP threshold rules (#7176, #7190)
- D1 strengthened with Backlinko AP#4763 "Omitting Schema Markup"
- D9 strengthened with Schema.org Rule #7375 "Schema Markup Must Match Visible Content"
- F1 strengthened with Backlinko AP#4698 "Burying the Answer" (cross-source consensus)
- F6 and F7 strengthened with Backlinko AP#4714 "Neglecting LLM-Friendly Formatting"
- F11 strengthened with Backlinko AP#4602 "Context-Dependent Sections" + AP#4623 "Human-Only Structure"
- I1-I7 strengthened with Backlinko AP#4607 "Sparse Off-Site Brand Mentions"

v1.2 changes (carried forward):
- E12 (NOARCHIVE) added with mapping to Rule 1424 + AP#766, #767
- E4 strengthened with NOARCHIVE/NOCACHE rules (#1424, AP#766, #767)
- E5 strengthened with Rule #2015 (AI Crawler JS Avoidance) + Rule #1426 (Bing JS exclusion)
- D3 strengthened with LocalBusiness rules (#1682, #1635, #1636)
- D9 strengthened with FAQ content quality rules (#1602, #1603, AP#815, #816)
- F1 strengthened with Perplexity-specific rules (#1471, #1472)
- I1-I7 strengthened with Rule #564 (Knowledge Panel requirement)

The remaining 29 unmapped checks are basic HTML/performance checks that don't need brain backing.
Sieve brain now has 7,794 rules + 4,533 anti-patterns + 5,827 principles (3x growth since v1.0 — 1,430 new rules + 820 new anti-patterns since v1.2).
Backlinko knowledge docs now fully integrated: AP#4602, #4607, #4623, #4698, #4714, #4763; Rules #7176, #7190.
Schema.org visible-content rule (#7375) now powers the D9 cross-check for markup-vs-rendered text mismatch.

## Maintenance

**When to update this file:**
- After Sieve ingests new documents (new rules may map to existing checks)
- After adding new checks to static-rules.md (new checks need mappings)
- Quarterly verification — confirm listed IDs still exist and confidence scores haven't dropped significantly
- After the periodic review cycle identifies supplementary findings that get promoted to new checks
