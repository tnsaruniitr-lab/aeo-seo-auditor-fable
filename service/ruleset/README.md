# auditor-ruleset-export/

Portable snapshot of the technical audit ruleset — ready to drop into another project (blog auditor, content QA tool, pre-publish pipeline, etc.).

**Version:** 1.0
**Exported:** 2026-04-21
**Source:** website-seo-aeo-auditor v3 (unified build)

## Contents

```
auditor-ruleset-export/
├── README.md                         ← this file (quick start)
├── technical-audit-ruleset-v1.md    ← integration guide + truth badges + citation tiers
├── schema-specs.json                ← 37 @types with required/google_required/recommended fields
├── brain-mappings.json              ← check-ID → Sieve rule IDs + truth badges + source tiers
└── validators.py                    ← pure-function library (stdlib only, 12 functions)
```

Total: ~45 KB. Zero pip dependencies.

## Download

**Option 1 — clone the whole repo:**
```bash
git clone https://github.com/tnsaruniitr-lab/aeo-seo-auditor
cd aeo-seo-auditor/auditor-ruleset-export
```

**Option 2 — just this directory via GitHub's CLI:**
```bash
gh repo clone tnsaruniitr-lab/aeo-seo-auditor -- --depth=1
cp -r aeo-seo-auditor/auditor-ruleset-export /your/blog-auditor/lib/
```

**Option 3 — individual file downloads from GitHub:**
- https://github.com/tnsaruniitr-lab/aeo-seo-auditor/blob/main/auditor-ruleset-export/schema-specs.json
- https://github.com/tnsaruniitr-lab/aeo-seo-auditor/blob/main/auditor-ruleset-export/brain-mappings.json
- https://github.com/tnsaruniitr-lab/aeo-seo-auditor/blob/main/auditor-ruleset-export/validators.py
- https://github.com/tnsaruniitr-lab/aeo-seo-auditor/blob/main/auditor-ruleset-export/technical-audit-ruleset-v1.md

## 60-second test

```bash
cd auditor-ruleset-export
python3 validators.py
# Expected: "validators.py self-tests passed."
```

If that prints, every exported function works in your environment.

## What goes where in your consumer project

| Your project | Put it here | Why |
|---|---|---|
| `lib/` or `vendor/` | `validators.py` | Import functions directly |
| `data/` or `config/` | `schema-specs.json` + `brain-mappings.json` | Runtime-loaded data |
| `docs/` | `technical-audit-ruleset-v1.md` | Reference for your team |

## Next steps

1. Read `technical-audit-ruleset-v1.md` end-to-end (10 min) — it has the integration cookbook + 4 code recipes
2. Run `python3 validators.py` to confirm the library imports cleanly
3. Paste `schema-specs.json` into your LLM prompt if using LLM-as-auditor
4. Build your blog-specific Layer 4 (content-quality rubrics) on top

## Questions to check before you start

- **Am I going to fork this?** Don't. Import as data + library, never duplicate.
- **Can I add new schema types?** Yes — extend `schema-specs.json`. Push back to source if generally useful.
- **Where do brain rule IDs come from?** Sieve project `aldraxqsqeywluohskhs`. You need your own Sieve access to fetch full rule text.
- **What if I don't have Sieve?** Findings still work; they just won't have Sieve citations. Use the static criteria from the specs alone.

## Related

Full website auditor (not included here, lives in the parent directory):
- https://github.com/tnsaruniitr-lab/aeo-seo-auditor/tree/main/skill-unified

Full audit reports (examples of the output shape):
- https://github.com/tnsaruniitr-lab/aeo-seo-auditor/tree/main/audit-reports
