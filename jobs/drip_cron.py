#!/usr/bin/env python3
"""Lead Drip Campaigns — sends whatever step is due right now for active
enrollments, individually via SES. Run hourly via cron, same cadence as
the WP-side drip/nurture engines. Standalone script (not Flask HTTP),
matching the convention of the other jobs/*.py in this directory.

2026-09-25: the legacy engine (process_due_enrollments/recover_blocked_
enrollments/process_check_completions, the drip_enrollments-table path)
was retired -- every drip_campaigns row has migrated_automation_id set,
so those functions had become permanently inert (their own queries
require migrated_automation_id IS NULL) and were removed from
leads_drip.py. This job now only runs the new engine (automation_runs).
The drip_enrollments/drip_campaigns/drip_steps tables and their historical
data are untouched -- only the dead send/branch code paths are gone.
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))

import json
import pymysql
from email_helper import send_email
import leads_drip

SES_FROM_ADDRESS = 'eric@citemetrix.com'
SES_CONFIGURATION_SET = 'citemetrix-tracking'
BASE_URL = 'https://admin.citemetrix.com'


def get_admin_db():
    return pymysql.connect(
        host=os.getenv('ADMIN_DB_HOST', 'localhost'),
        user=os.getenv('ADMIN_DB_USER', 'root'),
        password=os.getenv('ADMIN_DB_PASSWORD'),
        database=os.getenv('ADMIN_DB_NAME', 'admin_portal'),
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
    )


def get_product_db():
    """Cross-box connection to the WP product DB -- same env vars
    (DB_HOST/DB_USER/DB_PASSWORD/DB_NAME) migrations/migrate_step1b.py's own
    product_db() already uses; admin_portal has had these credentials
    configured since that migration, nothing new to provision. Needed here
    so process_check_completions_runs() can look up citemetrix_freecheck_log
    rows by drip_lead_token -- see that function's docstring.

    Also writes wp_citemetrix_score_leads.nurture_stood_down=1 through this same
    connection when a warm-track enrollment succeeds (warm-track priority
    decision -- WordPress's generic nurture must actually stop, not just
    get outvoted). Kept as one connection rather than adding a second,
    write-scoped one -- this box's DB_USER already has full write access
    (it's the same user WordPress itself uses), so a separate connection
    would add complexity without a real permissions boundary behind it."""
    return pymysql.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
    )


def _send(to, subject, body_text, body_html=None, list_unsubscribe=None):
    return send_email(
        to=to, subject=subject, body_text=body_text, body_html=body_html,
        from_address=SES_FROM_ADDRESS,
        configuration_set=SES_CONFIGURATION_SET,
        list_unsubscribe=list_unsubscribe,
    )


def _log_run(cur, conn, branch_result, result):
    """CODE-BRIEF-WARM-NURTURE-NEVER-SENT-2026-09-11.md SS5: a run that completes
    without error but blocks/suppresses real sends must not look identical, from the
    portal's point of view, to a run with nothing to do. The cron.log line below has
    always carried this data -- it just had no reader. This is the reader: one row per
    run, cheap enough to write every hour forever, queried by the dashboard's job-health
    check the same way CRON_STALENESS_CHECKS already reads log mtimes. total_problem is
    what that check keys off; 0 means a clean run even if checked==0 (nothing was due)."""
    total_problem = result['failed'] + result['skipped_blocked']
    detail = {**branch_result, **result}
    cur.execute(
        "INSERT INTO cron_run_log (job_name, total_processed, total_problem, detail_json) VALUES (%s,%s,%s,%s)",
        ('drip_cron', result['checked'], total_problem, json.dumps(detail))
    )
    conn.commit()


def main():
    conn = get_admin_db()
    product_conn = get_product_db()
    try:
        with conn.cursor() as cur, product_conn.cursor() as pcur:
            # New engine's three phases: branch cold->warm before sending anything due
            # this run, recover any run stuck 'blocked' (so a lead that clears this run
            # gets its due step sent same-cycle), then send.
            branch_result = leads_drip.process_check_completions_runs(cur, conn, pcur, product_conn)
            recovery_result = leads_drip.recover_blocked_runs(cur, conn, pcur, product_conn)
            branch_result = {**branch_result, **recovery_result}
            result = leads_drip.process_due_runs(cur, conn, _send, BASE_URL, max_sends=50)

            _log_run(cur, conn, branch_result, result)
    finally:
        conn.close()
        product_conn.close()
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] branch_checked={branch_result['cold_active_checked']} branched={branch_result['branched']} "
          f"blocked_checked={branch_result['blocked_checked']} recovered={branch_result['recovered']} | "
          f"checked={result['checked']} sent={result['sent']} failed={result['failed']} skipped_unsub={result['skipped_unsubscribed']} skipped_suppressed={result['skipped_suppressed']} skipped_blocked={result['skipped_blocked']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        from cron_alert import alert_and_exit
        alert_and_exit('drip_cron.py', e)
