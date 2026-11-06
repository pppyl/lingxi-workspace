import json
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from http_transport import requests
from recharge.cmcc import SHANGHAI
from activities.daily_gold.client import (DailyGoldClient, DailyGoldError,
    ACT_ID, ENTRY, PLATFORM, campaign_config)
from credential_cache import CredentialCache
from monthly_coupon_auth import DEFAULT_ENTRY_URL, guizhou_session


class Session:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.headers, self.cookies = {}, requests.Cookies()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: value)

    def get(self, url, **kwargs):
        return self.request('get', url, **kwargs)

    def close(self):
        pass


def ok(value):
    return {'code': 200, 'data': value}


class DailyGoldProtocolTests(unittest.TestCase):
    def client(self, replies, cache=None):
        session = Session(replies)
        client = DailyGoldClient(lambda: {}, credential_cache=cache,
            now=lambda: datetime(2026, 11, 6, 0, 0, 1, tzinfo=SHANGHAI))
        client.session = session
        return client, session

    def test_read_progress_and_real_prize_history_without_draw(self):
        client, session = self.client([ok(1), ok([{'actResource': {'resourceName': '2元话费满减券'},
            'createTime': '2026-11-05 00:00:02'}])])
        result = client.observe()
        self.assertFalse(result['signed'])
        self.assertEqual(result['remaining'], 1)
        self.assertEqual(result['prizes'][0]['name'], '2元话费满减券')
        self.assertEqual([call[1].rsplit('/', 1)[1] for call in session.calls],
                         ['restParticipateNum', 'initActivities'])

    def test_first_draw_uses_current_agreement_confirmation_and_fresh_scene_ticket(self):
        client, session = self.client([ok({'sceneIdx': 1}),
            ok({'agrId': 23212, 'cfmLogId': 'current-confirmation'}), ok(None),
            ok({'sceneIdx': 2, 'validCode': 'fresh-bound-ticket'}),
            ok({'prizeName': '1元话费满减券', 'prizeId': 'official-prize'})])
        result = client.signin()
        self.assertTrue(result['signed'])
        self.assertEqual(result['prizes'][0]['name'], '1元话费满减券')
        self.assertEqual(session.calls[2][2]['json']['cfmLogId'], 'current-confirmation')
        self.assertEqual(session.calls[-1][0], 'get')
        self.assertEqual(session.calls[-1][2]['params']['validCode'], 'fresh-bound-ticket')
        self.assertTrue(all(call[2].get('params', call[2].get('json'))['actId'] == ACT_ID for call in session.calls))

    def test_submission_timeout_is_uncertain_and_never_replayed(self):
        cache = Mock()
        client, session = self.client([ok({'sceneIdx': 2, 'validCode': 'one-ticket'}),
                                      requests.RequestsError('timeout')], cache)
        with self.assertRaises(DailyGoldError) as caught:
            client.signin()
        self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(session.calls), 2)
        cache.recover.assert_not_called()

    def test_explicit_read_auth_rejection_recovers_once(self):
        cache = Mock()
        cache.recover.return_value = True
        client, session = self.client([{'code': 401}, {'code': 401}], cache)
        with self.assertRaises(DailyGoldError) as caught:
            client.observe()
        self.assertEqual(caught.exception.status, 401)
        cache.recover.assert_called_once_with(session)

    def test_submission_auth_rejection_does_not_relogin_and_replay(self):
        cache = Mock()
        client, session = self.client([ok({'sceneIdx': 2, 'validCode': 'one-ticket'}), {'code': 401}], cache)
        with self.assertRaises(DailyGoldError):
            client.signin()
        cache.recover.assert_not_called()

    def test_verification_stops_before_draw(self):
        client, session = self.client([{'code': 505}])
        with self.assertRaises(DailyGoldError) as caught:
            client.signin()
        self.assertEqual(caught.exception.code, 'VERIFICATION_REQUIRED')
        self.assertEqual(len(session.calls), 1)

    def test_unknown_scene_or_malformed_agreement_never_draws(self):
        for replies in [[ok({'sceneIdx': -1})], [ok({'sceneIdx': 1}), ok({'agrId': 23212})]]:
            client, session = self.client(replies)
            with self.assertRaises(DailyGoldError):
                client.signin()
            self.assertFalse(any(call[1].endswith('/draw') for call in session.calls))

    def test_public_template_updates_deadline_and_component_ids(self):
        page = {'list': [{'id': 'fresh-template', 'boardData': {'compList': [
            {'id': 'fresh-draw', 'cpName': 'BigWheelPointNew', 'options': {'event': {'sceneList': [
                {'condList': [{'condType': 7}]}]}}},
            {'cpName': 'Agree', 'options': {'data': {'agrId': '[99]'}}},
            {'cpName': 'RichText', 'options': {'config': {'text': '<p>活动有效期：2026年8月-2027年4月30日23:59:59。</p>'}}}
        ]}}]}
        session = Session([ok({'pageDetail': json.dumps(page)})])
        value = campaign_config(session=session)
        self.assertEqual(value['end_at'], '2027-04-30T23:59:59+08:00')
        self.assertEqual(value['protocol']['draw_comp'], 'fresh-draw')

    def test_compatible_provincial_bearer_is_reused_across_pages_and_rotation(self):
        snapshot = {'id': 'test-owner', 'phone': '13800000000', 'cookie': 'JSESSIONID=first'}
        cache = CredentialCache()
        previous = Session([])
        previous.headers['AuthorizationAweb'] = 'Bearer valid-provincial-bearer'
        cache.connect(PLATFORM+'|'+DEFAULT_ENTRY_URL, snapshot, previous, lambda: {'serviceNum': snapshot['phone']},
                      headers=('AuthorizationAweb',))
        session = Session([])
        with patch('monthly_coupon_auth._guizhou_login') as login:
            with guizhou_session(lambda: {**snapshot, 'cookie': 'JSESSIONID=rotated'}, ENTRY,
                                 session_factory=lambda: session, credential_cache=cache):
                self.assertEqual(session.headers['AuthorizationAweb'], 'Bearer valid-provincial-bearer')
                self.assertEqual(session.headers['Referer'], ENTRY)
            login.assert_not_called()


if __name__ == '__main__':
    unittest.main()
