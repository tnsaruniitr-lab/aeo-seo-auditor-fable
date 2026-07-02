#!/usr/bin/env bash
# bots_eye_view.sh — Deterministic bot visibility check
#
# Usage: bash bots_eye_view.sh <URL>
# Output: JSON to stdout with everything AI crawlers see about the page.
#
# This is the single most important crawlability diagnostic. It answers:
#   1. What do GPTBot / PerplexityBot / ClaudeBot / Googlebot actually receive?
#   2. Does the server serve the same empty shell for every URL? (SPA-no-SSR detector)
#   3. Is the site cloaking (different content per user-agent)?
#   4. Is the content JS-rendered or actually in the raw HTML?
#   5. Are bot UAs blocked (401/403/429) while browsers get content?
#
# Designed to produce identical output across runs when the underlying page is unchanged.

set -euo pipefail

if [ "${1:-}" = "" ]; then
  echo '{"error":"missing URL argument","usage":"bash bots_eye_view.sh https://example.com/path"}'
  exit 1
fi

URL="$1"

# Default to https:// when no scheme is given, so curl doesn't misread the
# input and the ORIGIN parse below always matches. Scheme match is
# case-insensitive (HTTP://x.com must not become https://HTTP://x.com).
case "$(printf '%s' "$URL" | tr '[:upper:]' '[:lower:]')" in
  http://*|https://*) ;;
  *) URL="https://${URL}" ;;
esac

# SSRF pre-flight: refuse internal/metadata targets before any curl runs.
# Uses the canonical guard in service/safety.py (one dir above scripts/).
# The BEV probes below add curl's own --location-trusted-off default and
# --max-redirs cap, but the authoritative allow/deny is this check.
SAFETY_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SAFETY_CHECK=$(SAFETY_DIR="$SAFETY_DIR" python3 - "$URL" <<'PYEOF' 2>/dev/null || true
import os, sys
sys.path.insert(0, os.environ.get('SAFETY_DIR', ''))
try:
    from safety import check_url_safe
except Exception:
    print('OK'); sys.exit(0)   # fallback: keep standalone behavior
ok, reason = check_url_safe(sys.argv[1])
print('OK' if ok else f'BLOCK {reason}')
PYEOF
)
if [ "${SAFETY_CHECK%% *}" = "BLOCK" ]; then
  reason="${SAFETY_CHECK#BLOCK }"
  printf '{"error":"url refused by SSRF guard","reason":%s,"url":%s}\n' \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$reason")" \
    "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$URL")"
  exit 0
fi

TMPDIR="${TMPDIR:-/tmp}"
RUNID="$(date +%s)_$$"
PREFIX="${TMPDIR}/bev_${RUNID}"

# Clean up probe bodies on any exit path — under `set -e` a failing analyzer
# would otherwise skip an rm placed at the bottom of the script.
trap 'rm -f "${PREFIX}"_*.html' EXIT

# Parse URL for origin + a guaranteed-404 path on the same origin
ORIGIN=$(printf '%s' "$URL" | sed -E 's|(https?://[^/]+).*|\1|')
NONEXISTENT_PATH="/nonexistent-probe-$(date +%s)-$$"
NONEXISTENT_URL="${ORIGIN}${NONEXISTENT_PATH}"

# Four UA probes + one 404 probe
# UAs cover: default, Googlebot, GPTBot, PerplexityBot, ClaudeBot
#
# -L: real crawlers (Googlebot, GPTBot, ClaudeBot, PerplexityBot) all follow
# 3xx. Without it, an http:// input or non-canonical host yields an empty
# 308 body that downstream classification misreads as an empty SPA shell
# (somana.com false positive, 2026-06). --max-redirs caps loop risk; the
# analyzer reports a final 3xx code as 'unresolved_redirect', not content.
# --compressed: send Accept-Encoding and decode, so gzip-only servers don't
# hand us binary. -w captures the FINAL hop's code plus the effective URL so
# the analyzer can detect per-UA redirect divergence.
fetch() {
  local ua="$1"; local out="$2"; local url="$3"
  local res
  res=$(curl -sS --max-time 20 -L --max-redirs 5 --compressed \
    -o "$out" \
    -w "%{http_code} %{size_download} %{time_starttransfer} %{num_redirects} %{url_effective}" \
    -H "User-Agent: $ua" \
    -H "Cache-Control: no-cache" \
    "$url" 2>/dev/null) || true
  # curl writes the -w line even when it fails mid-transfer — a redirect
  # loop exits 47 but still reports '301 ... 5 <url>', which the analyzer
  # turns into 'unresolved_redirect'. Only synthesize the failure sentinel
  # when curl wrote nothing at all (DNS failure, no connection).
  [ -n "$res" ] || res="000 0 0 0 -"
  printf '%s\n' "$res"
}

# Run all probes
DEFAULT_UA="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
GBOT_UA="Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
GPT_UA="Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.0; +https://openai.com/gptbot)"
PERP_UA="Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; +https://perplexity.ai/perplexitybot)"
CLAUDE_UA="Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)"

DEFAULT_RESULT=$(fetch "$DEFAULT_UA" "${PREFIX}_default.html" "$URL")
GBOT_RESULT=$(fetch "$GBOT_UA" "${PREFIX}_gbot.html" "$URL")
GPT_RESULT=$(fetch "$GPT_UA" "${PREFIX}_gpt.html" "$URL")
PERP_RESULT=$(fetch "$PERP_UA" "${PREFIX}_perp.html" "$URL")
CLAUDE_RESULT=$(fetch "$CLAUDE_UA" "${PREFIX}_claude.html" "$URL")
NE_RESULT=$(fetch "$DEFAULT_UA" "${PREFIX}_404.html" "$NONEXISTENT_URL")

# Delegate all parsing + classification to Python. Contract: JSON on stdin,
# JSON analysis on stdout. We build the stdin payload with a tiny inline
# python3 script to avoid shell-escaping bugs on html paths / curl results.
PAYLOAD=$(python3 - "$URL" "$NONEXISTENT_URL" \
  "${PREFIX}_default.html" "$DEFAULT_RESULT" \
  "${PREFIX}_gbot.html" "$GBOT_RESULT" \
  "${PREFIX}_gpt.html" "$GPT_RESULT" \
  "${PREFIX}_perp.html" "$PERP_RESULT" \
  "${PREFIX}_claude.html" "$CLAUDE_RESULT" \
  "${PREFIX}_404.html" "$NE_RESULT" <<'PYEOF'
import json, sys
a = sys.argv[1:]
url, probe_url = a[0], a[1]
rest = a[2:]
keys = ['default', 'gbot', 'gpt', 'perp', 'claude', 'not_found']
probes = {k: {'html_file': rest[i*2], 'curl_result': rest[i*2 + 1]}
          for i, k in enumerate(keys)}
sys.stdout.write(json.dumps({'url': url, 'probe_url': probe_url, 'probes': probes}))
PYEOF
)

# Last command's exit status is the script's; trap above handles cleanup.
printf '%s' "$PAYLOAD" | python3 "$(dirname "$0")/_bev_analyze.py"
