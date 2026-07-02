# Competitor Gap Analysis — Output Template

## How to Present Competitor Comparison

### Summary Table (Layer 1 — Executive Diagnosis)

```
## Competitor Comparison — "[target query]"

| Signal | Your Page | Comp. 1 | Comp. 2 | Comp. 3 | Comp. 4 | Comp. 5 |
|---|---|---|---|---|---|---|
| **Answer Position** | Para 4 | Sent 1 | Sent 2 | Sent 1 | Para 2 | Sent 1 |
| **Word Count** | 800 | 2,100 | 1,600 | 2,400 | 1,900 | 1,200 |
| **FAQ Pairs** | 0 | 8 | 6 | 12 | 5 | 0 |
| **Schema Types** | 1 | 3 | 2 | 4 | 2 | 1 |
| **dateModified** | 14mo ago | 3wk | 6wk | 2mo | 1mo | 8mo |
| **Author Byline** | No | Yes+bio | Yes | Yes+creds | No | Yes |
| **Comparison Table** | No | Yes | Yes | No | Yes | No |
| **Outbound Citations** | 0 | 5 | 3 | 8 | 2 | 1 |
| **Named Entities** | 2 | 7 | 5 | 9 | 6 | 3 |
```

Color/indicator guide:
- Your page value is BEST or tied for best → highlight as strength
- Your page value is WORST → highlight as critical gap
- Your page value is below median → highlight as gap

### Gap Narrative (Layer 1)

After the table, 3 bullets maximum:

```
**Key Competitive Gaps:**

1. **Answer position**: Top 3 competitors answer the query in their first sentence.
   Your page doesn't directly answer until paragraph 4. This is the #1 reason for
   low citation likelihood.

2. **FAQ coverage**: 4 of 5 competitors have FAQ sections (avg 7.75 pairs).
   Your page has none. FAQ is the most extractable format for AI answer engines.

3. **Content freshness**: Competitor average lastmod is 2.5 months ago.
   Your page hasn't been updated in 14 months. AI systems heavily weight recency.
```

### Detailed Competitor Profiles (Layer 2)

For each competitor, a brief structural profile:

```
### Competitor 1: [URL]
- **Title**: [title tag text]
- **Page Type**: Blog article
- **Word Count**: 2,100
- **Structure**: H1 + 8 H2s + 12 H3s, 2 tables, 1 comparison grid
- **Schema**: Article + FAQPage + BreadcrumbList (8 FAQ pairs)
- **Answer Format**: Direct definition in first sentence, expanded with bullet list
- **Author**: Jane Smith, "Senior Product Analyst" — Person schema with LinkedIn sameAs
- **Freshness**: Published 2025-06-15, Modified 2026-03-20
- **Unique Content**: Original survey data (n=500), proprietary benchmarks
- **Citations**: 5 outbound links (3 to .gov/.edu sources)
- **What they do better**: Deeper content, original data, complete schema, fresh
- **What you do better**: [identify any advantages — important for balanced analysis]
```

### Gap-to-Fix Mapping (Layer 1)

Each significant gap maps to a specific fix:

```
| Gap | Impact | Fix | Effort |
|---|---|---|---|
| No FAQ vs avg 7.75 pairs | High | Generate 8 FAQ pairs (provided below) | Easy |
| Answer at para 4 vs sent 1 | High | Rewrite intro (before/after below) | Easy |
| 14mo stale vs avg 2.5mo | High | Update content + add dateModified | Moderate |
| 0 citations vs avg 3.8 | Medium | Add 3-5 outbound links to sources | Easy |
| No comparison table vs 3/5 have | High | Generate comparison table (below) | Moderate |
| 2 entities vs avg 6 | Medium | Name specific products/tools | Easy |
```

---

## Competitor Selection Rules

1. **Primary source**: Top 5 organic SERP results for the primary target query
2. **Exclude**: Results from the same domain as the audited page
3. **Exclude**: Results that are clearly different content types (e.g., video results for a blog audit)
4. **Include**: Even if they outrank the audited page (especially if they do)
5. **Label each**: "[Rank #X for '{query}']" so the user understands positioning

If fewer than 5 organic results are available (common for niche queries), use what's available and note the count.

---

## What to Extract from Each Competitor

When crawling competitor pages via WebFetch, extract these signals:

### Structural Signals (deterministic)
- Title tag text + char count
- Meta description text + char count
- H1 text
- Heading count by level (H2, H3, H4)
- Word count (body text)
- FAQ pair count (from HTML structure or FAQPage schema)
- Table count
- List count (ol + ul in body)
- Image count + alt text coverage
- Internal link count
- External link count
- Schema types present
- Schema field completeness (count of fields per type)

### Content Signals (LLM-assessed)
- Answer position (which paragraph first directly answers the query)
- Named entity count (specific products, brands, people, places mentioned)
- Factual density (claims with numbers/dates/percentages per 500 words)
- Writing style (definition-first vs narrative)
- Unique insights (data, research, perspectives not found elsewhere)

### Authority Signals
- Author name + credentials visible
- Author schema present with sameAs
- datePublished + dateModified
- Outbound citation count (links to primary sources)
- Organization schema completeness

---

## Presentation Rules

1. **Never present raw data without interpretation** — always say what the gap means
2. **Identify your page's strengths too** — balanced analysis builds trust
3. **Rank gaps by impact** — not all gaps are equal
4. **Connect every gap to a fix** — diagnosis without prescription is useless
5. **Use specific numbers** — "4 of 5 competitors have FAQ" not "most competitors have FAQ"
6. **Name competitors by domain** — transparency about who you're compared against
