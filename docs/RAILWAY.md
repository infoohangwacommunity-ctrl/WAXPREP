# Deploying WAXPREP on Railway

WAXPREP needs **two processes** and **one Postgres database**:

| Process | Role | Start command |
|---------|------|----------------|
| **Web** | FastAPI HTTP (`/health`, WhatsApp webhooks when enabled) | `uv run uvicorn waxprep.app:app --host 0.0.0.0 --port $PORT` |
| **Worker** | Background derivation / scheduled jobs | `uv run python -m waxprep.scheduler run-interval --seconds 30` |
| **Postgres** | Primary store (RLS, history, outbox) | Railway Postgres plugin |

The repository ships:

- `railway.toml` — default **web** service (build, health check, migrations, start)
- `railway.worker.toml` — reference config for the **worker** service

Secrets stay in the Railway dashboard, never in Git.

## 1. Create the project

1. New Railway project → **Deploy from GitHub** → `infoohangwacommunity-ctrl/WAXPREP`.
2. Add **PostgreSQL** (Railway plugin). This sets `DATABASE_URL` on linked services.
3. The first service is the **web** service and should pick up `railway.toml`.

## 2. Environment variables (web + worker)

Set these on **both** services (or share via a Railway shared variable group):

| Variable | Required | Notes |
|----------|----------|--------|
| `DATABASE_URL` | yes | Usually injected by the Postgres plugin |
| `WAXPREP_MASTER_KEYS` | yes | `id:base64(32-bytes)` — generate offline; never commit |
| `WAXPREP_ACTIVE_KEY_ID` | yes | e.g. `1` |
| `UPSTAGE_API_KEY` | for live tutoring | Main Teacher / document parse |
| `WAXPREP_PUBLIC_BASE_URL` | for WhatsApp | Public HTTPS URL of the web service |
| `WAXPREP_WHATSAPP_ENABLED` | optional | `true` only when WhatsApp is fully configured |
| `WHATSAPP_TOKEN` | if WhatsApp | Cloud API token |
| `WHATSAPP_PHONE_NUMBER_ID` | if WhatsApp | |
| `WHATSAPP_APP_SECRET` | if WhatsApp | Webhook signature |
| `WHATSAPP_VERIFY_TOKEN` | if WhatsApp | Webhook challenge |
| `WAXPREP_BLOB_ROOT` | optional | Local encrypted blobs; ephemeral on Railway unless you attach a volume |

Generate encryption keys:

```bash
python -c "import os,base64; print('1:'+base64.b64encode(os.urandom(32)).decode())"
```

## 3. Add the worker service

1. In the same Railway project: **New service** → **GitHub repo** (same repo, same branch).
2. Override the start command to:

   ```text
   uv run python -m waxprep.scheduler run-interval --seconds 30
   ```

3. Link the same Postgres and copy the same env vars (especially `DATABASE_URL` and encryption keys).
4. The worker does **not** need a public domain.

## 4. Domain and WhatsApp

1. On the **web** service, generate a Railway domain (or attach a custom domain).
2. Set `WAXPREP_PUBLIC_BASE_URL` to that HTTPS origin (no trailing slash).
3. WhatsApp webhook URL:

   ```text
   https://<your-domain>/channels/whatsapp/webhook
   ```

4. Enable WhatsApp only after token, phone number id, app secret, and verify token are set.

## 5. Migrations

`railway.toml` runs `apply_migrations` as `preDeployCommand` on the web service so schema updates apply before the new web process starts.

If a release fails on migrations, fix the SQL or env and redeploy; do not hand-edit `schema_migrations` in production.

## 6. What you do **not** need

- A second history/session database
- Redis (not required by current architecture)
- A volume for the first deploy (optional later for local blob storage)
- Committing `.env` or real API keys
- OpenAI/Gemini as the main Teacher (Upstage is the configured main model path)

## 7. Smoke check after deploy

```bash
curl -sS https://<your-domain>/health
```

Expect JSON roughly like `{"status":"ok","service":"waxprep","version":"..."}`.

Then confirm the worker service is running (Railway metrics / logs show periodic scheduler passes).
