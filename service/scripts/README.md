# Auditor Scripts — Unified canonical build

This is the **single source of truth** for the auditor scripts with all
v2 correctness fixes baked in. It replaces both the original `skill/scripts/`
and the parallel `scripts-v2/` — don't maintain those separately, use this.

## Files

| File | Status | Notes |
|---|---|---|
| `bots_eye_view.sh` | Original, unchanged | Bash wrapper; delegates parsing to `_bev_analyze.py` |
| `_bev_analyze.py` | **Fixed** | FAQ question-intent gate + `ssr_shell_js_hidden_content` classification + stdlib html.parser text extraction |
| `deterministic_checks.py` | Original, unchanged | 9 targeted checks |
| `deterministic_checks_extras.py` | **New** | Hreflang detector (sees Next.js streaming) + safe URL helpers |
| `check_robots_txt.py` | **Fixed** | Empty-body tolerance + explicit HTTP 4xx/5xx handling |
| `check_sitemap.py` | **Fixed** | Real XML parser (xml.etree.ElementTree) + HEAD-with-GET fallback |
| `check_schema_completeness.py` | Original, unchanged | 28 @type validator |
| `run_deterministic.sh` | **Fixed** | Per-PID wait (one child failure can't kill the orchestrator) + per-child timeout + child-health reporting |

## Usage

```bash
# Full 5-script orchestrator (human-readable)
bash scripts/run_deterministic.sh https://example.com human

# Full orchestrator (JSON)
bash scripts/run_deterministic.sh https://example.com > audit.json

# Individual scripts
bash scripts/bots_eye_view.sh https://example.com
python3 scripts/deterministic_checks.py https://example.com
python3 scripts/check_robots_txt.py https://example.com
python3 scripts/check_sitemap.py https://example.com
python3 scripts/check_schema_completeness.py https://example.com
python3 scripts/_bev_analyze.py --selftest
python3 scripts/deterministic_checks_extras.py --selftest
```

## Fixes baked in (vs original skill/scripts/)

1. **FAQ false-positive gate** — `<details>/<summary>` pairs only count when summary looks like a question (contains `?` or starts with a question word). Country expanders and nav toggles no longer register as FAQs.
2. **Real XML parser** — sitemap parsing uses `xml.etree.ElementTree` instead of regex. URLs with `&amp;` entities and CDATA no longer corrupt.
3. **HEAD-with-GET fallback** — URL reachability probes fall back to GET (with Range header) when a server returns 405/501 for HEAD. No more false-fail on CDN-fronted sites.
4. **Per-PID wait in orchestrator** — one failed child script no longer kills the whole audit. Every child's exit code is captured; `overall_summary.child_health` surfaces degraded runs.
5. **Per-child timeout** — each child wrapped in `timeout 60s`; a hung child cannot block the orchestrator forever.
6. **Empty/error robots.txt tolerance** — empty body returns an empty structure, not a crash. HTTP 4xx/5xx is explicit: `robots_reachable` → FAIL, downstream checks → WARN (not fake PASS).
7. **Hreflang detector sees Next.js streaming** — `deterministic_checks_extras.py::detect_hreflang` scans both top-level `<link>` tags and `self.__next_f.push(...)` chunks. No more false "0 hreflang" on App Router sites.
8. **`ssr_shell_js_hidden_content` classification** — new verdict when a thin SSR modal/gate fronts a JS-rendered landing page. Actionable diagnosis instead of generic "minimal_content".
9. **URL interpolation safety** — all subprocess calls pass URLs as separate argv entries; `safe_url_for_shell` helper available for rare single-string cases.

## Dependencies

- `curl` (any version)
- `python3` (3.8+)
- Standard Unix tools: `grep`, `wc`, `mktemp`, `timeout`
- **No external Python packages.** stdlib only.

## Verification

Run the fixture test suite from the repo root:

```bash
bash tests/run_tests.sh
```

Expected: `12 passed, 0 failed`.

The fixtures reproduce pathological patterns that produced wrong answers in
real audits — if any fixture fails, the script behavior has regressed against
the documented fixes.

## Adopting as your live skill

Backup your current live skill, then replace:

```bash
# Backup
cp -R ~/Documents/New\ project/.claude/skills/website-seo-aeo-auditor \
      ~/Desktop/website-seo-aeo-auditor-backup-$(date +%Y%m%d)

# Replace with unified build
rm -rf ~/Documents/New\ project/.claude/skills/website-seo-aeo-auditor
cp -R ~/Desktop/aeo-seo-auditor/skill-unified \
      ~/Documents/New\ project/.claude/skills/website-seo-aeo-auditor
```

Next audit run uses the fixed scripts. No other change needed.
