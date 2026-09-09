#!/usr/bin/env python3
"""Publication Log fields on source_refs.

Per CITEMETRIX-MEASUREMENT-ARCHITECTURE-2026-09-09.md §6 ("do not build a
second registry") the Publication Log is a superset VIEW, not a new table:
engine-generated companions are read live from the WP product DB
(wp_citemetrix_ce_companions / wp_citemetrix_ce_generations, same
product_db() pattern as migrations/migrate_beta_signups.py), and
hand-entered posts (LinkedIn personal, Substack, Medium -- wherever no API
exists) write into source_refs, same as Campaign Builder already does for
kind='ad_campaign' (v2 §2 ruling 10). This migration adds the fields that
ruling's hand-entered side needs and didn't have yet:

  - kind ENUM gains 'publication'
  - reach       int, optional manual reach (LinkedIn personal/Substack/Medium
                have no reach API -- see decisions-v3 §4)
  - notes       text, optional
  - published_at  datetime, optional -- when the post actually went up,
                which may not be "now" if logging retroactively; falls back
                to created_at in the UI if null

No new table, no ENUM removal, no data rewritten -- additive only, and
idempotent (checks current state before altering), safe to re-run.

Usage: venv/bin/python migrations/add_publication_log_fields.py [--dry-run]
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


def column_exists(cur, table, column):
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone()["c"] > 0


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM source_refs LIKE 'kind'")
            kind_type = cur.fetchone()["Type"]
            if "'publication'" in kind_type:
                print("kind ENUM already has 'publication' -- skipping")
            else:
                print("Adding 'publication' to source_refs.kind ENUM")
                if not DRY_RUN:
                    cur.execute(
                        "ALTER TABLE source_refs MODIFY kind "
                        "ENUM('show','import_batch','ad_campaign','ad_set','ad_creative','signup','publication') NOT NULL"
                    )

            for col, ddl in (
                ("reach", "ALTER TABLE source_refs ADD COLUMN reach INT NULL AFTER tagged_url"),
                ("notes", "ALTER TABLE source_refs ADD COLUMN notes TEXT NULL AFTER reach"),
                ("published_at", "ALTER TABLE source_refs ADD COLUMN published_at DATETIME NULL AFTER notes"),
            ):
                if column_exists(cur, "source_refs", col):
                    print(f"source_refs.{col} already exists -- skipping")
                else:
                    print(f"Adding source_refs.{col}")
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
