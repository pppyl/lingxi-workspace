"""Persisted daily sign-in and noon coupon jobs using the shared APP pool."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta
import math
import re
import secrets
from threading import Event, RLock, Thread

from checkin_client import CheckinClient, CheckinError, FLASH_PRODUCT
from checkin_flash import BURST_DEFAULTS, PREPARE_SECONDS
from login_storage import LoginStorageError
from recharge.cmcc import SHANGHAI


class CheckinTaskError(CheckinError):
    def __init__(self, message, status=400):
        super().__init__(message, 'TASK_ERROR', status)


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
        self.readiness = {}
        self.warning, self.load_error = '', ''
        self.data = {'tasks': {}, 'observations': {}, 'daily': {},
                     'settings': {'burst_workers': BURST_DEFAULTS['burst_workers']}}
        if storage is not None:
            try:
                saved = storage.load_checkin_data()
                if saved is not None:
                    if not isinstance(saved, dict) or any(not isinstance(saved.get(k), dict) for k in ('tasks', 'observations', 'daily')):
                        raise LoginStorageError('签到数据格式无效，已保留原数据库。')
                    settings = saved.get('settings', {'burst_workers': BURST_DEFAULTS['burst_workers']})
                    if (not isinstance(settings, dict) or isinstance(settings.get('burst_workers'), bool)
                            or not isinstance(settings.get('burst_workers'), int) or not 1 <= settings['burst_workers'] <= 32):
                        raise LoginStorageError('签到秒杀设置无效，已保留原数据库。')
                    saved['settings'] = settings
                    self.data = saved
                # A crash after a request may leave an unknown upstream result.
                # Keep its submitted marker and query later; never send again.
                recovered = deepcopy(self.data)
                known = {account['id'] for account in self.accounts()}
                recovered['tasks'] = {key: task for key, task in recovered['tasks'].items() if task['login_id'] in known}
                recovered['observations'] = {key: value for key, value in recovered['observations'].items() if key in known}
                recovered['daily'] = {key: value for key, value in recovered['daily'].items() if key.split(':', 1)[0] in known}
                for record in recovered['daily'].values():
                    if record['state'] in ('preparing', 'running'):
                        record.update(state='interrupted', message='上次执行中断，结果待核实；未自动重发')
                for task in recovered['tasks'].values():
                    if task['kind'] == 'flash':
                        for key, value in BURST_DEFAULTS.items():
                            task.setdefault(key, value)
                    if task['state'] in ('preparing', 'running', 'stopping'):
                        task.update(state='interrupted', message='上次执行中断，结果待核实；未自动重发')
                        scheduled = task.get('round_scheduled_at')
                        daily = recovered['daily'].get(f"{task['login_id']}:flash:{self.now():%Y%m%d}", {})
                        if (task['kind'] == 'flash' and task['enabled'] and scheduled
                                and scheduled[:10] == self.now().date().isoformat() and not daily.get('submitted')):
                            task.update(state='waiting', next_run_at=scheduled, message='上次预热中断且未提交，等待本场执行')
                self._commit(recovered)
            except LoginStorageError as error:
                self.warning = self.load_error = str(error)
        clocks = [t['clock'] for t in self.data['tasks'].values() if t['kind'] == 'signin' and t['enabled']]
        flash = next((t for t in self.data['tasks'].values() if t['kind'] == 'flash' and t['enabled']), {})
        self.data.setdefault('automation', {'signin_enabled': True, 'signin_time': max(clocks, key=clocks.count) if clocks else '08:00',
            'flash_enabled': True, 'product_id': FLASH_PRODUCT,
            **{key: flash.get(key, self.data['settings'].get(key, value)) for key, value in BURST_DEFAULTS.items()}})
        self.data['automation']['burst_workers'] = self.data['settings']['burst_workers']

    def _ensure_accounts(self, accounts):
        with self.lock:
            if self.load_error:
                return
            existing = {(t['login_id'], t['kind']) for t in self.data['tasks'].values()}
            missing = [(a['id'], kind) for a in accounts if a['status'] == 'active'
                       for kind in ('signin', 'flash') if (a['id'], kind) not in existing]
            if not missing:
                return
            updated = deepcopy(self.data)
            for login_id, kind in missing:
                self._task(updated, login_id, kind)
            self._commit(updated)

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

    def _retry_at(self, clock):
        now = self.now()
        hour, minute = map(int, clock.split(':'))
        at = max(now + timedelta(minutes=5), now.replace(hour=hour, minute=minute, second=0, microsecond=0))
        return at.isoformat() if at.date() == now.date() else self._next(clock)

    def update_settings(self, payload):
        workers, apply_existing = payload.get('burst_workers'), payload.get('apply_existing', True)
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 32:
            raise CheckinTaskError('单账号并发数请输入 1–32 的整数。')
        if not isinstance(apply_existing, bool):
            raise CheckinTaskError('请选择是否同步到已有秒杀任务。')
        with self.lock:
            updated = deepcopy(self.data)
            updated['settings']['burst_workers'] = workers
            updated['automation']['burst_workers'] = workers
            affected = 0
            if apply_existing:
                for task in updated['tasks'].values():
                    if task['kind'] == 'flash':
                        task['burst_workers'] = workers
                        affected += 1
            self._commit(updated)
            return {'settings': deepcopy(updated['settings']), 'affected_tasks': affected,
                    'message': f'单账号默认并发已设为 {workers}'+(f'，已同步 {affected} 个秒杀任务' if apply_existing else '')+'；正在执行的本轮保持原设置'}

    def _task(self, data, login_id, kind):
        task = next((t for t in data['tasks'].values() if t['login_id'] == login_id and t['kind'] == kind), None)
        if task is None:
            config = data['automation']
            clock = config['signin_time'] if kind == 'signin' else '12:00'
            enabled = config[kind+'_enabled']
            task = {'id': secrets.token_hex(16), 'login_id': login_id, 'kind': kind,
                    'enabled': enabled, 'state': 'waiting' if enabled else 'stopped', 'clock': clock,
                    'product_id': FLASH_PRODUCT if kind == 'flash' else '', 'next_run_at': self._next(clock) if enabled else None,
                    'last_run_at': None, 'message': '尚未执行', 'history': []}
            if kind == 'flash':
                task.update({key: config.get(key, value) for key, value in BURST_DEFAULTS.items()})
            data['tasks'][task['id']] = task
        return task

    def save(self, payload):
        ids = payload.get('login_ids')
        accounts = {a['id']: a for a in self.accounts()}
        global_settings = ids is None
        if global_settings:
            ids = list(accounts)
        elif (not isinstance(ids, list) or not ids or any(not isinstance(i, str) or i not in accounts for i in ids)):
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
            defaults = {key: self.data['automation'].get(key, value) for key, value in BURST_DEFAULTS.items()}
        burst = {key: payload.get(key, value) for key, value in defaults.items()}
        if (flash or global_settings) and (isinstance(burst['burst_count'], bool) or not isinstance(burst['burst_count'], int)
                or not 1 <= burst['burst_count'] <= 100
                or isinstance(burst['burst_workers'], bool) or not isinstance(burst['burst_workers'], int)
                or not 1 <= burst['burst_workers'] <= 32
                or isinstance(burst['burst_interval'], bool) or not isinstance(burst['burst_interval'], (int, float))
                or not math.isfinite(burst['burst_interval']) or not 0.01 <= burst['burst_interval'] <= 5):
            raise CheckinTaskError('秒杀次数为1–100，间隔为0.01–5秒，并发线程为1–32。')
        with self.lock:
            if any(t['login_id'] in ids and t['state'] in ('preparing', 'running', 'stopping') for t in self.data['tasks'].values()):
                raise CheckinTaskError('所选账号正在执行，请结束后调整。', 409)
            updated = deepcopy(self.data)
            if global_settings:
                updated['automation'].update(signin_enabled=signin, signin_time=clock, flash_enabled=flash,
                    product_id=FLASH_PRODUCT, **burst)
                updated['settings']['burst_workers'] = burst['burst_workers']
            for login_id in dict.fromkeys(ids):
                if not global_settings and accounts[login_id]['status'] != 'active':
                    raise CheckinTaskError('所选账号尚未就绪，请先校验并重连。', 409)
                for kind, enabled, time in [('signin', signin, clock), ('flash', flash, '12:00')]:
                    task = self._task(updated, login_id, kind)
                    task.update(enabled=enabled, clock=time, state='waiting' if enabled else 'stopped',
                                cancel_requested=False, next_run_at=self._next(time) if enabled else None)
                    if kind == 'flash':
                        task.update(burst)
            self._commit(updated)
        return '自动任务已保存，默认应用于所有有效账号，新增或恢复登录后自动纳入' if global_settings else '自动任务已保存，每个账号每天执行一次'

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
        accepted = sum(self._enqueue(i, 'signin', retry=True,
            revision=accounts[i].get('credential_revision', 0)) for i in dict.fromkeys(ids))
        return f'已安排 {accepted} 个账号签到；今日已完成或正在执行的账号不会重复提交'

    def _enqueue(self, login_id, kind, *, scheduled_at=None, retry=False, revision=0):
        now = self.now()
        key = f'{login_id}:{kind}:{now:%Y%m%d}'
        with self.lock:
            if scheduled_at is not None and not any(t['login_id'] == login_id and t['kind'] == kind and t['enabled']
                                                    for t in self.data['tasks'].values()):
                return False
            old = self.data['daily'].get(key)
            observed = self.data['observations'].get(login_id, {})
            if kind == 'signin' and observed.get('date') == now.strftime('%Y%m%d') and observed.get('today_state') == 'signed':
                return False
            uncertain = old and old.get('uncertain', bool(old.get('submitted')) and 'GWT_LIMITED' not in old.get('message', ''))
            if old and ((old.get('submitted') and not (kind == 'signin' and retry and not uncertain))
                        or old['state'] not in ('failed', 'login_required', 'interrupted')):
                return False
            updated = deepcopy(self.data)
            task = self._task(updated, login_id, kind)
            if task['state'] in ('preparing', 'running', 'stopping'):
                return False
            task.update(state='preparing', cancel_requested=False, last_run_at=now.isoformat(),
                        round_scheduled_at=scheduled_at.isoformat() if scheduled_at is not None else None,
                        message='正在预热秒杀会话，等待官方开放时间' if kind == 'flash' else '正在核对官方状态')
            if task['enabled']:
                task['next_run_at'] = self._next(task['clock'], after=max(now, scheduled_at or now))
            burst = {key: task.get(key, value) for key, value in BURST_DEFAULTS.items()}
            updated['daily'][key] = {'time': now.isoformat(), 'state': 'preparing', 'submitted': False,
                'message': '', 'credential_revision': revision}
            # Retain a year of per-day deduplication records; histories are bounded separately.
            cutoff = (now - timedelta(days=370)).strftime('%Y%m%d')
            updated['daily'] = {k: v for k, v in updated['daily'].items() if k.rsplit(':', 1)[-1] >= cutoff}
            self._commit(updated)
            account_lock = self.account_locks.setdefault(login_id, RLock())
            task_id = task['id']
        self.executor.submit(self._perform, login_id, kind, task_id, key, account_lock, scheduled_at, burst)
        return True

    def _perform(self, login_id, kind, task_id, key, account_lock, scheduled_at, burst):
        submitted = False
        def before_submit():
            nonlocal submitted
            with self.lock:
                task = self.data['tasks'][task_id]
                if self.quit.is_set() or task.get('cancel_requested'):
                    raise CheckinTaskError('任务已停止，本次没有提交。', 409)
                if not submitted:
                    updated = deepcopy(self.data)
                    updated['daily'][key].update(state='running', submitted=True)
                    updated['tasks'][task_id].update(state='running', message='正在并发尝试秒杀' if kind == 'flash' else '正在签到')
                    self._commit(updated)
                    submitted = True
        def cancelled():
            with self.lock:
                return self.quit.is_set() or self.data['tasks'][task_id].get('cancel_requested', False)
        try:
            with account_lock:
                client = self.client_factory(lambda: self.credentials(login_id))
                result = (client.signin(before_submit=before_submit) if kind == 'signin' else
                          client.flash(FLASH_PRODUCT, before_submit=before_submit, burst=burst,
                                       cancelled=cancelled, target_at=scheduled_at))
        except (CheckinError, LoginStorageError) as error:
            result = {'state': 'login_required' if getattr(error, 'status', None) == 401 else 'failed',
                'message': str(error), 'uncertain': getattr(error, 'uncertain', False)}
            if submitted:
                result['message'] += ('；将继续核对官方状态，未确认的请求不会重复提交' if result['uncertain']
                    else '；恢复登录后或稍后重新核对' if kind == 'signin' else '；本日不再自动提交，请查看官方结果')
        with self.lock:
            updated = deepcopy(self.data)
            task = updated['tasks'][task_id]
            state, message = result['state'], result['message']
            record = {'time': self.now().isoformat(), 'state': state, 'message': message, 'submitted': submitted,
                'uncertain': result.get('uncertain', submitted and state == 'failed'),
                'credential_revision': updated['daily'][key].get('credential_revision', 0)}
            if kind == 'flash':
                record.update(attempt_count=result.get('attempt_count', 0), attempts=result.get('attempts', []))
            updated['daily'][key] = record
            task.update(state=state, message=message)
            if kind == 'signin' and task['enabled'] and state in ('failed', 'login_required', 'interrupted'):
                task['next_run_at'] = self._retry_at(task['clock'])
            task['history'] = [*task['history'], record][-120:]
            observation = updated['observations'].setdefault(login_id, {})
            observation.update({k: v for k, v in result.items() if k not in ('state', 'message', 'submitted', 'attempt_count', 'attempts')})
            if 'today_state' in result:
                observation.pop('query_message', None)
            if kind == 'signin':
                observation.update(last_signin_at=record['time'], signin_message=message)
            else:
                observation.update(flash_message=message)
            self._commit(updated)
            self.checked[login_id] = self.now()

    def tick(self):
        accounts, now = self.accounts(), self.now()
        self._ensure_accounts(accounts)
        active = {a['id']: a for a in accounts if a['status'] == 'active'}
        previous, self.readiness = self.readiness, {a['id']: (a['status'], a.get('credential_revision', 0)) for a in accounts}
        with self.lock:
            if self.load_error:
                return
            signins = [deepcopy(t) for t in self.data['tasks'].values() if t['enabled'] and t['kind'] == 'signin'
                       and t['login_id'] in active and t['state'] not in ('preparing', 'running', 'stopping')]
            due = [deepcopy(t) for t in self.data['tasks'].values() if t['enabled'] and t['next_run_at']
                   and t['kind'] == 'flash' and t['login_id'] in active
                   and datetime.fromisoformat(t['next_run_at']) <= self.now()+timedelta(seconds=PREPARE_SECONDS if t['kind']=='flash' else 0)
                   and t['state'] not in ('preparing', 'running', 'stopping')]
        for task in signins:
            login_id = task['login_id']
            hour, minute = map(int, task['clock'].split(':'))
            today_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now < today_start:
                continue
            with self.lock:
                old = deepcopy(self.data['daily'].get(f'{login_id}:signin:{now:%Y%m%d}'))
                observed = self.data['observations'].get(login_id, {})
                if observed.get('date') == now.strftime('%Y%m%d') and observed.get('today_state') == 'signed':
                    continue
            recovered = previous.get(login_id) != self.readiness[login_id]
            revision = active[login_id].get('credential_revision', 0)
            changed_credentials = old and old.get('credential_revision', revision) != revision
            if old and not recovered and not changed_credentials and (now-datetime.fromisoformat(old['time'])).total_seconds() < 300:
                continue
            if not self._enqueue(login_id, 'signin', scheduled_at=today_start, retry=True,
                    revision=revision):
                with self.lock:
                    checked = self.checked.get(login_id)
                    if login_id not in self.refreshing and (recovered or checked is None or (now-checked).total_seconds() >= 300):
                        self.refreshing.add(login_id)
                        self.executor.submit(self._refresh, login_id, self.account_locks.setdefault(login_id, RLock()))
        for task in due:
            due_time = datetime.fromisoformat(task['next_run_at'])
            if task['kind'] == 'flash' and (self.now() - due_time).total_seconds() > 120:
                with self.lock:
                    updated = deepcopy(self.data)
                    updated['tasks'][task['id']].update(next_run_at=self._next('12:00'), state='skipped', message='本地服务错过今日12点，已安排下次执行')
                    self._commit(updated)
            elif not self._enqueue(task['login_id'], task['kind'], scheduled_at=due_time):
                with self.lock:
                    if task['id'] not in self.data['tasks']:
                        continue
                    updated = deepcopy(self.data)
                    updated['tasks'][task['id']]['next_run_at'] = self._next(task['clock'])
                    self._commit(updated)

    def _refresh(self, login_id, account_lock):
        try:
            with self.lock:
                if self.account_locks.get(login_id) is not account_lock:
                    return
            with account_lock:
                result = self.client_factory(lambda: self.credentials(login_id)).inspect()
            with self.lock:
                if self.account_locks.get(login_id) is not account_lock:
                    return
                updated = deepcopy(self.data)
                updated['observations'].setdefault(login_id, {}).update(result)
                updated['observations'][login_id].pop('query_message', None)
                for task in updated['tasks'].values():
                    if task['login_id'] != login_id or task['kind'] != 'signin' or not task['enabled']:
                        continue
                    if task['state'] in ('preparing', 'running', 'stopping'):
                        continue
                    if result.get('date') == self.now().strftime('%Y%m%d') and result.get('today_state') == 'signed':
                        task.update(state='already_signed', message='今日已签到，已核对官方记录', next_run_at=self._next(task['clock']))
                    elif task['state'] in ('failed', 'login_required', 'interrupted'):
                        task['next_run_at'] = self._retry_at(task['clock'])
                self._commit(updated)
        except (CheckinError, LoginStorageError) as error:
            with self.lock:
                if self.account_locks.get(login_id) is not account_lock:
                    return
                updated = deepcopy(self.data)
                observation = updated['observations'].setdefault(login_id, {})
                observation['query_message'] = str(error)
                self._commit(updated)
        finally:
            with self.lock:
                if self.account_locks.get(login_id) is account_lock:
                    self.checked[login_id] = self.now()
                    self.refreshing.discard(login_id)

    def status(self):
        accounts, now = self.accounts(), self.now()
        self._ensure_accounts(accounts)
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
                    signin_message='今日已签到' if current and observed.get('today_state') == 'signed'
                        else observed.get('query_message') or observed.get('signin_message', '尚未查询'),
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
                if (task['kind'] == 'signin' and task['enabled'] and info.get('today_state') == 'signed'
                        and task['state'] not in ('preparing', 'running', 'stopping')):
                    task.update(state='already_signed', message='今日已签到，已核对官方记录')
            return {'accounts': accounts, 'tasks': tasks, 'products': list(products.values()),
                    'settings': deepcopy(self.data['settings']),
                    'automation': deepcopy(self.data['automation']),
                    'server_time': now.isoformat(), 'warning': self.warning}

    def busy_login(self, login_id):
        with self.lock:
            return any(t['login_id'] == login_id and t['state'] in ('preparing', 'running', 'stopping') for t in self.data['tasks'].values())

    def forget(self, login_id):
        with self.lock:
            if self.busy_login(login_id):
                raise CheckinTaskError('该账号正在签到或领取，请完成后退出。', 409)
            updated = deepcopy(self.data)
            updated['tasks'] = {key: task for key, task in updated['tasks'].items() if task['login_id'] != login_id}
            updated['observations'].pop(login_id, None)
            updated['daily'] = {key: value for key, value in updated['daily'].items() if key.split(':', 1)[0] != login_id}
            self._commit(updated)
            self.checked.pop(login_id, None)
            self.refreshing.discard(login_id)
            self.account_locks.pop(login_id, None)

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
        self.tick()
        with self.lock:
            if self.load_error:
                return
            active = {a['id'] for a in self.accounts() if a['status'] == 'active'}
            tasks = [deepcopy(t) for t in self.data['tasks'].values() if t['enabled'] and t['kind'] == 'signin'
                     and t['login_id'] in active and t['state'] not in ('preparing', 'running', 'stopping')]
        for task in tasks:
            now = self.now()
            hour, minute = map(int, task['clock'].split(':'))
            today_start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if now >= today_start and self._enqueue(task['login_id'], 'signin', scheduled_at=today_start):
                continue
            with self.lock:
                if task['id'] not in self.data['tasks']:
                    continue
                login_id = task['login_id']
                if login_id not in self.refreshing:
                    self.refreshing.add(login_id)
                    self.executor.submit(self._refresh, login_id, self.account_locks.setdefault(login_id, RLock()))

    def shutdown(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        self.executor.shutdown(wait=False, cancel_futures=True)
