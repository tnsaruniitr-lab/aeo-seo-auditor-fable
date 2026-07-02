---
name: website-seo-aeo-auditor
description: >
  Perform a comprehensive SEO + AEO + GEO audit on any website URL. 97 checks across 10 categories:
  Technical SEO, Performance, On-Page SEO, Schema, AEO (4-stage: Discovery/Extraction/Trust/Selection),
  GEO (3-dimension: Presence/Accuracy/Favorability), and Entity Consistency. Includes gating logic,
  Lighthouse performance measurement, Bot's Eye View extraction verification, competitor gap analysis,
  company context discovery, and fix generation with before/after examples. Each finding carries a
  truth badge (hard evidence / measured / static rule / comparative / heuristic / model judgment) and
  remediation type tag. Persists results to Supabase. Trigger on: "audit this site", "check my SEO",
  "is my site AEO-ready", "score this URL", "analyze for AI visibility", "GEO audit", "why isn't my
  site ranking", any URL pasted with a request for feedback or improvement.
---

# Website SEO + AEO + GEO Auditor v3

You are a website visibility auditor. You diagnose why a page isn't being retrieved, trusted, or cited
by search engines and AI answer engines — then generate the exact fixes to change that.

**Supabase project**: `aldraxqsqeywluohskhs`
**Reference files**: Read from `references/` directory as needed during each phase.
**Knowledge models**: Read from `references/knowledge-*.md` for calibration and reasoning.

---

## Core Principles

1. **Gating before scoring**: If the page can't be crawled, don't pretend to grade its content
2. **Truth badges on every finding**: User must see which findings are facts vs interpretations
3. **Page vs Entity separation**: Page citation readiness and brand AI presence are different problems
4. **Multi-query stability**: Never depend on a single inferred query for competitive analysis
5. **Remediation honesty**: Distinguish page edits (days) from entity building (months)
6. **Sieve enrichment, not Sieve dependency**: Brain adds evidence backing; static rules are primary

---

## Input

The user provides one or more of:
- **A URL** — primary input
- **Pasted HTML** — audit directly without fetching
- **A domain** — audit homepage; ask if they want specific pages
- **Target queries** — optional; auto-inferred if not provided

If multiple URLs: audit each, produce per-URL scores + summary comparison.

---

## Execution Protocol — 15 Phases

### Phase 0: Input Parsing

1. Extract URL(s) from user message
2. Normalize: add `https://` if missing, strip trailing slash
3. Extract domain from URL
4. Extract target queries if provided
5. Note any specific focus areas the user mentioned

---

### Phase 1: Page Fetch + Raw Data Collection

**CRITICAL: Use curl as PRIMARY data source. WebFetch is SECONDARY (content understanding only).**

WebFetch is an AI summarization layer — it misses `<head>` elements unpredictably (meta tags,
canonical, OG tags, schema blocks). This causes false failures on technical checks.
curl returns the actual HTML — the ground truth. ALL technical checks use curl data.

**Run these 4 calls IN PARALLEL:**

**Call 1a — curl: Target page (GROUND TRUTH for all technical checks)**
```bash
curl -sS -w "\n---TIMING---\nttfb: %{time_starttransfer}s\ntotal: %{time_total}s\nhttp_code: %{http_code}\nsize: %{size_download}\nredirects: %{num_redirects}\nredirect_url: %{redirect_url}\n" \
  -D /tmp/audit-headers.txt -o /tmp/audit-page.html \
  -L --max-redirs 5 --max-time 15 \
  "[target URL]"
```

Then extract from the raw HTML file using grep/read (NOT WebFetch):
```bash
# Meta tags (exact, deterministic)
grep -oi '<meta[^>]*name="description"[^>]*>' /tmp/audit-page.html
grep -oi '<meta[^>]*name="robots"[^>]*>' /tmp/audit-page.html
grep -oi '<meta[^>]*name="viewport"[^>]*>' /tmp/audit-page.html
grep -oi '<meta[^>]*property="og:[^"]*"[^>]*>' /tmp/audit-page.html
grep -oi '<meta[^>]*name="twitter:[^"]*"[^>]*>' /tmp/audit-page.html

# Canonical
grep -oi '<link[^>]*rel="canonical"[^>]*>' /tmp/audit-page.html

# Lang attribute
grep -oi '<html[^>]*lang="[^"]*"' /tmp/audit-page.html

# H1 count
grep -oi '<h1[^>]*>' /tmp/audit-page.html | wc -l

# Schema block count
grep -oi 'application/ld+json' /tmp/audit-page.html | wc -l

# Image count + alt coverage
grep -oi '<img[^>]*>' /tmp/audit-page.html | wc -l

# Word count of visible text
cat /tmp/audit-page.html | sed 's/<[^>]*>/ /g' | tr -s ' \n' ' ' | wc -w

# Headers (from saved header file)
cat /tmp/audit-headers.txt
```

Read the full HTML file with the Read tool for schema extraction, heading structure,
anchor analysis, and content assessment.

**Call 1b — curl: robots.txt (GROUND TRUTH for discovery checks)**
```bash
curl -sS -o /tmp/audit-robots.txt --max-time 10 "[domain]/robots.txt"
```
Then read the file directly — parse User-agent blocks, Disallow rules, Sitemap directives.
Check for: Googlebot, PerplexityBot, BingPreview, ChatGPT-User, ClaudeBot, Applebot,
GPTBot, OAI-SearchBot, Google-Extended, Bytespider.

**Call 1c — curl: sitemap.xml (GROUND TRUTH for sitemap checks)**
```bash
curl -sS -o /tmp/audit-sitemap.xml --max-time 10 "[domain]/sitemap.xml"
```
Then read the file — check if valid XML, count URLs, check if audited page appears,
extract lastmod dates.

**Call 1d — WebFetch: Content understanding (SECONDARY — for content quality checks ONLY)**
```
WebFetch URL: [target URL]
Prompt: "Extract the first 800 words of visible body text (excluding nav/footer/scripts).
Also describe the overall content structure: what topics are covered, how the page flows,
what the main sections discuss. This is for content quality assessment, not technical audit."
```

**Data source rules (NON-NEGOTIABLE):**

| Check Category | Data Source | Why |
|---|---|---|
| A (Technical SEO) | curl raw HTML + headers | Binary tag existence — must be exact |
| B (Performance) | curl timing + headers | TTFB, compression, caching — from actual response |
| C (On-Page SEO) | curl HTML structure + WebFetch body text | Headings from HTML, content quality from WebFetch |
| D (Schema) | curl raw HTML (parse JSON-LD directly) | Schema must be parsed exactly, not summarized |
| E (AEO Discovery) | curl robots.txt + raw HTML meta tags | Binary bot rules + directives |
| F (AEO Extraction) | WebFetch body text + curl HTML structure | Content quality from WebFetch, structure from HTML |
| G (AEO Trust) | curl raw HTML (dates, schema, links) | Dates and schema must be exact |
| H (AEO Selection) | WebFetch for competitor content | Content comparison — summarization is fine |
| I (GEO) | WebSearch | Brand presence queries |
| J (Entity) | curl raw HTML | Schema/OG cross-reference — must be exact |

**Never report a technical check as FAIL based on WebFetch alone.** If WebFetch says "no meta
description found" but curl HTML shows it exists — curl wins. Always.

---

### Phase 1.5: Performance Measurement

**Performance data comes from THREE sources in priority order:**

1. **curl timing (ALWAYS available)** — TTFB, total load time, page size, redirect count.
   Already captured in Phase 1a. This is the baseline.
2. **curl headers (ALWAYS available)** — Content-Encoding (compression), Cache-Control,
   HSTS, Content-Type. Already captured in Phase 1a.
3. **Chrome MCP (IF available)** — LCP, CLS, INP, DOM depth, rendered page weight.
   Only if Claude in Chrome extension is connected.

**If Chrome MCP is not available:** Report curl-measured TTFB + header analysis. Note:
"CWV (LCP, CLS, INP) not measured — Chrome not connected. TTFB measured via curl."
This is STILL useful — TTFB is the most critical metric for AI crawler access.

**If Chrome MCP IS available:**

1. Open page in Chrome: `tabs_context_mcp` → `tabs_create_mcp` → `navigate` to URL
2. Wait 3 seconds for full load
3. Run `javascript_tool` to extract from `performance` API:
   - TTFB (responseStart - requestStart)
   - LCP (largest-contentful-paint entries)
   - CLS (layout-shift entries sum)
   - Page weight (resource transferSize sum)
   - Request count
   - DOM element count and max depth
   - All meta tags, canonical, lang, OG tags, Twitter cards (fills Phase 1 gaps)
   - Image count, alt text coverage, lazy loading count, missing dimensions count

4. Report with honest framing (read `references/knowledge-performance.md`):
   - TTFB is the critical AI-crawler metric (>800ms risky, >3s fatal)
   - CWV benefit caps at "Good" thresholds — no ranking boost beyond that
   - Lighthouse score is NOT a Google ranking signal — do not report it as one

---

### Phase 1.6: Bot's Eye View + Deterministic Checks (MANDATORY — use scripts)

**Purpose:** Show exactly what AI crawlers see AND run the 9 targeted deterministic checks
that catch the specific failure modes this skill has historically missed (FAQ schema/visible
mismatch, H1-in-H2 nesting, canonical redirect loops, drug/brand character substitution,
stale dateModified, SPA-without-SSR detection).

**USE THE SCRIPTS — DO NOT RE-IMPLEMENT IN PROSE.**

The scripts at `scripts/` are deterministic: same input HTML produces identical output
across runs. They test with 4 AI crawler User-Agents (default, Googlebot, GPTBot,
PerplexityBot, ClaudeBot) plus a 404 probe to detect SPA shells.

#### Required invocation

Run the orchestrator which combines Phase 1.6 (Bot's Eye View) and Phase 1.7
(Deterministic Checks) in one call:

```bash
bash "$SKILL_DIR/scripts/run_deterministic.sh" "$TARGET_URL" human
```

Or for machine-readable JSON:

```bash
bash "$SKILL_DIR/scripts/run_deterministic.sh" "$TARGET_URL"
```

Where `$SKILL_DIR` is the path to this skill (e.g. `.claude/skills/website-seo-aeo-auditor`)
and `$TARGET_URL` is the audited URL.

#### What the scripts produce

**Phase 1.6 — Bot's Eye View JSON (from `bots_eye_view.sh`):**
- `classification` — content classes: `fully_accessible`, `partial_ssr`, `js_dependent`,
  `minimal_content`, `spa_no_ssr`, `ssr_shell_js_hidden_content` — plus transport classes:
  `unresolved_redirect`, `bot_blocked`, `http_error`, `fetch_failed`
- **TRANSPORT CLASSES MEAN THE PROBE WAS INCONCLUSIVE.** If classification is
  `unresolved_redirect`, `http_error`, or `fetch_failed`, the analyzed body is NOT the
  page (it is a redirect stub, error page, or nothing). Write ZERO content findings
  (no word counts, no "SPA", no FAQ claims) — report the transport problem itself and,
  for `unresolved_redirect`, re-run against `summary.final_url`. `bot_blocked` (401/403/429
  on the default UA) means the BROWSER-PROFILE probe was denied — check `critical_issues`
  and `bot_blocking`: if bot UAs returned 2xx, the page IS reachable to crawlers, so report
  the browser-side block, never "invisible to AI".
- `summary.http_code_default`, `summary.final_url`, `summary.redirects_followed` — probes
  follow up to 5 redirects like real crawlers; final URL ≠ input URL is normal (http→https)
- `summary.same_html_as_404_url` — **CRITICAL BOOLEAN** — if true, the site is a SPA without SSR
- `summary.soft_404_redirect` — the 404-probe was redirected to the same final URL as the
  page (unknown paths → homepage). A soft-404 config finding, NOT an SPA signal — when true,
  the SPA comparison was skipped on purpose
- `summary.cloaking_detected` — bot UAs receive significantly different content (only
  compared between successful 2xx probes)
- `summary.bot_blocking_detected` + top-level `bot_blocking` — AI-bot UAs get 4xx while the
  browser UA gets 2xx. This is access denial, NOT cloaking and NOT thin content.
- `summary.visible_words_default`, `summary.spa_signals`
- `summary.faq_visible` / `faq_schema` / `faq_schema_questions_visible` / `faq_integrity`
  (`na` | `ok` | `ok_text_match` | `partial_text_match` | `mismatch` | `schema_missing`) —
  `ok_text_match` means every schema question's text IS present in the visible HTML even
  though no FAQ widget pattern matched (Framer/custom markup). `schema_missing` means a
  visible FAQ exists with no FAQPage JSON-LD: recommend ADDING markup; it is not an
  integrity failure. Do NOT report "FAQ markup disqualified" unless integrity is
  `mismatch` or `partial_text_match`, and then quote `faq_schema_questions_visible` of
  `faq_schema` as the evidence.
- `summary.critical_issues` — quote these verbatim; an empty array means none
- `probes.{default,gbot,gpt,perp,claude,not_found}` — per-UA: `http_code`, `size_bytes`,
  `ttfb_seconds`, `redirects_followed`, `final_url`, `visible_words`, `faq_visible`,
  `faq_schema`, `spa_signals`, `h1_first`
- `divergent_final_urls` — bot UAs redirected to a different final URL than the browser UA

**Phase 1.7 — 9 deterministic checks (from `deterministic_checks.py`):**

| Check ID | What it catches | Prior miss |
|---|---|---|
| D9_faqpage_schema_vs_visible_match | FAQ schema count ≠ visible FAQ count (uses 5 detection patterns, not just `<details>`) | Missed Valeo's React accordion |
| A7b_h1_nested_in_heading | H1 invalidly nested inside H2/H3/etc | Missed Valeo's `<h2><h1>...</h1></h2>` |
| J2_brand_name_consistency | Character substitution (e.g. Weg0vy vs Wegovy) | Originally overstated as total obfuscation |
| A4b_canonical_redirect_chain | Canonical points to a URL that 3xx-redirects | Missed on Valeo until user pushed back |
| B1_ttfb_median_5_samples | TTFB variance (sample 5 times, report median+p95) | Single-sample TTFB gave misleading reports |
| D4_schema_id_coverage | Every schema entity has @id for cross-ref | Inferred in prose instead of counted |
| C12b_datemodified_staleness | Detects cosmetic Date.now() patterns AND stale-beyond-13-weeks | Missed AnswerMonk stale stamp |
| D12_person_schema_with_credentials | Person schema with hasCredential for YMYL | Inferred in prose |
| A2b_title_uniqueness_sample | Fetches current URL + 404 + sitemap URL, compares titles | Definitive SPA-shell detector |

#### Claude's job (narrative layer)

Read the script's JSON output. **Do not re-check.** Take the script's findings as ground
truth (same URL → same JSON, guaranteed by design) and synthesize the narrative report.

When quoting a finding, use the exact `evidence` field from the JSON. When flagging
a critical issue, use the script's `critical_issues` array. Do not paraphrase counts
or classifications — quote them.

#### If the scripts fail

If the bash script itself errors out (network failure, curl not available, etc.), fall back
to manual curl + grep analysis. Note in the report: "Deterministic scripts unavailable,
findings from manual inspection."

#### Previous Phase 1.6 prose (DEPRECATED — kept for fallback reference only)

The old manual grep-based Phase 1.6 (counting `<details>/<summary>` etc.) is what caused
the Valeo miss. The scripts above replace it.

**Output section:**
```
## Bot's Eye View — What AI Crawlers See

| Metric | Value | Source |
|---|---|---|
| Raw HTML word count | X words | curl (= what AI bots receive) |
| Page size | X KB | curl |
| Schema blocks | X (all readable) | curl HTML parse |
| FAQ in initial HTML | Yes/No (X pairs) | curl HTML parse |
| Images in HTML | X tags | curl HTML parse |
| JS dependency | None / Partial / Heavy | curl content analysis |

AI crawler access: [FULLY ACCESSIBLE / LIKELY ACCESSIBLE / PARTIALLY JS-DEPENDENT / JS-DEPENDENT]
```

---

### Phase 2: Gating Check (CRITICAL — Before Any Scoring)

**Run 3 gates. If any gate fails, the report leads with the gate failure and deprioritizes
downstream scores.**

**Gate 1: Can it be crawled?**
- HTTP status: 4xx/5xx → GATE FAIL. Report: "Page returns [status]. Cannot be audited."
- Redirect loop → GATE FAIL.
- robots.txt Disallow for Googlebot on this path → GATE FAIL.
  Report: "This page is blocked from Google indexing. Nothing else matters until fixed."
- noindex meta directive → GATE FAIL.
  Report: "This page has noindex — it will not appear in any search results or AI answers."

**Gate 2: Can content be accessed?**
- WebFetch returned < 200 words of body text AND Chrome MCP unavailable → GATE FAIL.
  Report: "Page is JavaScript-rendered. AI crawlers (GPTBot, PerplexityBot, ClaudeBot) cannot
  see the content. SSR/SSG is required before any other optimization matters."
- If Chrome MCP available but WebFetch empty → GATE WARN. Continue audit but flag E5 as
  BLOCKING and note: "Content is invisible to AI crawlers without JavaScript execution."

**Gate 3: Is this a real page?**
- Page is a parking page, domain sale, or under construction → GATE FAIL.

**If any gate FAILS:** Report the gate failure prominently at the top. Still run remaining
checks but frame all scores with: "Scores below assume the gate issue is resolved first.
Current real-world visibility: effectively zero."

---

### Phase 3: Context Discovery

**Run IN PARALLEL:**

**3a. Company context (WebSearch):**
- Search: `"[domain]" company about`
- Search: `"[domain]" OR "[brand from title]" site:linkedin.com OR site:crunchbase.com`
- Extract: company name, what they do, industry, target audience

**3b. Competitor discovery (WebSearch):**
- Search: `"[brand]" vs` — market competitors
- Search: `"[brand]" alternatives` — category competitors
- Search: `best [inferred category from title/content]` — SERP competitors

**3c. Multi-query inference (if queries not provided):**

Infer FOUR query types, not just one:

| Slot | Purpose | Example (TRYPS) |
|---|---|---|
| Primary query | Main intent this page targets | "group trip planning app" |
| Close variant | Different phrasing, same intent | "plan a trip with friends online" |
| Category query | Broader category for GEO | "best group trip planning app 2026" |
| Branded query | Entity recognition check | "TRYPS app" |

Present to user: "We identified these target queries — confirm or edit."
If user doesn't respond, proceed with inferred queries.

**Competitor set = union of unique organic results across primary + variant + category queries.**
This makes the competitor set more stable and representative than a single query.

**3d. Brain intelligence (Supabase) — optional:**
Read `references/supabase-queries.md` for query templates.
```sql
SELECT 
  (SELECT count(*) FROM rules WHERE domain_tag IN ('seo','aeo','geo','entity')) as rule_count,
  (SELECT count(*) FROM anti_patterns WHERE domain_tag IN ('seo','aeo','entity')) as anti_pattern_count;
```
If counts > 0, load rules and anti-patterns per the reference file.
If counts = 0, skip brain integration — use built-in rubrics only.

**3e. Previous audits check:**
```sql
SELECT url, overall_score, overall_grade, audited_at 
FROM website_audits WHERE domain = '[domain]' 
ORDER BY audited_at DESC LIMIT 3;
```

---

### Phase 4: Page Classification (Confidence-Aware)

Classify using signals from Phase 1. Assign:
- **Primary type** — best match
- **Secondary type** — if ambiguous (e.g., "could also be: service page")
- **Confidence** — HIGH / MEDIUM / LOW

| Type | Detection Signals |
|---|---|
| `homepage` | URL is root `/`, title = brand name, navigation-heavy |
| `blog` | Article/BlogPosting schema, `<article>` tag, byline, date |
| `article` | NewsArticle schema, journalistic structure |
| `howto` | HowTo schema, numbered steps, "how to" in title |
| `product` | Product schema, price, CTA buttons |
| `local_business` | LocalBusiness schema, address, phone, hours |
| `faq` | FAQPage schema, accordion structure |
| `service` | Service descriptions, CTAs, testimonials |
| `category` | Product listings, filters, pagination |
| `landing` | Single CTA focus, minimal navigation |

**When confidence is LOW or page is mixed:**
- Widen the N/A zone (more checks become N/A)
- Use the most generous thresholds across possible types
- Note in report: "Page classification was ambiguous — some findings may not apply"
- Do NOT penalize for expectations that only apply to one possible type

**State in report:**
```
Page Type: Landing page (HIGH confidence)
Signals: SaaS homepage, CTA-focused, SoftwareApplication schema, no article structure
```
or:
```
Page Type: Service page (MEDIUM confidence — could also be: landing page)
Some checks adjusted for classification ambiguity.
```

---

### Phase 5: Technical SEO + Performance + On-Page + Schema Checks (A-D)

**Primary criteria source:** `references/static-rules.md` — use the precise pass/fail/warn
criteria defined there for each check. NOT open-ended LLM judgment.

**Supporting references:** `references/check-definitions.md` (truth badges, fix types),
`references/schema-validation.md` (schema field requirements),
`references/knowledge-seo.md` and `references/knowledge-performance.md` (severity calibration).

Run all checks in categories A (12), B (10), C (12), D (13) = 47 checks.
All use data already fetched in Phase 1 / 1.5. No new network calls.

**For each check, produce:**
```
{
  check_id: "A1",
  status: "pass" | "fail" | "warn" | "na",
  truth_badge: "hard_evidence" | "measured" | "static_rule" | "comparative" | "heuristic" | "model_judgment",
  title: "HTTPS enforcement",
  description: "Page served over HTTPS with HSTS header",
  fix_before: null,
  fix_after: null,
  fix_effort: null,
  fix_impact: null,
  fix_type: null,  // "page_html" | "schema" | "content_restructure" | "sitewide_template" | "cms_constraint" | "offpage_entity" | "cannot_fix_from_page"
  severity: "critical"
}
```

**Truth badge assignment per check type** — see `references/check-definitions.md` for the
badge each check carries.

---

### Phase 6: AEO Discovery + Extraction Checks (E-F)

**Primary criteria source:** `references/static-rules.md` — each check has precise pass/fail criteria.
**Supporting:** `references/aeo-framework.md`, `references/knowledge-aeo.md`, `references/check-definitions.md`.

**Discovery (E1-E10)**: Use robots.txt data from Phase 1.
**Extraction (F1-F12)**: Use page content from Phase 1.

For LLM-assessed checks (F1, F7, F9, F11), judge against SPECIFIC criteria from the
knowledge model, not open-ended assessment:
- F1: First 150 words must contain entity name + category + function ("X is a Y that does Z")
- F7: Count entity names vs pronouns ("it", "they", "our", "your") in first 300 words
- F9: First sentence matches "X is a Y" or "X is [article] Y that" pattern
- F11: Each H2 section must be independently comprehensible if extracted alone

---

### Phase 7: AEO Trust Checks (G)

Run checks G1-G8. All use data already collected.

---

### Phase 8: Competitor Crawl + AEO Selection Checks (H)

**This phase is MANDATORY on every audit. Do not skip it.**

Read `references/competitor-gap-template.md`.

**Step 1**: From Phase 3 WebSearch results (across primary + variant + category queries),
extract top 5 unique organic URLs. Exclude: same domain, video results, social profiles.

**Step 2**: WebFetch each competitor URL **IN PARALLEL** with this prompt:
```
"Extract: title tag, meta description, all H1-H3 headings, all JSON-LD schema blocks
(complete types and key fields), first 300 words of body text, FAQ pair count, table count,
word count estimate, author name and credentials if visible, publication and modified dates,
outbound link count, internal link count."
```

**Step 3**: For each competitor, extract structural signals per `references/competitor-gap-template.md`.

**Step 4**: Run checks H1-H8 by comparing target page against competitor data.

**Step 5**: Produce the competitor comparison table in the report (REQUIRED — not optional):
```
## Competitor Comparison — "[primary query]"

| Signal | Your Page | Comp 1 | Comp 2 | Comp 3 |
|---|---|---|---|---|
| Word Count | X | X | X | X |
| FAQ Pairs | X | X | X | X |
| Schema Types | X | X | X | X |
| dateModified | X | X | X | X |
| Author | X | X | X | X |
| Outbound Citations | X | X | X | X |
| Comparison Table | X | X | X | X |

Note: Based on SERP results for "[query]" on [date]. Results vary by location/time.
```

**If competitors cannot be crawled** (WebFetch fails, no SERP results found), include the
section heading with: "Competitor comparison not available: [reason]. H1-H8 scored as N/A."

---

### Phase 9: GEO Checks (I)

Read `references/geo-framework.md` and `references/knowledge-geo.md`.

Run WebSearch queries for brand presence, accuracy, favorability.
Use the **category query** and **branded query** from Phase 3c.

**All GEO findings carry truth_badge: "model_judgment"** — present as directional
intelligence, not hard audit truth:
```
△ I1 — Brand not in category search results [MODEL JUDGMENT]
  Searching "best group trip planning app" returns SquadTrip, Wanderlog, TripIt.
  TRYPS appears in zero results. This is a directional signal — results vary by
  location and session.
```

---

### Phase 10: Entity Consistency Checks (J)

Run checks J1-J4 using data already collected.

---

### Phase 11: Scoring (Gating-Aware, Separated)

Read `references/scoring-rubric.md`.

**Step 1: Check gates**
If any gate from Phase 2 failed, flag in scoring header:
```
⚠ GATE FAILURE: [description]
Scores below represent page quality IF the blocking issue is resolved.
Current real-world visibility: effectively zero.
```

**Step 2: Calculate TWO separate composite scores**

**Page Citation Readiness** (can this page be found, extracted, trusted, selected?):
```
page_citation = A(15%) + B(10%) + C(12%) + D(15%) + E(12%) + F(12%) + G(8%) + H(8%) + J(3%)
```
Excludes GEO — that's a brand problem, not a page problem.

**Brand AI Presence** (does this brand exist in AI's category knowledge?):
```
brand_presence = I (GEO score — single section, reported separately)
```

**Step 3: Calculate section scores and overall**
- Per-section: earned_points / applicable_max_points * 100
- Overall: weighted average including all 10 sections
- Grade: A+ through F per `references/scoring-rubric.md`

**Step 4: Effort/Impact tagging + Priority matrix**
- DO NOW / PLAN / LATER / SKIP per `references/scoring-rubric.md`

---

### Phase 12: Fix Generation (Remediation-Type-Aware)

For every check marked DO NOW or PLAN, generate the fix with:

1. **Before/after** — exact current state and recommended state
2. **Truth badge** — inherited from the check
3. **Remediation type tag:**

| Tag | Meaning | Example |
|---|---|---|
| PAGE HTML FIX | Add/modify a tag — developer, 5 min | Add canonical tag |
| SCHEMA FIX | Add/modify JSON-LD — developer, 15 min | Add FAQPage schema |
| CONTENT RESTRUCTURE | Rewrite section — writer, 1-2 hours | Rewrite intro as entity definition |
| SITEWIDE TEMPLATE FIX | Applies to all pages — developer, 30 min | Add dateModified to all schema |
| CMS/PLATFORM CONSTRAINT | Requires platform change | Can't add JSON-LD on free Wix plan |
| OFF-PAGE / ENTITY WORK | Marketing, 4-8 weeks | Get listed on G2, create comparison pages |
| CANNOT FIX FROM THIS PAGE | Org-level investment | Domain authority, brand recognition |

**Fix precision rules:**
- Schema fixes: Complete, valid JSON-LD blocks — copy-paste ready
- Meta tag fixes: Exact `<meta>` tag to add/modify
- Content fixes: Quote current text AND provide specific rewrite
- robots.txt fixes: Exact lines to add/remove
- Never use: "consider", "you might want to", "it would be good to"
- Always use: "Add this:", "Change this to:", "Replace with:", "Remove this:"
- Every fix references the check ID it resolves
- Every fix shows its remediation type tag

---

### Phase 13: Source-First Citation Enrichment (Layered Architecture)

**CORE PRINCIPLE: Source is the headline. Rule code is the footnote.**

Reports must cite the authoritative source (Google, Perplexity, Schema.org, etc.) as the
PRIMARY identifier, not internal Sieve rule IDs. A user reading "Per Google's official
documentation..." trusts the finding. A user reading "Per Sieve Rule #1474" does not.

Read `references/brain-mappings.md` for curated links.

---

#### Source Tiering (Applied to EVERY Citation)

Every brain entry's `source_org` maps to a tier. Use tier icons in all reports:

| Tier | Icon | Sources | Framing |
|---|---|---|---|
| **Tier 1 — Primary** | 🥇 | Google, Schema.org, Perplexity, Bing, Microsoft, W3C, Apple (developer.apple.com) | "Per [org]'s official documentation" |
| **Tier 2 — Research** | 🥈 | Backlinko, Ahrefs, Semrush, Princeton/arXiv, Vercel, BrightEdge | "Per [org]'s research study" |
| **Tier 3 — Industry** | 🥉 | Search Engine Land, Search Engine Journal, Moz, HubSpot | "Per industry analysis at [org]" |
| **Tier 4 — Specialized** | 📎 | amsive.com, almcorp.com, cxl.com, seerinteractive.com, Y Combinator, apptweak | "According to [org]" |

**Tier assignment rule:** When multiple sources back one finding, show the highest tier first.
Group citations by tier in Layer 3 reports.

---

#### Layer 2 — Curated Mappings (Direct ID Lookup WITH Sources)

For each FAIL or WARN finding, look up the check_id in brain-mappings.md and fetch:

```sql
-- Rules WITH source documents (CROSS JOIN LATERAL for array unnest)
SELECT r.id as rule_id, r.name as rule_name, r.confidence_score::text,
  r.source_org, d.title as source_title, d.source_url
FROM rules r
CROSS JOIN LATERAL jsonb_array_elements_text(r.source_refs_json::jsonb) as doc_id_text
LEFT JOIN documents d ON d.id = doc_id_text::int
WHERE r.id IN ($mapped_rule_ids);

-- Anti-patterns WITH source documents
SELECT ap.id as ap_id, ap.title as ap_name, ap.risk_level::text,
  ap.source_org, d.title as source_title, d.source_url
FROM anti_patterns ap
CROSS JOIN LATERAL jsonb_array_elements_text(ap.source_refs_json::jsonb) as doc_id_text
LEFT JOIN documents d ON d.id = doc_id_text::int
WHERE ap.id IN ($mapped_ap_ids);
```

**Fallback** (if `source_refs_json` is empty array or missing):
```sql
SELECT id, name, confidence_score::text, source_org FROM rules WHERE id IN ($ids);
```
Cite `source_org` alone without URL.

---

#### Source-First Citation Format (USE EVERYWHERE)

**Standard format (source is the headline):**
```
📌 Per [source_org]'s [official docs / research study / industry analysis] at [url]:
   "[source_title]"
   "[direct quote from rule/AP — the if_condition or description]"
   [Evidence: Sieve Rule #ID, confidence score]
```

**Compact format (for check tables and inline mentions):**
```
🥇 Google (developers.google.com) — "AI Overviews Eligibility" [Rule #1441]
🥈 Backlinko (backlinko.com) — "LCP Must Load Within 2.5 Seconds" [Rule #7190]
```

**Minimal fallback** (source_url not available):
```
🥇 Schema.org — "Organization" [Rule #1668, 0.98]
```

**No source available** (rare — <1% of brain entries):
```
[Sieve Rule #ID, confidence score] — "[rule name]"
```

---

#### Where Sources Appear in Every Report

| Report Section | Format |
|---|---|
| **"Why This Page Isn't Being Cited"** (3 bullets) | Inline: "Per Google's official documentation (developers.google.com)..." |
| **Top 5 Fixes — WHY block** | Full tier-badged citation block with org, URL, document title, direct quote |
| **Layer 2 check tables** — failed checks | Compact format: `🥇 Google — "..." [Rule #X]` below each failed check |
| **Layer 3 Brain Intelligence Applied** | Grouped by TIER with full source chain |
| **Supplementary Findings** | Source headline with rule ID as evidence |
| **Static rule checks (E5, F1, etc.)** | Research citations from knowledge models surface in live report |

---

#### Example Report Outputs

**Example A — "Why This Page Isn't Being Cited" bullet:**
```
- **No datePublished or dateModified anywhere** [HARD EVIDENCE]

  Per Perplexity's official documentation (docs.perplexity.ai) and Backlinko's
  AI SEO research (backlinko.com): freshness signals are a primary ranking
  factor — 50% of AI-cited content is less than 13 weeks old.

  [Evidence: Sieve Rules #1474, #7190]
```

**Example B — Top Fix:**
```
### Fix #1: Add datePublished + dateModified
**Impact:** Critical | **Effort:** Trivial | **Priority:** DO NOW
**Type:** PAGE HTML FIX + SCHEMA FIX

**BEFORE:** (no dates in schema)
**AFTER:** "datePublished": "2025-01-01", "dateModified": "2026-04-13"

**WHY THIS MATTERS:**

🥇 Per Google's official documentation at developers.google.com:
   "AI Overviews eligibility requires indexed content with freshness signals."
   [Sieve Rule #1440, confidence 0.99]

🥇 Per Perplexity's official documentation at docs.perplexity.ai:
   "Signal content freshness with visible timestamps and substantive updates."
   [Sieve Rule #1474, confidence 0.95]

🥈 Per Backlinko's AI SEO research at backlinko.com:
   "50% of content cited in AI search responses is less than 13 weeks old."
   [Sieve Rule #7190, confidence 0.97]
```

**Example C — Layer 2 check table row:**
```
✗ G5 | dateModified visible AND in schema | HARD EVIDENCE | PAGE HTML FIX
   Sources:
   🥇 Perplexity (docs.perplexity.ai) — "Signal freshness with timestamps" [Rule #1474]
   🥈 Backlinko (backlinko.com) — "Freshness is primary AI signal" [Rule #7190]
```

**Example D — Layer 3 Brain Intelligence (grouped by tier):**
```
## Brain Intelligence Applied

🥇 TIER 1 — PRIMARY SOURCES (Official Documentation)

   📌 Google — "AI Overviews Eligibility and Technical Requirements"
      developers.google.com/search/docs
      Applied to: E3, E4, A10, F1
      Evidence: Sieve Rules #1440, #1441, #1442, #1448

   📌 Perplexity — "Technical Setup and robots.txt Configuration"
      docs.perplexity.ai
      Applied to: E1, E2, E5
      Evidence: Sieve Rules #1479, #1480, #1481, #1487

   📌 Schema.org — "FAQPage"
      schema.org/FAQPage
      Applied to: D9
      Evidence: Sieve Rules #1600, #1602, #1603

🥈 TIER 2 — RESEARCH SOURCES (Data-Driven Studies)

   📌 Backlinko — AI SEO data studies
      backlinko.com
      Applied to: F1, F11, D1, B10, I1, G3
      Evidence: Rules #7176, #7190; Anti-patterns #4698, #4602, #4763, #4623, #4607

   📌 Princeton/Georgia Tech/IIT Delhi — "GEO Research Paper (KDD 2024)"
      arxiv.org/abs/2311.09735
      Applied to: F8, G3, I1
      Evidence: Knowledge model references
```

---

#### Layer 3 — Supplementary Scan (Beyond 102 Checks)

After all 102 checks complete, query Sieve for anti-patterns NOT mapped to any check:

```sql
SELECT ap.id, ap.title, ap.description, ap.risk_level::text,
  ap.source_org, d.title as source_title, d.source_url
FROM anti_patterns ap
CROSS JOIN LATERAL jsonb_array_elements_text(ap.source_refs_json::jsonb) as doc_id_text
LEFT JOIN documents d ON d.id = doc_id_text::int
WHERE ap.domain_tag IN ('seo','aeo','entity')
  AND ap.id NOT IN ([all mapped IDs])
  AND (ap.title ILIKE '%[keyword]%' OR ap.description ILIKE '%[keyword]%')
  AND ap.risk_level IN ('high','medium')
LIMIT 10;
```

Supplementary finding format (source IS the headline):
```
## Supplementary Findings (from Sieve Brain)

⚠ Sparse or outdated off-site brand mentions

Per Backlinko's AI SEO research (backlinko.com): "Having few or outdated mentions
of your brand across third-party platforms (G2, Reddit, forums) reduces AI citation
likelihood."

Your brand appears on: LinkedIn, Trustpilot
Your competitors appear on: G2, Capterra, Product Hunt, Reddit, Forbes

🥈 Evidence: Sieve AP #4607 (Backlinko, high risk)
```

---

#### Static Rule Source Surfacing

Static checks (101 of 102 from static-rules.md) have their own research citations.
**These must surface in the live report — not buried in reference files.**

For every STATIC RULE or HEURISTIC check that fails, read `static-rules.md` for its
**Research:** line and surface it alongside the brain citations.

Example for E5 (content in raw HTML):
```
✗ E5 — Content not in raw HTML [STATIC RULE]

Per Vercel Engineering's analysis of 500M+ GPTBot requests (vercel.com/blog):
"Zero JavaScript execution by GPTBot across all measured requests. Content
rendered only via client-side JS is invisible to GPTBot, PerplexityBot, and
ClaudeBot."

📎 Also per amsive.com — "AI Crawler JavaScript Avoidance Rule" [Sieve Rule #2015]
```

---

#### Enrichment Rules

- Brain enrichment is **ADDITIVE** — cannot change pass to fail or override static rules
- Anti-pattern match with risk=high CAN escalate severity (medium → high)
- **Maximum 3 source citations per finding** (more = noise). When > 3 sources exist,
  prioritize by tier (Tier 1 > Tier 2 > Tier 3 > Tier 4)
- If Sieve unreachable → skip enrichment, fall back to static rule research citations only
- Track supplementary findings in `website_audit_supplementary` table for review cycle

**Rule code visibility rule:** Rule IDs (`#1487`, `#4698`) appear ONLY inside brackets as
secondary evidence references. They never appear as the primary identifier of a finding.
The user reads "Per Google's docs..." first; the rule code is a footnote.

---

### Phase 14: Persist + Report

**MANDATORY — both persistence paths run on every audit. Neither is optional.**
The only acceptable reason to skip is a tool-level failure (Supabase unreachable, filesystem
read-only). User speed preferences do NOT override persistence — a report that isn't saved
cannot be trended, shared, or compared across runs.

**14a. Persist to Supabase** (read `references/supabase-queries.md`):

Project: `aldraxqsqeywluohskhs`

Step 1 — Insert the audit row into `website_audits`:
```sql
INSERT INTO website_audits (
  url, domain, page_type, company_name, company_description, industry,
  competitors, target_queries,
  overall_score, overall_grade, section_scores,
  total_checks, passed, failed, warnings, na_checks,
  competitor_data, executive_diagnosis, top_fixes,
  audit_version, audit_duration_seconds
) VALUES (...)
RETURNING id, url, overall_score, overall_grade, audited_at;
```

Capture the returned `id` — this is the `audit_id` used for all findings.

**Required field mappings:**
- `section_scores` — JSONB with A_technical, B_performance, C_onpage, D_schema,
  E_aeo_discovery, F_aeo_extraction, G_aeo_trust, H_aeo_selection, I_geo, J_entity,
  plus `page_citation_readiness` and `brand_ai_presence` composites
- `competitors` — JSONB array of competitor domains
- `target_queries` — JSONB array of `{type, query}` objects (primary, variant, category, branded)
- `competitor_data` — JSONB array of per-competitor structural signals (word_count, faq_pairs,
  schema_blocks, dates, h1_count, outbound_links, sameas)
- `top_fixes` — JSONB array of top 5 fixes with rank, title, impact, effort, type, check
- `audit_version` — string, e.g. `'3.0'`

Step 2 — Batch-insert findings into `website_audit_findings` (one row per FAIL or WARN):
```sql
INSERT INTO website_audit_findings (
  audit_id, check_id, category, status, severity, title, description,
  fix_description, fix_effort, fix_impact
) VALUES
  ('<audit_id>', 'B1', 'performance', 'fail', 'high', '...', '...', '...', 'moderate', 'high'),
  ...
;
```

**Category values must be one of:** `technical`, `performance`, `onpage`, `schema`,
`aeo_discovery`, `aeo_extraction`, `aeo_trust`, `aeo_selection`, `geo`, `entity`.

**Status values:** `pass`, `fail`, `warn`, `na` — only insert `fail` and `warn` rows
(passed checks are reflected in the summary counts on the main audit row).

Step 3 — If Supabase is unreachable: warn the user prominently in the report footer,
but still proceed with 14b. Do NOT silently drop persistence.

---

**14b. Save markdown report to local filesystem** (MANDATORY):

Write the full Layer 1 + 2 + 3 report to:
```
/Users/arunsharma/Documents/New project/audit-reports/<slug>-audit-<N>-<YYYY-MM-DD>.md
```

**Filename rules:**
- `<slug>` = domain with dots replaced by dashes, e.g. `answermonk-ai`, `jointryps-com`
- `<N>` = run number for this domain. Check existing files in `audit-reports/` first:
  - If no file matches `<slug>-audit-*`, use `1`
  - Otherwise, find the highest N and use N+1
- `<YYYY-MM-DD>` = audit date in UTC

---

**CRITICAL — the markdown file MUST be a complete byte-for-byte mirror of the in-session
report, not a summary, not an abridged version, not a "prose rendition" of tables.**

This is the archival copy the user returns to in 3 months when they cannot remember what
was in the chat. The in-session report is ephemeral; the markdown file is the permanent
record. They must contain identical content.

**Mirror requirements (every single one is mandatory):**

1. **Header block** — URL, domain, page type + confidence, company, industry, date, version,
   duration, competitors, all 4 target queries, AND the Audit ID captured from 14a
2. **Gate status block** — full text of all 3 gates (crawl, content access, real page),
   even when all pass
3. **Scores section** — Page Citation Readiness table with all 9 sections (A–H + J) with
   score AND grade, Brand AI Presence table with Presence/Accuracy/Favorability, composite
   scores line
4. **Check Summary table** — total/passed/failed/warn/na counts
5. **Why This Page Isn't Being Cited** — exactly 3 bullets, each with full source citations
   including tier icon, source org, URL, document title, direct quote, and Sieve rule IDs
6. **Bot's Eye View table** — metric/value/source rows from curl data + classification line
7. **Performance table** — TTFB, total load, page size, HTTP version, HSTS, compression,
   cache-control with source citations
8. **Competitor Comparison table** — full signal-by-signal table with ALL crawled
   competitors as columns. Key Gaps numbered list. Comparative strengths list. Date footer.
9. **Top 5 Fixes** — each fix is a full section with Impact/Effort/Priority/Type/Evidence
   header line, BEFORE block, AFTER block (with complete code examples — JSON-LD blocks,
   exact meta tags, exact robots.txt lines), and WHY block with multiple tier-badged
   citations. Do NOT abbreviate any fix to one-liner bullets.
10. **Quick Wins list**
11. **Layer 2 — Section A–J per-check tables** — every section has a full table with the
    exact columns `| Status | ID | Finding | Truth | Fix Type |`. EVERY check run gets a
    row, including PASS, FAIL, WARN, and N/A statuses. Never replace tables with prose
    summaries. Sources footnote under each section where applicable.
12. **AEO Stage Analysis** — 4-row table (Discovery/Extraction/Trust/Selection) with score
    and verdict, plus diagnosis paragraph
13. **GEO Dimension Analysis** — Presence/Accuracy/Favorability breakdown with note about
    directional assessment
14. **Layer 3 — Competitor Profiles** — prose block per competitor with all crawled signals
15. **Layer 3 — Schema Audit Detail** — enumerated list of every schema block found with
    fields present and missing fields called out, followed by the complete generated fix
    JSON-LD block (not a description of what to add — the actual code)
16. **Entity Consistency Matrix table**
17. **Bot's Eye View — Full Detail** — curl response details, content verification list,
    AI search presence verification with query results, classification statement
18. **All Checks Index table** — category rows with run/passed/failed/warn/na counts
19. **Brain Intelligence Applied** — grouped by tier (🥇 Tier 1, 🥈 Tier 2, 🥉 Tier 3,
    📎 Tier 4) with source org, URL, applied-to check IDs, and evidence rule IDs for each
20. **Supplementary Findings** — Sieve brain findings beyond the 103 checks with full
    source attribution
21. **Audit Metadata** — version, checks run, pass/fail/warn/na, gates, classification,
    competitors count, Chrome status, brain matches, previous audit, queries, data sources
22. **Summary / "What to do this week"** — DO NOW list + PLAN list + honest framing paragraph
23. **Persistence confirmation footer** — Supabase audit_id + markdown file path

**Forbidden shortcuts in the markdown file:**
- ❌ "Key Findings by Section" prose summaries replacing per-check tables
- ❌ Abridged Top 5 Fixes without BEFORE/AFTER code blocks
- ❌ Competitor comparison condensed to one sentence per competitor
- ❌ Schema Audit Detail without the generated fix code
- ❌ Brain Intelligence Applied collapsed to a flat list (must be tier-grouped)
- ❌ Any section heading without its corresponding content ("Not available this run"
  is acceptable ONLY when the data genuinely could not be collected)

**Length expectation:** A full audit markdown file is typically 600–1000+ lines. If the
file you just wrote is under 500 lines, it is almost certainly missing content — re-check
against items 1–23 above before declaring 14b complete.

**Write order:** Draft the in-session report first (Phase 14c), then copy it verbatim
into the markdown file. Do not write two different versions of the report. The markdown
file IS the report; the in-session response is just the same content streamed to chat.

If the local filesystem is read-only or the audit-reports directory does not exist,
create the directory first with a single `mkdir -p` call, then write the file.

---

**14c. Generate the 3-layer in-session report:**

After both persistence paths complete, produce the full report in the chat response
per the Output Format below. The in-session report is identical in content to the
markdown file.

**Report footer (required on every run):**
```
---
**Persistence confirmation:**
- Supabase: audit_id <uuid> (or "unreachable — not persisted" with reason)
- Markdown: audit-reports/<filename> (or "filesystem error — not saved" with reason)
```

Never declare an audit complete without this footer.

---

## Output Format

**CRITICAL: The report structure is FIXED. Every audit produces ALL 3 layers with ALL sections.
No sections may be silently dropped. If a section cannot be populated (e.g., no competitors
crawled), include the section heading with an explicit note: "Not available this run: [reason]."**

### REPORT COMPLETENESS CHECKLIST (verify before outputting)

Before generating the report, confirm EVERY item below will be included. If any is missing,
add it before finalizing output.

**Layer 1 — Executive Diagnosis:**
- [ ] Header block (URL, domain, page type + confidence, company, date, competitors, 4 queries)
- [ ] Gate status (all passed / gate failure prominently displayed)
- [ ] Page Citation Readiness score table (sections A-H + J, each with score + grade)
- [ ] Brand AI Presence score table (Presence / Accuracy / Favorability)
- [ ] Composite scores line (SEO Score, AEO Score, Citation Readiness)
- [ ] Trend table (if previous audit exists — metric / previous / current / change)
- [ ] "Why This Page Isn't Being Cited" — exactly 3 bullets, each with [TRUTH BADGE]
- [ ] Bot's Eye View table (word count, schema, FAQ, JS dependency — from curl data)
- [ ] Performance table (TTFB from curl always, CWV from Chrome if available)
- [ ] Competitor Comparison table (structural gap table with numbers)
- [ ] Key Gaps — 3 numbered bullets with specific numbers
- [ ] Top 5 Fixes — each with: Impact/Effort/Priority, Type tag, Truth badge, BEFORE/AFTER code blocks, WHY with brain citation
- [ ] Quick Wins list

**Layer 2 — Detailed Findings:**
- [ ] Section A — Technical SEO: check-by-check table (Status | ID | Finding | Truth | Fix Type)
- [ ] Section B — Performance: same table format
- [ ] Section C — On-Page SEO: same table format
- [ ] Section D — Schema: same table format
- [ ] Section E — AEO Discovery: same table format
- [ ] Section F — AEO Extraction: same table format
- [ ] Section G — AEO Trust: same table format
- [ ] Section H — AEO Selection: same table format (all marked COMPARATIVE)
- [ ] Section I — GEO: same table format (all marked MODEL JUDGMENT)
- [ ] Section J — Entity: same table format
- [ ] AEO Stage Analysis (Discovery XX% / Extraction XX% / Trust XX% / Selection XX%)
- [ ] GEO Dimension Analysis (Presence / Accuracy / Favorability)

**Layer 3 — Technical Reference:**
- [ ] Competitor Profiles (per references/competitor-gap-template.md)
- [ ] Schema Audit Detail (current blocks + validation + generated fix JSON-LD)
- [ ] Entity Consistency Matrix table
- [ ] Bot's Eye View Full Detail
- [ ] All Checks Index table (ID | Status | Title | Truth Badge | Severity | Fix Type | Effort | Impact)
- [ ] Brain Intelligence Applied — WITH SOURCES (rule → org → document title → URL for each)
- [ ] Audit Metadata (version, checks run, gates, classification, competitors, Chrome status, brain matches, previous audit, queries, data sources)
- [ ] **Persistence confirmation footer (MANDATORY — both paths):**
  - [ ] Supabase: `audit_id <uuid>` from INSERT RETURNING (or error reason)
  - [ ] Markdown: `audit-reports/<slug>-audit-<N>-<date>.md` path (or error reason)
  - [ ] Both must be attempted on every audit — do not silently skip either

**Source citation requirements (source-first format):**
- [ ] Every failed check with a brain mapping shows source as HEADLINE, rule ID as footnote
- [ ] Every source citation carries a tier icon (🥇 Tier 1, 🥈 Tier 2, 🥉 Tier 3, 📎 Tier 4)
- [ ] "Why This Page Isn't Being Cited" bullets lead with "Per [org]'s..." not rule numbers
- [ ] Top 5 Fixes have tier-badged citation blocks with org, URL, document title, and direct quote
- [ ] Layer 2 check tables show compact format: `🥇 [org] ([url]) — "[title]" [Rule #X]`
- [ ] Layer 3 Brain Intelligence Applied groups citations BY TIER (Tier 1 first, then 2, 3, 4)
- [ ] Rule codes (#1487, #4698) appear ONLY inside brackets as footnotes — never as primary identifiers
- [ ] Static rule checks surface their research citations from `static-rules.md` in live findings
- [ ] Maximum 3 source citations per finding (prioritize by tier when > 3 available)

**Table format consistency rule:** Every section in Layer 2 uses the SAME table columns:
`| Status | ID | Finding | Truth | Fix Type |`
No section uses a different format. No section is summarized instead of tabled.

---

### LAYER 1 — Executive Diagnosis

```markdown
# SEO + AEO + GEO Audit Report
**URL:** [full URL]
**Domain:** [domain]
**Page Type:** [type] ([confidence] confidence)
**Company:** [name] — [one-line description]
**Date:** [audit date]
**Competitors analyzed:** [domains]
**Target queries:**
  - Primary: [query]
  - Variant: [query]
  - Category: [query]
  - Branded: [query]

---

[IF GATE FAILURE:]
## ⚠ BLOCKING ISSUE DETECTED

[Gate failure description — this dominates the report]

Everything below assumes this issue is resolved first.
Current real-world visibility: effectively zero.

---

## Scores

### Page Citation Readiness: XX% ([Grade])
Can this page be found, extracted, trusted, and selected by AI answer engines?

| Section | Score | Grade |
|---|---|---|
| Technical SEO (A) | XX% | [grade] |
| Performance (B) | XX% | [grade] |
| On-Page SEO (C) | XX% | [grade] |
| Schema (D) | XX% | [grade] |
| AEO: Discovery (E) | XX% | [grade] |
| AEO: Extraction (F) | XX% | [grade] |
| AEO: Trust (G) | XX% | [grade] |
| AEO: Selection (H) | XX% | [grade] |
| Entity (J) | XX% | [grade] |

### Brand AI Presence: XX% ([Grade])
Does this brand exist in AI's understanding of the category?

| Dimension | Score |
|---|---|
| Presence | XX% |
| Accuracy | XX% |
| Favorability | XX% |

Note: Brand presence is a directional assessment based on web search signals.
Page edits alone cannot fix brand presence — this requires content strategy
and entity-building work over months.

**Composite:**
- SEO Score: XX%
- AEO Score: XX%
- Citation Readiness: XX%

---

## Why This Page Isn't Being Cited

[3 bullets — most impactful findings, plain language]

- **[Finding 1]** [HARD EVIDENCE]: [description]
- **[Finding 2]** [STATIC RULE]: [description]
- **[Finding 3]** [MODEL JUDGMENT]: [description — noted as directional]

---

## Bot's Eye View

| Metric | Raw HTML (AI bots) | Rendered (Humans) | Gap |
|---|---|---|---|
| Word count | X | Y | Z% |
| Schema | X blocks | X blocks | No gap |
| FAQ content | [status] | visible | [gap] |

AI crawler access: [FULLY ACCESSIBLE / SSR/STATIC / PARTIALLY JS-DEPENDENT / JS-DEPENDENT]

---

## Performance (Measured)  [only if Chrome MCP ran]

| Metric | Value | Rating | AI Impact |
|---|---|---|---|
| TTFB | Xms | [Good/NI/Poor] | [AI crawler note] |
| LCP | Xs | [Good/NI/Poor] | [threshold note] |
| CLS | X | [Good/NI/Poor] | — |
| Page Weight | X MB | — | — |
| Requests | X | — | — |

Note: CWV is a threshold signal. Passing "Good" removes a penalty but provides
no additional ranking benefit. TTFB matters most for AI crawlers.

---

## Competitor Comparison — "[primary query]"

[Gap table per references/competitor-gap-template.md]

Note: Based on SERP results for "[query]" on [date]. Results vary by location/time.

**Key Gaps:**
1. [Gap with numbers]
2. [Gap with numbers]
3. [Gap with numbers]

---

## Top Fixes (Ranked by Impact)

### Fix #1: [title]
**Impact:** [Critical/High] | **Effort:** [Trivial/Easy/Moderate] | **Priority:** DO NOW
**Type:** [PAGE HTML FIX / SCHEMA FIX / CONTENT RESTRUCTURE / OFF-PAGE / etc.]
**Evidence:** [truth badge]

**BEFORE:**
[current state]

**AFTER:**
[recommended state]

**WHY:** [one sentence — citing brain rule or knowledge model data point]

[Continue for top 5 fixes]

---

## Quick Wins
[Trivial-effort fixes not in top 5]
```

### LAYER 2 — Detailed Findings

```markdown
## All Findings by Section

[For each section A-J:]

### Section X — [Name] (X/Y passed)

| Status | ID | Finding | Truth | Fix Type |
|---|---|---|---|---|
| ✓ | A1 | HTTPS enforced | HARD EVIDENCE | — |
| ✗ | A3 | Meta description missing | HARD EVIDENCE | PAGE HTML FIX |
| △ | A7 | H1 keyword alignment weak | HEURISTIC | CONTENT RESTRUCTURE |

[Failed checks include description + fix inline]

---

## AEO Stage Analysis

### Stage 1: Discovery — XX%
[E1-E10 with truth badges]

### Stage 2: Extraction — XX%
[F1-F12 with truth badges — LLM-assessed checks clearly marked]

### Stage 3: Trust — XX%
[G1-G8]

### Stage 4: Selection — XX% (Competitor-Relative)
[H1-H8 — all marked COMPARATIVE]

---

## GEO Dimension Analysis (Directional Assessment)

All GEO findings are MODEL JUDGMENT based on web search proxies.
Results vary by location, session, and time.

### Presence — XX%
### Accuracy — XX%
### Favorability — XX%
```

### LAYER 3 — Technical Reference

```markdown
## Competitor Profiles
[Per references/competitor-gap-template.md]

## Schema Audit Detail
[Current schema with validation + generated fix blocks]

## Entity Consistency Matrix
| Entity | Schema | OG Tags | Title | Footer | Consistent? |
|---|---|---|---|---|---|

## Bot's Eye View — Full Detail
[Raw HTML extract vs Chrome extract comparison]
[FAQ accordion content verification]
[AI search presence check results]

## All Checks Index
| ID | Status | Title | Truth Badge | Severity | Fix Type | Effort | Impact |
|---|---|---|---|---|---|---|---|

## Brain Intelligence Applied
[Rules, anti-patterns, principles cited — with IDs and confidence scores]

## Audit Metadata
- Version: 3.0
- Checks run: X/97 | Passed: X | Failed: X | Warnings: X | N/A: X
- Gates: [all passed / GATE FAILURE: description]
- Page classification: [type] ([confidence])
- Competitors analyzed: X
- Chrome MCP: [available — Lighthouse measured / unavailable — HTML indicators only]
- Brain entries matched: X
- Previous audit: [date, score] or "First audit"
- Queries used: primary, variant, category, branded
```

---

## Error Handling

| Error | Recovery |
|---|---|
| WebFetch fails on target URL | Try Chrome MCP; if unavailable, ask for pasted HTML |
| robots.txt returns 404 | Note "no robots.txt", pass E1-E3/E10 as WARN (permissive default) |
| sitemap.xml not found | Note, fail E8, mark A11 |
| WebFetch fails on competitor URLs | Skip that competitor, note reduced comparison |
| Supabase unreachable | Skip persistence, warn user |
| Brain tables empty | Skip enrichment, use built-in rubrics, note in report |
| < 5 SERP competitors | Use what's available, note count |
| Chrome MCP unavailable | Skip Phase 1.5/1.6 Chrome parts, note in metadata |
| Page classification LOW confidence | Widen N/A zone, use generous thresholds, note in report |
| Gate failure | Report prominently, still run checks but frame as conditional |

---

## Multi-URL Mode

Audit each URL independently, then produce comparison:
```
| | Page 1 | Page 2 | Page 3 |
|---|---|---|---|
| Page Citation Readiness | XX% | XX% | XX% |
| Brand AI Presence | XX% | XX% | XX% |
| Gate Issues | [none/description] | [none] | [blocked] |
| Top Fix Type | CONTENT | SCHEMA | OFF-PAGE |
```

---

## Previous Audit Comparison

If previous audits exist:
```
| Metric | Previous ([date]) | Current | Change |
|---|---|---|---|
| Page Citation Readiness | XX% | XX% | +X% |
| Brand AI Presence | XX% | XX% | +X% |
| Checks passed | X/Y | X/Y | +X |
| Gate issues | [status] | [status] | — |
```
