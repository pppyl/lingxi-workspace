"""Local-only sidebar issue counts; never invoke business status/refresh APIs."""
from copy import deepcopy
from datetime import datetime

from flask import jsonify
from recharge.cmcc import SHANGHAI
from coupon_reminders import reminder_summary

FAILED = {'failed', 'partial', 'interrupted', 'missed', 'exhausted', 'auth_failed',
          'login_required', 'rate_limited', 'ineligible', 'unknown_result', 'uncertain'}


def failure_counts(accounts, snapshots, now):
    known = {account['id'] for account in accounts}
    day, month = now.strftime('%Y%m%d'), now.strftime('%Y%m')
    counts = {'/lingxi/tasks': 0, '/star-day': 0, '/recharge/': 0,
              '/checkin': 0, '/monthly-coupon': 0, '/coupons': 0,
              'accounts': sum(account['status'] == 'login_required' for account in accounts)}
    counts['/lingxi/tasks'] = sum(job['login_id'] in known and job['state'] in FAILED
        for job in snapshots.get('lingxi', {}).values())
    counts['/recharge/'] = sum(job['state'] in FAILED for job in snapshots.get('recharge', {}).values())
    counts['/recharge/'] += sum(login_id in known and record.get('state')=='failed'
        for login_id,record in snapshots.get('wed_checkin',{}).get('records',{}).items())

    checkin = snapshots.get('checkin', {})
    for task in checkin.get('tasks', {}).values():
        if task['login_id'] not in known or not task['enabled'] or task['state'] not in FAILED:
            continue
        observed = checkin.get('observations', {}).get(task['login_id'], {})
        complete = (observed.get('date') == day and
            (observed.get('today_state') == 'signed' if task['kind'] == 'signin'
             else observed.get('flash_state') in ('claimed', 'already_claimed')))
        counts['/checkin'] += not complete

    for login_id, record in snapshots.get('monthly', {}).get('accounts', {}).items():
        if login_id not in known or not record['auto_enabled'] or record.get('completed_date') == day:
            continue
        observed = record.get('observation', {})
        rejected = (observed.get('month') == month and
                    any(observed.get(key) in FAILED for key in ('direct_state', 'bonus_state')))
        counts['/monthly-coupon'] += bool(record.get('query_error') or record.get('query_state') in FAILED or rejected)

    star = snapshots.get('star', {})
    problems = set()
    for login_id, record in star.get('accounts', {}).items():
        observed = record.get('observation', {})
        complete = observed.get('date') == day and observed.get('today_state') == 'signed' and observed.get('month_claimed')
        if login_id in known and not complete and record.get('query_state') in FAILED:
            problems.add(login_id)
    for job in star.get('jobs', {}).values():
        if job['state'] in ('stopped', 'cancelled') or job.get('results_month') != month:
            continue
        for login_id, result in job.get('results', {}).items():
            observed = star.get('accounts', {}).get(login_id, {}).get('observation', {})
            claimed = observed.get('date', '')[:6] == month and observed.get('month_claimed')
            if login_id in known and not claimed and result.get('state') in FAILED:
                problems.add(login_id)
    counts['/star-day'] = len(problems)
    # Accepted exchanges, closed/expired orders and unpaid orders are not failed
    # tasks. Uncertain submissions remain actionable until explicitly resolved.
    counts['/coupons'] = sum(record.get('login_id') in known and record['state'] in ('unknown', 'uncertain')
        for record in snapshots.get('coupons', {}).values())
    return counts


def register_navigation_status(app, accounts, sources, platforms):
    @app.get('/api/navigation/issues')
    def navigation_issues():
        snapshots = {}
        for key, (owner, attribute) in sources.items():
            with owner.lock:
                snapshots[key] = deepcopy(getattr(owner, attribute))
        now = datetime.now(SHANGHAI)
        known_accounts = accounts()
        counts = failure_counts(known_accounts, snapshots, now)
        reminders = reminder_summary(snapshots.get('coupon_reminders', {}),
            [account['id'] for account in known_accounts], snapshots.get('coupons', {}), now)
        features = app.extensions.get('feature_control')
        for path, feature_id in {'/lingxi/tasks':'lingxi', '/recharge/':'recharge', '/checkin':'checkin',
                '/star-day':'star-day', '/monthly-coupon':'monthly-coupon', '/coupons':'coupons'}.items():
            if features is not None and not features.enabled(feature_id):
                counts[path] = 0
                if feature_id == 'coupons':
                    reminders = {'accounts': {}, 'total': 0}
        with platforms.lock:
            for entry in platforms.platforms.values():
                if entry['stopped'] or features is not None and not features.enabled(entry['id']) or not entry['controller'].metadata().get('active'):
                    continue
                counter = getattr(entry['controller'].scheduler, 'failure_count', None)
                if counter is not None:
                    count = counter()
                    counts[entry['home']] = count
                    if entry['navigation'] == 'checkin':
                        counts['/checkin'] += count
        return jsonify(counts=counts, coupon_reminders=reminders, checked_at=now.isoformat())
