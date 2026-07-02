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
