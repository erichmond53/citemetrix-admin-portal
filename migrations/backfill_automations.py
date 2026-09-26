#!/usr/bin/env python3
"""Schema split, stage 2: backfill emails/automations/automation_steps from the
live drip_campaigns/drip_steps tables, DARK -- no automation_runs are created
here, so nothing live depends on this data yet. Stage 1
(migrations/create_automation_tables.py) must have already run.

For every drip_campaigns row: one automations row (status='inactive' always,
regardless of drip_campaigns.active -- a migrated automation is turned on
explicitly at cutover, never implicitly by this backfill), keyed on
legacy_campaign_id.

For every drip_steps row: one emails row (content only), keyed on
legacy_step_id, PLUS a linear node graph per campaign:

    trigger -> delay(day_offset of step 1) -> send_email(step 1)
            -> delay(day_offset of step 2) -> send_email(step 2)
            -> ... -> send_email(step N) -> [end]

day_offset is carried onto the delay node exactly as process_due_enrollments()
already interprets it today: "days after the PREVIOUS step actually sent",
anchored on send time, not on original enrollment -- see that function's own
docstring (the 2026-09-08 same-day step1+step2 bug this fixed). This backfill
does not change that semantic, only relocates day_offset from drip_steps onto
the delay node that precedes each send_email node.

cta_kind is derived from the real code path, not guessed: build_cta() in
leads_drip.py routes on `cta_key.lower() == 'book-demo'` (BOOK_DEMO_CTA_KEY),
not on cta_key being NULL -- confirmed against live data before writing this
(the three "-- Warm (Checked)" campaigns all have literal cta_key='book-demo'
on every step; only the unrelated Free-Check Nurture campaign has NULL
cta_key, and for a NULL cta_key build_cta() returns (None, None) regardless of
cta_kind, so the default 'check' there is inert either way).

Idempotent (upserts keyed on legacy_*_id, safe to re-run -- a re-run must
show zero new rows). Everything in one script since automations/emails/graph
for one campaign are one logical unit; each campaign's backfill is its own
transaction so a failure partway through doesn't leave a different campaign
half-written.

Usage: venv/bin/python migrations/backfill_automations.py [--dry-run]
"""
import os
import sys
import json

DRY_RUN = "--dry-run" in sys.argv

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

BOOK_DEMO_CTA_KEY = "book-demo"  # leads_drip.py's own constant, mirrored here (do not diverge)


def admin_db():
    return pymysql.connect(
        host=os.getenv("ADMIN_DB_HOST", "localhost"), user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def cta_kind_for(cta_key):
    if cta_key and cta_key.strip().lower() == BOOK_DEMO_CTA_KEY:
        return "book_demo"
    return "check"


def backfill_campaign(cur, campaign, steps, stats):
    cur.execute("SELECT id FROM automations WHERE legacy_campaign_id=%s", (campaign["id"],))
    existing = cur.fetchone()
    if existing:
        automation_id = existing["id"]
        stats["automations_skipped"] += 1
    else:
        cur.execute(
            """INSERT INTO automations
                 (name, description, status, run_once_per_subscriber, trigger_type,
                  source_batch_id, created_by, created_at, legacy_campaign_id)
               VALUES (%s,%s,'inactive',1,'batch_enroll',%s,%s,%s,%s)""",
            (campaign["name"], campaign["description"], campaign["batch_id"],
             campaign["created_by"], campaign["created_at"], campaign["id"]),
        )
        automation_id = cur.lastrowid
        stats["automations_created"] += 1

    prev_send_node_key = None
    for step in steps:
        cur.execute("SELECT id FROM emails WHERE legacy_step_id=%s", (step["id"],))
        existing_email = cur.fetchone()
        if existing_email:
            email_id = existing_email["id"]
            stats["emails_skipped"] += 1
        else:
            cur.execute(
                """INSERT INTO emails
                     (name, subject, body_text, body_html, cta_text, cta_key, cta_kind,
                      onpage_title, onpage_copy, onpage_sync_status, onpage_sync_error,
                      is_survey_step, survey_epoch, status, created_by, created_at, legacy_step_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s,%s)""",
                (f"{campaign['name']} — Step {step['step_order']}", step["subject"],
                 step["body"], step["body_html"], step["cta_text"], step["cta_key"],
                 cta_kind_for(step["cta_key"]), step["onpage_title"], step["onpage_copy"],
                 step["onpage_sync_status"], step["onpage_sync_error"], step["is_survey_step"],
                 step["survey_epoch"], campaign["created_by"], step["created_at"], step["id"]),
            )
            email_id = cur.lastrowid
            stats["emails_created"] += 1

        delay_key = f"delay{step['step_order']}"
        send_key = f"send{step['step_order']}"

        cur.execute(
            "SELECT id FROM automation_steps WHERE automation_id=%s AND node_key=%s",
            (automation_id, delay_key),
        )
        if cur.fetchone():
            stats["nodes_skipped"] += 1
        else:
            cur.execute(
                """INSERT INTO automation_steps
                     (automation_id, node_key, node_type, config, next_node_key, legacy_step_id)
                   VALUES (%s,%s,'delay',%s,%s,%s)""",
                (automation_id, delay_key, json.dumps({"unit": "days", "value": step["day_offset"]}),
                 send_key, step["id"]),
            )
            stats["nodes_created"] += 1

        cur.execute(
            "SELECT id FROM automation_steps WHERE automation_id=%s AND node_key=%s",
            (automation_id, send_key),
        )
        if cur.fetchone():
            stats["nodes_skipped"] += 1
        else:
            # next_node_key filled in on the NEXT iteration once we know the following
            # delay's key; left NULL for now (== end of sequence), corrected below if a
            # following step exists. A fresh insert is always NULL-next here since this
            # is the newest node in the chain being built left-to-right.
            cur.execute(
                """INSERT INTO automation_steps
                     (automation_id, node_key, node_type, config, next_node_key, legacy_step_id)
                   VALUES (%s,%s,'send_email',%s,NULL,%s)""",
                (automation_id, send_key, json.dumps({"email_id": email_id}), step["id"]),
            )
            stats["nodes_created"] += 1

        if prev_send_node_key:
            cur.execute(
                "UPDATE automation_steps SET next_node_key=%s WHERE automation_id=%s AND node_key=%s AND next_node_key IS NULL",
                (delay_key, automation_id, prev_send_node_key),
            )
        prev_send_node_key = send_key

    # trigger node points at the first delay -- created/verified last so first_delay_key is known.
    if steps:
        first_delay_key = f"delay{steps[0]['step_order']}"
        cur.execute(
            "SELECT id FROM automation_steps WHERE automation_id=%s AND node_key='trigger'",
            (automation_id,),
        )
        if cur.fetchone():
            stats["nodes_skipped"] += 1
        else:
            cur.execute(
                """INSERT INTO automation_steps (automation_id, node_key, node_type, config, next_node_key)
                   VALUES (%s,'trigger','trigger',%s,%s)""",
                (automation_id, json.dumps({"trigger_type": "batch_enroll", "source_batch_id": campaign["batch_id"]}),
                 first_delay_key),
            )
            stats["nodes_created"] += 1


def main():
    conn = admin_db()
    stats = {"automations_created": 0, "automations_skipped": 0, "emails_created": 0, "emails_skipped": 0,
              "nodes_created": 0, "nodes_skipped": 0}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, description, batch_id, active, created_by, created_at FROM drip_campaigns ORDER BY id")
            campaigns = cur.fetchall()
            for campaign in campaigns:
                cur.execute(
                    "SELECT * FROM drip_steps WHERE campaign_id=%s ORDER BY step_order ASC",
                    (campaign["id"],),
                )
                steps = cur.fetchall()
                print(f"Campaign {campaign['id']} ({campaign['name']!r}): {len(steps)} step(s)")
                backfill_campaign(cur, campaign, steps, stats)

        print(json.dumps(stats, indent=2))
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
