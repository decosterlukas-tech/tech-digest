#!/usr/bin/env python3
"""
Tech news digest — Mon/Wed/Fri + breaking news alerts via Gmail.
Three tracks: tech & product, deals & funding, VC firm network.
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

RSS_FEEDS = [
    ("tech",  "Hacker News",          "https://news.ycombinator.com/rss"),
    ("tech",  "The Verge",            "https://www.theverge.com/rss/index.xml"),
    ("tech",  "Ars Technica",         "https://feeds.arstechnica.com/arstechnica/index"),
    ("tech",  "MIT Tech Review",      "https://www.technologyreview.com/feed/"),
    ("tech",  "Wired",                "https://www.wired.com/feed/rss"),
    ("tech",  "TechCrunch",           "https://techcrunch.com/feed/"),
    ("deals", "TechCrunch Fundings",  "https://techcrunch.com/category/fundings-exits/feed/"),
    ("deals", "Crunchbase News",      "https://news.crunchbase.com/feed/"),
    ("deals", "StrictlyVC",           "https://strictlyvc.com/feed/"),
    ("deals", "Axios Pro Rata",       "https://www.axios.com/pro/deals/rss"),
    ("vc",    "Fortune Term Sheet",   "https://fortune.com/tag/term-sheet/feed/"),
    ("vc",    "PitchBook News",       "https://pitchbook.com/news/rss"),
    ("vc",    "StrictlyVC",           "https://strictlyvc.com/feed/"),
    ("vc",    "Axios VC",             "https://www.axios.com/pro/venture-capital/rss"),
    ("vc",    "The Information",      "https://www.theinformation.com/feed"),
]

TOPICS_IGNORE = [
    "US domestic politics and elections",
    "natural disasters, fires, and local emergencies",
    "celebrity news and entertainment",
    "sports",
    "crime unless directly tech-related",
    "personal finance tips and listicles",
]

VC_FIRMS_ONLY = [
    "venture capital firms",
    "tech-focused growth equity firms",
    "tech-focused PE firms",
]

VC_EXCLUDE = [
    "investment banks (Goldman, Morgan Stanley, JP Morgan, etc.)",
    "hedge funds",
    "generalist asset managers",
    "sovereign wealth funds unless directly investing in VC",
    "retail banks",
]

HN_BREAKING_THRESHOLD = int(os.getenv("HN_BREAKING_THRESHOLD", "300"))
MAX_ARTICLES_PER_FEED = 15
LOOKBACK_HOURS        = 48

ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
GMAIL_USER         = os.environ["GMAIL_USER"]
GMAIL_APP_PASSWORD = os.environ["GMAIL_APP_PASSWORD"]
DIGEST_RECIPIENT   = os.environ.get("DIGEST_RECIPIENT", GMAIL_USER)

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_rss_articles(hours_back: int = LOOKBACK_HOURS) -> dict:
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=hours_back)
    buckets = {"tech": [], "deals": [], "vc": []}

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
                summary = re.sub(r"<[^>]+>", "", getattr(entry, "summary", "") or "")[:600]
                buckets[track].append({
                    "source":    source_name,
                    "title":     entry.get("title", "").strip(),
                    "url":       entry.get("link", ""),
                    "summary":   summary.strip(),
                    "published": published.isoformat() if published else "",
                })
        except Exception as e:
            print(f"[warn] Failed to fetch {source_name}: {e}")

    return buckets


def fetch_article_text(url: str, max_chars: int = 3000) -> str:
    """Fetch full article text for richer summarization."""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        # Trim to roughly the middle content
        start = max(0, len(text) // 6)
        return text[start:start + max_chars]
    except Exception:
        return ""


def enrich_top_articles(articles: list, n: int = 8) -> list:
    """Fetch full text for the top n articles to give Claude more to work with."""
    enriched = []
    for a in articles[:n]:
        full = fetch_article_text(a["url"])
        enriched.append({**a, "full_text": full})
    return enriched + articles[n:]


def fetch_hn_top(min_score: int = HN_BREAKING_THRESHOLD) -> list:
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
                    "source":    "Hacker News",
                    "title":     story.get("title", ""),
                    "url":       story.get("url", f"https://news.ycombinator.com/item?id={story_id}"),
                    "summary":   f"HN score: {story['score']} | {story.get('descendants', 0)} comments",
                    "published": "",
                    "hn_score":  story.get("score", 0),
                })
        return hot
    except Exception as e:
        print(f"[warn] HN fetch failed: {e}")
        return []

# ── Format ────────────────────────────────────────────────────────────────────

def _format_article(a: dict) -> str:
    full = a.get("full_text", "")
    body = f"{a['summary']}\n{full}" if full else a["summary"]
    return f"[{a['source']}] {a['title']}\nURL: {a['url']}\n{body}"


def _format_bucket(articles: list) -> str:
    return "\n\n---\n\n".join(_format_article(a) for a in articles[:30])

# ── Summarize ─────────────────────────────────────────────────────────────────

def summarize_digest(buckets: dict) -> str:

    ignore_str   = "\n".join(f"- {t}" for t in TOPICS_IGNORE)
    vc_only_str  = "\n".join(f"- {t}" for t in VC_FIRMS_ONLY)
    vc_excl_str  = "\n".join(f"- {t}" for t in VC_EXCLUDE)

    prompt = f"""You are writing a briefing for a European venture capital partner. \
He reads this at 08:00 with coffee. He wants depth and signal, not summaries of summaries.

=== STRICT RULES — NEVER VIOLATE THESE ===

1. OUTPUT ORDER IS FIXED. You must write the four sections in exactly this order, \
with these exact headers on their own line, nothing else on that line:

SUMMARY
TECH & PRODUCT
DEALS & FUNDING
FIRM NETWORK

2. SUMMARY IS ALWAYS FIRST. It appears before any other section. No exceptions.

3. IGNORE THESE TOPICS ENTIRELY — do not mention them anywhere in the digest:
{ignore_str}

4. FIRM NETWORK contains ONLY these types of organisations:
{vc_only_str}
NEVER include these in FIRM NETWORK:
{vc_excl_str}
If a story is about a bank or asset manager, drop it entirely.

5. DEALS & FUNDING must be split into three sub-tiers in this order:
LARGE (>$100M)
MID ($10M–$100M)
EARLY (<$10M or seed)
Omit a tier only if there are genuinely zero deals for it.

6. No markdown. No asterisks. No hyphens as bullets. Plain text only.

=== CONTENT INSTRUCTIONS ===

SUMMARY
Exactly 3 sentences. What are the dominant themes across tech, deals, and VC this period? \
Write it like a partner pre-brief: sharp, direct, no filler.

TECH & PRODUCT
5-6 stories. For each:
Line 1: the headline (plain, no label)
Lines 2+: 4-6 sentences of real analysis. What happened, the context, \
why it matters strategically, any European or Asian angle.
Final line: the URL
Blank line after each story.

DEALS & FUNDING
After each tier sub-header, list every deal from the source articles in that tier.
For each deal:
Line 1: Company name — Amount — Stage — Sector
Line 2-3: What the company does (1 sentence). Why this round matters (1 sentence).
Line 4: Best available link (press release > news article > company website)
Blank line after each deal.

FIRM NETWORK
VC and tech-focused growth equity / PE only — see strict rules above.
3-5 items: fund closes, new funds, partner moves, LP dynamics, secondaries, strategy shifts.
2-3 sentences per item. Blank line between items.
If nothing material from VC/tech-PE sources: write exactly "Quiet period."

=== SOURCE ARTICLES ===

TECH & PRODUCT SOURCES
{_format_bucket(buckets.get("tech", []))}

DEALS & FUNDING SOURCES
{_format_bucket(buckets.get("deals", []))}

FIRM NETWORK SOURCES
{_format_bucket(buckets.get("vc", []))}
"""

    msg = Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def summarize_breaking(articles: list) -> str:
    prompt = f"""Breaking tech stories trending on Hacker News right now.
Write a plain-text alert, max 3 stories.
For each: headline on its own line, then 3-4 sentences (what happened, why it matters \
to a VC/tech professional, any immediate implication), then the URL on its own line.
No markdown. No filler. Skip US politics, local emergencies, celebrity news.

Stories:
{_format_bucket(articles)}"""

    msg = Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text

# ── HTML renderer ─────────────────────────────────────────────────────────────

def _render_html(body: str, is_breaking: bool = False) -> str:

    SECTION_HEADERS = {
        "SUMMARY":         "Summary",
        "TECH & PRODUCT":  "Tech &amp; product",
        "DEALS & FUNDING": "Deals &amp; funding",
        "FIRM NETWORK":    "Firm network",
    }

    DEAL_TIERS = {
        "LARGE (>$100M)",
        "MID ($10M–$100M)",
        "EARLY (<$10M or seed)",
    }

    lines      = body.strip().split("\n")
    html_parts = []
    in_summary = False
    prev_type  = None  # "header", "tier", "headline", "body", "url", "blank"

    for line in lines:
        stripped = line.strip()

        if stripped == "---":
            continue

        if stripped in SECTION_HEADERS:
            in_summary = (stripped == "SUMMARY")
            html_parts.append(
                f'<p style="margin:36px 0 10px; font-family:sans-serif; font-size:11px; '
                f'font-weight:700; letter-spacing:0.12em; text-transform:uppercase; '
                f'color:#999; border-bottom:1px solid #ebebeb; padding-bottom:8px;">'
                f'{SECTION_HEADERS[stripped]}</p>'
            )
            prev_type = "header"
            continue

        if stripped in DEAL_TIERS:
            html_parts.append(
                f'<p style="margin:22px 0 8px; font-family:sans-serif; font-size:11px; '
                f'font-weight:700; letter-spacing:0.08em; text-transform:uppercase; '
                f'color:#bbb;">{stripped}</p>'
            )
            prev_type = "tier"
            continue

        if stripped.startswith("http"):
            html_parts.append(
                f'<p style="margin:3px 0 0; font-family:sans-serif; font-size:12px;">'
                f'<a href="{stripped}" style="color:#999; text-decoration:none;">'
                f'Read more</a></p>'
            )
            prev_type = "url"
            continue

        if not stripped:
            html_parts.append('<div style="height:14px"></div>')
            prev_type = "blank"
            continue

        if in_summary:
            html_parts.append(
                f'<p style="margin:0; font-family:Georgia,serif; font-size:15px; '
                f'color:#333; line-height:1.8; font-style:italic;">{stripped}</p>'
            )
            prev_type = "body"
            continue

        # Headline: line that follows a header, tier, or blank-after-url
        is_headline = (
            prev_type in ("header", "tier") or
            (prev_type == "blank" and len(html_parts) >= 2 and 'href=' in html_parts[-2])
        )

        if is_headline:
            html_parts.append(
                f'<p style="margin:0 0 5px; font-family:sans-serif; font-size:15px; '
                f'font-weight:600; color:#111; line-height:1.4;">{stripped}</p>'
            )
            prev_type = "headline"
        else:
            html_parts.append(
                f'<p style="margin:0; font-family:Georgia,serif; font-size:15px; '
                f'color:#444; line-height:1.8;">{stripped}</p>'
            )
            prev_type = "body"

    date_str = datetime.now().strftime("%A, %d %b %Y")
    accent   = "#c0392b" if is_breaking else "#2c3e50"
    label    = "Breaking alert" if is_breaking else "Tech digest"

    return f"""<html>
<body style="margin:0;padding:0;background:#f0f0ec;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f0ec;padding:32px 16px;">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#fff;border-radius:6px;border:1px solid #e0e0db;">

  <tr><td style="padding:28px 44px 22px;border-bottom:1px solid #ebebeb;">
    <p style="margin:0 0 5px;font-family:sans-serif;font-size:11px;font-weight:700;
              letter-spacing:0.12em;text-transform:uppercase;color:{accent};">{label}</p>
    <p style="margin:0;font-family:sans-serif;font-size:21px;font-weight:600;
              color:#111;line-height:1.3;">{date_str}</p>
  </td></tr>

  <tr><td style="padding:4px 44px 40px;">
    {"".join(html_parts)}
  </td></tr>

  <tr><td style="padding:14px 44px;border-top:1px solid #ebebeb;background:#fafaf8;">
    <p style="margin:0;font-family:sans-serif;font-size:11px;color:#bbb;">
      Mon &middot; Wed &middot; Fri &middot; 08:00 Brussels
    </p>
  </td></tr>

</table>
</td></tr></table>
</body></html>"""

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str, is_breaking: bool = False):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = DIGEST_RECIPIENT
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(_render_html(body, is_breaking=is_breaking), "html"))
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_USER, DIGEST_RECIPIENT, msg.as_string())
    print(f"[ok] Email sent to {DIGEST_RECIPIENT}")

# ── Entrypoints ───────────────────────────────────────────────────────────────

def run_digest():
    print("[digest] Fetching articles...")
    buckets = fetch_rss_articles(hours_back=LOOKBACK_HOURS)
    buckets["tech"].extend(fetch_hn_top(min_score=100))

    print("[digest] Enriching top articles with full text...")
    buckets["tech"]  = enrich_top_articles(buckets["tech"],  n=8)
    buckets["deals"] = enrich_top_articles(buckets["deals"], n=10)
    buckets["vc"]    = enrich_top_articles(buckets["vc"],    n=6)

    total = sum(len(v) for v in buckets.values())
    if total == 0:
        print("[digest] No articles found, skipping.")
        return

    print(f"[digest] {total} articles enriched — summarizing...")
    summary = summarize_digest(buckets)
    day     = datetime.now().strftime("%A %d %b")
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
        subject=f"Breaking — {hot[0]['title'][:60]}",
        body=summary,
        is_breaking=True,
    )


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "digest"
    if mode == "breaking":
        run_breaking_check()
    else:
        run_digest()
