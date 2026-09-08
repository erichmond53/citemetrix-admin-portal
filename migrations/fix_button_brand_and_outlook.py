#!/usr/bin/env python3
"""Eric's read on campaign 11: buttons should be the brand's actual CTA
color scheme -- teal background (#10E07C, CiteMetrix_Email_Template's
COLOR_GREEN), dark navy text (#0a1015, COLOR_DARK) -- matching the button
every other CiteMetrix email already uses via block_cta_button() in the WP
plugin's shared class-citemetrix-email-template.php. The ported version
used navy-bg/white-text, inherited from the older build_nurture_*()
functions in class-citemetrix-free-score.php, which never matched the
shared template's own convention.

Also switched to the standard bulletproof-button pattern (table + bgcolor
attribute + mso-padding-alt) rather than a bare styled <a>, since Outlook
desktop's Word rendering engine unreliably applies CSS background/padding
on inline anchors. border-radius still degrades to a square corner in
Outlook specifically -- that's the one known, accepted gap; a rounded
corner there needs VML, not done here.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

OLD_BUTTON_TEMPLATE = (
    '<p style="margin:0;"><a href="{url}" '
    'style="display:inline-block;background:#0A1628;color:#ffffff;text-decoration:none;'
    'padding:12px 24px;border-radius:6px;font-family:Arial,Helvetica,sans-serif;'
    'font-size:15px;font-weight:600;">{label}</a></p>'
)

NEW_BUTTON_TEMPLATE = (
    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0;"><tr>'
    '<td bgcolor="#10E07C" style="border-radius:6px;mso-padding-alt:12px 24px;">'
    '<a href="{url}" style="display:inline-block;padding:12px 24px;'
    'font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:600;'
    'color:#0a1015;text-decoration:none;border-radius:6px;">{label}</a>'
    '</td></tr></table>'
)

URL = "https://citemetrix.com/pricing/?utm_source=nurture&amp;utm_medium=email&amp;utm_campaign=freecheck&amp;utm_content=nurture"
LABELS = {1: "See the Full Picture", 2: "Start Fixing Your Visibility", 3: "See Plans &amp; Pricing"}


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    with conn.cursor() as cur:
        cur.execute("SELECT active FROM drip_campaigns WHERE id=11")
        row = cur.fetchone()
        if not row:
            print("ERROR: campaign 11 not found. Aborting.")
            return
        if row["active"]:
            print("ERROR: campaign 11 is active=1 -- refusing to edit a live campaign's content unattended.")
            return

        for step_order, label in LABELS.items():
            old = OLD_BUTTON_TEMPLATE.format(url=URL, label=label)
            new = NEW_BUTTON_TEMPLATE.format(url=URL, label=label)
            cur.execute("SELECT body_html FROM drip_steps WHERE campaign_id=11 AND step_order=%s", (step_order,))
            current = cur.fetchone()["body_html"]
            if old not in current:
                print(f"step {step_order}: OLD button markup not found verbatim -- skipping, not guessing.")
                continue
            updated = current.replace(old, new)
            cur.execute(
                "UPDATE drip_steps SET body_html=%s WHERE campaign_id=11 AND step_order=%s",
                (updated, step_order)
            )
            print(f"step {step_order}: button updated ({cur.rowcount} row)")
        conn.commit()
    conn.close()
    print("Done. Campaign remains active=0 (dark).")


if __name__ == "__main__":
    main()
