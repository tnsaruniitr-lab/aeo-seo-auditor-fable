# GEO Knowledge Model — How Brands Appear in Generative AI Responses

Based on the Princeton/Georgia Tech/Allen AI GEO paper (KDD 2024), industry research, and
documented AI engine behavior. Use this for GEO check calibration and brand visibility diagnosis.

---

## The GEO Research Paper — The Field's Foundation

**Paper:** "GEO: Generative Engine Optimization" — Aggarwal et al.
**Published:** KDD 2024 (30th ACM SIGKDD), Barcelona, August 2024
**Validated on:** Perplexity.ai with real visibility improvements up to 37%

### Key Findings

**Optimization strategies ranked by effectiveness:**

| Strategy | Visibility Boost | Best For |
|---|---|---|
| Cite Sources | 30-40% | All domains — adding authoritative citations is universally effective |
| Quotation Addition | 30-40% | People/Society, Explanation, History content |
| Statistics Addition | 30-40% | Law/Government, Opinion queries |
| Fluency Optimization | 15-30% | All domains |
| Easy-to-Understand Rewrites | 15-30% | Technical domains |
| Authoritative Tone | 10-20% | Professional/business content |
| Keyword Stuffing | NEGATIVE | All domains — hurts visibility |

**Critical finding:** Effectiveness varies SIGNIFICANTLY across domains. There is no universal
optimization. A law firm page benefits most from statistics; a tech blog benefits from citations
and simplification. The paper explicitly frames GEO as a black-box optimization problem.

**What this means for the auditor:** When recommending GEO fixes, the page's industry/domain
must inform which strategies to emphasize. Don't blanket-recommend the same fixes for all pages.

---

## How AI Systems Form Brand Representations

### Training Data vs RAG Retrieval — Two Different Mechanisms

**Training data** (how LLMs "know" brands natively):
- Based on web content as of training cutoff
- Biased toward frequently mentioned, well-documented entities
- Stale — can be months or years out of date
- Brands with Wikipedia articles, extensive coverage, many web mentions have stronger representations
- Small/new brands may not exist in training data at all

**RAG retrieval** (how LLMs get current brand info):
- Based on real-time or recently indexed web content
- Quality depends on what pages the engine finds and selects
- Brand description comes from the highest-authority pages the engine retrieves
- If your homepage describes you poorly, that's what the AI quotes
- If third-party sites describe you differently than you describe yourself, the AI may cite either

**What this means for the auditor:** GEO has two fronts:
1. **Controllable**: How your own pages describe the brand (homepage, about, schema, content)
2. **Partially controllable**: How external sources describe you (review sites, directories, press)
We can assess #1 fully. We can only observe #2 via web search.

---

## What Makes a Brand "Known" to AI Systems

### Entity Signals That Establish Brand Identity

**On-site (directly controllable):**
- Organization schema with complete description, sameAs, foundingDate, contactPoint
- Consistent brand name across every page (title, schema, OG, footer, content)
- Clear one-sentence brand definition in extractable position (first paragraph of homepage)
- Product/service pages with structured schema
- Author entities (Person schema) linked to Organization

**Off-site (partially controllable):**
- Wikipedia/Wikidata entry — strongest entity establishment signal
- Google Knowledge Panel — indicates Google recognizes the entity
- Presence on review aggregators (G2, Capterra, Trustpilot, Product Hunt)
- Industry directory listings
- Press coverage with brand mentions in entity-rich context
- "Best [category]" listicle inclusion
- "[Brand] vs [Competitor]" content existence

**Measurable by the auditor:**
- Organization schema completeness and consistency → YES
- Brand description consistency across site pages → YES
- sameAs links that resolve to real profiles → YES
- Web search results for brand name → YES (via WebSearch)
- Presence in "best [category]" results → YES (via WebSearch)
- Knowledge Panel existence → YES (via WebSearch)
- Wikipedia entry → PARTIALLY (can check via WebSearch)
- Third-party review site presence → PARTIALLY (via WebSearch)

---

## Brand Accuracy in AI Responses

### Why AI Gets Brands Wrong

1. **Inconsistent self-description**: Homepage says "collaboration platform," about page says
   "project management tool," schema says "SaaS company." AI picks randomly among these
2. **Stale information**: Brand pivoted or changed pricing but old descriptions persist on web
3. **Third-party dominance**: Competitor comparison pages describe the brand more prominently
   than the brand's own site, and the comparison framing may be unfavorable
4. **Feature mismatch**: Product features listed differently across site pages, pricing pages,
   and external review sites
5. **No extractable brand definition**: If the homepage opens with "Welcome to the future of
   work" instead of "Acme is a project management tool for remote teams," AI has no clean
   entity definition to extract

### How to Fix It (What the Auditor Should Recommend)

**The canonical brand sentence**: One sentence that describes the brand, used identically in:
- Homepage first paragraph
- About page first paragraph
- Organization schema description field
- og:description (or close variant)
- Social media profile bios (LinkedIn, Twitter)

Example: "Acme is a project management platform for remote teams with built-in time tracking,
Gantt charts, and Slack integration."

This gives every AI system the same extractable definition. Consistency = accuracy.

---

## Brand Favorability in AI Responses

### What Influences How AI Positions a Brand

**Positive signals:**
- High ratings on review aggregators (G2, Capterra scores)
- Favorable "[Brand] vs [Competitor]" content (especially brand-owned)
- Case studies with specific metrics ("saved 40% of time" with named customers)
- Awards, recognitions, certifications mentioned in extractable format
- Use-case-specific positioning ("best for remote teams," "best for startups")
- Social proof with numbers (customer count, companies served, ARR)

**Negative signals:**
- Negative reviews dominating search results
- Competitor comparison content (written by competitors) positioning brand unfavorably
- Brand doesn't own its comparison narrative
- Outdated pricing or discontinued features appearing in AI responses
- No specific use-case positioning — brand is "generically mentioned" not "recommended for X"

### What the Auditor Can Check

| Signal | How to Check | Impact |
|---|---|---|
| Review scores | WebSearch "[brand] reviews" | Directly influences AI sentiment |
| Comparison ownership | WebSearch "[brand] vs" — does brand own any results? | Controls narrative |
| Use-case positioning | Check homepage/product pages for specific audience targeting | Specific > generic |
| Social proof density | Check for customer counts, case study results on site | Extractable proof points |
| Negative result dominance | WebSearch brand name — what's the sentiment? | AI reflects web sentiment |

---

## GEO Scoring Philosophy

Unlike SEO and AEO where we check specific technical elements, GEO is inherently:
- **More subjective** — "Does AI describe this brand correctly?" requires judgment
- **Harder to measure** — We can't query every AI system in real time
- **Slower to change** — Entity reputation changes over months, not days
- **Partially outside control** — Third-party content influences AI brand perception

### What We CAN Score Confidently:
- On-site entity consistency (Organization schema, brand name, description) → deterministic
- sameAs link validity → deterministic
- Brand definition extractability → structural check + LLM judgment
- Schema completeness for entity signals → deterministic

### What We Score Directionally (with caveats):
- Brand presence in "best [category]" searches → proxy via WebSearch
- AI description accuracy → proxy via WebSearch snippets
- Brand sentiment → proxy via WebSearch results
- Competitor positioning → proxy via WebSearch "[brand] vs" results

### What We Acknowledge We Can't Score:
- What ChatGPT/Claude/Perplexity actually say about the brand in a live query
- Training data representation (what LLMs "know" without retrieval)
- Full external entity graph (Wikipedia, Wikidata, Knowledge Graph internals)

**For the auditor:** GEO scores should be presented as "directional assessment" not "definitive
measurement." Use language like: "Based on web search signals, your brand's AI presence appears
[strong/moderate/weak]" not "Your GEO score is 45%."

---

## Domain-Specific GEO Recommendations

Based on the GEO paper's finding that strategy effectiveness varies by domain:

| Industry/Domain | Top GEO Strategies | Why |
|---|---|---|
| SaaS/Technology | Source citations, comparison tables, feature specificity | Technical audiences, competitive landscape |
| Legal/Finance | Statistics, authoritative tone, regulatory citations | YMYL, trust-critical, data-driven |
| Healthcare | Expert credentials, study citations, E-E-A-T signals | YMYL, expertise-critical |
| E-commerce | Product schema, reviews, pricing transparency | Transaction-intent, comparison shopping |
| Local Business | NAP consistency, LocalBusiness schema, review management | Location-specific, review-dependent |
| Media/Publishing | Author entities, original reporting, freshness | Authority + timeliness |
| Education | Easy-to-understand rewrites, structured explanations | Clarity + accessibility |

---

## GEO Fix Types

| Fix | Effort | Expected Impact | Timeline |
|---|---|---|---|
| Unify brand description across site + schema | Easy | High — immediate consistency | Days |
| Add/complete Organization schema with sameAs | Easy | Medium — entity clarity | Days |
| Create "[Brand] vs [Competitor]" pages | Moderate | High — controls narrative | Weeks |
| Create use-case-specific landing pages | Complex | High — specific positioning | Weeks |
| Get listed on review aggregators | Moderate | Medium — third-party signals | Weeks-Months |
| Add structured social proof (customer numbers, metrics) | Easy | Medium — extractable proof | Days |
| Add source citations to content (GEO paper strategy) | Easy | 30-40% visibility boost | Days |
| Add statistics to claims (GEO paper strategy) | Moderate | 30-40% for certain domains | Days-Weeks |

---

## Key Insight for the Auditor

GEO is the layer where the auditor transitions from **page doctor** to **brand strategist**.

SEO fixes a page. AEO fixes how a page is cited. GEO fixes how a **brand** is perceived across AI.

Most GEO recommendations will be content strategy ("create these pages," "get listed here,"
"own your comparison narrative") rather than on-page technical fixes. The auditor should clearly
separate:
- **Do now** (on-page: schema, description consistency, social proof) — immediate
- **Plan** (content strategy: comparison pages, use-case pages, review presence) — weeks/months

Don't oversell GEO quick fixes. Entity reputation changes slowly. But the on-page foundation
(consistent description, complete schema, extractable brand definition) is fast and directly
controllable.
