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
    # Tech & product
    ("tech", "Hacker News",          "https://news.ycombinator.com/rss"),
    ("tech", "The Verge",            "https://www.theverge.com/rss/index.xml"),
    ("tech", "Ars Technica",         "https://feeds.arstechnica.com/arstechnica/index"),
    ("tech", "MIT Tech Review",      "https://www.technologyreview.com/feed/"),
    ("tech", "Wired",                "https://www.wired.com/feed/rss"),
    ("tech", "TechCrunch",           "https://techcrunch.com/feed/"),
    # Deals & funding
    ("deals", "TechCrunch Fundings", "https://techcrunch.com/category/fundings-exits/feed/"),
    ("deals", "Crunchbase News",     "https://news.crunchbase.com/feed/"),
    ("deals", "StrictlyVC",          "https://strictlyvc.com/feed/"),
    ("deals", "Axios Pro Rata",      "https://www.axios.com/pro/deals/rss"),
    # VC & firm network
    ("vc", "Fortune Term Sheet",     "https://fortune.com/tag/term-sheet/feed/"),
    ("vc", "PitchBook News",         "https://pitchbook.com/news/rss"),
    ("vc", "StrictlyVC",             "https://strictlyvc.com/feed/"),
    ("vc", "Axios VC",               "https://www.axios.com/pro/venture-capital/rss"),
    ("vc", "The Information",        "https://www.theinformation.com/feed"),
]

# ── Topic tuning ──────────────────────────────────────────────────────────────

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

# ── Summarize ─────────────────────────────────────────────────────────────────

def _format_bucket(articles: list) -> str:
    return "\n\n".join(
        f"[{a['source']}] {a['title']}\nURL: {a['url']}\n{a['summary']}"
        for a in articles[:30]
    )


def summarize_digest(buckets: dict) -> str:
    focus_str  = "\n".join(f"- {t}" for t in TOPICS_FOCUS)
    ignore_str = "\n".join(f"- {t}" for t in TOPICS_IGNORE)

    prompt = f"""You are a sharp analyst curating a digest for a European venture capital partner. \
He reads this over morning coffee and wants real depth, not bullet-point summaries.

PRIORITISE:
{focus_str}

IGNORE entirely (drop these stories, do not mention them):
{ignore_str}

Write the digest with exactly these five sections in this order. \
Use these headers word for word on their own line:

SUMMARY
TECH & PRODUCT
DEALS & FUNDING
FIRM NETWORK
---

SUMMARY
3 sentences maximum. What are the 2-3 dominant themes across tech, deals, and VC this period? \
Write it as an analyst would brief a partner before a Monday morning meeting. \
This appears at the top so it must stand alone.

TECH & PRODUCT
5-6 stories. For each story:
- Headline on its own line (no label, no number, just the headline)
- Then 4-6 sentences of substance: what happened, the context behind it, \
why it matters strategically, and any implication for European or Asian markets where relevant.
- Then the source URL on its own line.
- Then a blank line before the next story.
Do not pad. If a story only deserves 3 sentences, write 3. \
Skip anything that is purely US-domestic with no global tech relevance.

DEALS & FUNDING
List every notable deal from the source articles. Group them into three tiers:

LARGE (>$100M)
For each deal: one line with company name, amount, round stage, and sector. \
Then 2 sentences: what the company does and why this round is significant. \
Then the most relevant link (press release if available, otherwise a news article, \
otherwise the company website). Then a blank line.

MID ($10M–$100M)
Same format as LARGE.

EARLY (<$10M or undisclosed seed/pre-seed)
For each: one line with company name, amount if known, and sector. \
One sentence on what they do. Link. Blank line.

If a tier has no deals, omit it entirely.

FIRM NETWORK
Strictly venture capital firms and tech-focused growth equity / PE only. \
No investment banks, no asset managers, no hedge funds.
Cover: new fund closes, fund II/III launches, new partners or promotions, \
LP-GP dynamics, secondaries activity, strategy pivots, notable portfolio decisions.
3-5 items. For each: 2-3 sentences. Blank line between items.
If nothing material: write "Quiet period."

---
End the digest with three dashes on their own line as shown above.

Formatting rules (strictly enforced):
- Plain text only. Zero markdown. No asterisks, no hyphens as bullets, no bold markers.
- Blank line between every story or item.
- URLs on their own line, immediately after the relevant story.
- No filler phrases like "in a significant development" or "it is worth noting".
- No sign-off or closing line.

TECH & PRODUCT SOURCE ARTICLES
{_format_bucket(buckets.get("tech", []))}

DEALS & FUNDING SOURCE ARTICLES
{_format_bucket(buckets.get("deals", []))}

FIRM NETWORK SOURCE ARTICLES
{_format_bucket(buckets.get("vc", []))}
"""

    msg = Anthropic(api_key=ANTHROPIC_API_KEY).messages.create(
        model="claude-opus-4-5",
        max_tokens=3500,
        messages=[{"role": "user", "content": prompt}]
    )
    return msg.content[0].text


def summarize_breaking(articles: list) -> str:
    prompt = f"""Breaking tech stories trending on Hacker News (high score = community-validated).
Write a plain-text alert covering max 3 stories.
For each: headline, then 3-4 sentences — what happened, why it matters to a VC/tech professional, \
any immediate implication.
Then the URL on its own line.
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
    """Convert plain-text digest into a clean, readable HTML email."""

    SECTION_HEADERS = {
        "SUMMARY":         "Summary",
        "TECH & PRODUCT":  "Tech &amp; product",
        "DEALS & FUNDING": "Deals &amp; funding",
        "FIRM NETWORK":    "Firm network",
    }

    DEAL_TIERS = {"LARGE (>$100M)", "MID ($10M–$100M)", "EARLY (<$10M or undisclosed seed/pre-seed)"}

    lines      = body.strip().split("\n")
    html       = []
    in_summary = False

    for line in lines:
        stripped = line.strip()

        if stripped == "---":
            continue

        # Top-level section header
        if stripped in SECTION_HEADERS:
            label      = SECTION_HEADERS[stripped]
            in_summary = (stripped == "SUMMARY")
            html.append(
                f'<p style="margin:36px 0 10px; font-family:sans-serif; font-size:11px; '
                f'font-weight:700; letter-spacing:0.12em; text-transform:uppercase; '
                f'color:#999; border-bottom:1px solid #ebebeb; padding-bottom:8px;">'
                f'{label}</p>'
            )
            continue

        # Deal tier sub-header
        if stripped in DEAL_TIERS:
            html.append(
                f'<p style="margin:24px 0 8px; font-family:sans-serif; font-size:11px; '
                f'font-weight:700; letter-spacing:0.08em; text-transform:uppercase; '
                f'color:#bbb;">{stripped}</p>'
            )
            continue

        # URL line
        if stripped.startswith("http"):
            html.append(
                f'<p style="margin:4px 0 0; font-family:sans-serif; font-size:12px;">'
                f'<a href="{stripped}" style="color:#888; text-decoration:none;">'
                f'{stripped[:72]}{"…" if len(stripped) > 72 else ""}</a></p>'
            )
            continue

        # Blank line
        if not stripped:
            html.append('<div style="height:14px"></div>')
            continue

        # Summary section — italic intro paragraph
        if in_summary:
            html.append(
                f'<p style="margin:0 0 0; font-family:Georgia,serif; font-size:15px; '
                f'color:#333; line-height:1.75; font-style:italic;">{stripped}</p>'
            )
            continue

        # Headline heuristic: previous block was a section/tier header or a spacer after a URL
        last = html[-1] if html else ""
        is_after_header  = "text-transform:uppercase" in last
        is_after_spacer  = last == '<div style="height:14px"></div>'
        prev_prev        = html[-2] if len(html) >= 2 else ""
        is_after_url     = 'href=' in prev_prev and is_after_spacer

        if is_after_header or is_after_url:
            html.append(
                f'<p style="margin:0 0 6px; font-family:sans-serif; font-size:15px; '
                f'font-weight:600; color:#111; line-height:1.4;">{stripped}</p>'
            )
        else:
            html.append(
                f'<p style="margin:0; font-family:Georgia,serif; font-size:15px; '
                f'color:#444; line-height:1.75;">{stripped}</p>'
            )

    date_str = datetime.now().strftime("%A, %d %b %Y")
    accent   = "#c0392b" if is_breaking else "#2c3e50"
    label    = "Breaking alert" if is_breaking else "Tech digest"

    return f"""<html>
<body style="margin:0; padding:0; background:#f2f2ef;">
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f2f2ef; padding:32px 16px;">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0"
       style="background:#ffffff; border-radius:6px; overflow:hidden;
              border:1px solid #e2e2de;">

  <tr><td style="padding:28px 44px 22px; border-bottom:1px solid #ebebeb;">
    <p style="margin:0 0 5px; font-family:sans-serif; font-size:11px; font-weight:700;
              letter-spacing:0.12em; text-transform:uppercase; color:{accent};">{label}</p>
    <p style="margin:0; font-family:sans-serif; font-size:21px; font-weight:600;
              color:#111; line-height:1.3;">{date_str}</p>
  </td></tr>

  <tr><td style="padding:4px 44px 40px;">
    {"".join(html)}
  </td></tr>

  <tr><td style="padding:14px 44px; border-top:1px solid #ebebeb; background:#fafaf8;">
    <p style="margin:0; font-family:sans-serif; font-size:11px; color:#bbb;">
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

    total = sum(len(v) for v in buckets.values())
    if total == 0:
        print("[digest] No articles found, skipping.")
        return

    print(f"[digest] {total} articles across 3 tracks — summarizing...")
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
