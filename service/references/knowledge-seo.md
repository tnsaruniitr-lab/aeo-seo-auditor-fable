# SEO Knowledge Model — How Google Actually Ranks Pages in 2025-2026

Research-backed understanding of Google's ranking signals, based on the 2024 API leak,
antitrust trial testimony, industry studies, and official documentation. Use this to
calibrate SEO check severity and provide expert-level diagnosis.

---

## The Signal Hierarchy (What Actually Matters)

### Tier 1: Dominant Signals (Move Rankings)

**1. Content Relevance + Search Intent Match**
- Text relevance correlates with 90.6% of top-10 results
- Google uses passage-level ranking (live since 2021): individual sections scored independently.
  A 3000-word article can rank for a query answered in paragraph 4
- Intent matching is the primary filter. Google categorizes queries: informational, navigational,
  transactional, commercial investigation. Wrong format = won't rank regardless of quality
- Semantic understanding via MUM/BERT successors handles synonyms natively — keyword density
  and exact-match H2s are outdated

**2. NavBoost / User Interaction Signals (Confirmed Top Signal)**
Revealed during 2023 DOJ antitrust trial. VP Pandu Nayak called it "one of the important signals."
- Tracks: good clicks, bad clicks (pogo-sticking), last longest clicks (final result user dwells on)
- 13-month rolling window of click history per query, segmented by locale and device
- Uses COEC (Clicks Over Expected Clicks) — comparing actual clicks against baseline for each position
- "Instant Glue" variant operates on 24-hour windows for breaking news
- Reduces candidate sets from tens of thousands to hundreds before expensive ranking passes

**What this means for the auditor:** A page that answers the query well and keeps users engaged
will outrank a page with better technical SEO. User satisfaction signals dominate. Our audit can't
measure NavBoost directly, but we CAN assess: Does this content match the query intent? Is the
answer easy to find? Would a user stay or bounce?

**3. Links + Authority**
- Backlinks remain top-3 signal (confirmed Google 2016, reinforced by 2024 API leak)
- API leak revealed `sourceType`: HIGH_QUALITY, MEDIUM_QUALITY, LOW_QUALITY anchor sources
- `siteAuthority` per-domain metric confirmed despite years of Google denials
- Topical alignment of linking domains matters more than raw authority
- March 2024 core update made 4 changes to link signal processing, devaluing manipulative patterns

**What this means for the auditor:** We can't measure backlinks without Ahrefs/Moz. But we CAN
check outbound links (citations to authoritative sources = trust signal) and internal linking
structure. Frame link-related findings honestly: "We cannot assess your backlink profile. For a
complete authority audit, use Ahrefs/Moz alongside this report."

### Tier 2: Important Signals (Influence Rankings)

**4. E-E-A-T (Experience, Expertise, Authoritativeness, Trust)**
NOT a ranking factor and NOT computed as a score. It's a framework used by ~16,000 human quality
raters. Their assessments train the algorithm to recognize quality patterns.

How Google technically approximates each signal:

- **Experience**: Frequency/consistency of publication on specific topics. First-instance content
  (pioneering coverage) scores higher. Evidence of real-world interaction with subject
- **Expertise**: Co-occurrence of related entities (topical depth). Citation frequency by credible
  sources. Author entity attributes (confirmed in API: `author` fields, `isAuthor` booleans)
- **Authoritativeness**: Steady link acquisition long after publication. PageRank + link diversity.
  Consistent high rankings across multiple query types
- **Trust**: Knowledge-Based Trust (factual accuracy vs known facts). Proximity to trusted seed
  sites in link graph. Domain reputation. HTTPS. Long-term engagement metrics

96% of AI Overview citations come from sources with strong E-E-A-T signals — this is where
SEO and AEO converge.

**What this means for the auditor:** Check for: author bylines with credentials, Person schema
with sameAs, Organization schema, publication dates, outbound citations, HTTPS. These are the
observable proxies for E-E-A-T. Don't call it an "E-E-A-T score" — frame it as "credibility
signals that Google's quality assessment correlates with."

**5. Brand + Entity Signals**
- `chromeInTotal` — site-level Chrome browser visits confirmed as signal (API leak)
- Direct URL navigation = trust/brand signal
- `smallPersonalSite` attribute exists — may trigger different ranking treatment
- Author entity tracking confirmed for news content, likely broader

**6. Content Freshness (Three Signals)**
API leak revealed three date signals: `bylineDate`, `syntacticDate`, `semanticDate`
- Freshness matters more for time-sensitive queries than evergreen content
- dateModified in schema maps to these signals
- A page updated 2 months ago is not "stale" for evergreen content but IS stale for AI citation

### Tier 3: Threshold Signals (Pass and Move On)

**7. Core Web Vitals** — see knowledge-performance.md for full analysis.
TL;DR: ~10-15% of signal weight. Caps at "Good" thresholds. Tiebreaker, not driver.

**8. Technical SEO** — HTTPS, canonical, indexability, mobile-friendly
These are prerequisites, not differentiators. A broken canonical blocks indexing.
A perfect canonical doesn't boost rankings.

**9. Structured Data / Schema**
Google's John Mueller (2025): "Structured data does NOT directly influence rankings."
What it does: enables rich results (25-30% CTR boost), 73% higher AI Overview citation rate,
3.2x more AI answer appearances. It's a VISIBILITY and AI-CITATION factor, not a ranking factor.

---

## The 2024 API Leak — What It Proved vs What Google Claimed

| What Google Said | What the Leak Revealed |
|---|---|
| "We don't have domain authority" | `siteAuthority` attribute exists and is used |
| "We don't use click data for ranking" | NavBoost with `goodClicks`, `badClicks`, `lastLongestClicks` |
| "We don't use Chrome data" | `chromeInTotal` tracks Chrome browser visits per site |
| "There is no sandbox" | `hostAge` used to "sandbox fresh spam in serving time" |
| "Author not a ranking factor" | `author` fields and `isAuthor` booleans stored per document |
| Content freshness is simple | Three date signals: `bylineDate`, `syntacticDate`, `semanticDate` |

Additional attributes: `OriginalContentScore` (uniqueness), `titlematchScore` (title-query
relevance), `avgTermWeight` (font size emphasis tracking).

**What this means for the auditor:** When Google says "X doesn't matter," be skeptical.
The API leak revealed systematic gaps between public statements and actual implementation.
However, don't overcorrect — the leak shows attributes exist, not how heavily they're weighted.

---

## Helpful Content Update — What Changed

- HCU was **deprecated as standalone system** in March 2024, folded into core ranking
- No longer a separate "helpful content classifier" — evaluation is now primarily page-level
- Previously site-wide: if enough unhelpful content detected, entire domain suppressed
- Post-March 2024: page-level assessment, though site-wide signals still occasionally considered
- Recovery from HCU hits typically takes 2-6 months, aligning with next core update
- One documented recovery: removed 38% of thin content, restructured internal linking,
  improved E-E-A-T signals → began recovering during March 2024 core update

**Patterns that get flagged:**
- AI-generated content without human editorial value-add
- Content created for search engines rather than users
- Template-based thin content across many pages
- Excessive ads relative to content value
- Lack of first-hand experience signals

**What this means for the auditor:** Content quality assessment should look for:
depth beyond what AI could generate, first-person experience signals, original data/research,
editorial indicators. A page full of generic "In today's fast-paced world..." preamble is
exactly what HCU targets.

---

## AI Overviews — Critical SEO Intersection

**Citation overlap with organic results is DROPPING fast:**
- Early 2025: ~76% of AI Overview citations from top-10 organic results
- October 2025: 54% overlap (BrightEdge)
- Late 2025/Early 2026: dropped to 38% after Gemini 3 upgrade (January 2026)

This means: pages outside top-10 organic now have significantly better AI citation chances.
And pages IN the top-10 can no longer assume AI Overview presence.

**AI Overviews now appear in 50%+ of searches** (up from 18% in March 2025).

**What this means for the auditor:** SEO ranking position alone no longer guarantees AI visibility.
A page can rank #3 organically but not appear in the AI Overview, while a #15 page gets cited.
This is why AEO checks are separate from SEO checks — they measure different things.

---

## Myths That Are Now Wrong

1. **"Longer content ranks better"** — Passage ranking means a well-structured 1200-word article
   with a perfect 150-word answer passage beats a 5000-word article that buries the answer

2. **"Google can't detect AI content"** — Quality rater guidelines now explicitly address AI content.
   The issue isn't detection; it's whether content adds value beyond what AI already knows

3. **"Domain age is a major factor"** — `hostAge` is for spam sandboxing, not a positive signal

4. **"Publish frequently to rank"** — NavBoost's 13-month memory means a single excellent page
   earning sustained clicks over months outperforms frequent thin publishing

5. **"Perfect CWV scores boost rankings"** — Benefit caps at "Good" thresholds

6. **"Schema is a ranking factor"** — It's not. It's a visibility/AI-citation factor

7. **"Disavow files matter"** — Google ignores most bad links automatically

8. **"Add author bio boxes for E-E-A-T"** — Google evaluates the actual entity's web reputation,
   not just on-page claims. A bio box without a real, verifiable author entity is theater

---

## Site Reputation Abuse (Parasite SEO) — 2024-2026 Crackdown

**What Google killed:**
- Third-party content sections on high-authority domains (coupon pages on news sites)
- "Rent a subfolder" model (companies paying established domains to host their content)
- Expired domain acquisition for link equity transfer
- First-party oversight/partial ownership loopholes closed November 2024

**What this means for the auditor:** If a site has third-party content sections or subfolder
partnerships, flag as risk. Also relevant for entity consistency — if a brand's content lives
on a rented subdomain, it fragments entity signals.

---

## How to Grade SEO Findings Using This Knowledge

### Severity Calibration Based on Signal Tier

| Finding | Severity | Rationale |
|---|---|---|
| Content doesn't match query intent | CRITICAL | Tier 1: intent mismatch = won't rank regardless |
| No answer in first 300 words for target query | HIGH | Passage ranking looks for direct answers |
| Missing canonical tag | CRITICAL | Blocks proper indexing |
| noindex on key page | CRITICAL | Complete ranking prevention |
| No H1 tag | HIGH | Title/heading alignment affects titlematchScore |
| Missing meta description | MEDIUM | Doesn't affect ranking, affects CTR |
| No author byline or Person schema | HIGH | E-E-A-T proxy, 96% of AI citations from E-E-A-T sources |
| No dateModified | HIGH | Three date signals in API, freshness matters |
| No Organization schema | MEDIUM | Entity clarity, not a ranking factor |
| Missing alt text on images | MEDIUM | Accessibility + image search, minor ranking signal |
| URL has underscores instead of hyphens | LOW | Cosmetic, minimal impact |
| Meta description slightly over 155 chars | COSMETIC | Google may truncate, no ranking impact |

### What We Can Assess vs What We Can't

**CAN assess (and should):**
- Content-query relevance and intent match
- E-E-A-T observable proxies (author, dates, citations, schema)
- Technical indexability (canonical, robots, meta robots)
- Content structure quality (heading hierarchy, answer positioning)
- Schema completeness and validity
- Internal linking structure
- On-page freshness signals

**CANNOT assess (and should acknowledge):**
- Backlink profile and authority metrics
- NavBoost / user engagement signals
- Chrome clickstream data
- Actual domain authority score
- Real CrUX / CWV field data
- Competitor backlink comparison
- Historical ranking data

**For the auditor:** Be transparent about what's outside our measurement scope. Frame findings
as "based on on-page analysis" and recommend backlink tools (Ahrefs, Moz) for the authority
dimension. Never imply we have a complete ranking picture.

---

## Key Insight for the Auditor

The single most important SEO question is NOT "does this page have good technical SEO?"

It's: **"Does this content satisfy the search intent better than the competition?"**

Technical SEO is the floor. Content-intent match is the ceiling. Most pages we audit will have
adequate technical SEO. The real findings will be about content structure, intent alignment,
E-E-A-T gaps, and freshness — the Tier 1 and Tier 2 signals that actually move rankings.
