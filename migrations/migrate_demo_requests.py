#!/usr/bin/env python3
"""Sync Bookly demo-request bookings (wp_bookly_appointments joined through
wp_bookly_customer_appointments to wp_bookly_customers for the actual name/email/phone --
confirmed schema, Bookly keeps appointment and customer identity in separate tables) into
admin_portal.leads, per Eric's explicit requirement: "Website contact forms of all types have
to create leads." Demo requests are the other real lead-capture surface beyond the contact
form, previously only ever read for display (/sales/demo-requests) and never synced anywhere.

Same shape as migrate_step1b.py/migrate_contact_entries.py: full table re-scan every run (only
a handful of rows), idempotent via upsert on (legacy_source_table, legacy_id) keyed to the
Bookly appointment id, new-vs-existing detected before the upsert so only a genuinely new
appointment gets a source_refs row + tags.

original_source='demo_request' (leads.original_source ENUM extended for this,
migrations/create_tags_segments_tables.py). Tag: channel:demo_request.

Usage: venv/bin/python migrations/migrate_demo_requests.py [--dry-run]
"""
import os
import sys
import json
import datetime
import secrets

DRY_RUN = "--dry-run" in sys.argv
UNSUB_TOKEN_LEN = 32

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql
import leads_drip

LEGACY_TABLE = "wp_bookly_appointments"


def admin_db():
    return pymysql.connect(
        host=os.getenv("ADMIN_DB_HOST", "localhost"), user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def product_db():
    return pymysql.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


# One row per (appointment, customer) pair -- in practice a demo booking has exactly one
# customer, but the join is written to tolerate more than one without erroring; legacy_id is
# keyed to the appointment id, so a second customer on the same appointment would collide on
# the upsert and simply be skipped by the is_new_lead check, not double-counted.
QUERY = """
    SELECT a.id AS appointment_id, a.start_date, a.created_at,
           c.full_name, c.first_name, c.last_name, c.email, c.phone
    FROM wp_bookly_appointments a
    JOIN wp_bookly_customer_appointments ca ON ca.appointment_id = a.id
    JOIN wp_bookly_customers c ON c.id = ca.customer_id
    ORDER BY a.id
"""


def main():
    snapshot_at = datetime.datetime.now().replace(microsecond=0)
    print(f"[{snapshot_at}] Demo-request sync {'(DRY RUN)' if DRY_RUN else ''}")

    prod = product_db()
    with prod.cursor() as pcur:
        pcur.execute(QUERY)
        appointments = pcur.fetchall()
    prod.close()

    print(f"Read {len(appointments)} appointment/customer row(s) from {LEGACY_TABLE}")

    admin = admin_db()
    with admin.cursor() as acur:
        migrated = 0
        new_leads = 0

        for r in appointments:
            if not r.get("email"):
                continue

            if DRY_RUN:
                migrated += 1
                continue

            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["appointment_id"])
            )
            is_new_lead = acur.fetchone() is None
            unsubscribe_token = secrets.token_urlsafe(UNSUB_TOKEN_LEN)

            acur.execute(
                """INSERT INTO leads
                     (email, first_name, last_name, phone, original_source, created_at,
                      unsubscribe_token, legacy_id, legacy_source_table)
                   VALUES (%s,%s,%s,%s,'demo_request',%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE email=VALUES(email)""",
                (r["email"], r.get("first_name") or "", r.get("last_name") or "", r.get("phone"),
                 r["created_at"], unsubscribe_token, r["appointment_id"], LEGACY_TABLE)
            )
            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["appointment_id"])
            )
            lead_id = acur.fetchone()["id"]

            acur.execute(
                "SELECT id FROM lead_events WHERE lead_id=%s AND type='meeting_booked'", (lead_id,)
            )
            if not acur.fetchone():
                acur.execute(
                    """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                       VALUES (%s,%s,'meeting_booked','web',%s)""",
                    (lead_id, r["created_at"], json.dumps({"appointment_start": r.get("start_date")}, default=str))
                )

            if is_new_lead:
                new_leads += 1
                acur.execute(
                    "INSERT INTO source_refs (path, kind, label, source) VALUES ('demo_request','signup',%s,'book_demo')",
                    (r.get("full_name") or r["email"],)
                )
                source_ref_id = acur.lastrowid
                acur.execute(
                    "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,'demo_request',%s)",
                    (lead_id, source_ref_id)
                )
                acur.execute("UPDATE leads SET original_source_ref_id=%s WHERE id=%s", (source_ref_id, lead_id))
                leads_drip.tag_lead(acur, lead_id, "channel:demo_request")

            migrated += 1

        if not DRY_RUN:
            admin.commit()
            acur.execute(
                "INSERT INTO cron_run_log (job_name, total_processed, total_problem, detail_json) VALUES (%s,%s,0,%s)",
                ("demo_requests_sync", migrated, json.dumps({"new_leads": new_leads}))
            )
            admin.commit()

    admin.close()
    print(f"Processed {migrated} row(s), {new_leads} new lead(s).")
    if DRY_RUN:
        print("--dry-run: no writes made")


if __name__ == "__main__":
    main()
