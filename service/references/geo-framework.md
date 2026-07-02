# GEO Framework — 3-Dimension Generative Engine Optimization

## AEO vs GEO

| | AEO | GEO |
|---|---|---|
| Focus | Single page citation | Brand representation across AI |
| Question | "Will this page be cited as an answer?" | "How does this brand appear when AI talks about the category?" |
| Unit | Page | Brand/entity |
| Fixes | On-page structure + content | Content strategy + entity management |
| Timeline | Weeks (page edits) | Months (reputation + content ecosystem) |

GEO is about controlling how AI systems represent your brand when generating responses about your category, competitors, or market — whether or not a specific page is cited.

---

## Dimension 1: Presence (Checks I1, I2, I8)

**Question**: "Does AI mention this brand when asked about the category?"

**What this measures**:
- When someone asks "best [category] tools" or "top [category] companies," does this brand appear in AI-generated lists?
- Does the brand have a structured entity presence (knowledge panels, cards)?
- Are sameAs links connecting the brand to authoritative profiles?

**How to assess (using WebSearch)**:
1. Search "best [category]" — check if brand appears in results/snippets
2. Search "[brand name]" — check for knowledge panel or entity card
3. Validate sameAs URLs in Organization schema resolve to real, active profiles

**Why brands fail here**:
- Not listed on enough comparison/review sites (G2, Capterra, Product Hunt, etc.)
- No Wikipedia/Wikidata entry
- Weak or missing Organization schema
- sameAs links point to dead or unrelated profiles
- Brand is too new or niche to have web presence density

**Fix pattern**:
- On-page: Complete Organization schema with sameAs
- Content strategy: Get listed on review/comparison platforms
- Entity: Ensure consistent brand definition across all sources
- Long-term: Build structured entity presence (Wikipedia, Wikidata, industry directories)

---

## Dimension 2: Accuracy (Checks I3, I4, I7)

**Question**: "Does AI describe this brand correctly?"

**What this measures**:
- When AI generates a description of the brand, does it match what the brand actually does?
- Is pricing, feature, and positioning information current?
- Is entity data consistent across different sources AI might reference?

**How to assess**:
1. Search "[brand name]" — compare AI-generated description to brand's actual positioning
2. Search "[brand name] pricing" or "[brand name] features" — check for outdated info
3. Cross-reference brand description across multiple search results for consistency

**Why brands fail here**:
- Brand pivoted or changed positioning but old descriptions persist on the web
- Different pages on the brand's own site describe it differently
- Third-party sites have outdated information
- No clear, concise "what we do" statement that AI can extract
- Schema Organization description doesn't match marketing copy

**Fix pattern**:
- On-page: Unify brand description — one clear sentence used everywhere (homepage, about, schema, social profiles)
- Content strategy: Update third-party profiles and listings with current positioning
- Schema: Ensure Organization.description matches the canonical brand description
- Freshness: Add dateModified to all key pages so AI knows info is current

---

## Dimension 3: Favorability (Checks I5, I6)

**Question**: "Does AI position this brand positively?"

**What this measures**:
- When AI compares this brand to competitors, is the brand positioned favorably?
- Is the sentiment of AI-generated brand descriptions positive or neutral?
- Is the brand recommended for specific use cases?

**How to assess**:
1. Search "best [category]" — check brand's ranking position in results
2. Search "[brand] vs [competitor]" — assess framing and sentiment
3. Analyze sentiment of AI-generated brand descriptions

**Why brands fail here**:
- Negative reviews dominate search results
- Competitor comparison content (written by competitors) positions the brand unfavorably
- Brand doesn't own its comparison narrative (no "[brand] vs [competitor]" content)
- No use-case-specific content ("best for remote teams", "best for startups")
- Lack of social proof signals (customer counts, case studies, awards)

**Fix pattern**:
- Content strategy: Create owned comparison pages ("[Brand] vs [Competitor]")
- Content strategy: Build use-case-specific landing pages
- On-page: Add structured social proof (customer numbers, awards, case study results)
- Review management: Encourage positive reviews on platforms AI references
- Schema: Add Review, AggregateRating where applicable

---

## GEO Score Calculation

```
Presence Score = SUM(passed_presence_weights) / SUM(applicable_presence_weights) * 100
Accuracy Score = SUM(passed_accuracy_weights) / SUM(applicable_accuracy_weights) * 100
Favorability Score = SUM(passed_favorability_weights) / SUM(applicable_favorability_weights) * 100
```

**GEO Score = (Presence * 0.40) + (Accuracy * 0.35) + (Favorability * 0.25)**

Presence is weighted highest because if AI doesn't mention you, accuracy and favorability are irrelevant.

---

## Interpreting GEO Scores

| Score | Meaning | Action |
|---|---|---|
| 80-100 | Strong AI brand presence — brand is known, accurate, and positively positioned | Maintain, monitor for accuracy drift |
| 60-79 | Moderate presence — brand appears but with gaps in accuracy or positioning | Fix entity consistency, update stale info |
| 40-59 | Weak presence — brand occasionally appears, may be inaccurately described | Build entity presence, create comparison content |
| 0-39 | Minimal/no AI presence — brand not part of AI's knowledge | Foundational work: schema, listings, content ecosystem |

---

## GEO vs AEO Fix Priority

For most brands, the priority order is:
1. **AEO Discovery fixes first** — ensure AI can access your content at all
2. **AEO Extraction fixes** — ensure content is citable once found
3. **GEO Accuracy fixes** — ensure AI describes you correctly
4. **AEO Trust + Selection** — build credibility and competitive edge
5. **GEO Presence + Favorability** — expand brand visibility in AI (longest-term)

GEO Presence and Favorability are slower to improve because they depend on external signals and content ecosystem, not just on-page changes. The audit should set expectations accordingly.
