
"""

CiteMetrix Admin Portal

"""

import os

import json

import random

import secrets

from datetime import datetime, timedelta


def gmdate_utc(ts):
    """Format a unix timestamp as 'YYYY-MM-DD HH:MM UTC' for debug displays."""
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return f'<invalid ts: {ts}>'


from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response

from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

from dotenv import load_dotenv

import pymysql

import requests

import bcrypt

from email_helper import send_email



load_dotenv()



app = Flask(__name__)

app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-change-this')

app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=8)



login_manager = LoginManager()

login_manager.init_app(app)

login_manager.login_view = 'login'



def get_db():

    return pymysql.connect(

        host=os.getenv('DB_HOST'), user=os.getenv('DB_USER'),

        password=os.getenv('DB_PASSWORD'), database=os.getenv('DB_NAME'),

        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)



def get_admin_db():

    return pymysql.connect(

        host='localhost', user=os.getenv('ADMIN_DB_USER', 'adminportal'),

        password=os.getenv('ADMIN_DB_PASSWORD'), database=os.getenv('ADMIN_DB_NAME', 'admin_portal'),

        charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor)



class User(UserMixin):

    def __init__(self, id, username, email, role, name):

        self.id, self.username, self.email, self.role, self.name = id, username, email, role, name

    @staticmethod

    def get(user_id):

        try:

            conn = get_admin_db()

            with conn.cursor() as cursor:

                cursor.execute("SELECT * FROM users WHERE id = %s AND active = 1", (user_id,))

                row = cursor.fetchone()

            conn.close()

            if row:

                return User(row['id'], row['username'], row['email'], row['role'], row['name'])

        except:

            pass

        return None

    @staticmethod

    def authenticate(username, password):

        try:

            conn = get_admin_db()

            with conn.cursor() as cursor:

                cursor.execute("SELECT * FROM users WHERE username = %s AND active = 1", (username,))

                row = cursor.fetchone()

            conn.close()

            if row and bcrypt.checkpw(password.encode('utf-8'), row['password_hash'].encode('utf-8')):

                conn = get_admin_db()

                with conn.cursor() as cursor:

                    cursor.execute("UPDATE users SET last_login = NOW() WHERE id = %s", (row['id'],))

                conn.commit()

                conn.close()

                return User(row['id'], row['username'], row['email'], row['role'], row['name'])

        except:

            pass

        return None



@login_manager.user_loader

def load_user(user_id):

    return User.get(user_id)



def role_required(*roles):

    def decorator(f):

        @wraps(f)

        def decorated_function(*args, **kwargs):

            if not current_user.is_authenticated:

                return redirect(url_for('login'))

            if current_user.role not in roles:

                flash('Permission denied.', 'error')

                return redirect(url_for('dashboard'))

            return f(*args, **kwargs)

        return decorated_function

    return decorator



def parse_json_field(val):

    if val is None:

        return None

    if isinstance(val, (dict, list)):

        return val

    try:

        return json.loads(val)

    except:

        return val



@app.context_processor

def inject_now():

    return {'now': datetime.now}



@app.template_filter('timeago')

def timeago_filter(dt):

    if not dt:

        return 'Never'

    diff = datetime.now() - dt

    if diff.days > 0:

        return f"{diff.days}d ago"

    elif diff.seconds >= 3600:

        return f"{diff.seconds // 3600}h ago"

    elif diff.seconds >= 60:

        return f"{diff.seconds // 60}m ago"

    return "Just now"



@app.template_filter('number')

def number_filter(n):

    return f"{n:,}" if n else '0'



@app.route('/login', methods=['GET', 'POST'])

def login():

    if current_user.is_authenticated:

        return redirect(url_for('dashboard'))

    if request.method == 'POST':

        user = User.authenticate(request.form.get('username', '').strip(), request.form.get('password', ''))

        if user:

            login_user(user, remember=request.form.get('remember') == 'on')

            return redirect(request.args.get('next') or url_for('dashboard'))

        flash('Invalid username or password.', 'error')

    return render_template('login.html')



@app.route('/logout')

@login_required

def logout():

    logout_user()

    return redirect(url_for('login'))



# IA spec §7: the Dashboard's cron-freshness check. Each job's own cron.log mtime is its
# last-successful-run signal (every job appends a line on every run, per crontab -l's `>>`),
# so no per-job log-format parsing is needed. stale_after_min is roughly 2-3x the real
# cadence, not 1x -- a job running a few minutes late is normal jitter, not a genuine
# problem (citemetrix-alerting-philosophy: alerts only for GENUINE sustained issues).
CRON_STALENESS_CHECKS = [
    {'name': 'GSC weekly report', 'log': 'reports/gsc/cron.log', 'stale_after_min': 9 * 24 * 60, 'link': '/marketing/measure/effectiveness', 'link_label': 'Measure → Campaigns'},
    {'name': 'Campaign effectiveness', 'log': 'reports/campaigns/cron.log', 'stale_after_min': 30 * 60, 'link': '/marketing/measure/effectiveness', 'link_label': 'Measure → Campaigns'},
    {'name': 'Traffic', 'log': 'reports/traffic/cron.log', 'stale_after_min': 30 * 60, 'link': '/marketing/measure/traffic', 'link_label': 'Measure → Traffic'},
    {'name': 'Deliverability', 'log': 'reports/deliverability/cron.log', 'stale_after_min': 30 * 60, 'link': '/marketing/measure/deliverability', 'link_label': 'Measure → Deliverability'},
    {'name': 'Blacklist / reputation', 'log': '/home/ubuntu/blacklist.log', 'stale_after_min': 30 * 60, 'link': '/marketing/measure/deliverability', 'link_label': 'Measure → Deliverability'},
    {'name': 'Messaging platform health', 'log': 'reports/messaging/cron.log', 'stale_after_min': 30, 'link': '/operations', 'link_label': 'Product Jobs'},
    {'name': 'Drip campaign sends', 'log': 'reports/drip/cron.log', 'stale_after_min': 180, 'link': '/marketing/drip-campaigns', 'link_label': 'Sequences'},
    {'name': 'Lead migration sync', 'log': 'reports/migrations/cron.log', 'stale_after_min': 180, 'link': '/marketing/leads', 'link_label': 'Leads'},
    {'name': 'Duplicate detector', 'log': 'reports/dedupe/cron.log', 'stale_after_min': 180, 'link': '/marketing/leads', 'link_label': 'Leads'},
]


def _check_stale_jobs():
    stale = []
    now = datetime.now()
    for job in CRON_STALENESS_CHECKS:
        path = job['log'] if os.path.isabs(job['log']) else os.path.join('/var/www/admin-portal', job['log'])
        try:
            age_min = (now - datetime.fromtimestamp(os.path.getmtime(path))).total_seconds() / 60
        except OSError:
            stale.append({**job, 'age_min': None})
            continue
        if age_min > job['stale_after_min']:
            stale.append({**job, 'age_min': age_min})
    return stale


@app.route('/')
@login_required
def dashboard():
    """IA spec §7: a work queue, not a status board -- the list of what's waiting for a
    human, not five numbers to read and move past. Five sections, each a door straight to
    the thing needing action:
      1. Leads that just crossed into engaged -- the marketing->sales handoff (§4). Highest
         priority; nothing surfaced this before this build.
      2. Open CS alerts -- now the SAME live computation /cs-alerts itself shows
         (_compute_cs_alerts()), not the disconnected wp_citemetrix_customer_alerts table
         the old dashboard stat read from (see that function's docstring).
      3. Investor threads awaiting reply (outreach_thread.status='sent').
      4. Failed jobs and stale data (_check_stale_jobs() above).
      5. Booked demos in the next 48 hours.
    Stats strip below keeps exactly the 4 figures the spec names (MRR, subscribers, leads,
    scans) -- CS alerts moved into the queue above instead of staying a redundant tile, and
    'leads' now reads the unified `leads` pool (admin_portal DB), not wp_citemetrix_score_leads."""
    q = {'engaged': [], 'cs_alerts': None, 'investor_threads': [], 'stale_jobs': [], 'demos': []}
    m = {'subscribers': 0, 'mrr': 0.0, 'leads_7d': 0, 'scans_today': 0}

    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT tier_key FROM wp_citemetrix_tier_entitlements WHERE price_monthly>0")
            _paid = [r['tier_key'] for r in cur.fetchall()]
            if _paid:
                cur.execute("SELECT COUNT(*) c FROM wp_usermeta WHERE meta_key='citemetrix_tier' AND meta_value IN (" + ','.join(['%s'] * len(_paid)) + ")", _paid)
                m['subscribers'] = int(cur.fetchone()['c'] or 0)
            cur.execute("SELECT COALESCE(SUM(total_amount),0) s FROM wp_wc_orders WHERE type='shop_subscription' AND status='wc-active'")
            m['mrr'] = float(cur.fetchone()['s'] or 0)
            cur.execute("SELECT COUNT(*) c FROM wp_citemetrix_scan_runs WHERE DATE(started_at)=CURDATE()")
            m['scans_today'] = int(cur.fetchone()['c'] or 0)

            cur.execute(
                "SELECT a.id, a.start_date, cu.full_name, cu.email "
                "FROM wp_bookly_appointments a "
                "LEFT JOIN wp_bookly_customer_appointments ca ON ca.appointment_id = a.id "
                "LEFT JOIN wp_bookly_customers cu ON cu.id = ca.customer_id "
                "WHERE a.start_date BETWEEN NOW() AND DATE_ADD(NOW(), INTERVAL 48 HOUR) "
                "ORDER BY a.start_date ASC"
            )
            q['demos'] = cur.fetchall()
        conn.close()
    except Exception:
        app.logger.exception('dashboard: WP-side queries failed')

    try:
        ac = get_admin_db()
        with ac.cursor() as cur:
            # 'leads' now reads the unified pool, not wp_citemetrix_score_leads (IA spec §7/§8).
            cur.execute("SELECT COUNT(*) c FROM leads WHERE excluded=0 AND created_at>=DATE_SUB(NOW(),INTERVAL 7 DAY)")
            m['leads_7d'] = int(cur.fetchone()['c'] or 0)

            cur.execute(
                "SELECT id, email, first_name, last_name, company, stage_entered_at "
                "FROM leads WHERE stage='engaged' AND excluded=0 ORDER BY stage_entered_at DESC LIMIT 10"
            )
            q['engaged'] = cur.fetchall()

            cur.execute(
                "SELECT id, firm_name, target_person, updated_at FROM outreach_thread "
                "WHERE status='sent' ORDER BY updated_at ASC LIMIT 10"
            )
            q['investor_threads'] = cur.fetchall()
        ac.close()
    except Exception:
        app.logger.exception('dashboard: admin-portal-side queries failed')

    try:
        cs = _compute_cs_alerts()
        q['cs_alerts'] = cs
    except Exception:
        app.logger.exception('dashboard: cs alerts computation failed')

    q['stale_jobs'] = _check_stale_jobs()
    q['all_clear'] = not (q['engaged'] or (q['cs_alerts'] and q['cs_alerts']['alerts']) or q['investor_threads'] or q['stale_jobs'] or q['demos'])

    return render_template('dashboard.html', m=m, q=q)



@app.route('/operations')

@login_required
@role_required('admin', 'operations')

def operations():

    """

    System Health dashboard.



    Renders a three-lane cron overview (fast/slow/digest) plus a

    grouped-by-lane hooks table, job-queue summaries, and a recent errors

    panel. Each lane's health is inferred from the most recent cm_last_run_*

    timestamp of hooks assigned to that lane — we don't have direct lockfile

    visibility from the admin portal (different server), so recency of the

    per-hook "last run" marker is the best signal available.

    """

    data = {

        'cron_hooks': [],

        'lanes': {},

        'job_queues': {},

        'job_queues_by_lane': {},

        'recent_errors': [],

        'show_all_errors': False,

        'hidden_error_count': 0,

        'job_stats': {},

        'failed_jobs_detail': [],
        'competition_batch': None,

    }



    # Hook metadata: (name, display_name, schedule, max_minutes, lane).

    # max_minutes uses a 1.5x multiplier in the OK/overdue check below so

    # we don't mark something red the second it misses its window by 30s.

    # Lane assignment mirrors cron-runner.php / cron-slow-runner.php /

    # cron-digest-runner.php. Anything scheduled less than hourly is

    # effectively fast-lane-adjacent — the scheduled fire itself is

    # instant; any work it enqueues is drained by the fast lane.

    hooks = [

        ('citemetrix_process_scan_queue',        'Scan Queue',          'every_minute',   2,     'fast'),

        ('citemetrix_process_analysis_queue',    'Analysis Queue',      'every_minute',   2,     'fast'),

        ('citemetrix_scheduled_scan',            'Scheduled Scan',      'every_15_min',   20,    'fast'),

        ('citemetrix_process_drip',              'Drip Campaign',       'hourly',         75,    'fast'),

        ('citemetrix_send_nurture_emails',       'Nurture Emails',      'hourly',         75,    'fast'),

        ('citemetrix_check_surveys',             'Survey Check',        'daily',          1500,  'fast'),

        ('citemetrix_data_retention',            'Data Retention',      'daily',          1500,  'fast'),

        ('citemetrix_analyst_coordinator_daily', 'Analyst Coordinator', 'daily',          1500,  'fast'),

        ('citemetrix_lifecycle_daily',           'Lifecycle',           'daily',          1500,  'fast'),

        ('citemetrix_aio_weekly',                'AIO Weekly',          'weekly Sun 02',  10200, 'fast'),

        ('cm_tech_enqueue_weekly',               'Tech Check Enqueue',  'weekly Sun 03',  10200, 'fast'),

        ('citemetrix_monthly_reset',             'Monthly Reset',       'monthly 1st',    44640, 'fast'),


        ('citemetrix_weekly_digest',             'Weekly Digest',       'weekly Mon 09',  10200, 'digest'),

    ]



    # Lane metadata for the top-of-page summary cards.

    lane_meta = {

        'fast': {

            'display_name':  'Fast Lane',

            'description':   'Accuracy, sentiment, technical checks. Every minute via cron-runner.php.',

            'job_types':     ['accuracy_check', 'sentiment_analysis', 'technical_check'],

            'interval_desc': 'every minute',

        },

        'slow': {

            'display_name':  'Slow Lane',

            'description':   'Competitor scans. Every minute via cron-slow-runner.php, 180s timeout.',

            'job_types':     ['competitor_scan', 'domain_competitor_scan'],

            'interval_desc': 'every minute',

        },

        'digest': {

            'display_name':  'Digest Lane',

            'description':   'User weekly digests. Every 5 minutes via cron-digest-runner.php, 240s timeout.',

            'job_types':     ['digest_user'],

            'interval_desc': 'every 5 minutes',

        },

    }



    try:

        conn = get_db()

        with conn.cursor() as cursor:

            # Failed-job DETAIL for the clickable failure tiles (last 24h). The lane tiles
            # show a failed COUNT (from the same table); this surfaces the rows behind it —
            # domain NAME (joined), error + error_category, and DISPOSITION: whether a later
            # successful scan of the same domain+job_type superseded the failure. So a red
            # tile can be triaged (resolved-since vs still-unresolved; upstream/transient vs
            # real system failure) in one click instead of a manual DB query.
            try:
                cursor.execute(
                    "SELECT f.id, f.domain_id, COALESCE(d.domain, CONCAT('domain #', f.domain_id)) AS domain_name, "
                    "       f.job_type, f.error, f.error_category, f.updated_at, "
                    "       EXISTS ( "
                    "         SELECT 1 FROM wp_citemetrix_analysis_jobs s "
                    "         WHERE s.domain_id = f.domain_id AND s.job_type = f.job_type "
                    "           AND s.status = 'completed' AND s.updated_at > f.updated_at "
                    "       ) AS resolved_since "
                    "FROM wp_citemetrix_analysis_jobs f "
                    "LEFT JOIN wp_citemetrix_domains d ON d.id = f.domain_id "
                    "WHERE f.status = 'failed' AND f.updated_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR) "
                    "ORDER BY f.updated_at DESC LIMIT 50"
                )
                data['failed_jobs_detail'] = cursor.fetchall()
            except Exception:
                data['failed_jobs_detail'] = []

            # Competitor-scan batch pipeline (async Anthropic Batch API, migrated off-box).
            # Outcome-layer health (apply-time + per-item failures) that SQS/queue depth
            # cannot see — a batch can COMPLETE with individually-failed items.
            try:
                cursor.execute(
                    "SELECT completed_at, item_count, apply_ms "
                    "FROM wp_citemetrix_competition_batch_jobs "
                    "WHERE status='completed' ORDER BY completed_at DESC LIMIT 1"
                )
                _last = cursor.fetchone()
                cursor.execute(
                    "SELECT SUM(status='failed') AS failed_items, "
                    "SUM(status IN ('succeeded','failed')) AS done_items "
                    "FROM wp_citemetrix_competition_batch_items "
                    "WHERE processed_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"
                )
                _agg = cursor.fetchone() or {}
                cursor.execute(
                    "SELECT COUNT(*) AS stuck FROM wp_citemetrix_competition_batch_jobs "
                    "WHERE status IN ('accumulating','submitted','polling') "
                    "AND created_at < DATE_SUB(NOW(), INTERVAL 6 HOUR)"
                )
                _stuck = int((cursor.fetchone() or {}).get('stuck') or 0)
                _failed = int(_agg.get('failed_items') or 0)
                _done = int(_agg.get('done_items') or 0)
                _fpct = round(_failed / _done * 100) if _done else 0
                _status = 'ok'
                if _done >= 5 and _fpct >= 20:
                    _status = 'warn'
                if _stuck:
                    _status = 'error'
                data['competition_batch'] = {
                    'last_completed_at': _last['completed_at'] if _last else None,
                    'last_items': int(_last['item_count'] or 0) if _last else 0,
                    'last_apply_ms': (int(_last['apply_ms']) if _last and _last['apply_ms'] is not None else None),
                    'failed_items_24h': _failed,
                    'done_items_24h': _done,
                    'fail_pct_24h': _fpct,
                    'stuck': _stuck,
                    'status': _status,
                }
            except Exception:
                data['competition_batch'] = None



            # Load WP's cron array once and build a map of hook_name -> next

            # scheduled timestamp. Same data wp_next_scheduled() returns, but

            # we query it once rather than per-hook. The PHP-serialized cron

            # option is keyed by timestamp; we extract (ts, hook_name) pairs

            # via regex and keep the earliest scheduled time per hook.

            cron_next_scheduled = {}

            cron_debug = {'status': 'not_attempted', 'raw_len': 0, 'hooks_found': 0}

            try:

                cursor.execute("SELECT option_value FROM wp_options WHERE option_name = 'cron'")

                _cron_row = cursor.fetchone()

                if _cron_row and _cron_row['option_value']:

                    _raw = _cron_row['option_value']

                    # pymysql may return TEXT/LONGTEXT columns as bytes with some
                    # driver configurations. Normalize to str before regex.
                    if isinstance(_raw, (bytes, bytearray)):

                        _raw = _raw.decode('utf-8', errors='replace')

                    cron_debug['raw_len'] = len(_raw)

                    import re as _re

                    # Sequentially scan timestamps and hook names.
                    _events = []

                    for _m in _re.finditer(r'i:(\d{10,11});', _raw):

                        _events.append(('ts', int(_m.group(1)), _m.start()))

                    for _m in _re.finditer(r's:\d+:"(citemetrix_[a-z_]+|cm_[a-z_]+)";a:1:\{s:32:', _raw):

                        _events.append(('hook', _m.group(1), _m.start()))

                    _events.sort(key=lambda e: e[2])

                    _current_ts = None

                    for _kind, _val, _pos in _events:

                        if _kind == 'ts':

                            _current_ts = _val

                        elif _kind == 'hook' and _current_ts is not None:

                            _prev = cron_next_scheduled.get(_val)

                            # Keep the earliest FUTURE timestamp per hook —
                            # a past-due entry that hasn't been cleaned up
                            # shouldn't override a legitimate future fire.
                            _now_for_filter = int(datetime.now().timestamp())

                            if _current_ts >= _now_for_filter:

                                if _prev is None or _current_ts < _prev:

                                    cron_next_scheduled[_val] = _current_ts

                    cron_debug['hooks_found'] = len(cron_next_scheduled)

                    cron_debug['status'] = 'ok'

                    # Include the actual map keys + timestamps so we can see
                    # whether a specific expected hook is present or missing.
                    cron_debug['map'] = {

                        k: gmdate_utc(v) for k, v in sorted(cron_next_scheduled.items())

                    }

                else:

                    cron_debug['status'] = 'empty_row'

            except Exception as _e:

                cron_debug['status'] = f'error: {type(_e).__name__}: {str(_e)[:200]}'

            data['cron_debug'] = cron_debug


            # Load cm_last_run_* for every hook

            for hook_name, display_name, schedule, max_minutes, lane in hooks:

                cursor.execute(

                    "SELECT option_value FROM wp_options WHERE option_name = %s",

                    (f'cm_last_run_{hook_name}',)

                )

                row = cursor.fetchone()

                last_run, status, minutes_ago = None, 'unknown', None

                if row and row['option_value']:

                    try:

                        ts = int(row['option_value'])

                        last_run = datetime.fromtimestamp(ts)

                        minutes_ago = (datetime.now() - last_run).total_seconds() / 60

                        # Schedule-aware status: prefer next_scheduled from WP's cron array.

                        # If the next fire is in the future, the hook is healthy regardless

                        # of how long it's been since last_run. Only fall back to last_run

                        # freshness when next_scheduled is unavailable.

                        _next_ts = cron_next_scheduled.get(hook_name)

                        if _next_ts is not None:

                            _now_ts = int(datetime.now().timestamp())

                            if _next_ts >= _now_ts:

                                status = 'ok'

                            else:

                                # Next fire is in the past — actually overdue. Tolerate

                                # 0.5x the expected interval before flashing red.

                                _overdue_m = (_now_ts - _next_ts) / 60

                                status = 'overdue' if _overdue_m > max_minutes * 0.5 else 'ok'

                        else:

                            # No schedule record — use last_run freshness with 1.5x margin.

                            status = 'ok' if minutes_ago <= max_minutes * 1.5 else 'overdue' 

                    except Exception:

                        status = 'error'

                else:

                    status = 'pending' if schedule != 'every_minute' else 'overdue'



                data['cron_hooks'].append({

                    'name':         hook_name,

                    'display_name': display_name,

                    'schedule':     schedule,

                    'lane':         lane,

                    'last_run':     last_run,

                    'minutes_ago':  int(minutes_ago) if minutes_ago is not None else None,

                    'status':       status,

                })



            # Per-lane summary. Lane status rollup uses each lane's MOST

            # FREQUENT hook as the health signal, because that hook's

            # recency is the most reliable indicator of whether the lane

            # is actually running right now.

            #

            # Fast Lane has every_minute hooks — expect them to be fresh

            # within ~2 minutes. Slow and Digest Lanes only have daily or

            # weekly hooks — a daily hook being 12 hours old is perfectly

            # normal. The old logic flagged slow/digest as "pending" at

            # every point between scheduled fires, which was alarmist

            # noise rather than a useful signal.

            schedule_priority = {

                'every_minute': 0,

                'every_15_min': 1,

                'hourly':       2,

                'daily':        3,

            }

            for lane_key, meta in lane_meta.items():

                lane_hooks = [h for h in data['cron_hooks'] if h['lane'] == lane_key]



                # Pick the hook with the tightest schedule — the one we

                # expect the freshest timestamp from. Weekly and monthly

                # schedules share the lowest rank; if a lane has nothing

                # shorter than weekly, that's what we use as the signal.

                signal_hook = None

                if lane_hooks:

                    signal_hook = min(lane_hooks, key=lambda h: schedule_priority.get(h['schedule'], 4))



                # Any error or overdue on ANY hook still wins — we don't

                # want to hide a failing hook just because the signal hook

                # is healthy.

                if any(h['status'] == 'overdue' for h in lane_hooks):

                    lane_status = 'overdue'

                elif any(h['status'] == 'error' for h in lane_hooks):

                    lane_status = 'error'

                elif signal_hook and signal_hook['status'] == 'ok':

                    lane_status = 'ok'

                elif signal_hook and signal_hook['status'] == 'pending':

                    # "pending" means the hook has never fired, which on

                    # a fresh install is normal for daily/weekly hooks.

                    # Only surface it as a problem if we really have no

                    # data at all.

                    lane_status = 'pending' if signal_hook['last_run'] is None else 'ok'

                else:

                    lane_status = 'unknown'



                recent_runs = [h['last_run'] for h in lane_hooks if h['last_run']]

                last_fire = max(recent_runs) if recent_runs else None

                last_fire_minutes_ago = None

                if last_fire:

                    last_fire_minutes_ago = int((datetime.now() - last_fire).total_seconds() / 60)



                data['lanes'][lane_key] = {

                    'display_name':          meta['display_name'],

                    'description':           meta['description'],

                    'interval_desc':         meta['interval_desc'],

                    'status':                lane_status,

                    'last_fire':             last_fire,

                    'last_fire_minutes_ago': last_fire_minutes_ago,

                    'hook_count':            len(lane_hooks),

                    'job_types':             meta['job_types'],

                    'signal_hook_schedule':  signal_hook['schedule'] if signal_hook else None,

                }





            # Per-lane job queue depth. Group by job_type so each lane
            # only shows counts for its own job types.
            #
            # IMPORTANT: pending/running are CURRENT-STATE counts (a job that's
            # been pending for days is still pending — do NOT time-filter those,
            # or a genuinely stuck job gets hidden). 'failed' is a TERMINAL state
            # that accumulates forever, so an all-time failed count makes a lane
            # look permanently broken long after the underlying issue is fixed
            # (e.g. the 4.8.5 competitor-scan admin-id fix stops NEW failures but
            # ~500 historical failed rows would still show here indefinitely).
            # So we window the failed count to the last LANE_FAILED_WINDOW_HOURS
            # while leaving pending/running unfiltered.
            LANE_FAILED_WINDOW_HOURS = 24

            cursor.execute(
                "SELECT job_type, "
                "  SUM(status = 'pending') AS pending, "
                "  SUM(status = 'running') AS running, "
                "  SUM(status = 'failed' AND updated_at >= DATE_SUB(NOW(), INTERVAL %s HOUR)) AS failed "
                "FROM wp_citemetrix_analysis_jobs "
                "WHERE status IN ('pending', 'running', 'failed') "
                "GROUP BY job_type",
                (LANE_FAILED_WINDOW_HOURS,)
            )

            job_type_counts = {}

            for r in cursor.fetchall():
                job_type_counts[r['job_type']] = {
                    'pending': int(r['pending'] or 0),
                    'running': int(r['running'] or 0),
                    'failed':  int(r['failed']  or 0),
                }

            for lane_key, meta in lane_meta.items():
                pending = sum(job_type_counts.get(jt, {}).get('pending', 0) for jt in meta['job_types'])
                running = sum(job_type_counts.get(jt, {}).get('running', 0) for jt in meta['job_types'])
                failed  = sum(job_type_counts.get(jt, {}).get('failed',  0) for jt in meta['job_types'])

                data['job_queues_by_lane'][lane_key] = {
                    'pending': pending,
                    'running': running,
                    'failed':  failed,
                    'failed_window_hours': LANE_FAILED_WINDOW_HOURS,
                }



            # Aggregate job-queue counts (unchanged from previous version)

            cursor.execute("SELECT status, COUNT(*) as c FROM wp_citemetrix_analysis_jobs GROUP BY status")

            data['job_queues']['analysis'] = {r['status']: r['c'] for r in cursor.fetchall()}



            cursor.execute(

                "SELECT status, COUNT(*) as c FROM wp_citemetrix_analysis_jobs "

                "WHERE updated_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR) GROUP BY status"

            )

            data['job_stats']['analysis_24h'] = {r['status']: r['c'] for r in cursor.fetchall()}



            cursor.execute("SELECT status, COUNT(*) as c FROM wp_citemetrix_scan_jobs GROUP BY status")

            data['job_queues']['scan'] = {r['status']: r['c'] for r in cursor.fetchall()}



            cursor.execute(

                "SELECT status, COUNT(*) as c FROM wp_citemetrix_scan_jobs "

                "WHERE updated_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR) GROUP BY status"

            )

            data['job_stats']['scan_24h'] = {r['status']: r['c'] for r in cursor.fetchall()}



            # ── Recent Errors ──────────────────────────────────────────
            # By default we hide:
            #   - upstream_*       (platform issues, not our bug)
            #   - user_setup_incomplete  (user hasn't finished setup)
            #   - subscription_inactive  (user's plan lapsed)
            # Showing only 'internal' (real CiteMetrix bugs) plus any
            # uncategorized rows for safety. The show_all flag flips
            # this off so the operator can see everything when needed.
            #
            # Wrapped in try/except: if the plugin hasn't been upgraded
            # yet on the WordPress side, the error_category column
            # won't exist. Fall back to the unfiltered query so the
            # admin portal stays functional during plugin deploys.
            show_all_errors = request.args.get('show_all') == '1'

            try:
                if show_all_errors:
                    cursor.execute(
                        "SELECT j.job_type, j.error, j.error_category, j.updated_at, d.domain, u.user_email "
                        "FROM wp_citemetrix_analysis_jobs j "
                        "LEFT JOIN wp_citemetrix_domains d ON j.domain_id = d.id "
                        "LEFT JOIN wp_users u ON j.user_id = u.ID "
                        "WHERE j.status = 'failed' "
                        "ORDER BY j.updated_at DESC LIMIT 25"
                    )
                else:
                    cursor.execute(
                        "SELECT j.job_type, j.error, j.error_category, j.updated_at, d.domain, u.user_email "
                        "FROM wp_citemetrix_analysis_jobs j "
                        "LEFT JOIN wp_citemetrix_domains d ON j.domain_id = d.id "
                        "LEFT JOIN wp_users u ON j.user_id = u.ID "
                        "WHERE j.status = 'failed' "
                        "  AND ( j.error_category IS NULL OR j.error_category = 'internal' ) "
                        "ORDER BY j.updated_at DESC LIMIT 25"
                    )

                data['recent_errors'] = cursor.fetchall()
                data['show_all_errors'] = show_all_errors

                # Count of hidden (filtered-out) errors so the UI can show
                # a "X user-side issues hidden — show all" button.
                cursor.execute(
                    "SELECT COUNT(*) AS cnt FROM wp_citemetrix_analysis_jobs "
                    "WHERE status = 'failed' "
                    "  AND error_category IS NOT NULL "
                    "  AND error_category != 'internal'"
                )
                data['hidden_error_count'] = cursor.fetchone()['cnt']

            except (pymysql.MySQLError, pymysql.OperationalError, pymysql.ProgrammingError):
                # Column doesn't exist yet — plugin hasn't been upgraded
                # to 3.13.45. Fall back to legacy unfiltered query so
                # the panel still works.
                cursor.execute(
                    "SELECT j.job_type, j.error, NULL AS error_category, j.updated_at, d.domain, u.user_email "
                    "FROM wp_citemetrix_analysis_jobs j "
                    "LEFT JOIN wp_citemetrix_domains d ON j.domain_id = d.id "
                    "LEFT JOIN wp_users u ON j.user_id = u.ID "
                    "WHERE j.status = 'failed' "
                    "ORDER BY j.updated_at DESC LIMIT 15"
                )
                data['recent_errors'] = cursor.fetchall()
                data['show_all_errors'] = True  # Effectively "show all" since we can't filter
                data['hidden_error_count'] = 0

            # ── DB Health ──────────────────────────────────────────────────
            # Capture connection pool state and abort counters from
            # GLOBAL_STATUS. These are the primary scale indicators that
            # told us last week the DB was undersized (33 peak on a 30
            # ceiling, 1054 aborted connects). Now visible on every page
            # load so we catch future pressure early.
            db_health = {}

            try:

                cursor.execute("SELECT @@max_connections AS m, @@innodb_buffer_pool_size AS b, @@version AS v")

                _meta = cursor.fetchone()

                db_health['max_connections']         = int(_meta['m'])

                db_health['innodb_buffer_pool_mb']   = int(_meta['b']) // (1024 * 1024)

                db_health['version']                 = _meta['v']

                cursor.execute("""

                    SELECT VARIABLE_NAME, VARIABLE_VALUE

                    FROM information_schema.GLOBAL_STATUS

                    WHERE VARIABLE_NAME IN (

                        'THREADS_CONNECTED',

                        'THREADS_RUNNING',

                        'MAX_USED_CONNECTIONS',

                        'CONNECTIONS',

                        'ABORTED_CONNECTS',

                        'ABORTED_CLIENTS',

                        'SLOW_QUERIES',

                        'UPTIME'

                    )

                """)

                _status = {r['VARIABLE_NAME'].lower(): int(r['VARIABLE_VALUE']) for r in cursor.fetchall()}

                db_health.update(_status)

                # Derived health signals
                _max = db_health['max_connections']

                _peak = db_health.get('max_used_connections', 0)

                _now_conn = db_health.get('threads_connected', 0)

                # Connection pressure: peak as % of ceiling. Under 50%
                # is comfortable, 50-75% is getting tight, over 75%
                # needs attention.
                db_health['peak_pct']    = round((_peak / _max) * 100, 1) if _max else 0

                db_health['current_pct'] = round((_now_conn / _max) * 100, 1) if _max else 0

                if db_health['peak_pct'] < 50:

                    db_health['conn_status'] = 'ok'

                elif db_health['peak_pct'] < 75:

                    db_health['conn_status'] = 'warn'

                else:

                    db_health['conn_status'] = 'alert'

                # Abort pressure: any aborted_connects is bad but we
                # grade by rate. 0-5 since restart = fine, anything
                # else is worth investigating.
                _aborts = db_health.get('aborted_connects', 0)

                if _aborts == 0:

                    db_health['abort_status'] = 'ok'

                elif _aborts < 10:

                    db_health['abort_status'] = 'warn'

                else:

                    db_health['abort_status'] = 'alert'

                # Uptime for display
                _uptime = db_health.get('uptime', 0)

                if _uptime < 3600:

                    db_health['uptime_str'] = f'{_uptime // 60}m'

                elif _uptime < 86400:

                    db_health['uptime_str'] = f'{_uptime // 3600}h'

                else:

                    db_health['uptime_str'] = f'{_uptime // 86400}d {(_uptime % 86400) // 3600}h'

            except Exception as _e:

                db_health['error'] = f'{type(_e).__name__}: {str(_e)[:200]}'

            data['db_health'] = db_health



        conn.close()

    except Exception as e:

        data['error'] = str(e)



    # ── Active ops alerts ────────────────────────────────────────────────
    # Loaded from the admin portal's own MySQL database (not PRD RDS) —
    # ops state lives separately from product state by design. Alerts are
    # INSERTed by the /api/alerts/ingest endpoint whenever the plugin's
    # CiteMetrix_Alert_Dispatcher fires. Unacknowledged rows surface here;
    # acknowledged rows are kept for audit but hidden from the panel.
    data['active_alerts'] = []

    data['alerts_error']  = None

    try:

        admin_conn = get_admin_db()

        with admin_conn.cursor() as cursor:

            cursor.execute("""

                SELECT id, alert_type, severity, user_id, user_email,

                       message, metadata, created_at

                FROM system_alerts

                WHERE acknowledged_at IS NULL

                ORDER BY

                    CASE severity

                        WHEN 'critical' THEN 0

                        WHEN 'warn'     THEN 1

                        ELSE              2

                    END,

                    created_at DESC

                LIMIT 50

            """)

            data['active_alerts'] = cursor.fetchall()

        admin_conn.close()

    except pymysql.err.ProgrammingError as _e:

        # Most common case: table doesn't exist yet. Surface a hint rather
        # than blowing up the page. Gets cleared once table is created.
        if '1146' in str(_e):

            data['alerts_error'] = 'system_alerts table not found — run the one-time migration (see admin portal README).'

        else:

            data['alerts_error'] = f'Alert query failed: {str(_e)[:200]}'

    except Exception as _e:

        data['alerts_error'] = f'Alert query failed: {type(_e).__name__}: {str(_e)[:200]}'



    # ── V2 AWS Scan Pipeline Health (added 2026-06-05) ─────────────────────
    # The panels above monitor the v1 WordPress-cron pipeline, which is dormant
    # since the 2026-06-04 v2 cutover. This block surfaces the v2 pipeline that
    # actually runs scans now: scan_runs completion state, stuck runs, 24h
    # completion health (the "fresh by 8am" SLA signal), and quota-failure rollup.
    # DB-only (Phase A) — SQS queue depths + fetcher task count (Phase B) need
    # boto3 + IAM on the portal role and are added separately.
    # Self-contained try/except: a failure here never breaks the rest of the page.
    v2 = {
        'active_runs': [], 'stuck_runs': [], 'completion_24h': {},
        'quota_rollup': [], 'error': None,
    }
    STUCK_THRESHOLD_MIN = 360  # mirrors scheduler sweep_stuck_runs
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            # Panel 1 — active/recent runs (last 6h), newest first.
            cursor.execute(
                "SELECT id, domain_id, scan_type, enqueued_count, fetched_count, "
                "       analyzed_count, failed_count, "
                "       (fetched_count + failed_count) AS terminal, "
                "       started_at, completed_at, failure_summary, "
                "       TIMESTAMPDIFF(MINUTE, started_at, COALESCE(completed_at, NOW())) AS mins "
                "FROM wp_citemetrix_scan_runs "
                "WHERE started_at >= DATE_SUB(NOW(), INTERVAL 6 HOUR) "
                "ORDER BY started_at DESC LIMIT 50"
            )
            for r in cursor.fetchall():
                enq = int(r['enqueued_count'] or 0)
                term = int(r['terminal'] or 0)
                r['pct'] = int(term * 100 / enq) if enq else 0
                r['is_complete'] = r['completed_at'] is not None
                r['has_failures'] = int(r['failed_count'] or 0) > 0
                v2['active_runs'].append(r)

            # Panel 2 — stuck runs (started, never completed, past threshold).
            # Same logic as scheduler sweep_stuck_runs — the "would've caught
            # the overnight stall" panel.
            cursor.execute(
                "SELECT id, domain_id, enqueued_count, fetched_count, analyzed_count, "
                "       failed_count, started_at, "
                "       TIMESTAMPDIFF(MINUTE, started_at, NOW()) AS mins "
                "FROM wp_citemetrix_scan_runs "
                "WHERE completed_at IS NULL "
                "  AND started_at < DATE_SUB(NOW(), INTERVAL %s MINUTE) "
                "ORDER BY started_at ASC LIMIT 50",
                (STUCK_THRESHOLD_MIN,)
            )
            v2['stuck_runs'] = cursor.fetchall()

            # Panel 3 — completion health, last 24h (the SLA signal).
            cursor.execute(
                "SELECT "
                "  COUNT(*) AS total, "
                "  SUM(completed_at IS NOT NULL) AS completed, "
                "  SUM(completed_at IS NULL) AS in_progress, "
                "  ROUND(AVG(CASE WHEN completed_at IS NOT NULL "
                "    THEN TIMESTAMPDIFF(MINUTE, started_at, completed_at) END), 1) AS avg_complete_min, "
                "  MAX(CASE WHEN completed_at IS NOT NULL "
                "    THEN TIMESTAMPDIFF(MINUTE, started_at, completed_at) END) AS max_complete_min "
                "FROM wp_citemetrix_scan_runs "
                "WHERE started_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR)"
            )
            c = cursor.fetchone() or {}
            v2['completion_24h'] = {
                'total': int(c.get('total') or 0),
                'completed': int(c.get('completed') or 0),
                'in_progress': int(c.get('in_progress') or 0),
                'avg_complete_min': c.get('avg_complete_min'),
                'max_complete_min': c.get('max_complete_min'),
            }

            # Panel 4 — quota/failure rollup across runs with failures (last 24h).
            # failure_summary is JSON keyed by platform; aggregate in Python since
            # it's a small set.
            cursor.execute(
                "SELECT domain_id, failure_summary FROM wp_citemetrix_scan_runs "
                "WHERE started_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR) "
                "  AND failure_summary IS NOT NULL"
            )
            platform_totals = {}
            for r in cursor.fetchall():
                fs = r['failure_summary']
                if isinstance(fs, (bytes, bytearray)):
                    fs = fs.decode('utf-8', 'replace')
                try:
                    parsed = json.loads(fs) if fs else {}
                except (ValueError, TypeError):
                    continue
                for platform, info in parsed.items():
                    if platform not in platform_totals:
                        platform_totals[platform] = {'count': 0, 'reason': info.get('reason', '')}
                    platform_totals[platform]['count'] += int(info.get('count', 0) or 0)
            v2['quota_rollup'] = sorted(
                ({'platform': k, 'count': v['count'], 'reason': v['reason']}
                 for k, v in platform_totals.items()),
                key=lambda x: x['count'], reverse=True
            )
    except Exception as _e:  # noqa: BLE001 — never break the page
        v2['error'] = f"{type(_e).__name__}: {str(_e)[:200]}"
    data['v2'] = v2

    # ── V2 Phase B: SQS queue depths + ECS fetcher fleet (added 2026-06-05) ──
    # These come from AWS APIs (not the DB), so they need boto3 + the portal's
    # IAM user to have SQS read + ECS describe permissions. Region is us-east-2
    # (queues/cluster) — NOT the env AWS_REGION, which is us-east-1 for SES.
    # Entirely self-contained + degrades gracefully: if boto3/creds/permissions
    # are missing, each sub-panel shows 'unavailable' and the page still renders.
    infra = {'queues': [], 'fetcher': {}, 'error': None}
    SCAN_REGION = 'us-east-2'
    FETCH_QUEUES = ['citemetrix-fetch-queue', 'citemetrix-analyze-queue', 'citemetrix-crawler-queue']
    DLQS = ['citemetrix-fetch-dlq', 'citemetrix-analyze-dlq', 'citemetrix-crawler-dlq']
    try:
        import boto3  # local import: portal works without it until Phase B IAM is set
        # IMPORTANT: build these clients from the DEDICATED read-only pipeline-visibility
        # IAM user's keys (PIPELINE_AWS_*), NOT the SES user's keys. Separation of duties:
        # the SES credential stays SES-only; this one is least-privilege read-only on SQS/ECS.
        # Both belong to account 939432267307 (infra). A bare boto3.client() would pick up the
        # Lightsail INSTANCE ROLE (account 647813929906), which has no access. Region us-east-2
        # (queues/cluster), NOT the env AWS_REGION which is us-east-1 for SES.
        _aws_key = os.getenv('PIPELINE_AWS_ACCESS_KEY_ID')
        _aws_secret = os.getenv('PIPELINE_AWS_SECRET_ACCESS_KEY')
        sqs = boto3.client('sqs', region_name=SCAN_REGION,
                           aws_access_key_id=_aws_key, aws_secret_access_key=_aws_secret)
        for qname in FETCH_QUEUES + DLQS:
            try:
                qurl = sqs.get_queue_url(QueueName=qname)['QueueUrl']
                attrs = sqs.get_queue_attributes(
                    QueueUrl=qurl,
                    AttributeNames=['ApproximateNumberOfMessages', 'ApproximateNumberOfMessagesNotVisible'],
                )['Attributes']
                infra['queues'].append({
                    'name': qname,
                    'is_dlq': qname.endswith('-dlq'),
                    'visible': int(attrs.get('ApproximateNumberOfMessages', 0)),
                    'in_flight': int(attrs.get('ApproximateNumberOfMessagesNotVisible', 0)),
                })
            except Exception as _qe:  # noqa: BLE001 — one bad queue shouldn't drop the rest
                infra['queues'].append({'name': qname, 'is_dlq': qname.endswith('-dlq'),
                                        'visible': None, 'in_flight': None, 'error': str(_qe)[:120]})

        ecs = boto3.client('ecs', region_name=SCAN_REGION,
                           aws_access_key_id=_aws_key, aws_secret_access_key=_aws_secret)
        svc = ecs.describe_services(cluster='citemetrix', services=['citemetrix-fetcher'])
        if svc.get('services'):
            s = svc['services'][0]
            infra['fetcher'] = {
                'desired': s.get('desiredCount'),
                'running': s.get('runningCount'),
                'pending': s.get('pendingCount'),
                'status': s.get('status'),
            }
    except Exception as _e:  # noqa: BLE001
        infra['error'] = f"{type(_e).__name__}: {str(_e)[:200]}"
    data['infra'] = infra

    try:
        import json as _mjson, os as _mos
        messaging = _mjson.load(open(_mos.path.join(_mos.path.dirname(_mos.path.abspath(__file__)), 'reports', 'messaging', 'health.json')))
    except Exception:
        messaging = None
    return render_template('operations/index.html', data=data, messaging=messaging)


def _compute_cs_alerts():
    """
    Customer Service alert detection (read-only surfacing - first step of the CS platform).

    Surfaces accounts in trouble states so an admin can decide whether to proactively
    reach out. Shows WHAT the problem is, WHICH account/domain, and WHO to contact
    (owner name + email). No remediation actions here - per the design, the CUSTOMER
    receives the alert with remediation steps (they own the fix); the admin view is
    situational awareness.

    Three detections, each tagged with a FAULT DOMAIN for triage:
      - byok_key_failing  (USER)      persistent quota/auth failures on their key.
      - domain_overdue    (AMBIGUOUS) active domain past due / never scheduled; could be
                                      a stuck pipeline (ours) OR account config (theirs).
      - stalled_onboarding(USER)      account created, zero scans ever completed.

    2026-09-04 (IA spec §7): extracted from the /cs-alerts route so the Dashboard work
    queue's "Open CS alerts" count and preview list come from this SAME computation --
    not the disconnected wp_citemetrix_customer_alerts table the old dashboard stat read,
    which nothing in this codebase actually writes to (confirmed: grepped for INSERTs/
    UPDATEs against it, found none). That table and this live detection were already two
    different, silently divergent answers to "how many CS alerts are open" -- exactly the
    kind of inconsistency the spec's "every number is a door" principle rules out. Now
    there's one computation, and the dashboard number always matches what /cs-alerts shows.
    """
    data = {'alerts': [], 'counts': {'byok_key_failing': 0, 'domain_overdue': 0, 'stalled_onboarding': 0}, 'error': None}

    try:
        conn = get_db()
        with conn.cursor() as cursor:

            # 1. BYOK key failing (USER) - persistent quota/auth failures in 24h, grouped
            # per user+platform. Threshold >= 3 so a transient blip is not an alert.
            try:
                cursor.execute(
                    "SELECT au.user_id, au.platform, au.error_category, COUNT(*) AS fails, "
                    "       MAX(au.created_at) AS last_fail, u.user_email, u.display_name "
                    "FROM wp_citemetrix_api_usage au "
                    "LEFT JOIN wp_users u ON au.user_id = u.ID "
                    "WHERE au.status = 'error' "
                    "  AND au.error_category IN ('upstream_quota','upstream_auth') "
                    "  AND au.created_at >= DATE_SUB(NOW(), INTERVAL 24 HOUR) "
                    "GROUP BY au.user_id, au.platform, au.error_category "
                    "HAVING fails >= 3 "
                    "ORDER BY last_fail DESC"
                )
                for r in cursor.fetchall():
                    cat = r['error_category']
                    problem = ('API key over quota' if cat == 'upstream_quota'
                               else 'API key rejected (auth failed)')
                    data['alerts'].append({
                        'type': 'byok_key_failing',
                        'fault': 'user',
                        'disposition': "FYI — customer's to fix; know in case they contact you",
                        'problem': f"{problem} on {r['platform']} — {r['fails']} failures in 24h",
                        'account': r.get('display_name') or '(no name)',
                        'email': r.get('user_email') or '(no email)',
                        'when': r.get('last_fail'),
                    })
                    data['counts']['byok_key_failing'] += 1
            except Exception:
                pass

            # 2. Domain overdue / never scheduled (AMBIGUOUS) - active, SCHEDULED domains
            # whose next_scan_at is NULL or > 24h past due. Excludes scan_frequency='manual'
            # because a manual domain has no next_scan_at BY DESIGN (the owner scans on
            # demand) — it is not overdue. This matches the scheduler's own filter
            # (class-citemetrix-scheduler.php: WHERE status='active' AND scan_frequency != 'manual').
            try:
                cursor.execute(
                    "SELECT d.id, d.domain, d.next_scan_at, d.last_scan_at, d.scan_frequency, "
                    "       u.user_email, u.display_name "
                    "FROM wp_citemetrix_domains d "
                    "LEFT JOIN wp_users u ON d.user_id = u.ID "
                    "WHERE d.status = 'active' "
                    "  AND d.scan_frequency != 'manual' "
                    "  AND ( d.next_scan_at IS NULL "
                    "        OR d.next_scan_at < DATE_SUB(NOW(), INTERVAL 24 HOUR) ) "
                    "ORDER BY d.next_scan_at IS NULL DESC, d.next_scan_at ASC"
                )
                for r in cursor.fetchall():
                    if r['next_scan_at'] is None:
                        problem = f"Active domain {r['domain']} has no scheduled scan (next_scan_at is empty)"
                    else:
                        problem = f"Active domain {r['domain']} is overdue — next scan was due {r['next_scan_at']}"
                    data['alerts'].append({
                        'type': 'domain_overdue',
                        'fault': 'ambiguous',
                        'disposition': 'NEEDS A LOOK — could be a pipeline issue (ours) or account config (theirs)',
                        'problem': problem,
                        'account': r.get('display_name') or '(no name)',
                        'email': r.get('user_email') or '(no email)',
                        'when': r.get('next_scan_at'),
                    })
                    data['counts']['domain_overdue'] += 1
            except Exception:
                pass

            # 3. Stalled onboarding (USER) - active, SCHEDULED domain created > 48h ago
            # with no completed scan run ever. 48h grace so a mid-first-scan domain is not
            # flagged. Excludes scan_frequency='manual': a manual domain that hasn't scanned
            # is a deliberate owner choice, not a stalled setup needing CS outreach.
            try:
                cursor.execute(
                    "SELECT d.id, d.domain, d.created_at, u.user_email, u.display_name "
                    "FROM wp_citemetrix_domains d "
                    "LEFT JOIN wp_users u ON d.user_id = u.ID "
                    "WHERE d.status = 'active' "
                    "  AND d.scan_frequency != 'manual' "
                    "  AND d.created_at < DATE_SUB(NOW(), INTERVAL 48 HOUR) "
                    "  AND NOT EXISTS ( "
                    "      SELECT 1 FROM wp_citemetrix_scan_runs sr "
                    "      WHERE sr.domain_id = d.id AND sr.completed_at IS NOT NULL ) "
                    "ORDER BY d.created_at ASC"
                )
                for r in cursor.fetchall():
                    data['alerts'].append({
                        'type': 'stalled_onboarding',
                        'fault': 'user',
                        'disposition': 'REACH OUT — likely needs CS to walk them through setup',
                        'problem': f"{r['domain']} added {r['created_at']} but has never completed a scan",
                        'account': r.get('display_name') or '(no name)',
                        'email': r.get('user_email') or '(no email)',
                        'when': r.get('created_at'),
                    })
                    data['counts']['stalled_onboarding'] += 1
            except Exception:
                pass

    except Exception as e:
        data['error'] = str(e)

    return data


@app.route('/cs-alerts')
@login_required
@role_required('admin', 'support')
def cs_alerts():
    return render_template('cs_alerts/index.html', data=_compute_cs_alerts())






@app.route('/competition')

@login_required
@role_required('admin',)

def competition():

    data = {'competitors': [], 'stats': {'total': 0, 'critical': 0, 'high': 0, 'medium': 0, 'low': 0, 'watch': 0}, 'unread_alerts': 0}

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT id, company_name, company_url, tagline, competitive_score, threat_level, funding_total, employee_count, platforms_supported, last_scanned_at FROM wp_citemetrix_competition WHERE status = 'active' ORDER BY competitive_score DESC")

            data['competitors'] = cursor.fetchall()

            data['stats']['total'] = len(data['competitors'])

            for c in data['competitors']:

                level = c['threat_level'] or 'medium'

                if level in data['stats']:

                    data['stats'][level] += 1

            cursor.execute("SELECT COUNT(*) as c FROM wp_citemetrix_competition_alerts WHERE is_read = 0")

            data['unread_alerts'] = cursor.fetchone()['c']

        conn.close()

    except Exception as e:

        data['error'] = str(e)

    return render_template('competition/index.html', data=data)



@app.route('/api/competition/<int:id>')

@login_required
@role_required('admin',)

def api_competition_detail(id):

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT * FROM wp_citemetrix_competition WHERE id = %s", (id,))

            c = cursor.fetchone()

            if not c:

                return jsonify({'error': 'Not found'}), 404

            for field in ['pricing_summary', 'platforms_supported', 'key_features', 'swot_analysis', 'strategy_recommendations', 'feature_parity']:

                c[field] = parse_json_field(c.get(field))

            for field in ['last_scanned_at', 'created_at', 'updated_at']:

                if c.get(field):

                    c[field] = c[field].strftime('%Y-%m-%d %H:%M')

            cursor.execute("SELECT change_type, change_summary, severity, detected_at FROM wp_citemetrix_competition_history WHERE competitor_id = %s ORDER BY detected_at DESC LIMIT 50", (id,))

            history = cursor.fetchall()

            for h in history:

                if h.get('detected_at'):

                    h['detected_at'] = h['detected_at'].strftime('%Y-%m-%d %H:%M')

            c['history'] = history

        conn.close()

        return jsonify(c)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/competition/alerts')

@login_required
@role_required('admin',)

def api_competition_alerts():

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT a.*, c.company_name FROM wp_citemetrix_competition_alerts a JOIN wp_citemetrix_competition c ON a.competitor_id = c.id ORDER BY a.is_read ASC, a.created_at DESC LIMIT 100")

            alerts = cursor.fetchall()

            for a in alerts:

                if a.get('created_at'):

                    a['created_at'] = a['created_at'].strftime('%Y-%m-%d %H:%M')

        conn.close()

        return jsonify(alerts)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/competition/history')

@login_required
@role_required('admin',)

def api_competition_history():

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT h.*, c.company_name FROM wp_citemetrix_competition_history h JOIN wp_citemetrix_competition c ON h.competitor_id = c.id ORDER BY h.detected_at DESC LIMIT 100")

            history = cursor.fetchall()

            for h in history:

                if h.get('detected_at'):

                    h['detected_at'] = h['detected_at'].strftime('%Y-%m-%d %H:%M')

        conn.close()

        return jsonify(history)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/competition/roadmap')

@login_required
@role_required('admin',)

def api_competition_roadmap():

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT * FROM wp_citemetrix_competition_roadmap ORDER BY priority_score DESC")

            items = cursor.fetchall()

            for item in items:

                item['source_competitors'] = parse_json_field(item.get('source_competitors'))

                if item.get('generated_at'):

                    item['generated_at'] = item['generated_at'].strftime('%Y-%m-%d %H:%M')

                if item.get('updated_at'):

                    item['updated_at'] = item['updated_at'].strftime('%Y-%m-%d %H:%M')

        conn.close()

        return jsonify(items)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/investors')

@login_required
@role_required('admin',)

def api_investors():

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT * FROM wp_citemetrix_investors WHERE status = 'active' ORDER BY fit_score DESC")

            investors = cursor.fetchall()

            for inv in investors:

                inv['portfolio_json'] = parse_json_field(inv.get('portfolio_json'))

                inv['contact_json'] = parse_json_field(inv.get('contact_json'))

                inv['key_partners'] = parse_json_field(inv.get('key_partners'))

                if inv.get('researched_at'):

                    inv['researched_at'] = inv['researched_at'].strftime('%Y-%m-%d %H:%M')

                if inv.get('created_at'):

                    inv['created_at'] = inv['created_at'].strftime('%Y-%m-%d %H:%M')

        conn.close()

        return jsonify(investors)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/investors/<int:id>')

@login_required
@role_required('admin',)

def api_investor_detail(id):

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT * FROM wp_citemetrix_investors WHERE id = %s", (id,))

            inv = cursor.fetchone()

            if not inv:

                return jsonify({'error': 'Not found'}), 404

            inv['portfolio_json'] = parse_json_field(inv.get('portfolio_json'))

            inv['contact_json'] = parse_json_field(inv.get('contact_json'))

            inv['key_partners'] = parse_json_field(inv.get('key_partners'))

            for field in ['researched_at', 'created_at', 'contacted_at']:

                if inv.get(field):

                    inv[field] = inv[field].strftime('%Y-%m-%d %H:%M')

        conn.close()

        return jsonify(inv)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



# ─────────────────────────────────────────────────────────────────────────────
# Marketing analytics — citemetrix.com's own GA4 + Search Console.
#
# Data is fetched live from the WP plugin's marketing API endpoint
# (REST: /wp-json/citemetrix/v1/marketing-data), which in turn calls
# the Google Analytics Data API and Search Console API using the
# stored OAuth tokens for the citemetrix.com owner. We don't sync or
# warehouse this data — every page load is a fresh API hit, gated by
# a short in-memory cache so consecutive loads (e.g. when refining
# the date range) don't hammer Google.
#
# Auth between the admin portal and the plugin is a shared secret
# header — env var MARKETING_API_SECRET on this side, wp_options key
# citemetrix_marketing_secret on the plugin side. The two must match.
# ─────────────────────────────────────────────────────────────────────────────

# In-memory cache keyed by (range_days). Tuple of (expires_at, payload).
_marketing_cache = {}
_MARKETING_CACHE_TTL_SECS = 300  # 5 minutes


def _fetch_marketing_data(days):
    """
    Call the plugin's marketing API and return the parsed payload, or a
    dict with an 'error' key on failure. Cached in memory for 5 minutes
    per range_days.
    """
    import time as _time
    cache_key = int(days)
    cached = _marketing_cache.get(cache_key)
    if cached and cached[0] > _time.time():
        return cached[1]

    api_url = os.getenv(
        'MARKETING_API_URL',
        'https://citemetrix.com/wp-json/citemetrix/v1/marketing-data'
    )
    secret = os.getenv('MARKETING_API_SECRET', '')

    if not secret:
        return {
            'error': 'MARKETING_API_SECRET is not set in admin portal .env. '
                     'Set it to match wp_options.citemetrix_marketing_secret.',
        }

    try:
        resp = requests.get(
            api_url,
            params={'days': days},
            headers={'X-CiteMetrix-Marketing-Secret': secret},
            timeout=20,
        )
    except requests.RequestException as e:
        return {'error': f'Could not reach marketing API: {e}'}

    if resp.status_code != 200:
        # Surface the upstream error body when possible — useful for
        # diagnosing 401 (bad secret) vs 503 (config missing) vs 502
        # (Google API error).
        try:
            body = resp.json()
        except ValueError:
            body = {'message': resp.text[:200]}
        return {
            'error': f'Marketing API returned {resp.status_code}',
            'detail': body,
        }

    try:
        payload = resp.json()
    except ValueError:
        return {'error': 'Marketing API returned non-JSON response.'}

    _marketing_cache[cache_key] = (_time.time() + _MARKETING_CACHE_TTL_SECS, payload)
    return payload


@app.route('/marketing')
@login_required
@role_required('admin', 'marketing')
def marketing():
    """
    Marketing analytics dashboard for citemetrix.com.

    Shows GA4 traffic + Search Console performance for the configured
    property/site, with top sources, top landing pages, and top search
    queries. All data is fetched live from Google via the WP plugin
    proxy — no admin-portal-side sync.

    Range is configurable via ?days= query param (default 28, max 90).
    """
    return redirect(url_for('campaign_effectiveness'))  # retired: old /marketing -> Overview tab
    days = max(1, min(90, int(request.args.get('days', 28))))
    data = _fetch_marketing_data(days)
    return render_template('marketing/index.html', data=data, days=days)


# ─────────────────────────────────────────────────────────────────────────────
# Investor Requests — view and act on investor application records that come
# in through the public /investors/ form on citemetrix.com.
#
# The data lives in wp_cm_investor_applications on the plugin RDS database;
# this admin portal calls the plugin's REST API at
# /wp-json/citemetrix/v1/investor-applications/* rather than touching the
# table directly. That keeps the BoldSign + Google Drive integration in the
# plugin (where it already works) and means admin-portal's Python doesn't
# need to learn how to talk to either external service.
#
# Auth uses the same shared secret as the marketing API — env var
# MARKETING_API_SECRET — to avoid making the operator track two credentials
# for what is effectively one trust boundary (admin portal → plugin).
# ─────────────────────────────────────────────────────────────────────────────


def _investor_api_request(method, path, payload=None, params=None, timeout=30):
    """
    Make an authenticated request to the plugin's investor portal API.
    Returns either the parsed JSON body or a dict with an 'error' key
    on failure. Larger timeout than marketing (30s vs 20s) because the
    grant-access action calls Google Drive sharing, which can be slow.
    """
    base_url = os.getenv(
        'INVESTOR_API_URL',
        'https://citemetrix.com/wp-json/citemetrix/v1/investor-applications'
    )
    secret = os.getenv('MARKETING_API_SECRET', '')
    if not secret:
        return {'error': 'MARKETING_API_SECRET not set in admin portal .env.'}

    url = base_url + path
    headers = {'X-CiteMetrix-Marketing-Secret': secret}

    try:
        resp = requests.request(
            method, url,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        return {'error': f'Could not reach investor API: {e}'}

    try:
        body = resp.json()
    except ValueError:
        body = {'message': resp.text[:200]}

    if resp.status_code >= 400:
        return {
            'error': body.get('error') or body.get('message') or f'HTTP {resp.status_code}',
            'status': resp.status_code,
            'detail': body,
        }
    return body


def _investor_status_label(status):
    """
    Translate raw status codes into human-readable labels + a CSS class
    for badge coloring. Keeps the template simple — no big inline if-tree.
    """
    mapping = {
        'pending':         ('Pending',         'badge-warning'),
        'nda_sent':        ('NDA Sent',        'badge-info'),
        'nda_in_progress': ('NDA In Progress', 'badge-info'),
        'nda_failed':      ('NDA Failed',      'badge-danger'),
        'nda_completed':   ('NDA Completed',   'badge-success'),
        'access_granted':  ('Access Granted',  'badge-success'),
        'access_revoked':  ('Access Revoked',  'badge-danger'),
    }
    label, css = mapping.get(status or '', (status or 'Unknown', 'badge-secondary'))
    return {'label': label, 'css': css}


@app.route('/investor-requests')
@login_required
@role_required('admin',)
def investor_requests():
    """
    List view of all investor applications. Optionally filtered by
    status via ?status= (e.g. ?status=nda_completed for the queue of
    requests waiting on Eric to grant Drive access).
    """
    status_filter = request.args.get('status', '')
    result = _investor_api_request('GET', '', params={
        'status': status_filter,
        'limit': 200,
    })
    error = result.get('error') if isinstance(result, dict) else None
    applications = (result.get('applications') or []) if not error else []

    # Enrich each row with rendered status badge so the template stays markup-only.
    for app_row in applications:
        app_row['_status_badge'] = _investor_status_label(app_row.get('status'))

    return render_template(
        'investor_requests/list.html',
        applications=applications,
        error=error,
        status_filter=status_filter,
    )


@app.route('/investor-requests/<int:app_id>')
@login_required
@role_required('admin',)
def investor_request_detail(app_id):
    """
    Detail view for a single investor application. Shows the full
    record, BoldSign signing timeline, and action buttons (grant
    access / revoke / resend NDA / update notes).
    """
    result = _investor_api_request('GET', f'/{app_id}')
    if isinstance(result, dict) and result.get('error'):
        return render_template(
            'investor_requests/detail.html',
            application=None,
            error=result.get('error'),
            error_detail=result.get('detail'),
        )

    application = result
    application['_status_badge'] = _investor_status_label(application.get('status'))

    # Determine which actions are available based on current status.
    # The point is to NOT show "Grant Access" before NDA is fully signed,
    # and not show "Revoke" before access has been granted.
    status = application.get('status', '')
    actions = {
        'can_grant':  status in ('nda_completed',),
        'can_revoke': status in ('access_granted',),
        'can_resend': status in ('pending', 'nda_failed', 'nda_sent', 'nda_in_progress'),
    }

    return render_template(
        'investor_requests/detail.html',
        application=application,
        actions=actions,
        error=None,
    )


@app.route('/investor-requests/<int:app_id>/grant-access', methods=['POST'])
@login_required
@role_required('admin',)
def investor_request_grant_access(app_id):
    """
    Trigger the Drive-share workflow on the plugin side. The plugin
    handles the Google Drive API call, status update, and notification
    email — we just kick it off and surface the result.
    """
    result = _investor_api_request('POST', f'/{app_id}/grant-access')
    if result.get('error'):
        flash(f'Grant failed: {result["error"]}', 'error')
    else:
        flash('Access granted. Investor has been emailed the Drive link.', 'success')
    return redirect(url_for('investor_request_detail', app_id=app_id))


@app.route('/investor-requests/<int:app_id>/revoke-access', methods=['POST'])
@login_required
@role_required('admin',)
def investor_request_revoke_access(app_id):
    result = _investor_api_request('POST', f'/{app_id}/revoke-access')
    if result.get('error'):
        flash(f'Revoke failed: {result["error"]}', 'error')
    else:
        flash('Access revoked.', 'success')
    return redirect(url_for('investor_request_detail', app_id=app_id))


@app.route('/investor-requests/<int:app_id>/resend-nda', methods=['POST'])
@login_required
@role_required('admin',)
def investor_request_resend_nda(app_id):
    result = _investor_api_request('POST', f'/{app_id}/resend-nda')
    if result.get('error'):
        flash(f'Resend failed: {result["error"]}', 'error')
    else:
        flash('NDA resent. Investor will receive a fresh BoldSign link.', 'success')
    return redirect(url_for('investor_request_detail', app_id=app_id))


@app.route('/investor-requests/<int:app_id>/notes', methods=['POST'])
@login_required
@role_required('admin',)
def investor_request_update_notes(app_id):
    notes = request.form.get('notes', '')
    result = _investor_api_request('PUT', f'/{app_id}/notes', payload={'notes': notes})
    if result.get('error'):
        flash(f'Notes save failed: {result["error"]}', 'error')
    else:
        flash('Notes saved.', 'success')
    return redirect(url_for('investor_request_detail', app_id=app_id))


@app.route('/revenue')
@login_required
@role_required('admin', 'sales')
def revenue():
    """
    Revenue / business metrics dashboard.

    Adapts to the lifecycle stage of the business: before May 1 the page
    shows pre-launch projections (committed users, projected MRR), after
    May 1 it shifts to live MRR from active paid subscriptions.

    Source-of-truth notes:
      - Current subscription state (who's active, who's pending-cancel,
        who's paused, etc.) comes from wp_wc_orders directly. That table
        IS the live state of every WC subscription. wp_usermeta is joined
        for tier (citemetrix_tier meta key, set by the plugin on every
        subscription activation).
      - The wp_citemetrix_subscription_events table is queried only for
        history (lifetime event counts, 30-day activity). It's an
        append-only audit log of subscription state changes, not a
        current-state source.

    Both halves correctly show 0/empty pre-launch, populate naturally
    post-May 1, and are not vulnerable to the previous lifecycle-tracker
    cascade bug (fixed in plugin 3.13.47, data cleaned 2026-04-26).
    """
    data = {
        'stats': {},
        'by_tier': {},
        'events_by_type': {},
        'events_30d': {},
        'acquisition': [],
        'api_costs': {},
        'pre_launch': True,  # flips to False once paid subscriptions exist
        'beta_conversion': {},
    }

    # Tier pricing — single source of truth on the dashboard side.
    # Memory #3: Starter $79 ($55 BETA30), Pro $199 ($139), Agency $499 ($349).
    TIER_PRICES_FULL = {
        'starter':      79,
        'professional': 199,
        'pro':          199,  # alias seen in some payloads
        'agency':       499,
        'enterprise':   999,
    }
    TIER_PRICES_DISCOUNTED = {
        'starter':      55,
        'professional': 139,
        'pro':          139,
        'agency':       349,
        'enterprise':   699,  # estimated — no enterprise discount documented
    }

    try:
        conn = get_db()
        with conn.cursor() as cursor:

            # ── Total event counts (lifetime) ─────────────────────────
            # Useful for context but NOT used for MRR. Each event type
            # gets its own count regardless of who triggered it.
            cursor.execute("""
                SELECT event_type, COUNT(*) AS c
                FROM wp_citemetrix_subscription_events
                GROUP BY event_type
            """)
            for row in cursor.fetchall():
                data['events_by_type'][row['event_type']] = row['c']

            # ── Last 30d event activity ───────────────────────────────
            # The "what's happening lately" view. Different from lifetime
            # counts because we want to know if cancellations are
            # accelerating or new signups are flowing.
            cursor.execute("""
                SELECT event_type, COUNT(*) AS c
                FROM wp_citemetrix_subscription_events
                WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)
                GROUP BY event_type
            """)
            for row in cursor.fetchall():
                data['events_30d'][row['event_type']] = row['c']

            # ── Current state per user ────────────────────────────────
            # Source of truth: wp_wc_orders (WooCommerce HPOS subscription
            # storage) joined with wp_usermeta for the citemetrix_tier
            # meta key, which the plugin sets on every subscription
            # activation. The events table (queried above for history) is
            # NOT the right source for current state — it logs changes
            # over time and was intentionally truncated by the 3.13.47
            # data cleanup. wp_wc_orders is what actually holds the live
            # subscription record.
            #
            # Three buckets, all distinct-by-user:
            #   • committed   = has at least one wc-active subscription
            #   • non_active  = no wc-active sub, but has a sub in
            #                   pending-cancel, on-hold, cancelled, or
            #                   expired (i.e. they're "off" right now)
            #
            # The committed query joins tier meta so we can build the
            # by-tier breakdown in the same pass.
            cursor.execute("""
                SELECT DISTINCT
                    o.customer_id AS user_id,
                    COALESCE(LOWER(NULLIF(um.meta_value, '')), 'unknown') AS tier
                FROM wp_wc_orders o
                LEFT JOIN wp_usermeta um
                  ON um.user_id = o.customer_id
                 AND um.meta_key = 'citemetrix_tier'
                WHERE o.type = 'shop_subscription'
                  AND o.status = 'wc-active'
            """)
            active_users = cursor.fetchall()

            cursor.execute("""
                SELECT
                    CASE
                        WHEN o.status = 'wc-pending-cancel' THEN 'pending_cancel'
                        WHEN o.status = 'wc-on-hold'        THEN 'paused'
                        WHEN o.status IN ('wc-cancelled', 'wc-expired') THEN 'cancelled'
                    END AS state,
                    COUNT(DISTINCT o.customer_id) AS users
                FROM wp_wc_orders o
                WHERE o.type = 'shop_subscription'
                  AND o.status IN ('wc-pending-cancel', 'wc-on-hold', 'wc-cancelled', 'wc-expired')
                  AND o.customer_id NOT IN (
                      SELECT DISTINCT customer_id
                      FROM wp_wc_orders
                      WHERE type = 'shop_subscription'
                        AND status = 'wc-active'
                  )
                GROUP BY state
            """)
            non_active_states = {row['state']: row['users'] for row in cursor.fetchall()}

            # Aggregate active users into headline numbers
            committed_users = 0       # active or committed to pay
            paid_subscribers = 0      # on a real paid tier (post-launch indicator)
            tier_counts = {}          # current-state tier → count

            for row in active_users:
                tier = row['tier']
                committed_users += 1
                tier_counts[tier] = tier_counts.get(tier, 0) + 1
                # Paid means tier is one of the real paid tiers (not beta)
                if tier in TIER_PRICES_FULL and tier != 'beta':
                    paid_subscribers += 1

            pending_cancel  = non_active_states.get('pending_cancel', 0)
            paused_users    = non_active_states.get('paused', 0)
            cancelled_users = non_active_states.get('cancelled', 0)

            # Pre-launch flag: are there any paid subscribers? If not,
            # we're in pre-launch mode and should show projections rather
            # than imaginary MRR.
            data['pre_launch'] = (paid_subscribers == 0)

            # ── MRR calculations ──────────────────────────────────────
            # In pre-launch mode: MRR is $0 (no one has been billed).
            # Projected MRR exists for users who have committed to a paid
            # tier — but in our actual data, all to_tier values are 'beta'
            # right now, so we can't project a per-user dollar amount yet.
            #
            # Once tier values shift to starter/pro/agency post-launch,
            # the same code computes real MRR by summing tier prices
            # for every committed user.
            mrr_full = 0
            mrr_discounted = 0
            for tier, count in tier_counts.items():
                full_price       = TIER_PRICES_FULL.get(tier, 0)
                discounted_price = TIER_PRICES_DISCOUNTED.get(tier, 0)
                mrr_full       += count * full_price
                mrr_discounted += count * discounted_price

                data['by_tier'][tier] = {
                    'count':           count,
                    'mrr_full':        count * full_price,
                    'mrr_discounted':  count * discounted_price,
                }

            # ── 30d activity: API users ───────────────────────────────
            # "Active CiteMetrix users in last 30d" — anyone who made an
            # API call. Distinct from "subscribers" because not every
            # subscriber is actively scanning, and not every active user
            # has subscribed yet.
            try:
                cursor.execute("""
                    SELECT COUNT(DISTINCT user_id) AS active_users
                    FROM wp_citemetrix_api_usage
                    WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)
                """)
                active_30d = cursor.fetchone()['active_users'] or 0
            except (pymysql.MySQLError, pymysql.OperationalError):
                active_30d = 0

            # ── Beta conversion funnel ────────────────────────────────
            # Total beta signups vs how many actually moved to a paid tier.
            #
            # Source of truth for "converted" is wp_wc_orders + wp_usermeta:
            # a user has converted iff they currently have an active WC
            # subscription AND their citemetrix_tier meta is one of the
            # paid tiers (starter, pro/professional, agency, enterprise).
            #
            # Today (pre-launch, beta active) this is 0. Post-May 1 as
            # users upgrade to paid plans the count grows naturally.
            cursor.execute("""
                SELECT COUNT(*) AS total
                FROM wp_citemetrix_beta_signups
            """)
            beta_signups_total = cursor.fetchone()['total'] or 0

            # Count distinct users currently on a paid tier — their
            # citemetrix_tier meta is starter/pro/professional/agency/
            # enterprise AND they have an active WC subscription. Same
            # source-of-truth pattern as the by-tier breakdown above.
            cursor.execute("""
                SELECT COUNT(DISTINCT o.customer_id) AS converted
                FROM wp_wc_orders o
                JOIN wp_usermeta um
                  ON um.user_id = o.customer_id
                 AND um.meta_key = 'citemetrix_tier'
                WHERE o.type = 'shop_subscription'
                  AND o.status = 'wc-active'
                  AND LOWER(um.meta_value) IN ('starter', 'pro', 'professional', 'agency', 'enterprise')
            """)
            beta_converted_users = cursor.fetchone()['converted'] or 0

            data['beta_conversion'] = {
                'total_signups':    beta_signups_total,
                'converted':        beta_converted_users,
                'conversion_rate':  round((beta_converted_users / beta_signups_total) * 100, 1) if beta_signups_total > 0 else 0,
            }

            # ── Headline stats ────────────────────────────────────────
            data['stats'] = {
                # Pre-launch headline numbers
                'committed_users':     committed_users,
                'pending_cancel':      pending_cancel,
                'paused':              paused_users,
                'cancelled':           cancelled_users,
                'paid_subscribers':    paid_subscribers,

                # MRR numbers — both flavors. In pre-launch these are 0.
                # Post-launch, mrr_full = contracted MRR, mrr_discounted
                # = actual cash MRR while BETA30 discount is in effect.
                'mrr_full':            mrr_full,
                'mrr_discounted':      mrr_discounted,
                'arr_full':            mrr_full * 12,

                # Activity
                'active_users_30d':    active_30d,
                # new_30d intentionally does NOT include beta_converted events
                # — they're polluted by the lifecycle tracker cascade bug
                # (see beta_conversion comment above). Counts only real new
                # subscriptions.
                'new_30d':             data['events_30d'].get('new_subscription', 0),
                'cancellations_30d':   data['events_30d'].get('cancelled', 0)
                                     + data['events_30d'].get('pending_cancel', 0),
            }
            data['stats']['net_new_30d'] = data['stats']['new_30d'] - data['stats']['cancellations_30d']

            # ── API costs (kept from previous version) ───────────────
            # Sum tokens and calls over the last 30d. Cost estimate is
            # rough — actual costs vary per platform per token.
            cursor.execute("""
                SELECT SUM(tokens_used) AS tokens, COUNT(*) AS calls
                FROM wp_citemetrix_api_usage
                WHERE created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 30 DAY)
            """)
            cost_row = cursor.fetchone()
            total_tokens   = cost_row['tokens'] or 0
            total_calls    = cost_row['calls']  or 0
            estimated_cost = (total_tokens / 1000000) * 3  # rough $3/M average

            data['api_costs'] = {
                'total_tokens_30d':    total_tokens,
                'total_calls_30d':     total_calls,
                'estimated_cost_30d':  round(estimated_cost, 2),
            }

            # ── Acquisition by source (kept from previous version) ───
            cursor.execute("""
                SELECT
                    CASE
                        WHEN utm_source IS NULL OR utm_source = '' THEN 'direct'
                        ELSE utm_source
                    END AS source,
                    COUNT(*) AS signups,
                    SUM(CASE WHEN status = 'approved' THEN 1 ELSE 0 END) AS approved
                FROM wp_citemetrix_beta_signups
                GROUP BY CASE
                    WHEN utm_source IS NULL OR utm_source = '' THEN 'direct'
                    ELSE utm_source
                END
                ORDER BY signups DESC
                LIMIT 10
            """)
            data['acquisition'] = cursor.fetchall()

        conn.close()

    except Exception as e:
        data['error'] = str(e)
        app.logger.exception("revenue route failed")

    return render_template('revenue/index.html', data=data)




@app.route('/beta')

@login_required
@role_required('admin', 'sales')

def beta():

    data = {'signups': [], 'stats': {}, 'campaigns': [], 'recent_sends': [], 'paid_users': []}

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT status, COUNT(*) as c FROM wp_citemetrix_beta_signups GROUP BY status")

            for row in cursor.fetchall():

                data['stats'][row['status'] or 'unknown'] = row['c']

            data['stats']['total'] = sum(data['stats'].values())

            cursor.execute("""

                SELECT id, email, name, company, role, status, utm_source, 

                       created_at, approved_at, first_scan_at, converted_at

                FROM wp_citemetrix_beta_signups 

                ORDER BY created_at DESC LIMIT 100

            """)

            data['signups'] = cursor.fetchall()

            cursor.execute("""

                SELECT 

                    u.ID as user_id, u.user_email, u.display_name,

                    (SELECT to_tier FROM wp_citemetrix_subscription_events WHERE user_id = u.ID ORDER BY created_at DESC LIMIT 1) as current_tier,

                    (SELECT created_at FROM wp_citemetrix_subscription_events WHERE user_id = u.ID AND event_type IN ('new_subscription', 'beta_converted') ORDER BY created_at ASC LIMIT 1) as subscribed_at,

                    (SELECT COUNT(*) FROM wp_citemetrix_domains WHERE user_id = u.ID AND status = 'active') as domain_count,

                    (SELECT COUNT(*) FROM wp_citemetrix_api_usage WHERE user_id = u.ID) as total_api_calls

                FROM wp_users u

                WHERE u.ID IN (SELECT DISTINCT user_id FROM wp_citemetrix_subscription_events WHERE event_type IN ('new_subscription', 'beta_converted'))

                ORDER BY subscribed_at DESC

            """)

            data['paid_users'] = cursor.fetchall()

            cursor.execute("""

                SELECT c.*, 

                    (SELECT COUNT(*) FROM wp_citemetrix_drip_steps WHERE campaign_id = c.id) as step_count,

                    (SELECT COUNT(*) FROM wp_citemetrix_drip_enrollments WHERE campaign_id = c.id AND status = 'active') as active_enrollments,

                    (SELECT COUNT(*) FROM wp_citemetrix_drip_enrollments WHERE campaign_id = c.id) as total_enrollments 

                FROM wp_citemetrix_drip_campaigns c ORDER BY c.id

            """)

            data['campaigns'] = cursor.fetchall()

            cursor.execute("SELECT s.*, st.subject as step_subject, st.step_order FROM wp_citemetrix_drip_sends s JOIN wp_citemetrix_drip_steps st ON s.step_id = st.id ORDER BY s.sent_at DESC LIMIT 20")

            data['recent_sends'] = cursor.fetchall()

        conn.close()

    except Exception as e:

        data['error'] = str(e)

    return render_template('beta/index.html', data=data)



@app.route('/settings')

@login_required

@role_required('admin')

def settings():

    return render_template('settings.html')



@app.route('/api/settings/keys', methods=['GET'])

@login_required

@role_required('admin')

def api_get_keys():

    try:

        conn = get_admin_db()

        with conn.cursor() as cursor:

            cursor.execute("SELECT setting_key, LENGTH(setting_value) > 0 as configured, updated_at FROM settings WHERE setting_key IN ('anthropic_api_key', 'perplexity_api_key')")

            rows = cursor.fetchall()

        conn.close()

        keys = {row['setting_key']: {'configured': bool(row['configured']), 'updated_at': row['updated_at'].strftime('%Y-%m-%d %H:%M') if row['updated_at'] else None} for row in rows}

        return jsonify(keys)

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/settings/keys', methods=['POST'])

@login_required

@role_required('admin')

def api_save_keys():

    try:

        data = request.get_json()

        conn = get_admin_db()

        with conn.cursor() as cursor:

            for key_name in ['anthropic_api_key', 'perplexity_api_key']:

                if key_name in data and data[key_name]:

                    cursor.execute("INSERT INTO settings (setting_key, setting_value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE setting_value = VALUES(setting_value)", (key_name, data[key_name].strip()))

        conn.commit()

        conn.close()

        return jsonify({'success': True})

    except Exception as e:

        return jsonify({'error': str(e)}), 500



# -----------------------------------------------------------------------------
# Competition research/rescan - async background jobs
#
# The research + rescan pipeline (Perplexity + website fetch + Claude analysis)
# takes 2-3 minutes. Running it inline in the request made the UI look dead and
# let a page reload abort it (nginx 499). These endpoints now enqueue a job,
# return immediately, and run the pipeline on a background thread. Job status is
# stored in the DB (cm_competition_jobs) so any gunicorn worker can serve the
# poll, and the job survives a client reload.
# -----------------------------------------------------------------------------
import threading


def ensure_competition_jobs_table():
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cm_competition_jobs (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    job_type VARCHAR(20) NOT NULL,
                    competitor_id INT DEFAULT NULL,
                    name VARCHAR(255) DEFAULT NULL,
                    url VARCHAR(512) DEFAULT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'queued',
                    phase VARCHAR(180) DEFAULT NULL,
                    error TEXT DEFAULT NULL,
                    result_json LONGTEXT DEFAULT NULL,
                    created_by INT DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY idx_status (status),
                    KEY idx_competitor (competitor_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[competition_jobs] table ensure failed: {e}")


ensure_competition_jobs_table()


def _competition_job_update(job_id, **fields):
    if not fields:
        return
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            sets = ', '.join(f"{k} = %s" for k in fields)
            cur.execute(f"UPDATE cm_competition_jobs SET {sets} WHERE id = %s",
                        list(fields.values()) + [job_id])
        conn.commit()
    finally:
        conn.close()


def _run_competition_job(job_id, job_type, competitor_id, name, url):
    prod_conn = None
    admin_conn = None
    try:
        _competition_job_update(job_id, status='running', phase='Starting...')
        from competition import rescan_competitor, research_competitor
        prod_conn = get_db()
        admin_conn = get_admin_db()

        def progress(msg):
            _competition_job_update(job_id, phase=str(msg)[:180])

        with admin_conn.cursor() as admin_cursor:
            if job_type == 'rescan':
                result = rescan_competitor(prod_conn, admin_cursor, competitor_id, progress=progress)
            else:
                result = research_competitor(prod_conn, admin_cursor, name, url, progress=progress)

        if isinstance(result, dict) and 'error' in result:
            _competition_job_update(job_id, status='error', phase='Failed',
                                    error=str(result['error'])[:2000])
        else:
            changes = result.get('changes') if isinstance(result, dict) else None
            _competition_job_update(job_id, status='done', phase='Complete',
                                    result_json=json.dumps(changes, default=str)[:60000])
    except Exception as e:
        import traceback
        traceback.print_exc()
        _competition_job_update(job_id, status='error', phase='Failed', error=str(e)[:2000])
    finally:
        for c in (prod_conn, admin_conn):
            try:
                if c:
                    c.close()
            except Exception:
                pass


@app.route('/api/competition/research', methods=['POST'])
@login_required
@role_required('admin')
def api_competition_research():
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        url = data.get('url', '').strip()
        if not url:
            return jsonify({'error': 'URL is required'}), 400
        if not url.startswith('http'):
            url = 'https://' + url
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cm_competition_jobs (job_type, name, url, status, created_by)
                           VALUES ('research', %s, %s, 'queued', %s)""",
                        (name, url, getattr(current_user, 'id', None)))
            job_id = cur.lastrowid
        conn.commit()
        conn.close()
        threading.Thread(target=_run_competition_job,
                         args=(job_id, 'research', None, name, url), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'queued'}), 202
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/competition/rescan/<int:id>', methods=['POST'])
@login_required
@role_required('admin')
def api_competition_rescan(id):
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cm_competition_jobs (job_type, competitor_id, status, created_by)
                           VALUES ('rescan', %s, 'queued', %s)""",
                        (id, getattr(current_user, 'id', None)))
            job_id = cur.lastrowid
        conn.commit()
        conn.close()
        threading.Thread(target=_run_competition_job,
                         args=(job_id, 'rescan', id, None, None), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'queued'}), 202
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# -----------------------------------------------------------------------------
# Investor Intelligence — research/suggest jobs + CRUD (mirrors competition jobs).
# Ported from WP class-citemetrix-investors.php. Research runs off the request
# thread; the frontend polls /api/investors/job/<id>.
# -----------------------------------------------------------------------------
def ensure_investor_jobs_table():
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cm_investor_jobs (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    job_type VARCHAR(20) NOT NULL,
                    investor_id INT DEFAULT NULL,
                    firm_name VARCHAR(255) DEFAULT NULL,
                    website VARCHAR(512) DEFAULT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'queued',
                    phase VARCHAR(180) DEFAULT NULL,
                    error TEXT DEFAULT NULL,
                    result_json LONGTEXT DEFAULT NULL,
                    created_by INT DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY idx_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[investor_jobs] table ensure failed: {e}")


ensure_investor_jobs_table()


def _sweep_orphaned_background_jobs():
    """Background research/rescan jobs run as a thread inside a gunicorn
    worker process — they do NOT survive a deploy restart. If this process
    is just starting up and a job is still marked 'running' or 'queued', the
    process that was running it is dead and it will never update again.
    Runs once at import time; anything caught here is real, not a race.
    Learned 2026-08-25: a deploy restart mid-request orphaned a live
    Investor Intel research job, leaving it stuck on "Researching the firm
    across the web..." forever with no error surfaced — looked identical to
    a silent API/credit failure from the outside."""
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            for table in ('cm_investor_jobs', 'cm_competition_jobs'):
                cur.execute(
                    f"UPDATE {table} SET status='error', phase='Interrupted', "
                    f"error='Interrupted by a server deploy/restart mid-request. Not a real failure — please retry.' "
                    f"WHERE status IN ('running','queued')"
                )
                if cur.rowcount:
                    print(f"[startup] swept {cur.rowcount} orphaned job(s) in {table}")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[startup] job sweep failed: {e}")


_sweep_orphaned_background_jobs()


def _investor_job_update(job_id, **fields):
    if not fields:
        return
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            sets = ', '.join(f"{k} = %s" for k in fields)
            cur.execute(f"UPDATE cm_investor_jobs SET {sets} WHERE id = %s",
                        list(fields.values()) + [job_id])
        conn.commit()
    finally:
        conn.close()


def _run_investor_job(job_id, job_type, investor_id, firm_name, website, user_id):
    prod_conn = None
    admin_conn = None
    try:
        _investor_job_update(job_id, status='running', phase='Starting...')
        from investors import research_investor, rescan_investor, suggest_investors
        prod_conn = get_db()
        admin_conn = get_admin_db()

        def progress(msg):
            _investor_job_update(job_id, phase=str(msg)[:180])

        with admin_conn.cursor() as admin_cursor:
            if job_type == 'rescan':
                result = rescan_investor(prod_conn, admin_cursor, investor_id, user_id, progress=progress)
            elif job_type == 'suggest':
                result = suggest_investors(prod_conn, admin_cursor, user_id, progress=progress)
            else:
                result = research_investor(prod_conn, admin_cursor, firm_name, website, user_id, progress=progress)

        if isinstance(result, dict) and 'error' in result:
            _investor_job_update(job_id, status='error', phase='Failed', error=str(result['error'])[:2000])
        else:
            _investor_job_update(job_id, status='done', phase='Complete',
                                 result_json=json.dumps(result, default=str)[:200000])
    except Exception as e:
        import traceback
        traceback.print_exc()
        _investor_job_update(job_id, status='error', phase='Failed', error=str(e)[:2000])
    finally:
        for c in (prod_conn, admin_conn):
            try:
                if c:
                    c.close()
            except Exception:
                pass


@app.route('/investors')
@login_required
@role_required('admin',)
def investors():
    data = {'investors': [], 'stats': {'total': 0, 'hot': 0, 'warm': 0, 'possible': 0, 'cold': 0, 'contacted': 0}}
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("""SELECT id, firm_name, website, hq, stage_focus, check_size, thesis,
                                     fit_score, fit_label, fit_rationale, key_partners, portfolio_json,
                                     contact_json, status, contacted, contacted_at, notes, researched_at, sectors_json
                              FROM wp_citemetrix_investors WHERE status = 'active'
                              ORDER BY fit_score DESC, firm_name ASC""")
            rows = cursor.fetchall()
            for inv in rows:
                inv['portfolio'] = parse_json_field(inv.pop('portfolio_json', None)) or []
                inv['contact'] = parse_json_field(inv.pop('contact_json', None)) or {}
                inv['sectors'] = parse_json_field(inv.pop('sectors_json', None)) or []
                inv['key_partners'] = parse_json_field(inv.get('key_partners')) or []
                if inv.get('researched_at'):
                    inv['researched_at'] = inv['researched_at'].strftime('%Y-%m-%d %H:%M')
                if inv.get('contacted_at'):
                    inv['contacted_at'] = inv['contacted_at'].strftime('%b %d')
                lbl = inv.get('fit_label') or 'cold'
                if lbl in data['stats']:
                    data['stats'][lbl] += 1
                if inv.get('contacted'):
                    data['stats']['contacted'] += 1
            data['investors'] = rows
            data['stats']['total'] = len(rows)
        conn.close()
    except Exception as e:
        data['error'] = str(e)
    return render_template('investors/index.html', data=data)


@app.route('/api/investors/research', methods=['POST'])
@login_required
@role_required('admin',)
def api_investors_research():
    try:
        d = request.get_json() or {}
        firm = (d.get('firm_name') or '').strip()
        website = (d.get('website') or '').strip()
        if not firm and not website:
            return jsonify({'error': 'Enter a firm name or website.'}), 400
        user_id = getattr(current_user, 'id', None)
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cm_investor_jobs (job_type, firm_name, website, status, created_by)
                           VALUES ('research', %s, %s, 'queued', %s)""",
                        (firm, website, user_id))
            job_id = cur.lastrowid
        conn.commit(); conn.close()
        threading.Thread(target=_run_investor_job, args=(job_id, 'research', None, firm, website, user_id), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'queued'}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/rescan/<int:id>', methods=['POST'])
@login_required
@role_required('admin',)
def api_investors_rescan(id):
    try:
        user_id = getattr(current_user, 'id', None)
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cm_investor_jobs (job_type, investor_id, status, created_by)
                           VALUES ('rescan', %s, 'queued', %s)""", (id, user_id))
            job_id = cur.lastrowid
        conn.commit(); conn.close()
        threading.Thread(target=_run_investor_job, args=(job_id, 'rescan', id, None, None, user_id), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'queued'}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/suggest', methods=['POST'])
@login_required
@role_required('admin',)
def api_investors_suggest():
    try:
        user_id = getattr(current_user, 'id', None)
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cm_investor_jobs (job_type, status, created_by)
                           VALUES ('suggest', 'queued', %s)""", (user_id,))
            job_id = cur.lastrowid
        conn.commit(); conn.close()
        threading.Thread(target=_run_investor_job, args=(job_id, 'suggest', None, None, None, user_id), daemon=True).start()
        return jsonify({'job_id': job_id, 'status': 'queued'}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/job/<int:job_id>')
@login_required
@role_required('admin',)
def api_investors_job(job_id):
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, job_type, investor_id, status, phase, error, result_json
                           FROM cm_investor_jobs WHERE id = %s""", (job_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(row)


# ─────────────────────────────────────────────────────────────────────────────
# Batch investor research (upload a CSV of firm,website → research each in turn)
# ─────────────────────────────────────────────────────────────────────────────

def ensure_investors_sectors_column():
    """Add sectors_json to the prod investors table if missing (enables sector filtering later)."""
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM wp_citemetrix_investors LIKE 'sectors_json'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE wp_citemetrix_investors ADD COLUMN sectors_json LONGTEXT DEFAULT NULL")
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[investors] sectors column ensure failed: {e}")


def ensure_investor_batches_table():
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS cm_investor_batches (
                    id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                    total INT NOT NULL DEFAULT 0,
                    done INT NOT NULL DEFAULT 0,
                    failed INT NOT NULL DEFAULT 0,
                    skipped INT NOT NULL DEFAULT 0,
                    status VARCHAR(20) NOT NULL DEFAULT 'queued',
                    phase VARCHAR(200) DEFAULT NULL,
                    error TEXT DEFAULT NULL,
                    created_by INT DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    KEY idx_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        conn.commit(); conn.close()
    except Exception as e:
        print(f"[investor_batches] table ensure failed: {e}")


ensure_investors_sectors_column()
ensure_investor_batches_table()


def _batch_update(batch_id, **fields):
    if not fields:
        return
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            sets = ', '.join(f"{k} = %s" for k in fields)
            cur.execute(f"UPDATE cm_investor_batches SET {sets} WHERE id = %s",
                        list(fields.values()) + [batch_id])
        conn.commit()
    finally:
        conn.close()


def _domain_of(url):
    import re as _re
    u = (url or '').strip().lower()
    u = _re.sub(r'^https?://', '', u)
    u = _re.sub(r'^www\.', '', u)
    return u.split('/')[0].strip()


def _investor_exists(prod_conn, firm, website):
    """Dedup check: firm already researched (matched by name or website domain)."""
    dom = _domain_of(website)
    with prod_conn.cursor() as cur:
        if firm:
            cur.execute("SELECT id FROM wp_citemetrix_investors WHERE LOWER(firm_name) = LOWER(%s) LIMIT 1", (firm,))
            if cur.fetchone():
                return True
        if dom:
            cur.execute("SELECT id FROM wp_citemetrix_investors WHERE website LIKE %s LIMIT 1", (f"%{dom}%",))
            if cur.fetchone():
                return True
    return False


def _run_investor_batch(batch_id, rows, user_id):
    """Sequential batch runner. Fresh DB connections PER firm — research makes long
    HTTP calls (Perplexity + Claude), so a held-open connection could go stale."""
    try:
        from investors import research_investor
        _batch_update(batch_id, status='running', phase='Starting...')
        total = len(rows)
        done = failed = skipped = 0
        for i, (firm, website) in enumerate(rows):
            label = firm or website or f'row {i+1}'
            _batch_update(batch_id, phase=f'Researching {i+1}/{total}: {label}'[:200])
            prod_conn = None; admin_conn = None
            try:
                prod_conn = get_db()
                if _investor_exists(prod_conn, firm, website):
                    skipped += 1
                    _batch_update(batch_id, skipped=skipped)
                    continue
                admin_conn = get_admin_db()
                with admin_conn.cursor() as admin_cursor:
                    result = research_investor(prod_conn, admin_cursor, firm, website, user_id)
                if isinstance(result, dict) and 'error' in result:
                    failed += 1
                else:
                    done += 1
            except Exception:
                import traceback; traceback.print_exc()
                failed += 1
            finally:
                for c in (prod_conn, admin_conn):
                    try:
                        if c:
                            c.close()
                    except Exception:
                        pass
            _batch_update(batch_id, done=done, failed=failed, skipped=skipped)
        _batch_update(batch_id, status='done',
                      phase=f'Complete: {done} researched, {skipped} skipped, {failed} failed')
    except Exception as e:
        import traceback; traceback.print_exc()
        _batch_update(batch_id, status='error', phase='Failed', error=str(e)[:2000])


@app.route('/api/investors/batch', methods=['POST'])
@login_required
@role_required('admin',)
def api_investors_batch():
    try:
        f = request.files.get('file')
        if not f:
            return jsonify({'error': 'No CSV file uploaded.'}), 400
        import csv, io
        raw = f.read().decode('utf-8-sig', errors='replace')
        all_rows = [r for r in csv.reader(io.StringIO(raw)) if any((c or '').strip() for c in r)]
        if not all_rows:
            return jsonify({'error': 'CSV is empty.'}), 400
        first = [(c or '').strip().lower() for c in all_rows[0]]
        is_header = any(h in ('firm', 'firm name', 'firm_name', 'name', 'website', 'url', 'domain') for h in first)
        fi, wi = 0, 1
        if is_header:
            for idx, h in enumerate(first):
                if h in ('firm', 'firm name', 'firm_name', 'name'):
                    fi = idx
                if h in ('website', 'url', 'domain'):
                    wi = idx
            data_rows = all_rows[1:]
        else:
            data_rows = all_rows
        pairs = []
        for r in data_rows:
            firm = r[fi].strip() if len(r) > fi else ''
            website = r[wi].strip() if len(r) > wi else ''
            if firm or website:
                pairs.append((firm, website))
        if not pairs:
            return jsonify({'error': 'No firm rows found in the CSV.'}), 400
        CAP = 100
        capped = len(pairs) > CAP
        pairs = pairs[:CAP]
        user_id = getattr(current_user, 'id', None)
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO cm_investor_batches (total, status, phase, created_by)
                           VALUES (%s, 'queued', 'Queued', %s)""",
                        (len(pairs), user_id))
            batch_id = cur.lastrowid
        conn.commit(); conn.close()
        threading.Thread(target=_run_investor_batch, args=(batch_id, pairs, user_id), daemon=True).start()
        return jsonify({'batch_id': batch_id, 'total': len(pairs), 'capped': capped}), 202
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/batch/<int:batch_id>')
@login_required
@role_required('admin',)
def api_investors_batch_status(batch_id):
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, total, done, failed, skipped, status, phase, error
                           FROM cm_investor_batches WHERE id = %s""", (batch_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'Batch not found'}), 404
    return jsonify(row)


@app.route('/api/investors/<int:id>/update', methods=['POST'])
@login_required
@role_required('admin',)
def api_investors_update(id):
    try:
        d = request.get_json() or {}
        fields = {}
        if 'notes' in d:
            fields['notes'] = (d.get('notes') or '')[:5000]
        if 'contacted' in d:
            c = 1 if d.get('contacted') else 0
            fields['contacted'] = c
            fields['contacted_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S') if c else None
        if not fields:
            return jsonify({'error': 'Nothing to update'}), 400
        conn = get_db()
        with conn.cursor() as cur:
            sets = ', '.join(f"{k} = %s" for k in fields)
            cur.execute(f"UPDATE wp_citemetrix_investors SET {sets} WHERE id = %s", list(fields.values()) + [id])
        conn.commit(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/<int:id>/delete', methods=['POST'])
@login_required
@role_required('admin',)
def api_investors_delete(id):
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("UPDATE wp_citemetrix_investors SET status = 'archived' WHERE id = %s", (id,))
        conn.commit(); conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/<int:id>/outreach')
@login_required
@role_required('admin',)
def api_outreach_get(id):
    try:
        admin_conn = get_admin_db()
        with admin_conn.cursor() as cur:
            cur.execute("SELECT * FROM outreach_thread WHERE investor_id=%s ORDER BY id DESC LIMIT 1", (id,))
            thread = cur.fetchone()
            messages, events = [], []
            if thread:
                cur.execute("SELECT * FROM outreach_message WHERE thread_id=%s ORDER BY seq DESC, id DESC", (thread['id'],))
                messages = cur.fetchall()
                cur.execute("SELECT * FROM outreach_event WHERE thread_id=%s ORDER BY occurred_at DESC, id DESC LIMIT 20", (thread['id'],))
                events = cur.fetchall()
        admin_conn.close()
        for row in ([thread] if thread else []) + messages + events:
            for k, v in list(row.items()):
                if hasattr(v, 'strftime'):
                    row[k] = v.strftime('%Y-%m-%d %H:%M')
        return jsonify({'thread': thread, 'messages': messages, 'events': events})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/investors/<int:id>/outreach/draft', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_draft(id):
    try:
        import outreach
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM wp_citemetrix_investors WHERE id=%s", (id,))
            investor = cur.fetchone()
        conn.close()
        if not investor:
            return jsonify({'error': 'Investor not found'}), 404

        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as admin_cursor:
                thread_id = outreach.get_or_create_thread(admin_cursor, admin_conn, investor, current_user.id)
                admin_cursor.execute("SELECT channel FROM outreach_thread WHERE id=%s", (thread_id,))
                thread_channel = admin_cursor.fetchone()['channel']
                draft = outreach.draft_outreach_email(admin_cursor, investor, thread_channel)
                if 'error' in draft:
                    return jsonify({'error': draft['error']}), 500

                admin_cursor.execute("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM outreach_message WHERE thread_id=%s", (thread_id,))
                next_seq = admin_cursor.fetchone()['next_seq']
                admin_cursor.execute(
                    """INSERT INTO outreach_message (thread_id, seq, subject, body, body_ai_original, status)
                       VALUES (%s, %s, %s, %s, %s, 'draft')""",
                    (thread_id, next_seq, draft['subject'], draft['body'], draft['body'])
                )
                admin_conn.commit()
                message_id = admin_cursor.lastrowid

                admin_cursor.execute("UPDATE outreach_thread SET status='drafted' WHERE id=%s AND status='not_started'", (thread_id,))
                admin_conn.commit()

                outreach.log_event(admin_cursor, admin_conn, thread_id, 'draft_created', detail=f'seq {next_seq}', message_id=message_id)
        finally:
            admin_conn.close()

        return jsonify({'success': True, 'thread_id': thread_id, 'message_id': message_id, 'subject': draft['subject'], 'body': draft['body']})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/message/<int:message_id>/update', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_message_update(message_id):
    try:
        d = request.get_json() or {}
        admin_conn = get_admin_db()
        with admin_conn.cursor() as cur:
            cur.execute("UPDATE outreach_message SET subject=%s, body=%s WHERE id=%s AND status='draft'",
                        ((d.get('subject') or '')[:500], d.get('body') or '', message_id))
        admin_conn.commit(); admin_conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/message/<int:message_id>/approve', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_message_approve(message_id):
    try:
        import outreach
        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT * FROM outreach_message WHERE id=%s", (message_id,))
                msg = cur.fetchone()
                if not msg:
                    return jsonify({'error': 'Message not found'}), 404
                cur.execute("UPDATE outreach_message SET status='approved', approved_by=%s, approved_at=NOW() WHERE id=%s",
                            (current_user.id, message_id))
                cur.execute("UPDATE outreach_thread SET status='approved' WHERE id=%s", (msg['thread_id'],))
                admin_conn.commit()
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'approved', message_id=message_id)
        finally:
            admin_conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/message/<int:message_id>/mark-sent', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_message_mark_sent(message_id):
    """Manual mark-sent — Eric sent it himself (Gmail, LinkedIn, pasted into a
    pitch form, forwarded a warm intro). The real automated path for channel
    'email' is /send-ses below; this stays as the manual fallback for every
    channel, including email if Eric prefers to send it himself."""
    try:
        import outreach
        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT * FROM outreach_message WHERE id=%s", (message_id,))
                msg = cur.fetchone()
                if not msg:
                    return jsonify({'error': 'Message not found'}), 404
                cur.execute("UPDATE outreach_message SET status='sent', sent_at=NOW() WHERE id=%s", (message_id,))
                cur.execute("SELECT * FROM outreach_thread WHERE id=%s", (msg['thread_id'],))
                thread = cur.fetchone()
                next_at = outreach.next_business_day_offset(datetime.now(), 5)
                cur.execute(
                    "UPDATE outreach_thread SET status='sent', next_action=%s, next_action_at=%s WHERE id=%s",
                    ('Follow up if no reply', next_at.strftime('%Y-%m-%d %H:%M:%S'), msg['thread_id'])
                )
                admin_conn.commit()
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'sent', detail='manual mark-sent', message_id=message_id)
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'followup_scheduled', detail=next_at.strftime('%Y-%m-%d'))
        finally:
            admin_conn.close()

        # Keep legacy investors.contacted in sync (existing UI/stats read this field)
        if thread:
            conn = get_db()
            with conn.cursor() as cur:
                cur.execute("UPDATE wp_citemetrix_investors SET contacted=1, contacted_at=NOW() WHERE id=%s", (thread['investor_id'],))
            conn.commit(); conn.close()

        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/message/<int:message_id>/send-ses', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_message_send_ses(message_id):
    """Phase 3: real send via SES for channel='email' threads only.
    Pitch-page/LinkedIn/warm-intro stay manual (mark-sent) per spec §6 —
    those aren't things this tool should ever auto-send."""
    try:
        import outreach
        from email_helper import send_email

        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT * FROM outreach_message WHERE id=%s", (message_id,))
                msg = cur.fetchone()
                if not msg:
                    return jsonify({'error': 'Message not found'}), 404
                if msg['status'] != 'approved':
                    return jsonify({'error': 'Message must be approved before sending.'}), 400

                cur.execute("SELECT * FROM outreach_thread WHERE id=%s", (msg['thread_id'],))
                thread = cur.fetchone()
                if not thread or thread['channel'] != 'email':
                    return jsonify({'error': 'This thread\'s channel is not email — use "Mark sent" after sending it yourself.'}), 400
                if not thread['target_email']:
                    return jsonify({'error': 'No target email address on this thread.'}), 400

                sent_today = outreach.real_sends_last_24h(cur)
                if sent_today >= outreach.DAILY_SEND_CAP:
                    return jsonify({'error': f'Daily send pace cap reached ({outreach.DAILY_SEND_CAP}/24h across SES+Gmail). This is intentional — investor outreach isn\'t a campaign. Try again tomorrow.'}), 429

                attachments = outreach.get_standard_attachments()
                if len(attachments) < len(outreach.STANDARD_ATTACHMENTS):
                    missing = set(outreach.STANDARD_ATTACHMENTS) - {os.path.basename(a) for a in attachments}
                    return jsonify({'error': f'Standard attachment(s) missing on server, refusing to send without them: {", ".join(missing)}'}), 400

                body_text = outreach.build_send_body(msg['body'])
                ok, result = send_email(
                    to=thread['target_email'],
                    subject=msg['subject'],
                    body_text=body_text,
                    from_address=outreach.SES_FROM_ADDRESS,
                    configuration_set=outreach.SES_CONFIGURATION_SET,
                    attachments=attachments,
                )

                if not ok:
                    cur.execute("UPDATE outreach_message SET status='failed', error=%s WHERE id=%s", (str(result)[:500], message_id))
                    admin_conn.commit()
                    outreach.log_event(cur, admin_conn, msg['thread_id'], 'status_change', detail=f'send failed: {result}', message_id=message_id)
                    return jsonify({'error': f'SES send failed: {result}'}), 500

                next_at = outreach.next_business_day_offset(datetime.now(), 5)
                cur.execute(
                    "UPDATE outreach_message SET status='sent', send_channel='ses', sent_at=NOW(), provider_msg_id=%s WHERE id=%s",
                    (result, message_id)
                )
                cur.execute(
                    "UPDATE outreach_thread SET status='sent', next_action=%s, next_action_at=%s WHERE id=%s",
                    ('Follow up if no reply', next_at.strftime('%Y-%m-%d %H:%M:%S'), msg['thread_id'])
                )
                admin_conn.commit()
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'sent', detail=f'SES msg_id={result}', message_id=message_id)
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'followup_scheduled', detail=next_at.strftime('%Y-%m-%d'))
        finally:
            admin_conn.close()

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("UPDATE wp_citemetrix_investors SET contacted=1, contacted_at=NOW() WHERE id=%s", (thread['investor_id'],))
        conn.commit(); conn.close()

        return jsonify({'success': True, 'provider_msg_id': result})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/message/<int:message_id>/send-gmail', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_message_send_gmail(message_id):
    """Phase 2: real send via Gmail API (eric@citemetrix.com) for channel='email'
    threads only. Same approval gate and pace cap as send-ses; recommended
    default for warm/high-value firms since it sends from Eric's real inbox."""
    try:
        import outreach
        from gmail_helper import send_gmail, gmail_configured

        if not gmail_configured():
            return jsonify({'error': 'Gmail not configured yet — GMAIL_CLIENT_ID/SECRET/REFRESH_TOKEN missing from .env.'}), 400

        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT * FROM outreach_message WHERE id=%s", (message_id,))
                msg = cur.fetchone()
                if not msg:
                    return jsonify({'error': 'Message not found'}), 404
                if msg['status'] != 'approved':
                    return jsonify({'error': 'Message must be approved before sending.'}), 400

                cur.execute("SELECT * FROM outreach_thread WHERE id=%s", (msg['thread_id'],))
                thread = cur.fetchone()
                if not thread or thread['channel'] != 'email':
                    return jsonify({'error': 'This thread\'s channel is not email — use "Mark sent" after sending it yourself.'}), 400
                if not thread['target_email']:
                    return jsonify({'error': 'No target email address on this thread.'}), 400

                sent_today = outreach.real_sends_last_24h(cur)
                if sent_today >= outreach.DAILY_SEND_CAP:
                    return jsonify({'error': f'Daily send pace cap reached ({outreach.DAILY_SEND_CAP}/24h across SES+Gmail). This is intentional — investor outreach isn\'t a campaign. Try again tomorrow.'}), 429

                attachments = outreach.get_standard_attachments()
                if len(attachments) < len(outreach.STANDARD_ATTACHMENTS):
                    missing = set(outreach.STANDARD_ATTACHMENTS) - {os.path.basename(a) for a in attachments}
                    return jsonify({'error': f'Standard attachment(s) missing on server, refusing to send without them: {", ".join(missing)}'}), 400

                body_text = outreach.build_send_body(msg['body'])
                ok, result, gmail_thread_id = send_gmail(
                    to=thread['target_email'],
                    subject=msg['subject'],
                    body_text=body_text,
                    thread_id=thread.get('gmail_thread_id'),
                    attachments=attachments,
                )

                if not ok:
                    cur.execute("UPDATE outreach_message SET status='failed', error=%s WHERE id=%s", (str(result)[:500], message_id))
                    admin_conn.commit()
                    outreach.log_event(cur, admin_conn, msg['thread_id'], 'status_change', detail=f'send failed: {result}', message_id=message_id)
                    return jsonify({'error': f'Gmail send failed: {result}'}), 500

                next_at = outreach.next_business_day_offset(datetime.now(), 5)
                cur.execute(
                    "UPDATE outreach_message SET status='sent', send_channel='gmail', sent_at=NOW(), provider_msg_id=%s WHERE id=%s",
                    (result, message_id)
                )
                cur.execute(
                    "UPDATE outreach_thread SET status='sent', gmail_thread_id=COALESCE(gmail_thread_id, %s), next_action=%s, next_action_at=%s WHERE id=%s",
                    (gmail_thread_id, 'Follow up if no reply', next_at.strftime('%Y-%m-%d %H:%M:%S'), msg['thread_id'])
                )
                admin_conn.commit()
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'sent', detail=f'Gmail msg_id={result}', message_id=message_id)
                outreach.log_event(cur, admin_conn, msg['thread_id'], 'followup_scheduled', detail=next_at.strftime('%Y-%m-%d'))
        finally:
            admin_conn.close()

        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("UPDATE wp_citemetrix_investors SET contacted=1, contacted_at=NOW() WHERE id=%s", (thread['investor_id'],))
        conn.commit(); conn.close()

        return jsonify({'success': True, 'provider_msg_id': result})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/thread/<int:thread_id>/status', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_thread_status(thread_id):
    try:
        import outreach
        d = request.get_json() or {}
        new_status = d.get('status')
        valid = {'not_started','drafted','approved','sent','opened','replied','meeting','passed','committed','bounced','do_not_contact'}
        if new_status not in valid:
            return jsonify({'error': 'Invalid status'}), 400
        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as cur:
                fields = {'status': new_status}
                if 'next_action' in d:
                    fields['next_action'] = (d.get('next_action') or '')[:500] or None
                if 'next_action_at' in d:
                    fields['next_action_at'] = d.get('next_action_at') or None
                sets = ', '.join(f"{k}=%s" for k in fields)
                cur.execute(f"UPDATE outreach_thread SET {sets} WHERE id=%s", list(fields.values()) + [thread_id])
                admin_conn.commit()
                outreach.log_event(cur, admin_conn, thread_id, 'status_change', detail=new_status)
        finally:
            admin_conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/outreach/thread/<int:thread_id>/channel', methods=['POST'])
@login_required
@role_required('admin',)
def api_outreach_thread_channel(thread_id):
    """Manual channel override. The auto-picked channel (from research notes)
    is a heuristic and can be wrong — this lets Eric correct it without
    needing a code change every time."""
    try:
        import outreach
        d = request.get_json() or {}
        new_channel = d.get('channel')
        valid = {'email', 'pitch_page', 'linkedin', 'warm_intro'}
        if new_channel not in valid:
            return jsonify({'error': 'Invalid channel'}), 400
        admin_conn = get_admin_db()
        try:
            with admin_conn.cursor() as cur:
                cur.execute("SELECT channel FROM outreach_thread WHERE id=%s", (thread_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({'error': 'Thread not found'}), 404
                cur.execute("UPDATE outreach_thread SET channel=%s WHERE id=%s", (new_channel, thread_id))
                admin_conn.commit()
                outreach.log_event(cur, admin_conn, thread_id, 'status_change', detail=f"channel: {row['channel']} -> {new_channel}")
        finally:
            admin_conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/outreach')
@login_required
@role_required('admin',)
def outreach_pipeline():
    try:
        admin_conn = get_admin_db()
        with admin_conn.cursor() as cur:
            cur.execute("""SELECT t.*,
                                  (SELECT subject FROM outreach_message m WHERE m.thread_id=t.id ORDER BY m.seq DESC LIMIT 1) AS latest_subject,
                                  (SELECT status FROM outreach_message m WHERE m.thread_id=t.id ORDER BY m.seq DESC LIMIT 1) AS latest_message_status
                           FROM outreach_thread t
                           ORDER BY FIELD(t.status,'meeting','replied','approved','drafted','sent','opened','not_started','passed','committed','bounced','do_not_contact'), t.updated_at DESC""")
            threads = cur.fetchall()
        admin_conn.close()
        for t in threads:
            for k, v in list(t.items()):
                if hasattr(v, 'strftime'):
                    t[k] = v.strftime('%Y-%m-%d %H:%M')
        return render_template('investors/outreach_pipeline.html', threads=threads)
    except Exception as e:
        return render_template('investors/outreach_pipeline.html', threads=[], error=str(e))


@app.route('/api/competition/job/<int:job_id>')
@login_required
@role_required('admin')
def api_competition_job(job_id):
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, job_type, competitor_id, status, phase, error
                           FROM cm_competition_jobs WHERE id = %s""", (job_id,))
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify(row)


@app.route('/api/competition/<int:id>', methods=['DELETE'])

@login_required

@role_required('admin')

def api_competition_delete(id):

    try:

        permanent = request.args.get('permanent', 'false').lower() == 'true'

        conn = get_db()

        with conn.cursor() as cursor:

            if permanent:

                cursor.execute("DELETE FROM wp_citemetrix_competition_alerts WHERE competitor_id = %s", (id,))

                cursor.execute("DELETE FROM wp_citemetrix_competition_history WHERE competitor_id = %s", (id,))

                cursor.execute("DELETE FROM wp_citemetrix_competition WHERE id = %s", (id,))

            else:

                cursor.execute("UPDATE wp_citemetrix_competition SET status = 'archived' WHERE id = %s", (id,))

        conn.commit()

        conn.close()

        return jsonify({'success': True})

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/api/competition/alerts/<int:id>/dismiss', methods=['POST'])

@login_required
@role_required('admin',)

def api_dismiss_alert(id):

    try:

        conn = get_db()

        with conn.cursor() as cursor:

            if id == 0:

                cursor.execute("UPDATE wp_citemetrix_competition_alerts SET is_read = 1 WHERE is_read = 0")

            else:

                cursor.execute("UPDATE wp_citemetrix_competition_alerts SET is_read = 1 WHERE id = %s", (id,))

        conn.commit()

        conn.close()

        return jsonify({'success': True})

    except Exception as e:

        return jsonify({'error': str(e)}), 500



# ══════════════════════════════════════════════════════════════════════════
# Ops alerts ingest + acknowledge
#
# These endpoints back the "Active Alerts" panel on /operations. Alerts are
# POSTed here by the plugin's CiteMetrix_Alert_Dispatcher when something
# like a daily scan cap is hit. The ingest endpoint is machine-to-machine
# and auths via a shared secret; the acknowledge endpoint is operator-facing
# and uses normal session login.
# ══════════════════════════════════════════════════════════════════════════

@app.route('/api/alerts/ingest', methods=['POST'])

def api_alerts_ingest():

    # Shared-secret auth. The plugin sends the secret in X-CiteMetrix-Secret;
    # we compare against ADMIN_PORTAL_INGEST_SECRET from .env. If either
    # side is missing, reject — better to error loud than accept unauthenticated.

    expected = os.getenv('ADMIN_PORTAL_INGEST_SECRET', '')

    provided = request.headers.get('X-CiteMetrix-Secret', '')

    if not expected:

        return jsonify({'error': 'ingest not configured'}), 503

    # Constant-time comparison to prevent timing attacks. Secret is short
    # enough that the difference is negligible but the habit is good.
    import hmac as _hmac

    if not _hmac.compare_digest(expected, provided):

        return jsonify({'error': 'unauthorized'}), 401

    try:

        payload = request.get_json(force=True, silent=True) or {}

        alert_type = str(payload.get('type', ''))[:50]

        severity   = payload.get('severity', 'info')

        if severity not in ('info', 'warn', 'critical'):

            severity = 'info'

        user_id    = payload.get('user_id')

        user_id    = int(user_id) if user_id else None

        user_email = str(payload.get('user_email') or '')[:255] or None

        message    = str(payload.get('message', ''))[:5000]

        metadata   = payload.get('metadata') or {}

        if not alert_type or not message:

            return jsonify({'error': 'type and message required'}), 400

        conn = get_admin_db()

        with conn.cursor() as cursor:

            cursor.execute("""

                INSERT INTO system_alerts

                    (alert_type, severity, user_id, user_email, message, metadata, created_at)

                VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())

            """, (alert_type, severity, user_id, user_email, message, json.dumps(metadata)))

            new_id = cursor.lastrowid

        conn.commit()

        conn.close()

        return jsonify({'success': True, 'alert_id': new_id}), 201

    except Exception as e:

        app.logger.exception('alerts ingest failed')

        return jsonify({'error': str(e)}), 500



@app.route('/api/alerts/<int:alert_id>/acknowledge', methods=['POST'])

@login_required
@role_required('admin', 'support')

def api_alerts_acknowledge(alert_id):

    try:

        conn = get_admin_db()

        with conn.cursor() as cursor:

            cursor.execute("""

                UPDATE system_alerts

                SET acknowledged_at = UTC_TIMESTAMP(), acknowledged_by = %s

                WHERE id = %s AND acknowledged_at IS NULL

            """, (current_user.email, alert_id))

        conn.commit()

        conn.close()

        return jsonify({'success': True})

    except Exception as e:

        return jsonify({'error': str(e)}), 500



@app.route('/customers')
@login_required
@role_required('admin', 'support')
def customers():
    """
    Customer service triage view.

    Lists all users (active first, then dormant), with at-a-glance health
    indicators per row: subscription state, tier, domain count, activity
    recency, and a flag for users who have recent errors needing attention.

    Designed for the trade-show / support workflow: someone says "I'm
    having issues," you find them by email or domain, see their state in
    seconds, then either explain ("your subscription expired, here's
    how to reactivate") or escalate ("let me look at this tonight").

    Read-only. Writes happen in WordPress admin via deep-link.

    Optional ?q= query param filters the list by email substring.
    """
    data = {
        'users': [],
        'total_users': 0,
        'active_users': 0,
        'q': request.args.get('q', '').strip(),
    }

    try:
        conn = get_db()
        with conn.cursor() as cursor:

            # Pull all users with their associated subscription state,
            # domain count, and recent-activity signal in a single query.
            # Subscription state comes from wp_citemetrix_subscription_events
            # (most recent event type wins). Last scan from
            # wp_citemetrix_api_usage. Recent error flag uses the
            # categorized error_category column we just shipped.
            base_sql = """
                SELECT
                    u.ID                AS user_id,
                    u.user_email,
                    u.display_name,
                    u.user_registered,
                    (SELECT to_tier
                       FROM wp_citemetrix_subscription_events
                      WHERE user_id = u.ID
                      ORDER BY created_at DESC LIMIT 1) AS current_tier,
                    (SELECT event_type
                       FROM wp_citemetrix_subscription_events
                      WHERE user_id = u.ID
                      ORDER BY created_at DESC LIMIT 1) AS last_subscription_event,
                    (SELECT COUNT(*)
                       FROM wp_citemetrix_domains
                      WHERE user_id = u.ID AND status = 'active') AS domain_count,
                    (SELECT MAX(created_at)
                       FROM wp_citemetrix_api_usage
                      WHERE user_id = u.ID) AS last_activity_at,
                    (SELECT COUNT(*)
                       FROM wp_citemetrix_api_usage
                      WHERE user_id = u.ID
                        AND status = 'error'
                        AND error_category IN ('upstream_quota', 'upstream_auth')
                        AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 24 HOUR)
                    ) AS actionable_errors_24h,
                    (SELECT COUNT(*)
                       FROM wp_citemetrix_analysis_jobs
                      WHERE user_id = u.ID
                        AND status = 'failed'
                        AND error_category = 'internal'
                        AND updated_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
                    ) AS internal_errors_7d
                FROM wp_users u
                WHERE u.ID IN (
                    -- "CiteMetrix users" = anyone who has interacted with
                    -- the product. wp_capabilities doesn't distinguish
                    -- CiteMetrix users from other WordPress users (they're
                    -- all just 'subscriber'), so we identify them by
                    -- behavior: configured a domain, recorded a
                    -- subscription event, or made an API call. This catches
                    -- everyone meaningful and excludes empty test accounts.
                    SELECT user_id FROM wp_citemetrix_domains
                    UNION
                    SELECT user_id FROM wp_citemetrix_subscription_events
                    UNION
                    SELECT DISTINCT user_id FROM wp_citemetrix_api_usage
                )
                """

            params = []
            if data['q']:
                base_sql += " AND (u.user_email LIKE %s OR u.display_name LIKE %s) "
                params.extend([f"%{data['q']}%", f"%{data['q']}%"])

            base_sql += " ORDER BY last_activity_at DESC, u.user_registered DESC"

            cursor.execute(base_sql, params)
            users = cursor.fetchall()

            # Also let people search by domain — if no matches by email
            # but there's a query, try matching against the domains table
            if not users and data['q']:
                cursor.execute("""
                    SELECT DISTINCT u.ID AS user_id, u.user_email, u.display_name
                    FROM wp_users u
                    JOIN wp_citemetrix_domains d ON d.user_id = u.ID
                    WHERE d.domain LIKE %s
                """, (f"%{data['q']}%",))
                domain_match_ids = [r['user_id'] for r in cursor.fetchall()]

                if domain_match_ids:
                    placeholders = ','.join(['%s'] * len(domain_match_ids))
                    fallback_sql = base_sql.replace(
                        "AND (u.user_email LIKE %s OR u.display_name LIKE %s)",
                        f"AND u.ID IN ({placeholders})"
                    )
                    cursor.execute(fallback_sql, domain_match_ids)
                    users = cursor.fetchall()

            data['users'] = users
            data['total_users'] = len(users)
            data['active_users'] = sum(
                1 for u in users
                if u.get('last_activity_at')
                and (datetime.utcnow() - u['last_activity_at']).days <= 30
            )

        conn.close()

    except Exception as e:
        data['error'] = str(e)
        app.logger.exception("customers route failed")

    return render_template('customers.html', data=data)


@app.route('/customers/<int:user_id>')
@login_required
@role_required('admin', 'support')
def customer_detail(user_id):
    """
    Customer detail view. Read-only deep-dive into a single account:
    subscription state, domains, recent scans, recent errors broken
    down by category. Provides a deep-link to wp-admin for any write
    operations the operator needs to perform.
    """
    data = {
        'user': None,
        'domains': [],
        'subscription_events': [],
        'error_breakdown': {},
        'recent_errors': [],
        'scan_activity': {},
        'recent_activity': [],
    }

    try:
        conn = get_db()
        with conn.cursor() as cursor:

            # Core user info
            cursor.execute("""
                SELECT
                    u.ID AS user_id,
                    u.user_email,
                    u.display_name,
                    u.user_registered,
                    (SELECT to_tier
                       FROM wp_citemetrix_subscription_events
                      WHERE user_id = u.ID
                      ORDER BY created_at DESC LIMIT 1) AS current_tier,
                    (SELECT event_type
                       FROM wp_citemetrix_subscription_events
                      WHERE user_id = u.ID
                      ORDER BY created_at DESC LIMIT 1) AS last_subscription_event,
                    (SELECT created_at
                       FROM wp_citemetrix_subscription_events
                      WHERE user_id = u.ID
                      ORDER BY created_at DESC LIMIT 1) AS last_subscription_change_at
                FROM wp_users u
                WHERE u.ID = %s
            """, (user_id,))
            data['user'] = cursor.fetchone()

            if not data['user']:
                return render_template('error.html', code=404, message='User not found'), 404

            # Their domains
            cursor.execute("""
                SELECT id, domain, status, created_at,
                       (SELECT MAX(created_at) FROM wp_citemetrix_api_usage
                          WHERE domain_id = d.id) AS last_scan_at
                FROM wp_citemetrix_domains d
                WHERE user_id = %s
                ORDER BY status, created_at DESC
            """, (user_id,))
            data['domains'] = cursor.fetchall()

            # Subscription history (last 10 events)
            cursor.execute("""
                SELECT event_type, from_tier, to_tier, created_at
                FROM wp_citemetrix_subscription_events
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 10
            """, (user_id,))
            data['subscription_events'] = cursor.fetchall()

            # Error breakdown by category — last 7 days
            try:
                cursor.execute("""
                    SELECT error_category, COUNT(*) AS cnt
                    FROM wp_citemetrix_api_usage
                    WHERE user_id = %s
                      AND status = 'error'
                      AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
                    GROUP BY error_category
                    ORDER BY cnt DESC
                """, (user_id,))
                for row in cursor.fetchall():
                    data['error_breakdown'][row['error_category'] or 'uncategorized'] = row['cnt']
            except (pymysql.MySQLError, pymysql.OperationalError, pymysql.ProgrammingError):
                # Plugin not yet upgraded to 3.13.44 — column doesn't exist
                pass

            # Recent specific errors (most recent 10, all categories)
            cursor.execute("""
                SELECT a.platform, a.call_type, a.error_message, a.error_category, a.created_at,
                       d.domain
                FROM wp_citemetrix_api_usage a
                LEFT JOIN wp_citemetrix_domains d ON a.domain_id = d.id
                WHERE a.user_id = %s
                  AND a.status = 'error'
                ORDER BY a.created_at DESC
                LIMIT 10
            """, (user_id,))
            data['recent_errors'] = cursor.fetchall()

            # Scan activity — counts for last 7d
            cursor.execute("""
                SELECT
                    COUNT(*) AS total_calls,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_calls,
                    SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_calls
                FROM wp_citemetrix_api_usage
                WHERE user_id = %s
                  AND created_at >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
            """, (user_id,))
            stats = cursor.fetchone()
            data['scan_activity'] = {
                'total_7d':   stats['total_calls']   or 0,
                'success_7d': stats['success_calls'] or 0,
                'error_7d':   stats['error_calls']   or 0,
            }

            # Recent activity from the audit log — last 20 events for
            # this user. Wrapped in try/except because the table may not
            # exist on older deployments and we don't want a missing
            # table to break the whole detail page.
            try:
                cursor.execute("""
                    SELECT event_category, event_type, severity,
                           description, new_value, ip_address, created_at
                    FROM wp_citemetrix_audit_log
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 20
                """, (user_id,))
                data['recent_activity'] = cursor.fetchall()
            except (pymysql.MySQLError, pymysql.OperationalError, pymysql.ProgrammingError):
                pass

        conn.close()

    except Exception as e:
        data['error'] = str(e)
        app.logger.exception("customer_detail route failed")

    return render_template('customer_detail.html', data=data)


@app.route('/audit')
@login_required
@role_required('admin')
def audit_log():
    """
    Audit log list view. Browse all logged platform activity events,
    optionally filtered by user. Admin-only because the audit trail
    contains user behavior data and IP addresses — not appropriate
    for sales/marketing/support roles to see in aggregate.

    Query params:
        ?user=N     Filter to events for a specific user_id
        ?page=N     Pagination (50 events per page)
    """
    PAGE_SIZE = 50
    page = max(1, int(request.args.get('page', '1') or '1'))
    user_filter = request.args.get('user', '').strip()

    data = {
        'events': [],
        'total': 0,
        'page': page,
        'page_size': PAGE_SIZE,
        'total_pages': 1,
        'user_filter': None,
        'filtered_user_email': None,
    }

    try:
        conn = get_db()
        with conn.cursor() as cursor:

            # Build the filter clause. The user filter is the only one
            # the data actually supports meaningfully — severity has only
            # 'info' values and category has only two ('data', 'team'),
            # so filter chips for those would be visual noise.
            where_clause = ""
            params = []
            if user_filter:
                try:
                    user_id = int(user_filter)
                    where_clause = " WHERE user_id = %s"
                    params.append(user_id)
                    data['user_filter'] = user_id

                    # Look up the email for the filter banner so the
                    # operator sees who they're filtered to, not just
                    # an opaque user_id number.
                    cursor.execute(
                        "SELECT user_email FROM wp_users WHERE ID = %s",
                        (user_id,)
                    )
                    row = cursor.fetchone()
                    if row:
                        data['filtered_user_email'] = row['user_email']
                except ValueError:
                    pass  # ignore non-integer ?user= values

            # Total count for pagination
            cursor.execute(
                "SELECT COUNT(*) AS cnt FROM wp_citemetrix_audit_log" + where_clause,
                params
            )
            data['total'] = cursor.fetchone()['cnt'] or 0
            data['total_pages'] = max(1, (data['total'] + PAGE_SIZE - 1) // PAGE_SIZE)

            offset = (page - 1) * PAGE_SIZE
            cursor.execute(
                "SELECT id, user_id, user_email, event_category, event_type, "
                "       severity, description, old_value, new_value, "
                "       ip_address, user_agent, created_at "
                "FROM wp_citemetrix_audit_log" + where_clause +
                " ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [PAGE_SIZE, offset]
            )
            data['events'] = cursor.fetchall()

        conn.close()

    except Exception as e:
        data['error'] = str(e)
        app.logger.exception("audit_log route failed")

    return render_template('audit_log.html', data=data)


@app.route('/team')
@login_required
@role_required('admin')
def team():
    """
    Team management list view (admin-only).

    Shows all admin portal users (active + deactivated) and any pending
    invitations. From here admins can invite new teammates, change roles,
    and deactivate accounts.

    Read-only — actions go to separate POST routes so refresh-on-error
    doesn't accidentally repeat them.
    """
    data = {'members': [], 'invitations': []}
    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT id, username, email, name, role, active, last_login, created_at
                FROM users
                ORDER BY active DESC, role, username
            """)
            data['members'] = cursor.fetchall()

            # Pending invitations: not accepted, not revoked, not expired
            cursor.execute("""
                SELECT i.id, i.email, i.role, i.invited_by, i.token,
                       i.expires_at, i.created_at,
                       u.name AS inviter_name
                FROM team_invitations i
                LEFT JOIN users u ON u.id = i.invited_by
                WHERE i.accepted_at IS NULL
                  AND i.revoked_at  IS NULL
                  AND i.expires_at  > NOW()
                ORDER BY i.created_at DESC
            """)
            data['invitations'] = cursor.fetchall()
        conn.close()
    except Exception as e:
        data['error'] = str(e)
        app.logger.exception("team route failed")
    return render_template('team.html', data=data)


@app.route('/team/invite', methods=['POST'])
@login_required
@role_required('admin')
def team_invite():
    """
    Create a new team invitation.

    Expects form fields: email, role.
    Generates a secure token, persists the invitation, sends the email,
    and redirects back to /team.

    Same email can be re-invited if a previous invitation expired or
    was revoked. If a *valid* (pending, not expired) invitation exists
    for the email, we error rather than send a duplicate.
    """
    email = (request.form.get('email') or '').strip().lower()
    role  = (request.form.get('role')  or '').strip().lower()

    VALID_ROLES = {'admin', 'sales', 'marketing', 'support', 'operations'}
    if not email or '@' not in email:
        flash('Please enter a valid email address.', 'error')
        return redirect(url_for('team'))
    if role not in VALID_ROLES:
        flash('Please select a valid role.', 'error')
        return redirect(url_for('team'))

    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            # Reject if email already corresponds to an active user
            cursor.execute("SELECT id FROM users WHERE email = %s AND active = 1", (email,))
            if cursor.fetchone():
                flash(f'A team member already exists with email {email}.', 'error')
                conn.close()
                return redirect(url_for('team'))

            # Reject if there's already a pending invitation for this email
            cursor.execute("""
                SELECT id FROM team_invitations
                WHERE email = %s
                  AND accepted_at IS NULL
                  AND revoked_at  IS NULL
                  AND expires_at  > NOW()
            """, (email,))
            if cursor.fetchone():
                flash(f'A pending invitation already exists for {email}. '
                      f'Revoke it first to send a new one.', 'error')
                conn.close()
                return redirect(url_for('team'))

            # Generate cryptographically random token. 32 bytes URL-safe
            # is 43 chars after base64; well within our 64-char column.
            token = secrets.token_urlsafe(32)
            expires_at = datetime.utcnow() + timedelta(days=7)

            cursor.execute("""
                INSERT INTO team_invitations (email, role, invited_by, token, expires_at)
                VALUES (%s, %s, %s, %s, %s)
            """, (email, role, current_user.id, token, expires_at))
            conn.commit()
        conn.close()

        # Send the invitation email. Build the accept-invite URL from
        # the request host so it works regardless of which domain the
        # admin portal is reached at.
        accept_url = url_for('accept_invite', token=token, _external=True)
        body_text = (
            f"You've been invited to join the CiteMetrix admin portal.\n\n"
            f"{current_user.name} has invited you to the {role.title()} role.\n\n"
            f"Accept the invitation and set your password here:\n"
            f"{accept_url}\n\n"
            f"This link expires in 7 days.\n\n"
            f"— The CiteMetrix team"
        )
        body_html = f"""<!DOCTYPE html>
<html><body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#1f2937;max-width:560px;margin:24px auto;padding:0 16px;">
  <h2 style="color:#4f46e5;">You're invited to CiteMetrix Admin</h2>
  <p>{current_user.name} has invited you to join the CiteMetrix admin portal as <strong>{role.title()}</strong>.</p>
  <p style="margin:24px 0;">
    <a href="{accept_url}" style="display:inline-block;background:#4f46e5;color:white;text-decoration:none;padding:12px 24px;border-radius:8px;font-weight:500;">Accept Invitation</a>
  </p>
  <p style="font-size:13px;color:#6b7280;">Or paste this URL into your browser: <br>{accept_url}</p>
  <p style="font-size:12px;color:#9ca3af;border-top:1px solid #e5e7eb;padding-top:16px;margin-top:32px;">
    This invitation expires in 7 days. If you weren't expecting this email, you can ignore it.
  </p>
</body></html>"""

        success, info = send_email(
            to=email,
            subject='You\'re invited to CiteMetrix Admin Portal',
            body_text=body_text,
            body_html=body_html,
        )
        if success:
            flash(f'Invitation sent to {email}.', 'success')
        else:
            # Email failed but the invitation is in the DB. Surface the
            # accept URL so the admin can hand-deliver it via Slack/etc.
            flash(f'Invitation created but email send failed ({info}). '
                  f'Manually share this link: {accept_url}', 'warning')

    except Exception as e:
        app.logger.exception("team_invite failed")
        flash(f'Could not create invitation: {e}', 'error')

    return redirect(url_for('team'))


@app.route('/team/<int:user_id>/role', methods=['POST'])
@login_required
@role_required('admin')
def team_change_role(user_id):
    """
    Change a team member's role.

    Self-protection: an admin cannot change their own role (would risk
    locking themselves out of admin functions). Going from admin to
    something else is also blocked if it would leave zero active admins.
    """
    new_role = (request.form.get('role') or '').strip().lower()
    VALID_ROLES = {'admin', 'sales', 'marketing', 'support', 'operations'}
    if new_role not in VALID_ROLES:
        flash('Invalid role.', 'error')
        return redirect(url_for('team'))

    if user_id == current_user.id:
        flash('You cannot change your own role.', 'error')
        return redirect(url_for('team'))

    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            # Look up the target user
            cursor.execute("SELECT id, role, active FROM users WHERE id = %s", (user_id,))
            target = cursor.fetchone()
            if not target:
                flash('User not found.', 'error')
                conn.close()
                return redirect(url_for('team'))

            # If demoting an admin, ensure there's at least one other
            # active admin remaining. Without this, a single misclick
            # could lock the whole organization out of admin functions.
            if target['role'] == 'admin' and new_role != 'admin':
                cursor.execute("""
                    SELECT COUNT(*) AS c FROM users
                    WHERE role = 'admin' AND active = 1 AND id != %s
                """, (user_id,))
                remaining = cursor.fetchone()['c']
                if remaining == 0:
                    flash('Cannot demote the last active admin.', 'error')
                    conn.close()
                    return redirect(url_for('team'))

            cursor.execute(
                "UPDATE users SET role = %s WHERE id = %s",
                (new_role, user_id)
            )
            conn.commit()
        conn.close()
        flash('Role updated.', 'success')
    except Exception as e:
        app.logger.exception("team_change_role failed")
        flash(f'Could not change role: {e}', 'error')

    return redirect(url_for('team'))


@app.route('/team/<int:user_id>/deactivate', methods=['POST'])
@login_required
@role_required('admin')
def team_deactivate(user_id):
    """
    Soft-delete a team member by setting active=0. They can no longer
    log in, but their audit trail and historical records remain intact.

    Self-protection: cannot deactivate yourself. Last-admin guard
    applies same as role change.
    """
    if user_id == current_user.id:
        flash('You cannot deactivate yourself.', 'error')
        return redirect(url_for('team'))

    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT role, active FROM users WHERE id = %s", (user_id,))
            target = cursor.fetchone()
            if not target:
                flash('User not found.', 'error')
                conn.close()
                return redirect(url_for('team'))

            if target['role'] == 'admin' and target['active']:
                cursor.execute("""
                    SELECT COUNT(*) AS c FROM users
                    WHERE role = 'admin' AND active = 1 AND id != %s
                """, (user_id,))
                if cursor.fetchone()['c'] == 0:
                    flash('Cannot deactivate the last active admin.', 'error')
                    conn.close()
                    return redirect(url_for('team'))

            cursor.execute("UPDATE users SET active = 0 WHERE id = %s", (user_id,))
            conn.commit()
        conn.close()
        flash('Team member deactivated.', 'success')
    except Exception as e:
        app.logger.exception("team_deactivate failed")
        flash(f'Could not deactivate: {e}', 'error')

    return redirect(url_for('team'))


@app.route('/team/<int:user_id>/reactivate', methods=['POST'])
@login_required
@role_required('admin')
def team_reactivate(user_id):
    """Re-enable a previously deactivated team member."""
    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE users SET active = 1 WHERE id = %s", (user_id,))
            conn.commit()
        conn.close()
        flash('Team member reactivated.', 'success')
    except Exception as e:
        app.logger.exception("team_reactivate failed")
        flash(f'Could not reactivate: {e}', 'error')

    return redirect(url_for('team'))


@app.route('/team/invitations/<int:invite_id>/revoke', methods=['POST'])
@login_required
@role_required('admin')
def team_revoke_invitation(invite_id):
    """Cancel a pending invitation. Sets revoked_at; the token stops working."""
    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE team_invitations SET revoked_at = NOW() "
                "WHERE id = %s AND accepted_at IS NULL AND revoked_at IS NULL",
                (invite_id,)
            )
            conn.commit()
        conn.close()
        flash('Invitation revoked.', 'success')
    except Exception as e:
        app.logger.exception("team_revoke_invitation failed")
        flash(f'Could not revoke: {e}', 'error')

    return redirect(url_for('team'))


@app.route('/accept-invite', methods=['GET', 'POST'])
def accept_invite():
    """
    Public route — invitee clicks the email link and lands here.

    GET:  Validates the token and shows the password setup form.
    POST: Creates the user account, marks the invite accepted, and
          logs the user in so they land on the dashboard authenticated.

    Token validation is the same on both — re-checking on POST
    matters because a token could be revoked between GET and POST.
    """
    token = (request.values.get('token') or '').strip()
    if not token:
        return render_template('accept_invite.html', error='Missing invitation token.'), 400

    try:
        conn = get_admin_db()
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT i.id, i.email, i.role, i.invited_by, i.expires_at,
                       i.accepted_at, i.revoked_at,
                       u.name AS inviter_name
                FROM team_invitations i
                LEFT JOIN users u ON u.id = i.invited_by
                WHERE i.token = %s
            """, (token,))
            invite = cursor.fetchone()

            # Reject any of: missing, accepted, revoked, expired
            if not invite:
                conn.close()
                return render_template('accept_invite.html',
                    error='Invitation not found.'), 404
            if invite['accepted_at']:
                conn.close()
                return render_template('accept_invite.html',
                    error='This invitation has already been accepted. '
                          'Please log in instead.'), 410
            if invite['revoked_at']:
                conn.close()
                return render_template('accept_invite.html',
                    error='This invitation has been revoked. '
                          'Contact your admin for a new one.'), 410
            if invite['expires_at'] < datetime.utcnow():
                conn.close()
                return render_template('accept_invite.html',
                    error='This invitation has expired. '
                          'Contact your admin for a new one.'), 410

            if request.method == 'GET':
                conn.close()
                return render_template('accept_invite.html',
                    invite=invite, token=token)

            # POST: process the password setup
            name     = (request.form.get('name')     or '').strip()
            password = request.form.get('password') or ''
            confirm  = request.form.get('confirm')  or ''

            if not name:
                conn.close()
                return render_template('accept_invite.html',
                    invite=invite, token=token,
                    error='Please enter your name.')
            if len(password) < 8:
                conn.close()
                return render_template('accept_invite.html',
                    invite=invite, token=token,
                    error='Password must be at least 8 characters.')
            if password != confirm:
                conn.close()
                return render_template('accept_invite.html',
                    invite=invite, token=token,
                    error='Passwords do not match.')

            # Username = email (per design decision Q1 option b — modern
            # SaaS pattern, simpler login). Email and username remain
            # separate columns in case we ever want to allow a display
            # username distinct from the login email.
            username = invite['email']

            password_hash = bcrypt.hashpw(
                password.encode('utf-8'), bcrypt.gensalt()
            ).decode('utf-8')

            try:
                cursor.execute("""
                    INSERT INTO users (username, password_hash, email, name, role)
                    VALUES (%s, %s, %s, %s, %s)
                """, (username, password_hash, invite['email'], name, invite['role']))
                new_user_id = cursor.lastrowid

                # Mark invitation accepted so the same token can't be
                # reused. Even though the unique constraint on username
                # would block a second insert, marking accepted_at gives
                # us a clean audit trail.
                cursor.execute("""
                    UPDATE team_invitations
                    SET accepted_at = NOW()
                    WHERE id = %s
                """, (invite['id'],))
                conn.commit()
            except pymysql.err.IntegrityError:
                conn.close()
                return render_template('accept_invite.html',
                    invite=invite, token=token,
                    error='An account already exists with this email. '
                          'Please log in instead.'), 409
        conn.close()

        # Log the new user in immediately and send them to the dashboard
        new_user = User.get(new_user_id)
        if new_user:
            login_user(new_user)
            flash(f'Welcome, {name}! Your account is set up.', 'success')
            return redirect(url_for('dashboard'))
        else:
            return redirect(url_for('login'))

    except Exception as e:
        app.logger.exception("accept_invite failed")
        return render_template('accept_invite.html',
            error=f'Something went wrong: {e}'), 500


# ─── Trade-show demo trigger ───────────────────────────────────────────────
#
# Calls the plugin's POST /citemetrix/v1/admin/demo-alert endpoint, which
# fires a synthetic key_health alert on PRD with a random platform, error
# category, and one of the operator's domains. The plugin clears the
# cooldown for whichever combo gets picked, so consecutive demos always
# fire. Auth is by shared secret in CM_DEMO_SECRET — same model as the
# investor / marketing APIs.
CM_DEMO_API_URL = os.getenv(
    'CM_DEMO_API_URL',
    'https://citemetrix.com/wp-json/citemetrix/v1/admin/demo-alert'
)

# Monitored domains the demo can fire against. The random scenario picks
# from this curated list rather than letting the WP plugin pick from all
# domains, because PWA push only delivers when the alert fires against a
# domain whose owner has an active push subscription. Edit manually as
# the monitored set changes.
MONITORED_DEMO_DOMAINS = {
    1:  'Cars and Coffee Darien',
    2:  'Expert SEO Consulting',
    3:  'Cars and Coffee Events',
    8:  'Uzedy',
    18: 'CiteMetrix',
    19: 'Canyon Ranch',
    20: 'Sripath Technologies',
    21: 'Black Dog Marketing Strategies',
}

# Hardcoded target for the "Flush Demo Account" button on /demo.
# See the /api/demo-flush-account endpoint below.
DEMO_FLUSH_EMAIL = 'eric@standonitmarketing.com'


@app.route('/demo')
@login_required
@role_required('admin', 'sales')
def demo_page():
    # Sorted by brand name for the dropdown.
    domains = sorted(MONITORED_DEMO_DOMAINS.items(), key=lambda kv: kv[1].lower())
    return render_template(
        'demo.html',
        monitored_domains=domains,
        cm_db_name=os.getenv('DB_NAME', '(unknown)'),
        is_admin=(getattr(current_user, 'role', None) == 'admin'),
        demo_flush_email=DEMO_FLUSH_EMAIL,
    )


@app.route('/api/demo-alert', methods=['POST'])
@login_required
@role_required('admin', 'sales')
def demo_alert_fire():
    secret = os.getenv('CM_DEMO_SECRET', '')
    if not secret:
        return jsonify({'error': 'CM_DEMO_SECRET not set in admin portal .env.'}), 500

    # Build payload. platform/category pass through (or omit for random).
    # domain_id is resolved server-side: 'random' picks one from
    # MONITORED_DEMO_DOMAINS, an explicit ID is validated against the same
    # set so non-monitored domains can't be selected (push wouldn't reach
    # your phone for those).
    body = request.get_json(silent=True) or {}
    payload = {}
    for key in ('platform', 'category'):
        val = body.get(key)
        if val:
            payload[key] = val

    domain_choice = body.get('domain_id') or 'random'
    if domain_choice == 'random':
        payload['domain_id'] = random.choice(list(MONITORED_DEMO_DOMAINS.keys()))
    else:
        try:
            domain_id_int = int(domain_choice)
        except (TypeError, ValueError):
            return jsonify({'error': f'Invalid domain_id: {domain_choice}'}), 400
        if domain_id_int not in MONITORED_DEMO_DOMAINS:
            return jsonify({'error': f'Domain {domain_id_int} is not in the monitored set.'}), 400
        payload['domain_id'] = domain_id_int

    try:
        resp = requests.post(
            CM_DEMO_API_URL,
            headers={
                'X-CM-Demo-Secret': secret,
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=15,
        )
    except requests.RequestException as e:
        return jsonify({'error': f'Could not reach demo API: {e}'}), 502

    try:
        body = resp.json()
    except ValueError:
        return jsonify({'error': f'Non-JSON response (HTTP {resp.status_code})'}), 502

    return jsonify(body), resp.status_code


# ─── Flush demo account ────────────────────────────────────────────────────
# Wipes the hardcoded demo account (eric@standonitmarketing.com) on whatever
# CiteMetrix DB this admin portal is connected to (typically citemetrix_prd).
# Used to reset the onboarding wizard between live demos.
#
# Hardcoded email constrains blast radius: the email is constant in code,
# so the resolved user_id can only be the demo account or NULL (abort).
# Mirrors the validated SQL in DEMO-RESET-standonitmarketing.md.
# DEMO_FLUSH_EMAIL is defined above (near MONITORED_DEMO_DOMAINS) so the
# /demo route can pass it to the template.

# Tables keyed directly on domain_id, cleaned via JOIN to the user's domains.
DEMO_FLUSH_DOMAIN_TABLES = [
    'wp_citemetrix_keywords',
    'wp_citemetrix_citations',
    'wp_citemetrix_scores',
    'wp_citemetrix_competitors',
    'wp_citemetrix_aio_mentions',
    'wp_citemetrix_api_usage',
    'wp_citemetrix_customer_alerts',
    'wp_citemetrix_scan_jobs',
    'wp_citemetrix_analysis_jobs',
    'wp_citemetrix_monitored_prompts',
    'wp_citemetrix_analyst_conversations',
    'wp_citemetrix_analyst_snapshots',
    'wp_citemetrix_analyst_coordinator_state',
    'wp_citemetrix_report_analyses',
    'wp_citemetrix_tool_runs',
    'wp_citemetrix_brand_facts',
    'wp_citemetrix_team_domain_access',
]


@app.route('/api/demo-flush-account', methods=['POST'])
@role_required('admin')
def demo_flush_account():
    # Belt-and-suspenders: only the exact hardcoded email is accepted from
    # the client. The constant is also the only target the SQL uses, so a
    # client posting any other email is rejected before any DB work.
    body = request.get_json(silent=True) or {}
    if body.get('email') != DEMO_FLUSH_EMAIL:
        return jsonify({'error': f'Flush is hardcoded to {DEMO_FLUSH_EMAIL}.'}), 400

    db_name = os.getenv('DB_NAME', '(unknown)')

    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # PRE-FLIGHT: resolve user_id from the hardcoded email. NULL =
            # abort (email doesn't exist in this DB — wrong env, or never
            # provisioned). This is the same guard the SQL script uses.
            cur.execute(
                "SELECT ID AS user_id FROM wp_users WHERE user_email = %s",
                (DEMO_FLUSH_EMAIL,)
            )
            row = cur.fetchone()
            if not row or not row.get('user_id'):
                return jsonify({
                    'error': f'{DEMO_FLUSH_EMAIL} not found in {db_name}. '
                             f'Nothing to flush.'
                }), 404
            uid = int(row['user_id'])

            # Snapshot what we're about to delete so the response is informative.
            cur.execute(
                "SELECT COUNT(*) AS c FROM wp_citemetrix_domains WHERE user_id=%s",
                (uid,)
            )
            domain_count_before = int(cur.fetchone()['c'])

        # All deletes in one transaction so a partial failure rolls back.
        with conn.cursor() as cur:
            # Competitor children (second-degree, via competitor_id -> domain_id -> user)
            cur.execute("""
                DELETE ca FROM wp_citemetrix_competition_alerts ca
                  JOIN wp_citemetrix_competitors c ON c.id = ca.competitor_id
                  JOIN wp_citemetrix_domains d     ON d.id = c.domain_id
                 WHERE d.user_id = %s
            """, (uid,))
            cur.execute("""
                DELETE ch FROM wp_citemetrix_competition_history ch
                  JOIN wp_citemetrix_competitors c ON c.id = ch.competitor_id
                  JOIN wp_citemetrix_domains d     ON d.id = c.domain_id
                 WHERE d.user_id = %s
            """, (uid,))

            # All tables keyed directly on domain_id, JOINed to the user's domains.
            total_child_rows = 0
            for tbl in DEMO_FLUSH_DOMAIN_TABLES:
                # Table names are from a constant list above, NOT user input —
                # safe to interpolate.
                cur.execute(
                    f"DELETE t FROM {tbl} t "
                    f"JOIN wp_citemetrix_domains d ON d.id=t.domain_id "
                    f"WHERE d.user_id = %s",
                    (uid,)
                )
                total_child_rows += cur.rowcount

            # The domains themselves.
            cur.execute(
                "DELETE FROM wp_citemetrix_domains WHERE user_id = %s",
                (uid,)
            )

            # Wizard suppression flags + stored settings (api_keys + prefs).
            # The legacy citemetrix_api_keys row is for early-beta accounts;
            # delete is a no-op if absent.
            cur.execute("""
                DELETE FROM wp_usermeta
                 WHERE user_id = %s
                   AND meta_key IN (
                     'citemetrix_wizard_completed',
                     'citemetrix_wizard_dismissed',
                     'citemetrix_settings',
                     'citemetrix_api_keys'
                   )
            """, (uid,))

        conn.commit()

        return jsonify({
            'success': True,
            'database': db_name,
            'email': DEMO_FLUSH_EMAIL,
            'user_id': uid,
            'domains_removed': domain_count_before,
            'child_rows_removed': total_child_rows,
            'message': (f'Flushed {DEMO_FLUSH_EMAIL} on {db_name}. '
                        f'{domain_count_before} domain(s) + {total_child_rows} '
                        f'child rows removed; wizard flags + settings cleared.')
        })
    except Exception as e:
        if conn:
            try:
                conn.rollback()
            except Exception:
                pass
        return jsonify({'error': f'Flush failed: {e}'}), 500
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Tier Entitlements
#
# wp_citemetrix_tier_entitlements (product DB, citemetrix_prd) is the source of
# truth for all plan gating; the WordPress plugin reads it (cached, keyed to the
# cm_tier_entitlements_version option). View is open to all roles; create/edit
# is admin/sales/support. Any edit must increment cm_tier_entitlements_version
# to bust the plugin cache (handled in the save route — Pass 2).
# ---------------------------------------------------------------------------

TIER_TRACK_ORDER = ['business', 'agency', 'legacy']
TIER_TRACK_LABELS = {'business': 'Business', 'agency': 'Agency', 'legacy': 'Legacy'}


# -----------------------------------------------------------------------------
# Product Roadmap (internal source of truth; investor-flagged items publish to
# the Google Drive data room in Phase 2). Table: wp_cm_roadmap (WP DB).
# -----------------------------------------------------------------------------
ROADMAP_BUCKETS = ['now', 'next', 'later']


@app.route('/roadmap')
@login_required
@role_required('admin',)
def roadmap():
    data = {'buckets': {b: [] for b in ROADMAP_BUCKETS}, 'error': None}
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("SELECT * FROM wp_cm_roadmap ORDER BY FIELD(bucket,'now','next','later'), sort_order, id")
            for row in cursor.fetchall():
                data['buckets'].setdefault(row['bucket'], []).append(row)
    except Exception as e:
        data['error'] = str(e)
        app.logger.exception('roadmap list failed')
    finally:
        if conn:
            conn.close()
    return render_template('roadmap/index.html', data=data, buckets=ROADMAP_BUCKETS)


@app.route('/roadmap/save', methods=['POST'])
@login_required
@role_required('admin',)
def roadmap_save():
    f = request.form
    rid = (f.get('id') or '').strip()
    bucket = f.get('bucket', 'next')
    if bucket not in ROADMAP_BUCKETS:
        bucket = 'next'
    title = (f.get('title') or '').strip()
    desc = (f.get('description') or '').strip()
    status = (f.get('status') or '').strip()
    try:
        sort_order = int(f.get('sort_order') or 0)
    except (TypeError, ValueError):
        sort_order = 0
    is_pub = 1 if f.get('is_published') else 0
    if not title:
        flash('Title is required.', 'error')
        return redirect(url_for('roadmap'))
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            if rid:
                cursor.execute(
                    "UPDATE wp_cm_roadmap SET bucket=%s, title=%s, description=%s, status=%s, sort_order=%s, is_published=%s WHERE id=%s",
                    (bucket, title, desc, status, sort_order, is_pub, rid))
                flash('Roadmap item updated.', 'success')
            else:
                cursor.execute(
                    "INSERT INTO wp_cm_roadmap (bucket, title, description, status, sort_order, is_published) VALUES (%s,%s,%s,%s,%s,%s)",
                    (bucket, title, desc, status, sort_order, is_pub))
                flash('Roadmap item added.', 'success')
        conn.commit()
    except Exception as e:
        flash(f'Save failed: {e}', 'error')
        app.logger.exception('roadmap_save failed')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('roadmap'))


@app.route('/roadmap/<int:rid>/delete', methods=['POST'])
@login_required
@role_required('admin',)
def roadmap_delete(rid):
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM wp_cm_roadmap WHERE id=%s", (rid,))
        conn.commit()
        flash('Roadmap item deleted.', 'success')
    except Exception as e:
        flash(f'Delete failed: {e}', 'error')
        app.logger.exception('roadmap_delete failed')
    finally:
        if conn:
            conn.close()
    return redirect(url_for('roadmap'))


@app.route('/roadmap/publish', methods=['POST'])
@login_required
@role_required('admin',)
def roadmap_publish():
    """Publish the investor-flagged roadmap items to the Google Drive data room (via the plugin)."""
    base = os.getenv('INVESTOR_API_URL', 'https://citemetrix.com/wp-json/citemetrix/v1/investor-applications')
    url = base.rsplit('/investor-applications', 1)[0] + '/roadmap/publish'
    secret = os.getenv('MARKETING_API_SECRET', '')
    if not secret:
        flash('MARKETING_API_SECRET not set in admin portal .env.', 'error')
        return redirect(url_for('roadmap'))
    try:
        resp = requests.post(url, headers={'X-CiteMetrix-Marketing-Secret': secret}, timeout=90)
        body = resp.json() if resp.content else {}
    except Exception as e:
        flash(f'Publish failed: could not reach the plugin ({e}).', 'error')
        return redirect(url_for('roadmap'))
    if resp.status_code >= 400 or (isinstance(body, dict) and body.get('error')):
        msg = body.get('error') if isinstance(body, dict) else None
        flash(f'Publish failed: {msg or ("HTTP " + str(resp.status_code))}', 'error')
    else:
        link = body.get('link', '') if isinstance(body, dict) else ''
        flash('Roadmap published to the investor data room.' + (f' View: {link}' if link else ''), 'success')
    return redirect(url_for('roadmap'))


@app.route('/api/competition/insights')
@login_required
@role_required('admin',)
def api_competition_insights():
    """Consolidated competitive insights (feature gaps + strategy plays) from wp_cm_competitive_insights."""
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, insight_type, title, description, competitors, source_count, progress, roadmap_item_id "
                "FROM wp_cm_competitive_insights "
                "ORDER BY insight_type, source_count DESC, id")
            rows = cursor.fetchall()
        for r in rows:
            r['competitors'] = parse_json_field(r.get('competitors'))
        return jsonify(rows)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/competition/insight/<int:iid>/progress', methods=['POST'])
@login_required
@role_required('admin',)
def competition_insight_progress(iid):
    val = request.form.get('progress', '')
    if val not in ('not_started', 'partially_completed', 'completed', 'added'):
        return jsonify({'error': 'bad progress value'}), 400
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE wp_cm_competitive_insights SET progress=%s WHERE id=%s", (val, iid))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/competition/insight/<int:iid>/add', methods=['POST'])
@login_required
@role_required('admin',)
def competition_insight_add(iid):
    """Create ONE product-roadmap item (wp_cm_roadmap) from a consolidated insight and mark it 'added'."""
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, insight_type, title, description, competitors, source_count, roadmap_item_id "
                "FROM wp_cm_competitive_insights WHERE id=%s", (iid,))
            ins = cursor.fetchone()
            if not ins:
                return jsonify({'error': 'insight not found'}), 404
            # already linked to a live roadmap item?
            if ins.get('roadmap_item_id'):
                cursor.execute("SELECT id FROM wp_cm_roadmap WHERE id=%s", (ins['roadmap_item_id'],))
                if cursor.fetchone():
                    cursor.execute("UPDATE wp_cm_competitive_insights SET progress='added' WHERE id=%s", (iid,))
                    conn.commit()
                    return jsonify({'ok': True, 'already': True, 'roadmap_item_id': ins['roadmap_item_id']})
            comps = parse_json_field(ins.get('competitors')) or []
            desc = (ins.get('description') or '').strip()
            if ins['insight_type'] == 'feature_gap' and comps:
                desc = (desc + ' ' if desc else '') + 'Competitors with this: ' + ', '.join(comps) + '.'
            desc = (desc + ' ' if desc else '') + '[From competitive analysis: %d signals consolidated.]' % int(ins.get('source_count') or 1)
            title = ins['title'] if ins['insight_type'] == 'strategy' else ('Close gap: ' + ins['title'])
            cursor.execute(
                "INSERT INTO wp_cm_roadmap (bucket, title, description, status, sort_order, is_published) "
                "VALUES ('next', %s, %s, '', 0, 1)", (title[:255], desc))
            new_id = cursor.lastrowid
            cursor.execute("UPDATE wp_cm_competitive_insights SET progress='added', roadmap_item_id=%s WHERE id=%s", (new_id, iid))
        conn.commit()
        return jsonify({'ok': True, 'roadmap_item_id': new_id})
    except Exception as e:
        app.logger.exception('competition_insight_add failed')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/competition/roadmap/item/<int:rid>/progress', methods=['POST'])
@login_required
@role_required('admin',)
def competition_roadmap_progress(rid):
    val = request.form.get('progress', '')
    if val not in ('not_started', 'partially_completed', 'completed', 'added'):
        return jsonify({'error': 'bad progress value'}), 400
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute("UPDATE wp_citemetrix_competition_roadmap SET progress=%s WHERE id=%s", (val, rid))
        conn.commit()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/competition/roadmap/bucket/add', methods=['POST'])
@login_required
@role_required('admin',)
def competition_roadmap_bucket_add():
    bucket = (request.form.get('bucket') or '').strip()
    if not bucket:
        return jsonify({'error': 'no bucket given'}), 400
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            # already added? (a live product-roadmap item linked to this bucket)
            cursor.execute("SELECT roadmap_item_id FROM wp_citemetrix_competition_roadmap WHERE bucket=%s AND roadmap_item_id IS NOT NULL LIMIT 1", (bucket,))
            ex = cursor.fetchone()
            if ex and ex.get('roadmap_item_id'):
                cursor.execute("SELECT id FROM wp_cm_roadmap WHERE id=%s", (ex['roadmap_item_id'],))
                if cursor.fetchone():
                    return jsonify({'ok': True, 'already': True, 'roadmap_item_id': ex['roadmap_item_id']})
            cursor.execute("SELECT id, feature_name FROM wp_citemetrix_competition_roadmap WHERE bucket=%s ORDER BY priority_score DESC", (bucket,))
            rows = cursor.fetchall()
            if not rows:
                return jsonify({'error': 'no items in bucket'}), 400
            top = [r['feature_name'] for r in rows[:4]]
            desc = ('Competitive theme drawn from %d tracked competitor recommendations. Representative moves: '
                    % len(rows)) + '; '.join(top) + '.'
            cursor.execute(
                "INSERT INTO wp_cm_roadmap (bucket, title, description, status, sort_order, is_published) VALUES ('next', %s, %s, '', 0, 1)",
                (bucket, desc))
            new_id = cursor.lastrowid
            cursor.execute("UPDATE wp_citemetrix_competition_roadmap SET progress='added', roadmap_item_id=%s WHERE bucket=%s", (new_id, bucket))
        conn.commit()
        return jsonify({'ok': True, 'roadmap_item_id': new_id, 'count': len(rows)})
    except Exception as e:
        app.logger.exception('competition_roadmap_bucket_add failed')
        return jsonify({'error': str(e)}), 500
    finally:
        if conn:
            conn.close()


@app.route('/tiers')
@login_required
@role_required('admin',)
def tiers():
    """List all tier entitlement definitions, grouped by track (read-only)."""
    data = {'tracks': [], 'error': None, 'cache_version': None, 'total': 0}
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM wp_citemetrix_tier_entitlements "
                "ORDER BY FIELD(track, 'business', 'agency', 'legacy'), display_order, tier_key"
            )
            rows = cursor.fetchall()
            cursor.execute(
                "SELECT option_value FROM wp_options "
                "WHERE option_name = 'cm_tier_entitlements_version'"
            )
            ver = cursor.fetchone()
            data['cache_version'] = ver['option_value'] if ver else None
        data['total'] = len(rows)
        grouped = {}
        for row in rows:
            grouped.setdefault(row['track'], []).append(row)
        for track in TIER_TRACK_ORDER:
            if track in grouped:
                data['tracks'].append({
                    'key': track,
                    'label': TIER_TRACK_LABELS.get(track, track.title()),
                    'tiers': grouped[track],
                })
    except Exception as e:
        data['error'] = str(e)
        app.logger.exception("tiers route failed")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    return render_template('tiers_list.html', data=data)


TIER_CAP_FIELDS = [
    'cap_white_label', 'cap_api_access', 'cap_sso_saml', 'cap_priority_support',
    'cap_dedicated_support', 'cap_sla', 'cap_soc2', 'cap_custom_onboarding',
    'cap_reports_basic', 'cap_reports_professional', 'cap_reports_all',
    'cap_competitor_tracking', 'cap_sentiment_analysis', 'cap_hallucination_detection',
    'cap_hallucination_autowatch', 'cap_viewer_role', 'cap_addon_seats',
    'cap_addon_domains', 'cap_addon_queries', 'cap_strategic_insights',
    'cap_strategic_insights_email',
]
# int field -> (nullable, allow_-1_unlimited)
TIER_INT_FIELDS = {
    'display_order': (False, False), 'max_domains': (False, True),
    'max_queries_per_domain': (True, False), 'query_pool': (True, False),
    'max_seats': (False, False), 'max_competitors_per_domain': (False, True),
    'max_scans_per_day': (False, False), 'wc_product_id': (True, False),
}
TIER_DECIMAL_FIELDS = {
    'price_monthly': False, 'price_annual': False,
    'addon_seat_price': True, 'addon_domain_price': True, 'addon_query_price': True,
}
TIER_STRING_FIELDS = {
    'display_name': False, 'wc_product_slug': True,
    'description_short': True, 'suitable_for': True,
}
TIER_EDIT_ROLES = ('admin',)
_TIER_KEY_CHARS = set('abcdefghijklmnopqrstuvwxyz0123456789_')


def _parse_tier_form(form, is_new):
    """Validate + coerce the tier form into a values dict. Returns (vals, errors)."""
    vals, errors = {}, []
    if is_new:
        key = (form.get('tier_key') or '').strip().lower()
        if not (2 <= len(key) <= 50 and key[:1].isalpha()
                and all(c in _TIER_KEY_CHARS for c in key)):
            errors.append("Tier key must be 2-50 lowercase letters/numbers/underscores, starting with a letter.")
        vals['tier_key'] = key
    track = (form.get('track') or '').strip().lower()
    if track not in TIER_TRACK_ORDER:
        errors.append("Track must be business, agency, or legacy.")
    vals['track'] = track
    vals['is_active'] = 1 if form.get('is_active') else 0
    for f, (nullable, allow_neg) in TIER_INT_FIELDS.items():
        raw = (form.get(f) or '').strip()
        if raw == '':
            vals[f] = None
            if not nullable:
                errors.append(f"{f} is required.")
            continue
        try:
            iv = int(raw)
            if iv < -1 or (iv < 0 and not allow_neg):
                errors.append(f"{f} must be >= 0" + (" (or -1 for unlimited)." if allow_neg else "."))
            vals[f] = iv
        except ValueError:
            errors.append(f"{f} must be a whole number.")
            vals[f] = None
    for f, nullable in TIER_DECIMAL_FIELDS.items():
        raw = (form.get(f) or '').strip()
        if raw == '':
            vals[f] = None
            if not nullable:
                errors.append(f"{f} is required.")
            continue
        try:
            fv = float(raw)
            if fv < 0:
                errors.append(f"{f} cannot be negative.")
            vals[f] = fv
        except ValueError:
            errors.append(f"{f} must be a number.")
            vals[f] = None
    for f, nullable in TIER_STRING_FIELDS.items():
        raw = (form.get(f) or '').strip()
        if raw == '' and not nullable:
            errors.append(f"{f} is required.")
        vals[f] = raw if raw != '' else None
    for f in TIER_CAP_FIELDS:
        vals[f] = 1 if form.get(f) else 0
    return vals, errors


def _bump_tier_version(cursor):
    """Increment cm_tier_entitlements_version so the WP plugin rebuilds its cache."""
    cursor.execute(
        "INSERT INTO wp_options (option_name, option_value, autoload) "
        "VALUES ('cm_tier_entitlements_version', '1', 'yes') "
        "ON DUPLICATE KEY UPDATE option_value = CAST(option_value AS UNSIGNED) + 1"
    )


def _audit_tier_change(conn, event_type, tier_key, old, new):
    """Best-effort audit entry in wp_citemetrix_audit_log. Never blocks the save."""
    try:
        changed = {}
        for k, v in new.items():
            if k == 'tier_key':
                continue
            if old is None or str(old.get(k)) != str(v):
                changed[k] = {'old': (old.get(k) if old else None), 'new': v}
        email = getattr(current_user, 'email', None)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO wp_citemetrix_audit_log "
                "(user_id, user_email, event_category, event_type, severity, description, "
                " old_value, new_value, ip_address, created_at) "
                "VALUES (0, %s, 'admin', %s, 'info', %s, %s, %s, %s, NOW())",
                (
                    email, event_type,
                    f"Admin portal: {email or '?'} {event_type} tier '{tier_key}'",
                    json.dumps({k: c['old'] for k, c in changed.items()}, default=str) if old else None,
                    json.dumps({k: c['new'] for k, c in changed.items()}, default=str),
                    request.remote_addr,
                ),
            )
        conn.commit()
    except Exception:
        app.logger.exception("tier audit log failed (non-blocking)")


@app.route('/tiers/new')
@login_required
@role_required(*TIER_EDIT_ROLES)
def tier_new():
    """Form to create a new tier."""
    return render_template('tier_edit.html', tier=None, is_new=True,
                           can_edit=True, tier_json=None, tracks=TIER_TRACK_ORDER)


@app.route('/tiers/<tier_key>')
@login_required
@role_required('admin',)
def tier_detail(tier_key):
    """View/edit a single tier. Editable for admin/sales/support; read-only otherwise."""
    conn, tier, error = None, None, None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM wp_citemetrix_tier_entitlements WHERE tier_key = %s", (tier_key,)
            )
            tier = cursor.fetchone()
    except Exception as e:
        error = str(e)
        app.logger.exception("tier_detail failed")
    finally:
        if conn:
            try:
                conn.close()
            except Exception:
                pass
    if not tier and not error:
        flash('Tier not found.', 'error')
        return redirect(url_for('tiers'))
    can_edit = getattr(current_user, 'role', None) in TIER_EDIT_ROLES
    tier_json = json.dumps(tier, default=str, indent=2, sort_keys=True) if tier else None
    return render_template('tier_edit.html', tier=tier, is_new=False,
                           can_edit=can_edit, tier_json=tier_json, tracks=TIER_TRACK_ORDER)


@app.route('/tiers/<tier_key>', methods=['POST'])
@login_required
@role_required(*TIER_EDIT_ROLES)
def tier_save(tier_key):
    """Save edits to an existing tier, bump the plugin cache version, and audit."""
    vals, errors = _parse_tier_form(request.form, is_new=False)
    if errors:
        for e in errors:
            flash(e, 'error')
        return redirect(url_for('tier_detail', tier_key=tier_key))
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM wp_citemetrix_tier_entitlements WHERE tier_key = %s", (tier_key,)
            )
            old = cursor.fetchone()
            if not old:
                flash('Tier not found.', 'error')
                conn.close()
                return redirect(url_for('tiers'))
            cols = [c for c in vals if c != 'tier_key']
            set_clause = ', '.join(f"`{c}` = %s" for c in cols)
            cursor.execute(
                f"UPDATE wp_citemetrix_tier_entitlements SET {set_clause} WHERE tier_key = %s",
                [vals[c] for c in cols] + [tier_key],
            )
            _bump_tier_version(cursor)
        conn.commit()
        _audit_tier_change(conn, 'tier_updated', tier_key, old, vals)
        conn.close()
        flash(f'Tier "{tier_key}" saved. Plugin cache version bumped.', 'success')
    except Exception as e:
        app.logger.exception("tier_save failed")
        flash(f'Save failed: {e}', 'error')
    return redirect(url_for('tier_detail', tier_key=tier_key))


@app.route('/tiers', methods=['POST'])
@login_required
@role_required(*TIER_EDIT_ROLES)
def tier_create():
    """Create a new tier, bump the plugin cache version, and audit."""
    vals, errors = _parse_tier_form(request.form, is_new=True)
    if errors:
        for e in errors:
            flash(e, 'error')
        return redirect(url_for('tier_new'))
    conn = None
    try:
        conn = get_db()
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT tier_key FROM wp_citemetrix_tier_entitlements WHERE tier_key = %s",
                (vals['tier_key'],),
            )
            if cursor.fetchone():
                flash(f'Tier key "{vals["tier_key"]}" already exists.', 'error')
                conn.close()
                return redirect(url_for('tier_new'))
            cols = list(vals)
            col_clause = ', '.join(f"`{c}`" for c in cols)
            placeholders = ', '.join(['%s'] * len(cols))
            cursor.execute(
                f"INSERT INTO wp_citemetrix_tier_entitlements ({col_clause}) VALUES ({placeholders})",
                [vals[c] for c in cols],
            )
            _bump_tier_version(cursor)
        conn.commit()
        _audit_tier_change(conn, 'tier_created', vals['tier_key'], None, vals)
        conn.close()
        flash(f'Tier "{vals["tier_key"]}" created. Plugin cache version bumped.', 'success')
        return redirect(url_for('tier_detail', tier_key=vals['tier_key']))
    except Exception as e:
        app.logger.exception("tier_create failed")
        flash(f'Create failed: {e}', 'error')
        return redirect(url_for('tier_new'))


@app.errorhandler(404)

def not_found(e):

    return render_template('error.html', code=404, message='Page not found'), 404



@app.errorhandler(500)

def server_error(e):

    return render_template('error.html', code=500, message='Server error'), 500



if __name__ == '__main__':

    app.run(debug=True, host='0.0.0.0', port=5000)



# ── GSC Weekly Baseline (dated snapshots from the Monday cron: jobs/gsc_weekly.py) ──
@app.route('/marketing/gsc-weekly')
@login_required
@role_required('admin', 'marketing')
def gsc_weekly():
    """Weekly Search Console baseline snapshots with week-over-week deltas.
    Data is produced by the Monday cron (jobs/gsc_weekly.py) into reports/gsc/*.json.
    Read-only; no live Google calls in the request path."""
    import os as _os, glob as _glob, json as _json
    rdir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'reports', 'gsc')
    reports = []
    for p in sorted(_glob.glob(_os.path.join(rdir, 'gsc-weekly-*.json')), reverse=True):
        try:
            reports.append(_json.load(open(p)))
        except Exception:
            continue
    return render_template('marketing/gsc_weekly.html', reports=reports)

@app.route('/marketing/measure/traffic')
@login_required
@role_required('admin', 'marketing')
def marketing_traffic():
    import os as _os, json as _json
    base = _os.path.dirname(_os.path.abspath(__file__))
    p = _os.path.join(base, 'reports', 'traffic', 'traffic-latest.json')
    try:
        data = _json.load(open(p))
    except Exception:
        data = None
    sessions_daily = []
    try:
        _ce = _json.load(open(_os.path.join(base, 'reports', 'campaigns', 'campaign-effectiveness.json')))
        sessions_daily = _ce.get('trends', {}).get('sessions_daily', [])
    except Exception:
        pass
    return render_template('marketing/traffic.html', data=data, sessions_daily=sessions_daily)


@app.route('/marketing/traffic')
@login_required
def marketing_traffic_redirect():
    # step1brief.md SS17 step 3: Measure tabbed, old bookmark redirects.
    return redirect(url_for('marketing_traffic'), code=301)


@app.route('/marketing/traffic/refresh', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def marketing_traffic_refresh():
    # step1brief.md SS17 Gates: "traffic/refresh shells out synchronously with
    # a 150s timeout... worth handling rather than inheriting." Was
    # subprocess.run(timeout=150) -- ties up a gunicorn worker (3 total) for up
    # to 2.5 minutes per click. Now non-blocking: Popen returns immediately,
    # the page polls /marketing/traffic/refresh-status (comparing
    # traffic-latest.json's own `generated` timestamp, already written by
    # jobs/traffic.py -- no new job-tracking table needed) rather than the
    # browser hanging on the POST itself.
    import subprocess, os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    try:
        subprocess.Popen(
            [_os.path.join(base, 'venv', 'bin', 'python'), _os.path.join(base, 'jobs', 'traffic.py')],
            cwd=base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except Exception:
        app.logger.exception('failed to start traffic refresh')
    return jsonify({'success': True})


@app.route('/marketing/traffic/refresh-status')
@login_required
@role_required('admin', 'marketing')
def marketing_traffic_refresh_status():
    import os as _os, json as _json
    base = _os.path.dirname(_os.path.abspath(__file__))
    p = _os.path.join(base, 'reports', 'traffic', 'traffic-latest.json')
    generated = None
    try:
        generated = _json.load(open(p)).get('generated')
    except Exception:
        pass
    return jsonify({'generated': generated})

@app.route('/marketing/measure/deliverability')
@login_required
@role_required('admin', 'marketing')
def marketing_deliverability():
    import os as _os, json as _json
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'reports', 'deliverability', 'ses-latest.json')
    ses = None; generated = None; notes = None; read_error = None
    try:
        full = _json.load(open(p)); ses = full.get('ses'); generated = full.get('generated'); notes = full.get('notes')
    except FileNotFoundError:
        read_error = 'No SES data has ever been written yet (reports/deliverability/ses-latest.json does not exist).'
    except Exception as e:
        read_error = f'Failed to read SES data: {e}'
    stale_hours = None
    if generated:
        try:
            gen_dt = datetime.strptime(generated, '%Y-%m-%dT%H:%M:%S')
            stale_hours = round((datetime.now() - gen_dt).total_seconds() / 3600, 1)
        except Exception:
            pass
    campaigns = []
    drip_suppressed_count = None
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("SELECT campaign_id, title, utm_campaign, sent, opens, clicks, bounced, complaints, unsubs, sent_at, updated_at FROM email_campaign_stats ORDER BY sent_at DESC LIMIT 50")
            campaigns = cur.fetchall()
            # step-1-brief.md SS9.2: the send-time suppression gate was invisible --
            # import showed the overlap percentage, but nothing showed how many
            # drip enrollments actually got blocked from sending. Surfaced here
            # next to the suppression total it's drawn from.
            cur.execute("SELECT COUNT(*) AS n FROM drip_enrollments WHERE status='suppressed'")
            drip_suppressed_count = cur.fetchone()['n']
        conn.close()
    except Exception:
        pass
    sendy_updated = None
    try:
        _mu = max((c['updated_at'] for c in campaigns if c.get('updated_at')), default=None)
        if _mu:
            sendy_updated = _mu.strftime('%Y-%m-%d %H:%M') + ' UTC'
    except Exception:
        pass
    anomalies = _deliverability_anomalies(ses)
    blacklist = None
    try:
        bp = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'reports', 'blacklist', 'blacklist-latest.json')
        blacklist = _json.load(open(bp))
    except Exception:
        pass
    return render_template('marketing/deliverability.html', ses=ses, generated=generated, notes=notes, read_error=read_error, stale_hours=stale_hours, campaigns=campaigns, sendy_updated=sendy_updated, anomalies=anomalies, blacklist=blacklist, drip_suppressed_count=drip_suppressed_count)


@app.route('/marketing/deliverability')
@login_required
def marketing_deliverability_redirect():
    # step1brief.md SS17 step 3: Measure tabbed, old bookmark redirects.
    return redirect(url_for('marketing_deliverability'), code=301)


@app.route('/marketing/deliverability/refresh', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def marketing_deliverability_refresh():
    import subprocess, os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    try:
        subprocess.run([_os.path.join(base, 'venv', 'bin', 'python'), _os.path.join(base, 'jobs', 'deliverability.py'), '--fast'], cwd=base, timeout=90, capture_output=True)
    except Exception:
        pass
    return redirect(url_for('marketing_deliverability'))


@app.route('/marketing/deliverability/sendy-refresh', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def marketing_deliverability_sendy_refresh():
    # Two-part refresh: (1) SSH-trigger the read-only extractor on the Sendy box (the key is locked
    # to a forced command, so it can only run cm-sendy-export.sh), pushing fresh per-campaign stats
    # into email_campaign_stats; (2) rebuild campaign-effectiveness.json so the Campaign
    # Effectiveness and Pipeline pages reflect the send too.
    import subprocess, os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    key = _os.path.expanduser('~/.ssh/id_sendy_trigger')
    try:
        subprocess.run(['ssh', '-i', key, '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                        '-o', 'StrictHostKeyChecking=accept-new', 'ec2-user@34.196.159.231'],
                       timeout=45, capture_output=True)
    except Exception:
        pass
    try:
        subprocess.run([_os.path.join(base, 'venv', 'bin', 'python'), _os.path.join(base, 'jobs', 'campaign_effectiveness.py')],
                       cwd=base, timeout=180, capture_output=True)
    except Exception:
        pass
    return redirect(url_for('marketing_deliverability'))

@app.route('/marketing/deliverability/blacklist-refresh', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def marketing_deliverability_blacklist_refresh():
    import subprocess, os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    try:
        subprocess.run([_os.path.join(base, 'venv', 'bin', 'python'), _os.path.join(base, 'jobs', 'blacklist.py')], cwd=base, timeout=90, capture_output=True)
    except Exception:
        pass
    return redirect(url_for('marketing_deliverability'))


@app.route('/marketing/measure/survey')
@login_required
@role_required('admin', 'marketing')
def marketing_survey():
    labels = {
        'q1_awareness': [('familiar', 'Familiar with it'), ('heard_of', 'Heard of it, knew little'), ('first_time', 'First time hearing it')],
        'q2_relevance': [('yes_affecting', 'Already affecting us'), ('probably', 'Probably, unsure how'), ('not_really', 'Not really relevant'), ('dont_know', "Don't know")],
        'q3_barrier': [('didnt_understand', "Didn't understand what it would show"), ('trust', "Didn't trust the site"), ('just_curious', 'Just curious, no real need'), ('technical', 'Something did not work'), ('time', 'Ran out of time'), ('email_gate', "Didn't want to give email"), ('not_relevant', 'Decided not relevant'), ('other', 'Other')],
        'q4_value': [('very_valuable', 'Very valuable'), ('somewhat', 'Somewhat, not urgent'), ('not_really', 'Not really'), ('dont_understand', "Don't understand the question")],
    }
    dist = {k: {code: 0 for code, _ in opts} for k, opts in labels.items()}
    epochs = ['1', '1A', '2', '3']
    barrier_codes = [c for c, _ in labels['q3_barrier']]
    by_epoch = {e: {c: 0 for c in barrier_codes} for e in epochs}
    by_source = {}
    open_responses = []
    by_batch = {}
    total = 0
    error = None
    try:
        conn = get_db()
        with conn.cursor() as cur:
            # 2026-09-03: this used to be wp_citemetrix_survey_responses, shared with an
            # unrelated customer-lifecycle-survey feature -- a schema addition on THAT
            # feature's side (a UNIQUE token column this query never populated) silently
            # broke every barrier-survey insert but one. Moved to its own dedicated table;
            # the old WHERE survey_type='' filter is gone because this table now only ever
            # contains barrier-survey rows.
            cur.execute("SELECT epoch,list_source,campaign_id,q1_awareness,q2_relevance,q3_barrier,q4_value,q5_open,q3_other_text,submitted_at FROM wp_citemetrix_barrier_survey_responses ORDER BY submitted_at DESC")
            rows = cur.fetchall()
        conn.close()
        total = len(rows)
        for r in rows:
            for k in dist:
                v = r.get(k)
                if v in dist[k]:
                    dist[k][v] += 1
            b = r.get('q3_barrier')
            if b:
                ep = r.get('epoch') if r.get('epoch') in by_epoch else None
                if ep:
                    by_epoch[ep][b] += 1
                src = r.get('list_source') or 'unknown'
                by_source.setdefault(src, {c: 0 for c in barrier_codes})
                by_source[src][b] += 1
            cid = r.get('campaign_id')
            if cid is not None:
                bb = by_batch.setdefault(cid, {'n': 0, 'epoch': r.get('epoch'), 'source': r.get('list_source'), 'barriers': {}})
                bb['n'] += 1
                if b:
                    bb['barriers'][b] = bb['barriers'].get(b, 0) + 1
            if r.get('q5_open'):
                open_responses.append({'text': r['q5_open'], 'epoch': r.get('epoch'), 'source': r.get('list_source'), 'campaign': r.get('campaign_id'), 'barrier': r.get('q3_barrier'), 'other': r.get('q3_other_text'), 'at': r.get('submitted_at')})
    except Exception as e:
        # 2026-09-03: this used to swallow every failure silently -- a query error here would
        # render "0 responses" indistinguishably from a real zero. Surface it instead.
        error = str(e)
    for _cid, _bb in by_batch.items():
        _bb['top'] = max(_bb['barriers'], key=_bb['barriers'].get) if _bb['barriers'] else None
    # 'sent' is manually tracked (no live Sendy API wired into this dashboard) -- update this
    # when a new batch goes out. Confirmed against Sendy 2026-09-02: 4 batches, 276 sent.
    return render_template('marketing/survey.html', total=total, sent=276, error=error, dist=dist, labels=labels, epochs=epochs, barrier_labels=dict(labels['q3_barrier']), barrier_codes=barrier_codes, by_epoch=by_epoch, by_source=by_source, open_responses=open_responses, by_batch=by_batch)


@app.route('/marketing/survey')
@login_required
def marketing_survey_redirect():
    # step1brief.md SS17 step 3: Measure tabbed, old bookmark redirects.
    return redirect(url_for('marketing_survey'), code=301)


# ═══════════════════════════════════════════════════════════════════════
# Lead Drip Campaigns — CSV-sourced cold leads (Marblism etc.)
# ═══════════════════════════════════════════════════════════════════════

@app.route('/marketing/drip-leads')
@login_required
def marketing_drip_leads():
    # step1brief.md SS17 step 2: merged into /marketing/leads (path=direct
    # filter). Old bookmarks redirect rather than 404 (Gates: "Eric has
    # bookmarks").
    return redirect(url_for('marketing_leads', path='direct'), code=301)


@app.route('/api/drip-leads/upload', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_leads_upload():
    import leads_drip
    try:
        f = request.files.get('file')
        batch_name = (request.form.get('batch_name') or '').strip()
        source = (request.form.get('source') or '').strip()[:100]
        path = request.form.get('path') or 'direct'
        if path not in ('direct', 'in_person'):
            path = 'direct'
        if not f or not batch_name:
            return jsonify({'error': 'A file and batch name are required.'}), 400

        rows = leads_drip.parse_leads_csv(f.read())
        if not rows:
            return jsonify({'error': 'No valid rows found (need at least an email column).'}), 400

        conn = get_admin_db()
        try:
            with conn.cursor() as cur:
                result = leads_drip.import_batch(cur, conn, batch_name, source, current_user.id, rows, path=path)
        finally:
            conn.close()
        return jsonify({'success': True, **result})
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/leads/add-single', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_leads_add_single():
    import leads_drip
    try:
        email = (request.form.get('email') or '').strip().lower()
        first_name = (request.form.get('first_name') or '').strip()[:255]
        last_name = (request.form.get('last_name') or '').strip()[:255]
        company = (request.form.get('company') or '').strip()[:255]
        path = request.form.get('path') or 'in_person'
        if path not in ('in_person', 'direct'):
            path = 'in_person'
        event_name = (request.form.get('event_name') or '').strip()[:255]
        if not email or '@' not in email:
            return jsonify({'error': 'A valid email is required.'}), 400

        conn = get_admin_db()
        try:
            with conn.cursor() as cur:
                result = leads_drip.add_single_lead(cur, conn, email, first_name, last_name, company, path, event_name, current_user.id)
        finally:
            conn.close()
        return jsonify({'success': True, **result})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── NeverBounce verification — deliverability-history-driven gate (marblism-list-
#    deliverability-diagnosis: the Jul 23 Marblism send to Sendy 78/79 was blocked
#    by enterprise mail gateways; the standing plan since then is never resume
#    cold sending to an imported list without verification first). Explicit
#    action, not automatic-on-upload, so verification credits are only spent
#    when Eric chooses to spend them. Async: submit returns immediately with a
#    job id, status polls (same non-blocking pattern as Traffic's refresh) and
#    applies results once NeverBounce finishes. ──
@app.route('/api/leads/verify/submit', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_leads_verify_submit():
    import neverbounce
    source_ref_id = request.form.get('batch_id', type=int)
    if not source_ref_id:
        return jsonify({'error': 'batch_id is required.'}), 400
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT l.email FROM leads l "
                "JOIN lead_source_touches t ON t.lead_id=l.id "
                "WHERE t.source_ref_id=%s AND l.email_verification_status='unverified'",
                (source_ref_id,)
            )
            emails = [r['email'] for r in cur.fetchall()]
            if not emails:
                return jsonify({'error': 'Nothing to verify — every lead in this batch is already verified (or the batch is empty).'}), 400
            try:
                job_id = neverbounce.submit_job(emails, filename=f'admin-portal-batch-{source_ref_id}')
            except Exception as e:
                return jsonify({'error': f'NeverBounce submission failed: {e}'}), 502
            cur.execute(
                "INSERT INTO email_verification_jobs (source_ref_id, neverbounce_job_id, status, total_submitted, created_by) "
                "VALUES (%s,%s,'submitted',%s,%s)",
                (source_ref_id, job_id, len(emails), current_user.id)
            )
        conn.commit()
        return jsonify({'success': True, 'job_id': job_id, 'submitted': len(emails)})
    finally:
        conn.close()


@app.route('/api/leads/verify/status')
@login_required
@role_required('admin', 'marketing')
def api_leads_verify_status():
    import neverbounce
    source_ref_id = request.args.get('batch_id', type=int)
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            where = "status='submitted'"
            params = []
            if source_ref_id:
                where += " AND source_ref_id=%s"; params.append(source_ref_id)
            cur.execute(f"SELECT * FROM email_verification_jobs WHERE {where} ORDER BY id DESC", params)
            pending = cur.fetchall()

            applied = []
            for job in pending:
                try:
                    status = neverbounce.job_status(job['neverbounce_job_id'])
                except Exception as e:
                    cur.execute("UPDATE email_verification_jobs SET status='failed', error=%s WHERE id=%s", (str(e), job['id']))
                    conn.commit()
                    continue
                if status.get('job_status') != 'complete':
                    continue
                try:
                    results = neverbounce.fetch_results(job['neverbounce_job_id'])
                    counts = neverbounce.apply_results(cur, results)
                except Exception as e:
                    cur.execute("UPDATE email_verification_jobs SET status='failed', error=%s WHERE id=%s", (str(e), job['id']))
                    conn.commit()
                    continue
                cur.execute("UPDATE email_verification_jobs SET status='complete', completed_at=NOW() WHERE id=%s", (job['id'],))
                conn.commit()
                applied.append({'job_id': job['neverbounce_job_id'], 'source_ref_id': job['source_ref_id'], **counts})

            cur.execute(
                "SELECT id, source_ref_id, neverbounce_job_id, status, total_submitted, submitted_at, completed_at "
                "FROM email_verification_jobs" + (" WHERE source_ref_id=%s" if source_ref_id else "") + " ORDER BY id DESC LIMIT 20",
                [source_ref_id] if source_ref_id else []
            )
            jobs = cur.fetchall()
        return jsonify({'success': True, 'jobs': jobs, 'applied': applied})
    finally:
        conn.close()


@app.route('/marketing/drip-campaigns')
@login_required
@role_required('admin', 'marketing')
def marketing_drip_campaigns():
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT c.*, b.label AS batch_name,
                                  (SELECT COUNT(*) FROM drip_enrollments e WHERE e.campaign_id=c.id) AS enrolled_count,
                                  (SELECT COUNT(*) FROM drip_enrollments e WHERE e.campaign_id=c.id AND e.status='active') AS active_count,
                                  (SELECT COUNT(*) FROM drip_enrollments e WHERE e.campaign_id=c.id AND e.status='completed') AS completed_count,
                                  (SELECT COUNT(*) FROM drip_steps s WHERE s.campaign_id=c.id) AS step_count
                           FROM drip_campaigns c LEFT JOIN source_refs b ON b.id=c.batch_id AND b.kind='import_batch'
                           ORDER BY c.created_at DESC""")
            campaigns = cur.fetchall()
            cur.execute("SELECT id, label AS name, new_count FROM source_refs WHERE kind='import_batch' ORDER BY created_at DESC")
            batches = cur.fetchall()
    finally:
        conn.close()
    return render_template('marketing/drip_campaigns.html', campaigns=campaigns, batches=batches)


@app.route('/marketing/drip-campaigns/<int:campaign_id>')
@login_required
@role_required('admin', 'marketing')
def marketing_drip_campaign_detail(campaign_id):
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT c.*, b.label AS batch_name FROM drip_campaigns c LEFT JOIN source_refs b ON b.id=c.batch_id AND b.kind='import_batch' WHERE c.id=%s", (campaign_id,))
            campaign = cur.fetchone()
            if not campaign:
                return "Campaign not found", 404
            cur.execute("SELECT * FROM drip_steps WHERE campaign_id=%s ORDER BY step_order ASC", (campaign_id,))
            steps = cur.fetchall()
            cur.execute("""SELECT s.step_order,
                                  COUNT(DISTINCT CASE WHEN e.current_step >= s.step_order THEN e.id END) AS reached,
                                  (SELECT COUNT(*) FROM drip_send_log dl WHERE dl.step_id=s.id AND dl.status='sent') AS sent_count
                           FROM drip_steps s LEFT JOIN drip_enrollments e ON e.campaign_id=s.campaign_id
                           WHERE s.campaign_id=%s GROUP BY s.id ORDER BY s.step_order ASC""", (campaign_id,))
            funnel = cur.fetchall()
            cur.execute("""SELECT status, COUNT(*) AS n FROM drip_enrollments WHERE campaign_id=%s GROUP BY status""", (campaign_id,))
            status_counts = {r['status']: r['n'] for r in cur.fetchall()}
            cur.execute("SELECT id, label AS name, new_count FROM source_refs WHERE kind='import_batch' ORDER BY created_at DESC")
            batches = cur.fetchall()
    finally:
        conn.close()
    return render_template('marketing/drip_campaign_detail.html', campaign=campaign, steps=steps, funnel=funnel, status_counts=status_counts, batches=batches)


@app.route('/api/drip/campaigns', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_create_campaign():
    try:
        d = request.get_json() or {}
        name = (d.get('name') or '').strip()
        if not name:
            return jsonify({'error': 'Campaign name is required.'}), 400
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO drip_campaigns (name, description, created_by) VALUES (%s,%s,%s)",
                (name, (d.get('description') or '')[:2000], current_user.id)
            )
            conn.commit()
            campaign_id = cur.lastrowid
        conn.close()
        return jsonify({'success': True, 'campaign_id': campaign_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drip/campaigns/<int:campaign_id>/steps', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_save_step(campaign_id):
    try:
        import leads_drip
        d = request.get_json() or {}
        step_id = d.get('step_id')
        day_offset = int(d.get('day_offset', 0))
        subject = (d.get('subject') or '')[:500]
        body = d.get('body') or ''
        is_survey_step = 1 if d.get('is_survey_step') else 0
        survey_epoch = (d.get('survey_epoch') or '')[:10]
        # CTA: cta_key must match a utm_campaign key defined in the WP plugin's ANGLE_COPY
        # (the /check/ message-match system) so the click-through lands on copy that
        # continues this email's pitch -- see leads_drip.build_cta()'s docstring. Free-typed
        # on purpose, not validated against a hardcoded list here (see that docstring for why).
        cta_text = (d.get('cta_text') or '').strip()[:255]
        cta_key = (d.get('cta_key') or '').strip()[:100]
        # On-page title/copy: what /check/ itself shows when this step's CTA is clicked --
        # pushed to WP below via leads_drip.push_message_variant() so it's live without a
        # plugin deploy. Reuses cta_text as the on-page button (Eric's ask was 4 fields, not
        # 5 -- one button label carries from email to landing page).
        onpage_title = (d.get('onpage_title') or '').strip()[:500]
        onpage_copy = (d.get('onpage_copy') or '').strip()

        sync_status, sync_error = None, None
        if cta_key and cta_text and onpage_title and onpage_copy:
            push_result = leads_drip.push_message_variant(cta_key, onpage_title, onpage_copy, cta_text)
            sync_status = 'synced' if push_result['ok'] else 'failed'
            sync_error = None if push_result['ok'] else push_result['error']

        conn = get_admin_db()
        with conn.cursor() as cur:
            if step_id:
                cur.execute(
                    "UPDATE drip_steps SET day_offset=%s, subject=%s, body=%s, cta_text=%s, cta_key=%s, "
                    "onpage_title=%s, onpage_copy=%s, onpage_sync_status=%s, onpage_sync_error=%s, "
                    "is_survey_step=%s, survey_epoch=%s WHERE id=%s AND campaign_id=%s",
                    (day_offset, subject, body, cta_text or None, cta_key or None,
                     onpage_title or None, onpage_copy or None, sync_status, sync_error,
                     is_survey_step, survey_epoch, step_id, campaign_id)
                )
            else:
                cur.execute("SELECT COALESCE(MAX(step_order),0)+1 AS next_order FROM drip_steps WHERE campaign_id=%s", (campaign_id,))
                next_order = cur.fetchone()['next_order']
                cur.execute(
                    "INSERT INTO drip_steps (campaign_id, step_order, day_offset, subject, body, cta_text, cta_key, "
                    "onpage_title, onpage_copy, onpage_sync_status, onpage_sync_error, is_survey_step, survey_epoch) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (campaign_id, next_order, day_offset, subject, body, cta_text or None, cta_key or None,
                     onpage_title or None, onpage_copy or None, sync_status, sync_error, is_survey_step, survey_epoch)
                )
            conn.commit()
        conn.close()
        return jsonify({'success': True, 'onpage_sync_status': sync_status, 'onpage_sync_error': sync_error})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drip/steps/<int:step_id>/delete', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_delete_step(step_id):
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM drip_steps WHERE id=%s", (step_id,))
            conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# Sample lead used by both preview and test-send below — a fake merge-tag context, not a
# real leads row, so Preview/Send-test work on a step's CURRENT unsaved textarea contents
# (the copywriter shouldn't have to save first just to see how it reads).
_DRIP_PREVIEW_LEAD = {
    'first_name': 'Jordan', 'last_name': 'Rivera', 'company': 'Acme Marketing Co.',
    'email': 'jordan@example.com', 'brand_name': 'Acme Marketing Co.', 'model_score': 42,
    'platforms_checked_count': 3, 'platforms_mentioned_count': 1, 'missing_platforms': 'Claude and Perplexity',
}


def _render_drip_step_preview(d):
    """Shared by /api/drip/preview and /api/drip/test-send: render subject/body/CTA from
    whatever fields the request sends (unsaved form values, not a stored drip_steps row) --
    same merge-tag + CTA machinery process_due_enrollments() uses for a real send, so what
    you preview is what actually goes out. Returns (subject, body, body_html) -- body_html
    is auto-derived by leads_drip.apply_cta_tags() whenever a CTA is configured (real button,
    not the plain-text "text: url" fallback), same as a real send now does. See that
    function's docstring -- this was a real gap caught live 2026-09-04: a test send showed
    the CTA as plain text because nothing ever supplied a body_html for the button-HTML code
    to run on."""
    import leads_drip
    step = {
        'cta_text': d.get('cta_text') or '',
        'cta_key': d.get('cta_key') or '',
        'step_order': int(d.get('step_order') or 1),
    }
    subject = leads_drip.render_merge_tags(d.get('subject') or '', _DRIP_PREVIEW_LEAD)
    body = leads_drip.render_merge_tags(d.get('body') or '', _DRIP_PREVIEW_LEAD)
    body, body_html = leads_drip.apply_cta_tags(body, None, step)
    return subject, body, body_html


@app.route('/api/drip/preview', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_preview():
    try:
        d = request.get_json() or {}
        subject, body, body_html = _render_drip_step_preview(d)
        import leads_drip
        cta_url, cta_text = leads_drip.build_cta({'cta_key': d.get('cta_key') or '', 'cta_text': d.get('cta_text') or '', 'step_order': int(d.get('step_order') or 1)})
        return jsonify({'success': True, 'subject': subject, 'body': body, 'body_html': body_html, 'cta_url': cta_url,
                         'onpage_title': (d.get('onpage_title') or '').strip(),
                         'onpage_copy': (d.get('onpage_copy') or '').strip(),
                         'sample_lead': {'first_name': _DRIP_PREVIEW_LEAD['first_name'], 'company': _DRIP_PREVIEW_LEAD['company']}})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drip/test-send', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_test_send():
    try:
        d = request.get_json() or {}
        to_email = (d.get('to_email') or '').strip()
        if not to_email or '@' not in to_email:
            return jsonify({'error': 'Enter a valid email address to send the test to.'}), 400
        subject, body, body_html = _render_drip_step_preview(d)
        import leads_drip
        # A test send has no real lead/enrollment, so no real unsubscribe_token exists --
        # but a real send always appends one (build_unsubscribe_footer), and Eric asked
        # specifically why a test send didn't show it. Append the SAME footer structure with
        # an obviously-fake token, so the test send shows the full, real email shape
        # (compliance footer included) rather than a stripped-down version that looks
        # different from what will actually go out.
        fake_unsub_url = 'https://citemetrix.com/unsubscribe/TEST-TOKEN-NOT-A-REAL-LINK'
        body += leads_drip.build_unsubscribe_footer(fake_unsub_url)
        if body_html:
            body_html += leads_drip.build_unsubscribe_footer_html(fake_unsub_url)
        disclaimer_text = (
            "This is a TEST send from the drip campaign builder, not a real enrollment. "
            "Merge tags above are filled with sample data (Jordan Rivera / Acme Marketing Co.), "
            "not a real lead. The unsubscribe link above is a placeholder, not real."
        )
        body += f"\n\n—\n{disclaimer_text}"
        if body_html:
            body_html += (
                '<p style="margin:24px 0 0;font-family:Arial,Helvetica,sans-serif;font-size:12px;'
                f'line-height:1.6;color:#8a94a3;border-top:1px solid #e5e7eb;padding-top:16px;">{disclaimer_text}</p>'
            )
        import outreach
        send_kwargs = {
            'to': to_email, 'subject': f"[TEST] {subject}", 'body_text': body,
            'from_address': outreach.SES_FROM_ADDRESS, 'configuration_set': outreach.SES_CONFIGURATION_SET,
        }
        if body_html:
            send_kwargs['body_html'] = body_html
        ok, result = send_email(**send_kwargs)
        if not ok:
            return jsonify({'error': f'Send failed: {result}'}), 500
        return jsonify({'success': True, 'message_id': result})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drip/campaigns/<int:campaign_id>/enroll', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_enroll(campaign_id):
    import leads_drip
    try:
        d = request.get_json() or {}
        batch_id = d.get('batch_id')
        if not batch_id:
            return jsonify({'error': 'batch_id is required.'}), 400
        conn = get_admin_db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM drip_steps WHERE campaign_id=%s", (campaign_id,))
                if cur.fetchone()['n'] == 0:
                    return jsonify({'error': 'Add at least one step before enrolling leads.'}), 400
                enroll_result = leads_drip.enroll_batch(cur, conn, campaign_id, batch_id)
                cur.execute("UPDATE drip_campaigns SET batch_id=%s WHERE id=%s AND batch_id IS NULL", (batch_id, campaign_id))
                conn.commit()
        finally:
            conn.close()
        return jsonify({
                    'success': True,
                    'enrolled': enroll_result['enrolled'],
                    'skipped_suppressed': enroll_result['skipped_suppressed'],
                    'skipped_nurture_owned': enroll_result['skipped_nurture_owned'],
                    'skipped_cluster_conflict': enroll_result['skipped_cluster_conflict'],
                })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/drip/campaigns/<int:campaign_id>/toggle', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_drip_toggle_campaign(campaign_id):
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("SELECT active FROM drip_campaigns WHERE id=%s", (campaign_id,))
            row = cur.fetchone()
            if not row:
                return jsonify({'error': 'Not found'}), 404
            new_active = 0 if row['active'] else 1
            cur.execute("UPDATE drip_campaigns SET active=%s WHERE id=%s", (new_active, campaign_id))
            conn.commit()
        conn.close()
        return jsonify({'success': True, 'active': bool(new_active)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/unsubscribe/<token>')
def public_unsubscribe(token):
    """Public, no-login unsubscribe -- the link in every drip email. Marks
    the lead unsubscribed globally and halts every active enrollment, not
    just one campaign."""
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id, email FROM leads WHERE unsubscribe_token=%s", (token,))
            lead = cur.fetchone()
            if not lead:
                conn.close()
                return "This unsubscribe link isn't valid or has already been used.", 404
            cur.execute("UPDATE leads SET suppressed_at=NOW(), suppression_reason='unsubscribed' WHERE id=%s", (lead['id'],))
            cur.execute("UPDATE drip_enrollments SET status='unsubscribed' WHERE lead_id=%s AND status='active'", (lead['id'],))
            conn.commit()
        conn.close()
        return f"You're unsubscribed. {lead['email']} won't receive any more of these emails."
    except Exception as e:
        return f"Something went wrong: {e}", 500


@app.route('/marketing/nurture')
@login_required
@role_required('admin', 'marketing')
def marketing_nurture():
    funnel = {k: {'leads': 0, 'rpt': 0, 'd2': 0, 'd5': 0, 'd8': 0, 'conv': 0} for k in ('agency', 'comparison', 'generic')}
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT source_page, results_email_sent, nurture_1_sent, nurture_2_sent, nurture_3_sent, converted FROM wp_citemetrix_score_leads WHERE excluded=0")
            for r in cur.fetchall():
                sp = (r['source_page'] or '').lower()
                if '/agencies' in sp:
                    seg = 'agency'
                elif ('vs-' in sp) or ('/compare' in sp) or ('best-ai-visibility' in sp):
                    seg = 'comparison'
                else:
                    seg = 'generic'
                funnel[seg]['leads'] += 1
                funnel[seg]['rpt'] += int(r['results_email_sent'] or 0)
                funnel[seg]['d2'] += int(r['nurture_1_sent'] or 0)
                funnel[seg]['d5'] += int(r['nurture_2_sent'] or 0)
                funnel[seg]['d8'] += int(r['nurture_3_sent'] or 0)
                funnel[seg]['conv'] += int(r['converted'] or 0)
        conn.close()
    except Exception:
        pass
    nurture_ga = []
    try:
        import os as _os2, json as _json2
        _p = _os2.path.join(_os2.path.dirname(_os2.path.abspath(__file__)), 'reports', 'campaigns', 'campaign-effectiveness.json')
        nurture_ga = (_json2.load(open(_p)) or {}).get('nurture', [])
    except Exception:
        pass
    nur_map = {}
    for _row in nurture_ga:
        _c = (_row.get('content') or '').lower()
        for _seg in ('agency', 'comparison', 'generic'):
            for _dn in ('2', '5', '8'):
                if _seg in _c and ('day' + _dn in _c or 'd' + _dn in _c or _c.endswith('_' + _dn)):
                    nur_map[_seg + '_' + _dn] = _row
    return render_template('marketing/nurture.html', funnel=funnel, nurture_ga=nurture_ga, nur_map=nur_map)

@app.route('/customers/inbox')
@login_required
@role_required('admin', 'support')
def customers_inbox():
    import datetime as _dt
    items = []
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, email, phone, subject, preferred_contact, message, is_read, created_at FROM wp_cm_contact_entries ORDER BY created_at DESC LIMIT 200")
            for r in cur.fetchall():
                items.append({'type': 'contact', 'id': r['id'], 'name': r['name'], 'email': r['email'],
                              'phone': r['phone'], 'subject': r['subject'], 'preferred': r['preferred_contact'],
                              'message': r['message'], 'read': bool(r['is_read']), 'when': r['created_at'],
                              'messages': [], 'lead_captured': 0, 'page_url': None, 'interest': None})
            cur.execute("SELECT c.id, c.page_url, c.started_at, c.message_count, c.lead_captured, "
                        "l.name AS ln, l.email AS le, l.phone AS lp, l.interest AS li "
                        "FROM wp_chatly_conversations c LEFT JOIN wp_chatly_leads l ON l.conversation_id = c.id "
                        "ORDER BY c.started_at DESC LIMIT 200")
            convs = cur.fetchall()
            ids = [c['id'] for c in convs]
            msgs = {}
            if ids:
                fmt = ','.join(['%s'] * len(ids))
                cur.execute("SELECT conversation_id, role, content, created_at FROM wp_chatly_messages WHERE conversation_id IN (" + fmt + ") ORDER BY created_at ASC", ids)
                for m in cur.fetchall():
                    msgs.setdefault(m['conversation_id'], []).append({'role': m['role'], 'content': m['content'], 'when': m['created_at']})
            for c in convs:
                ml = msgs.get(c['id'], [])
                preview = ''
                for m in ml:
                    if m['role'] in ('user', 'visitor'):
                        preview = m['content']
                        break
                if not preview and ml:
                    preview = ml[0]['content']
                items.append({'type': 'chat', 'id': c['id'], 'name': c['ln'] or 'Anonymous visitor',
                              'email': c['le'], 'phone': c['lp'], 'subject': c['li'], 'message': preview,
                              'read': True, 'when': c['started_at'], 'messages': ml,
                              'lead_captured': c['lead_captured'], 'page_url': c['page_url'],
                              'interest': c['li'], 'preferred': None})
        conn.close()
    except Exception:
        pass
    items.sort(key=lambda x: x['when'] or _dt.datetime.min, reverse=True)
    return render_template('customers/inbox.html', items=items)

@app.route('/sales/demo-requests')
@login_required
@role_required('admin', 'sales')
def sales_demo_requests():
    import datetime as _dt
    rows = []
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT a.id, a.start_date, a.end_date, a.created_at AS booked_at, ca.status, ca.notes, cu.full_name, cu.email, cu.phone FROM wp_bookly_appointments a LEFT JOIN wp_bookly_customer_appointments ca ON ca.appointment_id = a.id LEFT JOIN wp_bookly_customers cu ON cu.id = ca.customer_id ORDER BY a.start_date DESC LIMIT 200")
            rows = cur.fetchall()
        conn.close()
    except Exception:
        pass
    return render_template('sales/demo_requests.html', rows=rows, now=_dt.datetime.now())

@app.route('/operations/api-status')
@login_required
@role_required('admin', 'operations')
def operations_api_status():
    data = {'calls_24h': 0, 'calls_7d': 0, 'errors_7d': 0, 'err_rate_7d': 0, 'avg_ms_7d': 0, 'by_platform': [], 'daily': [], 'error_cats': [], 'attrib': {'client': 0, 'uncat': 0, 'structural': 0}}
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) n FROM wp_citemetrix_api_usage WHERE created_at>=DATE_SUB(NOW(),INTERVAL 1 DAY)")
            data['calls_24h'] = int(cur.fetchone()['n'] or 0)
            cur.execute("SELECT COUNT(*) n, SUM(status='error') e, AVG(NULLIF(response_time_ms,0)) a FROM wp_citemetrix_api_usage WHERE created_at>=DATE_SUB(NOW(),INTERVAL 7 DAY)")
            r = cur.fetchone(); data['calls_7d'] = int(r['n'] or 0); data['errors_7d'] = int(r['e'] or 0); data['avg_ms_7d'] = int(r['a'] or 0)
            data['err_rate_7d'] = round(100*data['errors_7d']/data['calls_7d'], 1) if data['calls_7d'] else 0
            cur.execute("SELECT platform, COUNT(*) n, SUM(status='error') e, AVG(NULLIF(response_time_ms,0)) a FROM wp_citemetrix_api_usage WHERE created_at>=DATE_SUB(NOW(),INTERVAL 30 DAY) GROUP BY platform ORDER BY n DESC")
            for r in cur.fetchall():
                n = int(r['n'] or 0); e = int(r['e'] or 0)
                data['by_platform'].append({'platform': r['platform'], 'calls': n, 'errors': e, 'err_rate': round(100*e/n, 1) if n else 0, 'avg_ms': int(r['a'] or 0)})
            cur.execute("SELECT DATE(created_at) d, COUNT(*) n, SUM(status='error') e FROM wp_citemetrix_api_usage WHERE created_at>=DATE_SUB(NOW(),INTERVAL 14 DAY) GROUP BY d ORDER BY d DESC")
            data['daily'] = [{'date': str(r['d']), 'calls': int(r['n'] or 0), 'errors': int(r['e'] or 0)} for r in cur.fetchall()]
            cur.execute("SELECT error_category ec, COUNT(*) n FROM wp_citemetrix_api_usage WHERE created_at>=DATE_SUB(NOW(),INTERVAL 30 DAY) AND status='error' AND error_category IS NOT NULL AND error_category<>'' GROUP BY ec ORDER BY n DESC LIMIT 10")
            data['error_cats'] = [{'cat': r['ec'], 'n': int(r['n'] or 0)} for r in cur.fetchall()]
            cur.execute("SELECT SUM(status='error' AND error_category IN ('upstream_quota','upstream_auth')) ce, SUM(status='error' AND (error_category IS NULL OR error_category='')) uc, SUM(status='error' AND error_category IS NOT NULL AND error_category<>'' AND error_category NOT IN ('upstream_quota','upstream_auth')) oe FROM wp_citemetrix_api_usage WHERE created_at>=DATE_SUB(NOW(),INTERVAL 7 DAY)")
            _a = cur.fetchone()
            data['attrib'] = {'client': int(_a['ce'] or 0), 'uncat': int(_a['uc'] or 0), 'structural': int(_a['oe'] or 0)}
        conn.close()
    except Exception:
        pass
    return render_template('operations/api_status.html', d=data)

@app.route('/customers/domain-transfer')
@login_required
@role_required('admin')
def customers_domain_transfer():
    import domain_transfer as _dt
    dbname = os.getenv('DB_NAME')
    domain_q = (request.args.get('domain_q') or '').strip()
    dest_q = (request.args.get('dest_q') or '').strip()
    domain_id = request.args.get('domain_id')
    dest_id = request.args.get('dest_id')
    ctx = {'domain_q': domain_q, 'dest_q': dest_q, 'domain_matches': [], 'dest_matches': [],
           'domain_id': domain_id, 'dest_id': dest_id, 'preview': None, 'result': None}
    conn = get_db()
    try:
        with conn.cursor() as c:
            if domain_q:
                if domain_q.isdigit():
                    c.execute("SELECT d.id, d.domain, d.brand_name, d.user_id, u.user_email FROM wp_citemetrix_domains d LEFT JOIN wp_users u ON u.ID=d.user_id WHERE d.id=%s", (domain_q,))
                else:
                    like = '%' + domain_q + '%'
                    c.execute("SELECT d.id, d.domain, d.brand_name, d.user_id, u.user_email FROM wp_citemetrix_domains d LEFT JOIN wp_users u ON u.ID=d.user_id WHERE d.domain LIKE %s OR d.brand_name LIKE %s LIMIT 25", (like, like))
                ctx['domain_matches'] = c.fetchall()
            if dest_q:
                if dest_q.isdigit():
                    c.execute("SELECT ID AS id, user_email, display_name FROM wp_users WHERE ID=%s", (dest_q,))
                else:
                    like = '%' + dest_q + '%'
                    c.execute("SELECT ID AS id, user_email, display_name FROM wp_users WHERE user_email LIKE %s OR display_name LIKE %s LIMIT 25", (like, like))
                ctx['dest_matches'] = c.fetchall()
            if domain_id:
                ctx['preview'] = _dt.build_preview(conn, dbname, int(domain_id), int(dest_id) if dest_id else None)
    finally:
        conn.close()
    return render_template('customers/domain_transfer.html', **ctx)


@app.route('/customers/domain-transfer/execute', methods=['POST'])
@login_required
@role_required('admin')
def customers_domain_transfer_execute():
    import domain_transfer as _dt, json as _json
    dbname = os.getenv('DB_NAME')
    domain_id = int(request.form['domain_id'])
    dest_id = int(request.form['dest_id'])
    confirm = (request.form.get('confirm') or '').strip()
    conn = get_db()
    try:
        dom = _dt.get_domain(conn, domain_id)
        if not dom or confirm != dom['domain']:
            result = {'ok': False, 'error': 'Confirmation text did not match the domain name - nothing was changed.'}
        else:
            result = _dt.execute_transfer(conn, dbname, domain_id, dest_id)
    finally:
        conn.close()
    try:
        ac = get_admin_db()
        with ac.cursor() as c:
            c.execute("INSERT INTO domain_transfer_log (domain_id, domain, source_user_id, dest_user_id, actor, ok, detail) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (domain_id, result.get('domain'), result.get('source_user_id'), dest_id, getattr(current_user, 'name', '?'), 1 if result.get('ok') else 0, _json.dumps(result, default=str)[:60000]))
        ac.commit(); ac.close()
    except Exception:
        pass
    return render_template('customers/domain_transfer.html', result=result, domain_q='', dest_q='', domain_matches=[], dest_matches=[], preview=None, domain_id=None, dest_id=None)

PIPELINE_STAGES = ['new', 'contacted', 'engaged', 'qualified', 'won', 'lost']
# IA spec §4: marketing owns new/contacted (works leads at scale, no human involved yet);
# sales owns engaged onward (a reply/click/form-submit means a human is waiting). Pipeline
# is scoped to sales's half only -- new/contacted live in Marketing -> Leads as a filterable
# table instead, the right shape for that volume (295+ rows) vs. a board (a handful of
# active deals a person is actually working).
PIPELINE_BOARD_STAGES = ['engaged', 'qualified', 'won', 'lost']


@app.route('/sales/pipeline')
@login_required
@role_required('admin', 'sales')
def sales_pipeline():
    """2026-09-03 addendum: replaces the old Pipeline, which had no DB table of its own --
    it re-rendered the same campaign-effectiveness.json Measure -> Campaigns already shows,
    computed from the pre-unification wp_citemetrix_score_leads/wp_wc_orders tables, with zero
    connection to leads.stage or to Direct/In-Person leads at all (see the audit finding this
    step was built from). This is a real stage-based view over the unified `leads` table,
    covering every path, with a live stage-change action -- "close the loop" meant making
    leads.stage the actual pipeline, not a second read-only funnel chart of the same numbers
    Campaigns already has. migrate_step1b.py's hourly sync was fixed alongside this so a
    manual move here doesn't get silently reverted on the next inbound sync.

    2026-09-04 (IA spec §4/§5): scoped to PIPELINE_BOARD_STAGES (engaged+) -- this is
    genuinely a board for the few leads a human is actively working, not a container for
    every lead regardless of whether anyone's replied. Also fetches the contacted count
    (unscoped by this board) so an empty Engaged column can point at the real next step
    (§5.2's example empty state) instead of a dead-end "No leads here.\""""
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            fmt = ','.join(['%s'] * len(PIPELINE_BOARD_STAGES))
            cur.execute(
                "SELECT leads.id, email, first_name, last_name, company, original_source, stage, "
                "stage_entered_at, leads.created_at, suppressed_at, source_override, dup_status, person_cluster_id, "
                "(SELECT payload FROM lead_events WHERE lead_id=leads.id AND type='free_check_completed' ORDER BY id DESC LIMIT 1) AS fc_payload, "
                "src.source AS batch_source "
                "FROM leads LEFT JOIN source_refs src ON src.id = leads.original_source_ref_id "
                f"WHERE excluded=0 AND stage IN ({fmt}) ORDER BY COALESCE(stage_entered_at, leads.created_at) DESC",
                PIPELINE_BOARD_STAGES
            )
            rows = cur.fetchall()
            cur.execute("SELECT COUNT(*) AS n FROM leads WHERE excluded=0 AND stage='contacted'")
            contacted_count = cur.fetchone()['n']
    finally:
        conn.close()

    for l in rows:
        source = l.get('source_override')
        if not source and l['original_source'] == 'inbound' and l.get('fc_payload'):
            try:
                source = json.loads(l['fc_payload']).get('utm_source')
            except (ValueError, TypeError):
                pass
        if not source:
            source = l.get('batch_source')
        l['source'] = source
        l['name'] = ((l.get('first_name') or '') + ' ' + (l.get('last_name') or '')).strip() or l.get('company') or None

    board = {s: [] for s in PIPELINE_BOARD_STAGES}
    for l in rows:
        board.setdefault(l['stage'], []).append(l)

    return render_template('sales/pipeline.html', board=board, stages=PIPELINE_BOARD_STAGES,
                           total=len(rows), contacted_count=contacted_count)







# ── Campaign Effectiveness (campaign→session→lead→sale; job: jobs/campaign_effectiveness.py) ──
def _source_comparison(campaigns):
    """Aggregate campaigns by list-source token (encoded in utm_campaign) for the DataZap-vs-CT-SOS decision."""
    MAP = (("ct_sos", "CT SOS"), ("datazap", "DataZap"), ("crossengaged", "Cross-engaged"),
           ("agency", "Agency (Marblism)"), ("subject_a", "Wave 2 A/B"), ("subject_b", "Wave 2 A/B"))
    buckets = {}
    for c in (campaigns or []):
        name = (c.get("campaign") or "").lower()
        src = None
        for key, label in MAP:
            if key in name:
                src = label; break
        if not src:
            continue
        b = buckets.setdefault(src, {"source": src, "campaigns": 0, "sent": 0, "opens": 0, "clicks": 0,
                                     "bounced": 0, "unsubs": 0, "sessions": 0, "scans": 0, "leads": 0})
        b["campaigns"] += 1
        for ks, kd in (("sent", "sent"), ("opens", "opens"), ("clicks", "clicks"), ("bounced", "bounced"),
                       ("unsubs", "unsubs"), ("sessions", "sessions"), ("scan_completed", "scans"), ("leads", "leads")):
            b[kd] += (c.get(ks) or 0)
    rows = []
    for b in buckets.values():
        sent = b["sent"] or 0
        rate = lambda x: (round(100.0 * x / sent, 2) if sent else 0.0)
        b["open_rate"] = rate(b["opens"]); b["click_rate"] = rate(b["clicks"]); b["bounce_rate"] = rate(b["bounced"])
        rows.append(b)
    rows.sort(key=lambda x: x["sent"], reverse=True)
    return rows


def _compare_campaigns(campaigns, na, nb):
    """Side-by-side A/B comparison of two campaigns (by utm_campaign name) with per-row winner."""
    idx = {(c.get("campaign") or "(none)"): c for c in (campaigns or [])}
    a = idx.get(na); b = idx.get(nb)
    if not a or not b:
        return None
    def rate(x, d): return (100.0 * (x or 0) / d) if d else 0.0
    def g(c, k): return c.get(k) or 0
    specs = [
        ("Sent", g(a, "sent"), g(b, "sent"), "int", None),
        ("Open rate", rate(g(a, "opens"), g(a, "sent")), rate(g(b, "opens"), g(b, "sent")), "pct", "hi"),
        ("Click rate", rate(g(a, "clicks"), g(a, "sent")), rate(g(b, "clicks"), g(b, "sent")), "pct", "hi"),
        ("CTOR", rate(g(a, "clicks"), g(a, "opens")), rate(g(b, "clicks"), g(b, "opens")), "pct", "hi"),
        ("Bounce", rate(g(a, "bounced"), g(a, "sent")), rate(g(b, "bounced"), g(b, "sent")), "pct", "lo"),
        ("Unsub", rate(g(a, "unsubs"), g(a, "sent")), rate(g(b, "unsubs"), g(b, "sent")), "pct", "lo"),
        ("Sessions", g(a, "sessions"), g(b, "sessions"), "int", "hi"),
        ("Scan focus", g(a, "form_focus"), g(b, "form_focus"), "int", "hi"),
        ("Scans", g(a, "scan_completed"), g(b, "scan_completed"), "int", "hi"),
        ("Leads", g(a, "leads"), g(b, "leads"), "int", "hi"),
    ]
    rows = []
    for label, av, bv, fmt, direction in specs:
        if fmt == "pct":
            ra, rb = round(av, 2), round(bv, 2)
            a_disp = "%.2f%%" % av; b_disp = "%.2f%%" % bv; delta = "%+.2f pp" % (av - bv)
        else:
            ra, rb = int(av), int(bv)
            a_disp = "{:,}".format(int(av)); b_disp = "{:,}".format(int(bv)); delta = "%+d" % (int(av) - int(bv))
        winner = None
        if direction and ra != rb:
            better_a = (ra > rb) if direction == "hi" else (ra < rb)
            winner = "a" if better_a else "b"
        rows.append({"label": label, "a": a_disp, "b": b_disp, "delta": delta, "winner": winner})
    return {"a_name": na, "b_name": nb, "rows": rows}


def _campaign_anomalies(campaigns):
    """Threshold banners for the Campaigns page (per-campaign, meaningful-volume email sends)."""
    out = []
    zero_scan = []
    for c in (campaigns or []):
        name = c.get("campaign") or "(none)"
        sent = c.get("sent") or 0
        opens = c.get("opens") or 0; clicks = c.get("clicks") or 0
        bounced = c.get("bounced") or 0; compl = c.get("complaints") or 0; unsubs = c.get("unsubs") or 0
        if sent >= 100:
            br = 100.0 * bounced / sent; cr = 100.0 * compl / sent; ur = 100.0 * unsubs / sent
            if opens > 0 and clicks > opens:
                out.append(("warning", name, "Click rate exceeds open rate \u2014 email security scanners (Barracuda, Proofpoint) are auto-clicking links. The real click rate is lower; use GA4 sessions for actual human engagement."))
            if br > 3:
                out.append(("alert", name, "Bounce rate %.1f%% exceeds 3%% \u2014 this damages sender reputation across ALL campaigns. Consider pausing sends to this list and running verification (NeverBounce, ZeroBounce) before resuming." % br))
            elif br > 2:
                out.append(("warning", name, "Bounce rate %.1f%% is elevated (>2%%). Monitor closely \u2014 if it climbs above 3%%, pause and verify the list." % br))
            if cr > 0.1:
                out.append(("alert", name, "Complaint rate %.2f%% exceeds 0.1%% \u2014 SES may suspend your sending. Review the email content and list targeting immediately." % cr))
            if ur > 10:
                out.append(("alert", name, "Unsubscribe rate %.1f%% exceeds 10%% \u2014 stop sending to this list. The targeting or messaging is fundamentally mismatched." % ur))
            elif ur > 5:
                out.append(("warning", name, "Unsubscribe rate %.1f%% exceeds 5%% \u2014 the segment may include contacts who don\u2019t identify as prospects. Consider tightening the segment definition." % ur))
        sess = c.get("sessions") or 0; scans = c.get("scan_completed") or 0
        if sess > 100 and scans == 0:
            zero_scan.append(name)
    if zero_scan:
        _names = ", ".join(zero_scan[:6]) + ((" +%d more" % (len(zero_scan) - 6)) if len(zero_scan) > 6 else "")
        out.append(("info", "%d campaigns" % len(zero_scan), "Drove sessions but produced no scans (%s) \u2014 visitors are landing but not engaging the check tool. Review the landing-page experience." % _names))
    _ord = {"alert": 0, "warning": 1, "info": 2}
    out.sort(key=lambda x: _ord.get(x[0], 3))
    return [{"severity": sv, "label": nm, "text": tx} for sv, nm, tx in out]


def _deliverability_anomalies(ses):
    """SES-account-level threshold banners for the Deliverability page."""
    out = []
    if ses:
        w = ses.get("window", {}) or {}
        att = w.get("attempts") or 0
        if att:
            br = 100.0 * (w.get("bounces") or 0) / att
            cr = 100.0 * (w.get("complaints") or 0) / att
            if br > 5:
                out.append(("alert", "14-day bounce rate %.1f%% exceeds 5%% \u2014 the SES account is at risk of suspension. Stop all sends and investigate list quality." % br))
            if cr > 0.1:
                out.append(("alert", "14-day complaint rate %.2f%% exceeds 0.1%% \u2014 the SES account is at risk of throttling. Review recent campaign content and targeting." % cr))
        tr = ses.get("trend", []) or []
        if len(tr) >= 2:
            a1 = tr[-1].get("attempts") or 0; a0 = tr[-2].get("attempts") or 0
            if a0 > 0 and a1 > 5 * a0:
                out.append(("warning", "Send volume spiked %.0fx over the previous day \u2014 monitor bounce and complaint rates closely for the next 24 hours. ISPs may throttle deliverability on sudden volume increases." % (a1 / a0)))
    return [{"severity": sv, "text": tx} for sv, tx in out]


@app.route('/marketing/measure/effectiveness')
@login_required
@role_required('admin', 'marketing')
def campaign_effectiveness():
    """Campaign effectiveness — GA4 funnel + deterministic email lead→sale join + GSC halo.
    Data produced by the daily cron (jobs/campaign_effectiveness.py) into reports/campaigns/. Read-only."""
    import os as _os, json as _json
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'reports', 'campaigns', 'campaign-effectiveness.json')
    try:
        data = _json.load(open(path))
    except Exception:
        data = None
    anomalies = _campaign_anomalies(data.get('campaigns', []) if data else [])
    camp_names = sorted({(c.get('campaign') or '(none)') for c in (data.get('campaigns') or [])}) if data else []
    ca = request.args.get('ca', ''); cb = request.args.get('cb', '')
    compare = _compare_campaigns(data['campaigns'], ca, cb) if (data and ca and cb) else None
    source_rows = _source_comparison(data.get('campaigns', []) if data else [])
    return render_template('marketing/campaign_effectiveness.html', data=data, anomalies=anomalies,
                           camp_names=camp_names, ca=ca, cb=cb, compare=compare, source_rows=source_rows)


@app.route('/marketing/campaign-effectiveness')
@login_required
def campaign_effectiveness_redirect():
    # step1brief.md SS17 step 3: Measure tabbed, old bookmark redirects.
    return redirect(url_for('campaign_effectiveness'), code=301)


# ── Sendy campaign-stats ingest — push from the Sendy box's read-only extractor.
#    Dedicated secret (SENDY_INGEST_SECRET), independently revocable. Mirrors /api/alerts/ingest. ──
@app.route('/api/marketing/sendy-ingest', methods=['POST'])
def api_sendy_ingest():
    import hmac as _hmac
    expected = os.getenv('SENDY_INGEST_SECRET', '')
    provided = request.headers.get('X-Sendy-Ingest-Secret', '')
    if not expected:
        return jsonify({'error': 'sendy ingest not configured'}), 503
    if not _hmac.compare_digest(expected, provided):
        return jsonify({'error': 'unauthorized'}), 401
    try:
        payload = request.get_json(force=True, silent=True) or {}
        rows = payload.get('campaigns')
        if not isinstance(rows, list):
            return jsonify({'error': 'campaigns must be a list'}), 400
        conn = get_admin_db(); n = 0
        with conn.cursor() as cursor:
            for r in rows:
                try:
                    cid = int(r.get('campaign_id'))
                except (TypeError, ValueError):
                    continue
                sa = r.get('sent_at') or None
                cursor.execute("""
                    INSERT INTO email_campaign_stats
                        (campaign_id, title, utm_campaign, sent, opens, clicks, bounced, complaints, unsubs, sent_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, UTC_TIMESTAMP())
                    ON DUPLICATE KEY UPDATE
                        title=VALUES(title), utm_campaign=VALUES(utm_campaign),
                        sent=VALUES(sent), opens=VALUES(opens), clicks=VALUES(clicks),
                        bounced=VALUES(bounced), complaints=VALUES(complaints), unsubs=VALUES(unsubs),
                        sent_at=VALUES(sent_at), updated_at=UTC_TIMESTAMP()
                """, (cid, str(r.get('title') or '')[:255], str(r.get('utm_campaign') or '')[:255],
                      int(r.get('sent') or 0), int(r.get('opens') or 0), int(r.get('clicks') or 0),
                      int(r.get('bounced') or 0), int(r.get('complaints') or 0), int(r.get('unsubs') or 0), sa))
                n += 1

            # step1brief.md-adjacent (2026-09-03): syncs Sendy's own unsubscribed/bounced/complaint
            # flags into a local mirror (same shape/discipline as ses_suppressions) so
            # leads_drip.py's import screen can check it without a live cross-box query --
            # this box has no network path to Sendy's DB, and shouldn't be given one just for this.
            # Full-sync semantics: every row in this payload gets a fresh synced_at; anything NOT
            # touched this run is stale (no longer suppressed in Sendy) and gets pruned -- but only
            # if the payload is a plausible full list (guards a broken/empty export from silently
            # un-suppressing everyone, same 10% abort threshold ses_suppressions' sync already uses).
            # A full-account export runs to 60k+ rows -- executemany (pymysql rewrites a plain
            # INSERT ... VALUES into one multi-row statement) instead of one execute() per row,
            # same reason jobs/deliverability.py's ses_suppressions sync already does this.
            sup_rows = payload.get('suppressions')
            sup_n = 0
            if isinstance(sup_rows, list):
                sync_started_at = datetime.utcnow()
                clean = []
                for s in sup_rows:
                    email = (s.get('email') or '').strip().lower()
                    reason = s.get('reason')
                    if not email or reason not in ('unsubscribed', 'bounced', 'complaint'):
                        continue
                    clean.append((email, reason, sync_started_at))
                if clean:
                    cursor.executemany(
                        "INSERT INTO sendy_suppressions (email_address, reason, synced_at) VALUES (%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE reason=VALUES(reason), synced_at=VALUES(synced_at)",
                        clean
                    )
                sup_n = len(clean)
                cursor.execute("SELECT COUNT(*) AS c FROM sendy_suppressions")
                existing = cursor.fetchone()['c']
                cursor.execute("SELECT COUNT(*) AS c FROM sendy_suppressions WHERE synced_at < %s", (sync_started_at,))
                stale = cursor.fetchone()['c']
                if existing > 0 and stale > existing * 0.10:
                    app.logger.warning(f'sendy_suppressions: prune ABORTED -- would delete {stale}/{existing}, over the 10% safety threshold. Upserts still committed.')
                else:
                    cursor.execute("DELETE FROM sendy_suppressions WHERE synced_at < %s", (sync_started_at,))
        conn.commit(); conn.close()
        return jsonify({'success': True, 'rows': n, 'suppressions': sup_n}), 200
    except Exception:
        app.logger.exception('sendy ingest failed')
        return jsonify({'error': 'ingest failed'}), 500


# ── Sources — step1brief.md SS17 step 4: the campaign builder becomes the
#    source_refs editor. Generating a tagged URL creates the source record
#    directly in the unified `leads` pool's attribution table (kind='ad_campaign',
#    path='inbound') instead of a separate marketing_campaigns table nothing else
#    ever joined against. label carries the campaign tag (was `campaign` column);
#    source/medium/content/term/goal/base_url/tagged_url added to source_refs
#    for this. Old marketing_campaigns retired (0 rows, no other consumers). ──
@app.route('/marketing/sources', methods=['GET'])
@login_required
@role_required('admin', 'marketing')
def campaign_builder():
    rows = []
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM source_refs WHERE kind='ad_campaign' ORDER BY created_at DESC LIMIT 200")
            rows = cur.fetchall()
        conn.close()
    except Exception:
        app.logger.exception('campaign builder list failed')
    return render_template('marketing/campaign_builder.html', campaigns=rows)


@app.route('/marketing/campaign-builder')
@login_required
def campaign_builder_redirect():
    return redirect(url_for('campaign_builder'), code=301)


@app.route('/marketing/campaign-builder/save', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def campaign_builder_save():
    import re
    from urllib.parse import urlencode
    def slug(v):
        return re.sub(r'[^a-z0-9_-]+', '', (v or '').strip().lower().replace(' ', '_'))[:120]
    base_url = (request.form.get('base_url') or '').strip()[:255]
    source   = slug(request.form.get('source'))
    medium   = slug(request.form.get('medium'))
    campaign = slug(request.form.get('campaign'))
    content  = slug(request.form.get('content'))
    term     = slug(request.form.get('term'))
    goal     = (request.form.get('goal') or '').strip()[:160]
    if not (base_url.startswith('http') and source and medium and campaign):
        return redirect(url_for('campaign_builder'))
    params = {'utm_source': source, 'utm_medium': medium, 'utm_campaign': campaign}
    if content: params['utm_content'] = content
    if term:    params['utm_term'] = term
    sep = '&' if '?' in base_url else '?'
    tagged = base_url + sep + urlencode(params)
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_refs
                (path, kind, label, source, medium, content, term, goal, base_url, tagged_url, uploaded_by, created_at)
                VALUES ('inbound','ad_campaign',%s,%s,%s,%s,%s,%s,%s,%s,%s, UTC_TIMESTAMP())""",
                (campaign, source, medium, content or None, term or None,
                 goal or None, base_url, tagged, current_user.id))
        conn.commit(); conn.close()
    except Exception:
        app.logger.exception('campaign builder save failed')
    return redirect(url_for('campaign_builder'))


# ── Publication Log — CITEMETRIX-MEASUREMENT-ARCHITECTURE-2026-09-09.md §6:
#    "one authoritative content record and it lives in the product." This is a
#    superset VIEW, not a second registry: engine-generated companions are read
#    live from the WP product DB (get_db(), same connection migrate_beta_signups.py
#    already uses to reach wp_citemetrix_ce_* tables) and merged in Python with
#    hand-entered posts, which write into source_refs (kind='publication') --
#    giving decisions-v2 §2 ruling 10 the interface it was missing. No caching,
#    no nightly sync, no second table: always current, and there's nothing to
#    go stale.
#
#    Same channel vocabulary as CiteMetrix_Content_Engine::COMPANION_RECIPES in
#    the WP plugin (4.58.0) -- no shared runtime to import a registry from, so
#    PUBLICATION_CHANNELS is kept in sync by hand, same caveat as
#    jobs/traffic.py's CHANNEL_NORMALIZATION_MAP.
PUBLICATION_CHANNELS = {
    'linkedin_article': {'label': 'LinkedIn Article', 'utm_source': 'linkedin', 'utm_medium': 'article', 'link_policy': 'tracked'},
    'linkedin_post':    {'label': 'LinkedIn Post',    'utm_source': 'linkedin', 'utm_medium': 'organic_social', 'link_policy': 'tracked'},
    'reddit':           {'label': 'Reddit',           'utm_source': 'reddit',   'utm_medium': 'organic_social', 'link_policy': 'none'},
    'medium':           {'label': 'Medium',           'utm_source': 'medium',   'utm_medium': 'article', 'link_policy': 'tracked'},
    'substack':         {'label': 'Substack',         'utm_source': 'substack', 'utm_medium': 'article', 'link_policy': 'tracked'},
    'x':                {'label': 'X',                'utm_source': 'x',        'utm_medium': 'organic_social', 'link_policy': 'tracked'},
    'facebook':         {'label': 'Facebook',         'utm_source': 'facebook', 'utm_medium': 'organic_social', 'link_policy': 'tracked'},
    'threads':          {'label': 'Threads',          'utm_source': 'threads',  'utm_medium': 'organic_social', 'link_policy': 'tracked'},
    'instagram':        {'label': 'Instagram',        'utm_source': 'instagram', 'utm_medium': 'organic_social', 'link_policy': 'plain'},
}


def _pub_channel_label(slug):
    ch = PUBLICATION_CHANNELS.get(slug)
    return ch['label'] if ch else (slug or '—')


@app.route('/marketing/measure/publications')
@login_required
@role_required('admin', 'marketing')
def measure_publications():
    rows = []
    try:
        conn = get_db()
        with conn.cursor() as cur:
            cur.execute("""SELECT c.id, c.channel, c.status, c.title, c.published_url,
                                  c.published_at, c.tracked_url, c.rate_at_publish, g.topic
                           FROM wp_citemetrix_ce_companions c
                           JOIN wp_citemetrix_ce_generations g ON g.id = c.generation_id
                           WHERE c.published_at IS NOT NULL
                           ORDER BY c.published_at DESC LIMIT 200""")
            for c in cur.fetchall():
                rows.append({
                    'source_type': 'engine', 'when': c['published_at'], 'platform': c['channel'],
                    'platform_label': _pub_channel_label(c['channel']),
                    'title': c['title'] or c['topic'], 'url': c['published_url'],
                    'tracked_url': c['tracked_url'],
                    'not_tracked': (PUBLICATION_CHANNELS.get(c['channel'], {}).get('link_policy') == 'none'),
                    'reach': None, 'notes': None, 'rate_at_publish': c['rate_at_publish'],
                })
        conn.close()
    except Exception:
        app.logger.exception('measure_publications: product DB read failed')

    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""SELECT id, source, label, base_url, tagged_url, reach, notes,
                                  published_at, created_at
                           FROM source_refs WHERE kind='publication'
                           ORDER BY COALESCE(published_at, created_at) DESC LIMIT 200""")
            for r in cur.fetchall():
                rows.append({
                    'source_type': 'manual', 'when': r['published_at'] or r['created_at'],
                    'platform': r['source'], 'platform_label': _pub_channel_label(r['source']),
                    'title': r['label'], 'url': r['base_url'], 'tracked_url': r['tagged_url'],
                    'not_tracked': (PUBLICATION_CHANNELS.get(r['source'], {}).get('link_policy') == 'none'),
                    'reach': r['reach'], 'notes': r['notes'], 'rate_at_publish': None,
                })
        conn.close()
    except Exception:
        app.logger.exception('measure_publications: admin DB read failed')

    rows.sort(key=lambda r: r['when'] or datetime.min, reverse=True)
    return render_template('marketing/publications.html', rows=rows, channels=PUBLICATION_CHANNELS)


@app.route('/marketing/measure/publications/save', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def publications_save():
    import re
    from urllib.parse import urlencode
    def slug(v):
        return re.sub(r'[^a-z0-9_-]+', '', (v or '').strip().lower().replace(' ', '_'))[:120]

    platform = request.form.get('platform') or ''
    title    = (request.form.get('title') or '').strip()[:255]
    base_url = (request.form.get('url') or '').strip()[:255]
    reach_raw = (request.form.get('reach') or '').strip()
    notes    = (request.form.get('notes') or '').strip() or None
    pub_on   = (request.form.get('published_on') or '').strip()

    ch = PUBLICATION_CHANNELS.get(platform)
    if not ch or not title or not base_url.startswith('http'):
        return redirect(url_for('measure_publications'))
    reach = int(reach_raw) if reach_raw.isdigit() else None
    published_at = pub_on if pub_on else datetime.utcnow().strftime('%Y-%m-%d')

    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO source_refs
                (path, kind, label, source, medium, base_url, reach, notes, published_at, uploaded_by, created_at)
                VALUES ('direct','publication',%s,%s,%s,%s,%s,%s,%s,%s, UTC_TIMESTAMP())""",
                (title, ch['utm_source'], ch['utm_medium'], base_url, reach, notes, published_at, current_user.id))
            new_id = cur.lastrowid
            # link_policy mirrors the WP registry: 'none' (Reddit) never gets a
            # tag -- a visible UTM there reads as self-promotion and risks the
            # post being removed, same reasoning whether the post came from the
            # engine or was typed by hand. 'plain' (Instagram) links through
            # untagged. Everything else gets the full utm_source/medium/campaign/
            # content tag, campaign derived from the title since there's no
            # separate campaign field on this simpler form.
            if ch['link_policy'] == 'none':
                tagged_url = None
            elif ch['link_policy'] == 'plain':
                tagged_url = base_url
            else:
                params = {'utm_source': ch['utm_source'], 'utm_medium': ch['utm_medium'],
                          'utm_campaign': slug(title) or f'post_{new_id}', 'utm_content': f'manual_{new_id}'}
                sep = '&' if '?' in base_url else '?'
                tagged_url = base_url + sep + urlencode(params)
            if tagged_url:
                cur.execute("UPDATE source_refs SET tagged_url=%s WHERE id=%s", (tagged_url, new_id))
        conn.commit(); conn.close()
    except Exception:
        app.logger.exception('publications_save failed')
    return redirect(url_for('measure_publications'))


# ── Daily Lead-Gen View — decisions-v3 §3/§6 step 3: "one page, date-first, all
#    six channel rows... Eric opens one page each morning and knows which
#    channel is working." Data comes from jobs/daily_leadgen.py (same
#    fire-and-forget-Popen-then-poll refresh pattern as Traffic, since the GA4 +
#    RDS + product-DB queries take a few seconds -- long enough to not want it
#    blocking a gunicorn worker on every page load).
@app.route('/marketing/measure/daily')
@login_required
@role_required('admin', 'marketing')
def measure_daily_leadgen():
    import os as _os, json as _json
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'reports', 'daily_leadgen', 'daily-leadgen-latest.json')
    try:
        data = _json.load(open(p))
    except Exception:
        data = None
    return render_template('marketing/daily_leadgen.html', data=data)


@app.route('/marketing/measure/daily/refresh', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def daily_leadgen_refresh():
    import subprocess, os as _os
    base = _os.path.dirname(_os.path.abspath(__file__))
    try:
        subprocess.Popen(
            [_os.path.join(base, 'venv', 'bin', 'python'), _os.path.join(base, 'jobs', 'daily_leadgen.py')],
            cwd=base, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
        )
    except Exception:
        app.logger.exception('failed to start daily_leadgen refresh')
    return jsonify({'success': True})


@app.route('/marketing/measure/daily/refresh-status')
@login_required
@role_required('admin', 'marketing')
def daily_leadgen_refresh_status():
    import os as _os, json as _json
    p = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'reports', 'daily_leadgen', 'daily-leadgen-latest.json')
    generated = None
    try:
        generated = _json.load(open(p)).get('generated')
    except Exception:
        pass
    return jsonify({'generated': generated})


# ── Leads — step1brief.md SS17 step 2: the merged view over the unified `leads`
#    pool (was two pages reading two different tables -- this one reading
#    wp_citemetrix_score_leads directly, drip-leads reading `leads`. That was
#    exactly the "view-owned lead table" the Gate in SS17 warns against.
#    Filterable by path/stage/suppressed/excluded/segment; campaign/source/
#    landing_page kept for campaign_effectiveness.html's drill-down links,
#    now matched against the free_check_completed lead_events payload since
#    that's where a free-check lead's UTM/source_page actually live post-3b. ──
@app.route('/marketing/leads')
@login_required
@role_required('admin', 'marketing')
def marketing_leads():
    def _lead_segment(sp):
        p = (sp or '').lower()
        if not p:
            return ('generic', None)
        if '/agencies' in p:
            return ('agency', None)
        if 'vs-profound' in p:
            return ('comparison', 'Profound')
        if 'vs-semrush' in p:
            return ('comparison', 'SEMrush')
        if 'vs-brandlight' in p:
            return ('comparison', 'Brandlight')
        if '/compare' in p or 'best-ai-visibility' in p or 'citemetrix-vs-' in p:
            return ('comparison', None)
        return ('generic', None)

    path_filter = (request.args.get('path') or '').strip()
    # IA spec §4: marketing's default view is the leads it's actually working -- new and
    # contacted, the two stages nothing has replied to yet. 'open' (not '') is the sentinel
    # for that default, distinguished from an explicit "All" pick via request.args.get()
    # returning None when the query param is entirely absent vs. '' when the user's own
    # dropdown pick was the blank "All" option -- collapsing those two into one ('' via
    # `or ''`, the pre-IA-spec code) is exactly what made "All" and "no filter yet" the same
    # state and lost the distinction this default needs.
    stage_param = request.args.get('stage')
    stage_filter = 'open' if stage_param is None else stage_param.strip()
    seg_filter = (request.args.get('segment') or '').strip()
    campaign_filter = (request.args.get('campaign') or request.args.get('source_campaign') or '').strip()
    source_filter = (request.args.get('source') or '').strip()
    landing_filter = (request.args.get('landing_page') or '').strip()
    show_excluded = request.args.get('show') == 'excluded'
    show_suppressed = request.args.get('suppressed') == '1'
    try:
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        page = 1
    PER_PAGE = 50

    where = ["excluded=1" if show_excluded else "excluded=0"]
    params = []
    if path_filter in ('in_person', 'direct', 'inbound', 'beta', 'chat'):
        where.append("original_source=%s"); params.append(path_filter)
    if stage_filter == 'open':
        where.append("stage IN ('new','contacted')")
    elif stage_filter:
        where.append("stage=%s"); params.append(stage_filter)
    if show_suppressed:
        where.append("suppressed_at IS NOT NULL")
    if campaign_filter:
        if campaign_filter == '(none)':
            where.append("NOT EXISTS (SELECT 1 FROM lead_events e WHERE e.lead_id=leads.id AND e.type='free_check_completed' AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.utm_campaign')) IS NOT NULL AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.utm_campaign'))!='')")
        else:
            where.append("EXISTS (SELECT 1 FROM lead_events e WHERE e.lead_id=leads.id AND e.type='free_check_completed' AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.utm_campaign'))=%s)")
            params.append(campaign_filter)
    if source_filter:
        if source_filter == '(none)':
            where.append("NOT EXISTS (SELECT 1 FROM lead_events e WHERE e.lead_id=leads.id AND e.type='free_check_completed' AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.utm_source')) IS NOT NULL AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.utm_source'))!='')")
        else:
            where.append("EXISTS (SELECT 1 FROM lead_events e WHERE e.lead_id=leads.id AND e.type='free_check_completed' AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.utm_source'))=%s)")
            params.append(source_filter)
    if landing_filter:
        where.append("EXISTS (SELECT 1 FROM lead_events e WHERE e.lead_id=leads.id AND e.type='free_check_completed' AND JSON_UNQUOTE(JSON_EXTRACT(e.payload,'$.source_page')) LIKE %s)")
        params.append('%' + landing_filter + '%')
    wsql = " WHERE " + " AND ".join(where)

    leads, batches = [], []
    path_counts = {'in_person': 0, 'direct': 0, 'inbound': 0}
    stage_counts, excluded_count, suppressed_count = {}, 0, 0
    enroll_by_lead = {}
    try:
        conn = get_admin_db()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT leads.id, email, first_name, last_name, company, title, phone, linkedin_url, "
                "source_override, original_source, stage, "
                "suppressed_at, suppression_reason, excluded, wp_nurture_owned, ai_referral_platform, "
                "legacy_id, legacy_source_table, leads.created_at, "
                "(SELECT payload FROM lead_events WHERE lead_id=leads.id AND type='free_check_completed' ORDER BY id DESC LIMIT 1) AS fc_payload, "
                "src.source AS batch_source, src.label AS batch_label "
                "FROM leads LEFT JOIN source_refs src ON src.id = leads.original_source_ref_id" + wsql + " ORDER BY leads.created_at DESC", params
            )
            leads = cur.fetchall()

            cur.execute("SELECT original_source, COUNT(*) n FROM leads WHERE excluded=%s GROUP BY original_source", (1 if show_excluded else 0,))
            # 2026-09-04 (IA spec §8): path_counts started as exactly the 3 original original_source
            # values (in_person/direct/inbound) -- the "All" chip's own count summed just those 3
            # keys in the template, which silently undercounted once 'beta'/'chat' became real
            # values (the actual All-filtered list was always correct; only this displayed number
            # was wrong). 'all' is now a real grand total across every value, not an in-template sum
            # of a hardcoded subset, so it can never drift out of sync with a future new path again.
            for r in cur.fetchall():
                path_counts[r['original_source']] = r['n']
            path_counts['all'] = sum(path_counts.values())

            cur.execute("SELECT COUNT(*) n FROM leads WHERE excluded=1")
            excluded_count = cur.fetchone()['n']
            cur.execute("SELECT COUNT(*) n FROM leads WHERE suppressed_at IS NOT NULL AND excluded=0")
            suppressed_count = cur.fetchone()['n']
            cur.execute("SELECT stage, COUNT(*) n FROM leads WHERE excluded=0 GROUP BY stage")
            stage_counts = {r['stage']: r['n'] for r in cur.fetchall()}

            lead_ids = [l['id'] for l in leads]
            if lead_ids:
                fmt = ','.join(['%s'] * len(lead_ids))
                cur.execute(
                    f"SELECT lead_id, status, campaign_id FROM drip_enrollments WHERE lead_id IN ({fmt}) ORDER BY id DESC",
                    lead_ids
                )
                for r in cur.fetchall():
                    enroll_by_lead.setdefault(r['lead_id'], r)

            cur.execute(
                "SELECT b.id, b.label AS name, b.source, b.path AS batch_path, b.kind AS batch_kind, b.uploaded_by, b.row_count, b.new_count, b.dup_count, "
                "b.suppressed_count, b.created_at, COUNT(DISTINCT t.lead_id) AS current_lead_count, "
                "COUNT(DISTINCT CASE WHEN l.email_verification_status='unverified' THEN l.id END) AS unverified_count, "
                "COUNT(DISTINCT CASE WHEN l.email_verification_status='valid' THEN l.id END) AS valid_count, "
                "COUNT(DISTINCT CASE WHEN l.email_verification_status IN ('invalid','disposable') THEN l.id END) AS bad_count, "
                "COUNT(DISTINCT CASE WHEN l.email_verification_status IN ('catchall','unknown') THEN l.id END) AS review_count, "
                "(SELECT status FROM email_verification_jobs j WHERE j.source_ref_id=b.id ORDER BY j.id DESC LIMIT 1) AS verify_job_status "
                "FROM source_refs b LEFT JOIN lead_source_touches t ON t.source_ref_id=b.id "
                "LEFT JOIN leads l ON l.id=t.lead_id "
                "WHERE b.kind IN ('import_batch','show') GROUP BY b.id ORDER BY b.created_at DESC"
            )
            batches = cur.fetchall()
        conn.close()
    except Exception:
        app.logger.exception('marketing leads query failed')

    seg_counts = {}
    for l in leads:
        l['brand_name'] = l['model_score'] = l['source_page'] = None
        l['utm_source'] = l['utm_medium'] = l['utm_campaign'] = None
        l['segment'] = l['competitor'] = None
        if l.get('fc_payload'):
            try:
                fc = json.loads(l['fc_payload'])
                l['brand_name'] = fc.get('brand_name')
                l['model_score'] = fc.get('model_score')
                l['source_page'] = fc.get('source_page')
                l['utm_source'] = fc.get('utm_source')
                l['utm_medium'] = fc.get('utm_medium')
                l['utm_campaign'] = fc.get('utm_campaign')
            except (ValueError, TypeError):
                pass
        if l['original_source'] == 'inbound':
            l['segment'], l['competitor'] = _lead_segment(l['source_page'])
            seg_counts[l['segment']] = seg_counts.get(l['segment'], 0) + 1
        else:
            # Direct/in-person leads have no free-check event, so no utm_source ever lands
            # here -- fall back to the batch's own source_refs.source (what was typed at
            # CSV-upload time, e.g. "Marblism LinkedIn") so the per-lead Source column isn't
            # silently blank for every non-inbound lead despite that data existing.
            l['utm_source'] = l.get('batch_source')
        # A manually-entered source_override (Addendum 29's Edit lead) always wins over
        # either derived value -- it's Eric correcting the record by hand, not a fallback.
        if l.get('source_override'):
            l['utm_source'] = l['source_override']
        l['enrollment'] = enroll_by_lead.get(l['id'])

    if seg_filter:
        leads = [l for l in leads if l.get('segment') == seg_filter]

    by_segment = [{'seg': s, 'n': seg_counts[s]} for s in ['agency', 'comparison', 'generic'] if seg_counts.get(s)]

    # Real pagination (2026-09-04, IA spec §9 bug 2): this used to be a flat LIMIT 500 on
    # the SQL query with no page controls at all -- correct at ~330 rows, silently drops
    # leads past 500 with zero indication once the list outgrows that. Paginated in Python,
    # after every filter (including seg_filter above, which is itself Python-side since
    # `segment` is derived from source_page, not a stored column SQL could filter/paginate
    # on directly) -- so the page count is always right for whatever's actually being shown,
    # not just the pre-segment-filter SQL row count.
    total_filtered = len(leads)
    total_pages = max(1, (total_filtered + PER_PAGE - 1) // PER_PAGE)
    page = min(page, total_pages)
    leads = leads[(page - 1) * PER_PAGE: page * PER_PAGE]

    return render_template('marketing/leads.html', leads=leads, batches=batches,
                           path_counts=path_counts, stage_counts=stage_counts,
                           f_path=path_filter, f_stage=stage_filter, f_segment=seg_filter,
                           f_campaign=campaign_filter, f_source=source_filter, f_landing=landing_filter,
                           show_excluded=show_excluded, show_suppressed=show_suppressed,
                           excluded_count=excluded_count, suppressed_count=suppressed_count,
                           by_segment=by_segment, page=page, total_pages=total_pages, total_filtered=total_filtered,
                           neverbounce_configured=bool(os.environ.get('NEVERBOUNCE_API_KEY')))


def _toggle_lead_excluded(conn, lead_id):
    """Flip leads.excluded and keep the upstream WP row in sync for inbound leads (source of
    truth for THAT flag is the WP row -- migrate_step1b.py's hourly sync would otherwise
    silently revert this within the hour). Shared by the Leads-page form action and the lead
    detail panel's JSON action so this WP-sync subtlety only has one home. Returns the new
    excluded value (0/1), or None if the lead doesn't exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT original_source, legacy_source_table, legacy_id, excluded FROM leads WHERE id=%s", (lead_id,))
        lead = cur.fetchone()
        if not lead:
            return None
        new_val = 0 if lead['excluded'] else 1
        cur.execute("UPDATE leads SET excluded=%s WHERE id=%s", (new_val, lead_id))
        conn.commit()
        if lead['legacy_source_table'] == 'wp_citemetrix_score_leads' and lead['legacy_id']:
            wp_conn = get_db()
            try:
                with wp_conn.cursor() as wcur:
                    wcur.execute("UPDATE wp_citemetrix_score_leads SET excluded=%s WHERE id=%s", (new_val, lead['legacy_id']))
                wp_conn.commit()
            finally:
                wp_conn.close()
    return new_val


@app.route('/marketing/leads/toggle', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def marketing_leads_toggle():
    lead_id = request.form.get('id')
    try:
        conn = get_admin_db()
        try:
            _toggle_lead_excluded(conn, lead_id)
        finally:
            conn.close()
    except Exception:
        app.logger.exception('lead toggle failed')
    return redirect(request.referrer or url_for('marketing_leads'))


# ── Edit lead — 2026-09-03: no edit path existed anywhere for an individual lead;
#    leads.title/phone/linkedin_url were live schema columns with zero readers/writers
#    anywhere in the app. source_override gives Eric a real way to correct an older
#    lead's source by hand -- Source is otherwise always derived (free-check utm_source,
#    or a CSV/event batch's own source_refs.source), never a stored per-lead value, so
#    "fix the source" had no field to write to until now. Takes priority over both
#    derived paths when set (see marketing_leads()'s template render). ──
@app.route('/api/leads/<int:lead_id>/update', methods=['POST'])
@login_required
@role_required('admin', 'marketing')
def api_leads_update(lead_id):
    try:
        first_name = (request.form.get('first_name') or '').strip()[:255]
        last_name = (request.form.get('last_name') or '').strip()[:255]
        company = (request.form.get('company') or '').strip()[:255]
        title = (request.form.get('title') or '').strip()[:255]
        phone = (request.form.get('phone') or '').strip()[:50]
        linkedin_url = (request.form.get('linkedin_url') or '').strip()[:500]
        source_override = (request.form.get('source_override') or '').strip()[:255]

        conn = get_admin_db()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE leads SET first_name=%s, last_name=%s, company=%s, title=%s,
                       phone=%s, linkedin_url=%s, source_override=%s WHERE id=%s""",
                    (first_name or None, last_name or None, company or None, title or None,
                     phone or None, linkedin_url or None, source_override or None, lead_id)
                )
            conn.commit()
        finally:
            conn.close()
        return jsonify({'success': True})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ── Sales > Pipeline stage change — the "close the loop" action: a human moving a lead
#    through new/contacted/engaged/qualified/won/lost, across ALL paths, not just the
#    free-check-only funnel the old Pipeline page showed. migrate_step1b.py's hourly
#    sync was fixed alongside this (2026-09-03 addendum) to stop overwriting stage on
#    every run for inbound leads -- without that fix, a manual move here would silently
#    revert within the hour, the exact same class of bug marketing_leads_toggle() above
#    already had to solve for `excluded`. ──
@app.route('/api/leads/<int:lead_id>/set-stage', methods=['POST'])
@login_required
@role_required('admin', 'sales', 'marketing')
def api_leads_set_stage(lead_id):
    stage = request.form.get('stage')
    if stage not in ('new', 'contacted', 'engaged', 'qualified', 'won', 'lost'):
        return jsonify({'error': 'Invalid stage.'}), 400
    try:
        conn = get_admin_db()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE leads SET stage=%s, stage_entered_at=NOW() WHERE id=%s", (stage, lead_id))
            conn.commit()
        finally:
            conn.close()
        return jsonify({'success': True})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def _platform_mentions(platform_results):
    """[{platform, mentioned}] for every platform the free check actually reached (status
    'success' only -- a skipped/errored platform has no real mention verdict to show).
    IA spec §6: 'which AI platforms mentioned them and which didn't' -- this is CiteMetrix's
    own value proposition sitting in a JSON column with no screen showing it until now."""
    if not isinstance(platform_results, dict):
        return []
    return [
        {'platform': r.get('platform'), 'mentioned': bool(r.get('mentioned'))}
        for r in platform_results.values()
        if isinstance(r, dict) and r.get('status') == 'success' and r.get('platform')
    ]


def _humanize_lead_event(ev_type, channel, payload):
    """Plain-language label for one lead_events row, for the lead detail panel's timeline
    (IA spec §6: 'in plain language'). lead_events.type's ENUM covers more states than
    anything currently writes -- as of this build only 'sourced', 'free_check_completed',
    and 'note' ever get inserted (see leads_drip.py/migrate_step1b.py) -- so an unmapped type
    falls back to a readable capitalized version of the enum value rather than rendering
    nothing once a future writer starts using 'opened'/'replied'/etc."""
    try:
        p = json.loads(payload) if isinstance(payload, str) else (payload or {})
    except (ValueError, TypeError):
        p = {}
    if ev_type == 'sourced':
        return f"Sourced via {channel}" if channel else "Sourced"
    if ev_type == 'free_check_completed':
        bits = ['Free check completed']
        if p.get('model_score') is not None:
            bits.append(f"— ModelScore {p['model_score']}")
        if p.get('brand_name'):
            bits.append(f"({p['brand_name']})")
        return ' '.join(bits)
    if ev_type == 'note':
        if p.get('event') == 'branched_to_warm':
            return 'Branched from cold sequence to warm sequence (completed a check)'
        if p.get('text'):
            return f"Note: {p['text']}"
        return 'Note added'
    return ev_type.replace('_', ' ').capitalize()


@app.route('/api/leads/<int:lead_id>/detail')
@login_required
@role_required('admin', 'sales', 'marketing')
def api_leads_detail(lead_id):
    """The lead detail panel's data source (IA spec §6) -- Identity/Attribution/Timeline/State
    for one lead, in one call. The timeline merges three genuinely separate tables: lead_events
    (sourced/free_check_completed/note today), drip_enrollments (the enrollment itself, at
    enrolled_at), and drip_send_log (each actual send attempt, joined through its step and
    campaign) -- lead_events alone does NOT record sends, that's drip_send_log's job, an easy
    thing to miss since both read as "history" tables at a glance. Returns raw JSON (not
    jsonify) with default=str on datetimes -- same pattern leads_drip.py already uses for
    lead_events payloads, so a date renders as a plain 'YYYY-MM-DD HH:MM:SS' string the
    frontend can display directly without a datetime-parsing library."""
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leads WHERE id=%s", (lead_id,))
            lead = cur.fetchone()
            if not lead:
                return jsonify({'error': 'Lead not found.'}), 404

            cur.execute(
                "SELECT t.path, t.occurred_at, sr.source, sr.label, sr.kind "
                "FROM lead_source_touches t LEFT JOIN source_refs sr ON sr.id = t.source_ref_id "
                "WHERE t.lead_id=%s ORDER BY t.occurred_at ASC", (lead_id,)
            )
            touches = cur.fetchall()

            cur.execute(
                "SELECT payload, occurred_at FROM lead_events WHERE lead_id=%s AND type='free_check_completed' "
                "ORDER BY id DESC LIMIT 1", (lead_id,)
            )
            fc_row = cur.fetchone()
            free_check = None
            if fc_row:
                try:
                    fc = json.loads(fc_row['payload']) if isinstance(fc_row['payload'], str) else fc_row['payload']
                    platform_results = fc.get('platform_results')
                    if isinstance(platform_results, str):
                        # migrate_step1b.py truncates this to 2000 chars before storing it,
                        # which very often lands mid-object and produces invalid JSON -- a
                        # parse failure here must not take out the rest of free_check (domain/
                        # model_score/etc. never depend on this field), so it's isolated in
                        # its own try rather than sharing the outer one.
                        try:
                            platform_results = json.loads(platform_results)
                        except (ValueError, TypeError):
                            platform_results = None
                    free_check = {
                        'domain': fc.get('domain'), 'brand_name': fc.get('brand_name'),
                        'category': fc.get('category'), 'model_score': fc.get('model_score'),
                        'brand_recognition': fc.get('brand_recognition'),
                        'utm_source': fc.get('utm_source'), 'utm_medium': fc.get('utm_medium'),
                        'utm_campaign': fc.get('utm_campaign'), 'source_page': fc.get('source_page'),
                        'occurred_at': fc_row['occurred_at'],
                        'platforms': _platform_mentions(platform_results),
                    }
                except (ValueError, TypeError):
                    pass

            # Full scan findings, straight from the product DB's own leads table (not the
            # lead_events 'free_check_completed' payload above -- that one is deliberately
            # thin, sourced from citemetrix_freecheck_log which has no platform_results
            # column; see leads_drip.py's process_check_completions() docstring). This is
            # the same wp_citemetrix_score_leads row the WP results page itself reads
            # (class-citemetrix-free-score.php), keyed uniquely on (email, domain), so a
            # lead who left an email while checking their score has a full, untruncated
            # per-platform response here -- exactly what's missing for writing a follow-up
            # that references what the lead actually saw.
            score_report = None
            if lead.get('email'):
                try:
                    pconn = get_db()
                    try:
                        with pconn.cursor() as pcur:
                            pcur.execute(
                                "SELECT domain, brand_name, model_score, platform_results, scan_query, created_at "
                                "FROM wp_citemetrix_score_leads WHERE email=%s ORDER BY created_at DESC LIMIT 1",
                                (lead['email'],)
                            )
                            sr = pcur.fetchone()
                    finally:
                        pconn.close()
                    if sr:
                        # platform_results is NOT a flat {platform: result} map -- it's the
                        # full scan blob class-citemetrix-free-score.php builds: brand_results
                        # (asked each platform directly about the brand -- recognition/
                        # sentiment/the actual answer text) and category_results (asked each
                        # platform a generic category query, e.g. "best digital marketing
                        # agency", with NO brand name in the prompt -- this is what
                        # appeared_on/absent_on/competitors are computed from, and it's the
                        # real "here's who AI recommends instead of you" finding).
                        pr = json.loads(sr['platform_results']) if sr['platform_results'] else {}
                        def _platform_list(results_dict):
                            if not isinstance(results_dict, dict):
                                return []
                            return [
                                {'platform': r.get('platform'), 'status': r.get('status'),
                                 'mentioned': r.get('mentioned'), 'sentiment': r.get('sentiment'),
                                 'response': r.get('response')}
                                for r in results_dict.values() if isinstance(r, dict) and r.get('platform')
                            ]
                        score_report = {
                            'domain': sr['domain'], 'brand_name': sr['brand_name'],
                            'category': pr.get('category'), 'category_query': pr.get('category_query'),
                            'model_score': sr['model_score'],
                            'brand_recognition': pr.get('brand_recognition'),
                            'category_visibility': pr.get('category_visibility'),
                            'competitors': pr.get('competitors') or [],
                            'appeared_on': pr.get('appeared_on') or [],
                            'absent_on': pr.get('absent_on') or [],
                            'created_at': sr['created_at'],
                            'brand_platforms': _platform_list(pr.get('brand_results')),
                            'category_platforms': _platform_list(pr.get('category_results')),
                        }
                except (pymysql.MySQLError, ValueError, TypeError):
                    # cross-box product DB unreachable, or a malformed platform_results blob --
                    # either way this is enrichment, not the panel's core data, so it must not
                    # take the rest of the lead detail down with it
                    score_report = None

            cur.execute(
                "SELECT type, channel, occurred_at, payload FROM lead_events WHERE lead_id=%s ORDER BY occurred_at DESC", (lead_id,)
            )
            timeline = [
                {'occurred_at': r['occurred_at'], 'text': _humanize_lead_event(r['type'], r['channel'], r['payload'])}
                for r in cur.fetchall()
            ]

            cur.execute(
                "SELECT e.id, e.campaign_id, c.name AS campaign_name, e.status, e.current_step, e.enrolled_at, e.next_send_due_at "
                "FROM drip_enrollments e JOIN drip_campaigns c ON c.id=e.campaign_id WHERE e.lead_id=%s ORDER BY e.enrolled_at DESC",
                (lead_id,)
            )
            enrollments = cur.fetchall()
            for e in enrollments:
                timeline.append({'occurred_at': e['enrolled_at'], 'text': f"Enrolled in {e['campaign_name']}"})

            if enrollments:
                enrollment_ids = [e['id'] for e in enrollments]
                fmt = ','.join(['%s'] * len(enrollment_ids))
                cur.execute(
                    f"SELECT l.enrollment_id, l.sent_at, l.status AS send_status, s.step_order, c.name AS campaign_name "
                    f"FROM drip_send_log l JOIN drip_steps s ON s.id=l.step_id JOIN drip_enrollments e ON e.id=l.enrollment_id "
                    f"JOIN drip_campaigns c ON c.id=e.campaign_id WHERE l.enrollment_id IN ({fmt})",
                    enrollment_ids
                )
                verbs = {'sent': 'sent', 'failed': 'failed to send', 'suppressed': 'suppressed', 'blocked': 'blocked', 'cancelled': 'cancelled'}
                for r in cur.fetchall():
                    verb = verbs.get(r['send_status'], r['send_status'])
                    timeline.append({'occurred_at': r['sent_at'], 'text': f"{r['campaign_name']} step {r['step_order']} {verb}"})

            timeline = [t for t in timeline if t['occurred_at'] is not None]
            timeline.sort(key=lambda x: x['occurred_at'], reverse=True)

            dup_siblings = []
            if lead.get('dup_status') == 'possible_dup' and lead.get('person_cluster_id'):
                cur.execute(
                    "SELECT id, email, first_name, last_name, stage FROM leads WHERE person_cluster_id=%s AND id != %s",
                    (lead['person_cluster_id'], lead_id)
                )
                dup_siblings = cur.fetchall()
    finally:
        conn.close()

    body = json.dumps({
        'lead': lead, 'touches': touches, 'free_check': free_check, 'score_report': score_report,
        'timeline': timeline, 'enrollments': enrollments, 'dup_siblings': dup_siblings,
    }, default=str)
    return Response(body, mimetype='application/json')


@app.route('/api/leads/<int:lead_id>/toggle-exclude', methods=['POST'])
@login_required
@role_required('admin', 'sales', 'marketing')
def api_leads_toggle_exclude(lead_id):
    """JSON counterpart to marketing_leads_toggle() (a form-POST + redirect) for the lead
    detail panel, which stays open on a modal rather than reloading the page. Shares the
    exact same WP-sync logic via _toggle_lead_excluded() -- see that function's docstring."""
    conn = get_admin_db()
    try:
        new_val = _toggle_lead_excluded(conn, lead_id)
    finally:
        conn.close()
    if new_val is None:
        return jsonify({'error': 'Lead not found.'}), 404
    return jsonify({'success': True, 'excluded': bool(new_val)})


@app.route('/api/leads/<int:lead_id>/note', methods=['POST'])
@login_required
@role_required('admin', 'sales', 'marketing')
def api_leads_add_note(lead_id):
    text = (request.form.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Note text is required.'}), 400
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM leads WHERE id=%s", (lead_id,))
            if not cur.fetchone():
                return jsonify({'error': 'Lead not found.'}), 404
            cur.execute(
                "INSERT INTO lead_events (lead_id, occurred_at, type, channel, payload) VALUES (%s,NOW(),'note',NULL,%s)",
                (lead_id, json.dumps({'text': text, 'by': current_user.name}))
            )
        conn.commit()
    finally:
        conn.close()
    return jsonify({'success': True})


@app.route('/api/drip/campaigns/list')
@login_required
@role_required('admin', 'sales', 'marketing')
def api_drip_campaigns_list():
    """Lightweight campaign picker for the lead detail panel's 'enroll in a sequence' action --
    the full /marketing/drip-campaigns page renders a lot more than a dropdown needs."""
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, active FROM drip_campaigns ORDER BY name ASC")
            campaigns = cur.fetchall()
    finally:
        conn.close()
    return jsonify({'campaigns': campaigns})


@app.route('/api/leads/<int:lead_id>/enroll', methods=['POST'])
@login_required
@role_required('admin', 'sales', 'marketing')
def api_leads_enroll(lead_id):
    """Manual single-lead enroll from the lead detail panel -- reuses leads_drip.
    enroll_single_lead(), the exact same helper the cold->warm branching feature uses, so a
    manual enroll and an automatic branch-enroll behave identically (same suppressed-row
    revival rule, same 'already on this campaign' no-op)."""
    import leads_drip
    campaign_id = request.form.get('campaign_id')
    if not campaign_id:
        return jsonify({'error': 'campaign_id is required.'}), 400
    conn = get_admin_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM leads WHERE id=%s", (lead_id,))
            if not cur.fetchone():
                return jsonify({'error': 'Lead not found.'}), 404
            result = leads_drip.enroll_single_lead(cur, conn, int(campaign_id), lead_id)
    finally:
        conn.close()
    if not result['enrolled']:
        return jsonify({'error': result['reason'] or 'Could not enroll.'}), 400
    return jsonify({'success': True})


# ── Campaign results (for the Builder popup) — returns one campaign's funnel from the
#    daily campaign-effectiveness snapshot, matched on the utm_campaign code. ──
@app.route('/marketing/campaign-builder/results')
@login_required
@role_required('admin', 'marketing')
def campaign_builder_results():
    import os as _os, json as _json
    camp = (request.args.get('campaign') or '').strip()
    path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                         'reports', 'campaigns', 'campaign-effectiveness.json')
    try:
        data = _json.load(open(path))
    except Exception:
        return jsonify({'found': False, 'campaign': camp})
    match = next((c for c in data.get('campaigns', []) if c.get('campaign') == camp), None)
    return jsonify({
        'found': match is not None,
        'campaign': camp,
        'generated': data.get('generated'),
        'metrics': match or {},
    })
