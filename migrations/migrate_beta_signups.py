#!/usr/bin/env python3
"""IA spec §8: migrate wp_citemetrix_beta_signups -> the unified admin_portal.leads pool.

Same shape as migrations/migrate_step1b.py (its own docstring is the fuller reference):
idempotent upsert keyed on (legacy_source_table, legacy_id) via leads' uq_legacy unique
key, dark/reversible (writes ONLY to admin_portal; the WP table is read-only here), and
per spec §8's explicit rule -- "preserve every record, migrate don't merge, let the dedupe
detector flag overlaps" -- this creates ONE leads row per beta signup regardless of whether
that email might already exist in the pool under a different origin (e.g. also ran a free
check). No pre-check, no merge: dedupe_detector.py's own hourly pass is what's supposed to
flag that overlap (dup_status='possible_dup'), exactly like it already does for the 5
pre-existing duplicate pairs. Doing a pre-check here would just be a second, competing
dedupe mechanism -- decisions-v2's whole point in keeping this a flag-only, single-detector
job.

Per-signup:
  - leads row: identity (name split into first/last -- the only reliably-populated name
    field on the source table; first_name/last_name/company/role are almost always empty
    strings there) + original_source='beta' + stage derived from real state, not a blanket
    'new' for all 40: a signup that already has a real WP user_id + subscription_id is
    already a customer in every way that matters, so it gets stage='won' -- same as any
    other lead that converts (see e.g. the free-check lead who's now Won in Pipeline).
    'declined' -> lost, 'pending' -> new, 'approved' without an account yet -> contacted.
  - one 'sourced' lead_event carrying the beta-specific fields with no leads-column home
    (role, interest, use_case, domain, website, utm_*, referrer, landing_page, beta_status)
    -- same "extra data goes in the sourced event's payload" pattern import_batch() and
    migrate_step1b.py both already use.
  - for a genuinely NEW lead only (not a re-sync): a source_refs row (path='beta',
    kind='signup') + a lead_source_touches row, same as migrate_step1b.py's SS14.2 pattern
    for its own population. No auto-enrollment into any drip campaign -- those are built
    for the free-check funnel specifically, not this population.

Usage: python3 migrate_beta_signups.py [--dry-run]
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

LEGACY_TABLE = "wp_citemetrix_beta_signups"


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


def _derive_stage(r):
    if r["user_id"] and r["subscription_id"]:
        return "won", (r["converted_at"] or r["approved_at"] or r["created_at"])
    if r["status"] == "declined":
        return "lost", (r["processed_date"] or r["created_at"])
    if r["status"] == "approved":
        return "contacted", (r["approved_at"] or r["created_at"])
    return "new", r["created_at"]


def main():
    snapshot_at = datetime.datetime.now().replace(microsecond=0)
    print(f"[{snapshot_at}] Beta signups migration {'(DRY RUN)' if DRY_RUN else ''}")

    prod = product_db()
    admin = admin_db()

    with prod.cursor() as pcur:
        pcur.execute(f"SELECT * FROM {LEGACY_TABLE} ORDER BY id")
        signups = pcur.fetchall()
    prod.close()

    print(f"Read {len(signups)} rows from {LEGACY_TABLE}")

    with admin.cursor() as acur:
        migrated, new_leads, resynced, event_events_inserted = 0, 0, 0, 0

        for r in signups:
            first_name, last_name = _split_name(r.get("name"))
            stage, stage_entered_at = _derive_stage(r)

            if DRY_RUN:
                migrated += 1
                continue

            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            is_new_lead = acur.fetchone() is None
            unsubscribe_token = secrets.token_urlsafe(UNSUB_TOKEN_LEN)

            acur.execute(
                """INSERT INTO leads
                     (email, first_name, last_name, original_source, created_at, stage, stage_entered_at,
                      excluded, unsubscribe_token, legacy_id, legacy_source_table)
                   VALUES (%s,%s,%s,'beta',%s,%s,%s,0,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     email=VALUES(email), first_name=VALUES(first_name), last_name=VALUES(last_name),
                     -- Same rule as migrate_step1b.py: a human manually moving this lead through
                     -- Pipeline must never get overwritten by the next hourly re-sync. 'won' is the
                     -- one hard-external-signal exception that always wins.
                     stage = IF(VALUES(stage)='won', 'won', stage),
                     stage_entered_at = IF(VALUES(stage)='won' AND stage<>'won', VALUES(stage_entered_at), stage_entered_at)""",
                (r["email"], first_name, last_name, r["created_at"], stage, stage_entered_at,
                 unsubscribe_token, r["id"], LEGACY_TABLE)
            )
            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            lead_id = acur.fetchone()["id"]

            # 'sourced' event -- idempotent: skip if this lead already has one from this migration.
            acur.execute(
                "SELECT id FROM lead_events WHERE lead_id=%s AND type='sourced' AND channel='web'", (lead_id,)
            )
            if not acur.fetchone():
                payload = json.dumps({
                    "beta_status": r["status"], "role": r["role"], "interest": r["interest"],
                    "domain": r["domain"], "website": r["website"], "use_case": r["use_case"],
                    "utm_source": r["utm_source"], "utm_medium": r["utm_medium"], "utm_campaign": r["utm_campaign"],
                    "referrer": r["referrer"], "landing_page": r["landing_page"],
                }, default=str)
                acur.execute(
                    """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                       VALUES (%s,%s,'sourced','web',%s)""",
                    (lead_id, r["created_at"], payload)
                )
                event_events_inserted += 1

            if is_new_lead:
                acur.execute(
                    "INSERT INTO source_refs (path, kind, label, source) VALUES ('beta','signup',%s,%s)",
                    (r["email"], (r["utm_source"] or "beta_signup")[:80])
                )
                source_ref_id = acur.lastrowid
                acur.execute(
                    "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,'beta',%s)",
                    (lead_id, source_ref_id)
                )
                acur.execute("UPDATE leads SET original_source_ref_id=%s WHERE id=%s", (source_ref_id, lead_id))
                new_leads += 1
            else:
                resynced += 1

            admin.commit()
            migrated += 1

        if not DRY_RUN:
            admin.commit()

    admin.close()

    print(f"Migrated: {migrated} beta signups ({new_leads} new leads, {resynced} re-synced). "
          f"New sourced events: {event_events_inserted}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        from cron_alert import alert_and_exit
        alert_and_exit('migrate_beta_signups.py', e)
