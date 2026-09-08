#!/usr/bin/env python3
"""Lead Drip Campaigns — sends whatever step is due right now for active
enrollments, individually via SES. Run hourly via cron, same cadence as
the WP-side drip/nurture engines. Standalone script (not Flask HTTP),
matching the convention of the other jobs/*.py in this directory.
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))

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
    """Read-only cross-box connection to the WP product DB -- same env vars
    (DB_HOST/DB_USER/DB_PASSWORD/DB_NAME) migrations/migrate_step1b.py's own
    product_db() already uses; admin_portal has had these credentials
    configured since that migration, nothing new to provision. Needed here
    so process_check_completions() can look up citemetrix_freecheck_log
    rows by drip_lead_token -- see that function's docstring."""
    return pymysql.connect(
        host=os.getenv('DB_HOST'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
        database=os.getenv('DB_NAME'),
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
    )


def _send(to, subject, body_text, body_html=None):
    return send_email(
        to=to, subject=subject, body_text=body_text, body_html=body_html,
        from_address=SES_FROM_ADDRESS,
        configuration_set=SES_CONFIGURATION_SET,
    )


def main():
    conn = get_admin_db()
    product_conn = get_product_db()
    try:
        with conn.cursor() as cur, product_conn.cursor() as pcur:
            # Branch cold->warm BEFORE sending anything due this run -- a step that would
            # otherwise go out as one more cold email instead goes out as the matching warm
            # campaign's Step 1 (CITEMETRIX-Drip-Campaigns.md branching summary, point 2).
            branch_result = leads_drip.process_check_completions(cur, conn, pcur)
            result = leads_drip.process_due_enrollments(cur, conn, _send, BASE_URL, max_sends=50)
    finally:
        conn.close()
        product_conn.close()
    ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{ts}] branch_checked={branch_result['cold_active_checked']} branched={branch_result['branched']} | "
          f"checked={result['checked']} sent={result['sent']} failed={result['failed']} skipped_unsub={result['skipped_unsubscribed']} skipped_suppressed={result['skipped_suppressed']} skipped_blocked={result['skipped_blocked']}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        from cron_alert import alert_and_exit
        alert_and_exit('drip_cron.py', e)
