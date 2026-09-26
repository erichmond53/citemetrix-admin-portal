#!/usr/bin/env python3
"""Lists mechanism: lists + list_memberships, resolving the open decision from the schema
split. Deliberately NOT built on lead_source_touches -- that table is explicitly append-only
provenance (its own docstring: "touches are append-only"), and remove_from_list needs real
removal, which an append-only log can't represent without breaking its own invariant.

Usage: venv/bin/python migrations/create_lists_tables.py [--dry-run]
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
    "lists": """
        CREATE TABLE lists (
          id         INT AUTO_INCREMENT PRIMARY KEY,
          name       VARCHAR(255) NOT NULL,
          created_by INT NULL,
          created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    "list_memberships": """
        CREATE TABLE list_memberships (
          id        INT AUTO_INCREMENT PRIMARY KEY,
          list_id   INT NOT NULL,
          lead_id   INT NOT NULL,
          added_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          UNIQUE KEY uq_list_lead (list_id, lead_id),
          KEY idx_memberships_list (list_id),
          CONSTRAINT fk_memberships_list FOREIGN KEY (list_id) REFERENCES lists(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
}


def table_exists(cur, table):
    cur.execute(
        "SELECT COUNT(*) c FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s",
        (table,),
    )
    return cur.fetchone()["c"] > 0


def main():
    conn = admin_db()
    try:
        with conn.cursor() as cur:
            for name in ("lists", "list_memberships"):  # order matters: FK needs lists first
                if table_exists(cur, name):
                    print(f"table {name} already exists -- skipping")
                else:
                    print(f"Creating table {name}")
                    if not DRY_RUN:
                        cur.execute(TABLES[name])
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
