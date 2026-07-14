# Deploying the Faceless YouTube Kit (always-on cloud worker)

This pipeline is a **background worker**, not a website. It polls Airtable
forever and runs multi-minute video renders, so it needs a host that runs a
persistent process — **not** Vercel (Vercel runs short-lived serverless
functions and will reject it with *"No python entrypoint found"*).

> The friendly, non-technical setup walkthrough is the **separate written setup
> guide** included with your purchase. This file is the technical reference.

Recommended hosts (any one): **Railway** (easiest), **Render**, or **Fly.io**.
All three run the same `python run.py` worker. A `Dockerfile`, `Procfile`, and
`render.yaml` are included so each platform auto-detects it.

---

## Step 1 — Get your Google token (one-time, on your laptop)

The finished MP4s upload straight into your Google Drive folder. The worker needs
a Google refresh token once, with the Drive, Gmail-send, and YouTube-upload
scopes. **No Microsoft / Azure involved.**

1. Create a free OAuth client at <https://console.cloud.google.com>:
   - Create/select any project → **APIs & Services → Library →** enable the
     **Google Drive API**, **Gmail API**, and **YouTube Data API v3**.
   - **OAuth consent screen →** External → add your Gmail as a **Test user**.
   - **Credentials → Create Credentials → OAuth client ID → Desktop app.**
     Copy the **Client ID** and **Client secret**.
2. Locally:
   ```bash
   pip install -r requirements.txt
   cp .env.example .env          # set YOUTUBE_CLIENT_ID + YOUTUBE_CLIENT_SECRET
   python -m tools.gdrive_auth
   ```
   A browser opens; approve access. It prints `YOUTUBE_REFRESH_TOKEN=...`. Save it.

## Step 2 — Deploy on Railway (recommended — usage-based billing)

Railway bills mostly for CPU actually used (≈0 while idle-polling, spikes only
during a render) plus a small always-on RAM cost — roughly **$1–3/month** for
this light workload. The repo ships a `railway.toml` + `Dockerfile` so it's
plug-and-play.

1. <https://railway.app> → **New Project → Deploy from GitHub repo** → pick this
   repository.
2. Railway reads `railway.toml`, builds the `Dockerfile`, and runs
   `python run.py`. No start command to set.
3. Open the service → **Variables** → add everything from your `.env`
   (see [`.env.example`](.env.example) for the full list). Click **Deploy**.
4. **Logs** should show the worker arming its daily poll + publish schedule.

> Tip: in Railway you can click **Raw Editor** in the Variables tab and paste
> your whole `.env` at once.

### Alternatives (same image)
- **Render:** New → Blueprint → uses the included `render.yaml` (Starter plan is
  always-on at ~$7/mo — flat, not usage-based). Set every secret in the Render
  dashboard.
- **Fly.io:** `fly launch --no-deploy` (detects the Dockerfile), then
  `fly secrets set KEY=... ...`, then `fly deploy`.

## Step 3 — Verify

In the host's shell/logs run (or check the boot logs):
```bash
python run.py --check    # validates config + Airtable connectivity
```
Then set a Video Library record's **Status = Scripted** and watch it flow to
**Video Ready**, then **Posted**.

---

## Not Vercel

If you accidentally connected this repo to Vercel, it will create projects that
fail on every push (this isn't a web app). Delete those Vercel projects or
disconnect the Git repo — they aren't used by this deployment.

## Environment variables

Every variable is documented in [`.env.example`](.env.example). The required
minimum: `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`, `AIRTABLE_VIDEO_LIBRARY_TABLE_ID`,
`ELEVENLABS_API_KEY`, and for Google/YouTube/Drive:
`YOUTUBE_CLIENT_ID` + `YOUTUBE_CLIENT_SECRET` + `YOUTUBE_REFRESH_TOKEN`.
`ANTHROPIC_API_KEY` powers the weekly scripting and tailored upload copy.
Set your niche with `NICHE_TOPIC`, `CHANNEL_NAME`, and `SCRIPT_STYLE_PROMPT`.
