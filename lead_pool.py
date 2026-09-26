"""Enrollment gate for the unified lead pool.

Encodes the two hard invariants decisions-v2 specifies for the sequence engine:

  1. Suppression is a hard gate, checked at send/enroll time, not just at
     enrollment time (decisions-v2 SS1: "The enrollment query must filter
     suppressed_at IS NULL at send time, not at enrollment time.").
  2. wp_nurture_owned = 0 -- the interlock (step-1-brief.md SS1). A lead the
     WordPress nurture cron already owns may never be enrolled here; that's
     what makes the nurture cutover safe to do in stages.
  3. One ACTIVE enrollment per person_cluster_id, not per lead id
     (decisions-v2 SS4.2) -- two lead rows that are really the same human
     (flagged possible_dup, same cluster) must not both be actively enrolled
     at once, which is how the same person gets emailed twice by two "different"
     leads. A lead with no cluster (person_cluster_id IS NULL) is only ever
     checked against enrollments on that exact lead id.
  4. excluded = 0 -- test/internal traffic flagged via the leads page's
     manual exclude toggle (propagates from wp_citemetrix_score_leads.excluded
     through migrate_step1b.py's upsert). If a row isn't real enough to count
     in reporting, it isn't real enough to email (step1brief.md SS12.2).

2026-09-02 (step 3c, step1brief (3).md SS11.1): repointed at drip_enrollments,
the table the consolidated engine (leads_drip.py) actually reads and writes
as of step 3b. lead_enrollments (the pristine step-1a table this was
originally built against) has 0 rows and no writer post-3b and is not the
live enrollment record. Wired into leads_drip.py's enroll_batch() (pre-
enrollment gate) and process_due_enrollments() (send-time re-verification,
per the decisions-v2 principle above) -- this module is no longer dark.

2026-09-02 (step1brief.md SS12.2): added the excluded=0 check. Discovered
missing after lead 43 -- a test/debris row synced in from WordPress -- was
found to be the one real lead the gate had ever returned eligible for, and
turned out to already be enrollable/mailable alongside 7 other excluded
test/internal rows, since nothing in this function looked at the flag that
exists specifically to keep them out of both reporting and sending.
"""


def is_eligible(cur, lead_id, exclude_enrollment_id=None, exclude_run_id=None, skip_wp_nurture_check=False):
    """Returns (eligible: bool, reason: str). reason explains a False result;
    for True it's a short confirmation, useful in test/audit output either way.

    exclude_enrollment_id: pass the enrollment row's own id when re-checking a
    lead that is *already* actively enrolled (send-time re-verification) --
    without it, a lead's own active enrollment would always self-block, since
    the active-enrollment checks below can't otherwise tell "this lead's
    existing active row" apart from "a genuine second active enrollment".
    Leave it None for the normal pre-enrollment check.

    exclude_run_id: same idea as exclude_enrollment_id, but for the new
    automation_runs table (schema-split migration, email-builder-spec-2026-09-21.md
    SS3.1) -- pass the run's own id when re-checking a lead already active on
    the new engine. This function checks BOTH drip_enrollments AND
    automation_runs for the one-active-enrollment-per-person_cluster_id
    invariant, since a cluster must not be simultaneously active in both
    engines during the migration's dual-run window -- one shared gate,
    taught to see both tables, never a second copy of this check.

    skip_wp_nurture_check: 2026-09-15 (warm-track priority decision) -- pass
    True ONLY for enrollment into an audience-specific warm track (a lead who
    completed a free check and branched off a cold sequence). Eric's decision:
    when a lead is wanted by BOTH WordPress's generic free-check nurture and
    an admin-portal warm track, the warm track wins -- it's tailored to the
    audience; WP's is generic. This does NOT weaken the interlock for cold
    campaigns (Marblism-style imports never pass this) -- a lead already
    genuinely being nurtured by WordPress still can't be cold-outbound
    spammed. The other invariant -- exactly one system nurtures a lead at a
    time -- is enforced on the WordPress side: leads_drip.py's
    process_check_completions() sets nurture_stood_down=1 on the matching
    wp_citemetrix_score_leads row the moment a warm enrollment succeeds
    (class-citemetrix-free-score.php's process_nurture_queue() now checks
    that flag), so WordPress actually stops, it doesn't just get outvoted.

    2026-09-23 bugfix: during the dual-run window a single logical enrollment can exist
    as a mirrored row in BOTH tables at once (automation_runs.legacy_enrollment_id points
    back at the drip_enrollments row it was migrated from). Passing only
    exclude_enrollment_id or only exclude_run_id excludes the row being re-checked but not
    ITS OWN mirror in the other table -- so the mirror was found as "a second active
    enrollment" and the row false-positive self-blocked, permanently (recover_blocked_
    enrollments()/recover_blocked_runs() re-run this exact check every cycle, so they hit
    the same false positive forever and can never clear it). Confirmed live: lead 37779's
    automation_runs row for Free-Check Nurture -- Generic blocked itself citing
    drip_enrollments.415, its own legacy mirror, as the "other" active enrollment. Fix:
    resolve and exclude the mirror row on whichever side wasn't explicitly passed, so a
    row can never see its own reflection as a conflict.
    """
    cur.execute(
        "SELECT id, suppressed_at, wp_nurture_owned, person_cluster_id, stage, excluded FROM leads WHERE id=%s",
        (lead_id,)
    )
    lead = cur.fetchone()
    if not lead:
        return False, f"lead {lead_id} does not exist"

    if lead["excluded"]:
        return False, "excluded=1 (flagged test/internal traffic, not real lead data)"

    if lead["suppressed_at"] is not None:
        return False, f"suppressed_at={lead['suppressed_at']}"

    if lead["wp_nurture_owned"] and not skip_wp_nurture_check:
        return False, "wp_nurture_owned=1 (WordPress cron owns this lead's nurture)"

    excluded_enrollment_ids = {exclude_enrollment_id} if exclude_enrollment_id else set()
    excluded_run_ids = {exclude_run_id} if exclude_run_id else set()
    if exclude_enrollment_id:
        cur.execute("SELECT id FROM automation_runs WHERE legacy_enrollment_id=%s", (exclude_enrollment_id,))
        excluded_run_ids.update(r["id"] for r in cur.fetchall())
    if exclude_run_id:
        cur.execute("SELECT legacy_enrollment_id FROM automation_runs WHERE id=%s", (exclude_run_id,))
        mirror = cur.fetchone()
        if mirror and mirror["legacy_enrollment_id"]:
            excluded_enrollment_ids.add(mirror["legacy_enrollment_id"])

    exclude_clause = f" AND e.id NOT IN ({','.join(['%s'] * len(excluded_enrollment_ids))})" if excluded_enrollment_ids else ""
    exclude_params = tuple(excluded_enrollment_ids)
    run_exclude_clause = f" AND r.id NOT IN ({','.join(['%s'] * len(excluded_run_ids))})" if excluded_run_ids else ""
    run_exclude_params = tuple(excluded_run_ids)

    if lead["person_cluster_id"] is not None:
        cur.execute(
            f"""SELECT e.lead_id AS lead_id FROM drip_enrollments e
               JOIN leads l ON l.id = e.lead_id
               WHERE l.person_cluster_id = %s AND e.status = 'active'{exclude_clause}
               UNION ALL
               SELECT r.lead_id AS lead_id FROM automation_runs r
               JOIN leads l ON l.id = r.lead_id
               WHERE l.person_cluster_id = %s AND r.status = 'active'{run_exclude_clause}""",
            (lead["person_cluster_id"], *exclude_params, lead["person_cluster_id"], *run_exclude_params)
        )
        active = cur.fetchall()
        if active:
            other_leads = sorted(set(a["lead_id"] for a in active))
            return False, f"person_cluster_id={lead['person_cluster_id']} already has an active enrollment (lead id(s) {other_leads})"
    else:
        cur.execute(
            f"""SELECT e.id AS id FROM drip_enrollments e WHERE e.lead_id=%s AND e.status='active'{exclude_clause}
               UNION ALL
               SELECT r.id AS id FROM automation_runs r WHERE r.lead_id=%s AND r.status='active'{run_exclude_clause}""",
            (lead_id, *exclude_params, lead_id, *run_exclude_params)
        )
        if cur.fetchone():
            return False, "already has an active enrollment"

    return True, "eligible"


def enroll(cur, conn, lead_id, campaign_id):
    """Raises ValueError if the lead is not eligible. Otherwise inserts a
    drip_enrollments row (status='active', due immediately) and returns its
    id. Ad-hoc single-lead helper for tests/verification -- the production
    engine's batch enrollment (leads_drip.enroll_batch) does its own insert
    since it needs per-step due-date math and 'suppressed'-row revival across
    a whole batch; is_eligible() above is the one shared gate both paths call
    through."""
    eligible, reason = is_eligible(cur, lead_id)
    if not eligible:
        raise ValueError(f"lead {lead_id} is not eligible for enrollment: {reason}")

    cur.execute(
        """INSERT INTO drip_enrollments (lead_id, campaign_id, current_step, next_send_due_at, status)
           VALUES (%s,%s,0,NOW(),'active')""",
        (lead_id, campaign_id)
    )
    conn.commit()
    return cur.lastrowid
