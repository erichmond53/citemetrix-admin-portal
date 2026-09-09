#!/usr/bin/env python3
"""Traffic data job for the portal Marketing -> Traffic page.

GA4 (property 521402961, gsc-sa-key.json): sessions by source/medium, landing page, campaign,
device, plus totals (sessions/engaged/users/avg duration/engagement rate) for 7d and 30d windows.
GSC (same key, webmasters.readonly): branded + /check/ + top-20 non-branded queries (last 28 days)
and an 8-point branded weekly trend from the existing gsc-weekly snapshots.

Writes reports/traffic/traffic-latest.json for the portal route to render. Read-only.
Usage: cd /var/www/admin-portal && venv/bin/python jobs/traffic.py
"""
import os, sys, json, glob, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
KEY_FILE = os.path.join(BASE, "gsc-sa-key.json")
GSC_DIR  = os.path.join(BASE, "reports", "gsc")
OUT_DIR  = os.path.join(BASE, "reports", "traffic")
GA4_PROPERTY = "521402961"
BRAND_RE = "(?i)cite ?metrix"

out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "ga4": {}, "gsc": {}, "notes": []}

# CITEMETRIX-CODE-KICKOFF-MEASUREMENT-2026-09-09.md §3 /
# CITEMETRIX-UTM-TAGGING-STANDARD-2026-09-09.md normalization map. Folds
# referrer-hostname and shortener variants of the same platform into one
# channel row, applied here at read time over an already-fetched GA4
# report -- this never rewrites GA4 itself. Same channel vocabulary as
# the WP plugin's CiteMetrix_Content_Engine::COMPANION_RECIPES utm_source
# values; there's no shared runtime between this Flask app and the WP
# plugin to import a single registry from, so keep both lists in sync by
# hand if a channel is added on either side.
CHANNEL_NORMALIZATION_MAP = {
    "fb": "facebook", "m.facebook.com": "facebook", "lm.facebook.com": "facebook",
    "l.facebook.com": "facebook", "facebook.com": "facebook",
    "ig": "instagram", "instagram.com": "instagram",
    "lnkd.in": "linkedin", "linkedin.com": "linkedin",
    "t.co": "x", "twitter": "x", "twitter.com": "x", "x.com": "x",
    "sendy": "email", "drip": "email", "mailer": "email",
    "reddit.com": "reddit", "old.reddit.com": "reddit",
    "threads.net": "threads",
    "youtu.be": "youtube",
}

# Legacy-unattributed source labels. Left exactly as GA4 reports them --
# never merged into each other or renamed -- but their combined share of
# sessions is surfaced as its own number: its decline over time is the
# measure of whether UTM tagging is actually being adopted.
UNATTRIBUTED_SOURCES = {"(direct)", "(not set)", "(data not available)"}

# ─────────────────────────── GA4 ───────────────────────────
try:
    from google.oauth2 import service_account
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (RunReportRequest, Dimension, Metric, DateRange, Filter, FilterExpression, FilterExpressionList)
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    client = BetaAnalyticsDataClient(credentials=creds)
    prop = f"properties/{GA4_PROPERTY}"

    def run(dims, mets, days, limit=100000):
        req = RunReportRequest(property=prop,
            date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
            dimensions=[Dimension(name=d) for d in dims],
            metrics=[Metric(name=m) for m in mets], limit=limit)
        return client.run_report(req).rows

    _EMAIL_F = FilterExpression(filter=Filter(field_name="sessionMedium",
        string_filter=Filter.StringFilter(value="email", match_type=Filter.StringFilter.MatchType.EXACT, case_sensitive=False)))
    def runf(dims, mets, days, dfilter, limit=100000):
        req = RunReportRequest(property=prop,
            date_ranges=[DateRange(start_date=f"{days}daysAgo", end_date="today")],
            dimensions=[Dimension(name=d) for d in dims],
            metrics=[Metric(name=m) for m in mets], limit=limit)
        req.dimension_filter = dfilter
        return client.run_report(req).rows

    def top(dim, days, n, label):
        rows = [{label: (r.dimension_values[0].value or "(none)"), "sessions": int(r.metric_values[0].value)}
                for r in run([dim], ["sessions"], days)]
        rows.sort(key=lambda x: x["sessions"], reverse=True)
        return rows[:n]

    def top_source_medium(days, n):
        """Like top('sessionSourceMedium', ...), but folds referrer/shortener
        variants into one row per channel (CHANNEL_NORMALIZATION_MAP) before
        truncating to the top n -- so a long-tail variant of a channel that's
        big in aggregate isn't dropped just because no single variant made
        the raw top n on its own. Rows outside the map (google/organic,
        google/cpc, etc.) pass through with their original source/medium
        pairing intact; only the listed variants collapse."""
        raw = [{"key": (r.dimension_values[0].value or "(none)"), "sessions": int(r.metric_values[0].value)}
               for r in run(["sessionSourceMedium"], ["sessions"], days)]
        total = sum(r["sessions"] for r in raw)
        unattributed = 0
        merged = {}
        for r in raw:
            key, sessions = r["key"], r["sessions"]
            source = (key.split(" / ", 1)[0] if " / " in key else key).strip().lower()
            if source in UNATTRIBUTED_SOURCES:
                unattributed += sessions
                merged[key] = merged.get(key, 0) + sessions
                continue
            channel = CHANNEL_NORMALIZATION_MAP.get(source)
            merge_key = channel if channel else key
            merged[merge_key] = merged.get(merge_key, 0) + sessions
        rows = sorted(({"key": k, "sessions": v} for k, v in merged.items()),
                       key=lambda x: x["sessions"], reverse=True)
        unattributed_share = round(unattributed / total * 100, 1) if total else 0.0
        return rows[:n], unattributed_share

    def window(days):
        w = {}
        t = run([], ["sessions", "engagedSessions", "activeUsers", "averageSessionDuration", "engagementRate"], days)
        if t:
            mv = t[0].metric_values
            w["totals"] = {"sessions": int(mv[0].value), "engaged": int(mv[1].value), "users": int(mv[2].value),
                           "avg_duration": round(float(mv[3].value), 1),
                           "engagement_rate": round(float(mv[4].value) * 100, 1)}
        else:
            w["totals"] = {"sessions": 0, "engaged": 0, "users": 0, "avg_duration": 0, "engagement_rate": 0}
        w["source_medium"], w["unattributed_share"] = top_source_medium(days, 15)
        w["landing_pages"] = top("landingPagePlusQueryString", days, 20, "page")
        w["campaigns"]     = top("sessionCampaignName", days, 15, "campaign")
        w["devices"]       = {r.dimension_values[0].value: int(r.metric_values[0].value)
                              for r in run(["deviceCategory"], ["sessions"], days)}
        dev = {}
        for r in runf(["deviceCategory"], ["sessions", "averageSessionDuration", "engagementRate"], days, _EMAIL_F):
            dc = r.dimension_values[0].value
            dev[dc] = {"sessions": int(r.metric_values[0].value),
                       "avg_duration": round(float(r.metric_values[1].value), 1),
                       "engagement_rate": round(float(r.metric_values[2].value) * 100, 1),
                       "scans": 0, "leads": 0}
        for ev_name, key in (("scan_completed", "scans"), ("generate_lead", "leads")):
            evf = FilterExpression(and_group=FilterExpressionList(expressions=[_EMAIL_F,
                FilterExpression(filter=Filter(field_name="eventName",
                    string_filter=Filter.StringFilter(value=ev_name)))]))
            for r in runf(["deviceCategory"], ["eventCount"], days, evf):
                dc = r.dimension_values[0].value
                if dc in dev:
                    dev[dc][key] = int(r.metric_values[0].value)
        w["device_email"] = dev
        return w

    out["ga4"]["d7"]  = window(7)
    out["ga4"]["d30"] = window(30)
except Exception as e:
    out["notes"].append(f"GA4 failed: {e}")

# ─────────────────────────── GSC (last 28 days) ───────────────────────────
try:
    from googleapiclient.discovery import build
    from google.oauth2 import service_account as sa2
    gcreds = sa2.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
    svc = build("searchconsole", "v1", credentials=gcreds, cache_discovery=False)
    sites = svc.sites().list().execute().get("siteEntry", [])
    cm = [s["siteUrl"] for s in sites if "citemetrix" in s["siteUrl"].lower()]
    if cm:
        SITE = sorted(cm, key=lambda u: (not u.startswith("sc-domain:"), len(u)))[0]
        END = datetime.date.today() - datetime.timedelta(days=3)
        START = END - datetime.timedelta(days=27)

        def gquery(dims, filters=None, row_limit=25):
            body = {"startDate": START.isoformat(), "endDate": END.isoformat(), "dimensions": dims, "rowLimit": row_limit}
            if filters:
                body["dimensionFilterGroups"] = [{"filters": filters}]
            return svc.searchanalytics().query(siteUrl=SITE, body=body).execute().get("rows", [])

        def gtotals(filters):
            rows = gquery([], filters=filters)
            if not rows:
                return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
            r = rows[0]
            return {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
                    "ctr": round(r.get("ctr", 0.0), 4), "position": round(r.get("position", 0.0), 1)}

        branded = gtotals([{"dimension": "query", "operator": "includingRegex", "expression": BRAND_RE}])
        check   = gtotals([{"dimension": "page", "operator": "contains", "expression": "/check/"}])
        nb = gquery(["query"], filters=[{"dimension": "query", "operator": "excludingRegex", "expression": BRAND_RE}], row_limit=200)
        nb.sort(key=lambda r: int(r.get("impressions", 0)), reverse=True)
        nonbranded = [{"query": r["keys"][0], "impressions": int(r.get("impressions", 0)),
                       "clicks": int(r.get("clicks", 0)), "position": round(r.get("position", 0.0), 1)} for r in nb[:20]]
        trend = []
        for p in sorted(glob.glob(os.path.join(GSC_DIR, "gsc-weekly-*.json")))[-8:]:
            try:
                j = json.load(open(p))
                trend.append({"week_end": j["window"]["end"], "impressions": j["branded"]["impressions"],
                              "clicks": j["branded"]["clicks"]})
            except Exception:
                pass
        out["gsc"] = {"site": SITE, "window": {"start": START.isoformat(), "end": END.isoformat()},
                      "branded": branded, "check": check, "nonbranded": nonbranded, "branded_trend": trend}
    else:
        out["notes"].append("GSC: no citemetrix property visible to service account")
except Exception as e:
    out["notes"].append(f"GSC failed: {e}")

os.makedirs(OUT_DIR, exist_ok=True)
json.dump(out, open(os.path.join(OUT_DIR, "traffic-latest.json"), "w"), indent=2)
g7 = out["ga4"].get("d7", {}).get("totals", {}).get("sessions", "?")
print(f"[{out['generated']}] traffic OK — GA4 7d sessions={g7}; GSC branded impr={out['gsc'].get('branded', {}).get('impressions', '?')}; notes={out['notes']}")
