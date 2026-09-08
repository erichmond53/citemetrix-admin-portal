#!/usr/bin/env python3
"""
Daily blacklist / DNSBL monitor for the admin-portal Deliverability dashboard.

Checks the sending domains against domain blocklists and the configured IPs
against IP blocklists, writes reports/blacklist/blacklist-latest.json, and
emails Eric if anything is newly LISTED.

Spamhaus public zones refuse queries from cloud resolvers (they answer
127.255.255.254). Set SPAMHAUS_DQS_KEY in the env to use Spamhaus's free Data
Query Service, which gives real answers from AWS. Without it, Spamhaus rows are
reported as "unavailable (needs DQS key)" rather than a false "clean".

No third-party deps — uses stdlib socket for the DNSBL A-record lookups.
"""
import socket
import os
import json
import sys
from datetime import datetime, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, '.env'))
except Exception:
    pass
OUT_DIR = os.path.join(BASE, 'reports', 'blacklist')
OUT = os.path.join(OUT_DIR, 'blacklist-latest.json')

# What we send from. Domains drive most sender-reputation listings on SES.
DOMAINS = ['citemetrix.com', 'expertseoconsulting.com', 'standonitmarketing.com']
# SES sends from shared SES IPs (not enumerable). The Sendy box IP is monitored
# for completeness; add a dedicated SES IP here if one is ever provisioned.
IPS = ['34.196.159.231']

DQS = os.getenv('SPAMHAUS_DQS_KEY', '').strip()

# Domain blocklists: (label, public_zone, dqs_zone_or_None)
DOMAIN_BLS = [
    ('Spamhaus DBL', 'dbl.spamhaus.org', 'dbl.dq.spamhaus.net'),
    ('SURBL',        'multi.surbl.org',  None),
    ('URIBL',        'multi.uribl.com',  None),
]
IP_BLS = [
    ('Spamhaus ZEN', 'zen.spamhaus.org',        'zen.dq.spamhaus.net'),
    ('Barracuda',    'b.barracudacentral.org',  None),
    ('SpamCop',      'bl.spamcop.net',          None),
]

socket.setdefaulttimeout(6)


def resolve(qname):
    try:
        return socket.gethostbyname(qname)
    except socket.gaierror:
        return None            # NXDOMAIN -> not listed
    except Exception as e:
        return 'ERR:' + type(e).__name__


def classify(res):
    if res is None:
        return ('clean', None)
    if isinstance(res, str) and res.startswith('ERR:'):
        return ('error', res)
    if isinstance(res, str) and res.startswith('127.255.255'):
        return ('unavailable', res)   # resolver blocked / DQS required
    if res == '127.0.0.1':
        return ('unavailable', res + ' (query refused / resolver blocked - not a listing)')
    if isinstance(res, str) and res.startswith('127.'):
        return ('listed', res)        # LISTED — 127.0.0.x code identifies the list
    return ('unknown', str(res))


def domain_query(domain, public_zone, dqs_zone):
    if DQS and dqs_zone:
        return f'{domain}.{DQS}.{dqs_zone}'
    return f'{domain}.{public_zone}'


def ip_query(ip, public_zone, dqs_zone):
    rev = '.'.join(reversed(ip.split('.')))
    if DQS and dqs_zone:
        return f'{rev}.{DQS}.{dqs_zone}'
    return f'{rev}.{public_zone}'


def run():
    targets = []
    listed = []
    for d in DOMAINS:
        rows = []
        for label, pub, dqs in DOMAIN_BLS:
            status, code = classify(resolve(domain_query(d, pub, dqs)))
            rows.append({'list': label, 'status': status, 'code': code})
            if status == 'listed':
                listed.append({'target': d, 'type': 'domain', 'list': label, 'code': code})
        targets.append({'target': d, 'type': 'domain', 'lists': rows})
    for ip in IPS:
        rows = []
        for label, pub, dqs in IP_BLS:
            status, code = classify(resolve(ip_query(ip, pub, dqs)))
            rows.append({'list': label, 'status': status, 'code': code})
            if status == 'listed':
                listed.append({'target': ip, 'type': 'ip', 'list': label, 'code': code})
        targets.append({'target': ip, 'type': 'ip', 'lists': rows})

    any_unavailable = any(r['status'] == 'unavailable' for t in targets for r in t['lists'])
    out = {
        'generated': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC'),
        'dqs_enabled': bool(DQS),
        'targets': targets,
        'listed': listed,
        'listed_count': len(listed),
        'any_listed': bool(listed),
        'any_unavailable': any_unavailable,
    }

    # Alert on a NEW listing (compare to previous snapshot).
    prev_listed = set()
    try:
        prev = json.load(open(OUT))
        prev_listed = {(x['target'], x['list']) for x in prev.get('listed', [])}
    except Exception:
        pass
    new_listings = [x for x in listed if (x['target'], x['list']) not in prev_listed]

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2)

    if new_listings:
        try:
            sys.path.insert(0, BASE)
            from email_helper import send_email
            lines = "\n".join(f"  - {x['target']} on {x['list']} ({x['code']})" for x in new_listings)
            send_email(
                'eric@expertseoconsulting.com',
                'ALERT: new email blacklist listing detected',
                "The daily blacklist monitor found a NEW listing:\n\n" + lines +
                "\n\nDo not resume sends until this clears. — admin portal blacklist monitor",
            )
        except Exception as e:
            print('alert email failed:', e)

    print(f"blacklist: targets={len(targets)} listed={len(listed)} "
          f"unavailable={'yes' if any_unavailable else 'no'} dqs={'on' if DQS else 'off'}")
    return out


if __name__ == '__main__':
    run()
