# Rebuild changelog — findings → fixes

This repo is a hardened rebuild of `aeo-seo-auditor`, structured as a clean
single-implementation service (the three dead parallel implementations —
`skill/`, `scripts-v2/`, and the `skill-unified` skill wrapper — were dropped;
only the deterministic core `service/scripts/` and ruleset were kept).

Below, each verified audit finding maps to what changed.

## P0 — Correctness & credibility

| Finding | Fix | Where |
|---|---|---|
| **SCORE-1 / SD-1** LLM computes the headline grade in its head | New `scoring.py` recomputes section scores, PCR, BAP, overall, and grade **deterministically** from the model's per-check statuses; wired into the agent after the loop. Model classifies, Python grades. | `scoring.py`, `agent.py`, `system_prompt.py` |
| **SEC-3** Stored XSS — raw score values in HTML/style | Added `numOrDash()` / `pctWidth()` coercion; every score sink (hero, section bars, BAP row, library card) now renders a coerced number or `—`. | `main.py` render JS |
| **SEC-4** No server-side validation of LLM scores | `scoring.validate_audit()` clamps every score to `[0,100]`, coerces non-numeric → `None`, forces grade into the enum, before persist/render. | `scoring.py` |
| **SCORE-2 / ENG-4** Divergent weight + grade tables | One `PCR_WEIGHTS` (sums to 1.0) and one `GRADE_TABLE`. The legacy `audit_pipeline.compute_section_scores` now delegates to `scoring.py`. | `scoring.py`, `audit_pipeline.py` |
| **SCORE-7 / PROD-4** GEO/BAP blended into the headline | BAP computed separately with a confidence tag; **excluded** from the letter grade; labelled "directional" in the UI. | `scoring.py`, `main.py` |
| **SCORE-5** FAQ passes on count-equality alone | Compares normalized question **text**, not just counts; downgrades to warn on divergence (conservative). | `scripts/deterministic_checks.py` |
| **SCORE-6** One dead sampled URL fails the whole sitemap check | Grades by **proportion** reachable (≥0.9 pass / ≥0.7 warn / else fail). | `scripts/check_sitemap.py` |

## P1 — Durability & security

| Finding | Fix | Where |
|---|---|---|
| **SEC-1** SSRF on the fetch path (guard was submission-only) | `check_url_safe()` enforced in `dispatch_tool` for every url-taking tool. | `tools.py` |
| **SEC-2** SSRF via redirect | Per-hop redirect validation in the scripts' fetch layer; blocked hops recorded, contract preserved. | `scripts/deterministic_checks.py`, `check_sitemap.py`, `check_robots_txt.py`, `check_schema_completeness.py`, `bots_eye_view.sh` |
| **SEC-5** Auth fails open when unconfigured | `require_auth` + `require_api_key` fail **closed** in production (`IS_PRODUCTION`/`AUDITOR_FAIL_CLOSED`). | `main.py` |
| **ENG-8** `/docs` etc. unauthenticated | Disabled in production. | `main.py` |
| **SEC-6** Unsigned webhooks | HMAC-SHA256 signature (`X-Auditor-Signature`) + retry with backoff. | `main.py` |
| **ENG-1 / SD-4 / PROD-6** Ephemeral job/artifact state; md/pdf 404 after redeploy | `persistence.py`: durable job status write-through + `/md` and `/pdf` **regenerate from Supabase** when the local artifact is gone. | `persistence.py`, `main.py` |
| **SD-2** No caching / cost ceiling; quadratic re-billing | Prompt caching (system block + moving conversation-prefix breakpoint) + per-audit `MAX_AUDIT_COST_USD` circuit-breaker. | `agent.py` |
| **SD-3** One transient error scraps the audit | App-level retry with backoff on 429/529/5xx/network. | `agent.py` |
| **SD-6** PDF dead on Linux (hardcoded macOS Chrome path) | `CHROME_PATH` env resolution + Playwright `page.pdf()` fallback. | `audit_pipeline.py` |
| **ENG-7 / SEC-8** In-memory-only suppression | Durable Supabase suppression table, loaded at boot, written on takedown. | `persistence.py`, `main.py` |
| **ENG-6 / SD-5** Reaper doesn't cancel work | Documented + instrumented; the agent's 900s budget self-terminates the thread before the 1200s reaper fires. | `main.py` |
| **ENG-3** Unpinned dependencies | All deps pinned to exact versions. | `requirements.txt` |

## P2 / P3 — Product loop, metering, polish

| Finding | Fix | Where |
|---|---|---|
| **PROD-1** No re-score / delta loop | `delta.py` + `GET /api/audit/{id}/delta`. | `delta.py`, `main.py` |
| **PROD-2** No path to money / metering | `billing.py`: per-key quota + usage metering + Stripe usage-reporting scaffold (guarded by `STRIPE_SECRET_KEY`); enforced on `/api/audit/start`. | `billing.py`, `main.py` |
| **PROD-5** No monitoring | `monitoring.py`: structured metric events (started/completed/failed/reaped, cost, score) at every lifecycle point. | `monitoring.py`, `main.py` |
| **ENG-2** Tests targeted dead paths; no CI | Test runner repointed at `service/scripts/`; added scoring + delta tests; GitHub Actions CI. | `tests/run_tests.sh`, `.github/workflows/ci.yml` |
| **ENG-5** Four parallel implementations | Dropped to one; scripts + ruleset + references co-located under `service/`. | repo structure |
| **PROD-3** Stale brain snapshots | Documented refresh-out-of-band approach; snapshots kept for reproducibility. | `README.md` |

## GROUND — Citation grounding & provenance display (2026-07-04)

Findings from the 6-lane grounding/determinism audit (code review, DB
forensics, retrieval determinism battery, verbatim check of every persisted
citation, live double-run E2E).

| Finding | Fix | Where |
|---|---|---|
| **GROUND-1** Citations round-trip through the LLM; only ~51% of persisted quote texts verbatim vs the brain; some `then_action` texts were invented remediation attributed to tier-1 sources | New `citation_grounding.py`: after the loop, every citation is re-fetched from the live sieve brain (or snapshot) by `(kind, id)` and all content/source fields are **overwritten with the stored values** — quotes are verbatim-by-construction; the LLM contributes only the check→id mapping. The three brain tables have ~94% id overlap, so a mislabeled `kind` is caught by a lexical plausibility gate and recovered cross-kind (or flagged) rather than mis-attributed. Unresolvable ids are kept but flagged `grounded:'unresolved', verbatim:false`; the renderer demotes them to the 'Other' tier with an *unverified* badge and suppresses their reasoning line. Stats persisted under `metadata.citation_grounding`. | `citation_grounding.py`, `agent.py` |
| **SEC-9** Stored-XSS sink: `confidence_score` (LLM/crawl-originated string) concatenated into report HTML unescaped | Confidence rendered numeric-only (`Number(...)` + `toFixed(2)`, else omitted). | `main.py` render JS |
| **GROUND-2** Report showed a source's name/org/tier but not its *reasoning* or freshness; principles mislabeled 'Item' | "Sources cited" now renders the rule's verbatim if/then reasoning (escaped, 240-char capped), a `verified <date>` badge from `last_verified`, and correct kind labels (Rule/AP/Principle). | `main.py` render JS |
| **OBS-1** `/readyz` reported only the static 4,980-row snapshot; whether the 23k-row live brain was reachable was invisible | `/readyz` now includes `sieve_live_stats` (best-effort `sieve_brain.stats()`: per-table rows + embedded counts + semantic-layer flag). | `main.py` |

## DET — Retrieval & report determinism (2026-07-04)

Closes the determinism findings from the grounding audit (identical inputs
must produce identical cited reports — HANDOFF invariant 3).

| Finding | Fix | Where |
|---|---|---|
| **DET-1** OpenAI query embeddings drift call-to-call (~1e-4/component, survives 7-decimal rounding) → 8/13 checks not byte-identical across runs | **Pinned query embeddings**: first vector per query text is stored in `public.check_query_embeddings` (auditor's own schema) and reused forever; in-process memo + ON CONFLICT re-read converges concurrent audits. Verified byte-identical across 3 fresh processes. Side effects: near-zero embedding cost after warm-up, and cached checks retrieve semantically even when OpenAI is down (kills the semantic→FTS layer-flapping failure). `scripts/warm_query_embeddings.py` pre-pins all 108 canonical checks. | `sieve_brain.py`, `scripts/warm_query_embeddings.py` |
| **DET-2** FTS `ORDER BY score DESC LIMIT k` has no tie-break; ts_rank ties at the cut changed citation identity run-to-run (12/13 checks ambiguous) | `ORDER BY score DESC, t.id::bigint ASC`; python re-rank tie-break extended with `kind` (rules/principles id spaces collide). Vector path needs no SQL tie-break — pinned vector ⇒ deterministic HNSW scan. Verified pure-FTS byte-identical across 3 fresh processes. | `sieve_brain.py` |
| **DET-3** LLM renames check_ids between runs (74/~100 stable: `A10_robots_txt` vs `A10_robots_txt_crawling`), breaking cross-run comparison + delta engine | New `check_vocab.py`: post-loop canonicalization against the brain-mappings registry (108 checks, letter+number prefixes unique). Conservative: exact ids and script sub-checks (`A2b_…`) untouched, renames refused on collision, unknowns flagged. Wired before `finalize_scoring` so scores/persistence/deltas see stable ids; stats under `metadata.check_id_normalization`. System prompt now demands exact ids. | `check_vocab.py`, `agent.py`, `system_prompt.py` |
| **OBS-2** No visibility into pinned-query coverage | `sieve_brain.stats()` (surfaced in `/readyz`) reports `pinned_query_embeddings`. | `sieve_brain.py` |

## CITE+UI — Deterministic citations everywhere + report redesign (2026-07-04)

| Finding | Fix | Where |
|---|---|---|
| **CITE-1** The model cited only ~17% of fail/warn findings, inconsistently between runs (Phase 13 applied ad hoc) | New `citation_attach.py`: post-loop, EVERY fail/warn finding gets the top-3 query_brain citations, replacing the model's selection. Live-verified on a real audit: 59/59 eligible checks cited, 177/177 citations verbatim-grounded, 0 errors. Completes "LLM classifies; Python grades, cites, grounds". Stats under `metadata.citation_attachment`. | `citation_attach.py`, `agent.py` |
| **UI-1** Report design dated; sources shown as plain rows | Full design-system revamp of the embedded SPA: gradient typography, layered glass cards with glows, SVG score-ring gauge hero with meta pills, section scores as color-banded tiles with gradient bars, severity/status chips, tier-tinted source cards with verbatim if→then quote blocks + verified badges, hover states, entrance animations, responsive + reduced-motion safe. Verified locally (desktop + mobile screenshots) against real audits. | `main.py` (INDEX_HTML CSS + render fns) |
| **UI-2** Markdown renderer hard-indexed citation keys and mislabeled principles as 'AP' | Tolerant key access; kind labels Rule/AP/Principle. | `audit_pipeline.py`, `ruleset/ranker.py` |

## VIS — Measured AI visibility (2026-07-04)

| Finding | Fix | Where |
|---|---|---|
| **VIS-1** The product generated 4 test queries + crawled 5 competitors per audit and never used them — "Brand AI Presence" was an LLM inference, not a measurement (graded D in the product evaluation) | New `ai_visibility.py`: post-loop, the audit's test queries are executed K times (default 2) against real answer engines (OpenAI + Anthropic web search now; adapter slots for Perplexity/Gemini/AI Overviews keys), recording cited URLs + brand mentions per run. Reports per-engine cited/mentioned **rates** (honest about stochasticity), share of voice vs the crawled competitors, and top-cited domains. Every raw answer is logged permanently to `public.ai_answer_runs` — the longitudinal dataset. Renders as "Measured AI visibility" (engine tiles + SOV table) directly under the hero. No-ops safely without keys; stats under `metadata.ai_visibility`. | `ai_visibility.py`, `agent.py`, `main.py` |

## LUM — Luminous re-skin: one design family with GrowthMonk (2026-07-05)

| Change | Detail | Where |
|---|---|---|
| **LUM-1** Report + homepage re-skinned to the Luminous design system (tnsaruniitr-lab/design-principles, as implemented in the growthmonk operator console) | Quiet tinted light canvas (auto dark via `prefers-color-scheme`), white surfaces with two-layer shadows, drifting header aurora, gradient indigo→teal wordmark, eyebrow pills. Section scores are now a **jewel constellation**: each of the 10 sections carries only `--g1/--g2` (cool spine, warm anchors) rendering a gradient orb with inner-light highlight + tinted outer glow, corner auras that bloom on hover, staggered 45ms entrances. Engine tiles, fix cards, tier cards, citation quote blocks, tables, library cards all re-tokenized to the shared `--ink/--surface/--hairline/--shadow-card` system. Reduced-motion guarded; all escaping and numeric-only interpolation preserved. | `main.py` (INDEX_HTML CSS + render fns) |

## LUM-2 — Manual dark / light toggle (2026-07-05)

| Change | Detail | Where |
|---|---|---|
| **LUM-2** Theme control | Dark tokens moved from media-query-only to `:root[data-theme="dark"]`; a pre-paint head script applies the saved choice (localStorage `aeo-theme`) or falls back to the system preference — no flash of wrong theme. Floating sun/moon button (top-right) flips + persists; with no saved choice the page keeps following OS scheme changes live. | `main.py` |

## Known remainders (documented, not yet done)

- **Separate worker process / durable queue.** Job *status* is now durable and
  artifacts regenerate, but execution still runs in-process via FastAPI
  BackgroundTasks. A dedicated worker + queue is the next durability step.
- **Inline SPA extraction (ENG-5 UI half).** `main.py` still embeds the ~900-line
  report SPA as a string. Left intact to avoid regressing a working UI; extract
  to a static asset when convenient.
- **Stripe go-live.** `billing.py` meters and enforces quota; the Stripe
  meter-event POST is scaffolded — supply price/meter IDs + a key→customer map.
- **Unguessable share tokens (SEC-7).** Public audit pages remain enumerable by
  domain (intentional for the public library); add share tokens if that changes.
