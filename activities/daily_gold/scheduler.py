"""Daily account automation with persisted budgets and non-replayed draw intents."""
from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
from threading import Event, RLock, Thread
import time

from feature_runtime import ThreadPoolExecutor, feature_running
from http_transport import RequestException
from login_storage import LoginStorageError
from recharge.cmcc import SHANGHAI
from .client import DailyGoldClient, DailyGoldError, DEFAULT_CAMPAIGN, campaign_config

SCOPE = 'daily-gold'
DEFAULT_SETTINGS = {'enabled': True, 'start_time': '00:00:01', 'repeat_count': 5, 'interval_seconds': 1}
TERMINAL = {'ineligible', 'rate_limited', 'verification_required', 'login_required', 'uncertain', 'completed', 'signed'}


def settings_value(payload):
    if (not isinstance(payload, dict) or type(payload.get('enabled')) is not bool
            or not isinstance(payload.get('start_time'), str)
            or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d', payload['start_time'])
            or type(payload.get('repeat_count')) is not int or not 1 <= payload['repeat_count'] <= 100
            or type(payload.get('interval_seconds')) not in (int, float)
            or not math.isfinite(payload['interval_seconds']) or not .1 <= payload['interval_seconds'] <= 60):
        raise DailyGoldError('请填写精确到秒的时间、1–100次尝试及0.1–60秒间隔。', 'INVALID_SETTINGS', 400)
    return {key: payload[key] for key in DEFAULT_SETTINGS}


def message(value):
    return re.sub(r'1[3-9]\d{9}', lambda match: match[0][:3]+'****'+match[0][-4:], str(value))[:500]


class DailyGoldScheduler:
    def __init__(self, credentials, accounts, *, storage=None, client_factory=DailyGoldClient,
                 config_loader=campaign_config, now=None, executor=None, wait=None):
        self.credentials, self.accounts, self.storage = credentials, accounts, storage
        self.client_factory, self.config_loader = client_factory, config_loader
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.executor = executor or ThreadPoolExecutor(max_workers=16, thread_name_prefix=SCOPE)
        self.wait = wait or self.quit.wait
        self.running, self.restored, self.readiness = set(), set(), {}
        self.warning = self.load_error = ''
        self.data = {'settings': dict(DEFAULT_SETTINGS), 'campaign': dict(DEFAULT_CAMPAIGN),
                     'observations': {}, 'attempts': {}, 'records': {}, 'config_check_after': None}
        if storage is not None:
            try:
                saved = storage.load_activity_data(SCOPE)
                if saved is not None:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(key), dict)
                            for key in ('settings', 'campaign', 'observations', 'attempts', 'records')):
                        raise LoginStorageError('天天转金喜数据格式无效，已保留原数据库。')
                    settings_value(saved['settings'])
                    self.data = saved
                known = {account['id'] for account in self.accounts()}
                updated = deepcopy(self.data)
                for key in ('observations', 'attempts', 'records'):
                    updated[key] = {owner: value for owner, value in updated[key].items() if owner in known}
                for days in updated['attempts'].values():
                    for attempt in days.values():
                        if attempt.get('state') == 'running':
                            attempt.update(state='uncertain' if attempt.get('submitted') else 'failed',
                                           uncertain=bool(attempt.get('submitted')),
                                           message='上次执行中断，将先核对官方记录，不自动重复提交。')
                self._commit(updated)
            except (LoginStorageError, DailyGoldError) as error:
                self.warning = self.load_error = str(error)

    def _commit(self, updated):
        if self.load_error:
            raise DailyGoldError(self.load_error, 'STORAGE_ERROR', 503)
        if self.storage is not None:
            try:
                self.storage.save_activity_data(SCOPE, updated)
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)
                raise
        self.data = updated

    def metadata(self):
        with self.lock:
            campaign = deepcopy(self.data['campaign'])
        now = self.now()
        start, end = datetime.fromisoformat(campaign['start_at']), datetime.fromisoformat(campaign['end_at'])
        ended = now >= end
        return {**campaign, 'active': start <= now < end, 'ended': ended, 'server_time': now.isoformat()}

    def _available(self):
        if self.quit.is_set() or not feature_running():
            raise DailyGoldError('功能已停止，本次不再执行。', 'STOPPED', 409)
        if self.load_error:
            raise DailyGoldError(self.load_error, 'STORAGE_ERROR', 503)
        campaign = self.metadata()
        if not campaign['active']:
            raise DailyGoldError('活动已结束' if campaign['ended'] else '活动尚未开始', 'ACTIVITY_CLOSED', 409)

    def _account(self, login_id):
        account = next((account for account in self.accounts() if account['id'] == login_id), None)
        if account is None or account['status'] != 'active':
            raise DailyGoldError('请先恢复该共享账号登录。', 'APP_LOGIN_REQUIRED', 401)
        return account

    def update_settings(self, payload):
        settings = settings_value(payload)
        self._available()
        with self.lock:
            updated = deepcopy(self.data)
            updated['settings'] = settings
            self._commit(updated)
        return {'message': '设置已保存，应用于所有有效账号。', 'settings': settings}

    def _done(self, login_id, day):
        observed = self.data['observations'].get(login_id, {})
        return observed.get('date') == day and (observed.get('signed') is True or observed.get('remaining') == 0)

    def _next(self, account, now):
        settings = self.data['settings']
        if not settings['enabled'] or account['status'] != 'active' or not self.metadata()['active']:
            return None
        hour, minute, second = map(int, settings['start_time'].split(':'))
        start = now.replace(hour=hour, minute=minute, second=second, microsecond=0)
        day = now.date().isoformat()
        attempt = self.data['attempts'].get(account['id'], {}).get(day, {})
        if self._done(account['id'], day) or attempt.get('blocked') or attempt.get('state') in TERMINAL or attempt.get('count', 0) >= settings['repeat_count']:
            return start+timedelta(days=1)
        if attempt.get('finished_at'):
            start = max(start, datetime.fromisoformat(attempt['finished_at'])+timedelta(seconds=settings['interval_seconds']))
        return max(start, now)

    def status(self):
        now, accounts = self.now(), self.accounts()
        with self.lock:
            rows = []
            for account in accounts:
                login_id = account['id']
                records = deepcopy(self.data['records'].get(login_id, []))
                last = records[-1] if records else {}
                next_run = self._next(account, now)
                rows.append({key: account.get(key, '') for key in ('id', 'name', 'phone_masked', 'status')})
                rows[-1].update(observation=deepcopy(self.data['observations'].get(login_id, {})),
                    running=login_id in self.running, next_run=next_run.isoformat() if next_run else None,
                    last_result=last, records=records,
                    activity_login_required=last.get('state') == 'login_required')
            return {'accounts': rows, 'settings': deepcopy(self.data['settings']), 'campaign': self.metadata(),
                    'server_time': now.isoformat(), 'warning': self.warning}

    def _observe(self, login_id, value):
        observed = {key: deepcopy(value[key]) for key in ('date', 'signed', 'remaining', 'eligible') if key in value}
        if 'prizes' in value:
            observed['prizes'] = [{key: message(prize[key]) for key in ('name', 'status', 'received_at') if key in prize}
                                  for prize in value['prizes']]
        with self.lock:
            updated = deepcopy(self.data)
            updated['observations'][login_id] = observed
            self._commit(updated)

    def _record(self, login_id, day, state, text, *, code='', uncertain=False, submitted=False, counted=False):
        with self.lock:
            updated = deepcopy(self.data)
            attempt = updated['attempts'].setdefault(login_id, {}).setdefault(day, {})
            if counted:
                attempt['count'] = attempt.get('count', 0)+1
            attempt.update(state=state, message=message(text), code=str(code), uncertain=uncertain,
                           submitted=submitted, finished_at=self.now().isoformat())
            if state in ('ineligible', 'rate_limited', 'verification_required'):
                attempt['blocked'] = state
            entry = {'at': self.now().isoformat(), 'state': state, 'message': message(text), 'code': str(code)}
            if state in ('signed', 'completed'):
                entry['prizes'] = deepcopy(updated['observations'].get(login_id, {}).get('prizes', []))
            entries = updated['records'].setdefault(login_id, [])
            entries.append(entry)
            updated['records'][login_id] = entries[-120:]
            self._commit(updated)

    def run(self, login_id, kind='refresh'):
        if not isinstance(login_id, str) or kind not in ('refresh', 'signin'):
            raise DailyGoldError('请选择账号及打卡操作。', 'INVALID_OPERATION', 400)
        self._available()
        self._account(login_id)
        accepted = self._enqueue(login_id, kind)
        return {'message': '已安排核对官方进度。' if accepted and kind == 'refresh' else
                '已安排打卡，先核对今日官方记录。' if accepted else '该账号正在处理，请稍候。'}

    def refresh(self):
        self._available()
        queued = sum(self._enqueue(account['id'], 'refresh') for account in self.accounts() if account['status'] == 'active')
        return {'message': f'已安排查询 {queued} 个有效账号。'}

    def login_restored(self, login_id):
        self._account(login_id)
        with self.lock:
            self.restored.add(login_id)

    def _enqueue(self, login_id, kind):
        with self.lock:
            if self.quit.is_set() or self.load_error or login_id in self.running:
                return False
            self.running.add(login_id)
        try:
            self.executor.submit(self._perform, login_id, kind, self.now().date().isoformat())
        except RuntimeError:
            with self.lock:
                self.running.discard(login_id)
            raise
        return True

    def _perform(self, login_id, kind, day):
        client = None
        try:
            self._available()
            self._account(login_id)
            with self.lock:
                old = deepcopy(self.data['attempts'].get(login_id, {}).get(day, {}))
                settings = dict(self.data['settings'])
                if kind != 'refresh' and self._done(login_id, day):
                    return
            client = self.client_factory(lambda: self.credentials(login_id))
            client.connect()
            observation = client.observe()
            self._observe(login_id, observation)
            if self._done(login_id, day):
                self._record(login_id, day, 'signed' if observation.get('signed') else 'completed',
                             '今日已打卡，奖品以官方记录为准。' if observation.get('signed') else '今日免费机会已用完，已同步官方奖品。')
                return
            if kind == 'refresh':
                self._record(login_id, day, old.get('state', 'ready') if old.get('state') in ('ineligible', 'rate_limited', 'uncertain') else 'ready',
                             '已更新官方进度。' if not old.get('uncertain') else '上次提交结果待核实，本次仅查询，不重复提交。',
                             code=old.get('code', ''), uncertain=bool(old.get('uncertain')), submitted=bool(old.get('submitted')))
                return
            if old.get('uncertain') or old.get('submitted'):
                self._record(login_id, day, 'uncertain', '上次提交结果待核实，本次仅查询，不重复提交。', uncertain=True, submitted=True)
                return
            if kind == 'automatic' and (old.get('blocked') or old.get('state') in ('ineligible', 'rate_limited', 'login_required')):
                return
            if observation.get('eligible') is False:
                self._record(login_id, day, 'ineligible', '官方确认不符合活动资格，今日暂停自动尝试。', code='INELIGIBLE')
                return
            count = old.get('count', 0)
            budget = max(0, settings['repeat_count']-count)
            if kind == 'restored' and not budget:
                budget = 1  # Explicit completed SMS login may reconcile/catch up once; never reset the automatic budget.
            for index in range(budget):
                if index:
                    deadline = time.monotonic()+settings['interval_seconds']
                    while time.monotonic() < deadline:
                        if self.wait(min(.1, deadline-time.monotonic())) or not feature_running():
                            return
                self._available()
                self._account(login_id)
                if kind in ('automatic', 'restored') and not self.data['settings']['enabled']:
                    return
                if self.now().date().isoformat() != day:
                    return
                self._record(login_id, day, 'running', '打卡已提交，正在接收官方结果。', submitted=True, uncertain=True, counted=True)
                try:
                    observation = client.signin()
                    self._observe(login_id, observation)
                    if self._done(login_id, day):
                        self._record(login_id, day, 'signed' if observation.get('signed') else 'completed',
                                     '今日打卡成功，已同步官方奖品。' if observation.get('signed') else '今日免费机会已用完，已同步官方奖品。')
                        return
                    self._record(login_id, day, 'uncertain', '官方尚未确认打卡结果，先查询，不自动重复提交。', uncertain=True, submitted=True)
                    observation = client.observe()
                    self._observe(login_id, observation)
                    if self._done(login_id, day):
                        self._record(login_id, day, 'signed' if observation.get('signed') else 'completed', '已核实今日打卡完成，已同步官方奖品。')
                    return
                except (DailyGoldError, RequestException) as error:
                    uncertain = isinstance(error, RequestException) or bool(getattr(error, 'uncertain', False))
                    code = str(getattr(error, 'code', 'NETWORK_ERROR'))
                    state = ('uncertain' if uncertain else 'login_required' if getattr(error, 'status', None) == 401 else
                             'ineligible' if code in ('INELIGIBLE', 'NOT_ELIGIBLE') else
                             'verification_required' if code == 'VERIFICATION_REQUIRED' else
                             'rate_limited' if code in ('RATE_LIMITED', '-800', '8053', '429') or re.search(r'请求频繁|风控|验证码|滑块|验证后', str(error)) else 'failed')
                    self._record(login_id, day, state, str(error), code=code, uncertain=uncertain, submitted=uncertain)
                    if uncertain:
                        observation = client.observe()
                        self._observe(login_id, observation)
                        if self._done(login_id, day):
                            self._record(login_id, day, 'signed' if observation.get('signed') else 'completed', '已核实今日打卡完成，已同步官方奖品。')
                    if state in TERMINAL:
                        return
        except (DailyGoldError, RequestException, LoginStorageError) as error:
            code = str(getattr(error, 'code', 'NETWORK_ERROR' if isinstance(error, RequestException) else 'STORAGE_ERROR'))
            if code in ('STOPPED', 'ACTIVITY_CLOSED'):
                return
            with self.lock:
                intent = deepcopy(self.data['attempts'].get(login_id, {}).get(day, {}))
            uncertain = bool(intent.get('uncertain'))
            state = ('uncertain' if uncertain else 'login_required' if getattr(error, 'status', None) == 401 else
                     'ineligible' if code in ('INELIGIBLE', 'NOT_ELIGIBLE') else
                     'verification_required' if code == 'VERIFICATION_REQUIRED' else
                     'rate_limited' if code in ('RATE_LIMITED', '-800', '8053', '429') or re.search(r'请求频繁|风控|验证码|滑块', str(error)) else 'failed')
            if isinstance(error, LoginStorageError):
                self.warning = self.load_error = str(error)
            else:
                self._record(login_id, day, state, str(error), code=code, uncertain=uncertain,
                             submitted=uncertain, counted=kind != 'refresh' and intent.get('state') != 'running' and not uncertain)
        finally:
            if client is not None:
                client.close()
            with self.lock:
                self.running.discard(login_id)

    def failure_count(self):
        day, accounts = self.now().date().isoformat(), self.accounts()
        with self.lock:
            return sum(not self._done(account['id'], day) and
                       self.data['attempts'].get(account['id'], {}).get(day, {}).get('state') in
                       ('failed', 'ineligible', 'rate_limited', 'verification_required', 'login_required', 'uncertain') for account in accounts)

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.running

    def forget(self, login_id):
        with self.lock:
            if login_id in self.running:
                raise DailyGoldError('该账号正在处理，请稍候再退出。', 'ACCOUNT_BUSY', 409)
            updated = deepcopy(self.data)
            for key in ('observations', 'attempts', 'records'):
                updated[key].pop(login_id, None)
            self._commit(updated)
            self.restored.discard(login_id)
            self.readiness.pop(login_id, None)

    def tick(self):
        now = self.now()
        if self.quit.is_set() or self.load_error or not self.metadata()['active'] or not feature_running():
            return
        with self.lock:
            due = self.data.get('config_check_after')
            update_config = not due or now >= datetime.fromisoformat(due)
        if update_config:
            try:
                campaign = self.config_loader()
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['campaign'].update(campaign)
                    updated['config_check_after'] = (now+timedelta(hours=1)).isoformat()
                    self._commit(updated)
                    self.warning = ''
            except (DailyGoldError, RequestException, LoginStorageError) as error:
                self.warning = str(error)
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['config_check_after'] = (now+timedelta(hours=1)).isoformat()
                    self._commit(updated)
        if not self.metadata()['active']:
            return
        accounts = self.accounts()
        known = {account['id'] for account in accounts}
        for owner in set(self.data['observations']) | set(self.data['attempts']) | set(self.data['records']):
            if owner not in known and not self.busy_login(owner):
                self.forget(owner)
        with self.lock:
            previous, self.readiness = self.readiness, {account['id']: account['status'] for account in accounts}
            settings = dict(self.data['settings'])
        if not settings['enabled']:
            return
        for account in accounts:
            login_id = account['id']
            if account['status'] != 'active' or self.busy_login(login_id):
                continue
            with self.lock:
                restored = login_id in self.restored or previous.get(login_id) not in (None, 'active')
                self.restored.discard(login_id)
                if self._done(login_id, now.date().isoformat()):
                    continue
                attempt = self.data['attempts'].get(login_id, {}).get(now.date().isoformat(), {})
                if restored and not attempt.get('blocked') and attempt.get('state') not in ('ineligible', 'rate_limited'):
                    kind = 'restored' if settings['enabled'] and now.strftime('%H:%M:%S') >= settings['start_time'] else 'refresh'
                else:
                    due = self._next(account, now)
                    if due is None or due > now:
                        continue
                    kind = 'automatic'
            self._enqueue(login_id, kind)

    def start(self):
        if self.thread is not None:
            return
        self.thread = Thread(target=self._loop, daemon=True, name='daily-gold-clock')
        self.thread.start()

    def _loop(self):
        while not self.quit.is_set():
            if self.metadata()['ended']:
                self.quit.set()
                break
            try:
                self.tick()
            except (DailyGoldError, LoginStorageError, RequestException) as error:
                self.warning = str(error)
            self.quit.wait(.1)

    def shutdown(self, wait=False):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=wait, cancel_futures=True)
