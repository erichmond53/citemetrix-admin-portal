#!/usr/bin/env python3
"""step1brief.md SS15.1/15.2 step 5: explicitly cancel any campaign-11
enrollment for a lead the WordPress nurture cron already owns
(wp_nurture_owned=1), rather than relying on the send-time gate to catch
them one at a time. The gate works, but leaves a real race open: drip_cron
runs at :05, the sync at :25, and both engines schedule their first touch
at day 2 -- there's up to an hour where a lead is genuinely due in both
systems and the gate can't yet see it. This closes that window explicitly
instead of leaving it to chance.

Meant to be re-run immediately before activating campaign 11 (step 6), to
keep the window as tight as possible between this cleanup and the flip
that makes it matter.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

CAMPAIGN_ID = 11


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    with conn.cursor() as cur:
        cur.execute(
            """SELECT e.id, e.lead_id, l.email FROM drip_enrollments e
               JOIN leads l ON l.id = e.lead_id
               WHERE e.campaign_id=%s AND e.status='active' AND l.wp_nurture_owned=1""",
            (CAMPAIGN_ID,)
        )
        mixed = cur.fetchall()
        print(f"Found {len(mixed)} mixed enrollment(s) (campaign 11, active, wp_nurture_owned=1)")

        for row in mixed:
            cur.execute("UPDATE drip_enrollments SET status='cancelled' WHERE id=%s", (row["id"],))
            cur.execute(
                "INSERT INTO drip_send_log (enrollment_id, status, error) VALUES (%s,'cancelled',%s)",
                (row["id"], f"{row['email']}: cancelled at cutover -- WordPress nurture cron already owns this lead (wp_nurture_owned=1)")
            )
            print(f"  cancelled enrollment {row['id']} (lead {row['lead_id']}, {row['email']})")
        conn.commit()

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
