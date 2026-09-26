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
import segments
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
    # 2026-09-25: was decode('utf-8-sig', errors='replace') -- silently corrupted
    # any non-UTF-8 byte into U+FFFD, permanently mangling accented names/companies
    # on every import (found real corrupted records from this). Marblism's exports
    # are sometimes Windows-saved (cp1252), so try that as a real fallback before
    # giving up -- errors='replace' only as a last resort, never silently for the
    # common case.
    try:
        text = file_bytes.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            text = file_bytes.decode('cp1252')
        except UnicodeDecodeError:
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
    linkedin_col = find_col({'linkedin url', 'linkedin', 'linkedin profile', 'linkedin link'})

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
        mapped_cols = (email_col, name_col, first_col, last_col, company_col, title_col, linkedin_col)
        rows.append({
            'email': email or None,
            'first_name': first_name[:255],
            'last_name': last_name[:255],
            'company': (row.get(company_col) or '').strip()[:255] if company_col else '',
            'title': (row.get(title_col) or '').strip()[:255] if title_col else '',
            'linkedin_url': (row.get(linkedin_col) or '').strip()[:500] if linkedin_col else '',
            'raw_data': {k: v for k, v in row.items() if k not in mapped_cols},
        })
    return rows


def _ingest_lead_row(cursor, email, first_name, last_name, company, path, source_ref_id, event_payload, title=None, linkedin_url=None):
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
        """INSERT INTO leads (email, first_name, last_name, company, title, linkedin_url, original_source, original_source_ref_id, unsubscribe_token)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (email, first_name, last_name, company, title or None, linkedin_url or None, path, source_ref_id, token)
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


def tag_lead(cursor, lead_id: int, tag_names) -> None:
    """Stamp one or more tags onto a lead by name, creating each tag if it doesn't already
    exist. Idempotent via lead_tags' own UNIQUE(tag_id, lead_id). Shared by every ingestion
    path (CSV upload, manual add, the WordPress sync scripts) so tag get-or-create logic
    lives in exactly one place rather than being copy-pasted per caller."""
    if isinstance(tag_names, str):
        tag_names = [tag_names]
    for name in tag_names:
        if not name:
            continue
        cursor.execute("SELECT id FROM tags WHERE name=%s", (name,))
        row = cursor.fetchone()
        if row:
            tag_id = row['id']
        else:
            cursor.execute("INSERT INTO tags (name) VALUES (%s)", (name,))
            tag_id = cursor.lastrowid
        cursor.execute("INSERT IGNORE INTO lead_tags (tag_id, lead_id) VALUES (%s,%s)", (tag_id, lead_id))


def import_batch(cursor, conn, batch_name: str, source: str, uploaded_by: int, rows: list, path: str = 'direct', tag_names=None) -> dict:
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
        result = _ingest_lead_row(cursor, r['email'], r['first_name'], r['last_name'], r['company'], path, source_ref_id, event_payload, title=r.get('title'), linkedin_url=r.get('linkedin_url'))
        if tag_names:
            tag_lead(cursor, result['lead_id'], tag_names)
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
                     path: str, event_name: str, added_by: int, tag_names=None) -> dict:
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
    if tag_names:
        tag_lead(cursor, result['lead_id'], tag_names)

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


def enroll_segment(cursor, conn, automation_id: int, segment_id: int) -> dict:
    """Batch-enroll every lead matching a saved segment into an automation on the NEW
    engine -- the new engine's counterpart to enroll_batch(), which only writes to the
    legacy drip_enrollments table the new engine's cron never reads. Reuses enroll_batch()'s
    exclusion-rule structure (no-email, suppression, lead_pool.is_eligible()) since those are
    universal data-hygiene concerns, not cold-campaign-specific ones.

    Deliberately does NOT reuse enroll_batch()'s cold->warm auto-redirect
    (_matching_warm_campaign_id/WARM_SUFFIX) -- that's cold-campaign business logic tied to a
    naming convention. A segment enroll puts a lead into the automation actually requested;
    branching within the graph (if_else) is what handles routing elsewhere, not the enroll
    step -- auto-redirecting here too would be redundant and surprising for a non-cold
    automation.

    Start-node resolution mirrors the cross-automation hand-off inside
    _advance_automation_graph() exactly (same call shape, run_id=None -- no prior run/send
    exists yet for a fresh enrollment): walk from the automation's own trigger node until
    reaching a real pause point, then create/revive the run there via
    _create_or_revive_run(). This lets a lead pass through any add_to_list/delay nodes between
    the trigger and the first real send, same as a hand-off already does.

    Returns {'enrolled', 'skipped_suppressed', 'skipped_nurture_owned',
    'skipped_cluster_conflict', 'skipped_no_email'}."""
    cursor.execute(
        "SELECT node_key FROM automation_steps WHERE automation_id=%s AND node_type='trigger'",
        (automation_id,)
    )
    trigger = cursor.fetchone()
    empty_result = {'enrolled': 0, 'skipped_suppressed': 0, 'skipped_nurture_owned': 0,
                     'skipped_cluster_conflict': 0, 'skipped_no_email': 0}
    if not trigger:
        return empty_result

    candidate_ids = segments.segment_member_ids(cursor, segment_id)
    if not candidate_ids:
        return empty_result

    placeholders = ','.join(['%s'] * len(candidate_ids))

    cursor.execute(
        f"""SELECT DISTINCT l.id FROM leads l
            LEFT JOIN ses_suppressions s ON s.email_address = l.email
            LEFT JOIN sendy_suppressions sy ON sy.email_address = l.email
            WHERE l.id IN ({placeholders}) AND l.email IS NOT NULL AND l.suppressed_at IS NULL
              AND s.email_address IS NULL AND sy.email_address IS NULL""",
        candidate_ids
    )
    clean_ids = [row['id'] for row in cursor.fetchall()]

    cursor.execute(
        f"SELECT COUNT(*) AS n FROM leads l WHERE l.id IN ({placeholders}) AND l.email IS NULL",
        candidate_ids
    )
    skipped_no_email = cursor.fetchone()['n']

    cursor.execute(
        f"""SELECT COUNT(DISTINCT l.id) AS n FROM leads l
            LEFT JOIN ses_suppressions s ON s.email_address = l.email
            LEFT JOIN sendy_suppressions sy ON sy.email_address = l.email
            WHERE l.id IN ({placeholders})
              AND (l.suppressed_at IS NOT NULL OR s.email_address IS NOT NULL OR sy.email_address IS NOT NULL)""",
        candidate_ids
    )
    skipped_suppressed = cursor.fetchone()['n']

    eligible_ids = []
    skipped_nurture_owned = 0
    skipped_cluster_conflict = 0
    for lead_id in clean_ids:
        eligible, reason = lead_pool.is_eligible(cursor, lead_id)
        if eligible:
            eligible_ids.append(lead_id)
        elif 'wp_nurture_owned' in reason:
            skipped_nurture_owned += 1
        else:
            skipped_cluster_conflict += 1

    enrolled = 0
    now = datetime.now()
    for lead_id in eligible_ids:
        outcome = _advance_automation_graph(
            cursor, conn, automation_id, run_id=None, start_node_key=trigger['node_key'],
            anchor_time=now, lead_id=lead_id,
        )
        if outcome.get('status') == 'next_send':
            result = _create_or_revive_run(
                cursor, conn, automation_id, lead_id, outcome['node_key'], due_at=outcome['due_at'],
            )
            if result['enrolled']:
                enrolled += 1

    return {'enrolled': enrolled, 'skipped_suppressed': skipped_suppressed,
            'skipped_nurture_owned': skipped_nurture_owned,
            'skipped_cluster_conflict': skipped_cluster_conflict,
            'skipped_no_email': skipped_no_email}


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
    # 2026-09-23: Outlook's VML v:roundrect doesn't reliably wrap text onto a second line
    # without clipping the shape, so instead of trying to make it wrap, the box is sized
    # wide enough to fit the button's own text on ONE line -- character-width estimate
    # tuned for 15px bold Arial (~9.5px/char), plus the same 26px horizontal padding the
    # modern (!mso) anchor below uses, so both versions read as the same button. The old
    # fixed 240px width didn't grow for longer CTA text (confirmed: "Show me how to use
    # this with clients", 37 chars, needed 2 lines and the box stayed 240px, clipping it).
    vml_width = max(200, round(len(cta_text) * 9.5) + 72)
    return (
        f'<table role="presentation" cellspacing="0" cellpadding="0" border="0" align="center" style="margin:8px auto;"><tr><td align="center" bgcolor="{teal}" style="border-radius:10px;">'
        f'<!--[if mso]><v:roundrect xmlns:v="urn:schemas-microsoft-com:vml" xmlns:w="urn:schemas-microsoft-com:office:word" href="{cta_url}" style="height:44px;v-text-anchor:middle;width:{vml_width}px;" arcsize="22%" strokecolor="{teal}" fillcolor="{teal}"><w:anchorlock/><center style="color:{navy};font-family:{font};font-size:15px;font-weight:bold;">{cta_text}</center></v:roundrect><![endif]-->'
        f'<!--[if !mso]><!--><a href="{cta_url}" style="display:inline-block;padding:12px 26px;font-family:{font};font-size:15px;font-weight:700;color:{navy};text-decoration:none;border-radius:10px;background:{teal};white-space:nowrap;">{cta_text}</a><!--<![endif]-->'
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


# 2026-09-22 (email-builder-spec-2026-09-21.md SS4.2): shared classifier for
# separating real human opens/clicks from automated security-scanner traffic.
# ONE function, called from the branch evaluator, the per-recipient table
# (work order SSD.1) and any report -- not the same logic written three
# times, which is the is_eligible() lesson (work order SSB.3) arriving here.
#
# Three states, not two. A fast open forced into a binary opened/not-opened
# either wrongly counts a scanner as a real lead, or -- the direction that
# actually costs more at this volume -- wrongly discards a real prospect who
# opened and clicked within minutes, the hottest kind of lead there is. The
# middle state exists so a misrouted lead is recoverable (the branch UI's
# own choice of where it goes), not silently lost.
#
# Classifies on open speed alone, not on whether a click followed. The first
# version of this filter treated "no click" as evidence of a real human,
# which forced a genuinely ambiguous case (an open landing in 94 seconds --
# bot-speed -- with no click after it) into "human" by default rather than
# into the unknown middle where it belongs. Click timing is informative but
# is not required to flag an open as suspicious; the scanner signal is in
# how fast the open itself happened, not in what happened after.
#
# Threshold source: Agency Principals (campaign_id=18), 2026-09-21/22. 28 of
# step 3's 31 opens land 32-219 seconds after send, tight and uniform across
# 28 different recipients -- real human variability does not produce that.
# The remaining opens in that step took 18 and 38 minutes, the shape a real
# person's open actually has.
ENGAGEMENT_FILTER_VERSION = 'v1-2026-09-22'
ENGAGEMENT_FAST_OPEN_SECONDS = 300  # open recorded within this many seconds of send


def classify_engagement(sent_at, opened_at, clicked_at=None):
    """Classify one send's engagement into exactly one of three states.

    Returns (state, filter_version):
      'not_opened'          -- opened_at is None
      'opened_bot_pattern'  -- opened within ENGAGEMENT_FAST_OPEN_SECONDS of sent_at
      'opened_human_like'   -- opened at or after that threshold

    clicked_at is accepted but not currently used in the classification
    itself (see module comment above) -- kept in the signature so a caller
    can pass the full row without the classifier's own logic dictating what
    the caller has on hand, and so a future filter version can use it
    without changing every call site.

    filter_version is returned alongside every call so a caller that
    persists a classification (a branch decision) can stamp which version
    made it -- email-builder-spec-2026-09-21.md SS4.2's second engineering
    requirement. Improving this function later must never silently rewrite
    a decision already made; only ever record against the version live at
    the time the decision was made.
    """
    if opened_at is None:
        return 'not_opened', ENGAGEMENT_FILTER_VERSION

    secs_to_open = (opened_at - sent_at).total_seconds()
    if secs_to_open < ENGAGEMENT_FAST_OPEN_SECONDS:
        return 'opened_bot_pattern', ENGAGEMENT_FILTER_VERSION

    return 'opened_human_like', ENGAGEMENT_FILTER_VERSION


# Work order §7.4/§8.6: neither send path capped how many times one address could be
# reached, and incidents/nurture-email-runaway-2026-09-21.md is 8,469 sends to one address in
# nine days because nothing watched volume. Generous relative to real cadence (a lead normally
# gets at most one send per day per sequence) so it never touches legitimate behavior, while
# still catching a runaway bug orders of magnitude sooner than the incident that prompted this.
SEND_CEILING_MAX_PER_24H = 5

# Work order §10.4: a refusal alone doesn't decide what happens to the run. Left un-deferred,
# next_due_at stays in the past and the SAME row gets re-selected, re-checked, and re-logged
# every single cron cycle until the 24h window clears on its own -- a bounded but wasteful
# hourly hot loop, and (worse) one new drip_send_log row per cycle for a single held-back send.
# Deferring past the window fixes the loop; capping deferrals is what makes a lead who trips
# this repeatedly surface for a human instead of retrying forever -- same "bounded retries with
# a hard attempt limit" shape §4A.3 requires of enrichment, applied here too.
SEND_CEILING_MAX_DEFERRALS = 3


def check_send_ceiling(cursor, email_address: str, window_hours: int = 24, limit: int = SEND_CEILING_MAX_PER_24H):
    """Counts real 'sent' rows to this address across BOTH engines in the trailing window --
    keyed on email, not lead_id, since the same person can exist as more than one `leads` row
    (confirmed this session: a free-check signup and a later Marblism import of the same email
    are two different lead_id values). drip_send_log.run_key covers the new engine,
    .enrollment_id the legacy one; a send logged either way counts against the same address.

    Returns (ok: bool, count: int) -- ok=False means the caller must not send."""
    cursor.execute(
        """SELECT COUNT(*) AS n FROM drip_send_log dsl
           LEFT JOIN automation_runs ar ON ar.id = dsl.run_key
           LEFT JOIN drip_enrollments de ON de.id = dsl.enrollment_id
           LEFT JOIN leads l1 ON l1.id = ar.lead_id
           LEFT JOIN leads l2 ON l2.id = de.lead_id
           WHERE dsl.status='sent' AND dsl.sent_at > DATE_SUB(NOW(), INTERVAL %s HOUR)
             AND (l1.email=%s OR l2.email=%s)""",
        (window_hours, email_address, email_address)
    )
    count = cursor.fetchone()['n']
    return count < limit, count


def _alert_send_ceiling_tripped(cursor, send_email_fn, email_address: str, count: int):
    """Fires once per address per 24h window, not once per cron cycle -- checks whether a
    'rate_limited' row for this address already exists in the window before sending (the row
    this call's caller just inserted counts as 1, so >1 means already alerted). Per this
    codebase's existing alerting posture (citemetrix-alerting-philosophy: alerts fire on
    genuine sustained problems, not routine variance) -- a tripped send ceiling is exactly
    that, not noise. Never let an alert failure block the send loop itself."""
    cursor.execute(
        """SELECT COUNT(*) AS n FROM drip_send_log dsl
           LEFT JOIN automation_runs ar ON ar.id = dsl.run_key
           LEFT JOIN drip_enrollments de ON de.id = dsl.enrollment_id
           LEFT JOIN leads l1 ON l1.id = ar.lead_id
           LEFT JOIN leads l2 ON l2.id = de.lead_id
           WHERE dsl.status='rate_limited' AND dsl.sent_at > DATE_SUB(NOW(), INTERVAL 24 HOUR)
             AND (l1.email=%s OR l2.email=%s)""",
        (email_address, email_address)
    )
    if cursor.fetchone()['n'] > 1:
        return
    try:
        send_email_fn(
            to='admin@citemetrix.com',
            subject=f'[CiteMetrix] Send ceiling tripped: {email_address}',
            body_text=(
                f'{email_address} already received {count} email(s) in the last 24 hours and has '
                f'been blocked from further sends (cap {SEND_CEILING_MAX_PER_24H}/24h). Further '
                f'sends to this address are blocked and logged as drip_send_log.status=\'rate_limited\' '
                f'until the window clears. Check drip_send_log for this address before raising the '
                f'cap or manually clearing anything.'
            ),
        )
    except Exception:
        pass


def build_unsubscribe_footer(unsub_url: str) -> str:
    return f"\n\n—\nEric Richmond\nCiteMetrix LLC, PO Box 324, Norwalk, CT 06853-0324\nDon't want these emails? {unsub_url}"


def build_unsubscribe_footer_html(unsub_url: str) -> str:
    inner = (
        '<p style="margin:24px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;'
        'line-height:1.6;color:#8a94a3;border-top:1px solid #e5e7eb;padding-top:16px;">'
        'Eric Richmond<br>CiteMetrix LLC, PO Box 324, Norwalk, CT 06853-0324<br>'
        f'Don\'t want these emails? <a href="{unsub_url}" style="color:#8a94a3;">Unsubscribe</a></p>'
    )
    return (
        '<table width="100%" border="0" cellpadding="0" cellspacing="0" role="presentation">'
        '<tr><td align="center">'
        '<table width="600" border="0" cellpadding="0" cellspacing="0" role="presentation" '
        'style="max-width:600px;width:100%;">'
        f'<tr><td>{inner}</td></tr>'
        '</table>'
        '</td></tr></table>'
    )


def wrap_html_document(body_html: str) -> str:
    """2026-09-23: needs a real <html><head>...</head><body> document, not a bare
    fragment -- Outlook/Word synthesizes its own unstyled document around a bare
    fragment otherwise. Confirmed against cac-business-dev's SendRadiusDigest.php,
    a proven-working Outlook-safe sender for the exact same audience/environment
    (Eric's own recipients, same Outlook client) -- its actual working shape does
    NOT put align="center" on <body> or on any <table> at all; centering is done
    purely via <td align="center"> on the wrapping cell (already present in each
    email's own body_html -- see apply_hero_images.py), plus the VML namespace
    declarations and MSO PixelsPerInch block below, which that reference always
    includes and this codebase never had. An earlier version of this function put
    align="center" on <body> based on a different (also real, but apparently not
    load-bearing) competitor email; dropped in favor of matching the one reference
    that's PROVEN to render correctly for CiteMetrix's own Eric, in the same
    Outlook, every send, today. Callers must invoke this ONCE, as the very last
    step after the unsubscribe footer (and any other trailing HTML) has already
    been appended -- wrapping any earlier would leave that trailing content
    outside </html>, which is invalid."""
    return (
        '<!doctype html>'
        '<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<!--[if mso]><noscript><xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch>'
        '</o:OfficeDocumentSettings></xml></noscript><![endif]-->'
        '</head>'
        f'<body style="margin:0;padding:0;">{body_html}</body></html>'
    )


def _matching_warm_automation_id(cursor, cold_automation_name: str):
    """automations-table counterpart to _matching_warm_campaign_id() above -- same
    WARM_SUFFIX naming convention, same None-means-not-available contract. Kept as
    its own small function rather than parameterizing the legacy one: the two
    engines' schemas diverge (automations vs drip_campaigns) enough that sharing one
    function would mean threading a table name through every query, which is more
    confusing than two short mirrors of the same idea."""
    if not cold_automation_name:
        return None
    cursor.execute("SELECT id FROM automations WHERE name=%s", (cold_automation_name + WARM_SUFFIX,))
    row = cursor.fetchone()
    return row['id'] if row else None


def _create_or_revive_run(cursor, conn, automation_id: int, lead_id: int, start_node_key: str, due_at=None) -> dict:
    """automation_runs counterpart to enroll_single_lead() -- identical semantics,
    targeting the new engine's table: a suppressed row is revived (reset to active,
    repositioned at start_node_key), active/completed/unsubscribed/blocked/cancelled
    rows are left alone. due_at defaults to now, same rationale as
    enroll_single_lead() (a warm automation's first send node has no preceding delay
    worth waiting on when branching in from a completed check).

    Returns {'enrolled': bool, 'reason': str|None}, same shape as enroll_single_lead()."""
    now = datetime.now()
    if due_at is None:
        due_at = now
    cursor.execute(
        "SELECT id, status FROM automation_runs WHERE lead_id=%s AND automation_id=%s",
        (lead_id, automation_id)
    )
    existing = cursor.fetchone()
    if existing is None:
        cursor.execute(
            """INSERT INTO automation_runs (automation_id, lead_id, current_node_key, next_due_at, status, entered_at)
               VALUES (%s,%s,%s,%s,'active',%s)""",
            (automation_id, lead_id, start_node_key, due_at.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        return {'enrolled': True, 'reason': None}
    if existing['status'] == 'suppressed':
        cursor.execute(
            """UPDATE automation_runs SET status='active', current_node_key=%s,
                   entered_at=%s, next_due_at=%s, last_sent_at=NULL
               WHERE id=%s""",
            (start_node_key, now.strftime('%Y-%m-%d %H:%M:%S'), due_at.strftime('%Y-%m-%d %H:%M:%S'), existing['id'])
        )
        conn.commit()
        return {'enrolled': True, 'reason': None}
    return {'enrolled': False, 'reason': f"already {existing['status']} on this automation"}


def _advance_automation_graph(cursor, conn, automation_id: int, run_id: int, start_node_key, anchor_time, lead_id: int) -> dict:
    """Walk forward from start_node_key through every node that doesn't itself
    consume a cron cycle, until reaching one of three outcomes for this run:

      {'status': 'next_send', 'node_key': <send_email node>, 'due_at': <datetime>}
      {'status': 'unsubscribed'}
      {'status': 'completed'}   -- ran off the end of the graph, or hit a dangling
                                   pointer (treated the same as "nothing left here")

    Called right after a successful send (start_node_key = that send node's
    next_node_key, anchor_time = the moment the send just completed) -- so a
    'delay' node's due_at is always anchored on actual send time, never on
    entered_at, preserving process_due_enrollments()'s existing fix for the
    2026-09-08 same-day step1+step2 bug (day_offset is 'days after this step
    actually sent', not 'days after original enrollment').

    if_else classifies the run's most recent send (by drip_send_log.run_key) via
    the shared classify_engagement() and stamps (state, filter_version) to
    automation_run_events BEFORE picking an edge -- email-builder-spec-2026-09-21.md
    SS4.2's second engineering requirement, so improving the filter later can never
    silently rewrite a routing decision already made. The three-state classifier
    reconciles onto the two graph edges via config: an exact 'opened_human_like'
    (or whatever states config lists) match takes next_node_key; 'opened_bot_pattern'
    -- the recoverable middle state -- takes whichever edge config's middle_routing
    says; everything else takes next_node_key_alt.

    add_to_list/remove_from_list are structural pass-throughs right now: the graph
    walks through them correctly (a campaign built with these nodes doesn't get
    stuck), but no list-membership write happens yet. The backing mechanism
    (lead_batches vs. a new first-class `lists` table) is an open decision for Eric
    per the approved plan -- this deliberately does not guess an answer.

    A visited-node cycle guard raises rather than looping forever on a malformed
    graph, so a broken automation surfaces loudly on the one run that hits it
    instead of silently spinning the cron."""
    node_key = start_node_key
    due_at = anchor_time
    visited = set()
    while node_key:
        if node_key in visited:
            raise RuntimeError(f"automation {automation_id}: graph cycle detected at node {node_key}")
        visited.add(node_key)

        cursor.execute(
            "SELECT node_key, node_type, config, next_node_key, next_node_key_alt, "
            "next_automation_id, next_automation_id_alt "
            "FROM automation_steps WHERE automation_id=%s AND node_key=%s",
            (automation_id, node_key)
        )
        node = cursor.fetchone()
        if not node:
            return {'status': 'completed'}

        if node['node_type'] == 'send_email':
            return {'status': 'next_send', 'node_key': node['node_key'], 'due_at': due_at}

        if node['node_type'] == 'unsubscribe':
            return {'status': 'unsubscribed'}

        if node['node_type'] == 'delay':
            cfg = json.loads(node['config']) if node['config'] else {}
            value = cfg.get('value', 0)
            unit = cfg.get('unit', 'days')
            due_at = anchor_time + (timedelta(hours=value) if unit == 'hours' else timedelta(days=value))
            node_key = node['next_node_key']
            continue

        if node['node_type'] in ('add_to_list', 'remove_from_list'):
            cfg = json.loads(node['config']) if node['config'] else {}
            list_id = cfg.get('list_id')
            if list_id is not None:
                if node['node_type'] == 'add_to_list':
                    cursor.execute('INSERT IGNORE INTO list_memberships (list_id, lead_id) VALUES (%s,%s)', (list_id, lead_id))
                else:
                    cursor.execute('DELETE FROM list_memberships WHERE list_id=%s AND lead_id=%s', (list_id, lead_id))
                conn.commit()
            node_key = node['next_node_key']
            continue

        if node['node_type'] == 'if_else':
            cursor.execute(
                "SELECT sent_at, opened_at, clicked_at FROM drip_send_log WHERE run_key=%s ORDER BY sent_at DESC LIMIT 1",
                (run_id,)
            )
            last_send = cursor.fetchone()
            if last_send:
                state, filter_version = classify_engagement(last_send['sent_at'], last_send['opened_at'], last_send['clicked_at'])
            else:
                state, filter_version = 'not_opened', ENGAGEMENT_FILTER_VERSION

            cfg = json.loads(node['config']) if node['config'] else {}
            true_states = cfg.get('true_states', [])
            middle_routing = cfg.get('middle_routing', 'false')

            if state in true_states:
                branch = 'true'
            elif state == 'opened_bot_pattern':
                branch = middle_routing
            else:
                branch = 'false'

            if branch == 'true':
                next_key, next_automation = node['next_node_key'], node['next_automation_id']
            else:
                next_key, next_automation = node['next_node_key_alt'], node['next_automation_id_alt']

            # Cross-automation hand-off (branching extension, 2026-09-22): an edge whose
            # next_automation_id is set routes this run to a DIFFERENT automation
            # entirely rather than continuing this graph -- generalizes the existing
            # cold->warm hand-off (process_check_completions_runs, unchanged) into
            # something any branch can do. Walk the target automation's own graph from
            # its trigger to find where a fresh run actually starts (same technique
            # process_check_completions_runs already uses), then create/revive a run
            # there. The source run still just completes -- which automation it handed
            # off to is recorded in this event row's branch_taken, not in run state.
            if next_automation and next_automation != automation_id:
                cursor.execute(
                    """INSERT INTO automation_run_events
                         (run_key, node_key, node_type, branch_taken, engagement_state, filter_version)
                       VALUES (%s,%s,'if_else',%s,%s,%s)""",
                    (run_id, node['node_key'], f'automation:{next_automation}', state, filter_version)
                )
                conn.commit()
                cursor.execute(
                    "SELECT node_key FROM automation_steps WHERE automation_id=%s AND node_type='trigger'",
                    (next_automation,)
                )
                trigger = cursor.fetchone()
                if trigger:
                    target_outcome = _advance_automation_graph(
                        cursor, conn, next_automation, run_id=None, start_node_key=trigger['node_key'],
                        anchor_time=datetime.now(), lead_id=lead_id,
                    )
                    if target_outcome.get('status') == 'next_send':
                        _create_or_revive_run(
                            cursor, conn, next_automation, lead_id,
                            target_outcome['node_key'], due_at=target_outcome['due_at'],
                        )
                return {'status': 'completed'}

            cursor.execute(
                """INSERT INTO automation_run_events
                     (run_key, node_key, node_type, branch_taken, engagement_state, filter_version)
                   VALUES (%s,%s,'if_else',%s,%s,%s)""",
                (run_id, node['node_key'], next_key, state, filter_version)
            )
            conn.commit()
            node_key = next_key
            continue

        # trigger, or any future node_type this walker doesn't specifically
        # recognize -- advance past it rather than getting stuck.
        node_key = node['next_node_key']

    return {'status': 'completed'}


def recover_blocked_runs(admin_cursor, admin_conn, product_cursor=None, product_conn=None) -> dict:
    """automation_runs counterpart to recover_blocked_enrollments() above -- identical
    logic against the new table: re-check every status='blocked' run's eligibility
    each cycle (same skip_wp_nurture_check-on-warm-track rule) and flip back to
    'active' on success, closing the same permanently-stuck-blocked incident class
    for the new engine. next_due_at is left untouched, same rationale: it was
    already due when the run got blocked, so it's still due now.

    Same WordPress nurture_stood_down write on a recovered warm-track run, same
    best-effort/non-fatal posture, as recover_blocked_enrollments().

    Returns {'blocked_checked': int, 'recovered': int}."""
    admin_cursor.execute(
        """SELECT r.id AS run_id, r.lead_id, l.email, a.name AS automation_name
           FROM automation_runs r
           JOIN leads l ON l.id = r.lead_id
           JOIN automations a ON a.id = r.automation_id
           WHERE r.status='blocked'"""
    )
    blocked = admin_cursor.fetchall()

    recovered = 0
    for row in blocked:
        is_warm_track = (row['automation_name'] or '').endswith(WARM_SUFFIX)
        elig, _reason = lead_pool.is_eligible(
            admin_cursor, row['lead_id'], exclude_run_id=row['run_id'],
            skip_wp_nurture_check=is_warm_track,
        )
        if elig:
            admin_cursor.execute("UPDATE automation_runs SET status='active' WHERE id=%s", (row['run_id'],))
            admin_conn.commit()
            recovered += 1

            if is_warm_track and product_conn is not None and row.get('email'):
                try:
                    product_cursor.execute(
                        """UPDATE wp_citemetrix_score_leads
                           SET nurture_stood_down = 1, nurture_stood_down_at = NOW()
                           WHERE email = %s AND nurture_stood_down = 0""",
                        (row['email'],)
                    )
                    product_conn.commit()
                except Exception as e:
                    print(f"[recover_blocked_runs] WARNING: failed to stand down "
                          f"WordPress nurture for {row['email']}: {e}")

    return {'blocked_checked': len(blocked), 'recovered': recovered}


def process_check_completions_runs(admin_cursor, admin_conn, product_cursor, product_conn=None) -> dict:
    """automation_runs counterpart to process_check_completions() above -- identical
    branching rule against the new tables: a lead who completes a /check/ scan while
    on an active COLD automation gets that run status='cancelled' and is (re)started
    on the matching WARM automation's graph via _create_or_revive_run(), due_at=now.
    Kept as its own function (not graph-native) for behavioral parity with the
    legacy engine during the migration -- a graph-native version of this branch
    (if_else on check-completion) is future work, not part of this migration.

    Same idempotency-by-construction as process_check_completions(): once a cold run
    is cancelled here it's excluded from this function's own starting query on the
    next run, so no separate already-branched check is needed.

    Returns {'cold_active_checked': int, 'branched': int}."""
    admin_cursor.execute(
        """SELECT r.id AS run_id, r.lead_id, r.automation_id, l.unsubscribe_token, l.email,
                  a.name AS automation_name
           FROM automation_runs r
           JOIN leads l ON l.id = r.lead_id
           JOIN automations a ON a.id = r.automation_id
           WHERE r.status='active'"""
    )
    active_cold = admin_cursor.fetchall()

    cold_active_checked = 0
    branched = 0
    now = datetime.now()
    for row in active_cold:
        warm_automation_id = _matching_warm_automation_id(admin_cursor, row['automation_name'])
        if not warm_automation_id:
            continue
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
            continue

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

        admin_cursor.execute("UPDATE automation_runs SET status='cancelled' WHERE id=%s", (row['run_id'],))
        admin_conn.commit()

        admin_cursor.execute(
            "SELECT node_key, next_node_key FROM automation_steps WHERE automation_id=%s AND node_type='trigger'",
            (warm_automation_id,)
        )
        trigger = admin_cursor.fetchone()
        # walk the warm automation's own graph from its trigger to find where a
        # fresh run actually starts (the first send_email node) -- mirrors
        # enroll_single_lead()'s "now == now + 0 days" reasoning: a warm automation's
        # own first delay is 0 days, so walking it costs nothing.
        outcome = _advance_automation_graph(
            admin_cursor, admin_conn, warm_automation_id, None,
            trigger['next_node_key'] if trigger else None, anchor_time=now, lead_id=row['lead_id'],
        ) if trigger else {'status': 'completed'}

        result = {'enrolled': False, 'reason': 'warm automation has no reachable send node'}
        if outcome['status'] == 'next_send':
            result = _create_or_revive_run(admin_cursor, admin_conn, warm_automation_id, row['lead_id'],
                                             outcome['node_key'], due_at=now)

        if result['enrolled']:
            admin_cursor.execute(
                """INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload)
                   VALUES (%s,NOW(),'note',NULL,%s)""",
                (row['lead_id'], json.dumps({'event': 'branched_to_warm', 'from_automation_id': row['automation_id'], 'to_automation_id': warm_automation_id}))
            )
            admin_conn.commit()
            branched += 1

            if product_conn is not None and row.get('email'):
                try:
                    product_cursor.execute(
                        """UPDATE wp_citemetrix_score_leads
                           SET nurture_stood_down = 1, nurture_stood_down_at = NOW()
                           WHERE email = %s AND nurture_stood_down = 0""",
                        (row['email'],)
                    )
                    product_conn.commit()
                except Exception as e:
                    print(f"[process_check_completions_runs] WARNING: failed to stand down "
                          f"WordPress nurture for {row['email']}: {e}")

    return {'cold_active_checked': cold_active_checked, 'branched': branched}


def process_due_runs(admin_cursor, admin_conn, send_email_fn, base_url: str, max_sends: int = 50) -> dict:
    """automation_runs counterpart to process_due_enrollments() -- the new engine's
    sender. A run's current_node_key always points at a send_email node when it is
    selected below: every non-send node between one send and the next is walked
    immediately by _advance_automation_graph(), either right after the previous send
    (see the tail of this function) or by whatever creates the run in the first
    place -- so 'due' here means exactly what it means in the legacy engine, 'this
    run's next scheduled SEND is due now', not 'some node is due'.

    Every send-affecting check runs in the SAME order, calling the SAME shared
    functions, as process_due_enrollments(): unsubscribe (terminal) -> suppression
    (status='suppressed' + log row) -> lead_pool.is_eligible() re-check
    (status='blocked' + log row) -> render via the same render_merge_tags()/
    apply_cta_tags() -> the same unsubscribe footer -> the same send_email_fn. On
    success: walk the graph forward from the sent node's next_node_key, anchored on
    the actual send time, and persist wherever the walk lands (another send node,
    unsubscribed, or completed). On failure: log status='failed', do NOT advance --
    next_due_at stays in the past and this run is picked up again next cron cycle,
    matching process_due_enrollments() exactly (no new retry/backoff introduced)."""
    admin_cursor.execute(
        """SELECT r.id AS run_id, r.lead_id, r.automation_id, r.current_node_key,
                  a.name AS automation_name,
                  l.email, l.first_name, l.last_name, l.company, l.unsubscribe_token,
                  l.suppressed_at, l.suppression_reason, l.email_verification_status,
                  (s.email_address IS NOT NULL OR sy.email_address IS NOT NULL) AS is_live_suppressed,
                  (SELECT payload FROM lead_events
                     WHERE lead_id = l.id AND type = 'free_check_completed'
                     ORDER BY id DESC LIMIT 1) AS fc_payload
           FROM automation_runs r
           JOIN automations a ON a.id = r.automation_id
           JOIN leads l ON l.id = r.lead_id
           LEFT JOIN ses_suppressions s ON s.email_address = l.email
           LEFT JOIN sendy_suppressions sy ON sy.email_address = l.email
           WHERE r.status='active' AND r.next_due_at <= NOW() AND a.status='active'
           ORDER BY r.next_due_at ASC LIMIT %s""",
        (max_sends,)
    )
    due = admin_cursor.fetchall()

    sent, failed, skipped_unsub, skipped_suppressed, skipped_blocked, skipped_rate_limited = 0, 0, 0, 0, 0, 0
    for row in due:
        if row['suppression_reason'] == 'unsubscribed':
            admin_cursor.execute("UPDATE automation_runs SET status='unsubscribed' WHERE id=%s", (row['run_id'],))
            admin_conn.commit()
            skipped_unsub += 1
            continue

        admin_cursor.execute(
            "SELECT node_key, config, next_node_key FROM automation_steps WHERE automation_id=%s AND node_key=%s AND node_type='send_email'",
            (row['automation_id'], row['current_node_key'])
        )
        node = admin_cursor.fetchone()
        if not node:
            # current_node_key doesn't resolve to a send_email node -- the graph
            # can't be walked further for this run. Treat as completed rather than
            # re-selecting it forever (mirrors process_due_enrollments()'s "no next
            # step -> completed" branch for the equivalent dangling case).
            admin_cursor.execute("UPDATE automation_runs SET status='completed' WHERE id=%s", (row['run_id'],))
            admin_conn.commit()
            continue
        email_id = json.loads(node['config'])['email_id']
        admin_cursor.execute("SELECT * FROM emails WHERE id=%s", (email_id,))
        email = admin_cursor.fetchone()

        if row['suppressed_at'] is not None or row['is_live_suppressed']:
            admin_cursor.execute("UPDATE automation_runs SET status='suppressed' WHERE id=%s", (row['run_id'],))
            admin_cursor.execute(
                "INSERT INTO drip_send_log (run_key, email_id, node_key, status, error) VALUES (%s,%s,%s,'suppressed',%s)",
                (row['run_id'], email_id, node['node_key'], f"{row['email']} is on the SES suppression list at send time")
            )
            admin_conn.commit()
            skipped_suppressed += 1
            continue

        # 2026-09-23 -- see process_due_enrollments()'s identical gate for the full
        # rationale. Same scoping: 'invalid'/'disposable' only, not bare 'unverified'.
        if row['email_verification_status'] in ('invalid', 'disposable'):
            admin_cursor.execute("UPDATE automation_runs SET status='blocked' WHERE id=%s", (row['run_id'],))
            admin_cursor.execute(
                "INSERT INTO drip_send_log (run_key, email_id, node_key, status, error) VALUES (%s,%s,%s,'blocked',%s)",
                (row['run_id'], email_id, node['node_key'], f"{row['email']} is verification-status '{row['email_verification_status']}' at send time")
            )
            admin_conn.commit()
            skipped_blocked += 1
            continue

        is_warm_track = (row['automation_name'] or '').endswith(WARM_SUFFIX)
        elig, elig_reason = lead_pool.is_eligible(
            admin_cursor, row['lead_id'], exclude_run_id=row['run_id'],
            skip_wp_nurture_check=is_warm_track,
        )
        if not elig:
            admin_cursor.execute("UPDATE automation_runs SET status='blocked' WHERE id=%s", (row['run_id'],))
            admin_cursor.execute(
                "INSERT INTO drip_send_log (run_key, email_id, node_key, status, error) VALUES (%s,%s,%s,'blocked',%s)",
                (row['run_id'], email_id, node['node_key'], f"{row['email']} failed the pool eligibility gate at send time: {elig_reason}")
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
                    platform_results = json.loads(platform_results)
                checked, mentioned, missing = _platform_stats(platform_results)
                lead['platforms_checked_count'] = checked
                lead['platforms_mentioned_count'] = mentioned
                lead['missing_platforms'] = missing
            except (ValueError, TypeError):
                pass
        unsub_url = f"{base_url}/unsubscribe/{row['unsubscribe_token']}"

        ceiling_ok, ceiling_count = check_send_ceiling(admin_cursor, row['email'])
        if not ceiling_ok:
            admin_cursor.execute(
                "INSERT INTO drip_send_log (run_key, email_id, node_key, status, error) VALUES (%s,%s,%s,'rate_limited',%s)",
                (row['run_id'], email_id, node['node_key'], f"{row['email']} already received {ceiling_count} sends in the last 24h (cap {SEND_CEILING_MAX_PER_24H})")
            )
            admin_cursor.execute(
                "SELECT COUNT(*) AS n FROM drip_send_log WHERE run_key=%s AND status='rate_limited'",
                (row['run_id'],)
            )
            deferrals = admin_cursor.fetchone()['n']
            if deferrals >= SEND_CEILING_MAX_DEFERRALS:
                admin_cursor.execute("UPDATE automation_runs SET status='blocked' WHERE id=%s", (row['run_id'],))
            else:
                new_due = datetime.now() + timedelta(hours=24)
                admin_cursor.execute(
                    "UPDATE automation_runs SET next_due_at=%s WHERE id=%s",
                    (new_due.strftime('%Y-%m-%d %H:%M:%S'), row['run_id'])
                )
            admin_conn.commit()
            _alert_send_ceiling_tripped(admin_cursor, send_email_fn, row['email'], ceiling_count)
            skipped_rate_limited += 1
            continue

        if email['is_survey_step']:
            survey_url = f"https://citemetrix.com/survey/?epoch={email['survey_epoch'] or ''}&campaign={row['automation_id']}&source=drip&email={row['email']}"
            subject = render_merge_tags(email['subject'], lead) or "Quick question - what stopped you?"
            body = render_merge_tags(email['body_text'], lead) or f"Hi {lead['first_name'] or 'there'},\n\n{survey_url}"
            body += f"\n\n{survey_url}"
            body_html = None
        else:
            subject = render_merge_tags(email['subject'], lead)
            body = render_merge_tags(email['body_text'], lead)
            body_html = render_merge_tags(email['body_html'], lead)
            # emails carries cta_key/cta_text in the same shape apply_cta_tags()
            # already expects from a drip_steps row -- a small shim dict means the
            # shared function needs no changes to serve both engines.
            step_shim = {'cta_text': email['cta_text'], 'cta_key': email['cta_key'], 'step_order': 0}
            body, body_html = apply_cta_tags(body, body_html, step_shim, lead_token=row['unsubscribe_token'])

        body += build_unsubscribe_footer(unsub_url)
        if body_html:
            body_html += build_unsubscribe_footer_html(unsub_url)
            body_html = wrap_html_document(body_html)

        send_kwargs = {'to': lead['email'], 'subject': subject, 'body_text': body,
                       'list_unsubscribe': unsub_url}
        if body_html:
            send_kwargs['body_html'] = body_html
        ok, result = send_email_fn(**send_kwargs)

        if ok:
            admin_cursor.execute(
                "INSERT INTO drip_send_log (run_key, email_id, node_key, provider_msg_id, status) VALUES (%s,%s,%s,%s,'sent')",
                (row['run_id'], email_id, node['node_key'], result)
            )
            admin_conn.commit()

            outcome = _advance_automation_graph(
                admin_cursor, admin_conn, row['automation_id'], row['run_id'], node['next_node_key'],
                anchor_time=datetime.now(), lead_id=row['lead_id'],
            )
            if outcome['status'] == 'next_send':
                admin_cursor.execute(
                    "UPDATE automation_runs SET current_node_key=%s, next_due_at=%s, last_sent_at=NOW(), last_email_id=%s WHERE id=%s",
                    (outcome['node_key'], outcome['due_at'].strftime('%Y-%m-%d %H:%M:%S'), email_id, row['run_id'])
                )
            elif outcome['status'] == 'unsubscribed':
                admin_cursor.execute(
                    "UPDATE automation_runs SET status='unsubscribed', last_sent_at=NOW(), last_email_id=%s WHERE id=%s",
                    (email_id, row['run_id'])
                )
            else:
                admin_cursor.execute(
                    "UPDATE automation_runs SET status='completed', last_sent_at=NOW(), last_email_id=%s WHERE id=%s",
                    (email_id, row['run_id'])
                )
            admin_conn.commit()
            sent += 1
        else:
            admin_cursor.execute(
                "INSERT INTO drip_send_log (run_key, email_id, node_key, status, error) VALUES (%s,%s,%s,'failed',%s)",
                (row['run_id'], email_id, node['node_key'], str(result)[:500])
            )
            admin_conn.commit()
            failed += 1

    return {'checked': len(due), 'sent': sent, 'failed': failed, 'skipped_unsubscribed': skipped_unsub,
            'skipped_suppressed': skipped_suppressed, 'skipped_blocked': skipped_blocked,
            'skipped_rate_limited': skipped_rate_limited}
