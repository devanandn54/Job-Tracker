# 🎯 JobTracker — Career Page Monitor

Monitors company career pages for **new jobs with ≤3 years experience** and sends  
**instant email + desktop alerts** so you can ask for referrals before the post fills.

Runs 24/7 on Railway (free tier) — set it up once, forget it.

---

## Phase 1 — Test Locally First

### Step 1 — Install Python 3.11+
```bash
python3 --version   # should say 3.11 or higher
```

### Step 2 — Set up environment
```bash
cd job_tracker
python3 -m venv venv
source venv/bin/activate        # Mac/Linux
# venv\Scripts\activate         # Windows

pip install -r requirements.txt
```

### Step 3 — Get a FREE Grok API key
1. Go to **https://console.x.ai** → sign in with X (Twitter)
2. Create an API key → copy it (starts with `xai-...`)
3. Free tier gives **$25/month** — more than enough for this

> **Alternative:** Claude key at https://console.anthropic.com (~$0.001/job)

### Step 4 — Configure
```bash
cp config.example.ini config.ini
```

Edit `config.ini`:
```ini
[ai]
provider = grok
grok_api_key = xai-YOURKEY_HERE

[email]
smtp_host     = smtp.gmail.com
smtp_port     = 587
smtp_user     = you@gmail.com
smtp_password = abcd efgh ijkl mnop    ← Gmail App Password (not normal password!)
to_address    = you@gmail.com
```

**Gmail App Password** (required):
1. https://myaccount.google.com/security → enable 2-Step Verification
2. https://myaccount.google.com/apppasswords → create password for "Mail"
3. Use the 16-char code as `smtp_password`

### Step 5 — Add your target companies
Edit `companies.csv` — one company per line:
```csv
Stripe,https://stripe.com/jobs/search
Airbnb,https://careers.airbnb.com/positions/
MyDreamCompany,https://dreamco.com/careers
```

**Common career page URL patterns:**
- Greenhouse boards: `https://boards.greenhouse.io/COMPANY`
- Lever: `https://jobs.lever.co/COMPANY`
- Workday: `https://COMPANY.wd1.myworkdayjobs.com/careers`

### Step 6 — Test run
```bash
python job_tracker.py --once
```

You should see:
```
✅ Loaded 5 companies
Checking Stripe → https://stripe.com/jobs/search
  Found 47 job links, 3 new
  [Stripe] 'Software Engineer, Payments' → min_years=2.0
  🎯 2 job(s) with ≤3 yrs experience found!
```
And receive an email alert!

---

## Phase 2 — Deploy to Railway (runs 24/7, free)

### Prerequisites
- Railway account: https://railway.app (free hobby plan works)
- Git installed
- GitHub account

### Step 1 — Push to GitHub
```bash
cd job_tracker
git init
git add .
git commit -m "Initial job tracker"
# Create a new repo on github.com, then:
git remote add origin https://github.com/YOURUSERNAME/job-tracker.git
git push -u origin main
```

> ⚠️ `config.ini` is in `.gitignore` — your API keys are NEVER pushed to GitHub.

### Step 2 — Create Railway project
1. Go to https://railway.app → **New Project**
2. Choose **Deploy from GitHub repo**
3. Select your `job-tracker` repo
4. Railway auto-detects the Dockerfile and builds it

### Step 3 — Set environment variables on Railway
In your Railway project → **Variables** tab, add these:

| Variable | Value |
|---|---|
| `AI_PROVIDER` | `grok` |
| `GROK_API_KEY` | `xai-your-key-here` |
| `SMTP_HOST` | `smtp.gmail.com` |
| `SMTP_PORT` | `587` |
| `SMTP_USER` | `you@gmail.com` |
| `SMTP_PASSWORD` | `your app password` |
| `EMAIL_TO` | `you@gmail.com` |
| `POLL_INTERVAL_MINUTES` | `30` |
| `MAX_EXPERIENCE_YEARS` | `3` |

> No `config.ini` needed on Railway — it reads these env vars directly.

### Step 4 — Deploy
Click **Deploy** (or it auto-deploys on push). That's it!

Railway runs your tracker every 30 minutes, forever, for free.

### Step 5 — Check logs
Railway dashboard → your service → **Logs** tab. You'll see:
```
Loaded 10 companies to track
Scheduler started — checking every 30 minutes
Checking Stripe → ...
```

---

## Updating your companies list

Edit `companies.csv` locally → `git commit` → `git push`  
Railway auto-redeploys within ~1 minute.

---

## Environment Variables Reference

| Variable | Default | Description |
|---|---|---|
| `AI_PROVIDER` | `grok` | `grok`, `claude`, or `both` |
| `GROK_API_KEY` | — | xAI key (free at console.x.ai) |
| `CLAUDE_API_KEY` | — | Anthropic key (console.anthropic.com) |
| `SMTP_HOST` | — | e.g. `smtp.gmail.com` |
| `SMTP_PORT` | `587` | Usually 587 for TLS |
| `SMTP_USER` | — | Your email address |
| `SMTP_PASSWORD` | — | App password |
| `EMAIL_TO` | — | Who to notify |
| `POLL_INTERVAL_MINUTES` | `30` | How often to check |
| `MAX_EXPERIENCE_YEARS` | `3` | Alert threshold |

---

## CLI options (local use)

```
python job_tracker.py --once        # one-shot test
python job_tracker.py               # run continuously
python job_tracker.py --config path/to/config.ini
python job_tracker.py --companies path/to/companies.csv
```

---

## View stored jobs

```bash
sqlite3 state/jobs.sqlite3 "SELECT company, title, min_years, found_at FROM job_details ORDER BY found_at DESC LIMIT 20;"
```
