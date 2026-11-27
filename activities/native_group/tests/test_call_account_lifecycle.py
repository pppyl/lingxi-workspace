"""Local route, real credential cache and on-disk restart regression tests."""
from collections import Counter
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from flask import Flask

from activity_packages import ActivityPackageStorage
from activities.native_group.client import NativeGroupClient, SCOPE as AUTH_SCOPE
from activities.native_group.phone_call import CallSession, CALL_GRANT_PATH
from activities.native_group.plugin import register
from activities.native_group.tests.test_native_group import observation
from workspace.accounts.login import AppLoginError, REFRESH_PATH
from workspace.accounts.storage import DatabaseLoginStorage


class CallAccountLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix='lingxi-call-lifecycle-')
        self.addCleanup(directory.cleanup)
        self.database = DatabaseLoginStorage(Path(directory.name) / 'accounts.sqlite3',
            protect=lambda value: b'test:' + value[::-1], unprotect=lambda value: value[5:][::-1])
        self.storage = ActivityPackageStorage(self.database, 'native-group')
        self.snapshots, self.authorized, self.logins, self.requests = {}, {}, [], []
        self.lose_grant_response = False
        self.device = {'role_holder': 'com.greenpoint.android.mc10086.activity',
                       'device_id': 'synthetic-real-device', 'xk': 'synthetic-real-key'}

        def initialize(client, http, snapshot):
            self.logins.append(snapshot['phone'])
            http.cookies.set('HLWHD_SESSION_TOKEN', 'synthetic-' + snapshot['id'], domain='wx.10086.cn')
            return self.identity(snapshot)

        def activity(client, http, path, body=None, **options):
            snapshot = http._credential_binding[1]
            identity = self.identity(snapshot)
            if path == 'userInfo':
                return identity
            if path == 'activityInfo':
                return observation({'groupSize': 3, 'groupMembers': [{'phone': identity['phone'],
                    'authTaskStatus': '1' if self.authorized.get(snapshot['phone']) else None,
                    'callTaskStatus': None}]})['campaign']
            if path in {'myPrizes', 'drawTasks'}:
                return []
            self.fail('Unexpected activity request: ' + path)

        def app_request(session, path, body, **options):
            snapshot = next(value for value in self.snapshots.values() if value['phone'] == session.phone)
            self.assertEqual(session.cookie, snapshot['cookie'])
            self.assertEqual((session.device_id, session.xk), (self.device['device_id'], self.device['xk']))
            self.assertEqual(body['cellNum'], snapshot['phone'])
            self.requests.append((session.phone, path))
            if path == CALL_GRANT_PATH:
                self.assertIs(body['isCallGrant'], True)
                self.authorized[session.phone] = True
                if self.lose_grant_response:
                    self.lose_grant_response = False
                    raise AppLoginError('simulated connection loss after accepted grant')
            else:
                self.assertEqual(path, REFRESH_PATH)
            return 200, {'retCode': '000000'}, {}

        for target, method, implementation in (
            (NativeGroupClient, '_authenticate', initialize),
            (NativeGroupClient, '_call', activity), (CallSession, 'call', app_request)):
            stub = patch.object(target, method, autospec=True, side_effect=implementation)
            stub.start()
            self.addCleanup(stub.stop)
        rules = patch.object(NativeGroupClient, 'rules', return_value='synthetic activity rules')
        rules.start()
        self.addCleanup(rules.stop)

    @staticmethod
    def identity(snapshot):
        return {'phone': snapshot['phone'][:3] + '****' + snapshot['phone'][-4:], 'channel': 'app'}

    def add_account(self, key, phone):
        self.snapshots[key] = {'id': key, 'phone': phone, 'cookie': 'JSESSIONID=synthetic-' + key,
            'revision': 1, 'primary_status': 'active', 'profile': {},
            'app_context': {'device_id': 'own-' + key, 'xk': 'own-key-' + key}}

    def start_client(self):
        app = Flask(__name__)
        controller = register(app, lambda key, **options: deepcopy(self.snapshots.get(key)),
            lambda: [{'id': key, 'name': key, 'status': 'active'} for key in self.snapshots], self.storage)
        self.addCleanup(controller.stop)
        return app.test_client(), controller

    def prepare(self, browser, key):
        return browser.post('/api/native-group/run', json={'login_id': key, 'kind': 'call_prepare',
            'destination': '13800138999', 'confirmed': True})

    def test_replace_every_account_then_restart_and_prepare_new_batches_without_manual_grants(self):
        self.add_account('old', '13800138000')
        browser, controller = self.start_client()
        controller.scheduler._change(lambda data: data.update(call_context=deepcopy(self.device)))
        self.assertEqual(self.prepare(browser, 'old').status_code, 200)
        controller.forget('old')
        self.snapshots.clear()
        empty = browser.get('/api/native-group/status').get_json()
        self.assertEqual(empty['accounts'], [])
        self.assertTrue(empty['call_authorization_available'])
        controller.stop()

        browser, controller = self.start_client()
        self.assertTrue(browser.get('/api/native-group/status').get_json()['call_authorization_available'])
        self.add_account('new', '13900138001')
        original = deepcopy(self.snapshots['new'])
        prepared = self.prepare(browser, 'new')
        self.assertEqual(prepared.status_code, 200, prepared.get_json())
        draft = prepared.get_json()['accounts'][0]['phone_call']
        self.assertEqual(draft['stage'], 'prepared')
        self.assertEqual(self.snapshots['new'], original)
        self.assertIsNone(self.storage.load_credentials(AUTH_SCOPE, 'old'))
        controller.stop()

        browser, controller = self.start_client()
        self.snapshots['new'].update(revision=2, cookie='JSESSIONID=synthetic-new-version')
        self.assertEqual(self.prepare(browser, 'new').get_json()['accounts'][0]['phone_call']['id'], draft['id'])
        controller.forget('new')
        self.snapshots.clear()
        self.add_account('next', '13700138002')
        result = self.prepare(browser, 'next')
        self.assertEqual(result.status_code, 200, result.get_json())
        self.assertNotEqual(result.get_json()['accounts'][0]['phone_call']['id'], draft['id'])
        self.assertEqual(self.storage.load_activity_data('native-group')['call_context'], self.device)
        self.assertEqual(Counter(self.logins), Counter({'13800138000': 1, '13900138001': 1, '13700138002': 1}))
        grants = [phone for phone, path in self.requests if path == CALL_GRANT_PATH]
        self.assertEqual(Counter(grants), Counter({'13800138000': 1, '13900138001': 1, '13700138002': 1}))

    def test_restart_after_accepted_grant_lost_response_reads_official_state_without_replay(self):
        self.add_account('new', '13800138000')
        browser, controller = self.start_client()
        controller.scheduler._change(lambda data: data.update(call_context=deepcopy(self.device)))
        self.lose_grant_response = True
        result = self.prepare(browser, 'new')
        self.assertEqual(result.status_code, 409)
        self.assertTrue(result.get_json()['uncertain'])
        pending = self.storage.load_activity_data('native-group')
        self.assertEqual(pending['pending']['new']['kind'], 'call_authorize')
        self.assertFalse(pending['phone_calls'])
        controller.stop()

        browser, _ = self.start_client()
        result = self.prepare(browser, 'new')
        self.assertEqual(result.status_code, 200, result.get_json())
        account = result.get_json()['accounts'][0]
        self.assertIsNone(account['pending'])
        self.assertEqual(account['phone_call']['stage'], 'prepared')
        self.assertEqual(self.requests, [('13800138000', REFRESH_PATH), ('13800138000', CALL_GRANT_PATH)])
        self.assertEqual(self.logins, ['13800138000'])

    def test_two_new_accounts_share_device_configuration_and_keep_separate_identities(self):
        from concurrent.futures import ThreadPoolExecutor
        self.add_account('a', '13800138000')
        self.add_account('b', '13900138001')
        browser, controller = self.start_client()
        controller.scheduler._change(lambda data: data.update(call_context=deepcopy(self.device)))
        def prepare(key):
            with browser.application.test_client() as client:
                response = self.prepare(client, key)
                self.assertEqual(response.status_code, 200, response.get_json())
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(prepare, ('a', 'b')))
        drafts = self.storage.load_activity_data('native-group')['phone_calls']
        self.assertNotEqual(drafts['a']['id'], drafts['b']['id'])
        self.assertEqual(Counter(self.requests), Counter({('13800138000', REFRESH_PATH): 1,
            ('13800138000', CALL_GRANT_PATH): 1, ('13900138001', REFRESH_PATH): 1,
            ('13900138001', CALL_GRANT_PATH): 1}))


if __name__ == '__main__':
    unittest.main()
