"""Persisted daily sign-in and noon coupon jobs using the shared APP pool."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import re
import secrets
from threading import Event, RLock, Thread

from checkin_client import CheckinClient, CheckinError, FLASH_PRODUCT
from login_storage import LoginStorageError
from recharge.cmcc import SHANGHAI


class CheckinTaskError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class CheckinScheduler:
    def __init__(self, credentials, accounts, *, storage=None, client_factory=CheckinClient,
                 now=None, executor=None):
        self.credentials, self.accounts, self.storage = credentials, accounts, storage
        self.client_factory = client_factory
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.quit = RLock(), Event()
        self.executor = executor or ThreadPoolExecutor(max_workers=32, thread_name_prefix='checkin')
        self.thread = None
        self.account_locks, self.refreshing, self.checked = {}, set(), {}
        self.warning, self.load_error = '', ''
        self.data = {'tasks': {}, 'observations': {}, 'daily': {}}
        if storage is not None:
            try:
                saved = storage.load_checkin_data()
                if saved is not None:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(k), dict) for k in self.data):
                        raise LoginStorageError('签到数据格式无效，已保留原数据库。')
                    self.data = saved
                # A crash after a request may leave an unknown upstream result.
                # Keep its submitted marker and query later; never send again.
                recovered = deepcopy(self.data)
                for record in recovered['daily'].values():
                    if record['state'] in ('preparing', 'running'):
                        record.update(state='interrupted', message='上次执行中断，结果待核实；未自动重发')
                for task in recovered['tasks'].values():
                    if task['state'] in ('preparing', 'running', 'stopping'):
                        task.update(state='interrupted', message='上次执行中断，结果待核实；未自动重发')
                self._commit(recovered)
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)

    def _commit(self, data):
        if self.load_error:
            raise CheckinTaskError(self.load_error, 503)
        if self.storage is not None:
            self.storage.save_checkin_data(data)
        self.data = data

    def _next(self, clock, after=None):
        after = after or self.now()
        hour, minute = map(int, clock.split(':'))
        target = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return (target if target > after else target + timedelta(days=1)).isoformat()

    def _task(self, data, login_id, kind):
        task = next((t for t in data['tasks'].values() if t['login_id'] == login_id and t['kind'] == kind), None)
        if task is None:
            task = {'id': secrets.token_hex(16), 'login_id': login_id, 'kind': kind,
                    'enabled': False, 'state': 'stopped', 'clock': '08:00' if kind == 'signin' else '12:00',
                    'product_id': FLASH_PRODUCT if kind == 'flash' else '', 'next_run_at': None,
                    'last_run_at': None, 'message': '尚未执行', 'history': []}
            data['tasks'][task['id']] = task
        return task

    def save(self, payload):
        ids = payload.get('login_ids')
        accounts = {a['id']: a for a in self.accounts()}
        if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in accounts for i in ids)):
            raise CheckinTaskError('请选择共享账号池中的账号。')
        signin, flash = payload.get('signin_enabled'), payload.get('flash_enabled')
        clock, product = payload.get('signin_time', '08:00'), payload.get('product_id', '')
        if not isinstance(signin, bool) or not isinstance(flash, bool) or not (signin or flash):
            raise CheckinTaskError('请至少启用一项自动任务。')
        if not isinstance(clock, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', clock):
            raise CheckinTaskError('请输入正确的签到时间。')
        if flash and product != FLASH_PRODUCT:
            raise CheckinTaskError('请选择月末2元话费券。')
        with self.lock:
            if any(t['login_id'] in ids and t['state'] in ('preparing', 'running', 'stopping') for t in self.data['tasks'].values()):
                raise CheckinTaskError('所选账号正在执行，请结束后调整。', 409)
            updated = deepcopy(self.data)
            for login_id in dict.fromkeys(ids):
                if accounts[login_id]['status'] != 'active':
                    raise CheckinTaskError('所选账号尚未就绪，请先校验并重连。', 409)
                for kind, enabled, time in [('signin', signin, clock), ('flash', flash, '12:00')]:
                    task = self._task(updated, login_id, kind)
                    task.update(enabled=enabled, clock=time, state='waiting' if enabled else 'stopped',
                                cancel_requested=False, next_run_at=self._next(time) if enabled else None)
            self._commit(updated)
        return '自动任务已保存，每个账号每天执行一次'

    def stop(self, ids):
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
            raise CheckinTaskError('请选择需要停止的任务。')
        with self.lock:
            if any(i not in self.data['tasks'] for i in ids):
                raise CheckinTaskError('任务不存在，请刷新。', 404)
            updated = deepcopy(self.data)
            for task_id in set(ids):
                task = updated['tasks'][task_id]
                running = task['state'] in ('preparing', 'running', 'stopping')
                task.update(enabled=False, next_run_at=None, cancel_requested=True,
                            state='stopping' if running else 'stopped')
            self._commit(updated)

    def run(self, ids):
        accounts = {a['id']: a for a in self.accounts()}
        if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in accounts for i in ids):
            raise CheckinTaskError('请选择共享账号池中的账号。')
        if any(accounts[i]['status'] != 'active' for i in ids):
            raise CheckinTaskError('所选账号需要校验或重新登录。', 409)
        accepted = sum(self._enqueue(i, 'signin') for i in dict.fromkeys(ids))
        return f'已安排 {accepted} 个账号签到；今日已完成或正在执行的账号不会重复提交'

    def _enqueue(self, login_id, kind):
        now = self.now()
        key = f'{login_id}:{kind}:{now:%Y%m%d}'
        with self.lock:
            old = self.data['daily'].get(key)
            if old and (old.get('submitted') or old['state'] not in ('failed', 'login_required', 'interrupted')):
                return False
            updated = deepcopy(self.data)
            task = self._task(updated, login_id, kind)
            if task['state'] in ('preparing', 'running', 'stopping'):
                return False
            task.update(state='preparing', cancel_requested=False, last_run_at=now.isoformat(), message='正在核对官方状态')
            if task['enabled']:
                task['next_run_at'] = self._next(task['clock'])
            updated['daily'][key] = {'time': now.isoformat(), 'state': 'preparing', 'submitted': False, 'message': ''}
            # Retain a year of per-day deduplication records; histories are bounded separately.
            cutoff = (now - timedelta(days=370)).strftime('%Y%m%d')
            updated['daily'] = {k: v for k, v in updated['daily'].items() if k.rsplit(':', 1)[-1] >= cutoff}
            self._commit(updated)
            account_lock = self.account_locks.setdefault(login_id, RLock())
            task_id = task['id']
        self.executor.submit(self._perform, login_id, kind, task_id, key, account_lock)
        return True

    def _perform(self, login_id, kind, task_id, key, account_lock):
        submitted = False
        def before_submit():
            nonlocal submitted
            with self.lock:
                task = self.data['tasks'][task_id]
                if task.get('cancel_requested'):
                    raise CheckinTaskError('任务已停止，本次没有提交。', 409)
                updated = deepcopy(self.data)
                updated['daily'][key].update(state='running', submitted=True)
                updated['tasks'][task_id]['state'] = 'running'
                self._commit(updated)
                submitted = True
        try:
            with account_lock:
                client = self.client_factory(lambda: self.credentials(login_id))
                result = client.signin(before_submit=before_submit) if kind == 'signin' else client.flash(FLASH_PRODUCT, before_submit=before_submit)
        except (CheckinError, CheckinTaskError, LoginStorageError) as error:
            result = {'state': 'login_required' if getattr(error, 'status', None) == 401 else 'failed', 'message': str(error)}
            if submitted:
                result['message'] += '；本日不再自动提交，请查看官方结果'
        with self.lock:
            updated = deepcopy(self.data)
            task = updated['tasks'][task_id]
            state, message = result['state'], result['message']
            record = {'time': self.now().isoformat(), 'state': state, 'message': message, 'submitted': submitted}
            updated['daily'][key] = record
            task.update(state=state, message=message)
            task['history'] = [*task['history'], record][-120:]
            observation = updated['observations'].setdefault(login_id, {})
            observation.update({k: v for k, v in result.items() if k not in ('state', 'message', 'submitted')})
            if 'today_state' in result:
                observation.pop('query_message', None)
            if kind == 'signin':
                observation.update(last_signin_at=record['time'], signin_message=message)
            else:
                observation.update(flash_message=message)
            self._commit(updated)
            self.checked[login_id] = self.now()

    def tick(self):
        with self.lock:
            if self.load_error:
                return
            due = [deepcopy(t) for t in self.data['tasks'].values() if t['enabled'] and t['next_run_at']
                   and datetime.fromisoformat(t['next_run_at']) <= self.now()
                   and t['state'] not in ('preparing', 'running', 'stopping')]
        for task in due:
            due_time = datetime.fromisoformat(task['next_run_at'])
            if task['kind'] == 'flash' and (self.now() - due_time).total_seconds() > 120:
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['tasks'][task['id']].update(next_run_at=self._next('12:00'), state='skipped', message='本地服务错过今日12点，已安排下次执行')
                    self._commit(updated)
            elif not self._enqueue(task['login_id'], task['kind']):
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['tasks'][task['id']]['next_run_at'] = self._next(task['clock'])
                    self._commit(updated)

    def _refresh(self, login_id, account_lock):
        try:
            with account_lock:
                result = self.client_factory(lambda: self.credentials(login_id)).inspect()
            with self.lock:
                updated = deepcopy(self.data)
                updated['observations'].setdefault(login_id, {}).update(result)
                updated['observations'][login_id].pop('query_message', None)
                self._commit(updated)
        except (CheckinError, LoginStorageError, CheckinTaskError) as error:
            with self.lock:
                updated = deepcopy(self.data)
                observation = updated['observations'].setdefault(login_id, {})
                observation['query_message'] = str(error)
                self._commit(updated)
        finally:
            with self.lock:
                self.checked[login_id] = self.now()
                self.refreshing.discard(login_id)

    def status(self):
        accounts, now = self.accounts(), self.now()
        with self.lock:
            tasks = deepcopy(list(self.data['tasks'].values()))
            observations = deepcopy(self.data['observations'])
            products = {}
            for account in accounts:
                login_id = account['id']
                observed = observations.get(login_id, {})
                current = observed.get('date') == now.strftime('%Y%m%d')
                account.update(today_state=observed.get('today_state', 'unknown') if current else 'unknown',
                    month_count=observed.get('month_count') if observed.get('date', '')[:6] == now.strftime('%Y%m') else None,
                    last_signin_at=observed.get('last_signin_at'),
                    signin_message=observed.get('query_message') or observed.get('signin_message', '尚未查询'),
                    flash_state=observed.get('flash_state', 'unknown') if current else 'unknown',
                    flash_message=observed.get('flash_message', '尚未查询'),
                    signin_enabled=any(t['login_id'] == login_id and t['kind'] == 'signin' and t['enabled'] for t in tasks),
                    flash_enabled=any(t['login_id'] == login_id and t['kind'] == 'flash' and t['enabled'] for t in tasks))
                for product in observed.get('products', []):
                    products[product['id']] = product
                checked = self.checked.get(login_id)
                if (account['status'] == 'active' and login_id not in self.refreshing and not self.load_error
                        and (checked is None or (now - checked).total_seconds() >= 300)):
                    self.refreshing.add(login_id)
                    self.executor.submit(self._refresh, login_id, self.account_locks.setdefault(login_id, RLock()))
            account_map = {a['id']: a for a in accounts}
            for task in tasks:
                info = account_map.get(task['login_id'], {})
                task.update(account_name=info.get('name') or info.get('phone_masked', '已退出账号'), phone_masked=info.get('phone_masked', ''))
            return {'accounts': accounts, 'tasks': tasks, 'products': list(products.values()),
                    'server_time': now.isoformat(), 'warning': self.warning}

    def busy_login(self, login_id):
        with self.lock:
            return any(t['login_id'] == login_id and t['state'] in ('preparing', 'running', 'stopping') for t in self.data['tasks'].values())

    def forget(self, login_id):
        with self.lock:
            if self.busy_login(login_id):
                raise CheckinTaskError('该账号正在签到或领取，请完成后退出。', 409)
            updated = deepcopy(self.data)
            for task in updated['tasks'].values():
                if task['login_id'] == login_id:
                    task.update(enabled=False, next_run_at=None, cancel_requested=True, state='stopped')
            self._commit(updated)

    def start(self):
        if self.thread is not None:
            return
        def loop():
            self.check_startup()
            while not self.quit.wait(0.5):
                try:
                    self.tick()
                except (LoginStorageError, CheckinTaskError) as error:
                    self.warning = str(error)
        self.thread = Thread(target=loop, daemon=True, name='checkin-daily')
        self.thread.start()

    def check_startup(self):
        """Check enabled accounts even if their 08:00 launch was missed."""
        with self.lock:
            if self.load_error:
                return
            tasks = [deepcopy(t) for t in self.data['tasks'].values() if t['enabled'] and t['kind'] == 'signin']
        for task in tasks:
            now = self.now()
            hour, minute = map(int, task['clock'].split(':'))
            today_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= today_start and self._enqueue(task['login_id'], 'signin'):
                continue
            with self.lock:
                login_id = task['login_id']
                if login_id not in self.refreshing:
                    self.refreshing.add(login_id)
                    self.executor.submit(self._refresh, login_id, self.account_locks.setdefault(login_id, RLock()))

    def shutdown(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)
