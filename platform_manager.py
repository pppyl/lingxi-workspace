"""Discover independent activities, stop expired work, and remove selected modules."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from importlib import import_module, invalidate_caches
import json
from pathlib import Path
import re
import shutil
import sys
from threading import Event, RLock, Thread

from flask import jsonify, render_template, request
from login_storage import LoginStorageError

SHANGHAI = timezone(timedelta(hours=8))


class PlatformError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class PlatformManager:
    def __init__(self, app, *, root_dir, storage=None, credentials=None, accounts=None, now=None):
        self.app, self.root_dir, self.storage = app, Path(root_dir).resolve(), storage
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.platforms, self.registry, self.warning = {}, {}, ''
        if storage is not None:
            saved = storage.load_platform_registry()
            if saved is not None:
                if not isinstance(saved, dict):
                    raise LoginStorageError('平台管理记录格式无效，已保留原数据库。')
                self.registry = saved
        for folder in sorted(self.root_dir.glob('*')):
            manifest_path = folder / 'manifest.json'
            if not manifest_path.is_file():
                continue
            if folder.is_symlink() or folder.resolve() != folder or folder.parent != self.root_dir:
                raise PlatformError('活动目录不能指向模块目录之外。')
            manifest = json.loads(manifest_path.read_text('utf-8'))
            platform_id, title = manifest.get('id'), manifest.get('title')
            home, module = manifest.get('home'), manifest.get('module')
            navigation = manifest.get('navigation', 'main')
            if (not isinstance(platform_id, str) or not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', platform_id)
                    or not isinstance(title, str) or not title.strip()
                    or not isinstance(home, str) or not re.fullmatch(r'/[a-zA-Z0-9_/-]+', home)
                    or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', folder.name)
                    or module != f'activities.{folder.name}.plugin'
                    or not isinstance(manifest.get('temporary'), bool)
                    or navigation not in ('main', 'checkin')
                    or platform_id in self.platforms):
                raise PlatformError('活动清单格式无效，请检查本地模块。')
            entry = {'id': platform_id, 'title': title, 'home': home, 'module': module,
                     'navigation': navigation,
                     'temporary': manifest['temporary'], 'folder': folder.resolve(),
                     'controller': None, 'state': 'running', 'stopped': False, 'metadata': {}}
            self.platforms[platform_id] = entry
            if platform_id in self.registry:
                entry.update(state='deleted', stopped=True)
                self._remove_module(entry)
                continue
            controller = import_module(module).register(app, credentials, accounts, storage)
            entry['controller'] = controller
        self._routes()
        self.tick()

    def _routes(self):
        @self.app.errorhandler(PlatformError)
        def platform_error(error):
            return jsonify(error=str(error)), error.status

        @self.app.get('/platforms')
        def platforms_page():
            return render_template('platforms.html')

        @self.app.get('/api/platforms')
        def platforms_status():
            return jsonify(platforms=self.public(), warning=self.warning, server_time=self.now().isoformat())

        @self.app.post('/api/platforms/<platform_id>/delete')
        def delete_platform(platform_id):
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or payload.get('confirmation') != platform_id:
                raise PlatformError('请输入活动标识，确认永久移除此活动。')
            self.delete(platform_id)
            return jsonify(platforms=self.public(), message='活动已永久移除，任务、活动数据和模块均已清理。')

    def public(self):
        with self.lock:
            rows = []
            for platform_id, entry in self.platforms.items():
                meta = entry['metadata']
                row = {key: entry[key] for key in ('id', 'title', 'home', 'temporary', 'state', 'navigation')}
                row.update(active=bool(meta.get('active')) and entry['state'] == 'running',
                           end_at=meta.get('end_at'), server_time=self.now().isoformat())
                if entry['state'] == 'deleted':
                    row.update(deleted_at=self.registry[platform_id].get('deleted_at'),
                               removal_pending=entry['folder'].exists())
                rows.append(row)
            known = set(self.platforms)
            for platform_id, tombstone in self.registry.items():
                if platform_id not in known:
                    rows.append({**deepcopy(tombstone), 'id': platform_id, 'state': 'deleted',
                                 'active': False, 'server_time': self.now().isoformat()})
            return rows

    def tick(self):
        with self.lock:
            for entry in self.platforms.values():
                if entry['state'] != 'running':
                    continue
                meta = entry['controller'].metadata()
                entry['metadata'] = meta
                if entry['temporary'] and meta.get('ended'):
                    entry['controller'].stop(wait=True)
                    entry.update(state='expired', stopped=True)

    def start(self):
        with self.lock:
            if self.thread is not None:
                return
            for entry in self.platforms.values():
                if entry['state'] == 'running':
                    entry['controller'].scheduler.start()
            self.thread = Thread(target=self._loop, daemon=True, name='activity-platforms')
            self.thread.start()

    def _loop(self):
        while not self.quit.wait(1):
            try:
                self.tick()
            except (PlatformError, LoginStorageError) as error:
                self.warning = str(error)

    def shutdown(self):
        self.quit.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        with self.lock:
            for entry in self.platforms.values():
                if entry['controller'] is not None and not entry['stopped']:
                    entry['controller'].stop(wait=True)
                    entry['stopped'] = True

    def busy_login(self, login_id):
        with self.lock:
            return any(entry['controller'].busy_login(login_id) for entry in self.platforms.values()
                       if entry['controller'] is not None and not entry['stopped'])

    def forget(self, login_id):
        with self.lock:
            for entry in self.platforms.values():
                if entry['controller'] is not None and entry['state'] != 'deleted':
                    entry['controller'].forget(login_id)

    def delete(self, platform_id):
        with self.lock:
            entry = self.platforms.get(platform_id)
            if entry is None:
                if platform_id in self.registry:
                    return
                raise PlatformError('活动不存在或已经移除。', 404)
            controller = entry['controller']
            if controller is not None and not entry['stopped']:
                controller.deleted = True
                controller.stop(wait=True)
                entry['stopped'] = True
            elif controller is not None:
                controller.deleted = True
            if self.storage is not None:
                self.storage.delete_activity_data(platform_id)
                self.storage.delete_credentials(scope=platform_id)
            tombstone = {key: entry[key] for key in ('title', 'home', 'temporary')}
            tombstone.update(deleted_at=self.now().isoformat())
            updated = {**self.registry, platform_id: tombstone}
            if self.storage is not None:
                self.storage.save_platform_registry(updated)
            self.registry = updated
            entry['state'] = 'deleted'
            self._remove_module(entry)
            entry['controller'] = None

    def _remove_module(self, entry):
        folder = entry['folder']
        # Validate the resolved, absolute target after all workers have stopped.
        if (self.root_dir.resolve() != self.root_dir or folder.is_symlink()
                or folder.resolve() != folder or folder.parent != self.root_dir):
            raise PlatformError('活动目录发生变化，任务已停止，请检查后重新移除。', 409)
        try:
            if folder.exists():
                shutil.rmtree(folder)
        except OSError as error:
            raise PlatformError('任务和数据已清理，活动模块仍被占用，请稍后重新移除。', 409) from error
        prefix = entry['module'].rsplit('.', 1)[0]
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + '.'):
                del sys.modules[name]
        invalidate_caches()
