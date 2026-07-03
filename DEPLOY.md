# Deploying to Railway

This service lives in `service/`, but the Docker build **must run with the
repository root as its build context** — the Dockerfile copies `service/...`
paths that only resolve from the root. Get these settings right and it builds
first try.

## Railway service settings (the part that usually breaks a deploy)

| Setting | Value | Why |
|---|---|---|
| **Root Directory** | **empty / `/`** (repo root) | If you set it to `service`, the build context becomes `service/` and `COPY service /app/service` fails with `"service": not found`. **This is the #1 cause of a failed build after the subdirectory restructure.** |
| **Builder** | Dockerfile | Set automatically by `railway.json` (`build.builder = DOCKERFILE`). If Railway picked Nixpacks, force Dockerfile. |
| **Config path** | `railway.json` (repo root) | Points `dockerfilePath` at `service/Dockerfile` and sets the healthcheck. |
| **Health check path** | `/healthz` | Public, no auth. Returns `{"status":"ok","brain_loaded":true}`. |

## Required environment variables (set in Railway → Variables)

Minimum to boot and audit:

```
ANTHROPIC_API_KEY=sk-ant-...
```

Recommended for production (the service fails CLOSED in prod without auth):

```
AUDIT_USERNAME=admin
AUDIT_PASSWORD=<random>
AUDIT_API_KEY=<random>            # server-to-server (X-API-Key)
SUPABASE_URL=https://<proj>.supabase.co
SUPABASE_SERVICE_KEY=<service-role-key>
AUDIT_WEBHOOK_SECRET=<random>
```

`AUDITOR_FAIL_CLOSED=1` is baked into the Dockerfile — set the three auth vars
above or internal/expensive routes return 503 (by design). `/healthz` stays
public so the health check still passes.

## Verify after deploy

```
curl https://<your-app>.up.railway.app/healthz
# → {"status":"ok","brain_loaded":true,"git_sha":"<sha>"}
```

## If the build still fails

Grab the **Build Logs** (Railway → the deployment → Build tab) and the last
~30 lines tell you exactly where. Common ones:
- `COPY failed: ... "service": not found` → Root Directory is set to `service`; clear it.
- Nixpacks output instead of Docker steps → builder wasn't Dockerfile; set it.
- Healthcheck timeout → the app didn't bind `$PORT`; confirm no PORT override.
