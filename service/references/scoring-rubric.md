# Scoring Rubric

## Two Separate Scores (Page vs Entity)

The audit produces TWO independent scores, not one blended number:

### 1. Page Citation Readiness (PCR)
"Can this specific page be found, extracted, trusted, and selected by AI answer engines?"
Fixable by editing this page. Timeline: days to weeks.

| Section | Code | Weight in PCR | Rationale |
|---|---|---|---|
| Technical SEO | A | 16% | Foundation — table stakes |
| Performance | B | 10% | Threshold signal |
| On-Page SEO | C | 13% | Content structure |
| Schema | D | 16% | SEO + AEO extraction |
| AEO: Discovery | E | 13% | Binary gate |
| AEO: Extraction | F | 13% | Core AEO value |
| AEO: Trust | G | 8% | Credibility signals |
| AEO: Selection | H | 8% | Competitive layer |
| Entity Consistency | J | 3% | Supporting signal |
| **TOTAL PCR** | | **100%** | |

### 2. Brand AI Presence (BAP)
"Does this brand exist in AI's understanding of the category?"
Requires content strategy + entity building. Timeline: months.

| Section | Code | Weight in BAP |
|---|---|---|
| GEO: Presence | I (I1, I2, I8) | 40% |
| GEO: Accuracy | I (I3, I4, I7) | 35% |
| GEO: Favorability | I (I5, I6) | 25% |
| **TOTAL BAP** | | **100%** |

### 3. Overall Score (Combined — for summary only)
```
overall = PCR * 0.80 + BAP * 0.20
```
PCR dominates because it's more actionable and measurable. BAP is weighted lower because
it's directional and harder to verify.

---

## Gating Logic (Overrides Scoring)

**Before calculating any scores, check gates:**

| Gate | Condition | Effect |
|---|---|---|
| GATE 1: Crawlability | robots.txt blocks Googlebot OR noindex directive | Report: "Page blocked from indexing. Scores are conditional." |
| GATE 2: Content Access | WebFetch < 200 words body AND Chrome unavailable | Report: "JS-rendered — AI crawlers can't access content." |
| GATE 3: Page Existence | 4xx/5xx status OR parking/construction page | Report: "Page not functional. Cannot audit." |

If any gate FAILS:
- Still calculate scores (they represent potential if gate is fixed)
- Lead report with gate failure prominently
- Add asterisk to all scores: "Scores assume blocking issue is resolved"
- Priority #1 fix is always the gate issue

---

## Per-Check Scoring

Each check has a weight (1, 2, or 3) and produces one of four statuses:

| Status | Symbol | Score contribution |
|---|---|---|
| Pass | `pass` | Full weight points |
| Warning | `warn` | Half weight points (rounded up) |
| Fail | `fail` | 0 points |
| N/A | `na` | Excluded from both numerator and denominator |

**Section Score Formula:**
```
section_score = SUM(earned_points) / SUM(applicable_max_points) * 100
```

Where:
- `earned_points` = weight for pass, ceil(weight/2) for warn, 0 for fail
- `applicable_max_points` = sum of weights for all non-N/A checks in that section

---

## Overall Score

```
page_citation_readiness = SUM(section_score * PCR_weight) for sections A-H, J
brand_ai_presence = weighted GEO dimensions (Presence 40%, Accuracy 35%, Favorability 25%)
overall_score = page_citation_readiness * 0.80 + brand_ai_presence * 0.20
```

---

## Grade Thresholds

| Grade | Score Range | Label |
|---|---|---|
| A+ | 95-100 | Exceptional — AI-ready, best-in-class |
| A | 90-94 | Excellent — strong SEO + AEO positioning |
| B+ | 85-89 | Very Good — minor gaps to address |
| B | 80-84 | Good — solid foundation, clear improvements available |
| C+ | 75-79 | Above Average — meaningful gaps in key areas |
| C | 70-74 | Average — several areas need attention |
| D+ | 65-69 | Below Average — significant issues |
| D | 60-64 | Poor — major rework needed |
| F | 0-59 | Failing — fundamental barriers to visibility |

---

## Composite Sub-Scores (Reported)

### Page Citation Readiness (Primary — page-level)
```
PCR = A(16%) + B(10%) + C(13%) + D(16%) + E(13%) + F(13%) + G(8%) + H(8%) + J(3%)
```
This is the main score. "Can this page be cited by AI answer engines?"

### Brand AI Presence (Secondary — entity-level)
```
BAP = I_presence(40%) + I_accuracy(35%) + I_favorability(25%)
```
Separate score. "Does this brand exist in AI's category knowledge?"
Reported with caveat: "Directional assessment based on web search signals."

### SEO Score (Traditional SEO subset)
```
seo_score = (A_score * 0.30) + (B_score * 0.20) + (C_score * 0.25) + (D_score * 0.25)
```

### AEO Score (Answer Engine subset)
```
aeo_score = (E_score * 0.30) + (F_score * 0.30) + (G_score * 0.20) + (H_score * 0.20)
```

### Citation Readiness (If found, will it be cited?)
```
citation_readiness = (F_score * 0.35) + (G_score * 0.25) + (H_score * 0.25) + (D_score * 0.15)
```

### Overall (Combined — for summary headline only)
```
overall = PCR * 0.80 + BAP * 0.20
```

---

## Effort/Impact Classification

Every failed or warning check gets tagged with effort and impact:

### Impact Levels

| Impact | Definition | How determined |
|---|---|---|
| Critical | Fix is blocking indexing, rendering, or AI access | Check severity = critical |
| High | Fix likely improves visibility measurably | Check weight = 3, OR competitor gap > 60% |
| Medium | Fix improves quality but uncertain direct impact | Check weight = 2 |
| Low | Cosmetic or minor improvement | Check weight = 1 |

### Effort Levels

| Effort | Definition | Examples |
|---|---|---|
| Trivial | < 5 minutes, no content changes | Add meta tag, fix robots.txt line |
| Easy | 15-30 minutes, minor edits | Write meta description, add alt text, add schema block |
| Moderate | 1-2 hours, content restructuring | Rewrite intro, create FAQ section, add comparison table |
| Complex | 4+ hours, significant content work | Full page restructure, create new content sections, entity overhaul |

### Priority Matrix

| | Trivial Effort | Easy Effort | Moderate Effort | Complex Effort |
|---|---|---|---|---|
| **Critical Impact** | DO NOW | DO NOW | DO NOW | PLAN NOW |
| **High Impact** | DO NOW | DO NOW | PLAN | PLAN |
| **Medium Impact** | DO NOW | PLAN | LATER | SKIP |
| **Low Impact** | OPTIONAL | OPTIONAL | SKIP | SKIP |

**Output labels:**
- **DO NOW** — Include in top fixes with generated fix
- **PLAN** — Include in recommendations with guidance
- **LATER** — Mention in detailed findings
- **SKIP** — Include only in technical reference
- **OPTIONAL** — Note as nice-to-have

---

## Score Presentation Format

```
## Overall Score: 67% — C (Average)

| Section | Score | Grade | Weight |
|---|---|---|---|
| A. Technical SEO | 85% | B+ | 15% |
| B. Performance | 70% | C | 10% |
| C. On-Page SEO | 75% | C+ | 12% |
| D. Schema | 45% | F | 15% |
| E. AEO Discovery | 80% | B | 12% |
| F. AEO Extraction | 35% | F | 12% |
| G. AEO Trust | 50% | F | 8% |
| H. AEO Selection | 40% | F | 8% |
| I. GEO | 55% | F | 5% |
| J. Entity | 75% | C+ | 3% |

Composite Scores:
- SEO Score: 69% (C)
- AEO Score: 52% (F)
- GEO Score: 55% (F)
- Citation Readiness: 41% (F)
```

The report should highlight: "Your SEO foundation is average but your AEO readiness is poor — this is why the page isn't being cited despite ranking on page 1."

---

## Competitor-Relative Scoring (Section H)

For Selection-stage checks, scoring is relative:

| Your position vs competitors | Score |
|---|---|
| Best among all 5 competitors | Pass (full weight) |
| Above median (top 2-3) | Pass (full weight) |
| At median | Warn (half weight) |
| Below median | Fail (0) |
| Worst among all 5 | Fail (0) + competitor benchmark note |

This ensures Selection scores reflect competitive reality, not absolute thresholds.
