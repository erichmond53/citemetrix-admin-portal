#!/usr/bin/env python3
"""Step 3a: dedupe detector over the unified lead pool (decisions-v2 SS4.2,
step-1-brief.md SS9.4). Flag-only -- sets dup_status='possible_dup' and a
shared person_cluster_id per duplicated email. Never merges, never deletes.

Must run before anything reads the pool for real (SS8.3): the hourly
migrate_step1b.py sync means new free-check leads arrive continuously with
dup_status='unique' and person_cluster_id=NULL, so unclustered duplicates
accumulate from the day the sync started, independent of whether a real cold
list has ever been imported. This is why the detector is cron'd, not a
one-time pass -- "at volume" is no longer the trigger condition.

Email is the only key used (Investigation C: the only structurally viable
dedupe key today -- company/name/linkedin_url aren't reliably present on
both sides of the pool). Relies on leads.email's own collation
(utf8mb4_0900_ai_ci, case-insensitive) for matching, same as ses_suppressions.

Idempotent: re-running makes no further writes once a group is already
correctly clustered. Preserves any pre-existing person_cluster_id on a group
(e.g. the 5 pairs clustered manually before this detector existed) rather
than reassigning a new one -- the canonical cluster id for a group is
whichever cluster id is already in use, or MIN(id) if none yet.

Full recompute, self-healing (step-1-brief.md SS10.2): a row flagged
possible_dup whose email is no longer part of a real duplicate group (a
corrected typo, a merge, or -- during testing -- a deleted duplicate) gets
reset to unique with its cluster cleared, rather than staying flagged
forever. dup_status='merged_away' is terminal and is never touched either
direction -- it means a human already resolved that row.

NULL/empty emails are excluded from grouping entirely (WHERE email IS NOT
NULL AND email != '' on the grouping query) specifically so multiple
email-less leads -- e.g. trade-show badge scans, the reason leads.email is
nullable at all -- are never collapsed into one false shared identity by
SQL's NULL-groups-as-one GROUP BY semantics.

Usage: python3 dedupe_detector.py
"""
import os
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    ts = datetime.datetime.now().replace(microsecond=0)
    conn = admin_db()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT email FROM leads WHERE email IS NOT NULL AND email != '' GROUP BY email HAVING COUNT(*) > 1"
        )
        dup_emails = [r["email"] for r in cur.fetchall()]
        dup_email_set = set(dup_emails)

        groups_touched, rows_changed = 0, 0
        for email in dup_emails:
            cur.execute(
                "SELECT id, person_cluster_id, dup_status FROM leads WHERE email=%s ORDER BY id", (email,)
            )
            rows = cur.fetchall()
            # merged_away rows still count toward picking the canonical cluster id
            # (a human already resolved them into this cluster) but are never
            # written to below -- terminal means terminal.
            existing_clusters = sorted(set(r["person_cluster_id"] for r in rows if r["person_cluster_id"] is not None))
            cluster_id = existing_clusters[0] if existing_clusters else rows[0]["id"]

            group_changed = False
            for r in rows:
                if r["dup_status"] == "merged_away":
                    continue
                if r["person_cluster_id"] != cluster_id or r["dup_status"] != "possible_dup":
                    cur.execute(
                        "UPDATE leads SET dup_status='possible_dup', person_cluster_id=%s WHERE id=%s",
                        (cluster_id, r["id"])
                    )
                    rows_changed += 1
                    group_changed = True
            if group_changed:
                groups_touched += 1

        # Self-heal: anything still flagged possible_dup whose email is no longer
        # in a real duplicate group (shrank to size 1, or was never eligible --
        # e.g. NULL) gets reset. merged_away is excluded by the WHERE itself,
        # since it's a different status value -- never a candidate for reset.
        cur.execute("SELECT id, email FROM leads WHERE dup_status='possible_dup'")
        currently_flagged = cur.fetchall()
        reset_count = 0
        for r in currently_flagged:
            if r["email"] in dup_email_set:
                continue
            cur.execute("UPDATE leads SET dup_status='unique', person_cluster_id=NULL WHERE id=%s", (r["id"],))
            reset_count += 1

        conn.commit()

        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE dup_status='possible_dup'")
        total_flagged = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(DISTINCT person_cluster_id) AS n FROM leads WHERE person_cluster_id IS NOT NULL")
        total_clusters = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM leads WHERE dup_status='merged_away'")
        total_merged = cur.fetchone()["n"]

    conn.close()
    print(f"[{ts}] dedupe_detector: {len(dup_emails)} duplicate email group(s) found, "
          f"{groups_touched} group(s) changed, {rows_changed} row(s) updated, "
          f"{reset_count} row(s) self-healed back to unique. "
          f"Totals: {total_flagged} possible_dup across {total_clusters} clusters, {total_merged} merged_away.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        from cron_alert import alert_and_exit
        alert_and_exit('dedupe_detector.py', e)
