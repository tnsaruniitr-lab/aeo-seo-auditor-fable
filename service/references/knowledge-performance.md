# Performance Knowledge Model

This document encodes how page performance ACTUALLY affects search rankings and AI visibility,
based on research data — not generic "speed matters" advice. Use this to calibrate severity
and messaging for all performance-related findings.

---

## Core Truth: Performance Is a Threshold, Not a Gradient

Google has confirmed Core Web Vitals are a ranking signal, but large-scale studies consistently
show the impact is minor once thresholds are met:

- **Backlinko (208K pages)**: Weak correlations between CWV scores and user behavior metrics.
  No significant correlation between CLS and any behavioral metric.
- **Dollar Pocket (10M results)**: Pages at position #1 are only ~10% more likely to pass CWV
  than pages at position #9.
- **DebugBear/RUMvision**: CWV functions as a tiebreaker between otherwise comparable pages.
- **Google's own classification**: In April 2023, Google reclassified page experience from a
  ranking "system" to a ranking "signal" — a meaningful downgrade. They retired the Page
  Experience System, Mobile-Friendly System, and Page Speed System as named systems.

**What this means for the auditor**: Never position performance as a primary ranking lever.
Frame it as: "Performance failures can hold you back, but performance improvements alone won't
push you forward. Content, authority, and relevance dominate."

---

## Official CWV Thresholds (As of 2025-2026)

| Metric | Good | Needs Improvement | Poor | What It Measures |
|---|---|---|---|---|
| LCP | <= 2.5s | 2.5s - 4.0s | > 4.0s | Time until largest content element renders |
| INP | <= 200ms | 200ms - 500ms | > 500ms | Responsiveness to user interactions (replaced FID March 2024) |
| CLS | <= 0.1 | 0.1 - 0.25 | > 0.25 | Visual stability during load |

**Critical detail**: These are measured at the **75th percentile of real user field data (CrUX)**,
not Lighthouse lab tests. 75% of actual page loads must meet the threshold.

---

## Lighthouse Scores Are NOT Ranking Signals

This is widely misunderstood. Google's John Mueller explicitly confirmed:
**Google does not use the X/100 Lighthouse score for search rankings.**

- Lighthouse = lab data (single synthetic test, controlled conditions)
- Rankings = field data (real Chrome users over 28 days, CrUX)
- A page can score 20 on Lighthouse and rank well if real users have good experiences
- A page can score 98 on Lighthouse and fail CrUX if real users are on slow connections

**For the auditor**: Never report Lighthouse scores as ranking factors. Use Lighthouse
diagnostically — to identify WHAT is slow — but don't tie the score to SEO impact.
We don't run Lighthouse anyway (no DevTools Protocol), so report HTML-observable indicators.

---

## What Performance Issues ACTUALLY Block AI Crawlers

This is where performance becomes critical — not for Google, but for AI visibility:

### AI Crawlers Are Less Patient Than Googlebot

- GPTBot, ClaudeBot, PerplexityBot make **one request and move on**. No retries, no JS execution.
- AI crawlers now make **3.6x more requests** than traditional search crawlers (2025 data).
- **Zero evidence of JavaScript execution** by GPTBot across 500M+ requests analyzed.
  ClaudeBot and PerplexityBot behave the same.

### What Actually Blocks AI Crawlers

| Blocker | Impact | Threshold |
|---|---|---|
| Slow TTFB | AI crawler moves on, content never ingested | > 800ms is risky, > 3s is fatal |
| JS-rendered content | Invisible to all AI crawlers | Any content requiring JS to render |
| Page timeout | Crawler abandons request | > 5s total load |
| robots.txt blocking | Complete exclusion | Any Disallow for AI user agents |

### What Does NOT Meaningfully Affect AI Crawlers

| Non-issue | Why |
|---|---|
| Image optimization | AI crawlers extract text, not images |
| CSS performance | AI crawlers don't render CSS |
| Client-side CLS | AI crawlers don't measure layout shifts |
| INP | AI crawlers don't interact with pages |
| Lighthouse score | Irrelevant to crawl behavior |

---

## How to Grade Performance Findings

### Severity Calibration

| Finding | Severity | Rationale |
|---|---|---|
| TTFB > 3s | BLOCKING | Both Google and AI crawlers affected |
| JS-rendered content (no SSR) | BLOCKING for AEO | AI crawlers see nothing |
| No HTTPS | BLOCKING | Google requirement, browser warnings |
| LCP > 4s (Poor) | LEVERAGE | Below Google threshold, user bounce risk |
| LCP 2.5-4s (Needs Improvement) | COSMETIC | Minor tiebreaker, not a driver |
| LCP < 2.5s (Good) | PASS | No further ranking benefit from improvement |
| CLS > 0.25 (Poor) | LEVERAGE | Noticeable instability, below threshold |
| CLS 0.1-0.25 | COSMETIC | Minor, questionable ranking impact |
| INP > 500ms (Poor) | LEVERAGE | Interaction lag, below threshold |
| INP 200-500ms | COSMETIC | Minor, most pages don't need fast interactions |
| No compression (Gzip/Brotli) | LEVERAGE | Affects load time, easy fix |
| Render-blocking resources | COSMETIC | Minor contributor to LCP |
| Images not optimized | COSMETIC | Helps page weight, not a ranking signal |
| No lazy loading | COSMETIC | Minor, affects page weight |
| Missing Cache-Control | COSMETIC | Affects repeat visits, not rankings |
| Mixed content (HTTP on HTTPS) | LEVERAGE | Browser warnings, trust signal |

### What We Can and Cannot Measure

Since we use WebFetch (not Lighthouse or Chrome DevTools):

**CAN measure:**
- TTFB (from WebFetch response timing)
- HTTPS status
- Compression headers (Content-Encoding)
- Cache-Control headers
- Render-blocking resources in `<head>` (script without async/defer, stylesheets)
- Image formats (webp/avif from src attributes)
- Lazy loading attributes
- Mixed content (http:// in src/href on https page)
- Whether content is in raw HTML vs JS-rendered (content volume check)
- DOM complexity indicators (nesting depth, element count)

**CANNOT measure (and should NOT claim to):**
- Actual LCP time
- Actual INP time
- Actual CLS score
- Lighthouse score
- Real CrUX data
- Actual page weight (we see HTML, not all resources)

**For the auditor**: Report what we observe as indicators, not as measured CWV scores.
Say "LCP risk: large hero image without explicit dimensions and lazy loading — likely above
2.5s on mobile" not "LCP: 3.2s (Poor)."

---

## Messaging Framework

### When performance is fine:
"Your page has no performance blockers. CWV indicators suggest acceptable load times. Focus
your optimization effort on content and AEO signals — performance is not holding this page back."

### When performance has issues:
"[Specific issue] may affect both user experience and AI crawler access. Server response time
is the most critical performance metric for AI visibility — AI crawlers make one request and
move on if the response is slow."

### When performance is catastrophic:
"This page has performance issues that likely prevent AI crawlers from accessing your content.
TTFB/rendering problems must be fixed before any content or schema optimization will matter."

### Never say:
- "Your Lighthouse score is X" (we don't measure it, and it's not a ranking factor)
- "Improving speed will boost your rankings" (oversimplified — only true if currently failing)
- "Performance is the main issue" (unless TTFB > 3s or JS-only rendering)

---

## Key Insight for the Auditor

The most important performance finding is NOT about CWV. It's:

**"Is your content available in raw HTML, or does it require JavaScript to render?"**

If JS-rendered: Google will eventually render and index it (with delay). AI crawlers never will.
This single check is more important for AI visibility than all CWV metrics combined.
