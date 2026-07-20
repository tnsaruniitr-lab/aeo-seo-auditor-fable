# AEO Reverse-Engineered Playbook & Scoring Model

**Derived empirically, not from opinion.** Every weight below traces to a measured
lift from the 2026-07-20/21 citation experiment: 1,000 AI probes (ChatGPT gpt-4o-mini +
gpt-5.2, Claude Sonnet, Gemini 2.5-flash × 5 categories × 5 phrasings × 10 repeats) +
Google SERP for all phrasings + a deep crawl of the top-cited pages/domains vs a control
set of *Google-ranked-but-not-AI-cited* pages. Raw data:
`<Downloads>/build-context/phrasing-experiment/`.

The control group is the load-bearing idea: we did not ask "what do cited pages have?"
(everyone has HTTPS). We asked **"what do cited pages have that equally-Google-ranked
UN-cited pages lack?"** — that difference is the causal signal. A factor where cited ≈
control is *table stakes* (score pass/fail, never reward); a factor where cited ≫ control
is a *differentiator* (the actual lever).

---

## 0. The two-layer model (why one score is wrong)

Measured: a brand's AI visibility is driven ~55–60% by **being in the documents the
engine retrieves** (third-party listicles/directories + your own citable page) and only
~20% by your own-domain technical setup — proven by 4 cited winners
(`verold.com`, `smyleee.com`, `sybill.ai`, `reddit.com`) that **block GPTBot in
robots.txt and get cited anyway**, carried by third-party pages.

Therefore the auditor emits **two scores, never blended:**

- **OWN-SITE CITE-READINESS** — can your own page be fetched, and is it worth citing?
  (governs the ~20% own-domain lever; this is what a technical audit can fix directly)
- **CATEGORY PRESENCE** — are you *in* the documents engines retrieve? (governs the
  ~55–60% lever; fixed via listings/PR, not code — feeds the Category Playbook)

This document specifies OWN-SITE CITE-READINESS scoring. CATEGORY PRESENCE is the
Source-Intelligence / Category-Playbook surface already built in answermonk.

---

## 1. Category dispatch (measured — advice inverts by category type)

Cited page-type differs sharply by category (from 7,941 stored citation URLs):

| Category type | What AI cites | Audit implication |
|---|---|---|
| **Product / SaaS** (CRM, PM tools) | Listicles/comparisons **43–54%**; own homepage rarely | Winning move is *get into the roundup*; own-site audit is secondary |
| **Local services** (dental, care, cards) | The business's **own homepage/deep pages 39–46%** | Own-site structure IS on trial — audit it hard |

The auditor classifies the target's category into `product` vs `local-service` (reuse
`site_context` industry) and **reweights**: local-service audits weight OWN-SITE
CITE-READINESS heavily; product audits down-weight it and push CATEGORY PRESENCE.

---

## 2. OWN-SITE CITE-READINESS — the scoring model

Three tiers. Tier weights reflect measured lift.

### TIER 0 — Fetchability gates (pass/fail; a fail zeroes the own-site score)

If an AI crawler can't get clean HTML, nothing else matters. Measured: 0% of cited
winners were JS-blocked; ~19% of all candidate URLs failed to return citable HTML.

| Gate | How measured | Rule |
|---|---|---|
| AI-bot not disallowed | robots.txt vs GPTBot/ClaudeBot/Google-Extended/PerplexityBot | FAIL if `Disallow: /` for any major AI UA |
| Server-rendered HTML | text words in raw HTML ≥ 300 | FAIL if JS-dependent (thin server HTML) |
| Reachable | HTTP 200 as GPTBot UA, follows redirects | FAIL on 4xx/5xx/timeout to bot UA |
| HTTPS | scheme | FAIL if not https |

These are **binary gates, not points.** 100% of cited winners pass all four; so do most
controls — they are the price of entry. Report only failures.

### TIER 1 — Differentiators (the measured levers; this is where points live)

Weights ∝ measured lift (cited-rate ÷ control-rate). Page-level factors dominate because
that is where the signal was strongest.

| Factor | Measured lift | Weight | Level | Rule |
|---|---|---|---|---|
| **FAQ schema (FAQPage / Question+Answer)** | **3.4×** (44% vs 13%) | **22** | page | present + valid JSON-LD |
| **Depth & structure** (≥ ~2,000 words, ≥10 H2s, ≥ ~12 lists/tables) | **1.9×** words, 1.6× headings | **18** | page | comprehensive, sectioned |
| **Question-form headings** (H2/H3 phrased as the user's question) | **2×** (2 vs 1 median) | **15** | page | ≥2 interrogative headings |
| **Article/BlogPosting schema** | **2.6×** (34% vs 13%) | **12** | page | on editorial/guide pages |
| **`llms.txt` published** | **2.4×** (65% vs 27%) | **12** | domain | valid `/llms.txt` |
| **Answer-shaped intro** (direct answer in first ~80 words) | proxy: cited pages front-load answers | **10** | page | needs render/DOM |
| **Author byline / E-E-A-T signal** | 1.8× (8% vs 4%) | **6** | page | visible author + date |
| **Delivery hardening** (CDN + compression + HSTS + Cache-Control) | CDN 1.4×, HSTS 1.2× | **5** | domain | behind CDN, compressed |

Sum of Tier-1 weights = 100. Score = Σ(weight × present) over factors applicable to the
page type, renormalized.

### TIER 2 — Confirmed NON-factors (score NOTHING; flag if a tool claims otherwise)

Measured at **no lift** — winners and controls have them equally, or controls have more.
Rewarding these is false precision and wastes the customer's budget.

| Non-factor | Measured | Why it's a non-factor |
|---|---|---|
| Product/SoftwareApplication schema | 0.7× (12% vs 17% — controls *higher*) | ubiquitous; not a citation signal |
| Review/AggregateRating schema | 0.9× | table stakes in review categories |
| Organization schema | ~1.0× (58% vs 61%) | everyone has it |
| Pricing present, meta-description, canonical, mobile viewport, sitemap.xml | ~1.0× | table stakes (Tier-0-adjacent, not differentiators) |
| Raw TTFB / speed | 430 vs 473 ms — both fine | not a differentiator above the "good enough" bar |

**This table is the trust weapon:** an audit that says "Product schema won't move your AI
citation in this category — don't spend on it" is unfalsifiable-honesty no competitor offers.

---

## 3. Scoring formula (deterministic)

```
own_site_cite_readiness =
    0                                   if any TIER-0 gate fails
    Σ(tier1_weight_i · present_i) / Σ(tier1_weight_applicable)   otherwise   → 0..100
```

- Page-type gating: FAQ/Article/answer-intro/byline apply to content pages; `llms.txt` +
  delivery apply at domain level; renormalize the denominator to applicable factors.
- Category reweight (§1): local-service → own-site score carries full weight in the
  headline; product → own-site capped, CATEGORY PRESENCE leads.
- **Every point cites its evidence:** each factor's contribution renders with its measured
  lift and the experiment date, so the score is auditable ("FAQ schema, +22, measured 3.4×
  citation lift, n=73, 2026-07-21") — this is the proof-chain philosophy applied to scoring.

---

## 4. The playbook (ranked plays, output alongside the score)

Ordered by expected value = measured lift × (gap: does the target lack it):

1. **Get into the category's carrier documents** (CATEGORY PRESENCE) — the ~40%-of-runs
   listicles/directories from Source Intelligence. Highest leverage; not a code fix.
2. **Add a FAQ block with FAQPage schema** answering the real buyer questions — 3.4× lever,
   the biggest own-site move.
3. **Make it the deep, sectioned page** — 2,000+ words, question-form H2s, lists/tables —
   1.9× depth + 2× question-heading levers combined.
4. **Publish `llms.txt`** — 2.4× correlate, near-zero effort, almost nobody does it.
5. **Front-load the answer** — direct answer in the first 80 words (answer-shaped intro).
6. **Article schema + visible byline/date** on guides — 2.6× / 1.8×.
7. **Ensure server-rendered HTML + AI-bot access** — the gate; if failing, this is P0
   regardless of everything above.
8. **Do NOT invest in** Product/Review schema *for citation* (§Tier-2) — spend the budget on 2–5.

---

## 5. Honesty caveats (ship with the spec)

- n is modest (73 pages, 59 domains, 5 categories, one snapshot). Lifts are directional and
  strongest for FAQ-schema/depth/llms.txt; treat single-factor weights as ±, the tier
  ordering as robust.
- Correlation, not proven cause: `llms.txt` likely proxies "AEO-savvy team." Labelled as a
  correlate, not a guarantee.
- Per-category weights will differ (FAQ schema may be 5× in SaaS, 1× in dental). Recalibrate
  per category as the corpus grows — the harness re-runs for ~$25/week.
- Re-derive quarterly; engine behavior drifts with model updates.

_Calibration set + harness: `<Downloads>/build-context/phrasing-experiment/`
(results-v2-1000probes-full.jsonl, crawl.jsonl, tech_audit.jsonl, serp-25phrasings.jsonl)._
