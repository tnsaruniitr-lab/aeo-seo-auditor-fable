# Reverse-Engineered AEO Playbook + Scoring Parameters (measured)

_Derived from the 1,000-probe experiment (5 categories × 5 phrasings × 4 engines × 10 repeats),
Google SERP arm (25 phrasings), page-structure crawl (73 pages) and technical audit (64 domains),
2026-07-20. Every weight below is anchored to a measured lift, not opinion. Raw data:
`build-context/phrasing-experiment/`. This doc maps each factor onto the auditor's existing
108-check vocabulary so the checks can be re-weighted rather than rebuilt._

---

## 0. The load-bearing finding that reframes the whole score

AI citation is **two layers**, and a website audit can only see one of them:

- **OFF-SITE (~55% of the game): being in the documents engines retrieve** — the category's
  listicles/directories. Measured: 2–3 named third-party domains sit in ~40% of all AI evidence per
  category; within-brand carrier-doc lift is **2.2–5×**; and 4 cited winners *block GPTBot on their own
  site yet still get cited* via carriers. **A website auditor cannot score this** — it belongs to the
  off-site / gap-engine / source-intelligence layer.
- **ON-SITE (~20%): is your own page cite-able** — structure + fetchability. This is what the auditor scores.
- Remainder: Google-rank carryover (engine-dependent, ~5–25%), model-memory fame (giants only), entity coherence.

**Product consequence:** the auditor score must be presented as **"on-site cite-readiness," NOT "AEO
visibility."** A perfect auditor score ≠ AI visibility, because the bigger lever is off-site. Score them separately.

---

## 1. Factor → check-ID → measured lift → weight tier (THE MAP)

Weight tier: **DIFFERENTIATOR** (measured lift ≥1.6×, separates cited from not-cited) ·
**TABLE-STAKES** (≥85% of *both* cited and control have it — score pass/fail, don't reward) ·
**GAP** (matters, no check exists).

| Factor | Measured lift | Tier | Auditor check(s) |
|---|---|---|---|
| **FAQ schema (FAQPage) + visible FAQ** | **3.4×** | DIFFERENTIATOR (top) | `D9_faqpage_schema_vs_visible`, `F4_faq_semantic_markup`, `F3_faq_section` |
| **Question-form headings** | **2.0×** | DIFFERENTIATOR | `F6_headings_as_questions` |
| **Article/BlogPosting schema** | **2.6×** | DIFFERENTIATOR | `D3_page_appropriate_type`, `D11_datepublished_datemodified` |
| **Content depth (word count ~2× control)** | **1.9×** | DIFFERENTIATOR | `C6_word_count_thin_content`, `H1_content_depth_vs_competitors` |
| **Heading richness / sectioning (H2 count)** | **1.6×** | DIFFERENTIATOR | `C1_heading_hierarchy` |
| **Lists & tables** | **1.6×** | DIFFERENTIATOR | `F12_tables_lists` |
| **Self-contained answer units / TL;DR / answer-first** | (structural, cited pages 88% visible-FAQ) | DIFFERENTIATOR | `F1_first_paragraph_answers_query`, `F2_quick_answer_block`, `F10_summary_tldr`, `F11_self_contained_answer_units`, `F9_definition_first_writing` |
| **`llms.txt` present** | **2.4×** (65% vs 27%) | **GAP — NO CHECK** | — (add `E14_llms_txt` / `A13_llms_txt`) |
| **Server-rendered HTML (not JS-dependent)** | winners 3% JS-blocked vs 9% control; 17/90 uncrawlable | DIFFERENTIATOR (hard gate) | `A12_js_rendering_dependency`, `E5_content_in_raw_html`, `E6_content_not_behind_accordions` |
| **CDN + compression + HSTS** | CDN 65% vs 45%; compress 97% vs 91% | soft differentiator | `B7_compression`, `B8_cache_control`, (CDN/HSTS = GAP, minor) |
| **Author byline / credentials** | 1.8× (low base) | minor differentiator | `G1_author_byline`, `G2_author_schema_credentials` |
| HTTPS | 100% / 100% | TABLE-STAKES | `A1_https_enforcement`, `G8_https_valid` |
| Mobile viewport | 100% / 91% | TABLE-STAKES | `A9_viewport_meta` |
| Sitemap present + referenced | 86% / 86% | TABLE-STAKES | `A11_sitemap_referenced`, `E8_page_in_sitemap` |
| Canonical tag | 89% / 86% | TABLE-STAKES | `A4_canonical_tag` |
| **AI-bot access (GPTBot/ClaudeBot/PerplexityBot allowed)** | ~90% / ~86% | TABLE-STAKES (but hard-fail if blocked) | `E1`,`E2`,`E3`,`E10_claudebot_chatgpt_applebot`,`E13_ccbot_llm_training_access` |
| TTFB / speed | 430ms / 473ms (both fine) | TABLE-STAKES | `B1_ttfb`, `B10_core_web_vitals` |
| Product/Review/Org schema, meta-desc | ~1.0× (no lift) | TABLE-STAKES / NULL | `D6/D8`, `A3_meta_description` |
| **Being in the category's listicles/directories** | **2.2–5× carrier lift; ~40% of evidence** | **OFF-SITE — not an auditor check** | gap-engine / source-intelligence / competitor-playbook |
| **Entity coherence (NAP/sameAs across profiles)** | indirect (fragmentation splits evidence) | supporting | `I7_consistent_entity_data`, `I8_sameas_authoritative_profiles`, `J1`–`J4` |

---

## 2. Recommended scoring weights (on-site cite-readiness sub-score)

Re-weight the auditor from "everything counts equally" to measured lift. Proposed weights for the
**on-site** score (the ~20% layer); table-stakes are pass/fail gates worth near-zero when passed but
**hard-fail the whole score when broken** (a JS-only page or a GPTBot block zeroes cite-readiness):

| Group | Weight | Rationale |
|---|---|---|
| **F-family (AEO extraction: FAQ, question-headings, answer-first, self-contained units, TL;DR)** | **40%** | Highest measured lift lives here (FAQ 3.4×, Q-headings 2×) |
| **Structured data that matters (FAQPage D9, Article D3/D11, depth-linked)** | **20%** | 2.6–3.4× where it's the *right* schema; null for Product/Review |
| **Content depth + structure (word count, headings, lists/tables)** | **15%** | 1.6–1.9× |
| **`llms.txt` + fetchability hardening (server-render, CDN, compression)** | **15%** | llms.txt 2.4×; JS-dependency is a hard risk |
| **E-E-A-T (byline, credentials, dates, entity consistency)** | **10%** | modest but real; matters most in YMYL (dental/care/finance) |
| Table-stakes (HTTPS, viewport, sitemap, canonical, bot-access, speed) | **gate, ~0%** | 85–100% of everyone passes; only *failures* score |

**And the two-layer disclosure (non-negotiable for honesty):** the report shows
`on-site cite-readiness` AND, separately, `off-site presence` (are you in the carrier documents),
because they are different games (measured: SaaS decouples AI from Google; local converges).

---

## 3. Category-conditional logic (measured, must be applied)

| Category type | AI cites… | Google↔AI correlation | Playbook emphasis |
|---|---|---|---|
| **Product/SaaS** (CRM, PM) | listicles/comparisons (43–54% of cited pages) | **decoupled** (9/12 AI-strong not on Google p1) | OFF-SITE: get into `thedigitalprojectmanager`-class roundups; own a comparison page |
| **Local services** (dental, care) | the business's **own homepage/deep pages** (46% homepages) | **converged** (0/7 AI-strong off Google p1) | ON-SITE: fix your own homepage structure + entity/NAP + directories (Practo/Doctify/Doctolib-class) |

The auditor should branch its top recommendations on category type — SaaS clients get "enter these
listicles," local clients get "fix your homepage + these directories." Same engine, opposite lead play.

---

## 4. The proven thesis (sales foundation)

- **Small beats big in AI, measured across every category:** Pemo 83% vs Emirates NBD 38% (+45pts);
  ClickUp 92% vs Jira 66%; Pipedrive 86% vs Salesforce 20%; a local Pflege startup over AOK. AEO
  rewards positioning + citable evidence, not brand mass. This is the promise, now empirically true.
- **AI ≠ Google:** 40% of AI-strong brands are not on Google page 1 by their own domain — so an
  AEO tool measures something classic SEO tools structurally miss.
- **Leader stability:** top-3 stable across independent sample-halves in 4/5 categories → the playbook
  is consolidated; expand *breadth* (more local categories), not repeats.

---

## 5. Immediate build actions (mapped)

1. **Re-weight the 108 checks** to the tiers above — biggest change: promote the F-family + D9 to
   dominate the on-site score; demote passed table-stakes to gates. (auditor `scoring.py` /
   sieve rule confidence.)
2. **Add the one missing check: `llms.txt`** (E14 or A13) — strongest domain-level differentiator, in
   zero competitors' audits. (auditor deterministic scripts + a sieve rule.)
3. **Split the headline score** into on-site cite-readiness vs off-site presence — stop implying a
   clean technical audit = AI visibility.
4. **Branch top recommendations on category type** (SaaS→listicles, local→homepage+directories).
5. Feed this file to the sieve rule library as the empirical calibration set for the F/D/H families.
