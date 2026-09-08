"""
Gmail API send helper for CiteMetrix Outreach (Phase 2).

Sends as a genuine one-to-one email from eric@citemetrix.com via the Gmail
API (gmail.send scope only — no inbox read access). Credentials come from
a one-time OAuth consent (see gmail_outreach_consent.py in seo-business-dev)
whose refresh token lives in this server's .env, never in code or git.
"""
import os
import base64
import logging
import mimetypes
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from typing import Optional, List

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ['https://www.googleapis.com/auth/gmail.send']
GMAIL_FROM_ADDRESS = 'eric@citemetrix.com'


def gmail_configured() -> bool:
    return bool(os.getenv('GMAIL_CLIENT_ID') and os.getenv('GMAIL_CLIENT_SECRET') and os.getenv('GMAIL_REFRESH_TOKEN'))


def _get_service():
    creds = Credentials(
        None,
        refresh_token=os.getenv('GMAIL_REFRESH_TOKEN'),
        client_id=os.getenv('GMAIL_CLIENT_ID'),
        client_secret=os.getenv('GMAIL_CLIENT_SECRET'),
        token_uri='https://oauth2.googleapis.com/token',
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return build('gmail', 'v1', credentials=creds, cache_discovery=False)


def send_gmail(to: str, subject: str, body_text: str, thread_id: Optional[str] = None, attachments: Optional[List[str]] = None) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Send a plain-text email via the Gmail API as eric@citemetrix.com.

    attachments: optional list of absolute file paths to attach.

    Returns (success, message_id_or_error, gmail_thread_id).
    On success: message_id_or_error is the Gmail message id, gmail_thread_id
    is the Gmail thread id (store it for follow-up threading).
    On failure: message_id_or_error is the error string, gmail_thread_id is None.
    """
    if not gmail_configured():
        return False, 'Gmail not configured (missing GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN in .env)', None

    try:
        service = _get_service()

        if attachments:
            msg = MIMEMultipart('mixed')
            msg.attach(MIMEText(body_text))
            for path in attachments:
                if not os.path.isfile(path):
                    logger.error(f"Attachment not found, skipping: {path}")
                    continue
                ctype, _ = mimetypes.guess_type(path)
                maintype, subtype = (ctype or 'application/octet-stream').split('/', 1)
                with open(path, 'rb') as f:
                    part = MIMEApplication(f.read(), _subtype=subtype)
                part.add_header('Content-Disposition', 'attachment', filename=os.path.basename(path))
                msg.attach(part)
        else:
            msg = MIMEText(body_text)

        msg['to'] = to
        msg['from'] = GMAIL_FROM_ADDRESS
        msg['subject'] = subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')

        body = {'raw': raw}
        if thread_id:
            body['threadId'] = thread_id

        sent = service.users().messages().send(userId='me', body=body).execute()
        message_id = sent.get('id')
        gmail_thread_id = sent.get('threadId')
        logger.info(f"Gmail send OK to={to} subject={subject!r} msg_id={message_id}")
        return True, message_id, gmail_thread_id

    except Exception as e:
        logger.exception(f"Gmail send FAILED to={to}")
        return False, str(e), None
