"""
NeverBounce integration — bulk email verification for CSV-imported cold
leads (Marblism etc.). Not used for free-check leads (self-submitted,
already a real deliverable address by construction).

step1brief.md-adjacent (2026-09-03): the Jul 23 Marblism send to Sendy
lists 78/79 got blocked by several enterprise mail gateways -- confirmed
via NeverBounce at the time to be a real mix of dead addresses (~35% of
bounces) and enrichment data mismatched to the wrong company domain, not
a sender-reputation problem (SPF/DKIM/DMARC were already clean). The
standing plan from that incident: never resume cold sending to a list
without NeverBounce verification first. This module is that gate, wired
into the CSV-import pipeline instead of a one-off script.

Uses the Bulk Jobs API (v4.2), not Single Check -- a few hundred emails
per batch would be too slow to verify one at a time inside a request.
Two-phase, matching the async job pattern already used elsewhere in this
app (see jobs/traffic.py's Popen+poll refresh): submit_job() kicks off a
NeverBounce job and returns immediately; check_job() is safe to call
repeatedly (from a button or a cron) until the job completes, at which
point it fetches results and applies them to `leads` in one pass.
"""
import os
import requests

API_BASE = 'https://api.neverbounce.com/v4.2'

# result -> (leads.email_verification_status, whether to auto-suppress via
# the existing suppression gate). 'catchall'/'unknown' are NOT suppressed
# automatically -- the original diagnosis found catchall bounces at
# legitimate enterprise domains were ambiguous, not conclusively bad, so
# they're surfaced for Eric to review rather than silently dropped.
_RESULT_MAP = {
    'valid':      ('valid', False),
    'invalid':    ('invalid', True),
    'disposable': ('disposable', True),
    'catchall':   ('catchall', False),
    'unknown':    ('unknown', False),
}


def _api_key():
    key = os.environ.get('NEVERBOUNCE_API_KEY', '')
    if not key:
        raise RuntimeError('NEVERBOUNCE_API_KEY is not set in the environment.')
    return key


def _call(path, params, method='GET'):
    url = f'{API_BASE}/{path}'
    if method == 'GET':
        resp = requests.get(url, params=params, timeout=30)
    else:
        # Docs are inconsistent about whether `key` goes in the query string
        # or the JSON body for POST endpoints -- send it both ways so this
        # works regardless of which NeverBounce actually reads.
        resp = requests.post(url, params={'key': params.get('key', '')}, json=params, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f'NeverBounce API error {resp.status_code}: {resp.text}')
    return resp.json()


def submit_job(emails, filename='admin-portal-batch'):
    """emails: list of email strings. Returns the NeverBounce job_id.
    auto_parse+auto_start=True so the job runs immediately with no
    separate 'parse' step to babysit."""
    if not emails:
        raise ValueError('No emails to submit.')
    resp = _call('jobs/create', {
        'key': _api_key(),
        'input_location': 'supplied',
        'filename': filename,
        'auto_parse': True,
        'auto_start': True,
        'input': [[e] for e in emails],
    }, method='POST')
    if resp.get('status') != 'success':
        raise RuntimeError(f'NeverBounce job create failed: {resp}')
    return resp['job_id'] if 'job_id' in resp else resp.get('id')


def job_status(job_id):
    """Returns the raw status response -- job_status ('queued'/'running'/
    'complete'/'failed'/...), percent_complete, and per-result-type totals."""
    return _call('jobs/status', {'key': _api_key(), 'job_id': job_id}, method='GET')


def fetch_results(job_id, items_per_page=1000):
    """Returns {email_lowercased: result_string} for every row in the job.
    Paginates if the batch exceeds items_per_page (NeverBounce's own max)."""
    out = {}
    page = 1
    while True:
        resp = _call('jobs/results', {
            'key': _api_key(), 'job_id': job_id, 'page': page, 'items_per_page': items_per_page,
        }, method='GET')
        if resp.get('status') != 'success':
            raise RuntimeError(f'NeverBounce results fetch failed: {resp}')
        for row in resp.get('results', []):
            email = (row.get('data', {}).get('email') or '').strip().lower()
            result = row.get('verification', {}).get('result')
            if email and result:
                out[email] = result
        if page >= resp.get('total_pages', 1):
            break
        page += 1
    return out


def apply_results(cursor, results):
    """results: {email: neverbounce_result}. Updates leads.email_verification_status
    for every matching row; for 'invalid'/'disposable' results, also sets the
    existing suppression gate (suppressed_at/suppression_reason='failed_verification')
    so enroll_batch()/process_due_enrollments() exclude them automatically --
    reusing the one suppression check already wired in, not a second gate.
    Returns counts by outcome."""
    counts = {'valid': 0, 'invalid': 0, 'catchall': 0, 'disposable': 0, 'unknown': 0, 'unmatched': 0}
    for email, result in results.items():
        mapped = _RESULT_MAP.get(result)
        if not mapped:
            counts['unmatched'] += 1
            continue
        status, suppress = mapped
        counts[result] = counts.get(result, 0) + 1
        if suppress:
            cursor.execute(
                "UPDATE leads SET email_verification_status=%s, email_verified_at=NOW(), "
                "suppressed_at=COALESCE(suppressed_at, NOW()), suppression_reason=COALESCE(suppression_reason, 'failed_verification') "
                "WHERE email=%s",
                (status, email)
            )
        else:
            cursor.execute(
                "UPDATE leads SET email_verification_status=%s, email_verified_at=NOW() WHERE email=%s",
                (status, email)
            )
    return counts
