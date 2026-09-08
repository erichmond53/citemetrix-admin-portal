"""
CiteMetrix Investor Outreach — drafting + thread/message management.

Phase 1: draft + track, no automated send. See
seo-business-dev/CiteMetrix-Investor-Outreach-Tool-SPEC.md for the full design.
"""
import json
import os
import re
import requests
from datetime import datetime, timedelta

from competition import get_api_keys, CLAUDE_MODEL

# Phase 3 (SES send) — confirmed by Eric 2026-08-25: citemetrix.com is
# domain-verified in SES (not just admin@), so eric@citemetrix.com is a
# valid from-address. Physical address required on commercial email (CAN-SPAM).
SES_FROM_ADDRESS = 'eric@citemetrix.com'
SES_CONFIGURATION_SET = 'citemetrix-tracking'
CITEMETRIX_MAILING_ADDRESS = 'CiteMetrix LLC, PO Box 324, Norwalk, CT 06853-0324'

# Standing deck + prospectus attached to every outreach send. Eric confirmed
# 2026-08-25: use the v6.0 set (dated same day), converted DOCX/PPTX -> PDF
# for cleaner cold-outreach rendering. Update this list (and the files in
# outreach_attachments/) when a new investor-doc version ships.
OUTREACH_ATTACHMENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'outreach_attachments')
STANDARD_ATTACHMENTS = ['CiteMetrix-Pitch-Deck-v6_0.pdf', 'CiteMetrix-Investor-Prospectus-v6_0.pdf']

# Pace guardrail (spec §7): investor outreach is not a campaign — cap real
# sends per rolling 24h (across SES + Gmail combined) so an approval spree
# can't turn into a blast.
DAILY_SEND_CAP = 15

# ---------------------------------------------------------------------------
# Canonical CiteMetrix fact block — the ONLY source of metrics for drafted
# emails. Update here when numbers change; nowhere else.
#
# Pulled live from production 2026-08-24/25 and reconciled against the deck/
# prospectus drafts. Two corrections from the first draft of this block:
#   - Dropped "14 domains across three verticals" — most of those 14 are
#     Eric's own agency/demo properties, not paying customers in distinct
#     verticals. Leads with the two real relationships instead (Woven pilot,
#     Sripath) rather than implying diversified traction from a raw domain
#     count.
#   - Notion is not a customer — it's a demo/showcase domain — and is
#     therefore excluded entirely from this fact block.
# ---------------------------------------------------------------------------
CITEMETRIX_FACT_BLOCK = """
CITEMETRIX — VERIFIED FACTS (as of 2026-08-25; use ONLY these numbers)

Positioning: AI search-visibility platform — measures and fixes how AI engines describe brands.
Closed loop: monitor -> detect -> diagnose -> fix -> verify. Most competitors stop at monitoring.

Nine AI engines monitored (ChatGPT, Claude, Perplexity, Gemini, Grok, Google AI Overview, Copilot,
DeepSeek, Mistral). ModelScore(TM) composite: Mention 45% / Brand Demand 20% / Authority Transfer 20% /
Technical Readiness 15%.

Traction (cumulative unless noted):
- 31,614 AI citations logged, 18,095 brand-cited
- 581 scan runs completed
- 1,302 ModelScores computed
- 224 verified brand facts
- 264 active monitored queries
- 69 active competitors tracked
- 570 releases shipped; current version v4.46.0

Real relationships (do not inflate beyond this):
- Anchor: a healthcare-marketing agency client (consulting-practice relationship) is piloting CiteMetrix
  across 7 pharma brands, going to 9.
- A specialty-materials manufacturer is a second real monitored relationship, independent of the
  agency pilot.
- Do not cite domain counts, "verticals," or account counts beyond what's stated above — most other
  monitored domains are internal test/demo properties, not customer traction.

Regulatory proof point: on one monitored pharma brand, 636 critical open accuracy records are tied to
a single verified fact (an FDA-approved pediatric age indication) that four different AI platforms
independently misstate. This is real, live, and pullable on demand — not illustrative.

Company: built solo, no outside capital to date. Raising $1.75M at $5M pre-money (pre-seed).

Tone guardrail: pre-revenue. Frame as distribution risk, not product risk. Never imply paying
customers or revenue that doesn't exist.
""".strip()


DRAFT_SYSTEM_PROMPT = """You are drafting a one-to-one pre-seed fundraising outreach email from Eric \
Richmond, founder/CEO of CiteMetrix, to a specific named investor or firm. The goal is to earn a \
20-minute conversation — not to close a round in one email.

Rules:
- Ground every specific in the per-firm research you're given. Reference the actual target person by \
name, the actual relevant portfolio companies, the actual thesis fit. A generic email is a failed email.
- Use ONLY the CiteMetrix facts in the fact block you're given. Do not state any metric, number, or \
claim that isn't in that block. If a firm's fit would be better served by a metric not in the block, \
stay general rather than invent one.
- Short: 120-180 words. Founder-to-investor register — direct, specific, no marketing gloss, no hype \
adjectives ("revolutionary," "game-changing," etc.).
- Exactly one clear ask (a short call). One line on why-them-specifically. One line of traction proof \
from the fact block.
- Do not describe attachments in the body; just note that a deck/prospectus are attached if relevant.
- Never fabricate a mutual connection.
- Subject line: specific, lowercase-ish, non-spammy, no "!", no "opportunity of a lifetime." Write it \
like a peer would. (LinkedIn/InMail sends don't need a subject line the same way — still fill it in as \
a short first-line-equivalent hook, the UI shows it above the body either way.)

CHANNEL determines who the draft is addressed to and its register — a CHANNEL line is given in the brief:
- CHANNEL=email: a direct cold email to the target person at the firm.
- CHANNEL=linkedin: a direct message/InMail to the target person THEMSELVES — not a forward-to-a-connector \
note. Eric is reaching out to them directly via LinkedIn (InMail can reach people with no mutual \
connection), even when the research found no warm-intro path. Address them by name, shorter than an \
email (80-130 words), same one-ask/one-why-them/one-proof-point structure, no forwarding language \
("hoping you can help", "would you mind passing this along") — this reads exactly like a direct \
LinkedIn message, first-person to the recipient.
- CHANNEL=warm_intro: the research found a specific person Eric could ask for an introduction through. \
Draft a short, forwardable intro-request addressed to that connector (not the investor), that Eric can \
send/forward to ask for the intro. Make clear who the ask is for and why, in a couple sentences the \
connector could paste straight into an email to the investor.
- CHANNEL=pitch_page: draft text meant to be pasted into the firm's own pitch-submission form — no \
greeting/signoff needed, just the substantive pitch content a form field would expect.

Your entire response must be a single JSON object and nothing else: {"subject": "...", "body": "..."}
Do not include markdown code fences. Do not include any explanation, preamble, or commentary before or
after the JSON — not even a one-line note about the approach you took. The first character of your
response must be "{" and the last character must be "}".
"""


def _pick_default_channel(contact_json: dict) -> str:
    """Best-effort default channel from the free-text notes in contact_json.
    Eric can always override in the UI (channel picker in the outreach
    panel) — this just avoids defaulting cold when the research explicitly
    recommends warm.

    NOTE: this is a heuristic over free-text research notes, not a reliable
    classifier — the research write-up style consistently produces phrasing
    like "No dedicated pitch submission form found. Best outreach path is
    direct email to X." A naive substring check on "pitch submission" reads
    that as a pitch-page recommendation when it's actually a direct-email
    recommendation with a negation in front of it. Caught on Laconia and
    Contour, both real firms with this exact phrasing — treat this as a
    likely-recurring pattern, not two isolated typos.
    """
    notes = (contact_json.get('notes') or '').lower()
    pitch_url = (contact_json.get('pitch_url') or '').strip()
    email = (contact_json.get('email') or '').strip()
    linkedin = (contact_json.get('linkedin') or '').strip()

    if 'warm intro' in notes or 'warm introduction' in notes:
        return 'warm_intro'

    no_pitch_form = any(neg in notes for neg in [
        'no dedicated pitch', 'no public pitch', 'no pitch submission',
        'no pitch form', "doesn't have a pitch", 'no publicly documented',
    ])
    direct_email_recommended = 'direct email' in notes or 'best outreach path is' in notes

    if email and (direct_email_recommended or no_pitch_form):
        return 'email'
    if 'no publicly documented' in notes and 'pitch' in notes and not email:
        return 'linkedin' if linkedin else 'email'
    if pitch_url and 'pitch submission' in notes and not no_pitch_form:
        return 'pitch_page'
    if email:
        return 'email'
    if linkedin:
        return 'linkedin'
    return 'email'


def build_drafting_brief(investor: dict, channel: str = 'email') -> str:
    """Assemble the per-firm brief handed to Claude alongside the fact block."""
    contact = {}
    try:
        contact = json.loads(investor.get('contact_json') or '{}')
    except Exception:
        contact = {}
    portfolio = []
    try:
        portfolio = json.loads(investor.get('portfolio_json') or '[]')
    except Exception:
        portfolio = []

    portfolio_lines = []
    for p in portfolio[:3]:
        if isinstance(p, dict):
            name = p.get('name') or p.get('company') or ''
            why = p.get('why') or p.get('rationale') or ''
            portfolio_lines.append(f"- {name}: {why}" if why else f"- {name}")
        elif isinstance(p, str):
            portfolio_lines.append(f"- {p}")

    brief = f"""CHANNEL: {channel}
FIRM: {investor.get('firm_name')}
FIT: {investor.get('fit_label')} ({investor.get('fit_score')}/100)
WHY THIS FIT: {investor.get('fit_rationale') or ''}
THESIS: {investor.get('thesis') or ''}
STAGE / CHECK SIZE: {investor.get('stage_focus') or ''} / {investor.get('check_size') or ''}
KEY PARTNERS: {investor.get('key_partners') or ''}
RELEVANT PORTFOLIO:
{chr(10).join(portfolio_lines) if portfolio_lines else '(none listed)'}
OUTREACH NOTES (channel/framing guidance from prior research): {contact.get('notes') or '(none)'}
"""
    return brief


def draft_outreach_email(cursor, investor: dict, channel: str = 'email') -> dict:
    """Call Claude to draft subject+body for this investor. `channel` is the
    outreach_thread's current channel (email/linkedin/warm_intro/pitch_page)
    — it changes who the draft is addressed to and its register (see
    DRAFT_SYSTEM_PROMPT). Returns {'subject':..., 'body':...} or {'error':...}.
    Synchronous — this is an interactive drafting call, NOT the batch
    pipeline; do not route this through the batch API."""
    keys = get_api_keys(cursor)
    if not keys.get('anthropic'):
        return {'error': 'Anthropic API key not configured. Add it in Settings.'}

    brief = build_drafting_brief(investor, channel)
    prompt = f"{CITEMETRIX_FACT_BLOCK}\n\n---\n\n{brief}\n\nDraft the outreach email now."

    try:
        response = requests.post(
            'https://api.anthropic.com/v1/messages',
            headers={'x-api-key': keys['anthropic'], 'anthropic-version': '2023-06-01', 'Content-Type': 'application/json'},
            json={
                'model': CLAUDE_MODEL,
                'max_tokens': 1000,
                'system': DRAFT_SYSTEM_PROMPT,
                'messages': [{'role': 'user', 'content': prompt}],
            },
            timeout=60,
        )
    except Exception as e:
        return {'error': f'Claude request failed: {e}'}

    if response.status_code != 200:
        try:
            msg = response.json().get('error', {}).get('message', f'HTTP {response.status_code}')
        except Exception:
            msg = f'Claude HTTP {response.status_code}'
        return {'error': f'Claude error: {msg}'}

    text = response.json().get('content', [{}])[0].get('text', '')
    # Claude sometimes adds a line of preamble despite instructions not to
    # (e.g. explaining a warm_intro framing choice before the JSON object).
    # Extract the outermost {...} span rather than trusting the response to
    # start/end exactly at the braces.
    start, end = text.find('{'), text.rfind('}')
    json_slice = text[start:end + 1] if start != -1 and end != -1 and end > start else text
    json_slice = re.sub(r'[\x00-\x1f]', ' ', json_slice)
    try:
        parsed = json.loads(json_slice)
    except Exception as e:
        return {'error': f'Could not parse Claude output as JSON: {e}', 'raw': text[:500]}

    if not parsed.get('subject') or not parsed.get('body'):
        return {'error': 'Claude output missing subject or body', 'raw': text[:500]}

    return {'subject': parsed['subject'], 'body': parsed['body']}


def get_or_create_thread(admin_cursor, admin_conn, investor: dict, owner_user_id: int) -> int:
    """Return the active outreach_thread id for this investor, creating one
    if none exists yet."""
    admin_cursor.execute(
        "SELECT id FROM outreach_thread WHERE investor_id=%s ORDER BY id DESC LIMIT 1",
        (investor['id'],)
    )
    row = admin_cursor.fetchone()
    if row:
        return row['id']

    contact = {}
    try:
        contact = json.loads(investor.get('contact_json') or '{}')
    except Exception:
        contact = {}

    channel = _pick_default_channel(contact)
    target_email = contact.get('email') or None

    admin_cursor.execute(
        """INSERT INTO outreach_thread
           (investor_id, firm_name, target_person, target_email, channel, status, owner_user_id)
           VALUES (%s, %s, %s, %s, %s, 'not_started', %s)""",
        (investor['id'], investor['firm_name'], None, target_email, channel, owner_user_id)
    )
    admin_conn.commit()
    return admin_cursor.lastrowid


def log_event(admin_cursor, admin_conn, thread_id: int, event_type: str, detail: str = None, message_id: int = None):
    admin_cursor.execute(
        "INSERT INTO outreach_event (thread_id, message_id, event_type, detail) VALUES (%s, %s, %s, %s)",
        (thread_id, message_id, event_type, detail)
    )
    admin_conn.commit()


def get_standard_attachments() -> list:
    """Absolute paths to the standard deck+prospectus attachments, filtered
    to files that actually exist so a missing/renamed file degrades to
    "send without that attachment" instead of crashing the send."""
    paths = [os.path.join(OUTREACH_ATTACHMENTS_DIR, f) for f in STANDARD_ATTACHMENTS]
    return [p for p in paths if os.path.isfile(p)]


def build_send_body(message_body: str) -> str:
    """Append the CAN-SPAM physical-address line as a genuine signoff, not a
    marketing-style unsubscribe footer — spec explicitly wants this to still
    read as a real 1:1 email."""
    return f"{message_body}\n\n—\nEric Richmond\n{CITEMETRIX_MAILING_ADDRESS}"


def next_business_day_offset(start: datetime, business_days: int) -> datetime:
    """Add N business days (Mon-Fri) to start, skipping weekends."""
    d = start
    added = 0
    while added < business_days:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0=Mon .. 4=Fri
            added += 1
    return d


def real_sends_last_24h(admin_cursor) -> int:
    """Counts real automated sends across BOTH channels (SES + Gmail) — the
    pace guardrail is about overall pace, not a per-tool limit."""
    admin_cursor.execute(
        "SELECT COUNT(*) AS n FROM outreach_message WHERE send_channel IN ('ses','gmail') AND sent_at >= NOW() - INTERVAL 24 HOUR"
    )
    return admin_cursor.fetchone()['n']
