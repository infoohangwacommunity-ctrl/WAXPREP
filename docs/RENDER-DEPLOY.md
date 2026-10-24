# Render + Supabase Deployment Guide

This guide deploys Wax Prep on Render's free tier using Supabase as the
PostgreSQL database. The web server and background worker run in ONE
process — no separate worker service needed.

## What you need

- A GitHub account (the repo is already there)
- A Render account (https://render.com — free, no credit card needed)
- A Supabase account (https://supabase.com — free tier includes PostgreSQL)
- Your API keys (Upstage, WhatsApp, Telegram)

## Step 1: Set up Supabase

1. Go to https://supabase.com and create a free project.
2. Wait for the project to provision (~2 minutes).
3. Go to **Project Settings > Database**.
4. Under **Connection info**, find the **Connection string**.
5. Copy the **Session mode** connection string (port 5432). It looks like:
   ```
   postgresql://postgres:[YOUR-PASSWORD]@db.[PROJECT-REF].supabase.co:5432/postgres
   ```
   Replace `[YOUR-PASSWORD]` with your database password.
6. Save this string — you'll paste it into Render as `DATABASE_URL`.

**Why session mode (port 5432)?** WaxPrep uses database transactions
with `SET ROLE` and session-level settings. Transaction mode (port 6543,
the pooler) doesn't support session-level operations reliably. Session
mode is safer for this app.

## Step 2: Generate encryption keys

WaxPrep encrypts sensitive student text at rest (AES-256-GCM). You
need to generate a master key:

```bash
python -c "import os,base64; print(f'1:{base64.b64encode(os.urandom(32)).decode()}')"
```

This prints something like `1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=`.
Save this — you'll set it as `WAXPREP_MASTER_KEYS` in Render.

## Step 3: Deploy on Render

1. Go to https://dashboard.render.com.
2. Click **New** → **Blueprint**.
3. Select your GitHub account and find the `WAXPREP` repository.
4. Select the `render-deploy` branch.
5. Render will detect `render.yaml` and show the service definition.
6. Before clicking **Apply**, set the secret environment variables:
   - `DATABASE_URL` — paste your Supabase connection string
   - `WAXPREP_MASTER_KEYS` — paste your generated key
   - `UPSTAGE_API_KEY` — your Upstage API key
   - `WHATAPPS_TOKEN` — your WhatsApp Cloud API token (if using WhatsApp)
   - `WHATAPPS_PHONE_NUMBER_ID` — your WhatsApp phone number ID
   - `WHATAPPS_APP_SECRET` — your WhatsApp app secret
   - `WHATAPPS_VERIFY_TOKEN` — your WhatsApp verify token
   - `TELEGRAM_BOT_TOKEN` — your Telegram bot token (if using Telegram)
   - `TELEGRAM_WEBHOOK_SECRET` — a random string you choose
7. Click **Apply**. Render will build and deploy.

The build takes ~2-3 minutes. Render installs dependencies with `uv`,
runs migrations against your Supabase database, and starts the app with
`uvicorn`.

## Step 4: Get your public URL

After the first deploy, Render assigns a URL like:
```
https://waxprep-xxxx.onrender.com
```

1. Copy this URL.
2. Go back to Render → your service → **Environment**.
3. Set `WAXPREP_PUBLIC_BASE_URL` to this URL.
4. Save and redeploy.

This URL is what WhatsApp and Telegram use to reach your webhooks.

## Step 5: Configure WhatsApp webhook

1. Go to https://developers.facebook.com/apps → your app.
2. WhatsApp → Configuration → Webhook.
3. Set the callback URL to:
   ```
   https://waxprep-xxxx.onrender.com/channels/whatsapp/webhook
   ```
4. Set the verify token to the same `WHATAPPS_VERIFY_TOKEN` you set in Render.
5. Subscribe to `messages`.

## Step 6: Configure Telegram webhook

The app automatically registers the Telegram webhook on startup when
`WAXPREP_PUBLIC_BASE_URL` is set. After your first successful deploy
with the URL set, send a message to your bot — it should respond.

## How the merged worker works

On Render's free tier, there's no separate background worker service.
WaxPrep solves this by running the worker as an **asyncio background
task inside the web process**:

```
uvicorn (web server) + asyncio._worker_loop (background worker)
```

The worker loop runs `services.worker.run_once()` every 5 seconds
(configurable via `WAXPREP_WORKER_INTERVAL`). It processes:
- Pending derivation jobs (embeddings, token indexing, artifact understanding)
- Pending deletion requests (best-effort — deletion is deferred)
- Post-turn notebook stewardship

The worker is **isolated** — a failure in the worker cannot crash the
web server (it's wrapped in try/except). The web server always
responds to webhooks first; the worker runs in the background.

**Free tier limitation**: Render's free tier sleeps the service after
15 minutes of no inbound traffic. When a webhook wakes it, the app
starts up, runs migrations, and the worker resumes. This is acceptable
for a tutoring service where students send messages, not a
high-traffic API.

## Environment variables reference

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | Supabase PostgreSQL connection string (port 5432) |
| `WAXPREP_MASTER_KEYS` | Yes | AES-256-GCM key (format: `1:base64-key`) |
| `WAXPREP_ACTIVE_KEY_ID` | Yes | Which key is active (default: `1`) |
| `UPSTAGE_API_KEY` | Yes | Upstage AI API key for the Teacher + CI models |
| `UPSTAGE_MODEL` | No | Upstage model name (default: `solar-pro4`) |
| `WAXPREP_WHATSAPP_ENABLED` | No | Set to `1` to enable WhatsApp |
| `WHATAPPS_TOKEN` | If WA | WhatsApp Cloud API token |
| `WHATAPPS_PHONE_NUMBER_ID` | If WA | WhatsApp phone number ID |
| `WHATAPPS_APP_SECRET` | If WA | WhatsApp app secret |
| `WHATAPPS_VERIFY_TOKEN` | If WA | WhatsApp webhook verify token |
| `WAXPREP_TELEGRAM_ENABLED` | No | Set to `1` to enable Telegram |
| `TELEGRAM_BOT_TOKEN` | If TG | Telegram bot token |
| `TELEGRAM_WEBHOOK_SECRET` | If TG | Random string for webhook verification |
| `WAXPREP_PUBLIC_BASE_URL` | After deploy | Your Render URL (for webhooks) |
| `WAXPREP_WORKER_INTERVAL` | No | Worker loop interval in seconds (default: 5) |
| `WAXPREP_WORKER_ENABLED` | No | Set to `0` to disable worker (default: 1) |
| `WAXPREP_LOG_LEVEL` | No | Log level (default: INFO) |

## Branch strategy

- **`main`** — kept intact for Railway (if Ray comes back)
- **`render-deploy`** — this branch, for Render deployment

Both branches share the same codebase. The only difference is the
deployment configuration (`render.yaml` exists only on `render-deploy`).
The app code is identical — the merged worker runs on both Railway
and Render.

## Troubleshooting

**"storage unreachable" in /health**
- Check that `DATABASE_URL` is set correctly in Render.
- Verify the Supabase project is running (not paused).
- Try session mode (port 5432) instead of transaction mode (port 6543).

**"migration apply failed" in logs**
- Check the Supabase connection string format.
- Make sure the database password doesn't have special characters that
  need URL-encoding (like `@`, `#`, `?`).
- If the password has special characters, URL-encode them in the
  connection string.

**WhatsApp webhook not receiving**
- Verify `WAXPREP_PUBLIC_BASE_URL` is set to your Render URL.
- Verify the webhook URL in Meta's dashboard matches.
- Check that `WHATAPPS_VERIFY_TOKEN` matches.

**Telegram bot not responding**
- Verify `TELEGRAM_BOT_TOKEN` is set.
- Verify `WAXPREP_PUBLIC_BASE_URL` is set.
- Check the logs for "telegram webhook set" on startup.

**Worker not processing jobs**
- Check `WAXPREP_WORKER_ENABLED` is `1` (default).
- Check `WAXPREP_WORKER_INTERVAL` is reasonable (5s default).
- Look for "merged background worker started" in the startup logs.
- On the free tier, the service sleeps after 15 min of inactivity.
  Send a message to wake it up.

**Service sleeps (free tier)**
- Render's free tier sleeps after 15 min of no inbound traffic.
- When a webhook arrives, the service wakes up (~30 seconds cold start).
- To keep it alive, use an external uptime monitor like UptimeRobot
  to ping `https://waxprep-xxxx.onrender.com/health` every 10 minutes.
