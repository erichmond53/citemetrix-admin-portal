"""
Lead Drip Campaigns — CSV-sourced cold leads (Marblism etc.), not CiteMetrix
product users. Deliberately separate from the WP plugin's citemetrix_drip_*
system, which is scoped to actual signed-up accounts (on_signup/on_approval
triggers, {{citation_rate}}/{{login_url}} merge tags) -- none of that applies
to someone who's never touched the product.

Sendy is not used for sending here -- it can't personalize send timing per
lead (day 3 for person A, day 1 for person B, based on when each enrolled),
only blast a whole list/segment at once. Leads come in via direct CSV
upload (Marblism has no export/API; the workflow is copy the rendered
HTML table into a spreadsheet, export CSV, upload here) and sends go out
individually via SES, same helper as the investor outreach tool, with the
same event-tracking configuration set.

2026-09-02 (step 3b, step-1-brief.md SS10.5): repointed at the unified
`leads` pool. Was `drip_leads_legacy` + `drip_leads_legacy.first_seen_batch_id`
+ `lead_batches` -- that table is now retired (renamed, not dropped, see
migrations/retire_legacy_tables.sql). Three things this repoint had to give
a real home to, per SS10.5:
  - unsubscribe_token: added directly to `leads` (the unified schema had
    suppressed_at/suppression_reason for the suppression SIDE of unsubscribe,
    but nothing for the per-lead secret token every send's footer link needs).
  - lead_batches -> source_refs rows (kind='import_batch') + a
    lead_source_touches row per lead, instead of a first_seen_batch_id FK.
    This is where source_refs gets its first real rows, for the Direct path.
  - raw_data (the write-only JSON of unmapped CSV columns) -> lands in the
    new lead's 'sourced' lead_events payload instead of a denormalized
    column with no reader.

2026-09-02 (step 3c, step1brief (3).md SS11.1): 3b repointed this engine at
`leads`, the same table the 43 free-check leads live in -- removing the
accidental protection two physically separate tables used to provide (42 of
those 43 are wp_nurture_owned=1, owned by the WordPress nurture cron).
Verified enrollment is strictly batch-scoped (enroll_batch only ever
enrolls leads with a lead_source_touches row for the given batch_id, and
free-check leads have none -- api_drip_enroll takes only a batch_id, no
route accepts an arbitrary lead_id), so the exposure was theoretical, not
live. Wired lead_pool.is_eligible() in anyway, immediately, as the
deliberate replacement: a pre-enrollment gate in enroll_batch() and a
send-time re-verification in process_due_enrollments() (decisions-v2:
suppression-style gates are checked live, not just once).
"""
import csv
import io
import json
import os
import re
import secrets
from datetime import datetime, timedelta

import requests

import lead_pool
from suppression import is_suppressed


UNSUB_TOKEN_LEN = 32

# ses_suppressions.reason is 'BOUNCE'/'COMPLAINT' (AWS's own vocabulary,
# confirmed to be the only two values this account's real data ever
# produces -- step-1-brief.md SS1). leads.suppression_reason is lowercase
# and has two more values ('unsubscribed', 'manual') sourced elsewhere.
_SES_REASON_MAP = {"BOUNCE": "bounced", "COMPLAINT": "complained"}

# sendy_suppressions.reason is 'unsubscribed'/'bounced'/'complaint' (Sendy's own
# vocabulary, set by the sync in api_sendy_ingest()); only 'complaint' needs
# remapping to match leads.suppression_reason's existing 'complained' spelling.
_SENDY_REASON_MAP = {"unsubscribed": "unsubscribed", "bounced": "bounced", "complaint": "complained"}


def _split_name(full_name: str):
    full_name = (full_name or '').strip()
    if not full_name:
        return '', ''
    parts = full_name.split(None, 1)
    if len(parts) == 1:
        return parts[0], ''
    return parts[0], parts[1]


def parse_leads_csv(file_bytes: bytes) -> list:
    """Parse an uploaded CSV into a list of dicts: email, first_name,
    last_name, company, title, raw_data (everything else as a dict). Accepts
    common column name variants since Marblism's copy/paste-to-CSV
    workflow doesn't guarantee consistent headers between batches.

    2026-09-04: a row with no email is no longer dropped. This importer used
    to require one (every early Marblism batch had full email coverage), but
    Marblism's LinkedIn-scrape exports routinely leave 30-40% of rows with no
    email at all -- those are still real contacts (LinkedIn-invite outreach
    doesn't need one), just not reachable by the drip-email side of this
    tool. Dropping them silently lost real leads for no reason; email=None
    rows still get a leads row (dedup/suppression are both already
    None-safe -- see _ingest_lead_row), they just never get enrolled in an
    email campaign since enroll_batch/process_due_enrollments both require a
    real email to send to."""
    text = file_bytes.decode('utf-8-sig', errors='replace')
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return []

    def find_col(candidates):
        for f in reader.fieldnames:
            if f.strip().lower() in candidates:
                return f
        return None

    email_col = find_col({'email', 'e-mail', 'email address'})
    name_col = find_col({'name', 'full name', 'contact name'})
    first_col = find_col({'first name', 'firstname'})
    last_col = find_col({'last name', 'lastname'})
    company_col = find_col({'company', 'company name', 'organization'})
    title_col = find_col({'title', 'job title', 'job', 'position'})

    rows = []
    for row in reader:
        email = (row.get(email_col) or '').strip().lower() if email_col else ''
        if email and '@' not in email:
            continue  # garbage in the email cell, not just a blank one -- still skip
        if first_col or last_col:
            first_name = (row.get(first_col) or '').strip() if first_col else ''
            last_name = (row.get(last_col) or '').strip() if last_col else ''
        elif name_col:
            first_name, last_name = _split_name(row.get(name_col))
        else:
            first_name, last_name = '', ''
        mapped_cols = (email_col, name_col, first_col, last_col, company_col, title_col)
        rows.append({
            'email': email or None,
            'first_name': first_name[:255],
            'last_name': last_name[:255],
            'company': (row.get(company_col) or '').strip()[:255] if company_col else '',
            'title': (row.get(title_col) or '').strip()[:255] if title_col else '',
            'raw_data': {k: v for k, v in row.items() if k not in mapped_cols},
        })
    return rows


def _ingest_lead_row(cursor, email, first_name, last_name, company, path, source_ref_id, event_payload, title=None):
    """One lead, one write path -- shared by import_batch()'s CSV loop and add_single_lead()'s
    one-at-a-time add, so suppression screening and touch/event bookkeeping only exist once.
    Dedupes globally against leads.email; a genuinely new email becomes a new `leads` row +
    a lead_source_touches row + a 'sourced' lead_events row; an existing email still gets a
    NEW lead_source_touches row (decisions-v2 SS4.1: touches are append-only, a lead CAN be
    sourced more than once -- a re-upload or a second in-person meeting is a real second touch
    on an existing lead, not nothing) but no new leads/events row.

    email may be None (2026-09-04: no-email CSV rows, e.g. a Marblism LinkedIn export with no
    address). `leads.email` is nullable and `email=%s` against NULL never matches in SQL, so
    a None-email row always takes the "new lead" branch below -- correct: with no email there
    is nothing to dedupe against, and person-level dedup for these is the existing
    person_cluster_id/dup_status recompute's job, not this function's. Both suppression checks
    are no-ops for a None email for the same NULL-never-matches reason, which is also correct:
    there is no address to have bounced or unsubscribed.

    Screens every new lead against both suppression mirrors at write time (SES checked first,
    Sendy checked only if SES has no record) -- suppressed rows are still created, but
    suppressed_at/suppression_reason are set immediately (step-1-brief.md SS3).

    Returns {'lead_id', 'is_new', 'suppressed'}."""
    if email:
        cursor.execute("SELECT id FROM leads WHERE email=%s", (email,))
        existing = cursor.fetchone()
        if existing:
            cursor.execute(
                "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,%s,%s)",
                (existing['id'], path, source_ref_id)
            )
            return {'lead_id': existing['id'], 'is_new': False, 'suppressed': False}

    token = secrets.token_urlsafe(UNSUB_TOKEN_LEN)
    cursor.execute(
        """INSERT INTO leads (email, first_name, last_name, company, title, original_source, original_source_ref_id, unsubscribe_token)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (email, first_name, last_name, company, title or None, path, source_ref_id, token)
    )
    lead_id = cursor.lastrowid

    cursor.execute(
        "INSERT INTO lead_source_touches (lead_id, path, source_ref_id) VALUES (%s,%s,%s)",
        (lead_id, path, source_ref_id)
    )
    cursor.execute(
        "INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload) VALUES (%s,NOW(),'sourced','web',%s)",
        (lead_id, json.dumps(event_payload, default=str))
    )

    reason = None
    if email:
        cursor.execute("SELECT reason FROM ses_suppressions WHERE email_address=%s", (email,))
        sup = cursor.fetchone()
        reason = _SES_REASON_MAP.get(sup['reason'], 'bounced') if sup else None
        if not reason:
            cursor.execute("SELECT reason FROM sendy_suppressions WHERE email_address=%s", (email,))
            sendy_sup = cursor.fetchone()
            if sendy_sup:
                reason = _SENDY_REASON_MAP.get(sendy_sup['reason'], 'bounced')
    if reason:
        cursor.execute(
            "UPDATE leads SET suppressed_at=NOW(), suppression_reason=%s WHERE id=%s",
            (reason, lead_id)
        )
    return {'lead_id': lead_id, 'is_new': True, 'suppressed': bool(reason)}


def import_batch(cursor, conn, batch_name: str, source: str, uploaded_by: int, rows: list, path: str = 'direct') -> dict:
    """Insert a source_refs row (kind='import_batch', path=path -- 'direct' or 'in_person')
    for this upload, then run every CSV row through _ingest_lead_row().

    Also reports the suppression overlap count/percentage back as a before-you-send quality
    signal on the list.

    Returns {'batch_id' (the source_refs id), 'new_count', 'dup_count',
    'total', 'suppressed_count', 'suppressed_pct'}."""
    cursor.execute(
        "INSERT INTO source_refs (path, kind, label, source, uploaded_by, row_count) VALUES (%s,'import_batch',%s,%s,%s,%s)",
        (path, batch_name, source, uploaded_by, len(rows))
    )
    conn.commit()
    source_ref_id = cursor.lastrowid

    new_count = 0
    dup_count = 0
    suppressed_count = 0
    for r in rows:
        event_payload = {'batch_name': batch_name, 'source': source}
        if r['raw_data']:
            event_payload['raw_data'] = r['raw_data']
        result = _ingest_lead_row(cursor, r['email'], r['first_name'], r['last_name'], r['company'], path, source_ref_id, event_payload, title=r.get('title'))
        if result['is_new']:
            new_count += 1
            if result['suppressed']:
                suppressed_count += 1
        else:
            dup_count += 1
    conn.commit()

    suppressed_pct = round(100 * suppressed_count / new_count, 1) if new_count else 0.0
    cursor.execute(
        "UPDATE source_refs SET new_count=%s, dup_count=%s, suppressed_count=%s WHERE id=%s",
        (new_count, dup_count, suppressed_count, source_ref_id)
    )
    conn.commit()

    return {
        'batch_id': source_ref_id, 'new_count': new_count, 'dup_count': dup_count, 'total': len(rows),
        'suppressed_count': suppressed_count, 'suppressed_pct': suppressed_pct,
    }


def add_single_lead(cursor, conn, email: str, first_name: str, last_name: str, company: str,
                     path: str, event_name: str, added_by: int) -> dict:
    """One-at-a-time manual add (In-Person or Direct) -- the counterpart to import_batch()
    for a single person instead of a CSV. If event_name is given, finds-or-creates a
    source_refs row (kind='show', same path) labeled with that event so multiple people
    added from the same show/meeting group together, exactly like a CSV batch groups its
    rows -- a manual add with an event name is a batch of one (or a join into an existing
    one), not a orphaned row with no attribution home. No event_name -> original_source_ref_id
    stays NULL, same as any other unattributed lead.

    Returns {'lead_id', 'is_new', 'suppressed', 'batch_id'}."""
    source_ref_id = None
    if event_name:
        cursor.execute("SELECT id FROM source_refs WHERE kind='show' AND path=%s AND label=%s", (path, event_name))
        existing_batch = cursor.fetchone()
        if existing_batch:
            source_ref_id = existing_batch['id']
        else:
            cursor.execute(
                "INSERT INTO source_refs (path, kind, label, uploaded_by, row_count, new_count, dup_count, suppressed_count) "
                "VALUES (%s,'show',%s,%s,0,0,0,0)",
                (path, event_name, added_by)
            )
            source_ref_id = cursor.lastrowid
        conn.commit()

    result = _ingest_lead_row(cursor, email, first_name, last_name, company, path, source_ref_id, {'method': 'manual_add'})

    if source_ref_id:
        if result['is_new']:
            cursor.execute(
                "UPDATE source_refs SET row_count=row_count+1, new_count=new_count+1, suppressed_count=suppressed_count+%s WHERE id=%s",
                (1 if result['suppressed'] else 0, source_ref_id)
            )
        else:
            cursor.execute(
                "UPDATE source_refs SET row_count=row_count+1, dup_count=dup_count+1 WHERE id=%s",
                (source_ref_id,)
            )
    conn.commit()

    return {**result, 'batch_id': source_ref_id}


def enroll_batch(cursor, conn, campaign_id: int, batch_id: int) -> dict:
    """Enroll every lead touched by this source_refs row (batch_id) that
    isn't currently suppressed into a campaign. Idempotent via the
    UNIQUE(lead_id, campaign_id) constraint -- and per step-1-brief.md
    SS10.3, a (lead, campaign) pair whose existing row is 'suppressed' gets
    explicitly revived (reset to active, step 0, fresh due date) rather than
    silently colliding and staying stuck forever; active/completed/
    unsubscribed rows are left alone.

    Per step1brief (3).md SS11.1 (step 3c): every batch-scoped candidate
    also has to clear lead_pool.is_eligible() -- the wp_nurture_owned
    interlock and cluster-uniqueness gate -- before it's enrolled. Today
    that gate never actually fires here (batch candidates come from
    lead_source_touches, and free-check leads have none), but it makes the
    protection deliberate instead of an accident of which table happened to
    hold which leads.

    2026-09-04: candidates with no email are now excluded outright (see
    skipped_no_email below). Before the Marblism August import this never
    mattered -- every lead in the table had an email. It matters now: a
    no-email lead that got enrolled would fail every send attempt forever,
    because process_due_enrollments() only advances next_send_due_at/
    current_step on a SUCCESSFUL send -- a permanently-failing lead's due
    date never moves, so it would generate a fresh 'failed' drip_send_log
    row on every single cron run indefinitely, not just once.

    2026-09-04 (CITEMETRIX-Drip-Campaigns.md branching edge case): a
    candidate who has ALREADY completed a /check/ scan before this
    enrollment even happens (e.g. imported after checking on their own, via
    a different channel) skips the cold track entirely and is enrolled
    straight into the matching warm track instead -- see
    process_check_completions()'s docstring for the ongoing (post-
    enrollment) version of this same rule. Only applies when campaign_id's
    own name maps to a warm track that actually exists yet (see
    _matching_warm_campaign_id()); if not, falls through to the normal cold
    enrollment unchanged, so this is a no-op until the warm campaigns exist.

    Returns {'enrolled', 'skipped_suppressed', 'skipped_nurture_owned',
    'skipped_cluster_conflict', 'skipped_no_email', 'already_checked_to_warm'}."""
    cursor.execute(
        "SELECT day_offset FROM drip_steps WHERE campaign_id=%s ORDER BY step_order ASC LIMIT 1",
        (campaign_id,)
    )
    first_step = cursor.fetchone()
    if not first_step:
        return {'enrolled': 0, 'skipped_suppressed': 0, 'skipped_nurture_owned': 0, 'skipped_cluster_conflict': 0, 'skipped_no_email': 0, 'already_checked_to_warm': 0}

    cursor.execute("SELECT name FROM drip_campaigns WHERE id=%s", (campaign_id,))
    campaign_row = cursor.fetchone()
    warm_campaign_id_for_this_cold = _matching_warm_campaign_id(cursor, campaign_row['name'] if campaign_row else '')

    cursor.execute(
        """SELECT DISTINCT l.id FROM leads l
           JOIN lead_source_touches t ON t.lead_id = l.id AND t.source_ref_id=%s
           LEFT JOIN ses_suppressions s ON s.email_address = l.email
           LEFT JOIN sendy_suppressions sy ON sy.email_address = l.email
           WHERE l.email IS NOT NULL AND l.suppressed_at IS NULL AND s.email_address IS NULL AND sy.email_address IS NULL""",
        (batch_id,)
    )
    candidate_ids = [row['id'] for row in cursor.fetchall()]

    cursor.execute(
        "SELECT COUNT(DISTINCT l.id) AS n FROM leads l JOIN lead_source_touches t ON t.lead_id=l.id AND t.source_ref_id=%s WHERE l.email IS NULL",
        (batch_id,)
    )
    skipped_no_email = cursor.fetchone()['n']

    cursor.execute(
        """SELECT COUNT(DISTINCT l.id) AS n FROM leads l
           JOIN lead_source_touches t ON t.lead_id = l.id AND t.source_ref_id=%s
           LEFT JOIN ses_suppressions s ON s.email_address = l.email
           LEFT JOIN sendy_suppressions sy ON sy.email_address = l.email
           WHERE l.suppressed_at IS NOT NULL OR s.email_address IS NOT NULL OR sy.email_address IS NOT NULL""",
        (batch_id,)
    )
    skipped_suppressed = cursor.fetchone()['n']

    lead_ids = []
    skipped_nurture_owned = 0
    skipped_cluster_conflict = 0
    for lead_id in candidate_ids:
        eligible, reason = lead_pool.is_eligible(cursor, lead_id)
        if eligible:
            lead_ids.append(lead_id)
        elif 'wp_nurture_owned' in reason:
            skipped_nurture_owned += 1
        else:
            # already-suppressed leads were excluded from candidate_ids above,
            # so a non-nurture ineligibility here is a cluster conflict
            skipped_cluster_conflict += 1

    enrolled = 0
    already_checked_to_warm = 0
    now = datetime.now()
    due_at = now + timedelta(days=first_step['day_offset'])
    for lead_id in lead_ids:
        target_campaign_id = campaign_id
        target_due_at = due_at
        if warm_campaign_id_for_this_cold:
            cursor.execute(
                "SELECT id FROM lead_events WHERE lead_id=%s AND type='free_check_completed' LIMIT 1",
                (lead_id,)
            )
            if cursor.fetchone():
                # already checked via some other channel before this cold enrollment even
                # happens (e.g. synced in by migrate_step1b.py's OTHER free-check population) --
                # branching point 3: skip cold, straight to warm Step 1, right now
                target_campaign_id = warm_campaign_id_for_this_cold
                target_due_at = now
                already_checked_to_warm += 1

        cursor.execute(
            "SELECT status FROM drip_enrollments WHERE lead_id=%s AND campaign_id=%s",
            (lead_id, target_campaign_id)
        )
        existing = cursor.fetchone()
        if existing is None:
            cursor.execute(
                """INSERT INTO drip_enrollments (lead_id, campaign_id, enrolled_at, current_step, next_send_due_at, status)
                   VALUES (%s,%s,%s,0,%s,'active')""",
                (lead_id, target_campaign_id, now.strftime('%Y-%m-%d %H:%M:%S'), target_due_at.strftime('%Y-%m-%d %H:%M:%S'))
            )
            enrolled += 1
        elif existing['status'] == 'suppressed':
            cursor.execute(
                """UPDATE drip_enrollments SET status='active', current_step=0,
                       enrolled_at=%s, next_send_due_at=%s, last_sent_at=NULL
                   WHERE lead_id=%s AND campaign_id=%s""",
                (now.strftime('%Y-%m-%d %H:%M:%S'), target_due_at.strftime('%Y-%m-%d %H:%M:%S'), lead_id, target_campaign_id)
            )
            enrolled += 1
        # else: active/completed/unsubscribed -- leave alone, not re-counted
    conn.commit()
    return {'enrolled': enrolled, 'skipped_suppressed': skipped_suppressed,
             'skipped_nurture_owned': skipped_nurture_owned, 'skipped_cluster_conflict': skipped_cluster_conflict,
             'skipped_no_email': skipped_no_email, 'already_checked_to_warm': already_checked_to_warm}


WARM_SUFFIX = ' — Warm (Checked)'


def _matching_warm_campaign_id(cursor, cold_campaign_name: str):
    """CITEMETRIX-Drip-Campaigns.md's branching summary, point 2: 'enroll in matching warm
    track (by audience)'. Naming convention (this codebase's own choice, not from the spec
    doc -- the spec left the mechanism open): a warm campaign's name is always the matching
    cold campaign's own name + WARM_SUFFIX, e.g. the cold 'Brand & SMB Marketing Leaders'
    (id 15 on this box) pairs with a warm 'Brand & SMB Marketing Leaders — Warm (Checked)'.
    Matched by name rather than a new FK column so the 3 warm campaigns (task: create 3 warm
    campaigns + 9 steps) are just ordinary drip_campaigns rows -- no schema migration needed
    first, and Eric can rename either side later as long as the pairing stays intact.

    Returns the warm campaign's id, or None if no such campaign exists yet (the warm
    campaigns haven't been created, cold_campaign_name is blank, or this IS already a warm/
    unrelated campaign, e.g. 'Free-Check Nurture — Generic'). Callers must treat None as
    'stay on cold, branching not available for this lead yet' -- never as an error."""
    if not cold_campaign_name:
        return None
    cursor.execute("SELECT id FROM drip_campaigns WHERE name=%s", (cold_campaign_name + WARM_SUFFIX,))
    row = cursor.fetchone()
    return row['id'] if row else None


def enroll_single_lead(cursor, conn, campaign_id: int, lead_id: int, due_at=None) -> dict:
    """Enroll (or revive) exactly one lead into one campaign -- the single-lead counterpart
    to enroll_batch()'s batch loop, extracted so process_check_completions() (branch a lead
    into its warm track the moment its cold enrollment would otherwise have sent) and the
    already-checked edge case in enroll_batch() can both drive one lead into one campaign
    without going through the batch/source_refs machinery neither of them has.

    due_at defaults to right now: CITEMETRIX-Drip-Campaigns.md's branching point 2 says
    'start warm Step 1' at the moment of branching, not day_offset days from branching --
    and since the warm campaign's own Step 1 is day_offset=0 already, 'now' and 'now + 0
    days' land on the same instant regardless, so there's no separate case to handle.

    Same suppressed-row revival rule as enroll_batch(): an existing 'suppressed' row is
    reset to active/step 0; active/completed/unsubscribed/cancelled rows are left alone
    (already on this campaign, nothing to do). Returns {'enrolled': bool, 'reason': str|None}
    -- reason is set (and enrolled=False) only when a live row already existed and blocked a
    fresh enrollment, so a caller doing a bulk branching pass can tell a genuine new
    enrollment apart from a no-op on an already-branched lead."""
    now = datetime.now()
    if due_at is None:
        due_at = now
    cursor.execute(
        "SELECT id, status FROM drip_enrollments WHERE lead_id=%s AND campaign_id=%s",
        (lead_id, campaign_id)
    )
    existing = cursor.fetchone()
    if existing is None:
        cursor.execute(
            """INSERT INTO drip_enrollments (lead_id, campaign_id, enrolled_at, current_step, next_send_due_at, status)
               VALUES (%s,%s,%s,0,%s,'active')""",
            (lead_id, campaign_id, now.strftime('%Y-%m-%d %H:%M:%S'), due_at.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        return {'enrolled': True, 'reason': None}
    if existing['status'] == 'suppressed':
        cursor.execute(
            """UPDATE drip_enrollments SET status='active', current_step=0,
                   enrolled_at=%s, next_send_due_at=%s, last_sent_at=NULL
               WHERE id=%s""",
            (now.strftime('%Y-%m-%d %H:%M:%S'), due_at.strftime('%Y-%m-%d %H:%M:%S'), existing['id'])
        )
        conn.commit()
        return {'enrolled': True, 'reason': None}
    return {'enrolled': False, 'reason': f"already {existing['status']} on this campaign"}


def process_check_completions(admin_cursor, admin_conn, product_cursor) -> dict:
    """CITEMETRIX-Drip-Campaigns.md's branching summary, points 1, 2, and 5: a lead who
    completes a /check/ scan while actively on a COLD track gets pulled off cold and dropped
    into the matching warm track, instead of finishing out a cold sequence that no longer
    fits. Call this once per cron cycle, BEFORE process_due_enrollments() sends anything, so
    a step that would otherwise have gone out on this very run goes out as the warm
    campaign's Step 1 instead -- never one more cold email first.

    'Has completed a check' (point 1) is detected via citemetrix_freecheck_log on the
    PRODUCT box (product_cursor): every /check/ scan run -- domain-only or with an email
    left -- logs a row there via log_scan_run() (see class-citemetrix-free-score.php), and
    that call now also stamps drip_lead_token whenever the click that led here carried
    &cm_lead=<token> (see build_cta()'s docstring for how the token gets there). So a
    matching row = this lead ran a check, full stop -- independent of whether they ever
    left an email, which is exactly what 'completed a check' means here (NOT the same
    signal migrate_step1b.py's free_check_completed lead_events use for its own, differently
    -sourced population -- that one requires an email capture into wp_citemetrix_score_leads).
    A lead who somehow already has a free_check_completed lead_events row (e.g. reached
    admin_portal via the OTHER free-check path) counts too, checked first since it's a plain
    local lookup and skips a product-DB round trip for those leads.

    Point 5 ('never on both tracks simultaneously; entering warm cancels ALL pending cold
    steps') is enforced by cancelling the ENTIRE remaining cold enrollment
    (status='cancelled', not just skipping the next step) before enrolling in warm. A
    cancelled row's next_send_due_at is left as-is but process_due_enrollments() only ever
    selects status='active' rows, so a cancelled enrollment can never resume sending
    regardless of what its due date says.

    Point 3 (already-checked leads entering the DB) is NOT this function's job -- there's no
    active cold enrollment yet to cancel by definition; that's enroll_batch()'s own edge
    case (see its docstring).

    Idempotent by construction: once a cold enrollment is cancelled here it's no longer
    status='active', so it's excluded from the very query this function starts with on its
    next run -- no separate "already branched" check is needed to avoid double-processing.

    Writes a 'free_check_completed' lead_events row (mirroring migrate_step1b.py's own
    payload shape, so render_merge_tags()/_platform_stats() pick it up for warm Step 1
    personalization exactly like they already do for the other population) whenever one
    doesn't already exist for this lead -- brand_name/model_score/category come straight off
    the freecheck_log row (model_score = category_visibility, matching
    class-citemetrix-free-score.php's own `$model_score = $category_visibility` -- that log
    table has no platform_results column, so that key is left None; render_merge_tags'
    existing fallbacks ('several'/'some'/'key AI platforms') cover it same as always) plus a
    'branched_to_warm' lead_events row recording which campaign moved to which, for the
    audit trail.

    Returns {'cold_active_checked': int, 'branched': int}."""
    admin_cursor.execute(
        """SELECT e.id AS enrollment_id, e.lead_id, e.campaign_id, l.unsubscribe_token,
                  c.name AS campaign_name
           FROM drip_enrollments e
           JOIN leads l ON l.id = e.lead_id
           JOIN drip_campaigns c ON c.id = e.campaign_id
           WHERE e.status='active'"""
    )
    active_cold = admin_cursor.fetchall()

    cold_active_checked = 0
    branched = 0
    now = datetime.now()
    for row in active_cold:
        warm_campaign_id = _matching_warm_campaign_id(admin_cursor, row['campaign_name'])
        if not warm_campaign_id:
            continue  # not a cold campaign with a warm counterpart (e.g. the free-check nurture campaign)
        cold_active_checked += 1

        admin_cursor.execute(
            "SELECT id FROM lead_events WHERE lead_id=%s AND type='free_check_completed' LIMIT 1",
            (row['lead_id'],)
        )
        fc_event = admin_cursor.fetchone()

        fc_row = None
        if not fc_event and row['unsubscribe_token']:
            product_cursor.execute(
                """SELECT domain, brand_name, category, brand_recognition, category_visibility,
                          utm_source, utm_medium, utm_campaign, source_page, created_at
                   FROM wp_citemetrix_freecheck_log
                   WHERE drip_lead_token=%s ORDER BY created_at DESC LIMIT 1""",
                (row['unsubscribe_token'],)
            )
            fc_row = product_cursor.fetchone()

        if not fc_event and not fc_row:
            continue  # hasn't checked yet -- stays on cold

        if not fc_event and fc_row:
            payload = json.dumps({
                'domain': fc_row['domain'], 'brand_name': fc_row['brand_name'], 'category': fc_row['category'],
                'brand_recognition': fc_row['brand_recognition'], 'model_score': fc_row['category_visibility'],
                'utm_source': fc_row['utm_source'], 'utm_medium': fc_row['utm_medium'], 'utm_campaign': fc_row['utm_campaign'],
                'source_page': fc_row['source_page'], 'platform_results': None,
            }, default=str)
            admin_cursor.execute(
                """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                   VALUES (%s,%s,'free_check_completed','web',%s)""",
                (row['lead_id'], fc_row['created_at'], payload)
            )
            admin_conn.commit()

        admin_cursor.execute("UPDATE drip_enrollments SET status='cancelled' WHERE id=%s", (row['enrollment_id'],))
        admin_conn.commit()
        result = enroll_single_lead(admin_cursor, admin_conn, warm_campaign_id, row['lead_id'], due_at=now)
        if result['enrolled']:
            # lead_events.type is a fixed ENUM with no 'branched_to_warm' member (adding one
            # would mean an ALTER TABLE for a single audit-trail row) -- 'note' already exists
            # for exactly this kind of generic system annotation, so the specific event is
            # named in the payload instead (event='branched_to_warm'), queryable via
            # JSON_EXTRACT(payload,'$.event') same as any other 'note' row would be. channel
            # is left NULL (nullable, and its own fixed ENUM has no 'system' member either) --
            # this isn't a lead-facing touch on any channel, it's internal bookkeeping.
            admin_cursor.execute(
                """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                   VALUES (%s,NOW(),'note',NULL,%s)""",
                (row['lead_id'], json.dumps({'event': 'branched_to_warm', 'from_campaign_id': row['campaign_id'], 'to_campaign_id': warm_campaign_id}))
            )
            admin_conn.commit()
            branched += 1

    return {'cold_active_checked': cold_active_checked, 'branched': branched}


def render_merge_tags(text: str, lead: dict) -> str:
    if not text:
        return text
    text = text.replace('{{first_name}}', lead.get('first_name') or 'there')
    text = text.replace('{{last_name}}', lead.get('last_name') or '')
    text = text.replace('{{company}}', lead.get('company') or 'your company')
    text = text.replace('{{email}}', lead.get('email') or '')
    # step 3d (step1brief.md SS13.1): the free-check nurture sequence's content
    # references the scan itself, not just lead identity. Sourced from the
    # lead's 'free_check_completed' lead_events payload (see
    # process_due_enrollments), not a leads column -- no scan, no value, hence
    # the fallbacks (only reachable in practice by a non-nurture campaign,
    # since nurture is the only campaign type that ever populates these).
    text = text.replace('{{brand_name}}', lead.get('brand_name') or 'your brand')
    model_score = lead.get('model_score')
    text = text.replace('{{model_score}}', str(model_score) if model_score is not None else 'N/A')
    # step1brief.md SS14.1: restored, but not as originally written. The WP
    # plugin's own "gap" line reads platform_results['brand_recognition']/
    # ['category_visibility'] -- keys that don't exist in that object (they
    # were never columns on wp_citemetrix_score_leads to begin with), so that
    # branch has never fired against real data; every real send has used the
    # model_score fallback, which is what step 3d already had. What IS real
    # and does vary per lead is platform_results itself -- computed below.
    checked = lead.get('platforms_checked_count')
    mentioned = lead.get('platforms_mentioned_count')
    text = text.replace('{{platforms_checked_count}}', str(checked) if checked is not None else 'several')
    text = text.replace('{{platforms_mentioned_count}}', str(mentioned) if mentioned is not None else 'some')
    text = text.replace('{{missing_platforms}}', lead.get('missing_platforms') or 'key AI platforms')
    return text


def _platform_stats(platform_results):
    """(checked_count, mentioned_count, missing_platforms_str) from a decoded
    platform_results dict (keyed by platform slug -> {platform, status,
    mentioned, ...}). Mirrors class-citemetrix-free-score.php's
    build_nurture_2_html() missing-platforms logic exactly -- this piece of
    the PHP was correct against real data, unlike brand_recognition above."""
    if not isinstance(platform_results, dict):
        return None, None, None
    checked = [r for r in platform_results.values() if isinstance(r, dict) and r.get('status') == 'success']
    if not checked:
        return None, None, None
    missing = [r['platform'] for r in checked if not r.get('mentioned') and r.get('platform')]
    mentioned_count = len(checked) - len(missing)
    missing_str = ' and '.join(missing) if missing else None
    return len(checked), mentioned_count, missing_str


CITEMETRIX_SITE_URL = 'https://citemetrix.com'

# CITEMETRIX-Drip-Campaigns.md's warm tracks CTA to /book-demo/, not /check/ -- they're for
# leads who've already checked, so a /check/ message-match key doesn't apply. This reserved
# cta_key value (checked case-insensitively) is how a drip step opts into that destination
# instead of the normal /check/ one; every other cta_key value behaves exactly as before.
BOOK_DEMO_CTA_KEY = 'book-demo'


def build_cta(step: dict, lead_token: str = None) -> tuple:
    """A drip step's CTA points at /check/ tagged with a utm_campaign key that must match
    one defined in the WP plugin's ANGLE_COPY (citemetrix.com's message-match system for
    /check/ -- see the check-page-message-match SPEC) so the visitor lands on copy that
    continues the email's own pitch instead of the generic default. cta_key is deliberately
    a free-typed field, not a dropdown pinned to today's known keys -- new keys get added
    per-campaign as Eric/Chat write new copy. 2026-09-04: push_message_variant() below lets
    a NEW key's on-page headline/copy/button be defined right from the drip step form and
    pushed live to WP, instead of needing a plugin code deploy every time -- see its
    docstring. Returns (cta_url, cta_text), or (None, None) if the step has no CTA
    configured (either field blank) -- callers must leave {{cta_url}}/{{cta_button}}
    untouched in that case rather than silently blanking them, so a forgotten CTA is
    obvious in the rendered email, not invisible.

    2026-09-04 (cold->warm branching): lead_token, when given, rides along as &cm_lead=
    on the URL so a completed scan on the WP box can be traced back to the specific lead
    who clicked -- most Marblism leads have no email or domain to match a completed check
    against, so the click itself is the only reliable link. Reuses leads.unsubscribe_token
    (already a stable, per-lead secret that already travels in every send's footer) rather
    than minting a second token -- see process_check_completions()'s docstring for the full
    detection flow. Deliberately optional: preview/test-send calls (sample data, not a real
    lead) omit it, so those URLs stay clean and never resolve to a real lead's token.
    """
    key = (step.get('cta_key') or '').strip()
    text = (step.get('cta_text') or '').strip()
    if not key or not text:
        return None, None
    step_order = step.get('step_order') or 0
    if key.lower() == BOOK_DEMO_CTA_KEY:
        # warm-track CTA -- no /check/ message-match key applies, and the token has already
        # done its job (it got this lead into warm in the first place), so it isn't carried
        # any further onto a page with nothing to consume it.
        return f"{CITEMETRIX_SITE_URL}/book-demo/?utm_source=email&utm_medium=drip&utm_campaign=warm&utm_content=step{step_order}", text
    url = f"{CITEMETRIX_SITE_URL}/check/?utm_source=email&utm_medium=drip&utm_campaign={key}&utm_content=step{step_order}"
    if lead_token:
        url += f"&cm_lead={lead_token}"
    return url, text


def push_message_variant(cta_key: str, onpage_title: str, onpage_copy: str, cta_text: str) -> dict:
    """Push a drip step's on-page headline/copy/button to citemetrix.com's /check/
    message-match system (see class-citemetrix-free-score.php's ajax_upsert_message_variant
    + get_dynamic_message_variant) so a visitor who clicks this step's CTA sees copy that
    continues it, without needing a WP plugin code change + deploy for every new drip
    campaign. Server-to-server call, secured by a shared secret (same value must be set as
    CITEMETRIX_MESSAGE_VARIANT_SECRET in wp-config.php on the WP box and here in this box's
    .env -- set independently on each side, never copied between them by this codebase).

    Reuses the CTA's own button text as the on-page button too, on purpose -- Eric's ask was
    four fields (CTA button text, the utm key, on-page title, on-page copy), not five; one
    button label carries through from the email to the landing page.

    Returns {'ok': bool, 'error': str|None}. Never raises -- a WP-side outage or a rejected
    key (e.g. it collides with a reserved ad-angle/internal-link key) must not block saving
    the step itself; the caller surfaces this result to the UI instead.
    """
    secret = os.getenv('CITEMETRIX_MESSAGE_VARIANT_SECRET')
    if not secret:
        return {'ok': False, 'error': 'CITEMETRIX_MESSAGE_VARIANT_SECRET is not set in this box\'s .env yet -- on-page copy was saved here but not pushed to /check/.'}
    try:
        resp = requests.post(
            f'{CITEMETRIX_SITE_URL}/wp-admin/admin-ajax.php',
            headers={'X-Cm-Secret': secret},
            data={
                'action': 'citemetrix_upsert_message_variant',
                'key': cta_key, 'headline': onpage_title, 'copy': onpage_copy,
                'button': cta_text, 'source': 'drip_admin_portal',
            },
            timeout=10,
        )
        j = resp.json()
        if resp.status_code == 200 and j.get('success'):
            return {'ok': True, 'error': None}
        return {'ok': False, 'error': (j.get('data') or {}).get('message') or f'HTTP {resp.status_code}'}
    except requests.RequestException as e:
        return {'ok': False, 'error': f'Could not reach citemetrix.com: {e}'}
    except ValueError:
        return {'ok': False, 'error': f'Unexpected response from citemetrix.com (HTTP {resp.status_code}, not JSON).'}


def _cta_button_html(cta_url, cta_text):
    """Bulletproof CTA button -- VML fallback for Outlook desktop. Outlook's Word-based
    rendering engine ignores padding/border-radius on a plain <a> (confirmed elsewhere in
    this codebase: class-citemetrix-free-score.php's own nurture-email CTA button hit this
    exact issue), so a naive styled anchor either loses its shape entirely or fails to render
    as clickable in Outlook desktop. Mirrors that exact same pattern (table + mso conditional
    comments) rather than inventing a second one -- an <!--[if mso]--> block renders a VML
    v:roundrect for Outlook, and everything else gets the plain styled <a> via <!--[if !mso]-->.

    2026-09-04: brand colors (CiteMetrix teal/navy, matching --cm-teal #00D4AA / --cm-navy
    #0A1628 used site-wide -- e.g. templates/page-citemetrix.php) instead of a generic blue.
    Rounding widened slightly (arcsize 18% -> 22%, border-radius 8px -> 10px) for a more
    visibly rounded shape -- some email clients strip or under-render small border-radius
    values on inline elements; this is a genuine, unresolved limitation of a handful of
    clients (notably ones that sanitize incoming HTML), not something CSS/VML alone can force
    everywhere. Confirmed rendering correctly (real rounded corners, real brand colors) in a
    real browser render of this exact generated HTML before shipping."""
    font = 'Arial,Helvetica,sans-serif'
    teal = '#00D4AA'
    navy = '#0A1628'
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" align="center" style="margin:8px auto;"><tr><td align="center" bgcolor="{teal}" style="border-radius:10px;">'
        f'<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{cta_url}" style="height:44px;v-text-anchor:middle;width:240px;" arcsize="22%" strokecolor="{teal}" fillcolor="{teal}"><w:anchorlock/><center style="color:{navy};font-family:{font};font-size:15px;font-weight:bold;">{cta_text}</center></v:roundrect><![endif]-->'
        f'<!--[if !mso]><!--><a href="{cta_url}" style="display:inline-block;padding:12px 26px;font-family:{font};font-size:15px;font-weight:700;color:{navy};text-decoration:none;border-radius:10px;background:{teal};">{cta_text}</a><!--<![endif]-->'
        '</td></tr></table>'
    )


def apply_cta_tags(body: str, body_html, step: dict, lead_token: str = None):
    """Substitute {{cta_url}}/{{cta_button}} in a rendered body (and body_html) using the
    step's own cta_key/cta_text -- separate from render_merge_tags() because a CTA is a
    property of the STEP, not of the lead being emailed, so it doesn't need (and shouldn't
    require plumbing) a lead dict through to build it. Returns (body, body_html) unchanged
    if no CTA is configured on this step -- see build_cta()'s docstring for why that's a
    deliberate no-op instead of blanking the placeholder.

    2026-09-04: when body_html is None (the normal case -- no drip step form field has ever
    written to drip_steps.body_html), an HTML version is now AUTO-DERIVED from the plain-text
    body instead of staying None, so {{cta_button}} renders as a real clickable button in
    HTML-capable email clients. Caught live: a real test send showed the CTA as a plain
    "text: url" line, not a button -- the button-HTML code path below already existed but was
    dead code, since nothing ever supplied a body_html to run it on. Eric/Chat still write
    only ONE body; there's no separate HTML-copy field to keep in sync, so the two versions
    can never drift apart -- the HTML is mechanically derived from the same text every time.
    An explicitly-authored body_html (if some future caller ever provides one) is still
    respected and just gets its own placeholders substituted, unchanged from before.

    lead_token: passed straight through to build_cta() for cold->warm branching click
    attribution (see that function's docstring) -- optional, omitted for preview/test-send."""
    cta_url, cta_text = build_cta(step, lead_token)
    if not cta_url:
        return body, body_html

    text_out = body
    if body:
        # {{cta_button}} in plain text is just the URL, not "cta_text: url" -- copy typically
        # already leads into it with its own phrase (e.g. "Check what AI says about us:
        # {{cta_button}}"), and prefixing the button's own label there duplicated it. The URL
        # alone reads fine in a plain-text email either way.
        text_out = body.replace('{{cta_url}}', cta_url).replace('{{cta_button}}', cta_url)

    if body_html:
        html_out = body_html.replace('{{cta_url}}', cta_url).replace('{{cta_button}}', _cta_button_html(cta_url, cta_text))
    elif body:
        import html as _html
        button_html = _cta_button_html(cta_url, _html.escape(cta_text))
        # {{cta_button}} expands to a block-level <table> (the bulletproof-button pattern),
        # so it can't just get substituted inline -- Eric/Chat write it wherever reads
        # naturally in their copy (e.g. "Check it out: {{cta_button}}"), which is normally
        # mid-sentence. A <table> nested inside a <p> is invalid HTML that different email
        # clients recover from inconsistently, so each paragraph block containing the token
        # gets split: any text before/after becomes its own <p>, the button becomes its own
        # standalone block in between -- never nested inside one.
        blocks_html = []
        for block in body.split('\n\n'):
            if not block.strip():
                continue
            if '{{cta_button}}' in block:
                before, _, after = block.partition('{{cta_button}}')
                before = _html.escape(before).replace('{{cta_url}}', cta_url).strip()
                after = _html.escape(after).replace('{{cta_url}}', cta_url).strip()
                if before:
                    blocks_html.append(f'<p style="margin:0 0 8px;">{before.replace(chr(10), "<br>")}</p>')
                blocks_html.append(button_html)
                if after:
                    blocks_html.append(f'<p style="margin:8px 0 16px;">{after.replace(chr(10), "<br>")}</p>')
            else:
                escaped_block = _html.escape(block).replace('{{cta_url}}', cta_url)
                blocks_html.append(f'<p style="margin:0 0 16px;">{escaped_block.replace(chr(10), "<br>")}</p>')
        html_out = (
            '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#1f2937;">'
            + ''.join(blocks_html) + '</div>'
        )
    else:
        html_out = body_html

    return text_out, html_out


def build_unsubscribe_footer(unsub_url: str) -> str:
    return f"\n\n—\nEric Richmond\nCiteMetrix LLC, PO Box 324, Norwalk, CT 06853-0324\nDon't want these emails? {unsub_url}"


def build_unsubscribe_footer_html(unsub_url: str) -> str:
    return (
        '<p style="margin:24px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;'
        'line-height:1.6;color:#8a94a3;border-top:1px solid #e5e7eb;padding-top:16px;">'
        'Eric Richmond<br>CiteMetrix LLC, PO Box 324, Norwalk, CT 06853-0324<br>'
        f'Don\'t want these emails? <a href="{unsub_url}" style="color:#8a94a3;">Unsubscribe</a></p>'
    )


def process_due_enrollments(admin_cursor, admin_conn, send_email_fn, base_url: str, max_sends: int = 50) -> dict:
    """The cron entry point. Finds enrollments due right now, sends the
    current step, advances to the next, or marks completed if there's no
    next step. max_sends caps a single run so a large backlog (e.g. after
    downtime) drips out over several cron cycles instead of firing a
    hundred emails in one burst -- same pacing principle as the outreach
    tool's daily send cap.

    Two distinct suppression paths, both against `leads` now: a real
    self-serve unsubscribe (leads.suppression_reason='unsubscribed', a
    genuine permanent opt-out) vs. everything else that blocks a send
    (leads.suppressed_at already set from import/migration, OR the live
    ses_suppressions/sendy_suppressions join -- an address can land on
    either mirror after enrollment and before its send date, so the live
    check is the authoritative one, not the stored flag). Both are marked
    and logged (step-1-brief.md SS9.1) rather than silently dropped.

    A third gate, added step 3c (step1brief (3).md SS11.1): lead_pool.
    is_eligible() re-checked at send time, not just at enrollment, per
    decisions-v2's live-not-stale principle -- catches a lead that became
    wp_nurture_owned or picked up a cluster conflict after it was already
    enrolled. exclude_enrollment_id=row['enrollment_id'] is required here:
    without it the lead's own active row would always self-block the check
    (is_eligible's job is normally "can a NEW enrollment start", and this
    row already is one)."""
    admin_cursor.execute(
        """SELECT e.id AS enrollment_id, e.lead_id, e.campaign_id, e.current_step,
                  l.email, l.first_name, l.last_name, l.company, l.unsubscribe_token,
                  l.suppressed_at, l.suppression_reason,
                  (s.email_address IS NOT NULL OR sy.email_address IS NOT NULL) AS is_live_suppressed,
                  (SELECT payload FROM lead_events
                     WHERE lead_id = l.id AND type = 'free_check_completed'
                     ORDER BY id DESC LIMIT 1) AS fc_payload
           FROM drip_enrollments e
           JOIN leads l ON l.id = e.lead_id
           JOIN drip_campaigns c ON c.id = e.campaign_id
           LEFT JOIN ses_suppressions s ON s.email_address = l.email
           LEFT JOIN sendy_suppressions sy ON sy.email_address = l.email
           WHERE e.status='active' AND e.next_send_due_at <= NOW() AND c.active=1
           ORDER BY e.next_send_due_at ASC LIMIT %s""",
        (max_sends,)
    )
    due = admin_cursor.fetchall()

    sent, failed, skipped_unsub, skipped_suppressed, skipped_blocked = 0, 0, 0, 0, 0
    for row in due:
        if row['suppression_reason'] == 'unsubscribed':
            admin_cursor.execute("UPDATE drip_enrollments SET status='unsubscribed' WHERE id=%s", (row['enrollment_id'],))
            admin_conn.commit()
            skipped_unsub += 1
            continue

        next_step_order = row['current_step'] + 1
        admin_cursor.execute(
            "SELECT * FROM drip_steps WHERE campaign_id=%s AND step_order=%s",
            (row['campaign_id'], next_step_order)
        )
        step = admin_cursor.fetchone()
        if not step:
            admin_cursor.execute("UPDATE drip_enrollments SET status='completed' WHERE id=%s", (row['enrollment_id'],))
            admin_conn.commit()
            continue

        if row['suppressed_at'] is not None or row['is_live_suppressed']:
            admin_cursor.execute("UPDATE drip_enrollments SET status='suppressed' WHERE id=%s", (row['enrollment_id'],))
            admin_cursor.execute(
                "INSERT INTO drip_send_log (enrollment_id, step_id, status, error) VALUES (%s,%s,'suppressed',%s)",
                (row['enrollment_id'], step['id'], f"{row['email']} is on the SES suppression list at send time")
            )
            admin_conn.commit()
            skipped_suppressed += 1
            continue

        elig, elig_reason = lead_pool.is_eligible(admin_cursor, row['lead_id'], exclude_enrollment_id=row['enrollment_id'])
        if not elig:
            admin_cursor.execute("UPDATE drip_enrollments SET status='blocked' WHERE id=%s", (row['enrollment_id'],))
            admin_cursor.execute(
                "INSERT INTO drip_send_log (enrollment_id, step_id, status, error) VALUES (%s,%s,'blocked',%s)",
                (row['enrollment_id'], step['id'], f"{row['email']} failed the pool eligibility gate at send time: {elig_reason}")
            )
            admin_conn.commit()
            skipped_blocked += 1
            continue

        lead = {'email': row['email'], 'first_name': row['first_name'], 'last_name': row['last_name'], 'company': row['company']}
        if row.get('fc_payload'):
            try:
                fc = json.loads(row['fc_payload'])
                lead['brand_name'] = fc.get('brand_name')
                lead['model_score'] = fc.get('model_score')
                platform_results = fc.get('platform_results')
                if isinstance(platform_results, str):
                    platform_results = json.loads(platform_results)  # double-encoded in the payload
                checked, mentioned, missing = _platform_stats(platform_results)
                lead['platforms_checked_count'] = checked
                lead['platforms_mentioned_count'] = mentioned
                lead['missing_platforms'] = missing
            except (ValueError, TypeError):
                pass  # malformed payload -- render_merge_tags' fallbacks cover it
        unsub_url = f"{base_url}/unsubscribe/{row['unsubscribe_token']}"

        if step['is_survey_step']:
            survey_url = f"https://citemetrix.com/survey/?epoch={step['survey_epoch'] or ''}&campaign={row['campaign_id']}&source=drip&email={row['email']}"
            subject = render_merge_tags(step['subject'], lead) or "Quick question - what stopped you?"
            body = render_merge_tags(step['body'], lead) or f"Hi {lead['first_name'] or 'there'},\n\n{survey_url}"
            body += f"\n\n{survey_url}"
            body_html = None
        else:
            subject = render_merge_tags(step['subject'], lead)
            body = render_merge_tags(step['body'], lead)
            body_html = render_merge_tags(step.get('body_html'), lead)
            body, body_html = apply_cta_tags(body, body_html, step, lead_token=row['unsubscribe_token'])

        body += build_unsubscribe_footer(unsub_url)
        if body_html:
            body_html += build_unsubscribe_footer_html(unsub_url)

        send_kwargs = {'to': lead['email'], 'subject': subject, 'body_text': body}
        if body_html:
            send_kwargs['body_html'] = body_html
        ok, result = send_email_fn(**send_kwargs)

        if ok:
            next_order2 = next_step_order + 1
            admin_cursor.execute(
                "SELECT day_offset FROM drip_steps WHERE campaign_id=%s AND step_order=%s",
                (row['campaign_id'], next_order2)
            )
            following = admin_cursor.fetchone()
            if following:
                admin_cursor.execute(
                    "SELECT enrolled_at FROM drip_enrollments WHERE id=%s", (row['enrollment_id'],)
                )
                enrolled_at = admin_cursor.fetchone()['enrolled_at']
                new_due = enrolled_at + timedelta(days=following['day_offset'])
                admin_cursor.execute(
                    "UPDATE drip_enrollments SET current_step=%s, next_send_due_at=%s, last_sent_at=NOW() WHERE id=%s",
                    (next_step_order, new_due.strftime('%Y-%m-%d %H:%M:%S'), row['enrollment_id'])
                )
            else:
                admin_cursor.execute(
                    "UPDATE drip_enrollments SET current_step=%s, status='completed', last_sent_at=NOW() WHERE id=%s",
                    (next_step_order, row['enrollment_id'])
                )
            admin_conn.commit()
            admin_cursor.execute(
                "INSERT INTO drip_send_log (enrollment_id, step_id, provider_msg_id, status) VALUES (%s,%s,%s,'sent')",
                (row['enrollment_id'], step['id'], result)
            )
            admin_conn.commit()
            sent += 1
        else:
            admin_cursor.execute(
                "INSERT INTO drip_send_log (enrollment_id, step_id, status, error) VALUES (%s,%s,'failed',%s)",
                (row['enrollment_id'], step['id'], str(result)[:500])
            )
            admin_conn.commit()
            failed += 1

    return {'checked': len(due), 'sent': sent, 'failed': failed, 'skipped_unsubscribed': skipped_unsub,
            'skipped_suppressed': skipped_suppressed, 'skipped_blocked': skipped_blocked}
