#!/usr/bin/env python3
"""Sync wp_cm_contact_entries (the citemetrix plugin's own [citemetrix_contact_form]
shortcode, the ONLY real contact form on the live site -- confirmed via direct investigation:
no CF7/Gravity/WPForms installed, no other hand-rolled form anywhere else on the site) into
admin_portal.leads, per Eric's explicit requirement: "Website contact forms of all types have
to create leads."

Same shape as migrate_step1b.py/migrate_beta_signups.py/migrate_chatly_captures.py: full
table re-scan every run (the source table is tiny -- a handful of rows), idempotent via
upsert on (legacy_source_table, legacy_id), a genuinely-new row gets a source_refs row +
lead_source_touches + tags; a re-synced existing row does not (matches migrate_step1b.py's
own "must know new-vs-existing BEFORE the upsert" reasoning).

original_source='contact_form' (leads.original_source ENUM extended for this,
migrations/create_tags_segments_tables.py). Tags: channel:contact_form always, plus
subject:<slug> from the contact form's own subject dropdown -- real targeting signal already
captured on the form, essentially free to carry through into a segment-filterable tag.

Deliberately excludes wp_citemetrix_barrier_survey_responses -- that table hashes email, is
anonymous market research, not an identity-bearing lead source.

Usage: venv/bin/python migrations/migrate_contact_entries.py [--dry-run]
"""
import os
import re
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

LEGACY_TABLE = "wp_cm_contact_entries"


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


def _slugify(s):
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:60] or "other"


def _split_name(full_name):
    parts = (full_name or "").strip().split(None, 1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def main():
    snapshot_at = datetime.datetime.now().replace(microsecond=0)
    print(f"[{snapshot_at}] Contact-entries sync {'(DRY RUN)' if DRY_RUN else ''}")

    prod = product_db()
    with prod.cursor() as pcur:
        pcur.execute(f"SELECT * FROM {LEGACY_TABLE} ORDER BY id")
        entries = pcur.fetchall()
    prod.close()

    print(f"Read {len(entries)} rows from {LEGACY_TABLE}")

    admin = admin_db()
    with admin.cursor() as acur:
        migrated = 0
        new_leads = 0

        for r in entries:
            if not r.get("email"):
                continue  # a contact form technically requires email client-side, but don't trust that server-side

            if DRY_RUN:
                migrated += 1
                continue

            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            is_new_lead = acur.fetchone() is None
            first_name, last_name = _split_name(r.get("name"))
            unsubscribe_token = secrets.token_urlsafe(UNSUB_TOKEN_LEN)

            acur.execute(
                """INSERT INTO leads
                     (email, first_name, last_name, phone, original_source, created_at,
                      unsubscribe_token, legacy_id, legacy_source_table)
                   VALUES (%s,%s,%s,%s,'contact_form',%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE email=VALUES(email)""",
                (r["email"], first_name, last_name, r.get("phone"), r["created_at"],
                 unsubscribe_token, r["id"], LEGACY_TABLE)
            )
            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            lead_id = acur.fetchone()["id"]

            acur.execute(
                "SELECT id FROM lead_events WHERE lead_id=%s AND type='contact_form_submitted'", (lead_id,)
            )
            if not acur.fetchone():
                acur.execute(
                    """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                       VALUES (%s,%s,'contact_form_submitted','web',%s)""",
                    (lead_id, r["created_at"], json.dumps({
                        "subject": r.get("subject"), "message": r.get("message"),
                        "preferred_contact": r.get("preferred_contact"),
                    }, default=str))
                )

            if is_new_lead:
                new_leads += 1
                subject_slug = _slugify(r.get("subject"))
                acur.execute(
                    "INSERT INTO source_refs (path, kind, label, source) VALUES ('contact_form','signup',%s,'website_contact')",
                    (r.get("name") or r["email"],)
                )
                source_ref_id = acur.lastrowid
                acur.execute(
                    "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,'contact_form',%s)",
                    (lead_id, source_ref_id)
                )
                acur.execute("UPDATE leads SET original_source_ref_id=%s WHERE id=%s", (source_ref_id, lead_id))
                leads_drip.tag_lead(acur, lead_id, ["channel:contact_form", f"subject:{subject_slug}"])

            migrated += 1

        if not DRY_RUN:
            admin.commit()
            acur.execute(
                "INSERT INTO cron_run_log (job_name, total_processed, total_problem, detail_json) VALUES (%s,%s,0,%s)",
                ("contact_entries_sync", migrated, json.dumps({"new_leads": new_leads}))
            )
            admin.commit()

    admin.close()
    print(f"Processed {migrated} rows, {new_leads} new lead(s).")
    if DRY_RUN:
        print("--dry-run: no writes made")


if __name__ == "__main__":
    main()
