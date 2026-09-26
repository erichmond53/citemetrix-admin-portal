"""
Email sending helper using AWS SES.

Wraps boto3.send_email() with sensible defaults and a clean function
signature. Designed to be reusable across team invitations, password
resets, customer notifications, and anywhere else the admin portal
needs to send transactional email.

Configuration is read from environment variables (loaded via .env in
app.py before this module gets called):

    AWS_ACCESS_KEY_ID       SES IAM user's key
    AWS_SECRET_ACCESS_KEY   SES IAM user's secret
    AWS_REGION              SES region (us-east-1 — see memory #19)
    SES_FROM_EMAIL          Verified sender address (noreply@citemetrix.com)
"""
import os
import logging
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from typing import Optional, List
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _get_ses_client():
    """
    Create a fresh SES client per call.

    boto3 clients are not threadsafe in all configurations and Flask
    serves multiple workers. Creating a new client on each send is
    slightly wasteful but eliminates an entire class of bug. Volume
    is too low for the overhead to matter (transactional only).
    """
    return boto3.client('ses', region_name=os.getenv('AWS_REGION'))


def send_email(
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[List[str]] = None,
    from_address: Optional[str] = None,
    configuration_set: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    list_unsubscribe: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """
    Send an email via AWS SES.

    Args:
        to:        Recipient email address. Single address only — for
                   bulk sends, call this multiple times.
        subject:   Email subject line.
        body_text: Plain text body. Always required (fallback for
                   clients that don't render HTML, plus higher
                   deliverability).
        body_html: Optional HTML body. When provided, both text and
                   HTML versions are sent as a multipart message.
        reply_to:  Optional Reply-To address. Useful for transactional
                   emails where the sender is noreply@ but replies
                   should go somewhere monitored.
        cc:        Optional list of CC addresses.
        from_address: Optional sender override. Defaults to
                   SES_FROM_EMAIL when omitted — pass this for sends
                   that need a different, SES-verified from address
                   (e.g. eric@citemetrix.com for outreach).
        configuration_set: Optional SES Configuration Set name to attach
                   for event tracking (opens/clicks/bounces via SNS).
                   Omit for transactional sends that don't need tracking.
        attachments: Optional list of absolute file paths to attach.
                   Switches to SES's raw-MIME send path (send_raw_email) --
                   the simple send_email API has no attachment support.
        list_unsubscribe: Optional recipient-specific HTTPS unsubscribe URL
                   (e.g. https://citemetrix.com/unsubscribe/<token>). When set,
                   also switches to the raw-MIME send path, since List-Unsubscribe/
                   List-Unsubscribe-Post are real MIME headers the simple
                   send_email API can't set. Sent alongside the existing mailto
                   fallback so mail clients (Gmail etc.) render a native one-click
                   Unsubscribe button next to the sender -- 2026-09-25 deliverability
                   fix: without the https URL + List-Unsubscribe-Post pair, clients
                   won't show that button, and a bored recipient's next-easiest
                   option is Report Spam instead, which is far more damaging to
                   sender reputation than an unsubscribe. See app.py's
                   /unsubscribe/<token> route (POST branch) for the RFC 8058
                   one-click receiving end.

    Returns:
        (success, message_id_or_error)
        success is True on send, False on failure.
        On success, second value is the SES MessageId.
        On failure, second value is the error code or message.
    """
    from_address = from_address or os.getenv('SES_FROM_EMAIL')
    if not from_address:
        logger.error("SES_FROM_EMAIL not configured; cannot send email")
        return False, "SES_FROM_EMAIL not configured"

    if attachments or list_unsubscribe:
        return _send_raw_with_attachments(
            to=to, subject=subject, body_text=body_text, body_html=body_html,
            reply_to=reply_to, cc=cc, from_address=from_address,
            configuration_set=configuration_set, attachments=attachments or [],
            list_unsubscribe=list_unsubscribe,
        )

    # Build the message body — always include text, optionally include
    # HTML alongside it so the recipient's client picks the better one.
    body: dict = {'Text': {'Data': body_text, 'Charset': 'UTF-8'}}
    if body_html:
        body['Html'] = {'Data': body_html, 'Charset': 'UTF-8'}

    destination: dict = {'ToAddresses': [to]}
    if cc:
        destination['CcAddresses'] = cc

    kwargs = {
        'Source':      from_address,
        'Destination': destination,
        'Message': {
            'Subject': {'Data': subject, 'Charset': 'UTF-8'},
            'Body':    body,
        },
    }
    if reply_to:
        kwargs['ReplyToAddresses'] = [reply_to]
    if configuration_set:
        kwargs['ConfigurationSetName'] = configuration_set

    try:
        response = _get_ses_client().send_email(**kwargs)
        message_id = response.get('MessageId', '')
        logger.info(f"SES send OK to={to} subject={subject!r} msg_id={message_id}")
        return True, message_id

    except ClientError as e:
        # SES errors come back with a structured Error dict. Surface the
        # code so the caller can distinguish "MessageRejected" (bad
        # address, sandbox) from "Throttling" (rate limit) and react
        # accordingly. For now the caller just logs.
        code   = e.response.get('Error', {}).get('Code', 'UnknownError')
        detail = e.response.get('Error', {}).get('Message', str(e))
        logger.error(f"SES send FAILED to={to} subject={subject!r} code={code} detail={detail}")
        return False, code

    except Exception as e:
        # Catchall for non-ClientError exceptions (network, credential
        # errors, etc). Don't let an email send crash the calling route.
        logger.exception(f"SES send unexpected failure to={to}")
        return False, str(e)


def _send_raw_with_attachments(
    to: str,
    subject: str,
    body_text: str,
    body_html: Optional[str],
    reply_to: Optional[str],
    cc: Optional[List[str]],
    from_address: str,
    configuration_set: Optional[str],
    attachments: List[str],
    list_unsubscribe: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """SES has no attachment support in its simple send_email API — this
    builds a raw MIME message and sends it via send_raw_email instead. Also the
    path used whenever list_unsubscribe is set (attachments or not), since
    List-Unsubscribe/List-Unsubscribe-Post are real MIME headers the simple
    send_email API can't set."""
    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From'] = from_address
    msg['To'] = to
    if reply_to:
        msg['Reply-To'] = reply_to
    if cc:
        msg['Cc'] = ', '.join(cc)
    if list_unsubscribe:
        # RFC 8058 one-click: BOTH headers required together, or mail clients
        # (Gmail etc.) won't render the native Unsubscribe button at all.
        # mailto kept as a fallback for clients that only understand the older
        # RFC 2369 form.
        msg['List-Unsubscribe'] = (
            f"<{list_unsubscribe}>, "
            f"<mailto:info@citemetrix.com?subject=Unsubscribe%20AI%20Visibility%20Report>"
        )
        msg['List-Unsubscribe-Post'] = 'List-Unsubscribe=One-Click'

    body_part = MIMEMultipart('alternative')
    body_part.attach(MIMEText(body_text, 'plain', 'UTF-8'))
    if body_html:
        body_part.attach(MIMEText(body_html, 'html', 'UTF-8'))
    msg.attach(body_part)

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

    destinations = [to] + (cc or [])
    kwargs = {
        'Source': from_address,
        'Destinations': destinations,
        'RawMessage': {'Data': msg.as_bytes()},
    }
    if configuration_set:
        kwargs['ConfigurationSetName'] = configuration_set

    try:
        response = _get_ses_client().send_raw_email(**kwargs)
        message_id = response.get('MessageId', '')
        logger.info(f"SES raw send OK to={to} subject={subject!r} msg_id={message_id} attachments={len(attachments)}")
        return True, message_id
    except ClientError as e:
        code = e.response.get('Error', {}).get('Code', 'UnknownError')
        logger.error(f"SES raw send FAILED to={to} subject={subject!r} code={code}")
        return False, code
    except Exception as e:
        logger.exception(f"SES raw send unexpected failure to={to}")
        return False, str(e)
