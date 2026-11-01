"""Independent daily progress and durable sign-in intents for the web channel."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import re
from threading import Event, RLock, Thread

from login_storage import LoginStorageError
from recharge.cmcc import SHANGHAI
from .client import WebCheckinClient, WebCheckinError

PLATFORM_ID = 'web-checkin'
DEFAULT_SETTINGS = {'enabled': True, 'time': '08:00'}
RETRY_MINUTES, MAX_FAILURES = 5, 5
OBSERVATION_FIELDS = ('today_signed', 'sign_date', 'cycle_complete', 'marked_times', 'total_times', 'prizes', 'channel', 'server_time')


def _message(value):
    return re.sub(r'1[3-9]\d{9}', lambda match: match[0][:3]+'****'+match[0][-4:], str(value))[:500]


class WebCheckinScheduler:
    def __init__(self, credentials, accounts, *, storage=None, client_factory=WebCheckinClient,
                 now=None, executor=None):
        self.credentials, self.accounts, self.storage = credentials, accounts, storage
        self.client_factory = client_factory
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.executor = executor or ThreadPoolExecutor(max_workers=12, thread_name_prefix='checkin-web')
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.running, self.pending_login, self.readiness = set(), set(), {}
        self.warning = self.load_error = ''
        self.data = {'settings': dict(DEFAULT_SETTINGS), 'observations': {}, 'records': {}, 'attempts': {}}
        if storage is not None:
            try:
                saved = storage.load_activity_data(PLATFORM_ID)
                if saved is not None:
                    if (not isinstance(saved, dict) or any(not isinstance(saved.get(k), dict) for k in self.data)
                            or not isinstance(saved['settings'].get('enabled'), bool)
                            or not isinstance(saved['settings'].get('time'), str)
                            or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', saved['settings']['time'])):
                        raise LoginStorageError('网页签到数据格式无效，已保留原数据库。')
                    self.data = saved
                known = {account['id'] for account in self.accounts()}
                updated = deepcopy(self.data)
                for key in ('observations', 'records', 'attempts'):
                    updated[key] = {login_id: value for login_id, value in updated[key].items() if login_id in known}
                for days in updated['attempts'].values():
                    for attempt in days.values():
                        if attempt.get('state') == 'running':
                            attempt.update(state='unknown_result' if attempt.get('submitted') else 'failed',
                                           uncertain=bool(attempt.get('submitted')),
                                           message='上次执行中断，将核实官方状态，不自动重复提交。')
                self._commit(updated)
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)

    def _commit(self, data):
        if self.load_error:
            raise WebCheckinError(self.load_error, 'STORAGE_ERROR', 503)
        if self.storage is not None:
            self.storage.save_activity_data(PLATFORM_ID, data)
        self.data = data

    def _account(self, login_id):
        account = next((account for account in self.accounts() if account['id'] == login_id), None)
        if account is None:
            raise WebCheckinError('共享账号不存在，请刷新账号列表。', 'ACCOUNT_MISSING', 404)
        if account['status'] != 'active':
            raise WebCheckinError('请先恢复该共享账号登录。', 'APP_LOGIN_REQUIRED', 401)
        return account

    def update_settings(self, payload):
        if (not isinstance(payload, dict) or not isinstance(payload.get('enabled'), bool)
                or not isinstance(payload.get('time'), str)
                or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', payload['time'])):
            raise WebCheckinError('请选择是否自动签到，并填写正确的北京时间。', 'INVALID_SETTINGS', 400)
        with self.lock:
            updated = deepcopy(self.data)
            updated['settings'] = {key: payload[key] for key in DEFAULT_SETTINGS}
            self._commit(updated)
        return {'message': '设置已保存，默认应用于所有有效账号。', 'settings': deepcopy(updated['settings'])}

    def _next(self, account, observation, attempt, now):
        settings = self.data['settings']
        if not settings['enabled'] or account['status'] != 'active' or attempt.get('state') == 'login_required':
            return None
        hour, minute = map(int, settings['time'].split(':'))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if ((observation.get('today_signed') and observation.get('sign_date') == now.date().isoformat())
                or observation.get('cycle_complete') or attempt.get('state') in ('completed', 'ineligible', 'rate_limited')
                or attempt.get('failures', 0) >= MAX_FAILURES):
            return (start+timedelta(days=1)).isoformat()
        last = attempt.get('finished_at') or attempt.get('started_at')
        if last and attempt.get('state') in ('failed', 'unknown_result'):
            return max(start, datetime.fromisoformat(last)+timedelta(minutes=RETRY_MINUTES)).isoformat()
        return max(start, now).isoformat()

    def status(self):
        now, accounts = self.now(), self.accounts()
        day = now.date().isoformat()
        with self.lock:
            rows = []
            for account in accounts:
                login_id = account['id']
                observation = deepcopy(self.data['observations'].get(login_id, {}))
                attempt = self.data['attempts'].get(login_id, {}).get(day, {})
                signed = bool(observation.get('today_signed') and observation.get('sign_date') == day)
                state = ('running' if login_id in self.running else 'signed' if signed
                         else attempt.get('state', 'completed' if observation.get('cycle_complete') else 'unknown'))
                rows.append({'id': login_id, 'label': account.get('name') or account.get('phone_masked', ''),
                    'phone_masked': account.get('phone_masked', ''), 'status': account['status'],
                    'activity_state': state, 'today_signed': signed, 'cycle_complete': bool(observation.get('cycle_complete')),
                    'marked_times': observation.get('marked_times'), 'total_times': observation.get('total_times'),
                    'prizes': observation.get('prizes', []), 'channel': observation.get('channel', ''),
                    'message': attempt.get('message') or (observation.get('message')
                        if str(observation.get('last_at', '')).startswith(day) else '') or '今日尚未查询网页渠道签到。',
                    'last_at': observation.get('last_at') or attempt.get('finished_at'),
                    'next_run_at': self._next(account, observation, attempt, now),
                    'records': deepcopy(self.data['records'].get(login_id, []))})
            return {'settings': deepcopy(self.data['settings']), 'accounts': rows,
                    'server_time': now.isoformat(), 'warning': self.warning}

    def run(self, login_id, kind='refresh'):
        if not isinstance(login_id, str) or kind not in ('refresh', 'signin'):
            raise WebCheckinError('请选择账号及签到操作。', 'INVALID_OPERATION', 400)
        self._account(login_id)
        accepted = self._enqueue(login_id, kind)
        return {'message': '已安排查询官方网页签到状态。' if accepted and kind == 'refresh' else
                '已安排签到，先核对今日官方进度。' if accepted else '该账号正在处理，请稍候。'}

    def logged_in(self, login_id):
        self._account(login_id)
        with self.lock:
            updated = deepcopy(self.data)
            attempt = updated['attempts'].get(login_id, {}).get(self.now().date().isoformat(), {})
            if attempt.get('state') == 'login_required':
                attempt.update(state='ready', failures=0, message='网页登录成功，正在核对签到状态。')
            self._commit(updated)
            if login_id in self.running:
                self.pending_login.add(login_id)
                return
            automatic = self.data['settings']['enabled'] and self.now().strftime('%H:%M') >= self.data['settings']['time']
        self._enqueue(login_id, 'automatic' if automatic else 'refresh')

    def _enqueue(self, login_id, kind):
        now, day = self.now(), self.now().date().isoformat()
        with self.lock:
            if self.quit.is_set() or self.load_error or login_id in self.running:
                return False
            updated = deepcopy(self.data)
            old = updated['attempts'].setdefault(login_id, {}).get(day, {})
            attempt = {**old, 'state': 'running', 'started_at': now.isoformat(), 'message': '正在核对官方状态。'}
            updated['attempts'][login_id][day] = attempt
            cutoff = (now-timedelta(days=370)).date().isoformat()
            for owner, days in updated['attempts'].items():
                updated['attempts'][owner] = {date: item for date, item in days.items() if date >= cutoff}
            self._commit(updated)
            self.running.add(login_id)
        self.executor.submit(self._perform, login_id, kind, day)
        return True

    def _perform(self, login_id, kind, day):
        submitted, result, state, message, code = False, {}, 'ready', '已核对官方网页签到状态。', ''
        with self.lock:
            previous = deepcopy(self.data['attempts'][login_id][day])
        uncertain = bool(previous.get('uncertain') or previous.get('submitted'))

        def before_submit():
            nonlocal submitted
            self._account(login_id)
            with self.lock:
                if self.quit.is_set():
                    raise WebCheckinError('任务已停止，本次没有提交。', 'STOPPED', 409)
                updated = deepcopy(self.data)
                updated['attempts'][login_id][day].update(submitted=True, uncertain=True)
                self._commit(updated)
                submitted = True

        try:
            client = self.client_factory(lambda: self.credentials(login_id))
            result = (client.signin(before_submit=before_submit)
                      if kind in ('signin', 'automatic') and not uncertain else client.status())
            signed = result.get('today_signed') and result.get('sign_date') == day
            if signed:
                state, message, uncertain = 'signed', result.get('message') or '今日已签到。', False
            elif result.get('cycle_complete'):
                state, message, uncertain = 'completed', '本周期签到已完成。', False
            elif uncertain:
                state, message = 'unknown_result', '上次签到结果待核实，官方尚未确认今日签到；不会自动重复提交。'
            elif kind in ('signin', 'automatic'):
                state, message, uncertain = 'unknown_result', result.get('message') or '签到已提交，等待官方确认。', True
            else:
                message = result.get('message') or '今日尚未签到。'
        except (WebCheckinError, LoginStorageError) as error:
            code = str(getattr(error, 'code', 'STORAGE_ERROR'))
            uncertain = bool(getattr(error, 'uncertain', False) or uncertain)
            if uncertain:
                state = 'unknown_result'
            elif getattr(error, 'status', None) == 401 or code in ('LOGIN_REQUIRED', 'ACTIVITY_LOGIN_REQUIRED'):
                state = 'login_required'
            elif code in ('INELIGIBLE', 'ACTIVITY_UNAVAILABLE', 'NOT_ELIGIBLE', 'USER_LIMITED'):
                state = 'ineligible'
            elif code == '-800' or re.search(r'FREQUEN|LIMIT|TOO_MANY|RISK|CAPTCHA|VERIFY|429', code):
                state = 'rate_limited'
            else:
                state = 'failed'
            message = str(error)
        with self.lock:
            updated = deepcopy(self.data)
            observation = updated['observations'].setdefault(login_id, {})
            observation.update({key: deepcopy(result[key]) for key in OBSERVATION_FIELDS if key in result})
            observation.update(message=_message(message), last_at=self.now().isoformat())
            attempt = updated['attempts'][login_id][day]
            failures = attempt.get('failures', 0)+(state in ('failed', 'unknown_result', 'ineligible', 'rate_limited'))
            attempt.update(state=state, message=_message(message), finished_at=self.now().isoformat(),
                           failures=failures, submitted=submitted and uncertain, uncertain=uncertain, code=code)
            records = updated['records'].setdefault(login_id, [])
            records.append({'kind': kind, 'state': state, 'message': _message(message), 'at': self.now().isoformat()})
            updated['records'][login_id] = records[-120:]
            self._commit(updated)
            self.running.discard(login_id)
            pending = login_id in self.pending_login
            self.pending_login.discard(login_id)
        if pending and not self.quit.is_set():
            self.logged_in(login_id)

    def tick(self):
        now, day, accounts = self.now(), self.now().date().isoformat(), self.accounts()
        with self.lock:
            settings = dict(self.data['settings'])
            previous = self.readiness
            self.readiness = {account['id']: (account['status'], account.get('credential_revision', 0)) for account in accounts}
            if self.load_error or not settings['enabled'] or now.strftime('%H:%M') < settings['time']:
                return
        for account in accounts:
            login_id = account['id']
            if account['status'] != 'active':
                continue
            with self.lock:
                observation = self.data['observations'].get(login_id, {})
                attempt = self.data['attempts'].get(login_id, {}).get(day, {})
                recovered = (attempt.get('code') == 'APP_LOGIN_REQUIRED'
                             and previous.get(login_id) != self.readiness[login_id])
                if (login_id in self.running or observation.get('today_signed') and observation.get('sign_date') == day
                        or attempt.get('state') in ('completed', 'ineligible', 'rate_limited')
                        or attempt.get('state') == 'login_required' and not recovered
                        or attempt.get('failures', 0) >= MAX_FAILURES):
                    continue
                last = attempt.get('finished_at') or attempt.get('started_at')
                if last and not recovered and (now-datetime.fromisoformat(last)).total_seconds() < RETRY_MINUTES*60:
                    continue
            self._enqueue(login_id, 'automatic')

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.running

    def forget(self, login_id):
        with self.lock:
            if login_id in self.running:
                raise WebCheckinError('网页渠道签到正在处理，请稍候再退出账号。', 'ACCOUNT_BUSY', 409)
            updated = deepcopy(self.data)
            for key in ('observations', 'records', 'attempts'):
                updated[key].pop(login_id, None)
            self._commit(updated)
            self.pending_login.discard(login_id)
            self.readiness.pop(login_id, None)

    def start(self):
        if self.thread is not None:
            return

        def loop():
            while not self.quit.is_set():
                try:
                    self.tick()
                except (WebCheckinError, LoginStorageError) as error:
                    self.warning = str(error)
                self.quit.wait(1)

        self.thread = Thread(target=loop, daemon=True, name='web-checkin-daily')
        self.thread.start()

    def shutdown(self, wait=False):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=wait)
