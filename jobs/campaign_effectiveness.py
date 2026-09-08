#!/usr/bin/env python3
"""Campaign Effectiveness data job — assembles the campaign -> session -> lead -> sale funnel.

Joins three first-party sources the admin-portal box can reach natively:
  - RDS citemetrix_prd (pymysql, .env creds): wp_citemetrix_score_leads (leads + utm_campaign),
    wp_wc_orders (HPOS, revenue by billing_email). Deterministic lead->sale join on email.
  - GA4 (property 521402961, gsc-sa-key.json): sessions / engaged / on-page event funnel by
    sessionCampaignName (= utm_campaign) and by landing page. The ANONYMOUS behavior layer.
  - GSC weekly JSON snapshots (reports/gsc/*.json): branded-search halo series.

Writes reports/campaigns/campaign-effectiveness.json for the portal page to render.
Runs daily via cron. Read-only everywhere. Sendy email metrics (sent/open/click) are a future
add — they live on a separate box and need an SSH path; noted in the output until wired.

Usage: cd /var/www/admin-portal && venv/bin/python jobs/campaign_effectiveness.py [DAYS]
"""
import os, sys, json, glob, datetime, decimal

HERE       = os.path.dirname(os.path.abspath(__file__))
BASE       = os.path.dirname(HERE)
KEY_FILE   = os.path.join(BASE, "gsc-sa-key.json")
GSC_DIR    = os.path.join(BASE, "reports", "gsc")
OUT_DIR    = os.path.join(BASE, "reports", "campaigns")
GA4_PROPERTY = "521402961"
DAYS       = int(sys.argv[1]) if len(sys.argv) > 1 else 30

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

today = datetime.date.today()
notes = []

def d2f(v):
    return float(v) if isinstance(v, decimal.Decimal) else v

# ─────────────────────────── RDS: leads + revenue ───────────────────────────
def db():
    return pymysql.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)

CAMP = "COALESCE(NULLIF(utm_campaign,''),'(none)')"

# Exclude internal/test purchases (Eric's own domains) from paying-customer + revenue math — not real customers.
def _not_internal(col="billing_email"):
    return f"({col} NOT LIKE '%@standonitmarketing.com' AND {col} NOT LIKE '%@expertseoconsulting.com')"

rds = {"kpis": {}, "by_campaign": {}}
try:
    conn = db()
    with conn.cursor() as cur:
        wk = (today - datetime.timedelta(days=today.weekday())).isoformat()   # Monday of this wk
        pwk = (today - datetime.timedelta(days=today.weekday()+7)).isoformat()
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_score_leads WHERE excluded=0")
        total_leads = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_score_leads WHERE converted=1 AND excluded=0")
        converted = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_score_leads WHERE created_at >= %s AND excluded=0", (wk,))
        leads_week = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_score_leads WHERE created_at >= %s AND created_at < %s AND excluded=0", (pwk, wk))
        leads_prev = cur.fetchone()["n"]
        # daily lead counts for the last DAYS days (sparklines)
        cur.execute("SELECT DATE(created_at) d, COUNT(*) n FROM wp_citemetrix_score_leads WHERE excluded=0 AND created_at >= %s GROUP BY DATE(created_at)",
                    ((today - datetime.timedelta(days=DAYS-1)).isoformat(),))
        _ld = {str(r["d"]): r["n"] for r in cur.fetchall()}
        _axis = [(today - datetime.timedelta(days=DAYS-1-i)) for i in range(DAYS)]
        _leads_daily = [_ld.get(d.isoformat(), 0) for d in _axis]
        _base = max(0, total_leads - sum(_leads_daily)); _run = _base; _cum = []
        for _v in _leads_daily:
            _run += _v; _cum.append(_run)
        rds["trends"] = {"leads_daily": _leads_daily, "leads_cumulative": _cum}
        # paid revenue overall
        cur.execute("""SELECT COUNT(DISTINCT billing_email) c, COALESCE(ROUND(SUM(total_amount),2),0) r
                       FROM wp_wc_orders WHERE type='shop_order' AND status IN ('wc-completed','wc-processing') AND total_amount>0
                       AND """ + _not_internal())
        row = cur.fetchone()
        rds["kpis"] = {"leads_total": total_leads, "converted": converted, "leads_week": leads_week,
                       "leads_prev": leads_prev, "paying_customers": row["c"], "revenue": d2f(row["r"])}
        # leads by campaign
        cur.execute(f"SELECT {CAMP} camp, COUNT(*) leads, SUM(converted) conv FROM wp_citemetrix_score_leads WHERE excluded=0 GROUP BY camp")
        for r in cur.fetchall():
            rds["by_campaign"].setdefault(r["camp"], {})
            rds["by_campaign"][r["camp"]].update({"leads": r["leads"], "converted": int(r["conv"] or 0)})
        # revenue by campaign (lead.email -> order.billing_email, paid)
        cur.execute(f"""SELECT {CAMP} camp, COUNT(DISTINCT o.billing_email) customers, COALESCE(ROUND(SUM(o.total_amount),2),0) revenue
                        FROM wp_citemetrix_score_leads l
                        JOIN wp_wc_orders o ON o.billing_email = l.email
                        WHERE o.type='shop_order' AND o.status IN ('wc-completed','wc-processing') AND o.total_amount>0 AND l.excluded=0
                        AND """ + _not_internal('o.billing_email') + """
                        GROUP BY camp""")
        for r in cur.fetchall():
            rds["by_campaign"].setdefault(r["camp"], {})
            rds["by_campaign"][r["camp"]].update({"customers": r["customers"], "revenue": d2f(r["revenue"])})
        # leads by source/channel (utm_source on the lead)
        cur.execute("SELECT COALESCE(NULLIF(utm_source,''),'(none)') src, COUNT(*) leads, SUM(converted) conv FROM wp_citemetrix_score_leads WHERE excluded=0 GROUP BY src")
        rds["by_source"] = {r["src"]: {"leads": r["leads"], "converted": int(r["conv"] or 0)} for r in cur.fetchall()}
        # revenue by source — the order's OWN stamped source (_cm_utm_source, HPOS meta); populates from new orders
        cur.execute("""SELECT COALESCE(NULLIF(m.meta_value,''),'(none)') src, COUNT(DISTINCT o.id) customers, COALESCE(ROUND(SUM(o.total_amount),2),0) revenue
                       FROM wp_wc_orders o
                       LEFT JOIN wp_wc_orders_meta m ON m.order_id=o.id AND m.meta_key='_cm_utm_source'
                       WHERE o.type='shop_order' AND o.status IN ('wc-completed','wc-processing') AND o.total_amount>0
                       AND """ + _not_internal('o.billing_email') + """
                       GROUP BY src""")
        rds["rev_by_source"] = {r["src"]: {"customers": r["customers"], "revenue": d2f(r["revenue"])} for r in cur.fetchall()}
    conn.close()
except Exception as e:
    notes.append(f"RDS query failed: {e}")

# ─────────────────────────── GA4: sessions / engagement / event funnel ───────────────────────────
ga = {"by_campaign": {}, "landing_pages": [], "channels": {}, "totals": {}}
try:
    from google.oauth2 import service_account
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.analytics.data_v1beta.types import (RunReportRequest, Dimension, Metric, DateRange,
        Filter, FilterExpression, FilterExpressionList)
    creds = service_account.Credentials.from_service_account_file(
        KEY_FILE, scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    client = BetaAnalyticsDataClient(credentials=creds)
    dr = [DateRange(start_date=f"{DAYS}daysAgo", end_date="today")]
    prop = f"properties/{GA4_PROPERTY}"

    def run(dims, mets, dim_filter=None, limit=100000):
        req = RunReportRequest(property=prop, date_ranges=dr,
            dimensions=[Dimension(name=d) for d in dims],
            metrics=[Metric(name=m) for m in mets], limit=limit)
        if dim_filter is not None:
            req.dimension_filter = dim_filter
        return client.run_report(req).rows

    # sessions + engagement by campaign
    for r in run(["sessionCampaignName"], ["sessions", "engagedSessions", "activeUsers"]):
        camp = r.dimension_values[0].value or "(none)"
        ga["by_campaign"].setdefault(camp, {})
        ga["by_campaign"][camp].update({
            "sessions": int(r.metric_values[0].value), "engaged": int(r.metric_values[1].value),
            "users": int(r.metric_values[2].value)})
    # funnel event counts by campaign
    FUNNEL = ["scan_form_focus", "scan_completed", "generate_lead"]
    ev_filter = FilterExpression(filter=Filter(field_name="eventName",
        in_list_filter=Filter.InListFilter(values=FUNNEL)))
    for r in run(["sessionCampaignName", "eventName"], ["eventCount"], ev_filter):
        camp = r.dimension_values[0].value or "(none)"
        ev = r.dimension_values[1].value
        ga["by_campaign"].setdefault(camp, {}).setdefault("events", {})[ev] = int(r.metric_values[0].value)
    # landing pages (top by sessions) + their lead events
    lp = {}
    for r in run(["landingPagePlusQueryString"], ["sessions", "engagedSessions"]):
        pg = r.dimension_values[0].value
        lp[pg] = {"page": pg, "sessions": int(r.metric_values[0].value),
                  "engaged": int(r.metric_values[1].value), "leads": 0}
    lead_filter = FilterExpression(filter=Filter(field_name="eventName",
        string_filter=Filter.StringFilter(value="generate_lead")))
    for r in run(["landingPagePlusQueryString"], ["eventCount"], lead_filter):
        pg = r.dimension_values[0].value
        if pg in lp:
            lp[pg]["leads"] = int(r.metric_values[0].value)
    ga["landing_pages"] = sorted(lp.values(), key=lambda x: x["sessions"], reverse=True)[:10]
    # channel mix (context)
    for r in run(["sessionDefaultChannelGroup"], ["sessions"]):
        ga["channels"][r.dimension_values[0].value or "(other)"] = int(r.metric_values[0].value)
    # totals
    tot = run([], ["sessions", "engagedSessions"])
    if tot:
        ga["totals"] = {"sessions": int(tot[0].metric_values[0].value),
                        "engaged": int(tot[0].metric_values[1].value)}
    # daily sessions for the last DAYS days (sparkline / trend)
    _sd = {}
    for r in run(["date"], ["sessions"]):
        _sd[r.dimension_values[0].value] = int(r.metric_values[0].value)
    _saxis = [(today - datetime.timedelta(days=DAYS-1-i)).strftime("%Y%m%d") for i in range(DAYS)]
    ga["sessions_daily"] = [_sd.get(d, 0) for d in _saxis]
    # nurture CTA click-throughs (utm_source=nurture) by utm_content (spec item 8)
    _nf = FilterExpression(filter=Filter(field_name="sessionSource",
        string_filter=Filter.StringFilter(value="nurture", match_type=Filter.StringFilter.MatchType.EXACT, case_sensitive=False)))
    _nur = {}
    for r in run(["sessionManualAdContent"], ["sessions", "engagedSessions"], _nf):
        _c = r.dimension_values[0].value or "(none)"
        _nur[_c] = {"content": _c, "sessions": int(r.metric_values[0].value),
                    "engaged": int(r.metric_values[1].value), "scans": 0, "leads": 0}
    for _ev, _k in (("scan_completed", "scans"), ("generate_lead", "leads")):
        _ef = FilterExpression(and_group=FilterExpressionList(expressions=[_nf,
            FilterExpression(filter=Filter(field_name="eventName",
                string_filter=Filter.StringFilter(value=_ev)))]))
        for r in run(["sessionManualAdContent"], ["eventCount"], _ef):
            _c = r.dimension_values[0].value or "(none)"
            if _c in _nur:
                _nur[_c][_k] = int(r.metric_values[0].value)
    ga["nurture"] = sorted(_nur.values(), key=lambda x: x["sessions"], reverse=True)
except Exception as e:
    notes.append(f"GA4 query failed: {e}")

# ─────────────────────────── GSC halo (branded series from weekly snapshots) ───────────────────────────
halo = []
for p in sorted(glob.glob(os.path.join(GSC_DIR, "gsc-weekly-*.json"))):
    try:
        j = json.load(open(p))
        halo.append({"week_end": j["window"]["end"], "branded_impr": j["branded"]["impressions"],
                     "branded_clicks": j["branded"]["clicks"], "check_impr": j["check"]["impressions"]})
    except Exception:
        continue

# ─────────────────────────── email (Sendy stats via ingest table, admin_portal DB) ───────────────────────────
email = {}
try:
    aconn = pymysql.connect(host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor)
    with aconn.cursor() as cur:
        cur.execute(f"""SELECT {CAMP} camp, SUM(sent) sent, SUM(opens) opens, SUM(clicks) clicks,
                        SUM(bounced) bounced, SUM(complaints) complaints, SUM(unsubs) unsubs
                        FROM email_campaign_stats GROUP BY camp""")
        for r in cur.fetchall():
            email[r["camp"]] = {"sent": int(r["sent"] or 0), "opens": int(r["opens"] or 0), "clicks": int(r["clicks"] or 0),
                                "bounced": int(r["bounced"] or 0), "complaints": int(r["complaints"] or 0), "unsubs": int(r["unsubs"] or 0)}
    aconn.close()
except Exception as e:
    notes.append(f"email stats read failed: {e}")

# ─────────────────────────── merge campaigns ───────────────────────────
camps = set(rds["by_campaign"]) | set(ga["by_campaign"]) | set(email)
merged = []
for c in camps:
    r = rds["by_campaign"].get(c, {}); g = ga["by_campaign"].get(c, {}); em = email.get(c, {})
    ev = g.get("events", {})
    sessions = g.get("sessions", 0); leads = r.get("leads", 0)
    merged.append({
        "campaign": c,
        "sent": em.get("sent", 0), "opens": em.get("opens", 0), "clicks": em.get("clicks", 0),
        "bounced": em.get("bounced", 0), "complaints": em.get("complaints", 0), "unsubs": em.get("unsubs", 0),
        "sessions": sessions, "engaged": g.get("engaged", 0),
        "form_focus": ev.get("scan_form_focus", 0), "scan_completed": ev.get("scan_completed", 0),
        "lead_events": ev.get("generate_lead", 0),
        "leads": leads, "converted": r.get("converted", 0),
        "customers": r.get("customers", 0), "revenue": r.get("revenue", 0.0),
        "sess_to_lead": round(100.0 * leads / sessions, 1) if sessions else None,
    })
# sort: real campaigns first (by sessions+leads), '(none)' last
merged.sort(key=lambda x: (x["campaign"] == "(none)", -(x["sessions"] + x["leads"] * 50)))

sess_week = ga["totals"].get("sessions", 0)
# leads + revenue rolled up by channel (source)
_srcs = set(rds.get("by_source", {})) | set(rds.get("rev_by_source", {}))
by_source = []
for s in _srcs:
    ls = rds.get("by_source", {}).get(s, {}); rv = rds.get("rev_by_source", {}).get(s, {})
    by_source.append({"source": s, "leads": ls.get("leads", 0), "converted": ls.get("converted", 0),
                      "customers": rv.get("customers", 0), "revenue": rv.get("revenue", 0.0)})
by_source.sort(key=lambda x: (x["source"] == "(none)", -(x["leads"] + x["revenue"])))
# deliverability roll-up (channel-health guardrail) — thresholds: complaint>0.1% or bounce>5% = critical; bounce>3% or unsub>0.5% = warn
_ts = sum(e.get("sent", 0) for e in email.values())
_tb = sum(e.get("bounced", 0) for e in email.values())
_tc = sum(e.get("complaints", 0) for e in email.values())
_tu = sum(e.get("unsubs", 0) for e in email.values())
def _r(x): return round(100.0 * x / _ts, 2) if _ts else 0.0
_br, _cr, _ur = _r(_tb), _r(_tc), _r(_tu)
deliverability = {"sent": _ts, "bounced": _tb, "complaints": _tc, "unsubs": _tu,
                  "bounce_rate": _br, "complaint_rate": _cr, "unsub_rate": _ur,
                  "status": ("critical" if (_cr > 0.1 or _br > 5) else ("warn" if (_br > 3 or _cr > 0.05) else "healthy"))}
# note: status is driven by complaints + bounces (the reputation killers). Unsubs are shown but don't trip the pill — a high unsub is a targeting/content signal, not a blocklist risk.
out = {
    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    "range_days": DAYS,
    "kpis": {**rds.get("kpis", {}), "ga_sessions": ga["totals"].get("sessions", 0),
             "ga_engaged": ga["totals"].get("engaged", 0)},
    "trends": {"leads_daily": rds.get("trends", {}).get("leads_daily", []),
               "leads_cumulative": rds.get("trends", {}).get("leads_cumulative", []),
               "sessions_daily": ga.get("sessions_daily", [])},
    "nurture": ga.get("nurture", []),
    "campaigns": merged,
    "by_source": by_source,
    "deliverability": deliverability,
    "landing_pages": ga["landing_pages"],
    "channels": ga["channels"],
    "halo": halo,
    "notes": notes + ["Email metrics (sent/opened/clicked) pushed daily from the Sendy box (read-only) to the ingest endpoint. Clicks = total click events (can exceed sent).",
                      "Revenue = completed (non-refunded) subscriptions only; internal/test purchases excluded."],
}
os.makedirs(OUT_DIR, exist_ok=True)
json.dump(out, open(os.path.join(OUT_DIR, "campaign-effectiveness.json"), "w"), indent=2, default=str)
print(f"[{out['generated']}] OK — {len(merged)} campaigns, {out['kpis'].get('leads_total',0)} leads total, "
      f"GA4 sessions({DAYS}d)={out['kpis'].get('ga_sessions',0)}, revenue=${out['kpis'].get('revenue',0)}")
if notes:
    print("  notes:", "; ".join(notes))
