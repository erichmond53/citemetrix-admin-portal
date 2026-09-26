#!/usr/bin/env python3
"""GrapesJS embed (email-builder-spec-2026-09-21.md SS1.2): tracks whether emails.body_text
was hand-edited (not system-auto-derived from body_html) since the last HTML change --
GrapesJS reverses the drip engine's existing plain-text->HTML derivation, so plain text
needs its own drift tracking now. 0 = safe to auto-regenerate from HTML on save; 1 = a
human edited it directly, leave it alone and show a staleness warning instead.

Usage: venv/bin/python migrations/add_body_text_manual.py [--dry-run]
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
            cur.execute("SHOW COLUMNS FROM emails LIKE 'body_text_manual'")
            if cur.fetchone():
                print("emails.body_text_manual already exists -- skipping")
            else:
                print("Adding emails.body_text_manual")
                if not DRY_RUN:
                    cur.execute(
                        "ALTER TABLE emails ADD COLUMN body_text_manual TINYINT(1) NOT NULL DEFAULT 0 AFTER body_text"
                    )
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
