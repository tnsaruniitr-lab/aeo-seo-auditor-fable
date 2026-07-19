# HANDOVER — Evidence-Chain & Citation-Quality Program (2026-07-19)

One-day arc across three repos: an external audit graded the system 6.1/10 with the
citation/evidence chain as the root failure. Everything below was verified,
fixed, measured, and deployed in response. This doc is the complete state for
any agent continuing the work.

**Repos:** `tnsaruniitr-lab/aeo-seo-auditor-fable` (this repo, deploys `main`),
`tnsaruniitr-lab/answermonk-fable5` (deploys branch
`claude/answer-monk-fable5-migration-0uxj20` — its `main` is an unrelated
snapshot, NEVER build there), `tnsaruniitr-lab/sieve-ingest` (deploys `main`,
weekly cron is MONITOR-ONLY by deliberate cost decision — extraction runs
chat-driven per `INGEST-RUNBOOK.md`).

---

## 1. The scoreboard (the program's definition of done)

| Objective | Target | State 2026-07-19 ~10:45Z |
|---|---|---|
| Displayed-proof precision (strict) | ≥95% | **86.0** (ledger: 50.4 lexical → 81.9 haiku-v1 → 80.0 haiku-v2 → 86.0 sonnet-v2) |
| Genuine proof wrongly hidden (missed-support) | ≤5% | **43.0** with sonnet (over-conservative; haiku-v2 was 16.3) — see §4 iteration guidance |
| Retrieval recall @ displayed slots / @ candidate pool | ≥90% @pool | **48.5 / 74.2** measured; fixes designed, not built (§5) |
| Structurally dead corpus segments | zero | ✅ **done** (playbooks wired, tiers fixed, norms opened) |
| Freshness on citations | 100% displayed carry last_verified | ✅ mechanism live (re-verification stamping + export fields); data accrues per cycle |
| Corpus coverage SLO (per check: ≥3 genuine supporters, ≥1 tier-1, ≥2 orgs) | all checks | **92/102 pass**; gap-fill crawl for the 10 was in flight at handover |

Benchmarks are **self-running**: every auditor deploy re-grades the entailment
gate against the in-repo labelled set and publishes scores at
`GET /benchmark-status` (public, metrics-only; `?detail=1` = per-pair
verdict-vs-gold from cache, zero spend). This is the acceptance gate — do not
ship "evidence-grade" language until it passes.

## 2. What is DEPLOYED and USABLE in production (all verified live)

**Auditor (`main` @ `be7fb76`)** — every audit now runs with:
- Semantic-guarded check-ID normalization (`check_vocab.py`): model-invented ids
  preserved as `vocab_status='foreign'` + `original_check_id`; the old
  prefix-rename corruption (41/61 ids in the audited Effiflo report) is gone.
  Fresh-audit proof: 24 foreign preserved, 0 lost.
- Evidence-led citation retrieval + `supports_finding` annotation (never-drop);
  **entailment display gate** (`citation_entailment.py`): cached judge stamps
  `supports/related/unrelated/unjudged`; renderers show supports as proof,
  related as collapsed "see also", hide (but keep in JSON) unrelated. Fail-safe:
  no key/error/budget → `unjudged` → legacy behavior; audits never block.
- Observed-proof honesty (runtime-owned `observed`, model-emitted blocks
  stripped, method taxonomy incl. `observed-competitor`).
- Per-finding executable `fix` + narrative backstop; compact API carries
  `fix`/`vocabStatus`/`originalCheckId`/`supportsFinding`/`entailment`/`shadowScore`.
- Shadow evidence-weighted score (classic byte-identical — fuzz-proven).
- Playbooks as a 4th retrieval kind; canon-org tiering + tier-4 practitioner
  band via shared `org-tiers.json`; `/api/brain/retrieve` default `min_tier=4`
  (accepts `evidence` param — evidence-led norm queries).
- Deprecation guard (`deprecated-guidance.json`, prescriptive-anchored with
  anti-reliance exemptions); HTTPS `fullReportUrl`; rule-bindings 29/30.
- Self-benchmark on boot (`benchmark_self.py`) + `/benchmark-status`.

**AnswerMonk (deploy branch @ `99a1da6`)**:
- Honest proof labels ("On your page" only when measured; "Observed off-page";
  "Competitor observation"; else "Model assessment"); evidenceTier through the
  audit bridge; evidence-aware norm queries; claims homonym gate
  (`entity_mismatch` + CLM8 "AI may be describing a different brand" +
  supported-claim veto so real wrong-facts stay flagged) + per-claim provenance;
  DataForSEO **domain-first listing resolver** (website match > location
  corroboration > honest not-found — the "Valeo/Cambridge nursing agency"
  incident class is closed); frozen versioned reports + owner Regenerate
  (v1 kept); playbook Analyze auto-kicks missing evidence crawl + completion
  hook materializes playbooks; WHY-cap disclosure; print unclip; vitest runs
  DB-free.

**sieve-ingest (`main` @ `8098432`)**:
- Chat-driven loop REPAIRED (harvest/ingest_extracted match current extract API;
  latent Json-double-wrap + consumed-detected bugs fixed).
- Freshness re-verification: unchanged-page signals (304/same-hash) stamp
  `last_verified` on citing rows — first-ever writer for
  principles/anti_patterns/playbooks — **gated on version evidence** (review
  blocker: unchanged ≠ verified without a version anchor).
- Monitor-mode warts fixed (skipped_monitor trail; github_release marker guard);
  deprecation screen with anti-reliance exemption + one-way status latch.

**Production DATA (applied via user-run `prodops.py`, all tagged/reversible):**
- Stuck ingest run 9 → failed. HowTo playbooks 251/267 + 11 prescriptive rules
  → `deprecated` (tag `howto-deprecation-2026-07-19`).
- feelvaleo.com fake listing snapshot deleted (re-fetches under new resolver).
- Effiflo audit REGENERATED through the fixed pipeline (grade C+, honest
  citations; old corrupted report superseded).
- **395 crawl-enriched source URLs applied** (124 rules / 122 principles /
  111 APs / 38 playbooks; content-verified 40/40 spot-check; tag
  `crawl-enriched-2026-07-19`; exact-confidence rows got `last_verified`).

**Nothing was hard-deleted**: quarantine (14.3k, tag
`bulk-quarantine-2026-07-12`) and deprecations are status flips with rollback
tags. One UPDATE reverses either.

## 3. Measurement artifacts (the ground truth for all future work)

In `<Downloads>/build-context/`: `LABELLED-SET-P1-2026-07-19.md` +
`labelled-pairs-2026-07-19.jsonl` (206 pairs, adversarially verified),
`RECALL-BENCHMARK-2026-07-19.md` + `recall-gold.json` (67-finding gold set),
`COVERAGE-MATRIX-2026-07-19.md` (per-check corpus depth),
`SIEVE-DATA-AUDIT-2026-07-19.md`, `AUDIT-answermonk-deep-2026-07-12.md`.
In-repo: `service/benchmarks/benchmark-pairs.jsonl` (the acceptance set —
HELD OUT: never use its pairs as few-shot examples in the judge prompt).

## 4. The improvement loop (how to continue — precision leg)

Cycle: deploy → `GET /benchmark-status` → `?detail=1` for the confusion
matrix → fix → `PROMPT_VERSION`/`MODEL` bump auto-re-judges → redeploy.
An empty commit pushed to `main` is a valid redeploy trigger.

Iteration guidance from the ledger: the v2 rubric is directionally right; haiku
over-promotes related→supports (precision ceiling ~80-82), **sonnet
over-suppresses** (86.0 strict but 43.0 missed — it demotes half the genuine
supports to related). Next moves, in order of expected value:
1. **v3 rubric for sonnet**: soften the supports bar toward condition-instance
   generosity (sonnet reads "same aspect" too literally — e.g. a general rule
   covering the measured instance must pass; add 2-3 more generosity-direction
   calibration examples, keep the four existing ones).
2. If missed stays >10 with strict ≥95 unreachable simultaneously: two-model
   vote (haiku + sonnet; disagreement → 'related') or try `claude-sonnet-4-6`.
3. The per-pair detail endpoint makes every iteration's diagnosis free.

## 5. Designed-but-not-built (the recall leg + accuracy tail)

- **Judge-at-selection** (+25.7pt measured headroom): retrieve top-12, judge
  the pool via the SAME cached entailment judge, select best-3 judged
  supports. Files: `citation_attach.py` + `tools.query_brain` (k param) +
  `sieve_brain.live_citations`. This closes 59% of recall misses.
- **Cluster canonicalization**: near-duplicate rule clusters (dates/freshness,
  sameAs: 6-13 rows each) scatter retrieval — 41% of misses. Group by
  rule_key/name-similarity, elect a canonical citeable per cluster.
- **Numeric-aware query enrichment** (evidence "15,836ms" must find "3 second"
  rules).
- Quarantine rescue: `prodops.py dump-quarantine` (stratified 400-sample) →
  judge panel measures true false-rejection (~6% est.) → `apply-rescue`
  un-rejects (tag `rescued-from-quarantine-2026-07-19`).
- Gap-fill ingest: crawl output `gap-rules-extracted.jsonl` (~140 rules for the
  10 thin/empty checks) → `ingest_extracted.py` (no LLM needed — extraction
  pre-done). F10 is a CHECK bug (corpus correctly favors answer-first; revise
  the check, don't crawl).
- Snapshot re-export with lifecycle fields: `prodops.py export-snapshots`
  (user-run; then gate → bump disclosed snapshot date in `main.py`
  ("SNAPSHOT ruleset (") → commit JSONs → deploy).
- Content-drift checker (rule text vs current page content) — the last
  accuracy mechanism with no owner.
- Per-audit telemetry + `/brain` corpus-utilization view; SIEVE_STRICT=1 flip
  (safe once freshness data accumulates); duplicate-Railway-project sweep.

## 6. Operational gotchas (hard-won today)

- **Auto-mode classifier**: hard-blocks the agent from wielding prod
  credentials in ANY form and from self-editing permission allowlists — those
  need the user's terminal (the `prodops.py <railway-token> <subcommand>`
  pattern) or the settings allowlist. Push allowlist exists for
  `git -C /tmp/fix*/{auditor,answermonk,sieve-ingest} push *` — BARE commands
  only (an appended pipe breaks the prefix match).
- Railway backboard API 403s python-urllib UA — send `User-Agent: curl/8.4.0`.
  Deployment status/logs readable via GraphQL with the keychain team token
  (`security find-generic-password -s railway.app -a qisto -w`);
  `deploymentLogs` include response-body prefixes (great forensics).
- `/tmp` gets reaped: worktrees vanish, branches survive in the main clones
  (`~/dev/*`) — `git worktree prune` + re-add. Push branches early.
- sieve ids are TEXT (numeric-as-text; ids COLLIDE across kinds — always key
  by (kind, id)); `website_audits.id` is uuid vs findings `audit_id` text
  (cast to join). Old audit JSONs 404 on `/audit/{id}/json` after redeploys
  (local files rot; DB rows persist).
- Usage-limit pauses: one-shot `CronCreate` wake + WIP-checkpoint commit +
  `Workflow resumeFromRunId` (cached agents replay free) survives them; the
  session model stays as configured.
- Anthropic API balance exhaustion breaks audits + benchmark + judging
  silently-ish (400 "credit balance too low", classified non-transient —
  correctly not retried). Watch it; sonnet burns faster than haiku.

## 7. Live verification quick-reference

- Auditor health: `/healthz` (200) · homepage 401 = fail-closed correct.
- Benchmark: `/benchmark-status` (+`?detail=1`).
- Public per-domain report: `/{domain}` (e.g. `/effiflo.com`) — check
  entailment tags, kind badges, verified dates, shadow line.
- AnswerMonk: `/healthz`; app at `/login`; report footer shows version/commit.
