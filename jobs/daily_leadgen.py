#!/usr/bin/env python3
"""Daily Lead-Gen View data job.

Per CITEMETRIX-Marketing-Module-Decisions-v3.md §3/§6 step 3 -- "one page,
date-first, all six channel rows, same columns, drill-down to campaign/
post/article." The acceptance test: Eric opens one page in the morning,
sees how many people came from each channel yesterday, what they did,
and which post sent anyone.

Channel rows come from GA4's own sessionDefaultChannelGroup, which
already buckets sessions correctly once utm_medium follows
CITEMETRIX-UTM-TAGGING-STANDARD-2026-09-09.md (organic_social / paid_social /
cpc / email / article / referral / qr) -- no custom bucketing logic needed
for that part. Two additions on top of GA4's own model:

  - Direct is kept as its own honest row, never hidden -- its share is the
    read on whether tagging is being adopted (same principle as jobs/
    traffic.py's unattributed_share).
  - "Content" (CITEMETRIX-MEASUREMENT-ARCHITECTURE-2026-09-09.md §9 step 5:
    "a Content row group, drillable to the post") is carved OUT of whatever
    GA4 group a tracked post's sessions would otherwise land in, by
    matching sessionManualAdContent (utm_content) against the Publication
    Log: CiteMetrix_Content_Engine companions' tracked_url (utm_content=
    c_<id>) read live from the WP product DB, plus admin-entered posts'
    tagged_url (utm_content=manual_<id>) from source_refs. Every Content
    row session is attributable to a specific logged post -- "every number
    is a door" (IA spec §2).

Customers/revenue can't honestly be sliced by day (conversion lags a
session by design), so that column is a trailing 30-day rollup regardless
of which period tab is showing, via the existing lead->order join
(same pattern as jobs/campaign_effectiveness.py), bucketed by the lead's
own utm_source/utm_medium.

Writes reports/daily_leadgen/daily-leadgen-latest.json. Read-only everywhere.
Usage: cd /var/www/admin-portal && venv/bin/python jobs/daily_leadgen.py
"""
import os, sys, json, re, datetime, decimal

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
KEY_FILE = os.path.join(BASE, "gsc-sa-key.json")
OUT_DIR = os.path.join(BASE, "reports", "daily_leadgen")
GA4_PROPERTY = "521402961"

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

today = datetime.date.today()
notes = []


def d2f(v):
    return float(v) if isinstance(v, decimal.Decimal) else v


ROWS = [
    ("organic_search", "Organic Search"),
    ("paid_search", "Paid Search"),
    ("email", "Email"),
    ("paid_social", "Paid Social"),
    ("organic_social", "Organic Social"),
    ("content", "Content (tracked posts)"),
    ("ai_assistant", "AI Assistant"),
    ("direct", "Direct / Unattributed"),
    ("other", "Other"),
]
ROW_LABELS = dict(ROWS)

GA4_GROUP_TO_BUCKET = {
    "Organic Search": "organic_search", "Paid Search": "paid_search", "Email": "email",
    "Paid Social": "paid_social", "Organic Social": "organic_social", "Direct": "direct",
    "AI Assistant": "ai_assistant", "Referral": "other", "Unassigned": "other",
    "Cross-network": "other",
}


def source_medium_to_bucket(source, medium):
    source = (source or "").strip().lower()
    medium = (medium or "").strip().lower()
    if not source and not medium:
        return "direct"
    if medium == "cpc":
        return "paid_search"
    if medium == "paid_social":
        return "paid_social"
    if medium == "organic_social":
        return "organic_social"
    if medium == "email" or source in ("email", "sendy", "drip", "mailer"):
        return "email"
    if medium == "organic" and source in ("google", "bing"):
        return "organic_search"
    if any(a in source for a in ("chatgpt", "claude", "perplexity", "gemini", "copilot")):
        return "ai_assistant"
    return "other"


def new_row():
    return {"sessions": 0, "engaged": 0, "engagement_seconds": 0.0, "scans": 0, "leads": 0}


# ─────────────────────────── Publication Log content-id lookup ───────────────────────────
# utm_content -> {title, platform, url, source_type}. Same registry vocabulary as
# CiteMetrix_Content_Engine::COMPANION_RECIPES (WP 4.58.0) and the Publication Log
# (admin-portal). No shared runtime -- parsed straight out of each tagged_url's
# querystring rather than duplicating the tagging logic here.
def _utm_content(url):
    if not url or "utm_content=" not in url:
        return None
    m = re.search(r"utm_content=([^&]+)", url)
    return m.group(1) if m else None


content_lookup = {}
try:
    conn = pymysql.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
                            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    with conn.cursor() as cur:
        cur.execute("""SELECT c.tracked_url, c.channel, c.title, c.published_url, g.topic
                       FROM wp_citemetrix_ce_companions c
                       JOIN wp_citemetrix_ce_generations g ON g.id = c.generation_id
                       WHERE c.tracked_url IS NOT NULL""")
        for r in cur.fetchall():
            cid = _utm_content(r["tracked_url"])
            if cid:
                content_lookup[cid] = {"title": r["title"] or r["topic"], "platform": r["channel"],
                                        "url": r["published_url"], "source_type": "engine"}
    conn.close()
except Exception as e:
    notes.append(f"product DB content lookup failed: {e}")

try:
    aconn = pymysql.connect(host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
                             password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
                             charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    with aconn.cursor() as cur:
        cur.execute("SELECT source, label, base_url, tagged_url FROM source_refs WHERE kind='publication' AND tagged_url IS NOT NULL")
        for r in cur.fetchall():
            cid = _utm_content(r["tagged_url"])
            if cid:
                content_lookup[cid] = {"title": r["label"], "platform": r["source"],
                                        "url": r["base_url"], "source_type": "manual"}
    aconn.close()
except Exception as e:
    notes.append(f"admin DB content lookup failed: {e}")

# ─────────────────────────── GA4: sessions/engagement/events by day x channel x content ───────────────────────────
by_period_bucket = {"1d": {}, "7d": {}, "30d": {}}
by_period_post = {"1d": {}, "7d": {}, "30d": {}}
direct_share = {}

try:
    from google.oauth2 import service_account
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (RunReportRequest, Dimension, Metric, DateRange,
        Filter, FilterExpression)
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    client = BetaAnalyticsDataClient(credentials=creds)
    prop = f"properties/{GA4_PROPERTY}"
    dr = [DateRange(start_date="30daysAgo", end_date="today")]

    def run(dims, mets, dim_filter=None, limit=100000):
        req = RunReportRequest(property=prop, date_ranges=dr,
            dimensions=[Dimension(name=d) for d in dims], metrics=[Metric(name=m) for m in mets], limit=limit)
        if dim_filter is not None:
            req.dimension_filter = dim_filter
        return client.run_report(req).rows

    yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y%m%d")

    def periods_for(date_str):
        d = datetime.datetime.strptime(date_str, "%Y%m%d").date()
        out = []
        if date_str == yesterday_str:
            out.append("1d")
        if d >= today - datetime.timedelta(days=7):
            out.append("7d")
        out.append("30d")
        return out

    def bucket_for(group, content):
        if content and content in content_lookup:
            return "content", content
        return GA4_GROUP_TO_BUCKET.get(group, "other"), None

    # sessions + engagement (engagementDuration is summable; averageSessionDuration is not)
    for r in run(["date", "sessionDefaultChannelGroup", "sessionManualAdContent"],
                 ["sessions", "engagedSessions", "userEngagementDuration"]):
        date_str, group, content = (dv.value for dv in r.dimension_values)
        sessions, engaged, secs = int(r.metric_values[0].value), int(r.metric_values[1].value), float(r.metric_values[2].value)
        bucket, post = bucket_for(group, content)
        for p in periods_for(date_str):
            row = by_period_bucket[p].setdefault(bucket, new_row())
            row["sessions"] += sessions; row["engaged"] += engaged; row["engagement_seconds"] += secs
            if post:
                prow = by_period_post[p].setdefault(post, new_row())
                prow["sessions"] += sessions; prow["engaged"] += engaged; prow["engagement_seconds"] += secs

    # scan_completed / generate_lead event counts, same dimension breakdown
    ev_filter = FilterExpression(filter=Filter(field_name="eventName",
        in_list_filter=Filter.InListFilter(values=["scan_completed", "generate_lead"])))
    for r in run(["date", "sessionDefaultChannelGroup", "sessionManualAdContent", "eventName"],
                 ["eventCount"], ev_filter):
        date_str, group, content, ev = (dv.value for dv in r.dimension_values)
        count = int(r.metric_values[0].value)
        key = "scans" if ev == "scan_completed" else "leads"
        bucket, post = bucket_for(group, content)
        for p in periods_for(date_str):
            row = by_period_bucket[p].setdefault(bucket, new_row())
            row[key] += count
            if post:
                by_period_post[p].setdefault(post, new_row())[key] += count

    # direct/unattributed share per period (adoption signal, same idea as jobs/traffic.py)
    for p in ("1d", "7d", "30d"):
        total = sum(b["sessions"] for b in by_period_bucket[p].values())
        direct = by_period_bucket[p].get("direct", new_row())["sessions"]
        direct_share[p] = round(100.0 * direct / total, 1) if total else 0.0
except Exception as e:
    notes.append(f"GA4 query failed: {e}")

# ─────────────────────────── Customers/revenue (trailing 30d, lead-level attribution) ───────────────────────────
customers_by_bucket = {}
try:
    conn = pymysql.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                            password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
                            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    with conn.cursor() as cur:
        cur.execute("""SELECT COALESCE(NULLIF(l.utm_source,''),'') utm_source,
                              COALESCE(NULLIF(l.utm_medium,''),'') utm_medium,
                              COUNT(DISTINCT o.billing_email) customers,
                              COALESCE(ROUND(SUM(o.total_amount),2),0) revenue
                       FROM wp_citemetrix_score_leads l
                       JOIN wp_wc_orders o ON o.billing_email = l.email
                       WHERE o.type='shop_order' AND o.status IN ('wc-completed','wc-processing') AND o.total_amount>0
                             AND l.excluded=0
                             AND o.billing_email NOT LIKE '%@standonitmarketing.com'
                             AND o.billing_email NOT LIKE '%@expertseoconsulting.com'
                       GROUP BY l.utm_source, l.utm_medium""")
        for r in cur.fetchall():
            bucket = source_medium_to_bucket(r["utm_source"], r["utm_medium"])
            cb = customers_by_bucket.setdefault(bucket, {"customers": 0, "revenue": 0.0})
            cb["customers"] += r["customers"]; cb["revenue"] += d2f(r["revenue"])
    conn.close()
except Exception as e:
    notes.append(f"customer attribution query failed: {e}")

# ─────────────────────────── assemble output ───────────────────────────
def assemble(period):
    rows = []
    for bucket, label in ROWS:
        b = by_period_bucket[period].get(bucket, new_row())
        cb = customers_by_bucket.get(bucket, {"customers": 0, "revenue": 0.0})
        avg_dur = round(b["engagement_seconds"] / b["sessions"], 1) if b["sessions"] else 0.0
        if not (b["sessions"] or b["scans"] or b["leads"] or cb["customers"]):
            continue
        rows.append({"bucket": bucket, "label": label, "sessions": b["sessions"], "engaged": b["engaged"],
                     "avg_duration": avg_dur, "scans": b["scans"], "leads": b["leads"],
                     "customers": cb["customers"], "revenue": cb["revenue"]})
    rows.sort(key=lambda r: r["sessions"], reverse=True)
    posts = []
    for content_id, b in by_period_post[period].items():
        meta = content_lookup.get(content_id, {"title": content_id, "platform": "?", "url": None, "source_type": "?"})
        posts.append({"content_id": content_id, **meta, "sessions": b["sessions"], "engaged": b["engaged"],
                      "scans": b["scans"], "leads": b["leads"]})
    posts.sort(key=lambda p: p["sessions"], reverse=True)
    return {"rows": rows, "posts": posts, "direct_share": direct_share.get(period, 0.0)}


out = {
    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    "periods": {p: assemble(p) for p in ("1d", "7d", "30d")},
    "yesterday": (today - datetime.timedelta(days=1)).isoformat(),
    "customers_window_days": 30,
    "notes": notes,
}
os.makedirs(OUT_DIR, exist_ok=True)
json.dump(out, open(os.path.join(OUT_DIR, "daily-leadgen-latest.json"), "w"), indent=2, default=str)
print(f"[{out['generated']}] daily_leadgen OK — 1d rows={len(out['periods']['1d']['rows'])}, "
      f"1d direct_share={out['periods']['1d']['direct_share']}%, notes={notes}")
