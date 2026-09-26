#!/usr/bin/env python3
"""Step 1b: migrate wp_citemetrix_score_leads -> the unified admin_portal.leads pool.

Per decisions-v2 SS1 + step-1-brief.md SS4/SS5: idempotent (upsert on
legacy_source_table+legacy_id, event inserts skip if already present for that
lead+type), reversible (DROP TABLE -- nothing else references these tables yet),
dark (writes ONLY to admin_portal; wp_citemetrix_score_leads is read-only here).

Per-lead:
  - leads row: identity + original_source='inbound' (step1brief.md SS13.2:
    renamed from 'paid' -- the free-check form is a capture mechanism, not a
    channel, and almost none of its leads arrived via anything paid; the
    channel detail belongs in source_refs, not this value) + excluded (own
    flag, not suppression) + suppressed_at/suppression_reason (seeded from
    ses_suppressions by email match) + wp_nurture_owned (1 if any WP touch
    flag is set) + stage (derived: converted=1 -> won; else has-a-touch ->
    contacted; else -> new) + ai_referral_platform (step1brief.md SS13.2:
    utm_source matched against known AI-assistant domains -- a stopgap
    answer to "how many leads come from AI platforms" ahead of source_refs
    carrying a real channel taxonomy at step 7)
  - one 'free_check_completed' lead_event carrying model_score/scan_query/
    platform_results/brand_name/domain/utm_*/source_page in payload (never
    denormalized onto the lead -- brand_name is the SCANNED domain, not the
    lead's employer, per decisions-v2 SS8 correction)
  - one 'touch_sent' lead_event per WP flag that's set (results_email_sent,
    nurture_1/2/3_sent) -- no per-touch timestamp exists in the source data,
    so occurred_at uses the lead's created_at for all of them (documented
    limitation, not a bug: the source table only ever stored booleans)

Explicitly NOT done here (out of this migration's stated scope):
  - company is left NULL (brand_name is not company -- decisions-v2 SS8)
  - dup_status stays 'unique' for all rows, including the 5 internally-
    duplicate emails -- the dedupe detector is an explicitly separate,
    deferred pass (decisions-v2 SS4.2 / step-1-brief SS5's "trigger, not a
    step"), not part of this migration. The 5 duplicate emails are reported
    in this script's output for visibility, not flagged in the DB.

2026-09-02 (step1brief.md SS14.2): for a genuinely NEW lead only (not on a
re-sync of an existing one), also creates a source_refs row (path='inbound',
kind='signup') + a lead_source_touches row, sets original_source_ref_id, and
calls leads_drip.enroll_batch() for that one-row batch into the generic
free-check nurture campaign (looked up by name, not hardcoded id). This is
the "smaller path" of the two SS14.2 named -- reuses enroll_batch()'s
existing gates (suppression, lead_pool.is_eligible()) unchanged rather than
building a separate rule-based enroller. The campaign stays active=0 (dark)
throughout this build -- enrollment rows can be created safely (no FK/gate
issue) but nothing sends until the campaign is activated, which per SS14.3
must not happen before the WordPress plugin stops enrolling new leads. Both
are still gated behind an explicit go.

Usage: python3 migrate_step1b.py [--dry-run]
"""
import os
import sys
import json
import datetime
import secrets

DRY_RUN = "--dry-run" in sys.argv
UNSUB_TOKEN_LEN = 32  # matches leads_drip.py's cold-lead path -- one token scheme, not two

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(HERE)
sys.path.insert(0, BASE)

from dotenv import load_dotenv
load_dotenv(os.path.join(BASE, ".env"))
import pymysql
import leads_drip
import lead_pool

GENERIC_NURTURE_CAMPAIGN_NAME = "Free-Check Nurture — Generic"


def admin_db():
    return pymysql.connect(
        host="localhost", user=os.getenv("ADMIN_DB_USER", "adminportal"),
        password=os.getenv("ADMIN_DB_PASSWORD"), database=os.getenv("ADMIN_DB_NAME", "admin_portal"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


def product_db():
    return pymysql.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
        charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
    )


LEGACY_TABLE = "wp_citemetrix_score_leads"
TOUCH_FLAGS = ["results_email_sent", "nurture_1_sent", "nurture_2_sent", "nurture_3_sent"]

# CODE-BRIEF-WARM-NURTURE-NEVER-SENT-2026-09-11.md SS4: wp_nurture_owned must reflect
# WordPress's NURTURE sequence actually owning this lead -- not the fact that they
# received their scan results, which is the product functioning, not a competing
# sequence. results_email_sent fires within the same minute as every free check,
# for every lead, unconditionally -- folding it into TOUCH_FLAGS here meant
# wp_nurture_owned was landing on 1 for essentially every inbound lead, permanently,
# confirmed against live data: 44 of 45 inbound leads had wp_nurture_owned=1 with
# nurture_1/2/3_sent all still 0 -- they were never actually touched by a real WP
# nurture send, only by the unrelated results email. This is the ONLY site that
# computes wp_nurture_owned (grepped before editing, per the brief's SS4 instruction).
NURTURE_OWNERSHIP_FLAGS = ["nurture_1_sent", "nurture_2_sent", "nurture_3_sent"]

# step1brief.md SS13.2: matched against utm_source. Domain-fragment match (not
# exact-equality) since a real value seen in this data is 'chatgpt.com', not a
# bare platform name -- covers the assistants CiteMetrix's own product tracks
# (see CLAUDE.md's business description) plus each one's parent-company domain.
AI_PLATFORM_DOMAINS = {
    "chatgpt": "ChatGPT", "openai": "ChatGPT",
    "perplexity": "Perplexity",
    "claude": "Claude", "anthropic": "Claude",
    "gemini": "Gemini", "bard.google": "Gemini",
    "copilot": "Copilot",
    "grok": "Grok", "x.ai": "Grok",
}


def classify_ai_referral(utm_source):
    if not utm_source:
        return None
    u = utm_source.strip().lower()
    for fragment, platform in AI_PLATFORM_DOMAINS.items():
        if fragment in u:
            return platform
    return None


def main():
    snapshot_at = datetime.datetime.now().replace(microsecond=0)
    print(f"[{snapshot_at}] Step 1b migration {'(DRY RUN)' if DRY_RUN else ''}")

    prod = product_db()
    admin = admin_db()

    with prod.cursor() as pcur:
        pcur.execute(f"SELECT * FROM {LEGACY_TABLE} ORDER BY id")
        score_leads = pcur.fetchall()
    prod.close()

    print(f"Read {len(score_leads)} rows from {LEGACY_TABLE}")

    # Report (not flag) the internally-duplicate emails, per decisions-v2 SS4.2 --
    # the dedupe detector is a separate, deferred pass, not part of this migration.
    by_email = {}
    for r in score_leads:
        by_email.setdefault(r["email"], []).append(r["id"])
    dups = {e: ids for e, ids in by_email.items() if len(ids) > 1}
    if dups:
        print(f"NOTE: {len(dups)} email(s) appear more than once in the source table (not merged, not flagged -- see docstring):")
        for e, ids in dups.items():
            print(f"  {e}: legacy ids {ids}")

    with admin.cursor() as acur:
        migrated, touch_events_inserted, scan_events_inserted, suppressed_count, ai_referral_count = 0, 0, 0, 0, 0
        auto_enrolled_count, auto_enroll_skipped_count = 0, 0
        # CODE-BRIEF-WARM-NURTURE-NEVER-SENT-2026-09-11.md SS3 Q5/SS5: enroll_batch()'s
        # skip reason was being thrown away here (only enrolled-or-not survived), which is
        # exactly how 42 silent pre-enrollment rejections left zero trace anywhere -- not
        # even a bad one, just nothing. Kept broken out by reason (not summed into
        # auto_enroll_skipped_count) so a real bug (skipped_nurture_owned climbing) reads
        # differently from expected behavior (skipped_no_email on a LinkedIn-only lead).
        skip_reasons = {'nurture_owned': 0, 'cluster_conflict': 0, 'no_email': 0, 'suppressed': 0, 'other': 0}

        # 2026-09-25: redirected from the legacy drip_campaigns/enroll_batch() path to the
        # new engine (automations/automation_runs) -- the legacy campaign's send function
        # (process_due_enrollments()) was removed entirely today as part of retiring the
        # dead legacy system, so continuing to enroll here would silently orphan every new
        # free-check lead going forward (exactly the bug this replaces -- see the alfixit04
        # incident). Automation 8 starts at 'delay1' (a 2-day wait node), matching legacy
        # campaign 11's own step_order=1 day_offset=2 -- not skipping straight to send1.
        acur.execute("SELECT id FROM automations WHERE name=%s", (GENERIC_NURTURE_CAMPAIGN_NAME,))
        automation_row = acur.fetchone()
        generic_automation_id = automation_row["id"] if automation_row else None
        if not generic_automation_id:
            print(f"NOTE: automation '{GENERIC_NURTURE_CAMPAIGN_NAME}' not found -- new leads will not be auto-enrolled this run.")

        for r in score_leads:
            # has_touch (stage classification: "has this person been contacted at all,
            # by anything") correctly still includes results_email_sent -- receiving your
            # scan results is a real touch. wp_owns_nurture (the interlock) must NOT --
            # see NURTURE_OWNERSHIP_FLAGS above.
            has_touch = any(r[f] for f in TOUCH_FLAGS)
            wp_owns_nurture = any(r[f] for f in NURTURE_OWNERSHIP_FLAGS)
            stage = "won" if r["converted"] else ("contacted" if has_touch else "new")
            # Real timestamp exists for 'won' (converted_at); no finer-grained data exists for
            # exactly when a touch happened, so 'contacted'/'new' fall back to created_at.
            stage_entered_at = r["converted_at"] if (stage == "won" and r["converted_at"]) else r["created_at"]

            # Suppression seed: join on email against ses_suppressions.
            acur.execute("SELECT reason FROM ses_suppressions WHERE email_address = %s", (r["email"],))
            sup = acur.fetchone()
            suppressed_at, suppression_reason = None, None
            if sup:
                suppressed_at = snapshot_at
                suppression_reason = "bounced" if sup["reason"] == "BOUNCE" else "complained"
                suppressed_count += 1

            ai_referral_platform = classify_ai_referral(r.get("utm_source"))
            if ai_referral_platform:
                ai_referral_count += 1

            if DRY_RUN:
                migrated += 1
                continue

            # step1brief.md SS14.2: must know new-vs-existing BEFORE the upsert,
            # since ON DUPLICATE KEY UPDATE makes that otherwise unrecoverable.
            # Only a genuinely new signup gets a source_refs row + auto-enroll --
            # never a re-sync of one of the original 43, which would retroactively
            # pull already-nurture-owned leads into the new engine.
            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            is_new_lead = acur.fetchone() is None
            # generated unconditionally but only ever used on INSERT (not in the
            # ON DUPLICATE KEY UPDATE clause below) -- re-issuing a token on every
            # sync would silently break any unsubscribe link already sent
            unsubscribe_token = secrets.token_urlsafe(UNSUB_TOKEN_LEN)

            acur.execute(
                """INSERT INTO leads
                     (email, original_source, created_at, stage, stage_entered_at,
                      suppressed_at, suppression_reason, excluded, wp_nurture_owned,
                      ai_referral_platform, unsubscribe_token, legacy_id, legacy_source_table)
                   VALUES (%s,'inbound',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON DUPLICATE KEY UPDATE
                     email=VALUES(email),
                     -- Sales > Pipeline (2026-09-03 addendum) lets a human move a lead through
                     -- contacted/engaged/qualified manually -- this sync must never overwrite that
                     -- on the next hourly run. 'won' is the one exception: a real WooCommerce
                     -- conversion is a hard external signal that should always win, even over a
                     -- lead someone had manually marked 'lost'. Anything else computed here
                     -- ('contacted'/'new') is only ever used to seed a genuinely NEW row (the
                     -- INSERT above), never to correct an existing one.
                     stage = IF(VALUES(stage)='won', 'won', stage),
                     stage_entered_at = IF(VALUES(stage)='won' AND stage<>'won', VALUES(stage_entered_at), stage_entered_at),
                     suppressed_at=VALUES(suppressed_at), suppression_reason=VALUES(suppression_reason),
                     excluded=VALUES(excluded), wp_nurture_owned=VALUES(wp_nurture_owned),
                     ai_referral_platform=VALUES(ai_referral_platform)""",
                (r["email"], r["created_at"], stage, stage_entered_at,
                 suppressed_at, suppression_reason, int(r["excluded"]), int(wp_owns_nurture),
                 ai_referral_platform, unsubscribe_token, r["id"], LEGACY_TABLE)
            )
            acur.execute(
                "SELECT id FROM leads WHERE legacy_source_table=%s AND legacy_id=%s",
                (LEGACY_TABLE, r["id"])
            )
            lead_id = acur.fetchone()["id"]

            # free_check_completed event -- idempotent: skip if this lead already has one.
            acur.execute(
                "SELECT id FROM lead_events WHERE lead_id=%s AND type='free_check_completed'", (lead_id,)
            )
            if not acur.fetchone():
                payload = json.dumps({
                    "domain": r["domain"], "brand_name": r["brand_name"], "model_score": r["model_score"],
                    "scan_query": r["scan_query"], "utm_source": r["utm_source"], "utm_medium": r["utm_medium"],
                    "utm_campaign": r["utm_campaign"], "source_page": r["source_page"],
                    "platform_results": r["platform_results"][:2000] if r["platform_results"] else None,
                }, default=str)
                acur.execute(
                    """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                       VALUES (%s,%s,'free_check_completed','web',%s)""",
                    (lead_id, r["created_at"], payload)
                )
                scan_events_inserted += 1

            # step1brief.md SS14.2: new-lead auto-enrollment. Gives free-check
            # signups the same source_refs+lead_source_touches path cold leads
            # already have, then runs them through enroll_batch() unchanged --
            # every existing gate (suppression, lead_pool.is_eligible()) applies
            # exactly as it does for a CSV import. Campaign stays active=0
            # (dark) until SS14.3's cutover, so this creates real enrollment
            # rows but cannot cause a real send.
            if is_new_lead and generic_automation_id:
                # step1brief.md SS17 step 4: settle the ai_referral_platform placement
                # question by giving it a real home in source_refs.source instead of
                # only the denormalized leads.ai_referral_platform column -- prefer the
                # AI-platform classification, then the lead's real utm_source, and only
                # fall back to the generic 'free_check' label when both are genuinely empty.
                signup_source = ai_referral_platform or (r.get("utm_source") or "").strip().lower()[:80] or "free_check"
                acur.execute(
                    "INSERT INTO source_refs (path, kind, label, source) VALUES ('inbound','signup',%s,%s)",
                    (r["email"], signup_source)
                )
                source_ref_id = acur.lastrowid
                acur.execute(
                    "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,'inbound',%s)",
                    (lead_id, source_ref_id)
                )
                acur.execute("UPDATE leads SET original_source_ref_id=%s WHERE id=%s", (source_ref_id, lead_id))
                tag_names = ["channel:free_check"]
                if ai_referral_platform:
                    tag_names.append(f"campaign:{ai_referral_platform.lower()}")
                leads_drip.tag_lead(acur, lead_id, tag_names)
                admin.commit()
                if not r.get("email"):
                    auto_enroll_skipped_count += 1
                    skip_reasons['no_email'] += 1
                else:
                    eligible, elig_reason = lead_pool.is_eligible(acur, lead_id)
                    if eligible:
                        run_result = leads_drip._create_or_revive_run(acur, admin, generic_automation_id, lead_id, 'delay1')
                        if run_result['enrolled']:
                            auto_enrolled_count += 1
                        else:
                            auto_enroll_skipped_count += 1
                    elif elig_reason.startswith('suppressed_at='):
                        auto_enroll_skipped_count += 1
                        skip_reasons['suppressed'] += 1
                    elif 'wp_nurture_owned' in elig_reason:
                        auto_enroll_skipped_count += 1
                        skip_reasons['nurture_owned'] += 1
                    elif 'active enrollment' in elig_reason:
                        auto_enroll_skipped_count += 1
                        skip_reasons['cluster_conflict'] += 1
                    else:
                        auto_enroll_skipped_count += 1
                        skip_reasons['other'] += 1

            # touch_sent events -- one per WP flag set, idempotent per (lead, flag) via payload check.
            for flag in TOUCH_FLAGS:
                if not r[flag]:
                    continue
                acur.execute(
                    """SELECT id FROM lead_events WHERE lead_id=%s AND type='touch_sent'
                       AND JSON_EXTRACT(payload, '$.touch') = %s""",
                    (lead_id, flag)
                )
                if acur.fetchone():
                    continue
                acur.execute(
                    """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                       VALUES (%s,%s,'touch_sent','email',%s)""",
                    (lead_id, r["created_at"], json.dumps({"touch": flag, "note": "no per-touch timestamp in source data"}))
                )
                touch_events_inserted += 1

            migrated += 1

        if not DRY_RUN:
            admin.commit()

        if not DRY_RUN:
            # CODE-BRIEF-WARM-NURTURE-NEVER-SENT-2026-09-11.md SS5: same reasoning as
            # drip_cron.py's _log_run -- total_problem is what the dashboard's job-health
            # check keys off. A real bug shows up as skip_reasons['nurture_owned'] or
            # ['cluster_conflict'] climbing on genuinely new leads; no_email/suppressed
            # are expected, routine skips (LinkedIn-only contacts, bounced addresses) and
            # are logged for visibility but don't count toward the anomaly signal.
            total_problem = skip_reasons['nurture_owned'] + skip_reasons['cluster_conflict'] + skip_reasons['other']
            acur.execute(
                "INSERT INTO cron_run_log (job_name, total_processed, total_problem, detail_json) VALUES (%s,%s,%s,%s)",
                ('lead_migration_sync', migrated, total_problem, json.dumps({
                    'auto_enrolled': auto_enrolled_count,
                    'auto_enroll_skipped': auto_enroll_skipped_count,
                    'skip_reasons': skip_reasons,
                }))
            )
            admin.commit()

    admin.close()

    print(f"Migrated: {migrated} leads. Suppression matches: {suppressed_count}. "
          f"New scan events: {scan_events_inserted}. New touch events: {touch_events_inserted}. "
          f"AI-platform referrals: {ai_referral_count}. "
          f"Auto-enrolled into generic nurture: {auto_enrolled_count} (skipped/ineligible: {auto_enroll_skipped_count}, "
          f"by reason: {skip_reasons}).")
    print(f"Snapshot timestamp: {snapshot_at} (free-check signups arriving after this point are not yet in the pool -- expected, see step-1-brief.md SS5.3)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        from cron_alert import alert_and_exit
        alert_and_exit('migrate_step1b.py', e)
