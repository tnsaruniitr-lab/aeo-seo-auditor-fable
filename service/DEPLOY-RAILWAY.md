# Deploy to Railway — step-by-step

This deploys `standalone/main.py` (the FastAPI audit service) on Railway, exposed at a public HTTPS URL that any project can call.

**Total time: ~10 minutes.** Most of it is waiting for the Docker build.

---

## Prerequisites

1. **Railway account** — sign up at https://railway.app (free tier works for v0; Hobby tier $5/mo recommended for sustained use)
2. **GitHub repo connected to Railway** — Railway needs read access to `tnsaruniitr-lab/aeo-seo-auditor`
3. **Anthropic API key** — get one at https://console.anthropic.com/settings/keys, set a $100/month budget cap

---

## Step 1: Create the Railway project

1. Open https://railway.app/dashboard
2. Click **New Project** → **Deploy from GitHub repo**
3. Authorize Railway to read your repos if prompted
4. Select `tnsaruniitr-lab/aeo-seo-auditor`
5. Railway will detect the `standalone/Dockerfile` and start building

If Railway doesn't auto-detect the Dockerfile path:
- Click **Settings** → **Service Settings**
- Set **Root Directory** to: `/` (repo root)
- Set **Dockerfile Path** to: `standalone/Dockerfile`

---

## Step 2: Set the API key

While the build runs (3–5 minutes), set the environment variable:

1. In your Railway project, click the service → **Variables** tab
2. Click **+ New Variable**
3. Set:
   - **Name:** `ANTHROPIC_API_KEY`
   - **Value:** your real key (starts with `sk-ant-api03-`)
4. Click **Add**

The variable is encrypted at rest. Never appears in logs, never visible in code.

You can also optionally set:
- `AUDIT_OUTPUT_DIR=/app/audits` (already the default in Dockerfile, no need to override)
- `PORT` — Railway sets this automatically; don't override

---

## Step 3: Generate a public URL

1. In your service, click **Settings** → **Networking**
2. Click **Generate Domain**
3. Railway gives you something like:
   ```
   https://aeo-seo-auditor-production.up.railway.app
   ```

(Optional: set up a custom domain like `audit.yourdomain.com` later.)

---

## Step 4: Verify the deployment

Once the build is "Active" / green:

```bash
# Health check — should return JSON with brain stats
curl https://YOUR-RAILWAY-URL.up.railway.app/healthz
```

Expected response:
```json
{
  "status": "ok",
  "brain_loaded": true,
  "brain_stats": {
    "rules": 4980,
    "anti_patterns": 2843,
    "playbooks": 1213,
    "principles": 3728,
    "mapped_checks": 40
  },
  "anthropic_key_set": true,
  "output_dir": "/app/audits"
}
```

If `anthropic_key_set` is `false`, the env var didn't propagate. Re-add it and Railway will redeploy automatically.

---

## Step 5: Submit your first audit

```bash
# Submit a URL — returns audit_id immediately
curl -X POST https://YOUR-RAILWAY-URL.up.railway.app/audit \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com"}'
```

Response:
```json
{
  "audit_id": "abc-123-...",
  "status": "queued",
  "message": "Audit queued. Poll the audit endpoint for status.",
  "poll_url": "/audit/abc-123-..."
}
```

Then poll:
```bash
# Wait 60-120 seconds, then poll
curl https://YOUR-RAILWAY-URL.up.railway.app/audit/abc-123-...
```

When `status` becomes `"completed"`, the response includes:
- `result_summary` — score, grade, top findings
- `artifacts` — URLs for `.json`, `.md`, `.pdf`

---

## Step 6: Calling from another project (Replit, Vercel, etc.)

Python:
```python
import requests, time

AUDITOR = "https://YOUR-RAILWAY-URL.up.railway.app"

# Submit
r = requests.post(f"{AUDITOR}/audit", json={"url": "https://example.com"})
audit_id = r.json()["audit_id"]

# Poll
while True:
    r = requests.get(f"{AUDITOR}/audit/{audit_id}")
    data = r.json()
    if data["status"] == "completed":
        break
    if data["status"] == "error":
        raise RuntimeError(data.get("error"))
    time.sleep(5)

# Download artifacts
pdf = requests.get(f"{AUDITOR}/audit/{audit_id}.pdf")
with open("audit.pdf", "wb") as f:
    f.write(pdf.content)
```

JavaScript:
```javascript
const AUDITOR = "https://YOUR-RAILWAY-URL.up.railway.app";

const sub = await fetch(`${AUDITOR}/audit`, {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({url: "https://example.com"})
});
const {audit_id} = await sub.json();

let result;
while (true) {
  const r = await fetch(`${AUDITOR}/audit/${audit_id}`);
  result = await r.json();
  if (result.status === "completed") break;
  if (result.status === "error") throw new Error(result.error);
  await new Promise(r => setTimeout(r, 5000));
}
```

---

## Cost expectations

| Volume | Anthropic cost (Sonnet 4.6) | Railway cost | Total |
|---|---|---|---|
| 10 audits/day | ~$10/mo | $5/mo (Hobby) | $15/mo |
| 50 audits/day | ~$50/mo | $5–$20/mo | $55–$70/mo |
| 100 audits/day | ~$100/mo | $20/mo | $120/mo |
| 500 audits/day | ~$500/mo | $50–$100/mo | $550–$600/mo |

Anthropic dominates the bill at any meaningful volume.

---

## Operational notes

### What happens on restart

The in-memory `JOBS` dict is wiped on every Railway redeploy or container restart. Audits in flight are lost. For v1 this is acceptable (just resubmit). For multi-instance scale, swap to Redis or Postgres for persistent job state.

Audit *artifacts* (the `.json`, `.md`, `.pdf` files) are written to `/app/audits/`, which is **ephemeral on Railway by default.** Files don't persist across redeploys. To make audits persistent:

- Mount a Railway Volume to `/app/audits` (Railway dashboard → Service → Settings → Volumes)
- Or write artifacts directly to S3 / R2 / Supabase Storage (small change to `audit_pipeline.py`)

For v1, ephemeral artifacts are fine — clients should download via API immediately after audit completes.

### Concurrency

Railway's free tier gives 1 instance. The FastAPI service uses `BackgroundTasks` for async audits, so a single instance can handle ~5–10 concurrent audits before contention. If you need more, scale via Railway's "Replicas" setting.

### Logs

Railway → Service → Deployments → View logs. The service prints `[1/6] Running deterministic scripts...` etc. for each audit, so you can trace per-request progress.

### Updating

When you `git push` to `main`, Railway auto-redeploys. ~3 min downtime per deploy. For zero-downtime, enable Railway's blue-green or use a custom rolling-restart strategy.

---

## Troubleshooting

| Issue | Likely cause | Fix |
|---|---|---|
| `/healthz` returns `degraded` | Snapshot files missing from build | Verify Dockerfile copies `auditor-ruleset-export/` |
| Audits return error "anthropic SDK not installed" | requirements.txt not pinned | Should never happen — pin in requirements |
| Audits return error "ANTHROPIC_API_KEY not set" | env var not propagated | Re-add in Variables tab; Railway should redeploy |
| `/audit/{id}.pdf` 404s | Chrome not in container | Verify Dockerfile installs `chromium` |
| Audit takes >180s | Network latency to Sieve / target site | Increase `timeout` in `audit_pipeline.run_deterministic_scripts` |
| Container crashes on start | `PORT` env var issue | Railway auto-sets PORT; check uvicorn binds to `${PORT}` |

---

## Migration off Railway later

When you outgrow Railway (probably at 1,000+ audits/day):

| Target | What changes |
|---|---|
| Fly.io | `flyctl deploy` from the same Dockerfile, set secrets via `fly secrets set` |
| Render | Connect repo, point to Dockerfile, set env vars in dashboard |
| AWS Fargate | Push image to ECR, run task, set env vars in task definition |
| Self-host | Same Dockerfile works anywhere — just `docker run --env-file=.env -p 8000:8000 <image>` |

The auditor code is platform-agnostic. Migration is changing the host, not the code.

---

## Done.

Once `/healthz` returns `ok` and a test audit completes, your auditor is live. Share the public URL with whoever needs to call it (your other Replit projects, internal tools, customer-facing flows, etc.).
