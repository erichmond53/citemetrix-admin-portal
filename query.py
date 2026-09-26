#!/usr/bin/env python3
"""admin_portal DB query runner. Credential-loading lives here, once -- callers
never reconstruct a connection with ADMIN_DB_PASSWORD inline, they just invoke
this script. Reads SQL from a file (avoids shell-quoting problems with
multi-line queries) or from stdin if no file is given.

Usage:
    ./query.py path/to/query.sql
    echo "SELECT 1" | ./query.py

SELECT-shaped statements print one row per line (dict repr, datetimes as
plain strings). Anything else (INSERT/UPDATE/ALTER/CREATE/DELETE) commits and
prints the affected row count. Multiple ;-separated statements in one file
are run in order, in one transaction, committed together at the end -- so a
migration-shaped file either fully lands or fully rolls back.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from dotenv import load_dotenv
load_dotenv(os.path.join(HERE, ".env"))

import pymysql


def get_admin_db():
    return pymysql.connect(
        host='localhost',
        user=os.getenv('ADMIN_DB_USER', 'adminportal'),
        password=os.getenv('ADMIN_DB_PASSWORD'),
        database=os.getenv('ADMIN_DB_NAME', 'admin_portal'),
        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1]) as f:
            sql = f.read()
    else:
        sql = sys.stdin.read()

    statements = [s.strip() for s in sql.split(';') if s.strip()]
    if not statements:
        print("(no SQL given)")
        return

    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
                if cur.description:  # SELECT-shaped -- has result columns
                    rows = cur.fetchall()
                    print(f"-- {len(rows)} row(s) --")
                    for row in rows:
                        print(row)
                else:
                    print(f"-- OK, {cur.rowcount} row(s) affected --")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"ERROR (rolled back): {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
