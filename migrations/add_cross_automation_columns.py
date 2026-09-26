#!/usr/bin/env python3
"""Automation canvas, branching schema extension: adds next_automation_id/
next_automation_id_alt to automation_steps -- an if_else edge's branch-to-a-different-
automation target, per Eric's own answer (2026-09-22): "opened and clicked CAN naturally
be part of the same campaign, but didn't open SHOULD LIKELY be part of a different
campaign." NULL (the default, and every existing row) means "continue in this same
automation" -- today's exact, unchanged behavior; the walker only branches to a different
automation when this is explicitly set on the edge that was taken.

Usage: venv/bin/python migrations/add_cross_automation_columns.py [--dry-run]
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
        host=os.getenv("ADMIN_DB_HOST", "localhost"), user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            for col, ddl in (
                ("next_automation_id", "ALTER TABLE automation_steps ADD COLUMN next_automation_id INT NULL AFTER next_node_key"),
                ("next_automation_id_alt", "ALTER TABLE automation_steps ADD COLUMN next_automation_id_alt INT NULL AFTER next_node_key_alt"),
            ):
                cur.execute("SHOW COLUMNS FROM automation_steps LIKE %s", (col,))
                if cur.fetchone():
                    print(f"automation_steps.{col} already exists -- skipping")
                else:
                    print(f"Adding automation_steps.{col}")
                    if not DRY_RUN:
                        cur.execute(ddl)
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
