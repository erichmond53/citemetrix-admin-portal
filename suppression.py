"""SES suppression check -- the single shared implementation (step-1-brief.md
SS8.2: "one implementation of the gate, not several"; SS9.3: kept in a module
that outlives any one intake path, rather than leads_drip.py, which retires
alongside drip_leads_legacy at step 3).

Every caller of the suppression gate -- the drip-leads import screen,
enroll_batch(), process_due_enrollments() today, and the unified pool's
sequence engine at step 3 -- imports this rather than re-implementing it.
Evaluated live against admin_portal.ses_suppressions on every call rather
than denormalized onto a lead row, so it can never go stale (step-1-brief.md
SS9: "the right call... keep it that way at step 3 rather than 'optimizing'
it into a stored column").
"""


def is_suppressed(cursor, email: str) -> bool:
    cursor.execute("SELECT 1 FROM ses_suppressions WHERE email_address=%s", (email,))
    return cursor.fetchone() is not None
