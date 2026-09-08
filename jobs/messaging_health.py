#!/usr/bin/env python3
"""Messaging Platform health — PWA delivery (app.citemetrix.com) + push dispatch (bitnami PRD).

Delivery: public endpoint checks + SSL expiry (curl/ssl) + box internals over SSH (forced-command
id_app_monitor -> pwa-health.sh). Dispatch: RDS (VAPID / subscriptions / fast-lane fire) + web-push
vendor over SSH (forced-command id_dispatch_monitor -> cm-dispatch-health.sh). Writes
reports/messaging/health.json. Read-only. Run every few minutes via cron."""
import os, sys, json, subprocess, datetime, ssl, socket, time

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
OUT_DIR = os.path.join(BASE, "reports", "messaging")
from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

APP_HOST    = "app.citemetrix.com"
APP_SSH     = "ubuntu@16.58.149.231"
BITNAMI_SSH = "bitnami@172.26.14.201"
KEY_APP  = os.path.expanduser("~/.ssh/id_app_monitor")
KEY_DISP = os.path.expanduser("~/.ssh/id_dispatch_monitor")


def ssh_kv(key, target):
    out = {}
    try:
        r = subprocess.run(["ssh", "-i", key, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                            "-o", "StrictHostKeyChecking=accept-new", target],
                           capture_output=True, text=True, timeout=25)
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
        if not out and r.returncode != 0:
            out["_error"] = (r.stderr or "ssh failed").strip()[:120]
    except Exception as e:
        out["_error"] = str(e)[:120]
    return out


def http_code(url):
    try:
        r = subprocess.run(["curl", "-sk", "-m", "15", "-o", "/dev/null", "-w", "%{http_code}", url],
                           capture_output=True, text=True, timeout=20)
        return int(r.stdout.strip() or 0)
    except Exception:
        return 0


def ssl_days_left(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                cert = ss.getpeercert()
        exp = datetime.datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z")
        return (exp - datetime.datetime.utcnow()).days
    except Exception:
        return None


def db():
    return pymysql.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=10)


# ── delivery ──────────────────────────────────────────────────────────────
delivery = {"endpoints": {}}
for path in ["/", "/service-worker.js", "/manifest.json"]:
    delivery["endpoints"][path] = http_code("https://%s%s" % (APP_HOST, path))
delivery["ssl_days_left"] = ssl_days_left(APP_HOST)
delivery["box"] = ssh_kv(KEY_APP, APP_SSH)
_ep_ok = all(c == 200 for c in delivery["endpoints"].values())
_ssl_ok = (delivery["ssl_days_left"] is not None and delivery["ssl_days_left"] > 14)
_nginx_ok = delivery["box"].get("nginx") == "active"
delivery["status"] = "healthy" if (_ep_ok and _ssl_ok and _nginx_ok) else ("warn" if (_ep_ok and _nginx_ok) else "critical")

# ── dispatch ──────────────────────────────────────────────────────────────
dispatch = {"vendor": ssh_kv(KEY_DISP, BITNAMI_SSH)}
try:
    conn = db()
    with conn.cursor() as cur:
        cur.execute("SELECT IF(option_value IS NOT NULL AND option_value<>'','yes','no') v FROM wp_options WHERE option_name='cm_pwa_vapid_keypair'")
        r = cur.fetchone(); dispatch["vapid_present"] = (r["v"] if r else "no")
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_pwa_subscriptions WHERE status='active'")
        dispatch["active_subs"] = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_pwa_subscriptions")
        dispatch["total_subs"] = cur.fetchone()["n"]
        cur.execute("SELECT MAX(last_push_at) m FROM wp_citemetrix_pwa_subscriptions")
        dispatch["last_push_at"] = cur.fetchone()["m"]
        cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_pwa_subscriptions WHERE failure_count >= 3")
        dispatch["failing_subs"] = cur.fetchone()["n"]
        cur.execute("SELECT MAX(CAST(option_value AS UNSIGNED)) m FROM wp_options "
                    "WHERE option_name IN ('cm_last_run_citemetrix_process_scan_queue','cm_last_run_citemetrix_process_analysis_queue')")
        r = cur.fetchone(); dispatch["fast_lane_epoch"] = int(r["m"]) if r and r["m"] else None
    conn.close()
except Exception as e:
    dispatch["_db_error"] = str(e)[:160]

_fl_age = (int(time.time()) - dispatch["fast_lane_epoch"]) if dispatch.get("fast_lane_epoch") else None
dispatch["fast_lane_age_sec"] = _fl_age
_vendor_ok = dispatch["vendor"].get("webpush_vendor") == "present"
_vapid_ok = dispatch.get("vapid_present") == "yes"
_fl_ok = (_fl_age is not None and _fl_age < 180)
_subs_ok = (dispatch.get("active_subs", 0) > 0)
if _vendor_ok and _vapid_ok and _fl_ok:
    dispatch["status"] = "healthy" if _subs_ok else "warn"
else:
    dispatch["status"] = "critical"

out = {"generated": datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z",
       "delivery": delivery, "dispatch": dispatch}
os.makedirs(OUT_DIR, exist_ok=True)
json.dump(out, open(os.path.join(OUT_DIR, "health.json"), "w"), indent=2, default=str)
print("messaging health OK — delivery=%s dispatch=%s (fast_lane_age=%ss)" % (delivery["status"], dispatch["status"], _fl_age))
