"""Finite campaign automation over the shared login pool, with durable intents."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
from threading import Event, RLock, Thread
import time

from checkin_flash import run_flash_burst
from login_storage import LoginStorageError
from recharge.cmcc import SHANGHAI
from .client import SevenDayClient, SevenDayError, DEFAULT_CAMPAIGN, DRAW_PRIZES, campaign_config

DEFAULT_SETTINGS = {'signin_enabled': True, 'signin_time': '00:01', 'draw_enabled': True,
                    'burst_count': 5, 'burst_interval': .2,
                    'retry_interval_minutes': 5, 'max_attempts': 5}


class SevenDayScheduler:
    def __init__(self, credentials, accounts, *, storage=None, client_factory=SevenDayClient,
                 now=None, executor=None, config_loader=campaign_config):
        self.credentials, self.accounts, self.storage = credentials, accounts, storage
        self.client_factory, self.config_loader = client_factory, config_loader
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.quit = RLock(), Event()
        self.executor = executor or ThreadPoolExecutor(max_workers=16, thread_name_prefix='seven-day')
        self.thread = None
        self.running, self.pending, self.due, self.revisions = set(), {}, {}, {}
        self.prepared, self.config_due = {}, 0
        self.warning = self.load_error = ''
        self.data = {'settings': dict(DEFAULT_SETTINGS), 'observations': {}, 'records': {},
                     'campaign': dict(DEFAULT_CAMPAIGN)}
        if storage is not None:
            try:
                saved = storage.load_seven_day_data()
                if saved is not None:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(k), dict) for k in self.data):
                        raise LoginStorageError('七日活动数据格式无效，已保留原数据库。')
                    self.data = saved
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)
        for key, value in DEFAULT_SETTINGS.items():
            self.data['settings'].setdefault(key, value)
        self.data.setdefault('attempts', {})
        for login_id, days in self.data['records'].items():
            for day, records in days.items():
                check = records.get('check', {})
                if (check.get('code') == 'INELIGIBLE'
                        and check.get('message') == '官方未返回可参与的网龄资格，请在APP确认'):
                    check.update(code='QUALIFICATION_CHECK_PENDING', message='此前本地资格校验误拦截，等待重新核对官方进度')
                    attempt = self.data['attempts'].get(login_id, {}).get(day, {})
                    if attempt.get('state') == 'ineligible':
                        attempt['state'] = 'failed'

    def _commit(self, data):
        if self.load_error:
            raise SevenDayError(self.load_error, 'STORAGE_ERROR', 503)
        if self.storage is not None:
            self.storage.save_seven_day_data(data)
        self.data = data

    def metadata(self):
        with self.lock:
            value = deepcopy(self.data['campaign'])
        now = self.now()
        end = datetime.fromisoformat(value['end_at'])
        start = datetime.fromisoformat(value['start_at'])
        ended = now >= end or bool(value.get('sold_out'))
        return {**value, 'active': start <= now < end and not ended, 'ended': ended,
                'server_time': now.isoformat()}

    def _available(self):
        campaign = self.metadata()
        if not campaign['active']:
            raise SevenDayError('活动已结束' if campaign['ended'] else '活动尚未开始', 'ACTIVITY_CLOSED', 409)

    def update_settings(self, payload):
        settings = {key: payload.get(key, self.data['settings'][key]) for key in DEFAULT_SETTINGS}
        if (not isinstance(settings['signin_enabled'], bool) or not isinstance(settings['draw_enabled'], bool)
                or not isinstance(settings['signin_time'], str)
                or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', settings['signin_time'])
                or isinstance(settings['burst_count'], bool) or not isinstance(settings['burst_count'], int)
                or not 1 <= settings['burst_count'] <= 40
                or isinstance(settings['burst_interval'], bool) or not isinstance(settings['burst_interval'], (int, float))
                or not math.isfinite(settings['burst_interval']) or not .05 <= settings['burst_interval'] <= 5
                or isinstance(settings['retry_interval_minutes'], bool) or not isinstance(settings['retry_interval_minutes'], int)
                or not 1 <= settings['retry_interval_minutes'] <= 1440
                or isinstance(settings['max_attempts'], bool) or not isinstance(settings['max_attempts'], int)
                or not 1 <= settings['max_attempts'] <= 100):
            raise SevenDayError('请填写有效时间；夺宝1–40次、间隔0.05–5秒；失败间隔1–1440分钟、每日最多1–100次（含首次）', 'INVALID_SETTINGS', 400)
        with self.lock:
            updated = deepcopy(self.data)
            updated['settings'] = settings
            self._commit(updated)
            self.due.clear()
        return {'message': '自动设置已保存，所有有效账号统一使用', 'settings': settings}

    def _account(self, login_id):
        account = next((a for a in self.accounts() if a['id'] == login_id), None)
        if account is None or account['status'] != 'active':
            raise SevenDayError('请先恢复该共享账号的登录', 'APP_LOGIN_REQUIRED', 401)
        return account

    def _expire_pending(self):
        for key, pending in list(self.pending.items()):
            if pending['expires'] <= time.monotonic() and key not in self.running:
                self.pending.pop(key)['client'].close()

    def busy_login(self, login_id):
        with self.lock:
            self._expire_pending()
            return login_id in self.running or login_id in self.pending

    def forget(self, login_id):
        with self.lock:
            updated = deepcopy(self.data)
            updated['observations'].pop(login_id, None)
            updated['records'].pop(login_id, None)
            updated['attempts'].pop(login_id, None)
            self._commit(updated)
            self.due.pop(login_id, None)
            self.revisions.pop(login_id, None)
            self.prepared.pop(login_id, None)
            pending = self.pending.pop(login_id, None)
        if pending:
            pending['client'].close()

    def _record(self, login_id, kind, **fields):
        with self.lock:
            updated = deepcopy(self.data)
            date = self.now().date().isoformat()
            record = updated['records'].setdefault(login_id, {}).setdefault(date, {}).setdefault(kind, {})
            if fields.get('confirmed'):
                record.pop('code', None)
                record.pop('uncertain', None)
            record.update(fields, at=self.now().isoformat())
            self._commit(updated)

    def _submission(self, login_id, kind, message):
        self._available()
        if self.quit.is_set():
            raise SevenDayError('自动任务已停止，本次没有提交', 'STOPPED', 409)
        self._record(login_id, kind, submitted=True, message=message)

    def _observe(self, login_id, observation):
        with self.lock:
            updated = deepcopy(self.data)
            old = updated['observations'].get(login_id, {})
            # A no-prize draw may not appear in the official prize-order list.
            if old.get('date') == observation['date'] and not observation.get('draw_prize'):
                observation['draw_prize'] = old.get('draw_prize', '')
            updated['observations'][login_id] = {**observation, 'updated_at': self.now().isoformat()}
            attempt = updated['attempts'].get(login_id, {}).get(observation['date'], {})
            if attempt.get('state') == 'ineligible' and observation.get('eligible'):
                attempt.update(state='success', finished_at=self.now().isoformat())
            check = updated['records'].get(login_id, {}).get(observation['date'], {}).get('check')
            if check and check.get('code'):
                check.pop('code', None)
                check.pop('uncertain', None)
                check.update(confirmed=True, message='已查询官方最新进度', at=self.now().isoformat())
            if observation['checked_today'] and observation['date'] == self.now().date().isoformat():
                sign = updated['records'].get(login_id, {}).get(observation['date'], {}).get('signin')
                if sign:
                    sign.pop('code', None)
                    sign.pop('uncertain', None)
                    if not sign.get('confirmed'):
                        sign.update(confirmed=True, message='官方已确认今日打卡', at=self.now().isoformat())
            self._commit(updated)

    def _retry_due(self, login_id, now):
        """One durable gate for automatic scheduling, direct enqueue and status."""
        day = now.date().isoformat()
        attempt = self.data['attempts'].get(login_id, {}).get(day, {})
        check = self.data['records'].get(login_id, {}).get(day, {}).get('check', {})
        if (check.get('code') == 'INELIGIBLE' or attempt.get('state') == 'ineligible'
                or attempt.get('failed_attempts', 0) >= self.data['settings']['max_attempts']):
            return (now+timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if attempt.get('state') in ('running', 'failed'):
            return datetime.fromisoformat(attempt.get('finished_at') or attempt['started_at']) + timedelta(
                minutes=self.data['settings']['retry_interval_minutes'])
        return None

    def status(self):
        campaign = self.metadata()
        with self.lock:
            self._expire_pending()
            settings = deepcopy(self.data['settings'])
            rows = []
            for account in self.accounts():
                login_id = account['id']
                observation = deepcopy(self.data['observations'].get(login_id, {}))
                if observation and observation.get('date') != self.now().date().isoformat():
                    observation.update(checked_today=False, chances=None, draw_prize='')
                records = self.data['records'].get(login_id, {})
                entries = [r for values in records.values() for r in values.values()]
                last = max(entries, key=lambda r: r.get('at', ''), default={})
                now = self.now()
                midnight = (now+timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                check = records.get(now.date().isoformat(), {}).get('check', {})
                attempt = self.data['attempts'].get(login_id, {}).get(now.date().isoformat(), {})
                ineligible = check.get('code') == 'INELIGIBLE' or attempt.get('state') == 'ineligible'
                if ineligible:
                    observation['eligible'] = False
                failed_attempts = attempt.get('failed_attempts', int(ineligible))
                exhausted = failed_attempts >= settings['max_attempts']
                due = self.due.get(login_id, now)
                retry_due = self._retry_due(login_id, now)
                if retry_due:
                    due = max(due, retry_due)
                candidates = []
                today = observation.get('date') == now.date().isoformat()
                if settings['signin_enabled'] and not observation.get('completed') and not observation.get('claimed'):
                    hour, minute = map(int, settings['signin_time'].split(':'))
                    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    candidates.append(at+timedelta(days=1) if today and observation.get('checked_today') else max(now, at))
                if settings['draw_enabled']:
                    candidates.append(midnight if today and observation.get('chances') == 0 else now)
                rows.append({**account, 'running': login_id in self.running,
                             'observation': observation, 'last_run': last.get('at'),
                             'failed_attempts': failed_attempts, 'max_attempts': settings['max_attempts'],
                             'retry_exhausted': exhausted,
                             'retry_message': '资格未通过，今日已暂停自动尝试' if ineligible else
                                f"今日失败已达 {settings['max_attempts']} 次，明日再检查" if exhausted else
                                f"今日失败 {failed_attempts}/{settings['max_attempts']} 次，间隔 {settings['retry_interval_minutes']} 分钟再试"
                                if failed_attempts and attempt.get('state') == 'failed' else '',
                             'message': last.get('message', observation.get('message', '等待自动检查')),
                             'next_run': max(min(candidates), due).isoformat()
                                if campaign['active'] and account['status'] == 'active' and candidates and login_id not in self.pending else None})
            return {'campaign': campaign, 'settings': settings, 'accounts': rows, 'warning': self.warning}

    def history(self, login_id):
        with self.lock:
            return deepcopy(self.data['records'].get(login_id, {}))

    def run(self, login_id, kind='refresh', *, target=None):
        if kind not in ('signin', 'draw', 'refresh', 'automatic', 'prewarm'):
            raise SevenDayError('请选择打卡、夺宝或刷新', 'INVALID_KIND', 400)
        self._available()
        self._account(login_id)
        with self.lock:
            self._expire_pending()
            if login_id in self.running or login_id in self.pending:
                raise SevenDayError('该账号正在执行或等待短信验证', 'ACCOUNT_BUSY', 409)
            attempt_day = None
            if kind in ('automatic', 'prewarm'):
                now = self.now()
                planned = target or now
                retry_due = self._retry_due(login_id, planned)
                if retry_due and now < retry_due:
                    return {'message': '当前处于自动重试等待或今日停止期，不会重复安排'}
                attempt_day = planned.date().isoformat()
                updated = deepcopy(self.data)
                attempt = updated['attempts'].setdefault(login_id, {}).setdefault(attempt_day, {})
                attempt.update(failed_attempts=attempt.get('failed_attempts', 0)+1,
                               state='running', started_at=now.isoformat())
                attempt.pop('finished_at', None)
                self._commit(updated)
            self.running.add(login_id)
        try:
            self.executor.submit(self._work, login_id, kind, target, attempt_day)
        except RuntimeError:
            with self.lock:
                self.running.discard(login_id)
            raise
        return {'message': '已加入执行队列'}

    def _work(self, login_id, kind, target=None, attempt_day=None):
        client = self.client_factory(lambda: self.credentials(login_id))
        failed, failure_code = False, ''
        try:
            if self.quit.is_set():
                return
            self._available()
            client.connect()
            observation = client.inspect(self.now())
            self._observe(login_id, observation)
            if client.stock() == 0:
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['campaign']['sold_out'] = True
                    self._commit(updated)
                return
            if target:
                deadline = time.monotonic() + max(0, (target-self.now()).total_seconds())
                while time.monotonic() < deadline and not self.quit.is_set():
                    self.quit.wait(min(.1, deadline-time.monotonic()))
                if self.quit.is_set():
                    return
                observation = client.inspect(self.now())
                self._observe(login_id, observation)
            settings = dict(self.data['settings'])
            clock = self.now().strftime('%H:%M')
            do_signin = kind == 'signin' or kind == 'automatic' and settings['signin_enabled'] and clock >= settings['signin_time']
            do_draw = kind in ('draw', 'prewarm') or kind == 'automatic' and settings['draw_enabled']
            if do_signin and not observation['checked_today'] and not observation['completed'] and not observation['claimed']:
                try:
                    client.signin(lambda: self._submission(login_id, 'signin', '打卡已提交，正在核实'))
                    observation = client.inspect(self.now())
                    self._observe(login_id, observation)
                    self._record(login_id, 'signin', confirmed=observation['checked_today'],
                                 message='今日已打卡' if observation['checked_today'] else '打卡结果待核实')
                except SevenDayError as error:
                    failure_code = error.code
                    self._record(login_id, 'signin', message=str(error), code=error.code, uncertain=error.uncertain)
            if do_draw and observation['chances'] > 0:
                scheduler = self
                class PreparedDraw:
                    ready = True
                    result = {'draw_state': 'ready', 'draw_message': '等待每日夺宝'}
                    start_monotonic = time.monotonic()
                    end_monotonic = start_monotonic + 30
                    def local_time(self):
                        return scheduler.now().isoformat()
                    def send(self, before_submit):
                        scheduler._available()
                        try:
                            result = client.draw(before_submit)
                        except SevenDayError as error:
                            retry = not error.uncertain and bool(re.search('未开始|尚未开始|稍后|繁忙|火爆', str(error)))
                            return {'state': 'uncertain' if error.uncertain else 'retryable' if retry else 'failed',
                                    'message': str(error), 'code': error.code}
                        prize = DRAW_PRIZES.get(str(result.get('skuCode')), str(result.get('skuName') or '已完成夺宝，请查看我的奖品'))
                        self.result = {'draw_state': 'already_claimed', 'draw_message': prize, 'draw_prize': prize}
                        return {'state': 'success', 'message': prize}
                    def reconcile(self):
                        current = client.inspect(scheduler.now())
                        scheduler._observe(login_id, current)
                        self.result = {'draw_state': 'already_claimed' if current['chances'] == 0 else 'ready',
                                       'draw_message': current['draw_prize'] or '今日机会已用完'}
                        return self.result
                prepared = PreparedDraw()
                result = run_flash_burst(prepared, {**settings, 'burst_workers': 1},
                    lambda: self._submission(login_id, 'draw', '夺宝已提交，正在接收结果'),
                    self.quit.is_set, followup_seconds=None, result_key='draw')
                self._record(login_id, 'draw', **result, confirmed=result['state'] == 'success')
                if any(item.get('code') == 'INELIGIBLE' for item in result['attempts']):
                    failure_code = 'INELIGIBLE'
                observation = client.inspect(self.now())
                observation['draw_prize'] = prepared.result.get('draw_prize') or observation['draw_prize']
                self._observe(login_id, observation)
            failed = (do_signin and not (observation['checked_today'] or observation['completed'] or observation['claimed'])
                      or do_draw and observation['chances'] > 0)
            if kind == 'refresh':
                self._record(login_id, 'refresh', message='已更新官方打卡和夺宝进度')
            if observation['completed'] and not observation['claimed']:
                self._record(login_id, 'claim', message='连续7天已完成，请填写短信验证码领取5元红包')
        except SevenDayError as error:
            failed, failure_code = True, error.code
            self._record(login_id, 'check', message=str(error), code=getattr(error, 'code', 'STORAGE_ERROR'),
                         uncertain=getattr(error, 'uncertain', False))
        except LoginStorageError as error:
            failed = True
            self.warning = str(error)
        finally:
            client.close()
            with self.lock:
                if attempt_day is not None:
                    updated = deepcopy(self.data)
                    attempt = updated['attempts'][login_id][attempt_day]
                    attempt.update(state='ineligible' if failed and failure_code == 'INELIGIBLE' else 'failed' if failed else 'success',
                                   finished_at=self.now().isoformat())
                    if not failed:
                        attempt['failed_attempts'] -= 1
                    self._commit(updated)
                self.running.discard(login_id)
                self.due[login_id] = self.now()+timedelta(minutes=self.data['settings']['retry_interval_minutes'])

    def send_sms(self, login_id):
        self._available()
        self._account(login_id)
        with self.lock:
            self._expire_pending()
            if login_id in self.running:
                raise SevenDayError('该账号正在执行，请稍候', 'ACCOUNT_BUSY', 409)
            self.running.add(login_id)
            pending = self.pending.pop(login_id, None)
        client = pending['client'] if pending else self.client_factory(lambda: self.credentials(login_id))
        try:
            if not pending:
                client.connect()
            observation = client.inspect(self.now())
            self._observe(login_id, observation)
            if not observation['completed'] or observation['claimed']:
                raise SevenDayError('请完成连续7天打卡；已领过的红包无需重复领取', 'REWARD_NOT_READY', 409)
            client.send_reward_sms()
            with self.lock:
                self.pending[login_id] = {'client': client, 'expires': time.monotonic()+300}
            return {'message': '领奖验证码已发送，请填写本次收到的验证码'}
        finally:
            with self.lock:
                self.running.discard(login_id)
                retained = login_id in self.pending
            if not retained:
                client.close()

    def claim(self, login_id, code):
        if not isinstance(code, str) or not re.fullmatch(r'\d{6}', code):
            raise SevenDayError('请输入6位短信验证码', 'INVALID_CODE', 400)
        self._available()
        with self.lock:
            self._expire_pending()
            if login_id in self.running:
                raise SevenDayError('该账号正在执行，请稍候', 'ACCOUNT_BUSY', 409)
            pending = self.pending.get(login_id)
            if pending is None:
                raise SevenDayError('请重新获取本次领奖验证码', 'SMS_EXPIRED', 409)
            self.running.add(login_id)
        client = pending['client']
        try:
            client.claim(code, lambda: self._submission(login_id, 'claim', '领奖已提交，正在接收结果'))
            self._record(login_id, 'claim', confirmed=True, message='已受理，5元话费券将在24小时内到账')
            with self.lock:
                updated = deepcopy(self.data)
                updated['observations'][login_id]['claimed'] = True
                self._commit(updated)
                self.pending.pop(login_id, None)
            return {'message': '领取成功，5元话费券将在24小时内到账'}
        except SevenDayError as error:
            if error.uncertain:
                self._record(login_id, 'claim', uncertain=True, message='领奖结果待核实，请刷新查询后再操作')
                with self.lock:
                    self.pending.pop(login_id, None)
            raise
        finally:
            with self.lock:
                self.running.discard(login_id)
                retained = login_id in self.pending
            if not retained:
                client.close()

    def tick(self):
        now = self.now()
        if self.metadata()['ended']:
            return
        if time.monotonic() >= self.config_due:
            self.config_due = time.monotonic()+300
            try:
                config = self.config_loader()
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['campaign'].update(config)
                    self._commit(updated)
                    self.warning = ''
            except (SevenDayError, LoginStorageError) as error:
                self.warning = str(error)
        if not self.metadata()['active']:
            return
        accounts = self.accounts()
        known = {a['id'] for a in accounts}
        for login_id in set(self.data['observations'])-known:
            self.forget(login_id)
        settings = self.data['settings']
        for account in accounts:
            login_id = account['id']
            if account['status'] != 'active' or self.busy_login(login_id):
                continue
            retry_due = self._retry_due(login_id, now)
            if retry_due and now < retry_due:
                continue
            revision = account.get('credential_revision')
            changed = self.revisions.get(login_id) != revision
            midnight = (now+timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            if settings['draw_enabled'] and 0 < (midnight-now).total_seconds() <= 30 and self.prepared.get(login_id) != midnight.date():
                self.prepared[login_id] = midnight.date()
                self.run(login_id, 'prewarm', target=midnight)
                continue
            if not changed and now < self.due.get(login_id, now):
                continue
            self.revisions[login_id] = revision
            observation = self.data['observations'].get(login_id, {})
            today = observation.get('date') == now.date().isoformat()
            signin_due = settings['signin_enabled'] and now.strftime('%H:%M') >= settings['signin_time'] and not observation.get('claimed') and not observation.get('completed') and not (today and observation.get('checked_today'))
            draw_due = settings['draw_enabled'] and not (today and observation.get('chances') == 0)
            if changed or signin_due or draw_due:
                self.run(login_id, 'automatic')

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = Thread(target=self._loop, daemon=True, name='seven-day-clock')
        self.thread.start()

    def _loop(self):
        while not self.quit.is_set():
            if self.metadata()['ended']:
                self.quit.set()
                break
            try:
                self.tick()
            except (SevenDayError, LoginStorageError) as error:
                self.warning = str(error)
            self.quit.wait(.5)

    def shutdown(self, wait=False):
        self.quit.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=wait, cancel_futures=True)
        with self.lock:
            pending, self.pending = self.pending, {}
        for value in pending.values():
            value['client'].close()
