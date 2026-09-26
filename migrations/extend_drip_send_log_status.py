#!/usr/bin/env python3
"""Extends drip_send_log.status (ENUM) with 'rate_limited', for the per-recipient send-ceiling
check (work-order §7.4 / §8.6 -- the one item both Code and Chat independently flagged as most
urgent: neither enroll_segment() nor process_due_runs()/process_due_enrollments() capped how many
times one address could be sent to, and today's build added three more ways to reach the same
lead than existed during incidents/nurture-email-runaway-2026-09-21.md's 8,469-sends incident).

Kept as its own distinct status rather than reusing 'blocked' (which already means specifically
"lead_pool.is_eligible() failed") -- a volume-cap block and an eligibility-gate block are
different failure reasons and this codebase has consistently kept those distinguishable in
drip_send_log rather than collapsing them (see e.g. skip_reasons in migrate_step1b.py).

Grepped app.py/leads_drip.py for hardcoded drip_send_log.status value lists first -- none found,
safe to extend.

Usage: venv/bin/python migrations/extend_drip_send_log_status.py [--dry-run]
"""
import os
import sys

DRY_RUN = "--dry-run" in sys.argv

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

NEW_ENUM = "ALTER TABLE drip_send_log MODIFY status ENUM('sent','failed','suppressed','blocked','cancelled','rate_limited') NOT NULL"


def admin_db():
    return pymysql.connect(
        host=os.getenv("ADMIN_DB_HOST", "localhost"), user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COLUMN_TYPE FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name='drip_send_log' AND column_name='status'"
            )
            current = cur.fetchone()["COLUMN_TYPE"]
            if "rate_limited" in current:
                print(f"drip_send_log.status already extended -- skipping (current: {current})")
            else:
                print(f"Extending drip_send_log.status ENUM (current: {current})")
                if not DRY_RUN:
                    cur.execute(NEW_ENUM)

        if DRY_RUN:
            print("--dry-run: rolling back")
            conn.rollback()
        else:
            conn.commit()
            print("Committed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
