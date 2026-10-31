"""Daily sign-in and monthly benefit goals with daily 10:00 attempts."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import re
import secrets
import time
from threading import Event, RLock, Thread

from login_storage import LoginStorageError
from recharge.cmcc import SHANGHAI
from star_day_client import StarDayClient, StarDayError
from star_day_auth import StarDaySessionCache
from star_day_sms import StarDaySmsLogin


class StarDayScheduler:
    def __init__(self, credentials, accounts, *, storage=None, client_factory=None,
                 executor=None, now=None):
        self.credentials, self.accounts, self.storage = credentials, accounts, storage
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.sessions = StarDaySessionCache(storage, now=lambda:self.now().timestamp())
        self.sms = StarDaySmsLogin(self.sessions, now=lambda:self.now().timestamp())
        self.client_factory = client_factory or (lambda provider:StarDayClient(provider, session_cache=self.sessions, allow_login=False))
        self.executor = executor or ThreadPoolExecutor(max_workers=32, thread_name_prefix='star-day')
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.running, self.checked, self.readiness = set(), {}, {}
        self.catalog = []
        self.calibration, self.calibrating, self.starts = None, False, {}
        self.cancellations = {}
        self.data = {'settings': {'signin_enabled': True, 'signin_time': '00:01', 'session_prepare_time':'09:30'},
                     'accounts': {}, 'actions': {}, 'jobs': {}}
        self.warning = self.load_error = ''
        if storage is not None:
            try:
                saved = storage.load_star_day_data()
                if saved is not None:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(k), dict) for k in self.data):
                        raise LoginStorageError('星动日数据格式无效，已保留原数据库。')
                    self.data = saved
                known = {a['id'] for a in self.accounts()}
                changed = deepcopy(self.data)
                changed['settings'].setdefault('session_prepare_time', '09:30')
                changed['accounts'] = {k: v for k, v in changed['accounts'].items() if k in known}
                for record in changed['accounts'].values():
                    reason = self._daily_retry_reason(record)
                    if reason and not record.get('retry_at'):
                        record.update(retry_at=self._tomorrow(changed['settings']['session_prepare_time']), retry_reason=reason)
                    elif reason:
                        hour, minute = map(int, changed['settings']['session_prepare_time'].split(':'))
                        record['retry_at'] = datetime.fromisoformat(record['retry_at']).replace(hour=hour,minute=minute,second=0,microsecond=0).isoformat()
                changed['actions'] = {k: v for k, v in changed['actions'].items() if k.split(':', 1)[0] in known}
                changed['jobs'] = {k:v for k,v in changed['jobs'].items() if v['state'] != 'stopped'}
                for job in changed['jobs'].values():
                    job['results'] = {k: v for k, v in job['results'].items() if k in known}
                    if 'login_ids' in job:
                        job['login_ids'] = [i for i in job['login_ids'] if i in known]
                    for result in job['results'].values():
                        if result['state'] in ('preparing', 'running'):
                            result.update(state='unknown', message='上次执行中断，请先核对官方领取记录。')
                    if job['state'] in ('preparing', 'running'):
                        job.update(state='interrupted', enabled=False)
                    if not job.get('immediate'):
                        job.update(daily=True, enabled=True)
                        at = datetime.fromisoformat(job['at']).replace(hour=10, minute=0, second=0, microsecond=0)
                        job['at'] = at.isoformat()
                        job.setdefault('round_date', at.date().isoformat() if job['results'] else None)
                self._commit(changed)
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)

    def _commit(self, data):
        if self.load_error:
            raise StarDayError(self.load_error, 'STORAGE_ERROR', 503)
        if self.storage is not None:
            self.storage.save_star_day_data(data)
        self.data = data

    def _record(self, data, login_id):
        return data['accounts'].setdefault(login_id, {'observation': {}, 'history': [],
            'last_run_at': None, 'message': '尚未查询', 'attempt_revision': None})

    @staticmethod
    def _daily_retry_reason(record):
        if record.get('error_code') in ('-800','UPSTREAM_RATE_LIMITED'):
            return 'rate_limited'
        if record.get('query_state') == 'auth_failed':
            return 'auth_failed'
        observed = record.get('observation', {})
        if observed.get('eligible') is False or observed.get('today_state') == 'ineligible':
            return 'ineligible'
        return ''

    def _tomorrow(self, clock):
        hour, minute = map(int, clock.split(':'))
        return (self.now()+timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0).isoformat()

    def _deferred(self, login_id):
        at = self.data['accounts'].get(login_id, {}).get('retry_at')
        return bool(at and self.now() < datetime.fromisoformat(at))

    def _next(self, clock, *, retry=False):
        now = self.now()
        hour, minute = map(int, clock.split(':'))
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if retry:
            at = max(at, now+timedelta(minutes=5))
            return (at if at.date() == now.date() else now.replace(hour=hour, minute=minute,
                second=0, microsecond=0)+timedelta(days=1)).isoformat()
        return (at if at > now else at+timedelta(days=1)).isoformat()

    def _claimed(self, login_id, month):
        observation = self.data['accounts'].get(login_id, {}).get('observation', {})
        return (self.data['actions'].get(f'{login_id}:benefit:{month}', {}).get('confirmed', False)
                or observation.get('date', '')[:6] == month and observation.get('month_claimed') is True)

    def settings(self, payload):
        enabled, clock = payload.get('signin_enabled'), payload.get('signin_time')
        prepare = payload.get('session_prepare_time', self.data['settings']['session_prepare_time'])
        if not isinstance(enabled, bool) or not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise StarDayError('请设置签到开关和正确的每日开始时间。', 'INVALID_SETTINGS', 400)
        if not isinstance(prepare, str) or not re.fullmatch(r'(?:0[89]:[0-5]\d|10:00)', prepare):
            raise StarDayError('凭据准备时间请设置为 08:00 至 10:00 之间的分钟。', 'INVALID_SETTINGS', 400)
        with self.lock:
            changed = deepcopy(self.data)
            changed['settings'] = {'signin_enabled': enabled, 'signin_time': clock, 'session_prepare_time':prepare}
            self._commit(changed)
        return '签到设置已应用于所有有效账号，新增或恢复登录后自动纳入。'

    def open_page(self):
        """A foreground page entry may establish a missing campaign login."""
        for account in self.accounts():
            if account['status'] != 'active' or self._deferred(account['id']):
                continue
            snapshot = self.credentials(account['id'])
            if self.sessions.load(account['id'], snapshot['phone']) is None:
                self._queue(account['id'], 'inspect', manual=True)
            elif self.data['accounts'].get(account['id'], {}).get('query_state') in ('failed','waiting_session','unknown'):
                self._queue(account['id'], 'inspect')

    def sms_snapshot(self, login_id):
        if not isinstance(login_id,str) or not any(a['id'] == login_id for a in self.accounts()):
            raise StarDayError('请选择共享账号池中的账号。', 'INVALID_SELECTION',400)
        return {**self.credentials(login_id),'id':login_id}

    def sms_login(self, payload):
        login_id = payload.get('login_id')
        result = self.sms.login(self.sms_snapshot(login_id),payload.get('code'))
        with self.lock:
            changed = deepcopy(self.data)
            record = self._record(changed,login_id)
            record.update(query_state='ready', error_code='', message=result['message'])
            record.pop('retry_at',None)
            record.pop('retry_reason',None)
            self._commit(changed)
        self._queue(login_id,'inspect')
        return result

    def products(self):
        account = next((a for a in self.accounts() if a['status'] == 'active'), None)
        if account is None:
            raise StarDayError('请先登录一个有效账号，再读取官方权益列表。', 'APP_LOGIN_REQUIRED', 401)
        products = self.client_factory(lambda: self.credentials(account['id'])).products()
        with self.lock:
            self.catalog = products
        return deepcopy(products)

    def calibrate(self, queued=False):
        with self.lock:
            if self.calibrating and not queued:
                return {'state':'running'}
            self.calibrating = True
        try:
            account = next((a for a in self.accounts() if a['status'] == 'active'
                and not self.data['accounts'].get(a['id'], {}).get('retry_at')), None)
            if account is None:
                raise StarDayError('暂无可校准账号；资格或认证检查已暂停至次日，未重复请求。', 'DAILY_CHECK_DEFERRED', 409)
            result = self.client_factory(lambda:self.credentials(account['id'])).calibrate()
            with self.lock:
                self.calibration = result
                self.warning = ''
            return deepcopy(result)
        except StarDayError as error:
            self.warning = '时间校准未完成：'+str(error)
            with self.lock:
                self.calibration = {'state':'failed', 'at':self.now().isoformat(), 'message':str(error)}
            return deepcopy(self.calibration)
        finally:
            with self.lock:
                self.calibrating = False

    def schedule(self, payload, *, immediate=False):
        product_id, phase, text = payload.get('product_id'), payload.get('phase'), payload.get('at')
        recipient = payload.get('recipient')
        if recipient is not None and (not isinstance(recipient, dict)
                or recipient.get('kind') not in ('family','phone')
                or recipient['kind'] == 'family' and (not isinstance(recipient.get('id'), str) or not re.fullmatch(r'[a-f0-9]{64}', recipient['id']))
                or recipient['kind'] == 'phone' and (not isinstance(recipient.get('phone'), str) or not re.fullmatch(r'1[3-9]\d{9}', recipient['phone']))):
            raise StarDayError('请选择接收亲友，或填写正确的中国移动号码。', 'INVALID_RECIPIENT', 400)
        offsets = [0] if immediate else payload.get('offsets_ms', [-50, 0, 50, 100, 150])
        if (not isinstance(product_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', product_id)
                or isinstance(phase, bool) or not isinstance(phase, int) or not 1 <= phase <= 4
                or text is not None and not isinstance(text, str)):
            raise StarDayError('请选择官方权益。' if immediate else '请选择官方权益和抢购时间。', 'INVALID_PRODUCT', 400)
        if (not isinstance(offsets, list) or not 1 <= len(offsets) <= 20
                or any(isinstance(v, bool) or not isinstance(v, int) or not -2000 <= v <= 2000 for v in offsets)):
            raise StarDayError('请配置 1–20 轮，每轮偏移为 -2000 至 2000 毫秒。', 'INVALID_OFFSETS', 400)
        offsets = sorted(offsets)
        if immediate:
            at = self.now()
        else:
            at = self.now().replace(hour=10, minute=0, second=0, microsecond=0)
            if text is not None:
                try:
                    requested = datetime.fromisoformat(text)
                except ValueError as error:
                    raise StarDayError('请输入正确的抢购时间。', 'INVALID_TIME', 400) from error
                requested = requested.replace(tzinfo=SHANGHAI) if requested.tzinfo is None else requested.astimezone(SHANGHAI)
                if requested <= self.now():
                    raise StarDayError('抢购时间必须晚于当前北京时间。', 'INVALID_TIME', 400)
                at = requested.replace(hour=10, minute=0, second=0, microsecond=0)
            if at <= self.now():
                at += timedelta(days=1)
        if not self.catalog:
            self.products()
        product = next((p for p in self.catalog if p['id'] == product_id and p['phase'] == phase), None)
        if product is None:
            raise StarDayError('此权益未在当前官方列表中，请刷新后选择。', 'PRODUCT_MISSING', 409)
        if recipient is not None and not product.get('transferable'):
            raise StarDayError('官方不支持赠送此权益。', 'NOT_TRANSFERABLE', 409)
        known = {a['id']:a for a in self.accounts()}
        ids = payload.get('login_ids', [a['id'] for a in known.values() if a['status'] == 'active'])
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in known for i in ids):
            raise StarDayError('请选择需要抢购的账号。', 'INVALID_SELECTION', 400)
        if any(known[i]['status'] != 'active' for i in ids):
            raise StarDayError('请先恢复所选账号的登录。', 'APP_LOGIN_REQUIRED', 401)
        if recipient is not None and len(set(ids)) != 1:
            raise StarDayError('每次赠送请选择一个来源账号。', 'INVALID_SELECTION', 400)
        recipient_label = ''
        if recipient is not None:
            options = self.gift_options(ids[0])
            if recipient['kind'] == 'family':
                member = next((m for m in options['family'] if m['id'] == recipient['id']), None)
                if member is None:
                    raise StarDayError('亲友列表已变化，请重新确认接收人。', 'FAMILY_CHANGED', 409)
                recipient_label = member['name']+' · '+member['phone_masked']
            else:
                if not options['can_send_other']:
                    raise StarDayError('官方仅允许卓越卡直接填写其他号码，请选择亲友。', 'FAMILY_ONLY', 409)
                recipient_label = recipient['phone'][:3]+'****'+recipient['phone'][-4:]
        with self.lock:
            changed = deepcopy(self.data)
            job_id = secrets.token_hex(16)
            changed['jobs'][job_id] = {'id': job_id, 'product_id': product_id, 'phase': phase,
                'product_name': product['name'], 'at': at.isoformat(), 'enabled': True, 'state': 'waiting',
                'immediate': immediate,
                'is_random':product.get('is_random', False),
                'daily': not immediate, 'round_date': None,
                'login_ids': list(dict.fromkeys(ids)),
                'burst_count': len(offsets), 'burst_interval': 0, 'burst_workers': len(offsets),
                'offsets_ms': offsets, 'results': {}}
            if recipient is not None:
                changed['jobs'][job_id]['recipient'] = {k:recipient[k] for k in ('kind','id' if recipient['kind'] == 'family' else 'phone')}
                changed['jobs'][job_id]['recipient_label'] = recipient_label
            self._commit(changed)
        if recipient is not None and immediate:
            return {'id':job_id, 'message':'已安排赠送，结果将显示在账号和权益领取任务中；赠送成功计入该账号本月已领取。'}
        if recipient is not None:
            return {'id':job_id, 'message':'已启用每日 10:00 抢购赠送；接收人按确认的配置执行，赠送成功后当月停止，下个月自动继续。'}
        if immediate:
            return {'id': job_id, 'message': f'已安排 {len(set(ids))} 个账号立即领取；先核对本月领取状态，每个账号仅提交一次。'}
        return {'id': job_id, 'message': f'已设置每日 10:00 抢购，每账号 {len(offsets)} 轮；成功后当月停止，下月自动继续。'}

    def stop(self, job_id):
        with self.lock:
            if not isinstance(job_id, str) or job_id not in self.data['jobs']:
                raise StarDayError('任务不存在，请刷新。', 'JOB_MISSING', 404)
            changed = deepcopy(self.data)
            changed['jobs'].pop(job_id)
            self._commit(changed)
            self.cancellations.pop(job_id, Event()).set()
            self.starts.pop(job_id, None)

    def run(self, ids=None, *, inspect=False):
        known = {a['id']: a for a in self.accounts()}
        if ids is None:
            ids = [a['id'] for a in known.values() if a['status'] == 'active']
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in known for i in ids):
            raise StarDayError('请选择共享账号池中的有效账号。', 'INVALID_SELECTION', 400)
        if any(known[i]['status'] != 'active' for i in ids):
            raise StarDayError('请先恢复所选账号的登录。', 'APP_LOGIN_REQUIRED', 401)
        count = sum(self._queue(i, 'inspect' if inspect else 'signin', manual=True) for i in dict.fromkeys(ids))
        if inspect:
            return f'已安排 {count} 个账号重新认证并查询官方状态，不会提交领取或发送验证码。'
        return f'已安排 {count} 个账号核对并签到，今日已签到的账号不会重复提交。'

    def gift_options(self, login_id):
        account = next((a for a in self.accounts() if a['id'] == login_id), None)
        if account is None or account['status'] != 'active':
            raise StarDayError('请选择已登录的来源账号。', 'APP_LOGIN_REQUIRED', 401)
        return self.client_factory(lambda:self.credentials(login_id)).gift_options()

    def _queue(self, login_id, kind, job_id=None, *, manual=False):
        with self.lock:
            if self.load_error or login_id in self.running or not manual and self._deferred(login_id):
                return False
            changed = deepcopy(self.data)
            record = self._record(changed, login_id)
            account = next(a for a in self.accounts() if a['id'] == login_id)
            if kind == 'prepare':
                record['prepared_date'] = self.now().date().isoformat()
            if kind in ('signin','prepare'):
                record.update(last_run_at=self.now().isoformat(), attempt_revision=account.get('credential_revision', 0))
            if job_id is not None:
                job = changed['jobs'].get(job_id)
                if job is None or not job['enabled']:
                    return False
                job['state'] = 'running'
                job['results'][login_id] = {'state': 'preparing', 'message': '正在准备账号会话'}
                task = deepcopy(job)
                task['cancel'] = self.cancellations.setdefault(job_id, Event())
                task['start_monotonic'] = self.starts.get(job_id)
            else:
                task = None
            self._commit(changed)
            self.running.add(login_id)
        self.executor.submit(self._perform, login_id, kind, task, manual)
        return True

    def _perform(self, login_id, kind, job, manual=False):
        job_id = job['id'] if job is not None else None
        submitted = set()
        def before_submit(action, period):
            expected = r'\d{8}' if action == 'signin' else r'\d{6}'
            if action not in ('signin', 'benefit') or not isinstance(period, str) or not re.fullmatch(expected, period):
                raise StarDayError('官方活动周期无效，本次没有提交。', 'INVALID_PERIOD', 502)
            key = f'{login_id}:{action}:{period}'
            with self.lock:
                if job is not None and job['cancel'].is_set():
                    raise StarDayError('任务已停止并移除，本次未提交。', 'JOB_STOPPED', 409, rejected=True)
                if key in submitted:
                    return True
                if self.data['actions'].get(key, {}).get('submitted'):
                    return False
                changed = deepcopy(self.data)
                changed['actions'][key] = {'submitted': True, 'time': self.now().isoformat()}
                self._commit(changed)
                submitted.add(key)
            return True
        for attempt in range(2):
            try:
                client = self.client_factory(lambda: self.credentials(login_id))
                client.allow_login = manual
                client.prepare_time = self.data['settings']['session_prepare_time']
                if kind == 'prepare':
                    client.prepare()
                    client.allow_login = False
                    hour, minute = map(int, self.data['settings']['signin_time'].split(':'))
                    can_sign = self.data['settings']['signin_enabled'] and self.now() >= self.now().replace(hour=hour,minute=minute,second=0,microsecond=0)
                    result = client.signin(before_submit=before_submit) if can_sign else client.inspect()
                elif kind == 'inspect':
                    result = client.inspect()
                elif kind == 'signin':
                    result = client.signin(before_submit=before_submit)
                else:
                    if job.get('immediate'):
                        if job['cancel'].is_set() or self.quit.is_set():
                            result = {'state':'stopped', 'message':'已停止立即领取，本次未提交。'}
                        else:
                            gift = {'recipient':job['recipient']} if 'recipient' in job else {}
                            result = client.claim(job['product_id'], job['phase'], before_submit=before_submit,
                                                  product_name=job['product_name'], **gift)
                    else:
                        month = datetime.fromisoformat(job['at']).strftime('%Y%m')
                        if self._claimed(login_id, month):
                            result = {'state':'already_claimed', 'message':'本月已领取，不再重复提交。'}
                        else:
                            pending = self.data['actions'].get(f'{login_id}:benefit:{month}', {}).get('submitted')
                            if pending:
                                observed = client.inspect()
                                if observed.get('month_claimed') is True:
                                    result = {**observed, 'state':'already_claimed', 'message':'官方确认本月已领取。'}
                                elif observed.get('month_claimed') is False and observed.get('date', '')[:6] == month:
                                    with self.lock:
                                        changed = deepcopy(self.data)
                                        changed['actions'].pop(f'{login_id}:benefit:{month}', None)
                                        self._commit(changed)
                                    pending = False
                                else:
                                    result = {**observed, 'state':'unknown', 'uncertain':True, 'message':'未取得本月领取状态，本轮未重复提交。'}
                            if not pending:
                                gift = {'recipient':job['recipient'], 'is_random':job.get('is_random', False)} if 'recipient' in job else {}
                                result = client.rush(job['product_id'], job['phase'], target_at=datetime.fromisoformat(job['at']),
                                    burst={key: job[key] for key in ('burst_count', 'burst_interval', 'burst_workers', 'offsets_ms')},
                                    before_submit=before_submit, start_monotonic=job['start_monotonic'],
                                    product_name=job['product_name'],
                                    cancelled=lambda: self.quit.is_set() or job['cancel'].is_set(), **gift)
                break
            except (StarDayError, LoginStorageError) as error:
                code = getattr(error, 'code', '')
                auth_failed = code in ('AUTH_FAILED', 'APP_UID_FAILED', 'APP_CREDENTIALS_CHANGED', '-800', 'UPSTREAM_RATE_LIMITED')
                # A changed snapshot can be read once more. A campaign denial
                # must never force a primary APP identity refresh.
                if code == 'APP_CREDENTIALS_CHANGED' and attempt == 0 and not submitted:
                    continue
                result = {'state': 'waiting_session' if code == 'CAMPAIGN_LOGIN_PENDING' else 'stopped' if code == 'JOB_STOPPED' else 'login_required' if getattr(error, 'status', None) == 401 else 'auth_failed' if auth_failed else 'failed',
                          'code':code, 'message': str(error), 'uncertain': bool(submitted) and not getattr(error, 'rejected', False)}
                break
        with self.lock:
            changed = deepcopy(self.data)
            record = self._record(changed, login_id)
            observed = record['observation']
            if (result.get('month_claimed') is False or result.get('date')
                    and result['date'][:6] != observed.get('date', '')[:6]):
                result.setdefault('benefit_recipient', '')
                result.setdefault('benefit_mode', '')
            if (not result.get('benefit_name') and result.get('benefit_id')
                    and result['benefit_id'] == observed.get('benefit_id')
                    and result.get('date', '')[:6] == observed.get('date', '')[:6]):
                result['benefit_name'] = observed.get('benefit_name', '')
            if (result.get('month_claimed') is True and result.get('benefit_id') == observed.get('benefit_id')
                    and result.get('date', '')[:6] == observed.get('date', '')[:6]):
                for key in ('benefit_recipient','benefit_mode'):
                    if key not in result and key in observed:
                        result[key] = observed[key]
            record['observation'].update({k: result[k] for k in ('date', 'today_state', 'consecutive_days',
                'month_claimed', 'benefit_name', 'benefit_id', 'benefit_recipient', 'benefit_mode', 'eligible', 'tier', 'tier_name', 'server_time') if k in result})
            record['message'] = result.get('message', '官方状态已更新')
            record['query_state'] = result.get('state', 'ready')
            record['error_code'] = result.get('code', '')
            record['last_checked_at'] = self.now().isoformat()
            reason = self._daily_retry_reason(record)
            if reason:
                record.update(retry_at=self._tomorrow(changed['settings']['session_prepare_time']), retry_reason=reason)
            else:
                record.pop('retry_at', None)
                record.pop('retry_reason', None)
            if result.get('month_claimed') is True or result.get('state') in ('success', 'already_claimed'):
                date = result.get('date') or (datetime.fromisoformat(job['at']).strftime('%Y%m%d') if job is not None else self.now().strftime('%Y%m%d'))
                changed['actions'][f'{login_id}:benefit:{date[:6]}'] = {'submitted':True, 'confirmed':True, 'time':self.now().isoformat()}
            if kind != 'inspect':
                event = {'time': self.now().isoformat(), 'kind': kind, 'state': result.get('state', 'unknown'),
                         'message': record['message'], 'attempt_count': result.get('attempt_count', 0),
                         'attempts': result.get('attempts', [])}
                record['history'] = [*record['history'], event][-90:]
                if result.get('state') in ('failed', 'auth_failed', 'skipped', 'login_required', 'stopped') and not result.get('uncertain'):
                    for key in submitted:
                        changed['actions'].pop(key, None)
            if job_id is not None and job_id in changed['jobs']:
                changed['jobs'][job_id]['results'][login_id] = {
                    'state': result.get('state', 'unknown'), 'message': record['message'],
                    'time':self.now().isoformat(),
                    'attempt_count': result.get('attempt_count', 0), 'attempts': result.get('attempts', [])}
            self._commit(changed)
            self.running.discard(login_id)
            self.checked[login_id] = self.now()

    def status(self):
        accounts, now = self.accounts(), self.now()
        with self.lock:
            for account in accounts:
                record = self.data['accounts'].get(account['id'], {})
                observation = record.get('observation', {})
                today = observation.get('date') == now.strftime('%Y%m%d')
                account.update(today_state=observation.get('today_state', 'unknown') if today else 'unknown',
                    consecutive_days=observation.get('consecutive_days') if today else None,
                    month_claimed=observation.get('month_claimed') if observation.get('date', '')[:6] == now.strftime('%Y%m') else None,
                    benefit_name=observation.get('benefit_name', '') if observation.get('date', '')[:6] == now.strftime('%Y%m') else '',
                    benefit_id=observation.get('benefit_id', '') if observation.get('date', '')[:6] == now.strftime('%Y%m') else '',
                    benefit_recipient=observation.get('benefit_recipient', '') if observation.get('date', '')[:6] == now.strftime('%Y%m') else '',
                    last_run_at=record.get('last_run_at'),
                    message=record.get('message', '尚未查询'), state='running' if account['id'] in self.running else record.get('query_state', 'unknown'),
                    error_code=record.get('error_code', ''),
                    retry_at=record.get('retry_at') if self._deferred(account['id']) else None,
                    retry_reason=record.get('retry_reason', ''),
                    eligible=observation.get('eligible'), tier=observation.get('tier'), tier_name=observation.get('tier_name', ''),
                    history=deepcopy(record.get('history', [])),
                    next_run_at=self._next(self.data['settings']['signin_time'], retry=True) if not today or observation.get('today_state') != 'signed'
                        else self._next(self.data['settings']['signin_time']))
                if account['status'] != 'active':
                    account['next_run_at'] = None
                elif account['retry_at']:
                    account['next_run_at'] = account['retry_at']
                elif account['state'] == 'waiting_session':
                    account['next_run_at'] = self._next(self.data['settings']['session_prepare_time'])
            jobs = deepcopy(list(self.data['jobs'].values()))
            names = {a['id']: a.get('name') or a['phone_masked'] for a in accounts}
            for job in jobs:
                job['is_gift'] = 'recipient' in job
                job.pop('recipient', None)
                for login_id, result in job['results'].items():
                    result['account_name'] = names.get(login_id, '已退出账号')
            return {'accounts': accounts, 'settings': deepcopy(self.data['settings']),
                    'products': deepcopy(self.catalog), 'jobs': jobs, 'server_time': now.isoformat(), 'warning': self.warning,
                    'calibration':{'state':'running'} if self.calibrating else deepcopy(self.calibration)}

    def tick(self):
        accounts, now = self.accounts(), self.now()
        previous, self.readiness = self.readiness, {a['id']: (a['status'], a.get('credential_revision', 0)) for a in accounts}
        with self.lock:
            if self.load_error:
                return
            settings, records = deepcopy(self.data['settings']), deepcopy(self.data['accounts'])
            jobs = deepcopy(list(self.data['jobs'].values()))
        hour, minute = map(int, settings['signin_time'].split(':'))
        for account in accounts:
            if account['status'] != 'active' or not settings['signin_enabled'] or now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
                continue
            record = records.get(account['id'], {})
            observed = record.get('observation', {})
            if record.get('query_state') == 'waiting_session':
                continue
            if observed.get('date') == now.strftime('%Y%m%d') and observed.get('today_state') == 'signed':
                continue
            last = record.get('last_run_at')
            recovered = previous.get(account['id']) != self.readiness[account['id']]
            revision = record.get('attempt_revision') != account.get('credential_revision', 0)
            if recovered or revision or not last or (now-datetime.fromisoformat(last)).total_seconds() >= 300:
                self._queue(account['id'], 'signin')
        active = {a['id'] for a in accounts if a['status'] == 'active'}
        known = {a['id'] for a in accounts}
        # One extra login per selected account before the daily rush. Persist
        # the reservation before dispatch so a restart cannot repeat it.
        prepare_hour, prepare_minute = map(int, settings['session_prepare_time'].split(':'))
        prepare_at = now.replace(hour=prepare_hour, minute=prepare_minute, second=0, microsecond=0)
        if prepare_at <= now < prepare_at+timedelta(minutes=1):
            preparation = set(active) if settings['signin_enabled'] else set()
            for job in jobs:
                if job['enabled'] and not job.get('immediate') and datetime.fromisoformat(job['at']).date() == now.date():
                    preparation.update(set(job.get('login_ids', active)) & active)
            for login_id in preparation:
                record = self.data['accounts'].get(login_id, {})
                if (record.get('prepared_date') != now.date().isoformat()
                        and not self._deferred(login_id)
                        and (settings['signin_enabled'] or not self._claimed(login_id, now.strftime('%Y%m')))):
                    self._queue(login_id, 'prepare')
        for snapshot in jobs:
            with self.lock:
                job = self.data['jobs'].get(snapshot['id'])
                if job is None or not job['enabled']:
                    continue
                job_id = job['id']
                at = datetime.fromisoformat(job['at'])
                selected = set(job.get('login_ids', active)) & known
                targets = selected & active
                outstanding = any(r['state'] in ('preparing', 'running') for r in job['results'].values())
                month = at.strftime('%Y%m')
                claimed = {i for i in selected if self._claimed(i, month)}
                done = bool(selected) and selected <= job['results'].keys()
                expired = now > at+timedelta(seconds=120)
                if not outstanding and (not selected or expired or done and
                        (job.get('immediate') or job.get('round_date') == at.date().isoformat())
                        or not job.get('immediate') and selected == claimed):
                    changed = deepcopy(self.data)
                    current = changed['jobs'][job_id]
                    if job.get('immediate'):
                        state = 'completed' if done and all(r['state'] in ('success', 'already_claimed') for r in job['results'].values()) else 'finished' if job['results'] else 'missed'
                        current.update(enabled=False, state=state)
                    elif not selected:
                        changed['jobs'].pop(job_id)
                    else:
                        next_at = now.replace(hour=10, minute=0, second=0, microsecond=0)
                        complete = selected == claimed and month == now.strftime('%Y%m')
                        if complete:
                            next_at = (now.replace(day=28)+timedelta(days=4)).replace(day=1, hour=10, minute=0, second=0, microsecond=0)
                            for login_id in claimed:
                                current['results'].setdefault(login_id, {'state':'already_claimed', 'message':'本月已领取权益。'})
                        elif next_at <= now or done and at.date() == now.date():
                            next_at += timedelta(days=1)
                        current.update(at=next_at.isoformat(), enabled=True,
                                       state='monthly_completed' if complete else 'waiting', round_date=None)
                        if next_at.strftime('%Y%m') != month:
                            current['results_month'] = month
                    self._commit(changed)
                    self.starts.pop(job_id, None)
                    continue
                if now < at-timedelta(seconds=30) or outstanding:
                    if job['state'] == 'monthly_completed' and month == now.strftime('%Y%m'):
                        changed = deepcopy(self.data)
                        changed['jobs'][job_id]['state'] = 'waiting'
                        self._commit(changed)
                    continue
                if not job.get('immediate') and job.get('round_date') != at.date().isoformat():
                    changed = deepcopy(self.data)
                    current = changed['jobs'][job_id]
                    current.update(round_date=at.date().isoformat(), results_month=month,
                        results={i:{'state':'already_claimed','message':'本月已领取，不再重复提交。'} for i in claimed})
                    self._commit(changed)
                    job = self.data['jobs'][job_id]
                if not job.get('immediate'):
                    recheck = {i for i in targets-job['results'].keys()
                        if self.data['accounts'].get(i, {}).get('retry_at') and not self._deferred(i)}
                    if recheck:
                        for login_id in recheck:
                            self._queue(login_id, 'inspect')
                    deferred = {i for i in targets-job['results'].keys() if self._deferred(i)}
                    if deferred:
                        changed = deepcopy(self.data)
                        for login_id in deferred:
                            record = changed['accounts'][login_id]
                            changed['jobs'][job_id]['results'][login_id] = {'state':'skipped',
                                'message':'资格或认证检查每日一次，下次检查 '+record['retry_at'], 'time':now.isoformat()}
                        self._commit(changed)
                        job = self.data['jobs'][job_id]
                    if not targets-job['results'].keys()-recheck:
                        continue
                if not job.get('immediate') and job_id not in self.starts:
                    calibration = self.calibration
                    fresh = (calibration is not None and calibration['state'] == 'ready'
                        and (now-datetime.fromisoformat(calibration['at'])).total_seconds() < 60)
                    if not fresh:
                        recent_failure = (calibration is not None and calibration['state'] == 'failed'
                            and (now-datetime.fromisoformat(calibration['at'])).total_seconds() < 10)
                        if not self.calibrating and not recent_failure:
                            self.calibrating = True
                            self.executor.submit(self.calibrate, True)
                        continue
                    clock_at, clock_mono = self.now(), time.monotonic()
                    self.starts[job_id] = clock_mono+(at-clock_at).total_seconds()-calibration['offset_ms']/1000
                    changed = deepcopy(self.data)
                    changed['jobs'][job_id]['calibration'] = deepcopy(calibration)
                    self._commit(changed)
                if now >= at and selected-targets-job['results'].keys():
                    changed = deepcopy(self.data)
                    for login_id in selected-targets-job['results'].keys():
                        changed['jobs'][job_id]['results'][login_id] = {'state':'login_required', 'message':'账号尚未恢复登录，恢复后纳入后续每日 10:00 抢购。'}
                    self._commit(changed)
                for login_id in targets-job['results'].keys():
                    if not job.get('immediate') and login_id in recheck:
                        continue
                    self._queue(login_id, 'benefit', job_id, manual=job.get('immediate', False))

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.running

    def forget(self, login_id):
        with self.lock:
            if login_id in self.running:
                raise StarDayError('该账号的星动日任务正在执行，请完成后退出。', 'ACCOUNT_BUSY', 409)
            changed = deepcopy(self.data)
            changed['accounts'].pop(login_id, None)
            changed['actions'] = {k: v for k, v in changed['actions'].items() if not k.startswith(login_id+':')}
            for job in changed['jobs'].values():
                job['results'].pop(login_id, None)
                if 'login_ids' in job:
                    job['login_ids'] = [i for i in job['login_ids'] if i != login_id]
            self._commit(changed)
            self.checked.pop(login_id, None)
            self.sessions.forget(login_id)
            self.sms.forget(login_id)

    def start(self):
        if self.thread is not None:
            return
        def loop():
            while not self.quit.is_set():
                try:
                    self.tick()
                except (StarDayError, LoginStorageError) as error:
                    self.warning = str(error)
                self.quit.wait(0.5)
        self.thread = Thread(target=loop, daemon=True, name='star-day-scheduler')
        self.thread.start()

    def shutdown(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)
