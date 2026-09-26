#!/usr/bin/env python3
"""Extends lead_events.type (a MySQL ENUM) with 'contact_form_submitted', needed by
migrations/migrate_contact_entries.py. Demo requests reuse the already-existing
'meeting_booked' value instead -- no ENUM change needed there.

Grepped app.py/leads_drip.py for hardcoded lead_events.type value lists first -- none found,
safe to extend.

Usage: venv/bin/python migrations/extend_lead_events_type.py [--dry-run]
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

NEW_ENUM = (
    "ALTER TABLE lead_events MODIFY type ENUM("
    "'sourced','enrolled','touch_sent','opened','link_clicked','replied','bounced',"
    "'unsubscribed','complained','meeting_booked','stage_changed','survey_submitted',"
    "'free_check_completed','note','contact_form_submitted') NOT NULL"
)


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
                "WHERE table_schema = DATABASE() AND table_name='lead_events' AND column_name='type'"
            )
            current = cur.fetchone()["COLUMN_TYPE"]
            if "contact_form_submitted" in current:
                print(f"lead_events.type already extended -- skipping (current: {current})")
            else:
                print(f"Extending lead_events.type ENUM (current: {current})")
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
