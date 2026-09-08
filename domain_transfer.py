"""Domain Transfer — move a single domain (and its per-domain, user-scoped data) from one
CiteMetrix account to another. Native portal implementation (replaces the never-tested WP tool).

Design decisions (verified against the live schema + confirmed by Eric 2026-07-28):
  * The transfer set is DERIVED AT RUNTIME: every table carrying BOTH user_id and domain_id
    (future-proof — auto-includes new tables). The old PHP hard-coded 3 of 15.
  * Domain-scoped tables (domain_id only: scores/citations/keywords/competitors/...) follow the
    domain automatically — no change.
  * Account-level tables (user_id, no domain_id) are never touched.
  * api_usage: user_id is re-pointed (visibility); billed_user_id is LEFT (historical billing fact).
  * team_domain_access for the domain is REVOKED (deleted) — the source org loses access.
  * Domain-scoped user_meta is moved; the coaching card is reset (fresh first-scan for new owner).
  * EVERYTHING runs in ONE transaction — atomic; any error rolls the whole thing back.
"""

META_KEYS = lambda did: [
    'citemetrix_alert_email_%d' % did,
    'citemetrix_cms_settings_%d' % did,
    'citemetrix_first_scan_email_sent_%d' % did,
    'cm_last_report_%d_visibility' % did,
    'cm_last_report_%d_progress' % did,
    'cm_last_report_%d_competitive' % did,
    'cm_last_report_%d_content' % did,
]
COACHING_KEY = lambda did: 'citemetrix_coaching_card_dismissed_%d' % did


def transfer_tables(conn, dbname):
    """All tables with BOTH user_id and domain_id (the runtime-derived transfer set)."""
    with conn.cursor() as c:
        c.execute(
            "SELECT TABLE_NAME t FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND COLUMN_NAME IN ('user_id','domain_id') "
            "GROUP BY TABLE_NAME HAVING SUM(COLUMN_NAME='user_id')>0 AND SUM(COLUMN_NAME='domain_id')>0 "
            "ORDER BY TABLE_NAME", (dbname,))
        return [r['t'] for r in c.fetchall()]


def get_domain(conn, domain_id):
    with conn.cursor() as c:
        c.execute("SELECT id, user_id, domain, brand_name FROM wp_citemetrix_domains WHERE id=%s", (domain_id,))
        return c.fetchone()


def get_user(conn, user_id):
    with conn.cursor() as c:
        c.execute("SELECT ID AS id, user_email, display_name FROM wp_users WHERE ID=%s", (user_id,))
        return c.fetchone()


def build_preview(conn, dbname, domain_id, dest_user_id=None):
    """Read-only: exactly what a transfer WOULD move. No writes."""
    domain = get_domain(conn, domain_id)
    if not domain:
        return {'error': 'Domain not found'}
    src = int(domain['user_id'])
    tables = transfer_tables(conn, dbname)
    plan = []
    with conn.cursor() as c:
        for t in tables:
            c.execute("SELECT COUNT(*) n FROM `%s` WHERE domain_id=%%s AND user_id=%%s" % t, (domain_id, src))
            plan.append({'table': t, 'rows': int(c.fetchone()['n']),
                         'note': 'user_id only — billing preserved' if t == 'wp_citemetrix_api_usage' else ''})
        c.execute("SELECT COUNT(*) n FROM wp_citemetrix_team_domain_access WHERE domain_id=%s", (domain_id,))
        team_revoke = int(c.fetchone()['n'])
        meta_present = []
        for k in META_KEYS(domain_id):
            c.execute("SELECT COUNT(*) n FROM wp_usermeta WHERE user_id=%s AND meta_key=%s", (src, k))
            if int(c.fetchone()['n']):
                meta_present.append(k)
    dest = get_user(conn, dest_user_id) if dest_user_id else None
    return {'domain': domain, 'source_user': get_user(conn, src), 'source_user_id': src, 'dest': dest,
            'tables': plan, 'total_rows': sum(p['rows'] for p in plan),
            'team_revoke': team_revoke, 'meta_keys': meta_present}


def execute_transfer(conn, dbname, domain_id, dest_user_id):
    """Atomic transfer. Returns {'ok':bool, ...counts / error}. Rolls back fully on any error."""
    dest_user_id = int(dest_user_id)
    domain = get_domain(conn, domain_id)
    if not domain:
        return {'ok': False, 'error': 'Domain not found'}
    src = int(domain['user_id'])
    if src == dest_user_id:
        return {'ok': False, 'error': 'Source and destination are the same account'}
    if not get_user(conn, dest_user_id):
        return {'ok': False, 'error': 'Destination user not found'}
    tables = transfer_tables(conn, dbname)
    counts = {}
    try:
        conn.begin()
        with conn.cursor() as c:
            for t in tables:
                c.execute("UPDATE `%s` SET user_id=%%s WHERE domain_id=%%s AND user_id=%%s" % t,
                          (dest_user_id, domain_id, src))
                counts[t] = c.rowcount
            c.execute("DELETE FROM wp_citemetrix_team_domain_access WHERE domain_id=%s", (domain_id,))
            counts['team_domain_access_revoked'] = c.rowcount
            c.execute("UPDATE wp_citemetrix_domains SET user_id=%s WHERE id=%s", (dest_user_id, domain_id))
            counts['domain_reparented'] = c.rowcount
            c.execute("UPDATE wp_citemetrix_scan_jobs SET status='cancelled' "
                      "WHERE domain_id=%s AND status IN ('pending','running')", (domain_id,))
            counts['scan_jobs_cancelled'] = c.rowcount
            # domain-scoped user_meta -> re-point to dest (delete dest's existing key first to avoid dupes)
            meta_moved = []
            for k in META_KEYS(domain_id):
                c.execute("DELETE FROM wp_usermeta WHERE user_id=%s AND meta_key=%s", (dest_user_id, k))
                c.execute("UPDATE wp_usermeta SET user_id=%s WHERE user_id=%s AND meta_key=%s", (dest_user_id, src, k))
                if c.rowcount:
                    meta_moved.append(k)
            c.execute("DELETE FROM wp_usermeta WHERE user_id=%s AND meta_key=%s", (src, COACHING_KEY(domain_id)))
            counts['meta_moved'] = meta_moved
        conn.commit()
    except Exception as e:
        conn.rollback()
        return {'ok': False, 'error': 'Transfer failed, rolled back: %s' % e, 'counts': counts}
    return {'ok': True, 'domain': domain['domain'], 'domain_id': domain_id,
            'source_user_id': src, 'dest_user_id': dest_user_id, 'counts': counts}
