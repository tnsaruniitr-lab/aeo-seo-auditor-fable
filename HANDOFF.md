# HANDOFF SPEC — Sieve-backed AEO/SEO/GEO Auditor + Freshness System

**Audience:** the engineer/agent continuing this project.
**Read this fully before writing code.** It defines what exists, the invariants you must not break, and the remaining work with exact implementation notes. Also read the two repos' `README.md` + `CHANGELOG-FABLE.md`.

Last updated: 2026-07-04. Everything below is verified against the live system unless marked TODO.

---

## 0. TL;DR — what this system is

A website auditor that scores a page for SEO/AEO/GEO and **cites the authoritative source (Google, Schema.org, Perplexity…) behind every finding**, fed by a **continuously-refreshed rule brain**. Two independent agents, one shared Postgres.

- **Auditor** = on-demand web service. LLM classifies checks; **Python grades deterministically**; citations come from the brain, ranked by authority tier.
- **Ingestion agent** = scheduled cron worker. Polls canonical sources, detects what changed, extracts rules with provenance, writes the brain. Never runs inside the auditor.

---

## 1. Architecture

```
   canonical sources (Google, Schema.org, Perplexity, …)
              │  (sieve-ingest: poll every X days, detect change, extract)
              ▼
   ┌───────────────────────── Railway Postgres ─────────────────────────┐
   │  schema `sieve`  = the brain (rules, principles, anti_patterns,     │
   │                    playbooks, documents) + control tables           │
   │  schema `public` = the auditor's own tables (website_audits, …)     │
   └────────────────────────────────┬───────────────────────────────────┘
              ▲ writes (ingest)      │ reads (query_brain)
              │                      ▼
        sieve-ingest           aeo-seo-auditor-fable (web service)
        (cron worker)          scores pages, cites sources
```

**Rule: two agents, one DB.** Auditor reads; ingestion writes. A heavy crawl+LLM-extract job must never live in the request path.

---

## 2. Where everything lives

| Thing | Location |
|---|---|
| Auditor repo | `github.com/tnsaruniitr-lab/aeo-seo-auditor-fable` (also mirrored to `answermonk-fable5`) |
| Auditor service | Railway project **AEO-SEO-fable**, service `aeo-seo-auditor-fable`, URL `https://aeo-seo-auditor-fable-production.up.railway.app` |
| Ingestion repo | `github.com/tnsaruniitr-lab/sieve-ingest` |
| Ingestion service | Railway project **sieve-ingest** (its own project — CLI quirk), cron `0 6 * * 1`, `restartPolicyType: NEVER` |
| Central DB | Railway Postgres service **Postgres-pxlu** (in AEO-SEO-fable). `sieve` + `public` schemas. pgvector 0.8.4 enabled. |
| Crawler (fetch engine, reused) | local `sieve-crawler/` — has the 12-source org map + sitemap discovery |
| Source data (one-time load) | `~/Downloads/marketo-db-export-20260703.tar.gz` (823 MB), loaded via `scratchpad/sieve-db/load_ruleset.py` |

**Secrets** (never commit; all are Railway env vars): `ANTHROPIC_API_KEY`, `SIEVE_DB_URL`/`DATABASE_URL`, `AUDIT_USERNAME`/`AUDIT_PASSWORD`/`AUDIT_API_KEY`, `AUDIT_WEBHOOK_SECRET`. The auditor login + API key are in Railway vars; ask the owner if you need them.

---

## 3. Data model

### `sieve` schema (the brain) — loaded from the latest Replit export (NOT the stale snapshots)
Row counts at load: `rules 23,289 · principles 18,805 · anti_patterns 11,293 · playbooks 4,212 · playbook_steps 21,588 · documents 5,139` (+ examples, brands, competitors, mapping_runs, mapping_run_sources, query_traces).

**All columns are currently `text`** (loaded permissively; embeddings were dropped in phase-1 — see task P2). Key columns:
- `sieve.rules(id, name, rule_type, if_condition, then_logic, domain_tag, confidence_score, source_refs_json, status, created_at, source_org, ...)` + provenance cols the ingestion agent added: `source_url, document_id, extracted_at, last_verified, rule_key, superseded_by`.
- `sieve.documents(id, title, source_type, domain_tag, author, source_url, source_org, authority_tier, ...)` — **`source_url` is the provenance anchor**; only ~34% populated (1,768/5,139).
- Provenance chain: `rules.source_refs_json = "[268]"` → `documents.id = 268` → `documents.source_url`.
- Indexes: GIN FTS on `sieve.rules` (`rules_fts_idx`), btree on `documents.id`.

### `sieve` schema (ingestion control tables — created by `sieve-ingest`)
- `source_registry(source_id, canonical_org, adapter_type, tier, root_url, sitemap_url, crawl_cadence_days, last_crawled_at, last_seen_marker, enabled, notes)`
- `ingest_runs`, `ingest_changes`, `url_state` — observability + freshness fingerprints.

### `public` schema (auditor) — see `service/db.py`, `service/migrations/001_*.sql`
`website_audits`, `website_audit_findings`, `audit_jobs`, `api_usage`, `suppressed_domains`.

---

## 4. What's DONE (verified)

1. **Auditor rebuilt & hardened** — deterministic Python scoring (LLM classifies, Python grades), XSS/SSRF/auth fixed, durable Postgres persistence, delta re-score loop, billing/monitoring scaffold. See `CHANGELOG-FABLE.md`. Live, CI green.
2. **Latest brain loaded** into `sieve` schema (23k rules, provenance intact; `source_org` ~99.99%).
3. **Live citation retrieval** (`service/sieve_brain.py`) — ADDITIVE, opt-in via `SIEVE_LIVE=1`. FTS over `sieve.rules`, canonicalizes `source_org`, ranks by **tier → confidence → relevance**, resolves `source_url` via the doc join, adds `last_verified`. `query_brain` (service/tools.py) tries it first, **falls through to the snapshot ranker on any miss** (static path untouched). Verified: D6→Google/Schema.org, G1→Schema.org Person, etc.
4. **Ingestion agent** (`sieve-ingest/`) — registry (13 sources) + 3-tier change detection (github release tag / changelog hash / sitemap lastmod→ETag/304→content-hash) + Claude rule extraction with provenance + dedupe-by-`rule_key` (refresh `last_verified`, never delete). Verified end-to-end live: detected Schema.org v30.0 + 20 Backlinko URLs, wrote 6 real rules with `source_org`/`source_url`/`last_verified`. Deployed as cron.

---

## 5. What REMAINS (prioritized, with implementation notes)

### P0 — Source coverage: Google + Perplexity + the no-op sources  ← **START HERE**
**How the original crawler collected these (verified from the `documents` table + `sieve-crawler/run-queue.sh`):** the crawler (`sieve-crawler`) was pointed at a **doc ROOT** and let its `discover_urls()` (sitemap + nav) expand it. What it actually captured:
- **Google — only 2 real SEO/AEO roots**: `https://developers.google.com/search/docs` and `https://developers.google.com/search/docs/appearance/ai-features`. (The other 8 "Google" docs in the brain are Firebase A/B-testing + Play Store — GTM content, not SEO — mislabeled under org "Google".)
- **Perplexity — 1**: `https://docs.perplexity.ai` (root only).
- **Schema.org — 53**: comprehensive, one per type (`/Article`, `/BreadcrumbList`, …). Good.
- **web.dev, Bing, OpenAI, MDN — 0**: never crawled.
- **Also:** stored `source_url` is the crawl ROOT, not the specific sub-page → per-finding citation precision is coarse.

**Problem in the NEW ingestion agent (`sieve-ingest/registry.py`):** it reproduces the gap — `perplexity-docs, openai-docs, bing-webmaster, w3c, semrush-blog, search-engine-land` have no `sitemap_url` (no-op), and `google-search-central` uses the `changelog` adapter that hashes ONE page instead of expanding the doc root.

**Fix — reuse the crawler's proven method, don't reinvent:**
1. Add a `discovery` **adapter** to `freshness.py` that calls `sieve_crawler.discovery.discover_urls()` on the source's doc root (Google `/search/docs`, Perplexity `docs.perplexity.ai`, web.dev, MDN) and change-detects each discovered URL (conditional-GET + content-hash). This captures the FULL doc corpus like the original crawl, and fixes per-page `source_url` granularity (store the specific page, not the root).
2. For sources without discoverable structure, add a `url_list` adapter + `seed_urls jsonb` column on `source_registry`; seed Google/Perplexity/OpenAI/Bing with curated canonical doc URLs (start from the 2 Google roots above + the AI-features page; enumerate `docs.perplexity.ai`).
3. Point `google-search-central` at the doc root with the discovery adapter (not `changelog`); wire `web.dev`, `MDN` (they have sitemaps — already set); add `openai-docs`, `bing-webmaster`.
**Acceptance:** `python -m sieve_ingest run` writes new Google/Perplexity rules whose `source_url` is the **specific** doc page; auditor `query_brain('F1_...')`/`query_brain('D6_...')` cites a fresh Google/Schema doc URL.
**Note:** `sieve-crawler/run-queue.sh` and `~/Desktop/sieve-docs/*` are the historical crawl artifacts — the engine-official-docs output folder is now empty (uploaded then cleared), so the `documents` table is the authoritative record of what was collected.

### P1 — Provenance completeness (the `source_url` 34% gap)
- Many rules resolve to a doc with **no** `source_url` (uploaded/manual docs). New ingestion always stamps `source_url` (good). For the historical 66%: backfill where possible; render honestly (show org even when URL is null).
- **Canonicalize `source_org` at write time** so tiering is right (the brain has both `"Google"` and raw domains like `backlinko.com`). `sieve_brain.canon_org()` already does this at read; mirror it at ingest.
- Surface `last_verified` + a **staleness badge** in the auditor report render (`service/main.py` `renderBrainSources`, ~line 1234–1251) — add "verified `<date>`" and flag >90d.

### P2 — Embeddings / semantic RAG
- Load the dropped `embedding_vector` columns (they're text pgvector reps in the export CSVs) into a `vector` column on `sieve.rules` etc.; add an **HNSW** index.
- Upgrade `sieve_brain.live_citations` from FTS-only to **FTS + vector** retrieval, still re-ranked by tier·confidence·recency. Keep FTS as fallback.
- In `sieve-ingest/extract.py`, embed each new rule on write (OpenAI 1536-dim, matching the corpus).

### P3 — The two-layer check model (the "100+ ruleset" question)
- The ~100 audit checks (`service/references/static-rules.md`) stay the **spine**. Build a `sieve.check_id_map(check_id, rule_key, weight)` binding each check to authoritative rules — replaces the stale hardcoded id map in `service/references/brain-mappings.md`.
- **Candidate-check promotion:** when ingestion finds a new tier-1/2 rule that maps to **no** existing check, insert a `candidate_checks` row for human review. This is how the ruleset grows deliberately without deviating.

### P4 — Ops
- `rule_versions` table + supersede chain (never hard-delete — `db.upsert_rule` already refreshes not-deletes; extend to full versioning).
- Move `sieve-ingest` into the AEO-SEO-fable Railway project so it can use the **internal** DB URL (currently uses the public proxy URL — works, but internal is faster/cheaper). Raise `MAX_URLS_PER_SOURCE` after the first successful run.
- Monitoring: alert on ingest run failures + score-distribution drift.

---

## 6. INVARIANTS — do NOT break these

1. **Additive only.** The live brain path (`SIEVE_LIVE`) and any new retrieval MUST fall back to the existing snapshot ranker on error. Turning `SIEVE_LIVE=0` must restore the exact old behavior. Never delete the snapshot path.
2. **Python grades, the LLM classifies.** Never let the model compute the headline score/grade. All scoring math stays in `service/scoring.py` (single source of truth for weights + grade table). See `SCORE-1` in CHANGELOG.
3. **Reproducibility.** The auditor pins a citation source that yields the same citations on re-run. Retrieval ordering must be deterministic (tier, then confidence, then relevance, then id). Don't introduce randomness.
4. **Provenance is sacred.** Every ingested rule MUST carry `source_org` + `source_url` (when resolvable) + `document_id` + `extracted_at` + `last_verified`. Never write a rule without its source.
5. **Authority-tier ranking.** 53% of the brain is "Personal Blog" (tier 5). Retrieval MUST rank Google/Schema.org/Perplexity (tier 1) above the long tail. Canonicalize `source_org` before tiering.
6. **Never hard-delete brain objects.** Supersede via `rule_versions`/`superseded_by`. Deleting breaks the auditor's citations and history.
7. **Two agents, one DB.** Don't move ingestion into the auditor process. Don't add a second brain DB.
8. **Security posture stays fail-closed in prod** (auth, no `/docs`, SSRF guard). See CHANGELOG P1.
9. **Change-detection is cheapest-signal-first.** Never re-extract (LLM cost) a page whose content hash is unchanged.

---

## 7. Gotchas / landmines

- **`helium` / `heliumdb` is Replit-internal** — unreachable externally. The source-of-truth brain was moved via a one-time export, not a live connection. The live brain now lives in Railway `sieve`.
- **Supabase project `aldraxqsqeywluohskhs` is STALE** (older, smaller brain). The Replit/Railway `sieve` schema is authoritative. Don't re-point at Supabase.
- **`sieve.*` columns are all `text`** right now (permissive load). Cast where needed (`confidence_score::float`, `id::bigint`). The embedding load (P2) converts embedding cols to `vector`.
- **`sieve-ingest` is its own Railway project** (CLI created it separately), so `${{Postgres-pxlu.DATABASE_URL}}` references don't resolve there — it uses the direct **public** Postgres URL in `SIEVE_DB_URL`. (P4: move it into AEO-SEO-fable for internal networking.)
- **Railway CLI**: adding a service from a *new* GitHub repo needs browser auth (fails headless). Use `railway up` to deploy local code, or the dashboard.
- **First ingest cron run processes all 13 sources** (all "due"); it's bounded by `MAX_URLS_PER_SOURCE` (currently 5) to cap cost. Raise deliberately.
- **The auditor's agent path** streams tool-use; `pydantic` must be `2.13.4` (2.7.1 threw `by_alias/PyBool` on turn 2). Don't downgrade.

---

## 8. Run / test / deploy

**Auditor (local):** `cd service && pip install -r requirements.txt && ANTHROPIC_API_KEY=… SIEVE_LIVE=1 DATABASE_URL=<railway pg> uvicorn main:app --port 8000`. Tests: `bash tests/run_tests.sh`.
**Ingestion (local):** `cd sieve-ingest && SIEVE_DB_URL=<railway pg> ANTHROPIC_API_KEY=… python -m sieve_ingest seed && … status && … run`.
**Deploy:** both auto/CLI-deploy to Railway. Auditor auto-deploys from GitHub `main`. Ingestion: `railway up --service sieve-ingest` (or dashboard). Cron schedule is in `sieve-ingest/railway.json`.

**Definition of done for the next milestone:** Google + Perplexity docs ingest real rules with source URLs (P0); auditor cites them with `verified <date>` (P1); embeddings power semantic retrieval (P2). Ship P0 first, verify a live audit cites a fresh Google doc, then proceed.
