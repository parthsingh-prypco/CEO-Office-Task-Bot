# CEO Office Slack Bot — Setup Guide

## What you get
| Command | What it does |
|---|---|
| `/add-task Review Q2 report \| 2026-06-10 \| @parth \| High \| Commercial` | Add a task (all fields after the name are optional) |
| `/due-this-week` | Tasks due Mon–Sun this week |
| `/task-list` | All tasks (filter with `status:Done`, `track:Commercial`, `priority:High`) |
| `/update-task Review Q2 \| status \| Done` | Update any field on a task |
| Auto-notifications | Bot posts to your channel whenever the list changes (checked every 5 min) |
| Monday 9am digest | Weekly summary of overdue + due-this-week tasks |

---

## Step 1 — Create the Slack app (5 min)

1. Go to **https://api.slack.com/apps** → **Create New App** → **From Manifest**
2. Select your Prypco workspace
3. Paste the contents of `manifest.yaml` (leave `YOUR-APP` as-is for now, you'll update it in Step 4)
4. Click **Create** → **Install to Workspace** → **Allow**
5. Copy your **Bot User OAuth Token** (`xoxb-...`) and **Signing Secret** — you'll need these soon

---

## Step 2 — Create free Upstash Redis (2 min)

1. Go to **https://console.upstash.com** → Sign up (free)
2. Create a Redis database → choose any region
3. Copy **REST URL** and **REST Token** from the database details page

---

## Step 3 — Deploy to Vercel (5 min)

1. Push this `ceo-slack-bot/` folder to a GitHub repository
2. Go to **https://vercel.com** → **Add New Project** → import your repo
3. In **Environment Variables**, add all the values from `.env.example`:
   - `SLACK_BOT_TOKEN` — from Step 1
   - `SLACK_SIGNING_SECRET` — from Step 1
   - `PROJECT_LIST_ID` — `F0B7RB480DA` (already the default)
   - `NOTIFY_CHANNEL` — the Slack channel ID where notifications should go (right-click a channel → View channel details → copy the ID at the bottom)
   - `UPSTASH_REDIS_REST_URL` — from Step 2
   - `UPSTASH_REDIS_REST_TOKEN` — from Step 2
   - `CRON_SECRET` — any random string (e.g. run `openssl rand -hex 16` in terminal)
4. Click **Deploy**. Copy your Vercel URL (e.g. `https://ceo-slack-bot.vercel.app`)

---

## Step 4 — Wire up Slack URLs (2 min)

1. Go back to **https://api.slack.com/apps** → your app
2. In **Slash Commands**, edit each command and replace `YOUR-APP` with your actual Vercel URL
3. Save each command

---

## Step 5 — Verify column names (important!)

Open the `api/index.py` file and check the column name constants at the top (around line 45):

```python
COL_TITLE    = "Title"
COL_STATUS   = "Status"
COL_DUE      = "Due Date"
COL_OWNER    = "Owner"
COL_PRIORITY = "Priority"
COL_TRACK    = "Track"
```

Open your "Project Tracker - CEO Office" list in Slack, look at the column headers,
and update these values to match exactly (they are case-sensitive).

---

## Step 6 — Test it

In any Slack channel, type:
```
/add-task Test task | 2026-06-07 | @parth | High | Commercial
```

Then:
```
/due-this-week
/task-list
```

---

## Free tier limits

| Service | Free limit | Notes |
|---|---|---|
| Vercel | 100k function calls/day, 2 cron jobs | More than enough |
| Upstash Redis | 10,000 commands/day | ~1 command per 5-min check + each slash command |

The 5-minute change polling uses ~288 Redis commands/day — well within free limits.

---

## Troubleshooting

**Bot responds with "Failed to add task: missing_scope"**
→ The `lists:read` / `lists:write` scopes weren't added. Go to OAuth & Permissions → Bot Token Scopes → add them → reinstall the app.

**Column names not matching**
→ Update the `COL_*` constants in `api/index.py` to match your list's exact column headers.

**Cron not running**
→ Vercel cron requires the project to be on a non-paused deployment. Check Vercel dashboard → Cron Jobs tab.
