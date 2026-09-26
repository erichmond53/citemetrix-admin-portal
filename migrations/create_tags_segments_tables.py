#!/usr/bin/env python3
"""Tags + saved segments: resolves how a growing, heterogeneous lead pool (demo requests,
contact form, free-check widget, purchased lists, Marblism outreach, trade shows, paid/organic
social, Google CPC) gets targeted for enrollment, per Eric's own call: a single master pool
with flexible, multi-conditional segments -- not MailPoet-style separate lists per source,
which would combinatorially explode across source x platform x campaign.

`tags`/`lead_tags` are a plain many-to-many, same FK convention as `list_memberships` (FK only
to the owning side, never to `leads`). `lead_segments.conditions` stores a flat-AND condition
list (v1 scope, chosen over nested AND/OR to keep this shippable) -- see segments.py for the
query builder that turns it into SQL.

Also extends `leads.original_source`, a real MySQL ENUM (not free text -- confirmed via
SHOW COLUMNS), to add 'contact_form' and 'demo_request': two new WordPress->leads sync paths
land in the same pass as this schema (migrate_contact_entries.py, migrate_demo_requests.py).
Grepped app.py/leads_drip.py for hardcoded original_source value lists first -- none found,
safe to extend.

Usage: venv/bin/python migrations/create_tags_segments_tables.py [--dry-run]
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


TABLES = {
    "tags": """
        CREATE TABLE tags (
          id         INT AUTO_INCREMENT PRIMARY KEY,
          name       VARCHAR(100) NOT NULL,
          created_by INT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_tags_name (name)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "lead_tags": """
        CREATE TABLE lead_tags (
          id        INT AUTO_INCREMENT PRIMARY KEY,
          tag_id    INT NOT NULL,
          lead_id   INT NOT NULL,
          added_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_tag_lead (tag_id, lead_id),
          KEY idx_lead_tags_lead (lead_id),
          CONSTRAINT fk_lead_tags_tag FOREIGN KEY (tag_id) REFERENCES tags(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "lead_segments": """
        CREATE TABLE lead_segments (
          id         INT AUTO_INCREMENT PRIMARY KEY,
          name       VARCHAR(255) NOT NULL,
          conditions JSON NOT NULL,
          created_by INT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}

NEW_ORIGINAL_SOURCE_ENUM = (
    "ALTER TABLE leads MODIFY original_source "
    "ENUM('in_person','direct','inbound','beta','chat','contact_form','demo_request') NOT NULL"
)


def table_exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return cur.fetchone()["c"] > 0


def original_source_enum_current(cur):
    cur.execute(
        "SELECT COLUMN_TYPE FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name='leads' AND column_name='original_source'"
    )
    return cur.fetchone()["COLUMN_TYPE"]


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            for name in ("tags", "lead_tags", "lead_segments"):  # order matters: FK needs tags first
                if table_exists(cur, name):
                    print(f"table {name} already exists -- skipping")
                else:
                    print(f"Creating table {name}")
                    if not DRY_RUN:
                        cur.execute(TABLES[name])

            current_enum = original_source_enum_current(cur)
            if "contact_form" in current_enum and "demo_request" in current_enum:
                print(f"leads.original_source already extended -- skipping (current: {current_enum})")
            else:
                print(f"Extending leads.original_source ENUM (current: {current_enum})")
                if not DRY_RUN:
                    cur.execute(NEW_ORIGINAL_SOURCE_ENUM)

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
