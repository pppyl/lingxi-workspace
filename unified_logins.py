"""One shared APP login per phone, with optional encrypted persistence."""
from dataclasses import dataclass, field
from copy import deepcopy
import secrets
import time
import re
from threading import RLock

from login_storage import LoginStorageError


@dataclass
class SavedLogin:
    id: str
    phone: str = field(repr=False)
    cookie: str = field(repr=False)
    profile: dict = field(repr=False)
    token: str = field(repr=False)
    name: str = ''
    created_at: float = field(default_factory=time.time)
    status: str = 'active'
    checked_at: float = 0
    message: str = ''
    app_context: dict = field(default_factory=dict, repr=False)
    credential_revision: int = 1

    def public(self):
        return {'id': self.id, 'name': self.name, 'phone_masked': self.phone[:3] + '****' + self.phone[-4:],
                'logged_in': self.status != 'login_required', 'created_at': self.created_at,
                'status': self.status, 'checked_at': self.checked_at, 'message': self.message,
                'credential_revision': self.credential_revision}


class UnifiedLogins:
    def __init__(self, storage=None):
        self.lock = RLock()
        self.records = {}
        self.pending = {}
        self.phone_locks = {}
        self.credential_revisions = {}
        self.storage, self.warning, self.load_error = storage, '', ''
        if storage is not None:
            try:
                records = {}
                for item in storage.load():
                    if (not isinstance(item, dict) or not re.fullmatch(r'[a-f0-9]{32}', item.get('id', ''))
                            or not re.fullmatch(r'1[3-9][0-9]{9}', item.get('phone', ''))
                            or not isinstance(item.get('cookie'), str) or not item['cookie']
                            or not isinstance(item.get('token', ''), str) or not isinstance(item.get('profile', {}), dict)
                            or not isinstance(item.get('name', ''), str)
                            or item.get('status', 'active') not in ('active', 'login_required')
                            or not isinstance(item.get('app_context', {}), dict)
                            or isinstance(item.get('credential_revision', 1), bool)
                            or not isinstance(item.get('credential_revision', 1), int)
                            or item.get('credential_revision', 1) < 1):
                        raise ValueError
                    record = SavedLogin(item['id'], item['phone'], item['cookie'], item.get('profile', {}),
                                        item.get('token', ''), item.get('name', '')[:40], float(item.get('created_at', time.time())),
                                        item.get('status', 'active'), 0,
                                        '账号需要短信验证' if item.get('status') == 'login_required' else '已读取本机登录态，后台自动维护',
                                        deepcopy(item.get('app_context', {})), item.get('credential_revision', 1))
                    records[record.phone] = record
                self.records = records
                self.credential_revisions = {phone:record.credential_revision for phone,record in records.items()}
            except (LoginStorageError, ValueError, TypeError) as error:
                self.load_error = self.warning = str(error) if isinstance(error, LoginStorageError) else '加密账号内容无效，已保留原文件。'

    def save(self):
        if self.storage is None or self.load_error:
            return
        with self.lock:
            try:
                self.storage.save([{'id':r.id, 'phone':r.phone, 'cookie':r.cookie, 'profile':r.profile,
                    'token':r.token, 'name':r.name, 'created_at':r.created_at,
                    'app_context':r.app_context, 'credential_revision':r.credential_revision,
                    'status':'login_required' if r.status == 'login_required' else 'active'} for r in self.records.values()])
                self.warning = ''
            except LoginStorageError as error:
                self.warning = str(error)

    def mark(self, saved, status, message='', *, expected_revision=None):
        with self.lock:
            if (self.records.get(saved.phone) is not saved or
                    expected_revision is not None and saved.credential_revision != expected_revision):
                return False
            login_changed = (saved.status == 'login_required') != (status == 'login_required')
            saved.status, saved.message = status, message
            saved.checked_at = time.time()
            if login_changed:
                self.save()
            return True

    def phone_lock(self, phone):
        with self.lock:
            return self.phone_locks.setdefault(phone, RLock())

    def get(self, phone):
        with self.lock:
            return self.records.get(phone)

    def get_id(self, login_id):
        with self.lock:
            return next((r for r in self.records.values() if r.id == login_id), None)

    def credentials(self, login_id):
        """Return one coherent credential snapshot; never expose it to the UI."""
        with self.lock:
            record = next((r for r in self.records.values() if r.id == login_id), None)
            if record is None:
                return None
            return {'cookie':record.cookie, 'phone':record.phone, 'profile':deepcopy(record.profile),
                    'app_context':deepcopy(record.app_context), 'credential_revision':record.credential_revision}

    def remember(self, phone, cookie, profile, token, name='', *, app_context=None):
        with self.lock:
            old = self.records.get(phone)
            if old is not None:
                record = old
                record.cookie, record.profile, record.token = cookie, dict(profile), token
                record.name = name or record.name
                if app_context is not None:
                    record.app_context = deepcopy(app_context)
                record.credential_revision = max(record.credential_revision, self.credential_revisions.get(phone, 0)) + 1
                record.status, record.checked_at, record.message = 'active', 0, ''
            else:
                record = SavedLogin(secrets.token_hex(16), phone, cookie, dict(profile), token, name,
                                    app_context=deepcopy(app_context or {}),
                                    credential_revision=self.credential_revisions.get(phone, 0) + 1)
            self.records[phone] = record
            self.credential_revisions[phone] = record.credential_revision
            self.pending.pop(phone, None)
            self.save()
            return record

    def rotate_credentials(self, phone, *, expected_revision, cookie, app_context=None, profile=None, token=None):
        """Commit a refresh only if its input version is still current.

        Returns the same SavedLogin on success, or None for a missing/stale
        account. Refresh context is merged; independent Lingxi credentials
        survive an APP Cookie rotation until their own server rejects them.
        """
        if (isinstance(expected_revision, bool) or not isinstance(expected_revision, int)
                or expected_revision < 1 or not isinstance(cookie, str) or not cookie
                or '\r' in cookie or '\n' in cookie):
            raise ValueError('Invalid credential update')
        if (app_context is not None and not isinstance(app_context, dict)
                or profile is not None and not isinstance(profile, dict)
                or token is not None and not isinstance(token, str)):
            raise ValueError('Invalid credential update')
        with self.phone_lock(phone), self.lock:
            record = self.records.get(phone)
            if record is None or record.credential_revision != expected_revision:
                return None
            record.cookie = cookie
            if app_context is not None:
                record.app_context = {**record.app_context, **deepcopy(app_context)}
            if profile is not None:
                record.profile = dict(profile)
            record.token = token if token is not None else record.token
            record.credential_revision += 1
            self.credential_revisions[phone] = record.credential_revision
            record.status, record.checked_at = 'active', 0
            record.message = 'APP 会话已维护，业务使用最新凭据'
            self.save()
            return record

    def forget(self, phone):
        with self.lock:
            self.records.pop(phone, None)
            self.pending.pop(phone, None)
            self.save()

    def public(self):
        with self.lock:
            return [r.public() for r in self.records.values()]
