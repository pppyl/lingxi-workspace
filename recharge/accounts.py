"""Independent account sessions and a small persistent account directory."""
from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from .cmcc import ActivityClient, ActivityError, ROOT
from login_storage import LoginStorageError
from runtime_paths import RECHARGE_DATA_DIR


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


class Account:
    def __init__(self, account_id, name, client):
        self.id, self.name, self.client = account_id, name, client
        self.cached = {'authenticated': False, 'mode': client.mode}
        self.error = ''
        self.pool_login_id = ''

    def snapshot(self):
        return {'id': self.id, 'name': self.name, 'mode': self.client.mode,
                'authenticated': self.cached.get('authenticated', False),
                'phone': self.cached.get('phone', ''), 'error': self.error,
                'session_status': self.cached.get('session_status', ''), 'login_id': self.pool_login_id,
                'activity': {key:self.cached[key] for key in ('can_claim','rd_status','state_label','reason',
                    'start_hour','server_time','next_start','prize') if key in self.cached}}


class AccountManager:
    def __init__(self, storage=None, default_client=None, client_factory=ActivityClient, database=None):
        self.storage = Path(storage) if storage else None
        self.database = database
        self.factory = client_factory
        self.lock = threading.RLock()
        self.accounts = {}
        self.load_error = ''
        if default_client:
            self.accounts['default'] = Account('default', '账号 1', default_client)
            return
        if database is not None or self.storage and self.storage.exists():
            try:
                items = database.load_accounts() if database is not None else []
                if not items and self.storage and self.storage.exists():
                    items = json.loads(self.storage.read_text('utf-8'))['accounts']
                for item in items:
                    account_id = item['id']
                    if account_id != 'default' and (len(account_id) != 32 or any(c not in '0123456789abcdef' for c in account_id)):
                        raise ValueError('Invalid account ID')
                    client = self.factory(profile_dir=self.profile(account_id))
                    # Restore the directory immediately. The shared session
                    # maintainer reconnects the saved APP binding after startup.
                    client.mode = 'none'
                    self.accounts[account_id] = Account(account_id, str(item['name'])[:40], client)
                    account = self.accounts[account_id]
                    account.pool_login_id = item.get('pool_login_id', '')
                    account.cached.update(phone=item.get('phone_masked', ''), session_status='restoring' if account.pool_login_id else '')
                if database is not None and self.accounts:
                    self.save()
            except (OSError, KeyError, TypeError, ValueError, LoginStorageError):
                self.accounts.clear()
                self.load_error = '账号目录读取失败，已保留原数据，请检查账号数据库或旧配置。'
        if not self.accounts:
            client = self.factory(profile_dir=self.profile('default'))
            self.accounts['default'] = Account('default', '账号 1', client)

    def profile(self,account_id):
        base=self.storage.parent if self.storage else RECHARGE_DATA_DIR
        if account_id == 'default':
            return base/'browser-profile'
        return base/'accounts'/account_id/'browser-profile'

    def save(self):
        if (self.storage or self.database is not None) and not self.load_error:
            with self.lock:
                records = [{'id': a.id, 'name': a.name,
                    'login_channel': 'app', 'browser_session': False, 'pool_login_id': a.pool_login_id,
                    'phone_masked': a.cached.get('phone', '')} for a in self.accounts.values()]
                if self.database is not None:
                    try:
                        self.database.save_accounts(records)
                    except LoginStorageError as error:
                        self.load_error = str(error)
                else:
                    atomic_json(self.storage, {'accounts':records})

    def get(self, account_id=None):
        with self.lock:
            if account_id is None:
                return next(iter(self.accounts.values()))
            if account_id not in self.accounts:
                raise ActivityError('账号不存在，请刷新账号列表', 'ACCOUNT_NOT_FOUND', 404)
            return self.accounts[account_id]

    def add(self, name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 40:
            raise ActivityError('账号备注应为 1～40 个字', 'INVALID_ACCOUNT_NAME')
        with self.lock:
            account_id = uuid.uuid4().hex
            account = Account(account_id, name.strip(), self.factory(profile_dir=self.profile(account_id)))
            self.accounts[account_id] = account
            self.save()
            return account

    def update(self, account, result=None, error=None):
        with self.lock:
            if result is not None:
                account.cached = {**account.cached, **result}
                if result.get('authenticated'):
                    account.cached['session_status'] = result.get('session_status', 'active')
            account.error = str(error) if error else ''
            if error and getattr(error, 'status', None) == 401:
                account.cached['authenticated'] = False
            self.save()

    def snapshots(self):
        with self.lock:
            return [a.snapshot() for a in self.accounts.values()]

    def remove(self, account_id):
        with self.lock:
            if len(self.accounts) == 1:
                raise ActivityError('请至少保留一个账号', 'LAST_ACCOUNT')
            account = self.get(account_id)
        with account.client.operation_lock:
            account.client.logout()
            account.client.bridge.stop()
        with self.lock:
            del self.accounts[account_id]
            self.save()
