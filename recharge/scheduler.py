"""Server-side scheduled workers: one sequential retry loop per independent account."""
from __future__ import annotations

import copy
import random
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from .accounts import atomic_json
from .cmcc import ActivityError, SHANGHAI, parse_server_time

ACTIVE = {'preparing', 'armed', 'running', 'cancelling'}
LOGIN_REQUIRED_MESSAGE = '共享账号需要恢复登录，恢复后自动加入周三领取。'
LOGIN_RECOVERED_MESSAGE = '共享账号已登录，已自动加入周三领取；每周三 07:55 准备，08:00 开始领取。'
# These responses require a user action; retries must not attempt to get past them.
ACTION_REQUIRED = {'GOTO_LOGIN', 'LOGIN_REQUIRED', 'BROWSER_MISSING', 'BROWSER_CLOSED',
                   '160030', '160019', 'CSRF_REJECTED', 'APP_LOGIN_REQUIRED', 'APP_SSO_REJECTED'}


def retry_policy(data):
    try:
        low, high = float(data.get('interval_min', 0.2)), float(data.get('interval_max', 1))
        count = data.get('max_attempts', 20)
        if (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20
                or not 0.2 <= low <= high <= 1):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ActivityError('重试间隔须在 0.2～1 秒内，次数须为 1～20 的整数', 'INVALID_RETRY_POLICY')
    return {'interval_min': low, 'interval_max': high, 'max_attempts': count}


class ClaimScheduler:
    def __init__(self, manager, storage=None, *, now=None, rng=None):
        self.manager = manager
        self.storage = Path(storage) if storage else None
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.rng = rng or random.SystemRandom()
        self.lock = threading.RLock()
        self.jobs = {}
        self.workers = {}
        self.cancellations = {}
        self.shutdown_event = threading.Event()
        self.wake = threading.Event()
        self.thread = None
        self.load_error = ''
        self.shared_accounts = None
        self.ensure_shared_account = None
        self.prepare_account = None
        self._load()

    def configure_automatic(self, accounts, ensure_account, prepare_account=None):
        self.shared_accounts, self.ensure_shared_account, self.prepare_account = accounts, ensure_account, prepare_account

    def _sync_automatic(self):
        if self.shared_accounts is None or self.load_error:
            return
        current = self.now()
        target = current.replace(hour=8, minute=0, second=0, microsecond=0)
        target += timedelta(days=(2-current.weekday()) % 7)
        week = target.date().isoformat()
        shared = self.shared_accounts()
        known = {a['id'] for a in shared}
        by_id = {a['id']:a for a in shared}
        changed = False
        with self.lock:
            for account_id, job in list(self.jobs.items()):
                account = self.manager.get(account_id)
                if account.pool_login_id not in known and account_id not in self.workers:
                    self.jobs.pop(account_id)
                    changed = True
                elif account_id not in self.workers and by_id[account.pool_login_id]['status'] != 'active' and not job.get('blocked_login'):
                    job.update(managed=True, enabled=False, next_run_at=None, state='failed', blocked_login=True,
                               attempt_status=by_id[account.pool_login_id]['status'],
                               message=LOGIN_REQUIRED_MESSAGE, updated_at=current.isoformat())
                    changed = True
            for item in shared:
                if item['status'] != 'active':
                    continue
                account = self.ensure_shared_account(item)
                if account.id in self.workers:
                    continue
                job = self.jobs.get(account.id)
                if job is None:
                    self.jobs[account.id] = {'account_id':account.id, 'managed':True,
                        'enabled':True, 'state':'waiting', 'run_at':target.isoformat(),
                        'next_run_at':target.isoformat(), 'weekly':True, 'immediate':False,
                        **retry_policy({}), 'attempts':0, 'submissions':0, 'history':[], 'result':None,
                        'message':'每周三 07:55 自动准备，08:00 开始领取', 'updated_at':current.isoformat()}
                    changed = True
                    continue
                if not job.get('managed'):
                    job.update(managed=True, weekly=True, immediate=False)
                    finished = parse_server_time(job.get('finished_at'))
                    if finished and job['state'] in {'success','completed','exhausted'}:
                        job['completed_week'] = finished.date().isoformat()
                    changed = True
                next_at = target+timedelta(days=7) if week in (job.get('completed_week'), job.get('skip_week')) else target
                retry_at = parse_server_time(job.get('retry_at'))
                revision_changed = (job.get('attempt_revision') != item.get('credential_revision', 0)
                                    or job.get('attempt_status') != item['status'])
                blocked = (job.get('failed_week') == week and not revision_changed
                           and (not job.get('retryable') or retry_at is not None and current < retry_at))
                if blocked:
                    continue
                login_recovered = bool(job.get('blocked_login'))
                stale_login_message = job.get('message') == LOGIN_REQUIRED_MESSAGE
                recovered_failure = (revision_changed and job.get('failure_code') in
                                     {'APP_LOGIN_REQUIRED', 'GOTO_LOGIN', 'LOGIN_REQUIRED'})
                if not job.get('enabled') or job.get('next_run_at') != next_at.isoformat() or login_recovered:
                    job.update(enabled=True, next_run_at=next_at.isoformat(), run_at=next_at.isoformat(),
                               state='waiting', blocked_login=False, updated_at=current.isoformat())
                    changed = True
                if login_recovered or stale_login_message or recovered_failure and job['state'] == 'waiting':
                    job.update(message=LOGIN_RECOVERED_MESSAGE, updated_at=current.isoformat())
                    if recovered_failure:
                        job.pop('failure_code', None)
                    changed = True
            if changed:
                self._save()

    def _load(self):
        if not self.storage or not self.storage.exists():
            return
        import json
        try:
            for job in json.loads(self.storage.read_text('utf-8')).get('jobs', []):
                self.manager.get(job['account_id'])
                retry_policy(job)
                if job.get('state') in {'running', 'cancelling'}:
                    if job.get('managed'):
                        job['uncertain'] = True
                    job.update(state='interrupted', enabled=False, next_run_at=None,
                               message='服务曾在执行中退出，请先查询奖品再重新启用')
                elif job.get('state') in {'preparing', 'armed'}:
                    job['state'] = 'waiting'
                self.jobs[job['account_id']] = job
        except (OSError, ValueError, TypeError, KeyError, ActivityError):
            self.jobs.clear()
            self.load_error = '定时配置读取失败，已保留原文件，请检查 .local/schedules.json'

    def _save(self):
        if self.storage and not self.load_error:
            atomic_json(self.storage, {'jobs': list(self.jobs.values())})

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.thread = threading.Thread(target=self._loop, name='claim-scheduler', daemon=True)
            self.thread.start()

    def stop(self):
        # Waiting jobs stay persisted. In-flight jobs remain interrupted on the next startup.
        self.shutdown_event.set()
        self.wake.set()
        with self.lock:
            for event in self.cancellations.values():
                event.set()

    def snapshots(self):
        with self.lock:
            return copy.deepcopy(self.jobs)

    def sync_automatic(self):
        self._sync_automatic()

    def busy(self, account_id):
        with self.lock:
            return account_id in self.workers

    def schedule(self, account_ids, data, *, immediate=False):
        if not isinstance(account_ids, list) or not account_ids or len(set(account_ids)) != len(account_ids):
            raise ActivityError('请勾选需要操作的账号', 'INVALID_ACCOUNTS')
        accounts = [self.manager.get(account_id) for account_id in account_ids]
        policy = retry_policy(data)
        weekly = data.get('weekly', False)
        if not isinstance(weekly, bool):
            raise ActivityError('重复设置格式无效', 'INVALID_SCHEDULE')
        target = self.now() if immediate else parse_server_time(data.get('run_at'))
        if target is None or not immediate and (target <= self.now() or target.weekday() != 2):
            raise ActivityError('请选择未来某个周三的执行时间（北京时间）', 'INVALID_SCHEDULE')
        with self.lock:
            if any(account.id in self.workers for account in accounts):
                raise ActivityError('部分账号正在执行，请先停止后再调整', 'JOB_RUNNING', 409)
            for account in accounts:
                self.jobs[account.id] = {'account_id': account.id, 'enabled': True, 'state': 'waiting',
                    'run_at': target.isoformat(), 'next_run_at': target.isoformat(),
                    'weekly': weekly and not immediate, 'immediate': immediate, **policy,
                    'attempts': 0, 'submissions': 0, 'message': '等待执行' if not immediate else '准备立即执行',
                    'history': [], 'result': None, 'updated_at': self.now().isoformat()}
            self._save()
        self.wake.set()
        return {account_id: self.snapshots()[account_id] for account_id in account_ids}

    def cancel(self, account_ids):
        with self.lock:
            for account_id in account_ids:
                self.manager.get(account_id)
                job = self.jobs.get(account_id)
                if not job:
                    continue
                event = self.cancellations.get(account_id)
                if event:
                    event.set()
                job.update(enabled=False, next_run_at=None,
                           state='cancelling' if account_id in self.workers else 'cancelled',
                           message='正在停止；已发送的请求仍会记录结果' if event else '已停止',
                           updated_at=self.now().isoformat())
                if job.get('managed'):
                    current = self.now()
                    job['skip_week'] = (current+timedelta(days=(2-current.weekday()) % 7)).date().isoformat()
            self._save()
        self.wake.set()

    def remove(self, account_id):
        self.cancel([account_id])
        with self.lock:
            if account_id in self.workers:
                raise ActivityError('请等待账号任务停止后再移除', 'JOB_RUNNING', 409)
            self.jobs.pop(account_id, None)
            self._save()

    def _update(self, account_id, *, message=None, log=False, **fields):
        with self.lock:
            job = self.jobs[account_id]
            job.update(fields, updated_at=self.now().isoformat())
            if message is not None:
                job['message'] = message
                if log:
                    job['history'].append({'time': self.now().isoformat(), 'message': message,
                                           'attempt': job['attempts']})
                    job['history'] = job['history'][-45:]
            self._save()

    def tick(self):
        """A deterministic dispatch boundary, also used by tests."""
        self._sync_automatic()
        with self.lock:
            for account_id, job in list(self.jobs.items()):
                if not job.get('enabled') or account_id in self.workers:
                    continue
                target = parse_server_time(job.get('next_run_at'))
                if target is None:
                    continue
                account = self.manager.get(account_id)
                current = account.client.server_now() if account.client.clock_anchor else self.now()
                preparation = timedelta(minutes=5) if job.get('managed') else timedelta(seconds=30)
                if current < target-preparation:
                    continue
                if current > target + timedelta(minutes=5) and not job.get('managed'):
                    self._finish(account_id, 'missed', '错过执行时间，请重新预约', current=current)
                    continue
                event = threading.Event()
                worker = threading.Thread(target=self._worker, args=(account_id, target, event),
                                          name=f'claim-{account_id[:8]}', daemon=True)
                self.cancellations[account_id] = event
                self.workers[account_id] = worker
                worker.start()

    def _loop(self):
        while not self.shutdown_event.is_set():
            try:
                self.tick()
            except Exception:
                # A bad individual job must not terminate dispatch for other accounts.
                pass
            self.wake.wait(0.1)
            self.wake.clear()

    def _finish(self, account_id, state, message, *, result=None, current=None, error=None):
        with self.lock:
            job = self.jobs[account_id]
            next_at = None
            enabled = bool(job['enabled'] and job['weekly'] and state in {'success', 'exhausted', 'completed', 'missed'})
            if enabled:
                current = current or self.now()
                next_at = parse_server_time(job['run_at']) + timedelta(days=7)
                while next_at <= current:
                    next_at += timedelta(days=7)
            self._update(account_id, state=state, message=message, result=result, log=True,
                         enabled=enabled, next_run_at=next_at.isoformat() if next_at else None,
                         finished_at=self.now().isoformat())
            if job.get('managed'):
                week = parse_server_time(job['run_at']).date().isoformat()
                if state in {'success','exhausted','completed'}:
                    job['completed_week'] = week
                    job['uncertain'] = False
                elif state == 'failed':
                    item = next((a for a in self.shared_accounts() if a['id'] == self.manager.get(account_id).pool_login_id), {})
                    job.update(failed_week=week, attempt_revision=item.get('credential_revision', 0),
                               attempt_status=item.get('status'),
                               failure_code=error.code if error is not None else None,
                               retryable=bool(error is not None and error.status != 401
                                   and error.code not in ACTION_REQUIRED and error.retryable is not False),
                               retry_at=(self.now()+timedelta(minutes=1)).isoformat(),
                               uncertain=self.manager.get(account_id).client.uncertain)
                self._save()

    def _worker(self, account_id, target, cancellation):
        account = self.manager.get(account_id)
        client = account.client
        try:
            if self.jobs[account_id].get('managed') and self.prepare_account is not None:
                self.prepare_account(account)
            self._update(account_id, state='preparing', attempts=0, submissions=0, result=None,
                         run_at=target.isoformat(), message='检查官方会话、服务器时间和设备标识', log=True)
            try:
                with client.operation_lock:
                    if account.pool_login_id and client.mode != 'app':
                        client.renew_app_session()
                    if self.jobs[account_id].get('uncertain'):
                        client.uncertain = True
                    state = client.refresh(automatic=True)
                    self.manager.update(account, state)
            except ActivityError as error:
                if error.code in ACTION_REQUIRED or error.status == 401 or error.retryable is False:
                    raise
                self.manager.update(account, error=error)
                state = {'device_ready': False}
                self._update(account_id, message='准备时连接暂时失败，到点后按设置的次数重试', log=True)
            if not state['device_ready']:
                # Browser scripts may still be initializing. The bounded rounds will read again.
                self._update(account_id, message='已准备，等待官方设备标识', log=True)
            if not self.jobs[account_id]['immediate'] and state.get('start_hour', 8) > target.hour:
                target = target.replace(hour=state['start_hour'], minute=0, second=0, microsecond=0)
                self._update(account_id, run_at=target.isoformat(), next_run_at=target.isoformat(),
                             message=f'该省官方 {state["start_hour"]}:00 开放，按官方时间执行', log=True)
            self._update(account_id, state='armed', message='准备就绪，等待执行时间')
            while not cancellation.is_set() and not self.shutdown_event.is_set():
                remaining = (target - client.server_now()).total_seconds()
                if remaining <= 0:
                    break
                cancellation.wait(min(0.5, remaining))
            if cancellation.is_set() or self.shutdown_event.is_set():
                self._finish(account_id, 'cancelled', '已停止')
                return
            self._run_attempts(account_id, cancellation)
        except ActivityError as error:
            self.manager.update(account, error=error)
            self._finish(account_id, 'failed', str(error), error=error)
        except Exception as error:
            self._finish(account_id, 'failed', f'执行异常：{type(error).__name__}，请检查后重新启用')
        finally:
            with self.lock:
                self.workers.pop(account_id, None)
                self.cancellations.pop(account_id, None)

    def _run_attempts(self, account_id, cancellation):
        account = self.manager.get(account_id)
        client = account.client
        settings = self.snapshots()[account_id]
        baseline = client.total_submissions
        last_message = ''
        for attempt in range(1, settings['max_attempts'] + 1):
            if cancellation.is_set() or self.shutdown_event.is_set():
                self._finish(account_id, 'cancelled', '已停止')
                return
            self._update(account_id, state='running', attempts=attempt,
                         message=f'第 {attempt}/{settings["max_attempts"]} 轮执行', log=True)
            try:
                with client.operation_lock:
                    result = client.claim(automatic=True)
                    # Snapshot follows the client, including recovered timeout outcomes.
                    self.manager.update(account, {'authenticated': True})
                self._update(account_id, submissions=client.total_submissions - baseline)
                self._finish(account_id, 'success', result['message'], result=result)
                return
            except ActivityError as error:
                last_message = str(error)
                self.manager.update(account, error=error)
                self._update(account_id, submissions=client.total_submissions - baseline,
                             message=last_message, log=True)
                if error.code == 'ROUND_COMPLETED':
                    self._finish(account_id, 'completed', last_message)
                    return
                if error.code in ACTION_REQUIRED or error.status == 401 or error.retryable is False:
                    self._finish(account_id, 'failed', last_message, error=error)
                    return
            if attempt < settings['max_attempts']:
                interval = self.rng.uniform(settings['interval_min'], settings['interval_max'])
                self._update(account_id, retry_in=round(interval, 3))
                if cancellation.wait(interval):
                    self._finish(account_id, 'cancelled', '已停止')
                    return
        self._finish(account_id, 'exhausted', f'已达 {settings["max_attempts"]} 轮上限：{last_message}')
