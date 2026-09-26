#!/usr/bin/env python3
"""Schema-split addendum to stage 1: drip_send_log.enrollment_id was left NOT NULL
(with a real FK to drip_enrollments, ON DELETE CASCADE) when create_automation_tables.py
added the nullable run_key/email_id/node_key columns for new-engine sends -- discovered
while writing process_due_runs() in stage 3, which has no drip_enrollments row to point
an enrollment_id at for a new-engine send.

Fix: relax enrollment_id to NULL-able. InnoDB FK constraints only validate non-NULL
values, so the existing drip_send_log_ibfk_1 constraint (-> drip_enrollments.id, ON
DELETE CASCADE) stays exactly as-is and keeps protecting every legacy-engine row,
which always supplies a real enrollment_id; only new-engine rows (which fill run_key
instead) will ever have enrollment_id NULL.

Idempotent (checks current nullability first, safe to re-run).

Usage: venv/bin/python migrations/fix_send_log_nullable_enrollment.py [--dry-run]
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
            cur.execute("SHOW COLUMNS FROM drip_send_log LIKE 'enrollment_id'")
            col = cur.fetchone()
            if col["Null"] == "YES":
                print("drip_send_log.enrollment_id already nullable -- skipping")
            else:
                print("Relaxing drip_send_log.enrollment_id to NULL-able")
                if not DRY_RUN:
                    cur.execute("ALTER TABLE drip_send_log MODIFY COLUMN enrollment_id INT NULL")

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
