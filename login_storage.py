"""SQLite account storage with Windows-user-bound encrypted credentials."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sqlite3
from threading import RLock


class LoginStorageError(Exception):
    pass


def _dpapi(data: bytes, *, decrypt=False) -> bytes:
    if os.name != 'nt':
        raise LoginStorageError('当前系统不支持 Windows 登录态加密，账号仅保留在内存中。')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]

    def blob(value):
        buffer = ctypes.create_string_buffer(value)
        return Blob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

    source, source_buffer = blob(data)
    entropy, entropy_buffer = blob(b'lingxi-web/shared-app-login/v1')
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel.LocalFree.restype = wintypes.HLOCAL
    if decrypt:
        function = crypt.CryptUnprotectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = [ctypes.byref(source), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)]
    else:
        function = crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        args = [ctypes.byref(source), 'Lingxi shared APP sessions', ctypes.byref(entropy), None, None, 1, ctypes.byref(output)]
    function.restype = wintypes.BOOL
    if not function(*args):
        raise LoginStorageError('无法读取本机加密登录态，请使用原 Windows 用户登录或重新验证账号。' if decrypt
                                else '本机登录态加密失败，账号暂时仅保留在内存中。')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(ctypes.cast(output.data, wintypes.HLOCAL))


class DatabaseLoginStorage:
    def __init__(self, path, *, protect=None, unprotect=None):
        self.path = Path(path)
        self.protect = protect or _dpapi
        self.unprotect = unprotect or (lambda data: _dpapi(data, decrypt=True))
        self.lock = RLock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('CREATE TABLE IF NOT EXISTS account_sessions '
                    '(login_id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT "", phone_masked TEXT NOT NULL DEFAULT "", '
                    'payload BLOB NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)')
                columns = {row[1] for row in connection.execute('PRAGMA table_info(account_sessions)')}
                for column in ('name', 'phone_masked'):
                    if column not in columns:
                        connection.execute(f'ALTER TABLE account_sessions ADD COLUMN {column} TEXT NOT NULL DEFAULT ""')
                connection.execute('CREATE TABLE IF NOT EXISTS activity_accounts '
                    '(account_id TEXT PRIMARY KEY, name TEXT NOT NULL, pool_login_id TEXT NOT NULL, phone_masked TEXT NOT NULL)')
                connection.execute('CREATE TABLE IF NOT EXISTS lingxi_tasks '
                    '(task_id TEXT PRIMARY KEY, payload BLOB NOT NULL)')
                connection.execute('CREATE TABLE IF NOT EXISTS lingxi_settings '
                    '(id INTEGER PRIMARY KEY CHECK(id=1), concurrency INTEGER NOT NULL CHECK(concurrency BETWEEN 1 AND 1024))')
                connection.execute('CREATE TABLE IF NOT EXISTS checkin_data '
                    '(id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)')
        except (sqlite3.Error, OSError) as error:
            raise LoginStorageError('无法打开本机账号数据库，请检查目录权限或数据库是否损坏。') from error

    def _connect(self):
        from contextlib import closing
        return closing(sqlite3.connect(self.path, timeout=5))

    def load_checkin_data(self):
        try:
            with self.lock, self._connect() as connection:
                row = connection.execute('SELECT payload FROM checkin_data WHERE id=1').fetchone()
            return json.loads(self.unprotect(row[0]).decode('utf-8')) if row else None
        except (sqlite3.Error, ValueError, UnicodeError) as error:
            raise LoginStorageError('签到数据读取失败，已保留原数据库。') from error

    def save_checkin_data(self, data):
        payload = self.protect(json.dumps(data, ensure_ascii=False).encode('utf-8'))
        try:
            with self.lock, self._connect() as connection, connection:
                connection.execute('INSERT INTO checkin_data(id,payload) VALUES(1,?) '
                    'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (payload,))
        except sqlite3.Error as error:
            raise LoginStorageError('签到数据保存失败，本次未提交。') from error

    def load(self):
        try:
            with self.lock, self._connect() as connection:
                rows = connection.execute('SELECT login_id, payload FROM account_sessions').fetchall()
            result = []
            for login_id, payload in rows:
                if len(payload) > 128 * 1024:
                    raise ValueError
                value = json.loads(self.unprotect(payload).decode('utf-8'))
                if not isinstance(value, dict) or value.get('id') != login_id:
                    raise ValueError
                result.append(value)
            return result
        except LoginStorageError:
            raise
        except (sqlite3.Error, OSError, ValueError, TypeError, UnicodeError) as error:
            raise LoginStorageError('数据库中的加密登录态读取失败，已保留原数据；请检查数据库或原 Windows 用户。') from error

    def save(self, accounts):
        try:
            encrypted = [(item['id'], item.get('name',''), item['phone'][:3]+'****'+item['phone'][-4:],
                          self.protect(json.dumps(item, ensure_ascii=False).encode('utf-8'))) for item in accounts]
            with self.lock, self._connect() as connection, connection:
                connection.execute('DELETE FROM account_sessions')
                connection.executemany('INSERT INTO account_sessions(login_id,name,phone_masked,payload) VALUES(?,?,?,?)', encrypted)
        except LoginStorageError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise LoginStorageError('账号数据库保存失败，请检查本机目录权限；当前内存会话仍可使用。') from error

    def load_accounts(self):
        try:
            with self.lock, self._connect() as connection:
                return [{'id':row[0], 'name':row[1], 'pool_login_id':row[2], 'phone_masked':row[3]} for row in
                        connection.execute('SELECT account_id,name,pool_login_id,phone_masked FROM activity_accounts ORDER BY rowid')]
        except sqlite3.Error as error:
            raise LoginStorageError('活动账号数据库读取失败，已保留原数据。') from error

    def save_accounts(self, accounts):
        try:
            with self.lock, self._connect() as connection, connection:
                connection.execute('DELETE FROM activity_accounts')
                connection.executemany('INSERT INTO activity_accounts(account_id,name,pool_login_id,phone_masked) VALUES(?,?,?,?)',
                    [(a['id'], a['name'], a.get('pool_login_id',''), a.get('phone_masked','')) for a in accounts])
        except sqlite3.Error as error:
            raise LoginStorageError('活动账号数据库保存失败，当前内存会话仍可使用。') from error

    def load_lingxi_tasks(self):
        try:
            with self.lock, self._connect() as connection:
                rows = connection.execute('SELECT task_id,payload FROM lingxi_tasks ORDER BY rowid').fetchall()
            result=[]
            for task_id,payload in rows:
                if len(payload)>8*1024*1024:
                    raise ValueError
                value=json.loads(self.unprotect(payload).decode('utf-8'))
                if not isinstance(value,dict) or value.get('id')!=task_id:
                    raise ValueError
                result.append(value)
            return result
        except LoginStorageError:
            raise
        except (sqlite3.Error,ValueError,UnicodeError,TypeError) as error:
            raise LoginStorageError('灵犀任务读取失败，已保留原数据库。') from error

    def save_lingxi_tasks(self, tasks):
        try:
            rows=[(task['id'],self.protect(json.dumps(task,ensure_ascii=False).encode('utf-8'))) for task in tasks]
            with self.lock,self._connect() as connection,connection:
                connection.executemany('INSERT INTO lingxi_tasks(task_id,payload) VALUES(?,?) '
                    'ON CONFLICT(task_id) DO UPDATE SET payload=excluded.payload',rows)
        except LoginStorageError:
            raise
        except (sqlite3.Error,OSError) as error:
            raise LoginStorageError('灵犀任务保存失败，本次操作未完成。') from error

    def delete_lingxi_task(self, task_id):
        self.delete_lingxi_tasks([task_id])

    def delete_lingxi_tasks(self, task_ids):
        try:
            with self.lock,self._connect() as connection,connection:
                connection.executemany('DELETE FROM lingxi_tasks WHERE task_id=?',[(task_id,) for task_id in task_ids])
        except sqlite3.Error as error:
            raise LoginStorageError('灵犀任务删除失败。') from error

    def load_lingxi_concurrency(self):
        try:
            with self.lock,self._connect() as connection:
                row=connection.execute('SELECT concurrency FROM lingxi_settings WHERE id=1').fetchone()
            return row[0] if row else None
        except sqlite3.Error as error:
            raise LoginStorageError('灵犀并发设置读取失败。') from error

    def save_lingxi_concurrency(self, value):
        try:
            with self.lock,self._connect() as connection,connection:
                connection.execute('INSERT INTO lingxi_settings(id,concurrency) VALUES(1,?) '
                    'ON CONFLICT(id) DO UPDATE SET concurrency=excluded.concurrency',(value,))
        except sqlite3.Error as error:
            raise LoginStorageError('灵犀并发设置保存失败。') from error
