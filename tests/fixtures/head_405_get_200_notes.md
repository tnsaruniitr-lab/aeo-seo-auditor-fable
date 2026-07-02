# Fixture: HEAD 405 / GET 200

## What this tests

Some servers (Cloudflare Workers, nginx with some configs, older PHP apps) return
HTTP 405 Method Not Allowed or 501 Not Implemented for `HEAD` requests, even
when `GET` against the same URL returns 200 OK.

The current `check_sitemap.py::check_url_probe` uses `curl -I` (HEAD-only) and
marks such URLs as **broken** — a false positive that degrades the audit grade.

## How to simulate

Set up a minimal test server (e.g., via `python3 -m http.server` is NOT useful
because it does allow HEAD). Instead, use a recorded response pattern:

```python
# Server configuration that reproduces the bug
HEAD /some-page → 405 "Method Not Allowed"
GET  /some-page → 200 "<html>...valid content...</html>"
```

## Expected audit behavior (after fix)

1. First attempt: `HEAD /some-page`
2. Response: 405
3. Fallback: `GET /some-page` with `Range: bytes=0-0` (fetches 1 byte)
4. Response: 200
5. Check result: **pass** — URL is reachable

## Real-world examples encountered during audits

- `trypsagent.com/blog/*` on some Cloudflare edge POPs (intermittent)
- Static sites behind Netlify with certain _redirects configs
- WordPress installations behind CDN-level security rules
