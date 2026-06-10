# Tech digest bot

Weekly tech news digest + breaking news alerts, delivered clean to your inbox.
No ads. No tracking. Summarized by Claude.

## What it does

- **Every Monday 08:00 Brussels time**: pulls the last 7 days from TechCrunch, The Verge, Ars Technica, Wired, MIT Tech Review, and Hacker News — Claude summarizes into a clean digest (~400 words)
- **Every 4 hours**: checks Hacker News for stories above 300 points — sends an alert only if something big is trending

---

## Setup (15 minutes)

### 1. Gmail App Password

You need a Gmail App Password (not your normal password):

1. Go to your Google Account → **Security** → **2-Step Verification** (must be enabled)
2. Scroll down to **App passwords** → create one named "Tech digest"
3. Copy the 16-character password — you'll use this below

### 2. Fork this repo on GitHub

Create a free GitHub account if you don't have one, then fork this repo (or create a new one and copy the files in).

### 3. Add secrets

In your GitHub repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (get one at console.anthropic.com) |
| `GMAIL_USER` | your.email@gmail.com |
| `GMAIL_APP_PASSWORD` | The 16-char App Password from step 1 |
| `DIGEST_RECIPIENT` | Email address to send to (can be same as GMAIL_USER) |

### 4. Enable Actions

Go to **Actions** tab in your repo → click "I understand my workflows, enable them".

That's it. The workflow runs automatically on the schedule.

---

## Run it manually

In GitHub → **Actions** → **Tech digest** → **Run workflow** → choose `weekly` or `breaking`.

## Run locally

```bash
pip install -r requirements.txt

export ANTHROPIC_API_KEY=sk-ant-...
export GMAIL_USER=you@gmail.com
export GMAIL_APP_PASSWORD=xxxxxxxxxxxx
export DIGEST_RECIPIENT=you@gmail.com

python digest.py weekly    # send the weekly digest now
python digest.py breaking  # check HN and send alert if something big
```

---

## Customise

**Add or remove RSS feeds** — edit the `RSS_FEEDS` list in `digest.py`:
```python
RSS_FEEDS = [
    ("Bloomberg Tech", "https://feeds.bloomberg.com/technology/news.rss"),
    # remove any you don't want
]
```

**Change the HN breaking threshold** — edit `HN_BREAKING_THRESHOLD` in the workflow env or in the script. 300 = fairly viral. 500 = only the biggest stories.

**Change the schedule** — edit the `cron` lines in `.github/workflows/digest.yml`. Use [crontab.guru](https://crontab.guru) to build expressions.

**Costs** — at typical usage (~52 weekly digests + occasional breaking alerts/year), Claude API cost is roughly €2-4/year.
