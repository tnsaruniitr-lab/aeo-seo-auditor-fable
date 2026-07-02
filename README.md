# AEO + SEO + GEO Website Auditor (`aeo-seo-auditor-fable`)

A website auditor for SEO, **AEO** (Answer Engine Optimization), and **GEO**
(Generative Engine Optimization). It fetches a URL, runs a deterministic
inspection suite, has an LLM agent classify ~100 checks with evidence, and then
**grades the result deterministically in Python** — the score is a reproducible
function of the classified checks, not model arithmetic.

This is a hardened rebuild of the original `aeo-seo-auditor`, addressing a
multi-dimensional audit (engineering, scoring validity, determinism, security,
product). See [`CHANGELOG-FABLE.md`](CHANGELOG-FABLE.md) for the finding-by-finding
list of what changed and why.

## Trust model — what's deterministic vs not

Two layers, and it matters which is which:

| Layer | Trustworthy? | Why |
|---|---|---|
| **Deterministic scripts** (robots precedence, sitemap sampling, schema completeness, FAQ match, Bot's-Eye-View) | **Yes** | Pure functions of the HTML/HTTP input; byte-stable; fixture-tested. |
| **Per-check status** (pass/warn/fail/na) | Model-assigned | The LLM classifies each check from evidence. This is the only place model judgment enters the score. |
| **The grade** (section scores, PCR, BAP, overall, letter grade) | **Yes — recomputed in Python** | `scoring.py` is the single source of truth for weights + grade cutoffs. It overwrites whatever the model reported and clamps/enum-guards every value before persist/render. |

So: **the LLM classifies, Python grades.** The same set of check statuses always
produces the same grade. (The old build let the model compute the headline number
in its head and persisted it verbatim — that's fixed.)

- **PCR** (Page Citation Readiness) is the deterministic, page-fixable headline; the letter grade is derived from it.
- **BAP** (Brand AI Presence) is a directional GEO signal, reported **separately** with a confidence tag, never folded into the letter grade.

## Repository layout

```
.
├── service/                  # the FastAPI service (single implementation)
│   ├── main.py               # routes, auth, rendering, persistence wiring
│   ├── agent.py              # Claude agent loop (classify → runtime grades)
│   ├── scoring.py            # ★ single source of truth: weights, grades, recompute
│   ├── audit_pipeline.py     # legacy deterministic path + renderers (delegates to scoring)
│   ├── tools.py              # agent tools (SSRF-guarded fetch path)
│   ├── safety.py             # SSRF guard (private/loopback/metadata rejection)
│   ├── system_prompt.py      # agent playbook (told: you classify, runtime grades)
│   ├── persistence.py        # durable job status + report regeneration
│   ├── delta.py              # ★ fix-verification / re-score / delta engine
│   ├── billing.py            # per-key metering + quota + Stripe scaffold
│   ├── monitoring.py         # structured metrics (audit health / cost / drift)
│   ├── scripts/              # deterministic inspectors (bash + stdlib Python)
│   ├── ruleset/              # Sieve brain snapshots + ranker
│   ├── references/           # on-demand knowledge files for the agent
│   └── migrations/           # SQL for durable jobs / usage / suppression tables
├── tests/                    # fixture + logic tests (run in CI)
├── Dockerfile → service/Dockerfile
└── railway.json              # Railway deploy config
```

## Quick start (local)

```bash
cd service
pip install -r requirements.txt
python -m playwright install chromium         # for JS render + PDF
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn main:app --reload --port 8000
# POST an audit:
curl -X POST localhost:8000/audit -H 'content-type: application/json' \
     -d '{"url":"https://example.com"}'
```

Run the deterministic scripts standalone (no LLM, no key needed):

```bash
cd service/scripts
bash run_deterministic.sh https://example.com human
```

## Tests

```bash
bash tests/run_tests.sh      # bev selftest, xml parse, scoring, delta, py_compile
```

CI (`.github/workflows/ci.yml`) runs this plus an import smoke test on every push/PR.

## The product loop (re-score / delta)

After a customer applies fixes, re-audit and diff:

```
GET /api/audit/{audit_id}/delta            # vs the most recent prior audit
GET /api/audit/{audit_id}/delta?vs={id}    # vs a specific prior audit
```

Returns `{resolved, regressed, new_issues, persisting, score_delta}` — the ROI
artifact ("62 → 81, 7 resolved, 1 regressed"). Pure data, no LLM call.

## Deploy (Railway)

`railway.json` points at `service/Dockerfile`. Set the env vars from
[`service/.env.example`](service/.env.example) in the Railway dashboard, and apply
[`service/migrations/001_durability_and_billing.sql`](service/migrations/001_durability_and_billing.sql)
to your Supabase project. In production the service **fails closed**: it refuses
internal/expensive routes unless `AUDIT_USERNAME`/`AUDIT_PASSWORD`/`AUDIT_API_KEY`
are set, and disables `/docs`.

## Sieve brain integration

The ranker runs against **snapshotted** brain data in `service/ruleset/` (fast,
reproducible, no per-audit dependency). Refresh the snapshots out-of-band with a
periodic re-export from the Sieve Supabase project rather than querying live at
audit time.
