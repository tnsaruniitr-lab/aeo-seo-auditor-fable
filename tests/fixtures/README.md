# Test Fixtures

Deterministic local inputs for verifying the auditor's behavior against known
pathological cases that have produced wrong answers in real audits.

## Fixture index

| File | What it tests | Which script consumes it |
|---|---|---|
| `empty_robots.txt` | Empty-body HTTP 200 response — must not crash the parser | `check_robots_txt.py` |
| `robots_403_response.json` | HTTP 403 fetch — parser must degrade gracefully, not report a fake Allow list | `check_robots_txt.py` |
| `sitemap_with_entities.xml` | Sitemap with `&amp;`, `CDATA`, and namespace declarations — regex parser corrupts URLs, XML parser doesn't | `check_sitemap.py` |
| `head_405_get_200_notes.md` | Describes HEAD-405 / GET-200 scenario — requires HTTP fixture server to test at runtime | `check_sitemap.py` |
| `country_accordion_not_faq.html` | 4 `<details>/<summary>` blocks that are country expanders — must count as 0 FAQ pairs (current code returns 4) | `_bev_analyze.py` |
| `real_faq_accordion.html` | 6 `<details>/<summary>` blocks that are real Q&As — must count as 6 FAQ pairs | `_bev_analyze.py` |
| `nextjs_streaming_hreflang.html` | 9 hreflang entries encoded in `self.__next_f.push` streaming — current code reports 0 | `deterministic_checks.py` (new A8b check) |
| `spa_shell_same_as_404.html` | Bare SPA shell — if returned for both real URL and 404 URL, classification must be `spa_no_ssr` | `_bev_analyze.py` |
| `ssr_full_landing.html` | Complete SSR landing page — classification must be `fully_accessible`, all schema checks pass | `_bev_analyze.py` + all validators |

## How to run

```bash
# From repo root
bash tests/run_tests.sh
```

See `run_tests.sh` for the assertion logic. Each fixture has an expected
outcome documented either in a `.md` sibling file or as inline comments
within the fixture itself.

## Adding a new fixture

1. Drop a new file (`.html`, `.xml`, `.txt`, `.json`) in `tests/fixtures/`
2. Document its expected outcome in a comment or a `_notes.md` file
3. Add an assertion to `tests/run_tests.sh`
