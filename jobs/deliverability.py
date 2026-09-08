#!/usr/bin/env python3
"""Deliverability data job — AWS SES account sending health for the portal
Marketing -> Deliverability page (Amazon SES tab).

Pulls (SES IAM creds from .env, us-east-1): 24h quota + usage + send rate, ~2 weeks of send
statistics (delivery attempts / bounces / complaints aggregated + daily trend), account
enforcement status, and suppression-list count. Writes reports/deliverability/ses-latest.json.

On a full (non --fast) run, also mirrors the suppression list's actual per-address rows into
admin_portal.ses_suppressions (email_address, reason, last_update_time) -- previously this data
was fetched from AWS and thrown away, keeping only a count. That address-level data is what a
future per-lead suppression_reason field needs; this job is now its source. Each full run is a
sync: addresses returned by AWS are upserted, and any row not touched by this run (no longer on
AWS's list) is pruned -- the table always reflects AWS's current state, not an ever-growing log.

The Sendy per-campaign tab is read live from the admin_portal.email_campaign_stats table in the
route (fast local DB), so it is NOT part of this job. Read-only against SES. Usage:
  cd /var/www/admin-portal && venv/bin/python jobs/deliverability.py
"""
import os, sys, json, datetime, collections, time

# --fast: skip the slow suppression-list pagination (carry the last count forward). Used by the
# on-demand Refresh button; the daily cron runs without it to recount the suppression list.
FAST = "--fast" in sys.argv

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
OUT_DIR = os.path.join(BASE, "reports", "deliverability")

_prev_suppressed = None
try:
    _prev_suppressed = json.load(open(os.path.join(OUT_DIR, "ses-latest.json"))).get("ses", {}).get("suppressed")
except Exception:
    pass

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import boto3
import pymysql


def _admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.Cursor,
    )

out = {"generated": datetime.datetime.now().isoformat(timespec="seconds"), "ses": {}, "notes": []}

try:
    region = os.getenv("AWS_REGION") or "us-east-1"
    ses = boto3.client("ses", region_name=region)
    q = ses.get_send_quota()
    st = ses.get_send_statistics().get("SendDataPoints", [])

    agg = {k: int(sum(d.get(k, 0) for d in st)) for k in ["DeliveryAttempts", "Bounces", "Complaints", "Rejects"]}
    da = agg["DeliveryAttempts"] or 1

    daily = collections.defaultdict(lambda: {"attempts": 0, "bounces": 0, "complaints": 0})
    for d in st:
        ts = d.get("Timestamp")
        day = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
        daily[day]["attempts"]   += int(d.get("DeliveryAttempts", 0))
        daily[day]["bounces"]    += int(d.get("Bounces", 0))
        daily[day]["complaints"] += int(d.get("Complaints", 0))
    trend = [{"date": k, "attempts": v["attempts"], "bounces": v["bounces"], "complaints": v["complaints"]}
             for k, v in sorted(daily.items())]

    # sesv2: enforcement status (fast) + suppression-list count (rate-limited: adaptive retries + pacing)
    status, suppressed = None, None
    try:
        from botocore.config import Config
        v2 = boto3.client("sesv2", region_name=region,
                          config=Config(retries={"max_attempts": 8, "mode": "adaptive"}))
        try:
            status = v2.get_account().get("EnforcementStatus")
        except Exception as e:
            out["notes"].append(f"ses_account: {str(e)[:100]}")
        if FAST:
            suppressed = _prev_suppressed  # carry forward; daily cron recounts
        else:
            try:
                # microsecond=0: the DATETIME column has no fractional-second precision, so a
                # full-precision Python value here would store truncated and then compare as
                # LESS than this same variable, making every freshly upserted row look stale.
                sync_started_at = datetime.datetime.now().replace(microsecond=0)
                db = _admin_db()
                db.autocommit(False)
                cnt, tok, pages, paginated_fully = 0, None, 0, False
                with db.cursor() as cur:
                    while pages < 120:
                        kw = {"PageSize": 1000}
                        if tok:
                            kw["NextToken"] = tok
                        r = v2.list_suppressed_destinations(**kw)
                        rows = r.get("SuppressedDestinationSummaries", [])
                        cnt += len(rows)
                        if rows:
                            cur.executemany(
                                """INSERT INTO ses_suppressions (email_address, reason, last_update_time, synced_at)
                                   VALUES (%s, %s, %s, %s)
                                   ON DUPLICATE KEY UPDATE reason=VALUES(reason),
                                       last_update_time=VALUES(last_update_time), synced_at=VALUES(synced_at)""",
                                [(row["EmailAddress"], row["Reason"], row["LastUpdateTime"], sync_started_at) for row in rows]
                            )
                        tok = r.get("NextToken")
                        pages += 1
                        if not tok:
                            paginated_fully = True
                            break
                        time.sleep(1.1)  # ListSuppressedDestinations limit is ~1 req/sec

                    # Only prune -- and only inside the SAME transaction as the upserts above -- if
                    # pagination reached the end naturally. An incomplete run (exception mid-loop, or
                    # the 120-page safety cap) means most of AWS's list was never seen this pass, so
                    # nothing "not touched" this run is actually evidence it left AWS's suppression
                    # list -- pruning on incomplete data would wrongly mark real bad addresses mailable.
                    pruned = 0
                    if paginated_fully:
                        cur.execute("SELECT COUNT(*) FROM ses_suppressions")
                        existing = cur.fetchone()[0]
                        cur.execute("SELECT COUNT(*) FROM ses_suppressions WHERE synced_at < %s", (sync_started_at,))
                        stale = cur.fetchone()[0]
                        if existing > 0 and stale > existing * 0.10:
                            out["notes"].append(
                                f"ses_suppressions: prune ABORTED -- would delete {stale}/{existing} "
                                f"({round(100*stale/existing,1)}%), over the 10% safety threshold. Upserts still committed."
                            )
                        else:
                            cur.execute("DELETE FROM ses_suppressions WHERE synced_at < %s", (sync_started_at,))
                            pruned = cur.rowcount
                    else:
                        out["notes"].append(f"ses_suppressions: pagination incomplete after {pages} page(s) -- prune skipped, upserts still committed")
                    db.commit()  # upserts (and the prune, if it ran) commit together
                db.close()
                suppressed = cnt if paginated_fully else _prev_suppressed
                out["notes"].append(f"ses_suppressions: synced {cnt} row(s) seen this run, pruned {pruned} stale row(s)")
            except Exception as e:
                try:
                    db.rollback()
                    db.close()
                except Exception:
                    pass
                out["notes"].append(f"ses_suppression: {str(e)[:100]}")
                suppressed = _prev_suppressed
    except Exception as e:
        out["notes"].append(f"sesv2_client: {str(e)[:100]}")

    out["ses"] = {
        "region": region,
        "quota": {"max_24h": int(q["Max24HourSend"]), "sent_24h": int(q["SentLast24Hours"]),
                  "rate": int(q["MaxSendRate"]),
                  "usage_pct": round(100 * q["SentLast24Hours"] / (q["Max24HourSend"] or 1), 2)},
        "window": {"attempts": agg["DeliveryAttempts"], "bounces": agg["Bounces"],
                   "complaints": agg["Complaints"], "rejects": agg["Rejects"],
                   "bounce_rate": round(100 * agg["Bounces"] / da, 3),
                   "complaint_rate": round(100 * agg["Complaints"] / da, 3),
                   "datapoints": len(st)},
        "trend": trend,
        "enforcement_status": status,
        "suppressed": suppressed,
    }
except Exception as e:
    out["notes"].append(f"SES failed: {str(e)[:160]}")

os.makedirs(OUT_DIR, exist_ok=True)
json.dump(out, open(os.path.join(OUT_DIR, "ses-latest.json"), "w"), indent=2, default=str)
w = out["ses"].get("window", {})
print(f"[{out['generated']}] deliverability OK — bounce={w.get('bounce_rate','?')}% complaint={w.get('complaint_rate','?')}% "
      f"status={out['ses'].get('enforcement_status','?')} suppressed={out['ses'].get('suppressed','?')} notes={out['notes']}")
