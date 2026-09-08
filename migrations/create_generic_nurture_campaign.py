#!/usr/bin/env python3
"""step 3d (step1brief.md SS13.1): create the generic free-check nurture
sequence in the consolidated engine. Content ported from
class-citemetrix-free-score.php's build_nurture_{1,2,3}_html() -- see
citemetrix-nurture-content-extraction.md for the full extraction record.

Deliberately created active=0 (dark). Decisions-v2 steps 4-5 (the nurture
cutover) is what flips wp_nurture_owned off for real leads and actually
activates this -- 3d's job is only to get the content into the engine,
ready for that switch, not to start sending.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql

FOOTER = ""  # unsubscribe footer is appended at send time by leads_drip.py, not stored here

STEPS = [
    {
        "day_offset": 2,
        "subject": "The shift is happening faster than you think",
        "body": (
            "The gap the free check just showed you\n\n"
            "A couple of days ago you checked how AI platforms see {{brand_name}}. "
            "Your category visibility scored {{model_score}}/100.\n\n"
            "Here's why that number matters more every day:\n"
            "- Over 50% of B2B buyers now start research in AI chatbots\n"
            "- AI-referred traffic converts at 4.4x the rate of organic search\n"
            "- The AEO category has grown 2,000% in the past year\n\n"
            "When AI doesn't mention your brand, you're not just missing traffic "
            "-- you're losing to competitors who are being mentioned.\n\n"
            "See the Full Picture: https://citemetrix.com/pricing/?utm_source=nurture&utm_medium=email&utm_campaign=freecheck&utm_content=nurture"
        ),
        "body_html": (
            '<h2 style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:22px;color:#0d1b2a;">The gap the free check just showed you</h2>'
            '<p style="margin:0 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">'
            'A couple of days ago you checked how AI platforms see <strong>{{brand_name}}</strong>. '
            'Your category visibility scored <strong>{{model_score}}/100</strong>.</p>'
            '<p style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">Here&#8217;s why that number matters more every day:</p>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:20px;">'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Over <strong>50% of B2B buyers</strong> now start research in AI chatbots</td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; AI-referred traffic converts at <strong>4.4x the rate</strong> of organic search</td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; The AEO category has grown <strong>2,000%</strong> in the past year</td></tr>'
            '</table>'
            '<p style="margin:0 0 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">When AI doesn&#8217;t mention your brand, you&#8217;re not just missing traffic &#8212; you&#8217;re losing to competitors who <em>are</em> being mentioned.</p>'
            '<p style="margin:0;"><a href="https://citemetrix.com/pricing/?utm_source=nurture&amp;utm_medium=email&amp;utm_campaign=freecheck&amp;utm_content=nurture" '
            'style="display:inline-block;background:#0A1628;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:600;">See the Full Picture</a></p>'
        ),
    },
    {
        "day_offset": 5,
        "subject": "What happens when AI starts recommending you",
        "body": (
            "What happens when AI starts recommending you\n\n"
            "Last time we checked, {{brand_name}} had room to grow across key AI platforms.\n\n"
            "Here's what typically changes when brands improve their AI visibility:\n"
            "- AI platforms begin citing your content as a source\n"
            "- Brand mentions shift from generic to specific and accurate\n"
            "- Referral traffic from AI sources starts appearing in analytics\n\n"
            "CiteMetrix doesn't just monitor these changes -- it gives you 9 built-in "
            "remediation tools to make them happen. Monitor + Fix. No other platform does both.\n\n"
            "Start Fixing Your Visibility: https://citemetrix.com/pricing/?utm_source=nurture&utm_medium=email&utm_campaign=freecheck&utm_content=nurture"
        ),
        "body_html": (
            '<h2 style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:22px;color:#0d1b2a;">What happens when AI starts recommending you</h2>'
            '<p style="margin:0 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">'
            'Last time we checked, <strong>{{brand_name}}</strong> had room to grow across key AI platforms.</p>'
            '<p style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">Here&#8217;s what typically changes when brands improve their AI visibility:</p>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:20px;">'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; AI platforms begin <strong>citing your content</strong> as a source</td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Brand mentions shift from generic to <strong>specific and accurate</strong></td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Referral traffic from AI sources <strong>starts appearing</strong> in analytics</td></tr>'
            '</table>'
            '<p style="margin:0 0 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">CiteMetrix doesn&#8217;t just monitor these changes &#8212; it gives you 9 built-in remediation tools to make them happen. <strong>Monitor + Fix.</strong> No other platform does both.</p>'
            '<p style="margin:0;"><a href="https://citemetrix.com/pricing/?utm_source=nurture&amp;utm_medium=email&amp;utm_campaign=freecheck&amp;utm_content=nurture" '
            'style="display:inline-block;background:#0A1628;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:600;">Start Fixing Your Visibility</a></p>'
        ),
    },
    {
        "day_offset": 8,
        "subject": "Your AI visibility won't fix itself",
        "body": (
            "Your AI visibility won't fix itself\n\n"
            "A week ago, {{brand_name}} scored {{model_score}}/100 on our free AI visibility check.\n\n"
            "That score isn't going to improve on its own. AI platforms update constantly, and "
            "without optimization your brand risks falling further behind.\n\n"
            "CiteMetrix gives you everything you need to monitor and improve:\n"
            "- Continuous monitoring across 10+ AI platforms\n"
            "- ModelScore(tm) tracking with 30-day trends\n"
            "- Built-in remediation tools to close the gaps\n"
            "- Competitor visibility comparison\n"
            "- Professional reports for stakeholders\n\n"
            "No long-term contracts. Cancel anytime.\n\n"
            "See Plans & Pricing: https://citemetrix.com/pricing/?utm_source=nurture&utm_medium=email&utm_campaign=freecheck&utm_content=nurture"
        ),
        "body_html": (
            '<h2 style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:22px;color:#0d1b2a;">Your AI visibility won&#8217;t fix itself</h2>'
            '<p style="margin:0 0 20px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">'
            'A week ago, <strong>{{brand_name}}</strong> scored <strong>{{model_score}}/100</strong> on our free AI visibility check.</p>'
            '<p style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">That score isn&#8217;t going to improve on its own. AI platforms update constantly, and without optimization your brand risks falling further behind.</p>'
            '<p style="margin:0 0 16px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">CiteMetrix gives you everything you need to monitor and improve:</p>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="margin-bottom:20px;">'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Continuous monitoring across <strong>10+ AI platforms</strong></td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; ModelScore&#8482; tracking with 30-day trends</td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Built-in remediation tools to close the gaps</td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Competitor visibility comparison</td></tr>'
            '<tr><td style="padding:8px 0;font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:#2d3742;">&#8226; Professional reports for stakeholders</td></tr>'
            '</table>'
            '<p style="margin:0 0 24px;font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.65;color:#2d3742;">No long-term contracts. Cancel anytime.</p>'
            '<p style="margin:0;"><a href="https://citemetrix.com/pricing/?utm_source=nurture&amp;utm_medium=email&amp;utm_campaign=freecheck&amp;utm_content=nurture" '
            'style="display:inline-block;background:#0A1628;color:#ffffff;text-decoration:none;padding:12px 24px;border-radius:6px;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:600;">See Plans &amp; Pricing</a></p>'
        ),
    },
]


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def main():
    conn = admin_db()
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM drip_campaigns WHERE name = %s", ("Free-Check Nurture — Generic",))
        existing = cur.fetchone()
        if existing:
            print(f"Campaign already exists (id {existing['id']}) -- not creating a duplicate.")
            conn.close()
            return

        cur.execute(
            "INSERT INTO drip_campaigns (name, description, active) VALUES (%s,%s,0)",
            (
                "Free-Check Nurture — Generic",
                "step 3d (step1brief.md SS13.1): the generic segment of the free-check nurture "
                "sequence, ported from class-citemetrix-free-score.php's build_nurture_*_html(). "
                "Created dark (active=0) -- decisions-v2 steps 4-5 (the nurture cutover) is what "
                "activates this and enrolls real leads. Agency/comparison content kept as text in "
                "citemetrix-nurture-content-extraction.md (0 and 1 lead respectively as of this "
                "writing); port them the day either segment earns real volume.",
            )
        )
        conn.commit()
        campaign_id = cur.lastrowid
        print(f"Created campaign id {campaign_id}")

        for i, step in enumerate(STEPS, start=1):
            cur.execute(
                """INSERT INTO drip_steps (campaign_id, step_order, day_offset, subject, body, body_html, is_survey_step)
                   VALUES (%s,%s,%s,%s,%s,%s,0)""",
                (campaign_id, i, step["day_offset"], step["subject"], step["body"], step["body_html"])
            )
            print(f"  step {i}: day_offset={step['day_offset']} subject={step['subject']!r}")
        conn.commit()

    conn.close()
    print("Done. Campaign is active=0 (dark) -- no enrollment, no sends, until the cutover.")


if __name__ == "__main__":
    main()
