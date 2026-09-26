#!/usr/bin/env python3
"""IA spec §8: migrate Chatly chat-widget lead captures -> the unified admin_portal.leads
pool. Same idempotent-upsert shape as migrate_beta_signups.py / migrate_step1b.py (see
those docstrings for the fuller pattern reference).

Source is wp_chatly_leads (the captured name/email/phone), joined to its parent
wp_chatly_conversations row for context (page_url, message_count) -- NOT every chat
conversation has a captured lead (most are anonymous visitors who never gave contact
info), so the join is filtered to conversations with lead_captured=1 AND a real email on
the captured-lead row; an anonymous chat has nothing to dedupe on and isn't a lead.

Per spec §8's table: 'still visible in Inbox, also in Leads' -- Customers > Inbox's own
chat listing (customers_inbox() in app.py) already reads wp_chatly_conversations/messages
directly and is UNCHANGED by this script; this only adds the second half, a real `leads`
row, so a captured chat lead now also shows up in Marketing > Leads, Pipeline once it
reaches engaged+, and the lead detail panel -- same as any other lead.

legacy_id keys off wp_chatly_leads.id (the captured-lead row's own PK, not the
conversation's) since that's the natural 1:1 unit being migrated.

Usage: python3 migrate_chatly_captures.py [--dry-run]
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

LEGACY_TABLE = "wp_chatly_leads"


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


def _split_name(full_name):
    full_name = (full_name or "").strip()
    if not full_name:
        return "", ""
    parts = full_name.split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def main():
    snapshot_at = datetime.datetime.now().replace(microsecond=0)
    print(f"[{snapshot_at}] Chatly captures migration {'(DRY RUN)' if DRY_RUN else ''}")

    prod = product_db()
    admin = admin_db()

    with prod.cursor() as pcur:
        pcur.execute(
            "SELECT l.id, l.conversation_id, l.name, l.email, l.phone, l.interest, l.source, "
            "l.status, l.created_at, c.page_url, c.message_count, c.started_at "
            "FROM wp_chatly_leads l JOIN wp_chatly_conversations c ON c.id = l.conversation_id "
            "WHERE c.lead_captured=1 AND l.email IS NOT NULL AND l.email != '' "
            "ORDER BY l.id"
        )
        captures = pcur.fetchall()
    prod.close()

    print(f"Read {len(captures)} qualifying rows from {LEGACY_TABLE} (lead_captured=1, real email)")

    with admin.cursor() as acur:
        migrated, new_leads, resynced, events_inserted = 0, 0, 0, 0

        for r in captures:
            first_name, last_name = _split_name(r.get("name"))

            if DRY_RUN:
                migrated += 1
                continue

            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            is_new_lead = acur.fetchone() is None
            unsubscribe_token = secrets.token_urlsafe(UNSUB_TOKEN_LEN)
            created_at = r["created_at"] or r["started_at"]

            acur.execute(
                """INSERT INTO leads
                     (email, first_name, last_name, phone, original_source, created_at,
                      stage, stage_entered_at, excluded, unsubscribe_token, legacy_id, legacy_source_table)
                   VALUES (%s,%s,%s,%s,'chat',%s,'new',%s,0,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     email=VALUES(email), first_name=VALUES(first_name), last_name=VALUES(last_name),
                     phone=VALUES(phone)
                     -- stage deliberately NOT in the UPDATE clause -- same rule as the other two
                     -- migrations: a human moving this lead through Pipeline must survive a re-sync.""",
                (r["email"], first_name, last_name, r["phone"], created_at, created_at,
                 unsubscribe_token, r["id"], LEGACY_TABLE)
            )
            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            lead_id = acur.fetchone()["id"]

            acur.execute(
                "SELECT id FROM lead_events WHERE lead_id=%s AND type='sourced' AND channel='web'", (lead_id,)
            )
            if not acur.fetchone():
                payload = json.dumps({
                    "interest": r["interest"], "chat_source": r["source"], "chatly_status": r["status"],
                    "page_url": r["page_url"], "message_count": r["message_count"],
                    "conversation_id": r["conversation_id"],
                }, default=str)
                acur.execute(
                    """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                       VALUES (%s,%s,'sourced','web',%s)""",
                    (lead_id, created_at, payload)
                )
                events_inserted += 1

            if is_new_lead:
                acur.execute(
                    "INSERT INTO source_refs (path, kind, label, source) VALUES ('chat','signup',%s,%s)",
                    (r["email"], (r["source"] or "chatly")[:80])
                )
                source_ref_id = acur.lastrowid
                acur.execute(
                    "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,'chat',%s)",
                    (lead_id, source_ref_id)
                )
                acur.execute("UPDATE leads SET original_source_ref_id=%s WHERE id=%s", (source_ref_id, lead_id))
                leads_drip.tag_lead(acur, lead_id, "channel:chat")
                new_leads += 1
            else:
                resynced += 1

            admin.commit()
            migrated += 1

        if not DRY_RUN:
            admin.commit()

    admin.close()

    print(f"Migrated: {migrated} chat captures ({new_leads} new leads, {resynced} re-synced). "
          f"New sourced events: {events_inserted}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        from cron_alert import alert_and_exit
        alert_and_exit('migrate_chatly_captures.py', e)
