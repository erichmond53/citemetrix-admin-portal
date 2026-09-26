#!/usr/bin/env python3
"""Extends source_refs.path and lead_source_touches.path -- both MySQL ENUMs mirroring
leads.original_source's own value set -- with 'contact_form' and 'demo_request', needed by
migrations/migrate_contact_entries.py and migrations/migrate_demo_requests.py.
(leads.original_source itself was already extended by create_tags_segments_tables.py.)

Grepped app.py/leads_drip.py for hardcoded path-enum value lists first -- none found, safe to
extend.

Usage: venv/bin/python migrations/extend_path_enums.py [--dry-run]
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

NEW_ENUM = "ENUM('in_person','direct','inbound','beta','chat','contact_form','demo_request') NOT NULL"
ALTERS = {
    "source_refs": f"ALTER TABLE source_refs MODIFY path {NEW_ENUM}",
    "lead_source_touches": f"ALTER TABLE lead_source_touches MODIFY path {NEW_ENUM}",
}


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
            for table, alter_sql in ALTERS.items():
                cur.execute(
                    "SELECT COLUMN_TYPE FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name=%s AND column_name='path'",
                    (table,)
                )
                current = cur.fetchone()["COLUMN_TYPE"]
                if "contact_form" in current and "demo_request" in current:
                    print(f"{table}.path already extended -- skipping (current: {current})")
                else:
                    print(f"Extending {table}.path ENUM (current: {current})")
                    if not DRY_RUN:
                        cur.execute(alter_sql)

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
