from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from flask import Flask
from login_storage import DatabaseLoginStorage
from platform_manager import PlatformManager, PlatformError

ROOT = Path(__file__).resolve().parents[1]


class PlatformManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)/'activities'
        self.root.mkdir()
        self.storage = DatabaseLoginStorage(Path(self.temp.name)/'state.sqlite3',
            protect=lambda value:b'encrypted:'+value,
            unprotect=lambda value:value.removeprefix(b'encrypted:'))
        self.controllers, self.events = {}, []
        self.app = Flask('platform-tests', template_folder=str(ROOT/'templates'))
        self.app.config['TESTING'] = True
        self.clock = datetime(2026, 11, 1, tzinfo=timezone.utc)
        self.credentials, self.accounts = Mock(), Mock()
        self.importer = patch('platform_manager.import_module', side_effect=self.module)
        self.imported = self.importer.start()
        self.addCleanup(self.importer.stop)

    def activity(self, name='sample', platform_id='sample', *, temporary=True):
        folder = self.root/name
        folder.mkdir()
        (folder/'manifest.json').write_text(json.dumps({
            'id': platform_id, 'title': '短期测试活动', 'home': '/sample',
            'module': f'activities.{name}.plugin', 'temporary': temporary}), 'utf-8')
        (folder/'plugin.py').write_text('# selected activity module', 'utf-8')
        (folder/'client.py').write_text('# selected activity client', 'utf-8')
        return folder

    def module(self, name):
        platform_id = name.split('.')[1].replace('_', '-')
        parent = self

        class Controller:
            def __init__(self):
                self.deleted, self.ended, self.metadata_calls = False, False, 0
                self.busy = set()
                self.scheduler = SimpleNamespace(start=lambda: parent.events.append(('start', platform_id)))

            def metadata(self):
                self.metadata_calls += 1
                return {'active': not self.ended, 'ended': self.ended,
                        'end_at': '2026-11-30T23:59:59+08:00', 'server_time': parent.clock.isoformat()}

            def stop(self, *, wait):
                parent.events.append(('stop', platform_id, wait, self.deleted))

            def busy_login(self, login_id):
                return login_id in self.busy

            def forget(self, login_id):
                parent.events.append(('forget', platform_id, login_id))

        controller = Controller()
        self.controllers[platform_id] = controller

        def register(app, credentials, accounts, storage):
            self.assertIs(app, self.app)
            self.assertIs(credentials, self.credentials)
            self.assertIs(accounts, self.accounts)
            self.assertIs(storage, self.storage)
            self.events.append(('register', platform_id))
            return controller

        return SimpleNamespace(register=register)

    def manager(self):
        manager = PlatformManager(self.app, root_dir=self.root, storage=self.storage,
            credentials=self.credentials, accounts=self.accounts, now=lambda:self.clock)
        self.addCleanup(manager.shutdown)
        return manager

    def test_manifest_discovers_module_without_starting_its_background_work(self):
        self.activity()
        manager = self.manager()
        self.assertEqual(self.imported.call_args.args, ('activities.sample.plugin',))
        self.assertEqual(self.events, [('register', 'sample')])
        self.assertEqual(manager.public()[0]['state'], 'running')
        self.assertTrue(manager.public()[0]['active'])
        self.assertNotIn('module', manager.public()[0])
        self.assertNotIn('folder', manager.public()[0])

    def test_start_is_idempotent_and_shutdown_waits_for_activity_workers(self):
        self.activity()
        manager = self.manager()
        manager.start()
        manager.start()
        manager.shutdown()
        self.assertEqual(self.events.count(('start', 'sample')), 1)
        self.assertIn(('stop', 'sample', True, False), self.events)
        self.assertFalse(manager.thread.is_alive())

    def test_expired_activity_stops_once_and_is_never_polled_again(self):
        folder = self.activity()
        manager = self.manager()
        controller = self.controllers['sample']
        controller.ended = True
        manager.tick()
        calls = controller.metadata_calls
        manager.tick()
        rows = manager.public()
        self.assertEqual(controller.metadata_calls, calls)
        self.assertEqual(self.events.count(('stop', 'sample', True, False)), 1)
        self.assertEqual(rows[0]['state'], 'expired')
        self.assertFalse(rows[0]['active'])
        self.assertTrue(folder.exists(), 'Expiry must stop work, not delete an activity without the user action')
        manager.start()
        self.assertNotIn(('start', 'sample'), self.events)

    def test_busy_and_forget_are_forwarded_to_independent_controllers(self):
        self.activity()
        manager = self.manager()
        self.controllers['sample'].busy.add('account-a')
        self.assertTrue(manager.busy_login('account-a'))
        self.assertFalse(manager.busy_login('account-b'))
        manager.forget('account-b')
        self.assertIn(('forget', 'sample', 'account-b'), self.events)

    def test_permanent_delete_removes_only_selected_code_data_and_module_cache(self):
        selected = self.activity()
        other = self.activity('other', 'other')
        manager = self.manager()
        self.storage.save_activity_data('sample', {'accounts': {'a': {'cookie': 'sensitive-login'}}})
        self.storage.save_activity_data('other', {'keep': 'other state'})
        with self.storage._connect() as connection, connection:
            connection.execute('INSERT INTO account_sessions(login_id,payload) VALUES(?,?)', ('shared-account', b'unchanged'))
        module_name = 'activities.sample.client'
        sys.modules[module_name] = SimpleNamespace()
        self.addCleanup(lambda:sys.modules.pop(module_name, None))
        sys.modules['activities.other.client'] = SimpleNamespace()
        self.addCleanup(lambda:sys.modules.pop('activities.other.client', None))
        controller = self.controllers['sample']
        manager.delete('sample')
        self.assertTrue(controller.deleted)
        self.assertIn(('stop', 'sample', True, True), self.events)
        self.assertFalse(selected.exists())
        self.assertTrue(other.exists())
        self.assertNotIn(module_name, sys.modules)
        self.assertIn('activities.other.client', sys.modules)
        self.assertIsNone(self.storage.load_activity_data('sample'))
        self.assertEqual(self.storage.load_activity_data('other'), {'keep': 'other state'})
        registry = self.storage.load_platform_registry()
        self.assertEqual(set(registry['sample']), {'title', 'home', 'temporary', 'deleted_at'})
        self.assertNotIn('sensitive-login', json.dumps(registry))
        with self.storage._connect() as connection:
            self.assertEqual(connection.execute('SELECT login_id,payload FROM account_sessions').fetchall(), [('shared-account', b'unchanged')])
        deleted = next(row for row in manager.public() if row['id'] == 'sample')
        self.assertEqual(deleted['state'], 'deleted')
        self.assertFalse(deleted['active'])
        self.assertFalse(deleted['removal_pending'])

    def test_delete_waits_for_inflight_request_before_removing_state_and_source(self):
        folder = self.activity()
        manager = self.manager()
        self.storage.save_activity_data('sample', {'state': 'before-flight'})
        waiting, release, finished = Event(), Event(), Event()
        controller = self.controllers['sample']

        def stop(*, wait):
            self.assertTrue(wait)
            self.assertTrue(controller.deleted)
            waiting.set()
            if not release.wait(3):
                raise AssertionError('Test worker was not released')
            self.storage.save_activity_data('sample', {'state': 'flight-finished'})

        controller.stop = stop

        def remove():
            manager.delete('sample')
            finished.set()

        worker = Thread(target=remove)
        worker.start()
        self.assertTrue(waiting.wait(1))
        self.assertTrue(folder.exists())
        self.assertIsNotNone(self.storage.load_activity_data('sample'))
        self.assertFalse(finished.is_set())
        release.set()
        worker.join(2)
        self.assertTrue(finished.is_set())
        self.assertFalse(folder.exists())
        self.assertIsNone(self.storage.load_activity_data('sample'))

    def test_api_requires_exact_confirmation_before_any_delete(self):
        folder = self.activity()
        manager = self.manager()
        client = self.app.test_client()
        for payload in ({}, {'confirmation': 'other'}, {'confirmation': 'sample '}):
            response = client.post('/api/platforms/sample/delete', json=payload)
            self.assertEqual(response.status_code, 400)
            self.assertTrue(folder.exists())
            self.assertFalse(self.controllers['sample'].deleted)
        response = client.post('/api/platforms/sample/delete', json={'confirmation': 'sample'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['platforms'][0]['state'], 'deleted')
        self.assertFalse(folder.exists())
        self.assertEqual(client.get('/api/platforms').status_code, 200)
        self.assertEqual(client.get('/platforms').status_code, 200)

    def test_unrecognized_module_and_path_are_rejected_before_import(self):
        folder = self.activity()
        path = folder/'manifest.json'
        manifest = json.loads(path.read_text('utf-8'))
        manifest['module'] = 'unrelated_module.plugin'
        path.write_text(json.dumps(manifest), 'utf-8')
        with self.assertRaises(PlatformError):
            self.manager()
        self.imported.assert_not_called()

    def test_discovery_rejects_junction_alias_even_when_target_is_another_activity(self):
        folder = self.activity()
        other = self.activity('other', 'other')
        resolve = Path.resolve

        def observed_resolve(path, *args, **kwargs):
            return other if path == folder else resolve(path, *args, **kwargs)

        with patch.object(Path, 'resolve', observed_resolve):
            with self.assertRaises(PlatformError):
                self.manager()
        self.assertTrue((other/'client.py').exists())

    def test_delete_rejects_retargeted_directory(self):
        folder = self.activity()
        manager = self.manager()
        outside = Path(self.temp.name)/'outside'
        outside.mkdir()
        (outside/'keep.txt').write_text('do not delete', 'utf-8')
        manager.platforms['sample']['folder'] = outside
        with self.assertRaises(PlatformError):
            manager.delete('sample')
        self.assertTrue((outside/'keep.txt').exists())
        self.assertTrue(folder.exists())

    def test_tombstone_survives_restart_and_removes_reintroduced_package_without_import(self):
        folder = self.activity()
        manager = self.manager()
        manager.delete('sample')
        self.activity()
        self.app = Flask('restart-test', template_folder=str(ROOT/'templates'))
        self.imported.reset_mock()
        restarted = self.manager()
        self.imported.assert_not_called()
        self.assertFalse(folder.exists())
        self.assertEqual(restarted.public()[0]['state'], 'deleted')
        self.assertFalse(restarted.public()[0]['removal_pending'])

    def test_restart_with_no_manifest_still_shows_deleted_tombstone(self):
        self.storage.save_platform_registry({'old-platform': {'title': '旧活动', 'home': '/old',
            'temporary': True, 'deleted_at': self.clock.isoformat()}})
        manager = self.manager()
        row = manager.public()[0]
        self.assertEqual(row['id'], 'old-platform')
        self.assertEqual(row['state'], 'deleted')
        self.imported.assert_not_called()

    def test_file_in_use_keeps_tombstone_and_can_retry_cleanup(self):
        folder = self.activity()
        manager = self.manager()
        with patch('platform_manager.shutil.rmtree', side_effect=PermissionError('file in use')):
            with self.assertRaises(PlatformError):
                manager.delete('sample')
        self.assertTrue(folder.exists())
        self.assertEqual(manager.public()[0]['state'], 'deleted')
        self.assertTrue(manager.public()[0]['removal_pending'])
        manager.delete('sample')
        self.assertFalse(folder.exists())


class PlatformStorageTests(unittest.TestCase):
    def test_activity_payloads_are_encrypted_independent_and_registry_has_own_row(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = DatabaseLoginStorage(Path(temp)/'state.sqlite3',
                protect=lambda value:b'encrypted:'+value[::-1],
                unprotect=lambda value:value.removeprefix(b'encrypted:')[::-1])
            first = {'login': 'cookie-secret', 'dates': ['20261101']}
            storage.save_activity_data('a', first)
            storage.save_activity_data('b', {'keep': True})
            storage.save_platform_registry({'removed': {'title': 'old'}})
            self.assertEqual(storage.load_activity_data('a'), first)
            self.assertEqual(storage.load_platform_registry(), {'removed': {'title': 'old'}})
            with storage._connect() as connection:
                payload = connection.execute('SELECT payload FROM activity_state WHERE platform_id="a"').fetchone()[0]
            self.assertTrue(payload.startswith(b'encrypted:'))
            self.assertNotIn(b'cookie-secret', payload)
            storage.delete_activity_data('a')
            self.assertIsNone(storage.load_activity_data('a'))
            self.assertEqual(storage.load_activity_data('b'), {'keep': True})
            self.assertEqual(storage.load_platform_registry(), {'removed': {'title': 'old'}})


if __name__ == '__main__':
    unittest.main()
