#!/usr/bin/env python3
"""Create demo_outcomes table -- Sales alerts (unified Dashboard alerts, 2026-09-26).

Bookly's own tables have no concept of an assigned salesperson or a
post-demo outcome (held/no-show/converted/lost) -- confirmed by reading
templates/sales/demo_requests.html and the Bookly join query in app.py,
neither has anything beyond the booking-approval workflow status. This adds
a small admin-portal-side table keyed to the Bookly appointment id so two
new Sales alerts can exist: "demo booked, no assigned salesperson" and
"demo passed, no outcome logged."

Idempotent -- checks table existence before creating, safe to re-run.

Usage: venv/bin/python migrations/create_demo_outcomes.py [--dry-run]
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


def admin_db():
    return pymysql.connect(
        host='localhost', user=os.getenv('ADMIN_DB_USER', 'adminportal'),
        password=os.getenv('ADMIN_DB_PASSWORD'), database=os.getenv('ADMIN_DB_NAME', 'admin_portal'),
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
    )


def table_exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return cur.fetchone()["c"] > 0


DDL = """
CREATE TABLE demo_outcomes (
  id INT AUTO_INCREMENT PRIMARY KEY,
  bookly_appointment_id INT NOT NULL UNIQUE,
  assigned_to INT NULL,
  outcome ENUM('pending','held','no_show','converted','lost') NOT NULL DEFAULT 'pending',
  notes TEXT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            if table_exists(cur, "demo_outcomes"):
                print("demo_outcomes already exists -- skipping")
            else:
                print("Creating demo_outcomes")
                if not DRY_RUN:
                    cur.execute(DDL)
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
