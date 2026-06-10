#!/usr/bin/env python3
"""
Tech news digest — Mon/Wed/Fri + breaking news alerts via Gmail.
Three tracks: tech & product, deals & funding, VC & firm news.
"""

import os
import re
import smtplib
import ssl
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
import requests
from anthropic import Anthropic

# ── Feed configuration ────────────────────────────────────────────────────────
# Each feed is tagged with one or more tracks so Claude knows the context.
# Tracks: "tech", "deals", "vc"

RSS_FEEDS = [
    # Tech & product — global relevance
    ("tech", "Hacker News",       "https://news.ycombinator.com/rss"),
    ("tech", "The Verge",         "https://www.theverge.com/rss/index.xml"),
    ("tech", "Ars Technica",      "https://feeds.arstechnica.com/arstechnica/index"),
    ("tech", "MIT Tech Review",   "https://www.technologyreview.com/feed/"),
    ("tech", "Wired",             "https://www.wired.com/feed/rss"),
    ("tech", "TechCrunch",        "https://techcrunch.com/feed/"),

    # Deals & funding
    ("deals", "TechCrunch Fundings", "https://techcrunch.com/category/fundings-exits/feed/"),
    ("deals", "Crunchbase News",     "https://news.crunchbase.com/feed/"),
    ("deals", "StrictlyVC",          "https://strictlyvc.com/feed/"),
    ("deals", "Axios Pro Rata",      "https://www.axios.com/pro/deals/rss"),

    # VC & firm news
    ("vc", "Fortune Term Sheet",  "https://fortune.com/tag/term-sheet/feed/"),
    ("vc", "PitchBook News",      "https://pitchbook.com/news/rss"),
    ("vc", "The Information VC",  "https://www.theinformation.com/feed"),  # partial free
    ("vc", "Axios VC",            "https://www.axios.com/pro/venture-capital/rss"),
    ("vc", "Bloomberg VC",        "https://feeds.bloomberg.com/technology/news.rss"),
]

# ── Topic tuning — edit freely ────────────────────────────────────────────────

TOPICS_FOCUS = [
    "AI, machine learning, and foundation models",
    "venture capital fund closes, LP dynamics, and GP moves",
    "startup funding rounds (Series A and above)",
    "M&A, acquisitions, and IPOs",
    "European and Asian tech companies and regulation",
    "semiconductors and hardware infrastructure",
    "developer tools and platforms",
]

TOPICS_IGNORE = [
    "US domestic politics and elections",
    "natural disasters, fires, and local emergencies",
    "celebrity news and entertainment",
    "sports",
    "crime and law enforcement (unless directly tech-related)",
    "personal finance tips and listicles",
]

# ── Runtime config ────────────────────────────────────────────────────────────

HN_BREAKING_THRESHOLD = int(os.getenv("HN_BREAKING_THRESHOLD", "300"))
MAX_ARTICLES_PER_FEED = 10
LOOKBACK_HOURS = 48

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
DIGEST_RECIPIENT   = os.environ.get("DIGEST_RECIPIENT", GMAIL_USER)

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_rss_articles(hours_back: int = LOOKBACK_HOURS) -> dict[str, list[dict]]:
    """Returns articles bucketed by track: {"tech": [...], "deals": [...], "vc": [...]}"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    buckets: dict[str, list[dict]] = {"tech": [], "deals": [], "vc": []}

    for track, source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:MAX_ARTICLES_PER_FEED]:
                published = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
                elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                    published = datetime(*entry.updated_parsed[:6], tzinfo=timezone.utc)

                if published and published < cutoff:
                    continue

                summary = getattr(entry, "summary", "") or ""
                summary = re.sub(r"<[^>]+>", "", summary)[:500]

                buckets[track].append({
                    "source": source_name,
                    "track": track,
                    "title": entry.get("title", "").strip(),
                    "url": entry.get("link", ""),
                    "summary": summary.strip(),
                    "published": published.isoformat() if published else "",
                })
        except Exception as e:
            print(f"[warn] Failed to fetch {source_name}: {e}")

    return buckets


def fetch_hn_top(min_score: int = HN_BREAKING_THRESHOLD) -> list[dict]:
    try:
        top_ids = requests.get(
            "https://hacker-news.firebaseio.com/v0/topstories.json", timeout=10
        ).json()[:50]
        hot = []
        for story_id in top_ids:
            story = requests.get(
                f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", timeout=5
            ).json()
            if story and story.get("score", 0) >= min_score:
                hot.append({
                    "source": "Hacker News",
                    "track": "tech",
                    "title": story.get("title", ""),
                    "url": story.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                    "summary": f"HN score: {story['score']} | {story.get('descendants', 0)} comments",
                    "published": "",
                    "hn_score": story.get("score", 0),
                })
        return hot
    except Exception as e:
        print(f"[warn] HN fetch failed: {e}")
        return []

# ── Summarize ─────────────────────────────────────────────────────────────────

def _format_bucket(articles: list[dict]) -> str:
    return "\n\n".join(
        f"[{a['source']}] {a['title']}\n{a['url']}\n{a['summary']}"
        for a in articles[:25]
    )


def summarize_digest(buckets: dict[str, list[dict]]) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    focus_str  = "\n".join(f"- {t}" for t in TOPICS_FOCUS)
    ignore_str = "\n".join(f"- {t}" for t in TOPICS_IGNORE)

    tech_text  = _format_bucket(buckets.get("tech", []))
    deals_text = _format_bucket(buckets.get("deals", []))
    vc_text    = _format_bucket(buckets.get("vc", []))

    prompt = f"""You are a sharp analyst curating a tech digest for a European venture capital professional. \
He follows global tech closely but has no interest in US-domestic noise.

PRIORITISE these topics:
{focus_str}

IGNORE or deprioritise these topics (drop them entirely if they add no VC-relevant insight):
{ignore_str}

You have three buckets of articles from the last 48 hours. Write a clean digest with exactly these three sections:

─── TECH & PRODUCT ───
Pick the 4-6 most globally relevant stories. One punchy headline per story + 2-3 sentences: \
what happened and why it matters to a global tech / VC audience. Skip anything that is purely US-domestic.

─── DEALS & FUNDING ───
Pick the 4-6 most noteworthy rounds, exits, or M&A moves. Include deal size and stage where available. \
2 sentences per item: the deal and its strategic significance.

─── VC & FIRMS ───
Pick 3-4 items: fund closes, LP news, partner moves, firm strategy shifts, secondaries market. \
2 sentences each. If there is nothing genuinely noteworthy, write "Quiet period — nothing material."

─── ONE-PARAGRAPH SUMMARY ───
3 sentences max. The dominant themes across all three sections this period.

Format: plain text only. No markdown, no asterisks, no bullet symbols. \
Use the section headers exactly as shown above. Keep the whole digest under 600 words.

─── TECH & PRODUCT SOURCE ARTICLES ───
{tech_text}

─── DEALS & FUNDING SOURCE ARTICLES ───
{deals_text}

─── VC & FIRMS SOURCE ARTICLES ───
{vc_text}
"""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def summarize_breaking(articles: list[dict]) -> str:
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    articles_text = _format_bucket(articles)
    prompt = f"""Breaking tech stories trending on Hacker News (high score = viral). \
Write a concise alert (plain text, no markdown) covering max 3 stories. \
For each: what happened + why it matters to a VC/tech professional. Under 250 words. \
Skip anything that is US politics, local emergencies, or celebrity news.

Stories:
{articles_text}"""

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = DIGEST_RECIPIENT

    msg.attach(MIMEText(body, "plain"))

    html_body = f"""
<html><body style="font-family: Georgia, serif; max-width: 640px; margin: 40px auto;
                   color: #222; line-height: 1.7; font-size: 15px;">
<pre style="white-space: pre-wrap; font-family: Georgia, serif; font-size: 15px;">{body}</pre>
<hr style="border:none; border-top:1px solid #ddd; margin:32px 0;">
<p style="font-size:11px; color:#aaa; font-family: sans-serif;">
  Tech digest · Mon / Wed / Fri · 08:00 Brussels
</p>
</body></html>"""
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, DIGEST_RECIPIENT, msg.as_string())
    print(f"[ok] Email sent to {DIGEST_RECIPIENT}")

# ── Entrypoints ───────────────────────────────────────────────────────────────

def run_digest():
    print("[digest] Fetching articles...")
    buckets = fetch_rss_articles(hours_back=LOOKBACK_HOURS)
    # Add HN noteworthy stories into the tech bucket
    hn = fetch_hn_top(min_score=100)
    buckets["tech"].extend(hn)

    total = sum(len(v) for v in buckets.values())
    if total == 0:
        print("[digest] No articles found, skipping.")
        return

    print(f"[digest] {total} articles across 3 tracks — summarizing...")
    summary = summarize_digest(buckets)

    day = datetime.now().strftime("%A %d %b")
    send_email(subject=f"Tech digest — {day}", body=summary)


def run_breaking_check():
    print("[breaking] Checking HN for viral stories...")
    hot = fetch_hn_top(min_score=HN_BREAKING_THRESHOLD)
    if not hot:
        print(f"[breaking] Nothing above {HN_BREAKING_THRESHOLD} points.")
        return
    print(f"[breaking] {len(hot)} hot stories — sending alert...")
    summary = summarize_breaking(hot)
    send_email(
        subject=f"⚡ Breaking — {hot[0]['title'][:60]}",
        body=summary
    )


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"
    if mode == "breaking":
        run_breaking_check()
    else:
        run_digest()
