#!/usr/bin/env python3
"""Weekly Google Search Console baseline for CiteMetrix — runs on the admin-portal server via cron.

Writes two artifacts to reports/gsc/ per run:
  gsc-weekly-<END>.json   structured data (rendered by the portal /reports/gsc route)
  gsc-weekly-<END>.txt    plain-text report (human-readable / email)

Branded-search lift from email campaigns typically shows 1-2 weeks after sends; each weekly run
records a dated snapshot and computes week-over-week deltas vs the previous JSON.

Cron: 17 9 * * 1  cd /var/www/admin-portal && venv/bin/python jobs/gsc_weekly.py >> reports/gsc/cron.log 2>&1
Read-only against GSC. Uses gsc-sa-key.json (service account, webmasters.readonly).
"""
import os, sys, json, glob, datetime

HERE       = os.path.dirname(os.path.abspath(__file__))
KEY_FILE   = os.path.join(HERE, "..", "gsc-sa-key.json")
REPORT_DIR = os.path.join(HERE, "..", "reports", "gsc")
BRAND_RE   = "(?i)cite ?metrix"

end   = datetime.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else (datetime.date.today() - datetime.timedelta(days=3))
start = end - datetime.timedelta(days=6)
START, END = start.isoformat(), end.isoformat()

from googleapiclient.discovery import build
from google.oauth2 import service_account

creds = service_account.Credentials.from_service_account_file(
    KEY_FILE, scopes=["https://www.googleapis.com/auth/webmasters.readonly"])
svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)

sites = svc.sites().list().execute().get("siteEntry", [])
cm = [s["siteUrl"] for s in sites if "citemetrix" in s["siteUrl"].lower()]
if not cm:
    print(f"[{datetime.datetime.now()}] ERROR: no citemetrix GSC property visible to service account")
    sys.exit(2)
SITE = sorted(cm, key=lambda u: (not u.startswith("sc-domain:"), len(u)))[0]

def query(dimensions, filters=None, row_limit=25):
    body = {"startDate": START, "endDate": END, "dimensions": dimensions, "rowLimit": row_limit}
    if filters:
        body["dimensionFilterGroups"] = [{"filters": filters}]
    return svc.searchanalytics().query(siteUrl=SITE, body=body).execute().get("rows", [])

def totals(filters):
    rows = query([], filters=filters)
    if not rows:
        return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
    r = rows[0]
    return {"clicks": int(r.get("clicks", 0)), "impressions": int(r.get("impressions", 0)),
            "ctr": round(r.get("ctr", 0.0), 4), "position": round(r.get("position", 0.0), 1)}

branded = totals([{"dimension": "query", "operator": "includingRegex", "expression": BRAND_RE}])
check   = totals([{"dimension": "page",  "operator": "contains",       "expression": "/check/"}])
nb = query(["query"], filters=[{"dimension": "query", "operator": "excludingRegex", "expression": BRAND_RE}], row_limit=200)
nb.sort(key=lambda r: int(r.get("impressions", 0)), reverse=True)
nonbranded = [{"query": r["keys"][0], "impressions": int(r.get("impressions", 0)),
               "clicks": int(r.get("clicks", 0)), "position": round(r.get("position", 0.0), 1)} for r in nb[:20]]

# week-over-week delta vs the most recent prior JSON
prev = None
for p in sorted(glob.glob(os.path.join(REPORT_DIR, "gsc-weekly-*.json")), reverse=True):
    try:
        d = json.load(open(p))
        if d.get("window", {}).get("end") != END:
            prev = d
            break
    except Exception:
        continue

def delta(cur, key):
    if not prev:
        return None
    return cur - prev.get("branded", {}).get(key, 0)

data = {
    "generated": datetime.datetime.now().isoformat(timespec="seconds"),
    "site": SITE,
    "window": {"start": START, "end": END},
    "branded": branded,
    "branded_delta": {"impressions": delta(branded["impressions"], "impressions"),
                      "clicks": delta(branded["clicks"], "clicks")} if prev else None,
    "prev_window_end": prev["window"]["end"] if prev else None,
    "check": check,
    "nonbranded": nonbranded,
}

os.makedirs(REPORT_DIR, exist_ok=True)
jpath = os.path.join(REPORT_DIR, f"gsc-weekly-{END}.json")
json.dump(data, open(jpath, "w"), indent=2)

# text report
L = []
L.append(f"GSC WEEKLY BASELINE — {SITE}")
L.append(f"window {START} .. {END}  (generated {data['generated']})")
L.append("=" * 74)
dz = data["branded_delta"]
dtxt = ""
if dz:
    dtxt = f"   (WoW vs {data['prev_window_end']}: impr {dz['impressions']:+d}, clicks {dz['clicks']:+d})"
L += ["", "BRANDED SEARCH (queries matching 'cite ?metrix')" + dtxt,
      f"  impressions : {branded['impressions']:>8,}",
      f"  clicks      : {branded['clicks']:>8,}",
      f"  CTR         : {branded['ctr']*100:>7.2f}%",
      f"  avg position: {branded['position']:>7.1f}"]
L += ["", "/check/ PAGE",
      f"  impressions : {check['impressions']:>8,}",
      f"  clicks      : {check['clicks']:>8,}",
      f"  avg position: {check['position']:>7.1f}"]
L += ["", "TOP 20 NON-BRANDED QUERIES (by impressions)",
      f"  {'query':44} {'impr':>7} {'clicks':>7} {'pos':>6}"]
for r in nonbranded:
    L.append(f"  {r['query'][:44]:44} {r['impressions']:>7,} {r['clicks']:>7,} {r['position']:>6.1f}")
report = "\n".join(L)
open(os.path.join(REPORT_DIR, f"gsc-weekly-{END}.txt"), "w").write(report + "\n")

print(f"[{data['generated']}] OK {SITE} window {START}..{END} — branded impr={branded['impressions']} clicks={branded['clicks']}"
      + (f" (WoW impr {dz['impressions']:+d})" if dz else " (no prior week)"))
