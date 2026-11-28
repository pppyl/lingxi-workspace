"""Discover independent activities, stop expired work, and remove selected modules."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib import import_module, invalidate_caches
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import shutil
import sys
from threading import Event, RLock, Thread

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge

from .activity_packages import (ActivityPackages, ActivityPackageStorage, BUNDLED_ACTIVITY_IDS, MAX_ARCHIVE,
                               PlatformError, export_package, read_manifest)
from workspace.accounts.credentials import CredentialCache
from workspace.accounts.storage import LoginStorageError

SHANGHAI = timezone(timedelta(hours=8))
PACKAGE_UPLOAD_PATH = '/api/platforms/packages'


class PackageRegistration(Flask):
    def register_blueprint(self, blueprint, **options):
        if options or blueprint.name in self.blueprints:
            raise PlatformError('请直接注册活动蓝图，并在蓝图中声明路由。')
        super().register_blueprint(blueprint)


class PlatformManager:
    def __init__(self, app, *, root_dir, package_dir=None, storage=None, credentials=None, accounts=None,
                 now=None, features=None):
        self.app, self.root_dir, self.storage = app, Path(root_dir).resolve(), storage
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.features = features
        self.lock, self.quit, self.thread = RLock(), Event(), None
        self.platforms, self.registry, self.warning = {}, {}, ''
        self.packages = ActivityPackages(package_dir) if package_dir is not None else None
        self.catalog, self.releases, self.failures = {}, {}, {}
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
            manifest = read_manifest(folder)
            if manifest.id in self.catalog:
                raise PlatformError('活动清单格式无效，请检查本地模块。')
            self.catalog[manifest.id] = manifest
        if self.packages is not None:
            for release in self.packages.installed():
                self.releases.setdefault(release.id, []).append(release)
            for releases in self.releases.values():
                releases.sort(key=lambda release: release.version_key, reverse=True)
        self._routes()
        for platform_id in dict.fromkeys((*self.catalog, *self.releases)):
            self._activate(platform_id, credentials, accounts)
        self.tick()

    def _entry(self, manifest, controller=None, *, state='running', namespace=None, metadata=None):
        return {**{key: getattr(manifest, key) for key in ('id', 'title', 'home', 'navigation', 'temporary', 'folder')},
                'manifest': manifest, 'namespace': namespace, 'controller': controller,
                'state': state, 'stopped': state != 'running', 'metadata': metadata or {}}

    def _activate(self, platform_id, credentials, accounts):
        builtin = self.catalog.get(platform_id)
        if platform_id in self.registry:
            if builtin is not None:
                self.platforms[platform_id] = self._entry(builtin, state='deleted')
                self._remove_module(builtin, builtin.module.rsplit('.', 1)[0])
            return
        releases = self.releases.get(platform_id, [])
        for release in releases:
            if builtin and release.version_key <= builtin.version_key:
                break
            try:
                self._validate_identity(release, builtin)
                controller, namespace, metadata = self._register_package(release, credentials, accounts)
            except Exception as error:
                self.app.logger.warning('Activity %s %s could not load (%s)', platform_id, release.version,
                                        type(error).__name__, exc_info=True)
                self.failures.setdefault(platform_id, release.version)
                continue
            self._register_entry(self._entry(release, controller, namespace=namespace, metadata=metadata))
            return
        if builtin is not None:
            controller = import_module(builtin.module).register(self.app, credentials, accounts,
                                                               self._package_storage(platform_id))
            self._register_entry(self._entry(builtin, controller, namespace=builtin.module.rsplit('.', 1)[0]))

    def _validate_identity(self, manifest, current=None):
        if current and current.identity != manifest.identity:
            raise PlatformError('更新活动时须保留原目录名、入口和导航位置。', 409)
        if manifest.id == 'navigation':
            raise PlatformError('该标识已由平台导航接口使用。', 409)
        candidates = [*self.catalog.values(), *(versions[0] for versions in self.releases.values())]
        if any(other.id != manifest.id and other.folder.name == manifest.folder.name for other in candidates):
            raise PlatformError('活动包目录名已被其他活动使用。', 409)
        if self.features is not None:
            known = self.features.entries.get(manifest.id)
            if known is not None and known['platform'] is None:
                raise PlatformError('该标识已由平台常驻功能使用。', 409)
            for prefix in (manifest.home, manifest.api):
                owner = self.features.identify(prefix)
                if owner not in (None, manifest.id) and not (owner == 'checkin' and manifest.navigation == 'checkin'
                                                             and prefix == manifest.home):
                    raise PlatformError('活动入口与现有功能冲突。', 409)
        existing_routes = (rule.rule for rule in self.app.url_map.iter_rules())
        if current is None and any(path == manifest.home or path == manifest.api or path.startswith(manifest.api + '/')
                                   for path in existing_routes):
            raise PlatformError('活动入口与平台已有路由冲突。', 409)

    def _register_package(self, manifest, credentials, accounts):
        # Each release has its own Python namespace. Relative imports and Flask resource paths
        # work with the same source layout in a checkout and in packaged clients.
        directory_key = sha256(str(manifest.folder).encode('utf-8')).hexdigest()[:16]
        namespace = f'activities._installed_{manifest.folder.name}_{directory_key}'
        spec = spec_from_file_location(namespace, manifest.folder/'__init__.py',
                                       submodule_search_locations=[str(manifest.folder)])
        package = module_from_spec(spec)
        sys.modules[namespace] = package
        controller = None
        staging = PackageRegistration(namespace, static_folder=None)
        staging.extensions.update(self.app.extensions)
        storage = self._package_storage(manifest.id)
        if isinstance(storage, ActivityPackageStorage):
            staging.extensions['credential_cache'] = CredentialCache(storage)
        try:
            spec.loader.exec_module(package)
            controller = import_module(namespace + '.plugin').register(staging, credentials, accounts, storage)
            metadata = controller.metadata()
            if (not isinstance(metadata, dict) or not callable(controller.scheduler.start)
                    or any(not callable(getattr(controller, name)) for name in ('busy_login', 'forget', 'stop'))):
                raise PlatformError('未实现活动控制器接口。')
            self._mount_package(staging, manifest)
        except Exception:
            if controller is not None and callable(getattr(controller, 'stop', None)):
                try:
                    controller.stop(wait=True)
                except Exception:
                    self.app.logger.exception('Activity %s cleanup failed', manifest.id)
            self._unload(namespace)
            raise
        return controller, namespace, metadata

    def _package_storage(self, platform_id):
        builtin = self.catalog.get(platform_id)
        if self.storage is None or platform_id in BUNDLED_ACTIVITY_IDS or builtin and builtin.version == '0.0.0':
            return self.storage
        return ActivityPackageStorage(self.storage, platform_id)

    def _mount_package(self, staging, manifest):
        prefixes = (manifest.home, manifest.api, f'/activities/{manifest.id}/static')
        occupied = {rule.rule for rule in self.app.url_map.iter_rules()}
        rules = list(staging.url_map.iter_rules())
        if (not any(rule.rule == manifest.home and 'GET' in rule.methods for rule in rules)
                or len(staging.blueprints) != 1
                or any(name in self.app.blueprints for name in staging.blueprints)
                or any(rule.rule in occupied or not any(rule.rule == prefix or rule.rule.startswith(prefix + '/')
                                                       for prefix in prefixes) for rule in rules)):
            raise PlatformError('活动包须使用一个独立蓝图，且全部路由必须在活动入口范围内。')
        name, blueprint = next(iter(staging.blueprints.items()))
        if name != manifest.folder.name or any(not rule.endpoint.startswith(name + '.') for rule in rules):
            raise PlatformError('活动蓝图名须与目录名一致，路由须注册在该蓝图上。')
        self.app.register_blueprint(blueprint)

    def _register_entry(self, entry):
        manifest = entry['manifest']
        self.platforms[manifest.id] = entry
        if self.features is not None:
            self.features.register(manifest.id, manifest.title, manifest.home, (manifest.home, manifest.api),
                                   temporary=manifest.temporary, platform=entry)
            scheduler = entry['controller'].scheduler
            self.features.bind_scheduler(manifest.id, scheduler,
                background=tuple(name for name in ('tick','run','_enqueue','login_restored','logged_in') if callable(getattr(scheduler, name, None))),
                workers=tuple(name for name in ('_work','_perform','_refresh') if callable(getattr(scheduler, name, None))))

    def _routes(self):
        @self.app.errorhandler(PlatformError)
        def platform_error(error):
            return jsonify(error=str(error)), error.status

        @self.app.get('/platforms')
        def platforms_page():
            return render_template('platforms.html', manage_packages=can_manage_packages())

        @self.app.get('/api/platforms')
        def platforms_status():
            return jsonify(platforms=self.public(), warning=self.warning, server_time=self.now().isoformat())

        def can_manage_packages():
            return self.packages is not None and request.environ.get('workspace.local_access', False)

        @self.app.post(PACKAGE_UPLOAD_PATH)
        def install_package():
            if not can_manage_packages():
                raise PlatformError('请从本机工具台导入活动包。', 403)
            if request.mimetype != 'application/zip':
                raise PlatformError('请选择 ZIP 格式的活动包。', 415)
            request.max_content_length = MAX_ARCHIVE
            try:
                payload = request.get_data(cache=False)
            except RequestEntityTooLarge as error:
                raise PlatformError('活动 ZIP 包不能超过 16 MB。', 413) from error
            manifest = self.install(payload)
            return jsonify(message=f'{manifest.title} {manifest.version} 已导入，退出并重新启动工具台后生效。'), 201

        @self.app.get('/api/platforms/<platform_id>/package')
        def download_package(platform_id):
            if not can_manage_packages():
                raise PlatformError('请从本机工具台导出活动包。', 403)
            with self.lock:
                entry = self.platforms.get(platform_id)
                releases = self.releases.get(platform_id)
                if platform_id in self.registry or not (entry or releases):
                    raise PlatformError('找不到可导出的活动包。', 404)
                manifest = entry['manifest'] if entry and entry['state'] != 'deleted' else releases[0]
                return send_file(export_package(manifest), mimetype='application/zip', as_attachment=True,
                                 download_name=f'{manifest.id}-{manifest.version}.zip')

        @self.app.post('/api/platforms/<platform_id>/delete')
        def delete_platform(platform_id):
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict) or payload.get('confirmation') != platform_id:
                raise PlatformError('请输入活动标识，确认永久移除此活动。')
            self.delete(platform_id)
            return jsonify(platforms=self.public(), message='活动已永久移除，任务、活动数据和模块均已清理。')

    def install(self, payload):
        with self.lock:
            def validate(manifest):
                builtin = self.catalog.get(manifest.id)
                releases = self.releases.get(manifest.id, [])
                available = [*releases, builtin] if builtin else releases
                entry = self.platforms.get(manifest.id)
                previous = max(available, key=lambda item: item.version_key, default=entry['manifest'] if entry else None)
                self._validate_identity(manifest, previous)
                if previous and manifest.id not in self.registry and manifest.version_key <= previous.version_key:
                    raise PlatformError(f'更新须高于现有版本 {previous.version}。', 409)

            try:
                manifest = self.packages.install(payload, validate)
            except OSError as error:
                raise PlatformError('无法保存活动包，请检查数据目录权限或可用空间。', 409) from error
            if manifest.id in self.registry:
                updated = {key: value for key, value in self.registry.items() if key != manifest.id}
                if self.storage is not None:
                    try:
                        self.storage.save_platform_registry(updated)
                    except LoginStorageError:
                        shutil.rmtree(manifest.folder.parent)
                        raise
                self.registry = updated
            self.releases.setdefault(manifest.id, []).insert(0, manifest)
            self.failures.pop(manifest.id, None)
            return manifest

    def public(self):
        with self.lock:
            rows = []
            for platform_id, entry in self.platforms.items():
                if entry['state'] == 'deleted' and platform_id not in self.registry:
                    continue
                meta = entry['metadata']
                row = {key: entry[key] for key in ('id', 'title', 'home', 'temporary', 'state', 'navigation')}
                row['version'] = entry['manifest'].version
                enabled = self.features is None or self.features.enabled(platform_id)
                row.update(active=enabled and bool(meta.get('active')) and entry['state'] == 'running', enabled=enabled,
                           end_at=meta.get('end_at'), end_reason=meta.get('end_reason'),
                           server_time=self.now().isoformat())
                if entry['state'] == 'deleted':
                    row.update(deleted_at=self.registry[platform_id].get('deleted_at'),
                               removal_pending=entry['folder'].exists() or bool(self.releases.get(platform_id)))
                rows.append(row)
            visible = {row['id']: row for row in rows}
            for platform_id, releases in self.releases.items():
                latest = releases[0]
                if platform_id in self.registry:
                    continue
                row = visible.get(platform_id)
                if row is None:
                    row = {key: getattr(latest, key) for key in ('id', 'title', 'home', 'temporary', 'navigation')}
                    row.update(state='pending', active=False, enabled=False)
                    rows.append(row)
                if platform_id in self.failures:
                    row['failed_version'] = self.failures[platform_id]
                    if row['state'] == 'pending':
                        row['state'] = 'load-failed'
                elif row.get('version') is None or latest.version_key > tuple(map(int, row['version'].split('.'))):
                    row['pending_version'] = latest.version
            for platform_id, tombstone in self.registry.items():
                if platform_id not in visible:
                    rows.append({**deepcopy(tombstone), 'id': platform_id, 'state': 'deleted',
                                 'active': False, 'server_time': self.now().isoformat(),
                                 'removal_pending': bool(self.releases.get(platform_id))})
            return self.features.order_rows(rows) if self.features is not None else rows

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

    def login_restored(self, login_id):
        with self.lock:
            callbacks = [entry['controller'].scheduler.login_restored for entry in self.platforms.values()
                         if entry['state'] == 'running'
                         and callable(getattr(entry['controller'].scheduler, 'login_restored', None))]
        for callback in callbacks:
            callback(login_id)

    def delete(self, platform_id):
        with self.lock:
            entry = self.platforms.get(platform_id)
            if entry is None:
                releases = self.releases.get(platform_id)
                if not releases:
                    if platform_id in self.registry:
                        return
                    raise PlatformError('活动不存在或已经移除。', 404)
                entry = self._entry(releases[0], state='pending')
                self.platforms[platform_id] = entry
            controller = entry['controller']
            if controller is not None and not entry['stopped']:
                controller.deleted = True
                controller.stop(wait=True)
                entry['stopped'] = True
            elif controller is not None:
                controller.deleted = True
            if self.storage is not None:
                storage = self._package_storage(platform_id) if self.packages is not None else self.storage
                storage.delete_activity_data(platform_id)
                storage.delete_credentials(scope=None if isinstance(storage, ActivityPackageStorage) else platform_id)
            tombstone = {key: entry[key] for key in ('title', 'home', 'temporary')}
            tombstone.update(deleted_at=self.now().isoformat())
            updated = {**self.registry, platform_id: tombstone}
            if self.storage is not None:
                self.storage.save_platform_registry(updated)
            self.registry = updated
            entry['state'] = 'deleted'
            builtin = self.catalog.get(platform_id)
            if entry['folder'] != entry['manifest'].folder:
                raise PlatformError('活动目录发生变化，任务已停止，请检查后重新移除。', 409)
            if builtin is not None:
                self._remove_module(builtin, builtin.module.rsplit('.', 1)[0])
            if self.packages is not None:
                try:
                    self.packages.remove(platform_id)
                except OSError as error:
                    raise PlatformError('活动包仍被占用，请稍后继续清理。', 409) from error
                self.releases.pop(platform_id, None)
                self.failures.pop(platform_id, None)
            self._unload(entry['namespace'])
            entry['controller'] = None

    def _remove_module(self, manifest, namespace):
        folder = manifest.folder
        # Validate the resolved, absolute target after all workers have stopped.
        if (self.root_dir.resolve() != self.root_dir or folder.is_symlink()
                or folder.resolve() != folder or folder.parent != self.root_dir):
            raise PlatformError('活动目录发生变化，任务已停止，请检查后重新移除。', 409)
        try:
            if folder.exists():
                shutil.rmtree(folder)
        except OSError as error:
            raise PlatformError('任务和数据已清理，活动模块仍被占用，请稍后重新移除。', 409) from error
        self._unload(namespace)

    @staticmethod
    def _unload(prefix):
        if prefix is None:
            return
        for name in list(sys.modules):
            if name == prefix or name.startswith(prefix + '.'):
                del sys.modules[name]
        invalidate_caches()
