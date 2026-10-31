"""Daily reconciliation with separate monthly claims and daily punch markers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import re
from threading import Event, RLock, Thread

from login_storage import LoginStorageError
from monthly_coupon_auth import MonthlyCouponAuthError, DEFAULT_ENTRY_URL, validate_entry_url
from recharge.cmcc import SHANGHAI


class MonthlyTaskError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class MonthlyCouponScheduler:
    def __init__(self, credentials, accounts, client_factory, *, storage=None, executor=None, now=None):
        self.credentials, self.accounts, self.client_factory = credentials, accounts, client_factory
        self.storage, self.now = storage, now or (lambda: datetime.now(SHANGHAI))
        self.executor = executor or ThreadPoolExecutor(max_workers=16, thread_name_prefix='monthly-coupon')
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.running, self.executing, self.execute_after_refresh, self.checked = set(), set(), set(), {}
        self.data = {'accounts': {}, 'actions': {}}
        self.warning = self.load_error = ''
        if storage is not None:
            try:
                saved = storage.load_monthly_coupon_data()
                if saved is not None:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(k), dict) for k in self.data):
                        raise LoginStorageError('每月话费券数据格式无效，已保留原数据库。')
                    self.data = saved
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)
        clocks = [r['clock'] for r in self.data['accounts'].values() if r['auto_enabled']]
        if 'automation' not in self.data:
            self.data['automation'] = {'enabled': True, 'time': max(clocks, key=clocks.count) if clocks else '08:05'}
            known = {a['id'] for a in self.accounts()}
            for login_id, record in self.data['accounts'].items():
                if login_id in known:
                    record.update(auto_enabled=True, clock=self.data['automation']['time'],
                        next_run_at=self._next(self.data['automation']['time']))
        self.data.setdefault('page', {'entry_url': DEFAULT_ENTRY_URL})
        self.data['automation'].setdefault('retry_interval_minutes', 5)
        self.data['automation'].setdefault('max_retries', 5)
        for record in self.data['accounts'].values():
            if record.get('last_run_at') and not record.get('attempt_date'):
                record.update(attempt_date=datetime.fromisoformat(record['last_run_at']).strftime('%Y%m%d'),
                              attempt_count=1, last_finished_at=record['last_run_at'])
            observed = record.get('observation', {})
            for kind in ('direct', 'bonus'):
                if (observed.get(kind+'_state') == 'claimed' and observed.get(kind+'_message') == '本月已领取'
                        and not observed.get(kind+'_prize') and not observed.get(kind+'_receipt')):
                    observed[kind+'_state'] = 'participated'
                    observed[kind+'_message'] = '官方提示本月参与次数已用完（901），奖品名称与发券结果待核实'

    def page(self):
        return {**self.data['page'], 'default_entry_url': DEFAULT_ENTRY_URL}

    def update_page(self, payload):
        if not isinstance(payload, dict):
            raise MonthlyTaskError('请提交活动地址设置。')
        entry_url = validate_entry_url(payload.get('entry_url'))
        with self.lock:
            if self.running:
                raise MonthlyTaskError('当前有每月话费券任务正在执行，请等待结束后修改活动地址。', 409)
            changed = deepcopy(self.data)
            changed['page'] = {'entry_url': entry_url}
            self._commit(changed)
            self.checked.clear()
        return {'page': self.page(), 'message': '活动地址已保存，后续认证和任务请求使用该地址的渠道参数。'}

    def _ensure_accounts(self, accounts):
        with self.lock:
            if self.load_error:
                return
            missing = [a['id'] for a in accounts if a['status'] == 'active' and a['id'] not in self.data['accounts']]
            transitions = [a for a in accounts if a['id'] in self.data['accounts']
                and self.data['accounts'][a['id']].get('login_status') != a['status']]
            if not missing and not transitions:
                return
            changed = deepcopy(self.data)
            for login_id in missing:
                record = self._record(changed, login_id)
                record['login_status'] = 'active'
                record['next_run_at'] = self._next(record['clock']) if record['auto_enabled'] else None
            recovered = []
            for account in transitions:
                record = changed['accounts'][account['id']]
                previous = record.get('login_status')
                record['login_status'] = account['status']
                if account['status'] == 'active' and (previous == 'login_required'
                        or previous is None and record.get('query_state') == 'login_required'):
                    recovered.append(account['id'])
                    # A new login resolves authentication failures, not activity
                    # qualification or uncertain business submission markers.
                    if record.get('query_state') == 'login_required':
                        for key in ('query_error', 'query_state', 'error_code', 'retry_at',
                                    'attempt_date', 'attempt_count', 'last_finished_at'):
                            record.pop(key, None)
                        record['message'] = '登录已恢复，正在核对官方领取与任务进度'
                        if record['auto_enabled']:
                            hour, minute = map(int, record['clock'].split(':'))
                            start = self.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
                            record['next_run_at'] = max(self.now(), start).isoformat()
            self._commit(changed)
            for login_id in recovered:
                self.checked.pop(login_id, None)

    def _commit(self, data):
        if self.load_error:
            raise MonthlyTaskError(self.load_error, 503)
        if self.storage is not None:
            self.storage.save_monthly_coupon_data(data)
        self.data = data

    def _record(self, data, login_id):
        return data['accounts'].setdefault(login_id, {'auto_enabled': data['automation']['enabled'], 'clock': data['automation']['time'],
            'next_run_at': None, 'last_run_at': None, 'message': '尚未执行', 'observation': {}, 'history': []})

    def _next(self, clock):
        now = self.now()
        hour, minute = map(int, clock.split(':'))
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (at if at > now else at + timedelta(days=1)).isoformat()

    def _retry_time(self, record, automation=None):
        now = self.now()
        hour, minute = map(int, record['clock'].split(':'))
        tomorrow = (now+timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if record.get('retry_at') and datetime.fromisoformat(record['retry_at']) > now:
            return record['retry_at']
        config = automation or self.data['automation']
        if record.get('attempt_date') == now.strftime('%Y%m%d') and record.get('attempt_count', 0) >= config['max_retries']+1:
            return tomorrow.isoformat()
        finished = datetime.fromisoformat(record['last_finished_at'])
        at = max(finished+timedelta(minutes=config['retry_interval_minutes']),
                 now.replace(hour=hour, minute=minute, second=0, microsecond=0))
        return (at if at.date() == now.date() else tomorrow).isoformat()

    def _selection(self, ids, *, active):
        known = {a['id']: a for a in self.accounts()}
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in known for i in ids):
            raise MonthlyTaskError('请选择共享账号池中的账号。')
        if active and any(known[i]['status'] != 'active' for i in ids):
            raise MonthlyTaskError('所选账号尚未就绪，请先校验或重新登录。', 409)
        return list(dict.fromkeys(ids))

    def settings(self, payload):
        enabled, clock = payload.get('enabled'), payload.get('time', '08:05')
        interval = payload.get('retry_interval_minutes', self.data['automation']['retry_interval_minutes'])
        retries = payload.get('max_retries', self.data['automation']['max_retries'])
        if not isinstance(enabled, bool) or not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise MonthlyTaskError('请设置自动开关和正确的执行时间。')
        if (isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 1440
                or isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 100):
            raise MonthlyTaskError('重试间隔为 1–1440 分钟，失败重试次数为 0–100 次。')
        global_settings = payload.get('login_ids') is None
        ids = [a['id'] for a in self.accounts()] if global_settings else self._selection(payload['login_ids'], active=enabled)
        with self.lock:
            changed = deepcopy(self.data)
            if global_settings:
                changed['automation'] = {'enabled': enabled, 'time': clock,
                    'retry_interval_minutes':interval, 'max_retries':retries}
            for login_id in ids:
                record = self._record(changed, login_id)
                record.update(auto_enabled=enabled, clock=clock, next_run_at=self._next(clock) if enabled else None)
                if (enabled and record.get('attempt_date') == self.now().strftime('%Y%m%d')
                        and record.get('last_finished_at') and record.get('completed_date') != self.now().strftime('%Y%m%d')):
                    record['next_run_at'] = self._retry_time(record, changed['automation'])
            self._commit(changed)
        return ('所有有效账号已启用每日检查，新增或恢复登录后自动纳入' if enabled else '已停用全局自动检查') if global_settings else f'已为 {len(ids)} 个账号'+('开启每日检查；两种话费券每月分别领取一次' if enabled else '停用自动执行')

    def run(self, ids):
        selected = self._selection(ids, active=True)
        count = sum(self._queue(i, execute=True, manual=True) for i in selected)
        return f'已安排 {count} 个账号执行，正在执行的账号不会重复安排'

    def _queue(self, login_id, *, execute, manual=False):
        with self.lock:
            if self.load_error:
                return False
            retry_at = self.data['accounts'].get(login_id, {}).get('retry_at')
            if not manual and retry_at and self.now() < datetime.fromisoformat(retry_at):
                return False
            record = self.data['accounts'].get(login_id, {})
            if (not manual and record.get('attempt_date') == self.now().strftime('%Y%m%d')
                    and record.get('completed_date') != self.now().strftime('%Y%m%d')):
                if (record.get('attempt_count', 0) >= self.data['automation']['max_retries']+1
                        or record.get('next_run_at') and self.now() < datetime.fromisoformat(record['next_run_at'])):
                    return False
            if login_id in self.running:
                if execute and login_id not in self.executing:
                    self.execute_after_refresh.add(login_id)
                    return True
                return False
            if execute:
                changed = deepcopy(self.data)
                record = self._record(changed, login_id)
                account = next(a for a in self.accounts() if a['id'] == login_id)
                record.update(last_run_at=self.now().isoformat(), message='正在核对官方领取与任务记录',
                    attempt_revision=account.get('credential_revision', 0))
                count = record.get('attempt_count', 0) if record.get('attempt_date') == self.now().strftime('%Y%m%d') else 0
                record.update(attempt_date=self.now().strftime('%Y%m%d'), attempt_count=count+1)
                if record['auto_enabled']:
                    record['next_run_at'] = self._next(record['clock'])
                self._commit(changed)
            self.running.add(login_id)
            if execute:
                self.executing.add(login_id)
        self.executor.submit(self._perform, login_id, execute)
        return True

    def _perform(self, login_id, execute):
        def before_submit(action, period):
            expected = r'\d{8}' if action == 'punch' else r'\d{6}'
            if action not in ('direct', 'bill', 'signin', 'age', 'punch', 'bonus') or not isinstance(period, str) or not re.fullmatch(expected, period):
                raise MonthlyTaskError('官方执行周期无效，本次未提交。', 502)
            key = f'{login_id}:{action}:{period}'
            with self.lock:
                if self.data['actions'].get(key, {}).get('submitted'):
                    return False
                changed = deepcopy(self.data)
                changed['actions'][key] = {'submitted': True, 'time': self.now().isoformat()}
                self._commit(changed)
            return True
        client = None
        try:
            client = self.client_factory(lambda: self.credentials(login_id), entry_url=self.data['page']['entry_url'])
            with self.lock:
                previous = deepcopy(self.data['accounts'].get(login_id, {}).get('observation', {}))
            result = client.execute(before_submit=before_submit, knowledge=previous) if execute else client.inspect()
            with self.lock:
                changed = deepcopy(self.data)
                record = self._record(changed, login_id)
                previous = record.get('observation', {})
                if previous.get('month') == result.get('month'):
                    for kind in ('direct', 'bonus'):
                        if previous.get(kind+'_state') in ('claimed', 'participated') and result.get(kind+'_state') in ('unknown', 'pending'):
                            for key in ('state', 'message', 'prize', 'receipt'):
                                if kind+'_'+key in previous:
                                    result[kind+'_'+key] = previous[kind+'_'+key]
                        elif (not execute and previous.get(kind+'_receipt') and not result.get(kind+'_receipt')
                                and previous.get(kind+'_state') == result.get(kind+'_state')):
                            for key in ('receipt', 'prize', 'message'):
                                if not result.get(kind+'_'+key) and kind+'_'+key in previous:
                                    result[kind+'_'+key] = deepcopy(previous[kind+'_'+key])
                    if previous.get('date') == result.get('date') and previous.get('punched_today') is True and result.get('punched_today') is None:
                        result['punched_today'] = True
                record['observation'] = result
                record['message'] = result.get('message') or '官方状态已更新'
                record.pop('query_error', None)
                record.pop('query_state', None)
                progress = result.get('task_progress')
                tasks = result.get('tasks', [])
                daily_done = (result.get('direct_state') in ('claimed', 'participated')
                    and len(tasks) == 3 and all(task.get('done') for task in tasks)
                    and (result.get('punched_today') is True or (result.get('punch_days') or 0) >= 3)
                    and (result.get('bonus_state') in ('claimed', 'participated') or progress is not None and progress < 100))
                if execute or daily_done:
                    record.pop('retry_at', None)
                    record.pop('error_code', None)
                if daily_done:
                    record['completed_date'] = self.now().strftime('%Y%m%d')
                    if record['auto_enabled']:
                        record['next_run_at'] = self._next(record['clock'])
                    if result.get('bonus_state') != 'claimed' and (result.get('punch_days') or 0) < 3:
                        record['message'] = f"今日任务已完成，累计打卡 {result['punch_days']}/3 天；满三天后自动领取额外话费券"
                elif execute:
                    record.pop('completed_date', None)
                if execute:
                    record['history'] = [*record['history'], {'time': self.now().isoformat(), 'message': record['message']}][-90:]
                # Old periods cannot affect a new month. Keep a year of audit markers.
                cutoff = (self.now() - timedelta(days=370)).strftime('%Y%m')
                changed['actions'] = {k: v for k, v in changed['actions'].items() if k.rsplit(':', 1)[-1][:6] >= cutoff}
                self._commit(changed)
        except (MonthlyCouponAuthError, MonthlyTaskError, LoginStorageError) as error:
            with self.lock:
                changed = deepcopy(self.data)
                record = self._record(changed, login_id)
                partial = getattr(client, 'observation', None)
                if partial is not None:
                    record['observation'] = deepcopy(partial)
                record.update(query_error=str(error), message=str(error))
                code = getattr(error, 'code', '')
                record['error_code'] = code
                record['query_state'] = 'login_required' if getattr(error, 'status', None) == 401 else 'failed'
                if code == 'NOT_ELIGIBLE':
                    record['observation']['eligible'] = False
                if code in ('NOT_ELIGIBLE', 'ACTIVITY_CLOSED'):
                    hour, minute = map(int, record['clock'].split(':'))
                    record['retry_at'] = (self.now()+timedelta(days=1)).replace(
                        hour=hour, minute=minute, second=0, microsecond=0).isoformat()
                action, period = getattr(error, 'rejected_action', None), getattr(error, 'rejected_period', None)
                if action and period and not getattr(error, 'uncertain', False):
                    changed['actions'].pop(f'{login_id}:{action}:{period}', None)
                if execute:
                    record['history'] = [*record['history'], {'time':self.now().isoformat(),
                        'message':str(error), 'code':code}][-90:]
                self._commit(changed)
        finally:
            with self.lock:
                record = self.data['accounts'][login_id]
                if execute:
                    changed = deepcopy(self.data)
                    record = changed['accounts'][login_id]
                    record['last_finished_at'] = self.now().isoformat()
                    if record['auto_enabled'] and record.get('completed_date') != self.now().strftime('%Y%m%d'):
                        record['next_run_at'] = self._retry_time(record)
                    self._commit(changed)
                self.running.discard(login_id)
                self.executing.discard(login_id)
                self.checked[login_id] = self.now()
                pending = login_id in self.execute_after_refresh
                self.execute_after_refresh.discard(login_id)
            if pending:
                self._queue(login_id, execute=True, manual=True)

    def status(self):
        accounts, now = self.accounts(), self.now()
        self._ensure_accounts(accounts)
        with self.lock:
            for account in accounts:
                login_id = account['id']
                record = self.data['accounts'].get(login_id, {})
                observed = record.get('observation', {})
                current = observed.get('month') == now.strftime('%Y%m')
                attempts = record.get('attempt_count', 0) if record.get('attempt_date') == now.strftime('%Y%m%d') else 0
                completed = record.get('completed_date') == now.strftime('%Y%m%d')
                account.update(month=now.strftime('%Y%m'), eligible=observed.get('eligible'),
                    direct_state=observed.get('direct_state', 'unknown') if current else 'unknown',
                    bonus_state=observed.get('bonus_state', 'unknown') if current else 'unknown',
                    direct_message=observed.get('direct_message', '') if current else '',
                    bonus_message=observed.get('bonus_message', '') if current else '',
                    direct_prize=observed.get('direct_prize', '') if current else '',
                    bonus_prize=observed.get('bonus_prize', '') if current else '',
                    direct_receipt=deepcopy(observed.get('direct_receipt', {})) if current else {},
                    bonus_receipt=deepcopy(observed.get('bonus_receipt', {})) if current else {},
                    task_progress=observed.get('task_progress') if current else None,
                    tasks=observed.get('tasks', []) if current else [],
                    punch_days=observed.get('punch_days') if current else None,
                    punched_today=observed.get('punched_today') if observed.get('date') == now.strftime('%Y%m%d') else None,
                    auto_enabled=record.get('auto_enabled', False), next_run_at=record.get('next_run_at'),
                    daily_completed=completed, retry_count=0 if completed else max(0, attempts-1),
                    max_retries=self.data['automation']['max_retries'],
                    retry_exhausted=attempts >= self.data['automation']['max_retries']+1 and not completed,
                    last_run_at=record.get('last_run_at'), message=record.get('query_error') or record.get('message', '尚未查询'))
                if record.get('query_error'):
                    for key in ('direct_state', 'bonus_state'):
                        if account[key] == 'unknown':
                            account[key] = record.get('query_state', 'failed')
                if login_id in self.running:
                    account['message'] = '正在读取或执行，请稍候'
                checked = self.checked.get(login_id)
                if account['status'] == 'active' and (checked is None or (now-checked).total_seconds() >= 300):
                    self._queue(login_id, execute=False)
            return {'accounts': accounts, 'automation': deepcopy(self.data['automation']), 'page': self.page(),
                    'server_time': now.isoformat(), 'warning': self.warning}

    def tick(self):
        accounts, now = self.accounts(), self.now()
        self._ensure_accounts(accounts)
        with self.lock:
            if self.load_error:
                return
            records = deepcopy(self.data['accounts'])
        for account in accounts:
            login_id = account['id']
            if account['status'] != 'active':
                continue
            record = records[login_id]
            hour, minute = map(int, record['clock'].split(':'))
            if not record['auto_enabled'] or now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
                continue
            if record.get('completed_date') == now.strftime('%Y%m%d'):
                continue
            self._queue(login_id, execute=True)

    def check_startup(self):
        self.tick()
        with self.lock:
            active = {a['id'] for a in self.accounts() if a['status'] == 'active'}
            enabled = [(i, r['clock']) for i, r in self.data['accounts'].items() if r['auto_enabled'] and i in active]
        for login_id, clock in enabled:
            hour, minute = map(int, clock.split(':'))
            due = self.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
            if self.now() < due:
                self._queue(login_id, execute=False)

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.running

    def forget(self, login_id):
        with self.lock:
            if login_id in self.running:
                raise MonthlyTaskError('该账号正在读取或执行每月话费券任务，请完成后退出。', 409)
            changed = deepcopy(self.data)
            if login_id in changed['accounts']:
                changed['accounts'][login_id].update(auto_enabled=False, next_run_at=None)
                self._commit(changed)

    def start(self):
        if self.thread is not None:
            return
        def loop():
            self.check_startup()
            while not self.quit.wait(1):
                try:
                    self.tick()
                except (MonthlyTaskError, LoginStorageError) as error:
                    self.warning = str(error)
        self.thread = Thread(target=loop, daemon=True, name='monthly-coupon-daily')
        self.thread.start()

    def shutdown(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)
