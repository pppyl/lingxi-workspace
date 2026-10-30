"""Daily reconciliation with separate monthly claims and daily punch markers."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import re
from threading import Event, RLock, Thread

from login_storage import LoginStorageError
from monthly_coupon_auth import MonthlyCouponAuthError
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

    def _commit(self, data):
        if self.load_error:
            raise MonthlyTaskError(self.load_error, 503)
        if self.storage is not None:
            self.storage.save_monthly_coupon_data(data)
        self.data = data

    def _record(self, data, login_id):
        return data['accounts'].setdefault(login_id, {'auto_enabled': False, 'clock': '08:05',
            'next_run_at': None, 'last_run_at': None, 'message': '尚未执行', 'observation': {}, 'history': []})

    def _next(self, clock):
        now = self.now()
        hour, minute = map(int, clock.split(':'))
        at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (at if at > now else at + timedelta(days=1)).isoformat()

    def _selection(self, ids, *, active):
        known = {a['id']: a for a in self.accounts()}
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in known for i in ids):
            raise MonthlyTaskError('请选择共享账号池中的账号。')
        if active and any(known[i]['status'] != 'active' for i in ids):
            raise MonthlyTaskError('所选账号尚未就绪，请先校验或重新登录。', 409)
        return list(dict.fromkeys(ids))

    def settings(self, payload):
        enabled, clock = payload.get('enabled'), payload.get('time', '08:05')
        if not isinstance(enabled, bool) or not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise MonthlyTaskError('请设置自动开关和正确的执行时间。')
        ids = self._selection(payload.get('login_ids'), active=enabled)
        with self.lock:
            changed = deepcopy(self.data)
            for login_id in ids:
                record = self._record(changed, login_id)
                record.update(auto_enabled=enabled, clock=clock, next_run_at=self._next(clock) if enabled else None)
            self._commit(changed)
        return f'已为 {len(ids)} 个账号'+('开启每日检查；两种话费券每月分别领取一次' if enabled else '停用自动执行')

    def run(self, ids):
        selected = self._selection(ids, active=True)
        count = sum(self._queue(i, execute=True) for i in selected)
        return f'已安排 {count} 个账号执行，正在执行的账号不会重复安排'

    def _queue(self, login_id, *, execute):
        with self.lock:
            if self.load_error:
                return False
            if login_id in self.running:
                if execute and login_id not in self.executing:
                    self.execute_after_refresh.add(login_id)
                    return True
                return False
            if execute:
                changed = deepcopy(self.data)
                record = self._record(changed, login_id)
                record.update(last_run_at=self.now().isoformat(), message='正在核对官方领取与任务记录')
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
        try:
            client = self.client_factory(lambda: self.credentials(login_id))
            with self.lock:
                previous = deepcopy(self.data['accounts'].get(login_id, {}).get('observation', {}))
            result = client.execute(before_submit=before_submit, knowledge=previous) if execute else client.inspect()
            with self.lock:
                changed = deepcopy(self.data)
                record = self._record(changed, login_id)
                previous = record.get('observation', {})
                if previous.get('month') == result.get('month'):
                    for kind in ('direct', 'bonus'):
                        if previous.get(kind+'_state') == 'claimed' and result.get(kind+'_state') == 'unknown':
                            for key in ('state', 'message', 'prize'):
                                result[kind+'_'+key] = previous.get(kind+'_'+key, '')
                    if previous.get('date') == result.get('date') and previous.get('punched_today') is True and result.get('punched_today') is None:
                        result['punched_today'] = True
                record['observation'] = result
                record['message'] = result.get('message') or '官方状态已更新'
                record.pop('query_error', None)
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
                record.update(query_error=str(error), message=str(error))
                record['query_state'] = 'login_required' if getattr(error, 'status', None) == 401 else 'failed'
                if getattr(error, 'code', '') == 'NOT_ELIGIBLE':
                    record['observation']['eligible'] = False
                action, period = getattr(error, 'rejected_action', None), getattr(error, 'rejected_period', None)
                if action and period and not getattr(error, 'uncertain', False):
                    changed['actions'].pop(f'{login_id}:{action}:{period}', None)
                self._commit(changed)
        finally:
            with self.lock:
                self.running.discard(login_id)
                self.executing.discard(login_id)
                self.checked[login_id] = self.now()
                pending = login_id in self.execute_after_refresh
                self.execute_after_refresh.discard(login_id)
            if pending:
                self._queue(login_id, execute=True)

    def status(self):
        accounts, now = self.accounts(), self.now()
        with self.lock:
            for account in accounts:
                login_id = account['id']
                record = self.data['accounts'].get(login_id, {})
                observed = record.get('observation', {})
                current = observed.get('month') == now.strftime('%Y%m')
                account.update(month=now.strftime('%Y%m'), eligible=observed.get('eligible'),
                    direct_state=observed.get('direct_state', 'unknown') if current else 'unknown',
                    bonus_state=observed.get('bonus_state', 'unknown') if current else 'unknown',
                    direct_message=observed.get('direct_message', '') if current else '',
                    bonus_message=observed.get('bonus_message', '') if current else '',
                    direct_prize=observed.get('direct_prize', '') if current else '',
                    bonus_prize=observed.get('bonus_prize', '') if current else '',
                    task_progress=observed.get('task_progress') if current else None,
                    tasks=observed.get('tasks', []) if current else [],
                    punch_days=observed.get('punch_days') if current else None,
                    punched_today=observed.get('punched_today') if observed.get('date') == now.strftime('%Y%m%d') else None,
                    auto_enabled=record.get('auto_enabled', False), next_run_at=record.get('next_run_at'),
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
            return {'accounts': accounts, 'server_time': now.isoformat(), 'warning': self.warning}

    def tick(self):
        with self.lock:
            if self.load_error:
                return
            due = [i for i, r in self.data['accounts'].items() if r['auto_enabled'] and r['next_run_at']
                   and datetime.fromisoformat(r['next_run_at']) <= self.now()]
        for login_id in due:
            self._queue(login_id, execute=True)

    def check_startup(self):
        with self.lock:
            enabled = [(i, r['clock']) for i, r in self.data['accounts'].items() if r['auto_enabled']]
        for login_id, clock in enabled:
            hour, minute = map(int, clock.split(':'))
            due = self.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
            self._queue(login_id, execute=self.now() >= due)

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
