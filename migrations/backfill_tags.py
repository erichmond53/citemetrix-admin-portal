#!/usr/bin/env python3
"""Best-effort tag backfill for the ~800 leads that existed before the tags/segments build
landed (per Eric's explicit choice: backfill what's cheaply inferable now, not forward-only --
see AskUserQuestion in the tags/segments plan). contact_form/demo_request leads already carry
real tags from their own sync scripts and are skipped here.

Inference is explicitly best-effort, NOT authoritative -- checked against real data first:
  - original_source='inbound' -> channel:free_check (+ campaign:<platform> when
    ai_referral_platform is already set -- 2 of 46 inbound leads have this)
  - original_source='beta'    -> channel:beta
  - original_source='chat'    -> channel:chat
  - original_source in ('direct','in_person') -> channel:import, PLUS a fuzzy match against
    source_refs.source/label for 'marbl' (case-insensitive substring) -> campaign:marblism.
    Checked against real data first: all 718 direct/in_person leads on this box link to a
    Marblism-labeled source_refs row today (including real typo variants seen in the data --
    "Marblism LinkedIn", "Marblisim LimkedIn", and one row with an empty `source` but a
    Marblism `label`) -- the substring match against BOTH columns catches all of them. A
    future purchased list with no "marbl" match just gets channel:import alone, which is
    correct (there is no signal to do better with yet).

Idempotent via lead_tags' own UNIQUE(tag_id, lead_id) + tag_lead()'s own get-or-create.
Any lead can be corrected/re-tagged later via the tag picker on manual add/CSV upload.

Usage: venv/bin/python migrations/backfill_tags.py [--dry-run]
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
import leads_drip


def admin_db():
    return pymysql.connect(
        host=os.getenv("ADMIN_DB_HOST", "localhost"), user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    counts = {}
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT l.id, l.original_source, l.ai_referral_platform,
                          sr.source AS ref_source, sr.label AS ref_label
                   FROM leads l
                   LEFT JOIN source_refs sr ON sr.id = l.original_source_ref_id
                   WHERE l.original_source NOT IN ('contact_form', 'demo_request')"""
            )
            rows = cur.fetchall()
            print(f"Considering {len(rows)} leads (excluding contact_form/demo_request, already tagged by their own syncs)")

            for r in rows:
                tag_names = []
                src = r["original_source"]
                if src == "inbound":
                    tag_names.append("channel:free_check")
                    if r["ai_referral_platform"]:
                        tag_names.append(f"campaign:{r['ai_referral_platform'].lower()}")
                elif src == "beta":
                    tag_names.append("channel:beta")
                elif src == "chat":
                    tag_names.append("channel:chat")
                elif src in ("direct", "in_person"):
                    tag_names.append("channel:import")
                    haystack = f"{r['ref_source'] or ''} {r['ref_label'] or ''}".lower()
                    if "marbl" in haystack:
                        tag_names.append("campaign:marblism")

                for t in tag_names:
                    counts[t] = counts.get(t, 0) + 1

                if tag_names and not DRY_RUN:
                    leads_drip.tag_lead(cur, r["id"], tag_names)

        if DRY_RUN:
            print("--dry-run: rolling back")
            conn.rollback()
        else:
            conn.commit()
            print("Committed.")
        print("Tag counts:", counts)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
