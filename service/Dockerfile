# Dockerfile for the AEO/SEO/GEO audit service (aeo-seo-auditor-fable).
# Clean single-implementation layout: everything the service needs lives under
# service/ (code + scripts/ + ruleset/ + references/). Build context = repo root.
# Used by Railway, Fly.io, Cloud Run, or any Docker-compatible host.

FROM python:3.11-slim

# System deps:
#   curl + bash for the deterministic audit scripts
#   chromium + fonts-liberation for PDF rendering
#   Playwright system deps (libnss3, libxkbcommon, etc.) for render_page_js
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    bash \
    chromium \
    fonts-liberation \
    libnss3 \
    libnspr4 \
    libxkbcommon0 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libpango-1.0-0 \
    libcairo2 \
    libcups2 \
    libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

# Tell the auditor where Chrome lives (the PDF renderer honors CHROME_PATH; the
# old hardcoded macOS path meant every /pdf 404'd in prod — SD-6).
ENV CHROME_PATH=/usr/bin/chromium

WORKDIR /app

# Requirements first for layer caching.
COPY service/requirements.txt /app/service/requirements.txt
RUN pip install --no-cache-dir -r /app/service/requirements.txt

# Playwright's bundled Chromium (used by render_page_js + the PDF fallback).
RUN python -m playwright install chromium && \
    python -m playwright install-deps chromium 2>/dev/null || true

# Copy the service (code + scripts/ + ruleset/ + references/) as one unit.
COPY service /app/service

# Make the deterministic scripts executable.
RUN chmod +x /app/service/scripts/*.sh /app/service/scripts/*.py 2>/dev/null || true

# Local artifact cache. Reports are also persisted to Supabase and regenerated
# on demand, so this dir is a cache, not the source of truth (redeploy-safe).
# Mount a volume here in production if you want warm local artifacts.
RUN mkdir -p /app/service/audits
ENV AUDIT_OUTPUT_DIR=/app/service/audits

# Default audit mode: "agent" (full parity) or "deterministic" (legacy fast path).
ENV AUDIT_MODE=agent

# Fail auth CLOSED in production (require AUDIT_USERNAME/PASSWORD + AUDIT_API_KEY).
ENV AUDITOR_FAIL_CLOSED=1

# Railway sets PORT automatically; default to 8000 locally.
ENV PORT=8000
EXPOSE 8000

WORKDIR /app/service

# Run the FastAPI app via uvicorn.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
