# AEO Framework — 4-Stage Answer Engine Optimization

## How Answer Engines Work

When an LLM-powered answer engine (ChatGPT, Perplexity, Google AI Overviews, Claude) answers a query, it follows a pipeline:

1. **Discovery** — Find candidate pages via search index or direct crawl
2. **Extraction** — Pull structured, citable content from those pages
3. **Trust** — Evaluate source credibility and recency
4. **Selection** — Choose which source(s) to cite in the final answer

Each stage is a filter. Failing at any stage means the page is dropped from consideration. The audit mirrors this pipeline so users understand exactly WHERE their page fails.

---

## Stage 1: Discovery (Checks E1-E10)

**Question**: "Can AI crawlers find and access this content?"

**What happens at this stage**: AI crawlers (PerplexityBot, BingPreview, ChatGPT-User, ClaudeBot, Applebot, GoogleBot) attempt to access the page. If blocked by robots.txt, nosnippet directives, or JS-rendering requirements, the page never enters the answer pipeline.

**Why pages fail here**:
- robots.txt blocks AI-specific user agents (common — many sites copied "block AI" configs)
- Content is entirely JS-rendered, so crawlers that don't execute JS see an empty page
- nosnippet or max-snippet:0 meta tags prevent snippet extraction
- Page isn't in sitemap, making discovery less likely
- Content hidden behind accordions/tabs without HTML fallback

**Impact of failure**: Total — if the page can't be found, nothing else matters. Discovery failures are always BLOCKING severity.

**Fix pattern**: Mostly configuration changes (robots.txt, meta tags). Low effort, highest impact.

---

## Stage 2: Extraction (Checks F1-F12)

**Question**: "Can the answer engine pull a clean, citable answer from this page?"

**What happens at this stage**: The AI system has the page content. It now needs to identify extractable answer chunks — self-contained, factual, directly responsive to the query.

**Why pages fail here**:
- Opening doesn't answer the query (starts with "In today's fast-paced world...")
- No structured answer blocks (FAQ, tables, lists, comparison grids)
- Content uses vague language instead of named entities and specific facts
- No summary/TL;DR section
- Headings are creative/branded instead of descriptive/question-based
- Content isn't atomically structured (sections can't stand alone)

**Impact of failure**: High — the page might be found but won't be cited because there's nothing clean to extract. Extraction failures are LEVERAGE severity — fixing them directly increases citation likelihood.

**Fix pattern**: Content restructuring + rewriting. Medium effort, high impact. The fix generator produces before/after rewrites for these.

---

## Stage 3: Trust (Checks G1-G8)

**Question**: "Does the answer engine consider this source reliable?"

**What happens at this stage**: The AI system has extractable content but must decide if the source is trustworthy enough to cite. Trust signals include author credibility, source citations, freshness, and institutional markers.

**Why pages fail here**:
- No author attribution (anonymous content)
- No publication or update dates
- No outbound citations to primary sources
- Missing Organization schema or institutional signals
- Outdated content (no dateModified, stale references)

**Impact of failure**: Medium-high — the page has good content but lacks the credibility signals that make an AI system confident in citing it. Particularly important for YMYL (Your Money, Your Life) topics.

**Fix pattern**: Adding metadata, author info, and citations. Low-medium effort, medium-high impact.

---

## Stage 4: Selection (Checks H1-H8)

**Question**: "Will the answer engine choose this page OVER the alternatives?"

**What happens at this stage**: Multiple candidate pages have been found, extracted, and trusted. The AI system now picks the best source(s). This is relative — the page is compared against competitors.

**Why pages fail here**:
- Competitors have deeper, more comprehensive content
- Competitors have better FAQ coverage
- Competitors have more complete schema
- Competitors are fresher (updated more recently)
- Competitors have stronger E-E-A-T signals
- Content doesn't match query intent as well as competitors

**Impact of failure**: This is the competitive layer. The page might be individually good but relatively worse. Selection failures are LEVERAGE severity and require the competitor gap analysis to diagnose.

**Fix pattern**: Content expansion, competitive differentiation, freshness updates. Medium-high effort, high impact. Fixes are informed by specific competitor comparison data.

---

## AEO Score Calculation

Each stage produces a subscore:

```
Stage Score = SUM(passed_check_weights) / SUM(applicable_check_weights) * 100
```

Composite AEO Score (weighted):
- Discovery: 30% (highest — it's a binary gate)
- Extraction: 30% (equally important — this is what gets cited)
- Trust: 20%
- Selection: 20%

**AEO Score = (Discovery * 0.30) + (Extraction * 0.30) + (Trust * 0.20) + (Selection * 0.20)**

---

## Interpreting AEO Scores

| Score | Meaning | Action |
|---|---|---|
| 90-100 | AI-ready — page has strong citation potential | Maintain freshness, monitor competitors |
| 70-89 | Good foundation — specific gaps reduce citation likelihood | Fix extraction or trust gaps (usually 2-3 targeted fixes) |
| 50-69 | Significant gaps — page may be found but rarely cited | Content restructuring + metadata fixes needed |
| 30-49 | Major issues — page is largely invisible to AI engines | Discovery fixes first, then content overhaul |
| 0-29 | Not AI-ready — fundamental barriers to AI visibility | Start with robots.txt/rendering, then rebuild content for extraction |

---

## Stage-to-Fix Mapping

| If lowest stage is... | Primary fix area | Typical fixes |
|---|---|---|
| Discovery | Configuration | Update robots.txt, add server-side rendering, fix meta tags |
| Extraction | Content structure | Rewrite intro, add FAQ, create tables, restructure headings |
| Trust | Metadata + authority | Add author info, dates, citations, Organization schema |
| Selection | Competitive content | Deepen content, add unique data, improve freshness, expand FAQ |
