#!/usr/bin/env python3
"""step1brief.md SS14.1: restore the two computed-personalization pieces that
were flattened in step 3d's first pass on campaign 11 (id 11), before it is
ever activated. See leads_drip.py's render_merge_tags()/_platform_stats()
for the corrected mechanism (mentioned/checked counts + missing platforms,
derived from platform_results -- NOT the brand_recognition/category_visibility
fields the WP plugin's own version reads, which don't exist in the real data
and have never fired in production; see MARKETING-MODULE-AUDIT-REPORT.md
Addendum 15 for the full finding).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

STEP1_BODY = (
    "The gap the free check just showed you\n\n"
    "A couple of days ago you checked how AI platforms see {{brand_name}}. "
    "AI mentioned you on {{platforms_mentioned_count}} of {{platforms_checked_count}} "
    "platforms we tested -- a category visibility score of {{model_score}}/100.\n\n"
    "Here's why that number matters more every day:\n"
    "- Over 50% of B2B buyers now start research in AI chatbots\n"
    "- AI-referred traffic converts at 4.4x the rate of organic search\n"
    "- The AEO category has grown 2,000% in the past year\n\n"
    "When AI doesn't mention your brand, you're not just missing traffic "
    "-- you're losing to competitors who are being mentioned.\n\n"
    "See the Full Picture: https://citemetrix.com/pricing/?utm_source=nurture&utm_medium=email&utm_campaign=freecheck&utm_content=nurture"
)

STEP1_BODY_HTML = (
    '<h2 style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:22px;color:#0d1b2a;">The gap the free check just showed you</h2>'
    '<p style="margin:0 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">'
    'A couple of days ago you checked how AI platforms see <strong>{{brand_name}}</strong>. '
    'AI mentioned you on <strong>{{platforms_mentioned_count}} of {{platforms_checked_count}}</strong> platforms we tested '
    '&#8212; a category visibility score of <strong>{{model_score}}/100</strong>.</p>'
    '<p style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">Here&#8217;s why that number matters more every day:</p>'
    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:20px;">'
    '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Over <strong>50% of B2B buyers</strong> now start research in AI chatbots</td></tr>'
    '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; AI-referred traffic converts at <strong>4.4x the rate</strong> of organic search</td></tr>'
    '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; The AEO category has grown <strong>2,000%</strong> in the past year</td></tr>'
    '</table>'
    '<p style="margin:0 0 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">When AI doesn&#8217;t mention your brand, you&#8217;re not just missing traffic &#8212; you&#8217;re losing to competitors who <em>are</em> being mentioned.</p>'
    '<p style="margin:0;"><a href="https://citemetrix.com/pricing/?utm_source=nurture&amp;utm_medium=email&amp;utm_campaign=freecheck&amp;utm_content=nurture" '
    'style="display:inline-block;background:#0A1628;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:600;">See the Full Picture</a></p>'
)

STEP2_BODY = (
    "What happens when AI starts recommending you\n\n"
    "Last time we checked, {{brand_name}} wasn't showing up on {{missing_platforms}}.\n\n"
    "Here's what typically changes when brands improve their AI visibility:\n"
    "- AI platforms begin citing your content as a source\n"
    "- Brand mentions shift from generic to specific and accurate\n"
    "- Referral traffic from AI sources starts appearing in analytics\n\n"
    "CiteMetrix doesn't just monitor these changes -- it gives you 9 built-in "
    "remediation tools to make them happen. Monitor + Fix. No other platform does both.\n\n"
    "Start Fixing Your Visibility: https://citemetrix.com/pricing/?utm_source=nurture&utm_medium=email&utm_campaign=freecheck&utm_content=nurture"
)

STEP2_BODY_HTML = (
    '<h2 style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:22px;color:#0d1b2a;">What happens when AI starts recommending you</h2>'
    '<p style="margin:0 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">'
    'Last time we checked, <strong>{{brand_name}}</strong> wasn&#8217;t showing up on <strong>{{missing_platforms}}</strong>.</p>'
    '<p style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">Here&#8217;s what typically changes when brands improve their AI visibility:</p>'
    '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:20px;">'
    '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; AI platforms begin <strong>citing your content</strong> as a source</td></tr>'
    '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Brand mentions shift from generic to <strong>specific and accurate</strong></td></tr>'
    '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Referral traffic from AI sources <strong>starts appearing</strong> in analytics</td></tr>'
    '</table>'
    '<p style="margin:0 0 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">CiteMetrix doesn&#8217;t just monitor these changes &#8212; it gives you 9 built-in remediation tools to make them happen. <strong>Monitor + Fix.</strong> No other platform does both.</p>'
    '<p style="margin:0;"><a href="https://citemetrix.com/pricing/?utm_source=nurture&amp;utm_medium=email&amp;utm_campaign=freecheck&amp;utm_content=nurture" '
    'style="display:inline-block;background:#0A1628;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:600;">Start Fixing Your Visibility</a></p>'
)


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM drip_campaigns WHERE id=11 AND name=%s", ("Free-Check Nurture — Generic",))
        if not cur.fetchone():
            print("ERROR: campaign 11 not found or name mismatch -- aborting, not touching anything.")
            conn.close()
            return
        cur.execute("SELECT active FROM drip_campaigns WHERE id=11")
        if cur.fetchone()["active"]:
            print("ERROR: campaign 11 is active=1 -- refusing to modify a live campaign's content unattended. Aborting.")
            conn.close()
            return

        cur.execute(
            "UPDATE drip_steps SET body=%s, body_html=%s WHERE campaign_id=11 AND step_order=1",
            (STEP1_BODY, STEP1_BODY_HTML)
        )
        print(f"step 1 updated: {cur.rowcount} row(s)")
        cur.execute(
            "UPDATE drip_steps SET body=%s, body_html=%s WHERE campaign_id=11 AND step_order=2",
            (STEP2_BODY, STEP2_BODY_HTML)
        )
        print(f"step 2 updated: {cur.rowcount} row(s)")
        conn.commit()

    conn.close()
    print("Done. Campaign remains active=0 (dark).")


if __name__ == "__main__":
    main()
