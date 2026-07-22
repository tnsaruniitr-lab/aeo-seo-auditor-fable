# LIGHT Audit Profile — Tier-0 Gates + 8 Deterministic Factors

Added on `feat/light-profile-competitors`. A per-request audit profile that is
**100% deterministic** (fetch + parse only — zero LLM calls, zero Anthropic
cost, ~45s) next to the unchanged full agent profile.

## What a light audit runs

| Stage | Implementation | Reused / new |
|---|---|---|
| Tier-0 fetchability gates | `scripts/bots_eye_view.sh` (multi-UA curl probe, 404-shell comparison, cloaking/bot-blocking) | reused as-is |
| 1. AI-bot robots access | `scripts/check_robots_txt.py` + new additive `per_bot_access()` / `ai_bot_access` output, mapped onto the existing canonical measured ids `E1/E2/E3/E10/E13/A10/A11` | reused + additive |
| 2. llms.txt presence | `E14_llms_txt` (canonical id; verdict bands delegated to `deterministic_checks.classify_llms_txt_response`, HTML catch-all/soft-200 guarded; 5xx and fetch-failure are `na`, never asserted absent) | existing id, shared classifier |
| 3. Raw-HTML depth | `E5b_raw_html_depth` — BEV word-count thresholds as a measured verdict | **new** (deterministic variant of LLM `E5`) |
| 4. LocalBusiness + Geo JSON-LD | `D15_localbusiness_geo_schema` — reuses the schema validator's entity extraction | **new** |
| 5. City in title/H1 | `C13_city_in_title_h1` — city from the request (`city` param) or derived from JSON-LD `addressLocality` | **new** (deterministic variant) |
| 6. FAQ content/schema | `F3b_faq_content_present` (**new**, variant of LLM `F3`) + `D9_faqpage_schema_vs_visible` (existing measured base, computed from the shared `_bev_analyze` primitives) | new + reused |
| 7. Question-form headings | `F6b_question_headings` — `looks_like_question` over H2/H3 | **new** (deterministic variant of LLM `F6`) |
| 8. Prices on page | `F8b_prices_visible` — visible currency regex + Offer/priceRange schema cross-check | **new** (deterministic variant of LLM `F8`) |

Deterministic variants of LLM-classified checks use **new base ids** on
purpose (adding e.g. `F6` to `scoring.MEASURED_CHECK_BASES` would have
re-stamped full-profile model verdicts as measured) and carry
`factor_variant: "deterministic"` on the finding.

### Threshold provenance (E5b, F6b)

- **`E5b_raw_html_depth` deliberately uses the BEV SSR-classification bands
  (fail <200 / warn 200–500 / pass >500 visible words), NOT the study's
  citation-depth bands** (Tier-0 gate ≈300 words; Tier-1 depth lever
  ≈2,000 words). E5b answers "is the content server-rendered at all?" — an
  E5b `pass` does **not** mean the study's depth lever is met. The finding
  stamps `detail.threshold_basis: "bev_ssr_classification"` so downstream
  consumers can't misread it. The *measurement* (raw pre-JS HTML, no render)
  matches the study exactly.
- **`F6b_question_headings` passes at ≥2 interrogative headings**, matching
  the study playbook's 15-weight lever (cited-winner median = 2). One
  question heading is a warn, zero is a fail; a ratio ≥0.3 is an additional
  pass route for short pages. Counting scope: the study counted H1+H2, F6b
  counts H2/H3 (H1 is a single title slot; H2/H3 is where answer-shaped
  structure lives).

**Skipped by design**: Playwright render (LCP/CLS), all web_search stages
(company context, competitor discovery, GEO presence), competitor crawl,
AI-visibility sweep, Sieve citations (no live-brain dependency), entailment,
fix generation, and every LLM-classified check. Narrative is a deterministic
template; `narrative.top_5_fixes` is always `[]`.

**Fetch stack**: only the existing SSRF-guarded fetchers are used —
`bots_eye_view.sh` (safety pre-flight), `check_robots_txt.fetch_robots`, and
`check_schema_completeness.fetch_html` (per-redirect-hop `safety.check_url_safe`).
No new fetcher was written.

## API

### Single audit
`POST /api/audit/start` (X-API-Key) — new optional fields, all backward
compatible (a request without `profile` behaves exactly as before):

```json
{
  "url": "https://competitor.example",
  "profile": "light",            // 'full' (default) | 'light'
  "target": "competitor",        // 'brand' (default) | 'competitor'
  "sessionRef": "scan-session-123",  // opaque passthrough
  "city": "Berlin",              // optional; else derived from JSON-LD address
  "webhookUrl": "https://..."    // unchanged
}
```

Response `estimatedSeconds` is profile-aware (45 for light vs 360 full);
`/api/audit/{id}/status` returns the per-job ETA and stamps
`profile`/`target` for non-default runs. Progress ramps against the per-job
ETA (a light run no longer crawls at ~10x slow motion).

### Batch (light only)
`POST /api/audit/start-batch` — up to **4 domains** per request:

```json
{ "urls": ["a.com", "b.com", "c.com", "d.com"],
  "target": "competitor", "sessionRef": "scan-session-123" }
```

**Architecture choice**: the pipeline is strictly single-domain (one
`audit_id` / scoring block / persisted row / AnswerMonk upsert per URL), so
batch is a thin loop spawning up to 4 independent light jobs. Per-URL failures
(SSRF-rejected, suppressed, quota) are reported per entry and never abort the
rest.

**Execution model**: accepted batch entries are submitted to the dedicated
light executor (`ThreadPoolExecutor` sized `MAX_CONCURRENT_LIGHT_AUDITS`)
and run **in parallel** — not via starlette `BackgroundTasks`, which awaits
its tasks strictly sequentially per request and would serialize a 4-URL
batch to ~4× the ETA. `estimatedSeconds` is therefore the expected wall
clock for the whole batch (assuming free pool slots), not per entry.

**Webhook fan-out**: a shared `webhookUrl` on a batch fires **one webhook
POST per URL** (up to 4 calls), each as its audit settles — there is no
aggregated batch webhook. Callers should key on `auditId` in the payload.

## Semantics & hooks

- **Idempotency** is keyed by URL **+ profile + target** — a light request
  within 60s of a full one no longer collides. The Supabase fallback applies
  only to `full`/`brand` (persisted recents carry no profile column).
- **Concurrency**: light runs have their own pool
  (`MAX_CONCURRENT_LIGHT_AUDITS`, default 8) — they never consume the 3
  chromium-weight full-audit slots. Admission (the 429 cap) and execution
  use the same number: light jobs run on a dedicated `ThreadPoolExecutor`
  of that size, so up to 8 light audits genuinely execute concurrently
  (full audits keep the per-request `BackgroundTasks` path).
- **Degraded-BEV gates**: if `bots_eye_view.sh` fails/times out but the
  direct page fetch succeeds, the `content_access` gate is derived from the
  fetched bytes with the same BEV word-count bands (≥500 fully_accessible /
  200–500 partial_ssr / <200 minimal_content) instead of defaulting to
  `fail` — a probe-infrastructure failure must not read as a content
  failure. `gates.details.bev_degraded: true` marks such runs.
- **Full-profile tag durability**: `target`/`sessionRef` on a
  `profile: "full"` request are stamped into result metadata only after the
  agent loop returns, but the agent persists to Supabase mid-run — so the
  **Supabase row of a full audit carries no target/session_ref**. The
  AnswerMonk ingest record IS re-posted with the tags after completion
  (the receiver upserts by `audit_id`, so the tagged POST overwrites the
  untagged one). On `profile: "light"` both channels carry the tags from
  the start. Status/compact/webhook consumers see the tags on both profiles.
- **Billing**: metered **neutrally** (1 unit, same quota) — the `api_usage`
  table is `(key_id, month, count)` with no per-profile shape. The decision
  and log line carry `profile` so a future meter can price light separately.
- **Monitoring**: `audit.started` / `audit.completed` metric lines carry
  `profile` (and `target`) for light runs; full-run metric lines unchanged.
- **Scoring**: `scoring.compute_from_findings` verbatim — absent sections
  renormalize natively. The audit is stamped `profile: "light"` (top level +
  `metadata.profile`) so a light PCR/grade is never compared like-for-like
  with a full audit (WinnerCompare mixed-universe lesson). All light findings
  are `evidence_tier: measured` with producer-owned `observed{}` blocks, so
  the shadow score covers 100% of them.
- **Persistence**: same `website_audits` row; `audit_mode` column carries
  `light-1.0` (from `metadata.version`).
- **AnswerMonk ingest**: `persistence.post_to_answermonk` fires for light
  audits too (the deterministic legacy path never called it; the light path
  does explicitly). `profile` / `target` / `session_ref` ride inside the
  ingest schema's optional `metadata` record — strictly conditional, so a
  default full-profile payload is byte-identical to before. Competitor
  audits are stored under the competitor's own domain (`brand_domain` key),
  which keeps them out of the brand's scorecard-pin path on the receiver.
- **Artifacts**: JSON always; Markdown best-effort via the shared renderer;
  no PDF (full-profile artifact).

## Tests

`tests/test_light_profile.py` (suite step [12], sentinel `LIGHT_PROFILE_OK`):
per-check units over saved-bytes fixtures (`tests/fixtures/light_rich.html`,
`light_bare.html` — no live network), llms.txt catch-all guard, per-bot robots
mapping, full pipeline via injected fetchers, transport-inconclusive path,
AnswerMonk payload shape for `target: competitor` + full-payload back-compat,
measured-tier vocabulary assertions, API model validation, idempotency key
separation.
