"""The official payment window must bound pending entries and cached links."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from coupon_use import CouponUseManager, CouponUseError, PAYMENT_WINDOW_SECONDS
from tests.test_coupon_use import Storage


LOGIN = 'account-a'
COUPON = 'a'*24
KEY = hashlib.sha256(json.dumps([LOGIN,COUPON],separators=(',',':')).encode()).hexdigest()


class PendingExpiryTests(unittest.TestCase):
    def setUp(self):
        self.now = 2000000000
        clock = patch('coupon_use.time.time',side_effect=lambda:self.now)
        clock.start();self.addCleanup(clock.stop)
        self.record = {'state':'payment_required','order_id':'old-order','login_id':LOGIN,
            'coupon_id':COUPON,'coupon_name':'6元话费加赠券','charge_amount':'30.00',
            'created_at':self.now-100,'result':{'state':'payment_required','order_id':'old-order'},
            'payment':{'state':'payment_required','order_id':'old-order','amount':'30.00',
                'payment_type':'alipay_web','payment_url':'https://mclient.alipay.com/cashierRoutePay.htm?fixture'}}

    def manager(self):
        self.storage = Storage({KEY:deepcopy(self.record)})
        return CouponUseManager(lambda _: {'credential_revision':1},self.storage)

    def test_startup_migrates_historical_pending_orders_without_deleting_submission_receipts(self):
        self.record['created_at'] = self.now-PAYMENT_WINDOW_SECONDS-1
        manager = self.manager()
        self.assertEqual(manager.pending(LOGIN),[])
        row = self.storage.data[KEY]
        self.assertEqual(row['state'],'closed')
        self.assertEqual(row['closed_reason'],'payment_window_ended')
        self.assertEqual(row['order_id'],'old-order')
        self.assertNotIn('payment',row)
        self.assertNotIn('paid',row)
        self.assertEqual(len(self.storage.writes),1)
        restarted = CouponUseManager(lambda _:None,self.storage)
        self.assertEqual(restarted.pending(LOGIN),[])
        self.assertEqual(len(self.storage.writes),1)

    def test_exact_deadline_closes_order_and_challenge_without_reaching_payment_gateway(self):
        manager = self.manager()
        before, = manager.pending(LOGIN)
        self.assertEqual(before['expires_in'],PAYMENT_WINDOW_SECONDS-100)
        manager.payment_challenges[(LOGIN,'old-order')] = {'private':'captcha-context'}
        self.now = before['expires_at']
        with patch('coupon_use.CouponAlipay') as gateway:
            self.assertEqual(manager.pending(LOGIN),[])
            self.assertEqual(manager.payment(LOGIN,'old-order')['state'],'closed')
            self.assertEqual(manager.payment(LOGIN,'old-order',refresh=True)['state'],'closed')
            gateway.assert_not_called()
        self.assertEqual(manager.payment_challenges,{})
        self.assertNotIn('payment',self.storage.data[KEY])

    def test_newer_live_order_keeps_original_deadline_and_saved_link(self):
        manager = self.manager()
        deadline = manager.pending(LOGIN)[0]['expires_at']
        with patch('coupon_use.CouponAlipay') as gateway:
            self.assertEqual(manager.payment(LOGIN,'old-order'),self.record['payment'])
            self.now += 50
            self.assertEqual(manager.pending(LOGIN)[0]['expires_at'],deadline)
            self.assertEqual(manager.payment(LOGIN,'old-order',refresh=False),self.record['payment'])
            gateway.assert_not_called()

    def test_explicit_payment_deadline_takes_precedence_over_fallback(self):
        self.record['payment_expires_at'] = self.now-1
        manager = self.manager()
        self.assertEqual(manager.pending(LOGIN),[])
        self.assertEqual(manager.data[KEY]['payment_expires_at'],self.now-1)

    def test_unknown_legacy_creation_time_does_not_invent_an_expiry_or_payment_result(self):
        self.record.pop('created_at')
        manager = self.manager()
        item, = manager.pending(LOGIN)
        self.assertIsNone(item['expires_at'])
        self.assertIsNone(item['expires_in'])
        self.assertEqual(manager.data[KEY]['state'],'payment_required')
        self.assertEqual(self.storage.writes,[])

    def test_expired_order_cannot_be_resumed_by_prepare_or_replaced_without_reading_official_coupon(self):
        self.record['created_at'] = self.now-PAYMENT_WINDOW_SECONDS
        manager = self.manager()
        with patch('coupon_use.CouponClient') as wallet:
            wallet.return_value._session.side_effect = CouponUseError('卡券已不存在','COUPON_NOT_FOUND',404)
            with self.assertRaises(CouponUseError) as caught:manager.prepare(LOGIN,COUPON)
            self.assertEqual(caught.exception.code,'COUPON_NOT_FOUND')
            wallet.return_value._session.assert_called_once()
        self.assertEqual(manager.data[KEY]['state'],'closed')
        self.assertEqual(manager.data[KEY]['order_id'],'old-order')

    def test_closed_order_is_still_isolated_from_another_account(self):
        self.record['created_at'] = self.now-PAYMENT_WINDOW_SECONDS
        manager = self.manager()
        with patch('coupon_use.CouponAlipay') as gateway:
            with self.assertRaises(CouponUseError) as caught:manager.payment('another-account','old-order')
            self.assertEqual(caught.exception.code,'ORDER_NOT_FOUND')
            gateway.assert_not_called()

    def test_used_and_uncertain_receipts_are_not_treated_as_expired_pending_orders(self):
        for state in ('accepted','unknown','submitted'):
            with self.subTest(state=state):
                self.record.update(state=state,created_at=self.now-PAYMENT_WINDOW_SECONDS-1)
                manager = self.manager()
                self.assertEqual(manager.pending(LOGIN),[])
                self.assertIn(manager.data[KEY]['state'],('accepted','unknown'))


if __name__ == '__main__':unittest.main()
