# tests/ — Fixture-driven verification

Stdlib-only test suite that verifies the auditor's behavior against
pathological cases that have produced wrong answers in real audits.

## Running

```bash
bash tests/run_tests.sh
```

Exit code `0` on full pass, non-zero on any failure.

## What it tests

| # | Fixture | Verifies |
|---|---|---|
| 1 | `empty_robots.txt` | Parser does not crash on empty input |
| 2 | `sitemap_with_entities.xml` | Real XML parser preserves `&amp;` and CDATA URLs correctly |
| 3 | `country_accordion_not_faq.html` | `<details>/<summary>` without question text does NOT count as FAQ |
| 4 | `real_faq_accordion.html` | `<details>/<summary>` with real questions DOES count as FAQ |
| 5 | `nextjs_streaming_hreflang.html` | hreflang inside `self.__next_f.push(...)` is detected |
| 6 | `spa_shell_same_as_404.html` | SPA shell (identical to 404 HTML) classifies as `spa_no_ssr` |
| 7 | `ssr_full_landing.html` | Full SSR landing (>500 words) classifies as `fully_accessible` |

12 assertions total across 7 fixtures. All must pass.

## Why fixtures instead of live-site tests

Live sites change underneath you. An audit tool's regression suite needs
deterministic local inputs. Each fixture in `fixtures/` is a minimal
reproducer of a real-world pattern that has caused wrong audit conclusions:

- `country_accordion_not_faq.html` — reproduces the feelvaleo.com /
  pattern that scored 4 false-positive FAQ pairs
- `nextjs_streaming_hreflang.html` — reproduces the Next.js App Router
  pattern that scored 0 false-negative hreflang locales
- `spa_shell_same_as_404.html` — reproduces the Angular/Vue SPA pattern
  that must trigger `spa_no_ssr` classification

## Adding a new fixture

1. Create a minimal file in `fixtures/` that reproduces the pattern
2. Add an expected-outcome comment at the top of the fixture OR a
   `<name>_notes.md` sibling file
3. Add an assertion block to `run_tests.sh`
4. Run `bash tests/run_tests.sh` to confirm the test infrastructure
   picks up the new case

## Dependencies

- bash
- python3 (3.8+)
- stdlib only — no pytest, no pip installs

This matches the auditor's own dependency profile.
