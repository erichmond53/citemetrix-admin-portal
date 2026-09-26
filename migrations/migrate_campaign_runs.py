#!/usr/bin/env python3
"""Schema split, stage 5/6: remap one campaign's drip_enrollments rows into
automation_runs, keyed on legacy_enrollment_id (idempotent), preserving every
value exactly:

  next_send_due_at -> next_due_at   bit-identical, no recompute (the sacred value)
  status            -> status       verbatim (same enum on both tables)
  current_step      -> current_node_key   via the send_email node whose
                        legacy_step_id matches the drip_steps row at
                        step_order = current_step + 1 in this campaign (NULL if
                        current_step is already past the last step -- a
                        completed/cancelled run has nothing left to point at)
  enrolled_at       -> entered_at
  last_sent_at      -> last_sent_at

Does NOT flip drip_campaigns.migrated_automation_id or automations.status --
that's the separate, explicit cutover step (this script's own --cutover flag),
kept apart so the remap can be dry-run and verified before anything about
ownership changes.

Usage:
  venv/bin/python migrations/migrate_campaign_runs.py --campaign-id N [--dry-run]
  venv/bin/python migrations/migrate_campaign_runs.py --campaign-id N --cutover
  venv/bin/python migrations/migrate_campaign_runs.py --campaign-id N --rollback
"""
import os
import sys
import argparse
from datetime import datetime

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


def dt(v):
    return v.strftime('%Y-%m-%d %H:%M:%S') if v else None


def remap(cur, campaign_id, dry_run):
    cur.execute("SELECT id, name FROM automations WHERE legacy_campaign_id=%s", (campaign_id,))
    automation = cur.fetchone()
    if not automation:
        print(f"ERROR: no automations row for legacy_campaign_id={campaign_id} -- run backfill_automations.py first")
        return None
    automation_id = automation["id"]

    # step_order -> send_email node_key, via the shared legacy_step_id key both
    # drip_steps and automation_steps carry.
    cur.execute(
        """SELECT ds.step_order, ast.node_key
           FROM drip_steps ds
           JOIN automation_steps ast ON ast.legacy_step_id = ds.id AND ast.node_type='send_email'
           WHERE ds.campaign_id=%s""",
        (campaign_id,)
    )
    step_to_node = {r["step_order"]: r["node_key"] for r in cur.fetchall()}

    cur.execute("SELECT * FROM drip_enrollments WHERE campaign_id=%s", (campaign_id,))
    enrollments = cur.fetchall()

    created, skipped, mapped_none = 0, 0, 0
    for e in enrollments:
        cur.execute("SELECT id FROM automation_runs WHERE legacy_enrollment_id=%s", (e["id"],))
        if cur.fetchone():
            skipped += 1
            continue

        target_step_order = e["current_step"] + 1
        node_key = step_to_node.get(target_step_order)
        if node_key is None:
            mapped_none += 1

        print(f"  enrollment {e['id']}: current_step={e['current_step']} -> node_key={node_key!r}, "
              f"status={e['status']!r}, next_due_at={e['next_send_due_at']}")

        if not dry_run:
            cur.execute(
                """INSERT INTO automation_runs
                     (automation_id, lead_id, current_node_key, next_due_at, status,
                      entered_at, last_sent_at, legacy_enrollment_id)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (automation_id, e["lead_id"], node_key, dt(e["next_send_due_at"]), e["status"],
                 dt(e["enrolled_at"]), dt(e["last_sent_at"]), e["id"])
            )
        created += 1

    return {"automation_id": automation_id, "automation_name": automation["name"],
            "total_enrollments": len(enrollments), "created": created, "skipped": skipped,
            "mapped_to_none": mapped_none}


def verify(cur, campaign_id):
    """Structural diff: #automation_runs == #drip_enrollments for this campaign,
    every next_due_at bit-identical, every status verbatim, zero unexplained
    mismatches. This is the "verify structurally, don't trust a clean run" bar
    from this session's earlier WordPress drip-engine rebuild."""
    cur.execute("SELECT id FROM automations WHERE legacy_campaign_id=%s", (campaign_id,))
    automation_id = cur.fetchone()["id"]

    cur.execute("SELECT COUNT(*) AS n FROM drip_enrollments WHERE campaign_id=%s", (campaign_id,))
    n_old = cur.fetchone()["n"]
    cur.execute("SELECT COUNT(*) AS n FROM automation_runs WHERE automation_id=%s", (automation_id,))
    n_new = cur.fetchone()["n"]
    print(f"  drip_enrollments: {n_old}, automation_runs: {n_new} -- {'MATCH' if n_old == n_new else 'MISMATCH!!'}")

    cur.execute(
        """SELECT e.id, e.status AS old_status, e.next_send_due_at AS old_due, r.status AS new_status, r.next_due_at AS new_due
           FROM drip_enrollments e JOIN automation_runs r ON r.legacy_enrollment_id = e.id
           WHERE e.campaign_id=%s""",
        (campaign_id,)
    )
    mismatches = 0
    for row in cur.fetchall():
        if row["old_status"] != row["new_status"] or row["old_due"] != row["new_due"]:
            mismatches += 1
            print(f"  MISMATCH enrollment {row['id']}: status {row['old_status']!r}->{row['new_status']!r}, "
                  f"due {row['old_due']}->{row['new_due']}")
    print(f"  value mismatches: {mismatches}")
    return n_old == n_new and mismatches == 0


def cutover(cur, conn, campaign_id):
    cur.execute("SELECT id FROM automations WHERE legacy_campaign_id=%s", (campaign_id,))
    automation_id = cur.fetchone()["id"]
    cur.execute("UPDATE drip_campaigns SET migrated_automation_id=%s WHERE id=%s", (automation_id, campaign_id))
    cur.execute("UPDATE automations SET status='active' WHERE id=%s", (automation_id,))
    conn.commit()
    print(f"CUTOVER: drip_campaigns.id={campaign_id} -> migrated_automation_id={automation_id}, automations.status='active'")


def rollback(cur, conn, campaign_id):
    cur.execute("UPDATE drip_campaigns SET migrated_automation_id=NULL WHERE id=%s", (campaign_id,))
    cur.execute("UPDATE automations SET status='inactive' WHERE legacy_campaign_id=%s", (campaign_id,))
    conn.commit()
    print(f"ROLLBACK: drip_campaigns.id={campaign_id} -> migrated_automation_id=NULL, automations.status='inactive'")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--campaign-id", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cutover", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    conn = admin_db()
    try:
        with conn.cursor() as cur:
            if args.cutover:
                cutover(cur, conn, args.campaign_id)
                return
            if args.rollback:
                rollback(cur, conn, args.campaign_id)
                return
            if args.verify_only:
                ok = verify(cur, args.campaign_id)
                print("VERIFY:", "PASS" if ok else "FAIL")
                return

            print(f"=== remap campaign {args.campaign_id} (dry_run={args.dry_run}) ===")
            result = remap(cur, args.campaign_id, args.dry_run)
            if result is None:
                sys.exit(1)
            print(result)

            if args.dry_run:
                conn.rollback()
                print("--dry-run: rolled back")
            else:
                conn.commit()
                print("Committed.")
                print("=== verify ===")
                ok = verify(cur, args.campaign_id)
                print("VERIFY:", "PASS" if ok else "FAIL")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
