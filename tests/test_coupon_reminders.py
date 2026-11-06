from datetime import datetime
import json
import unittest
from unittest.mock import Mock, patch

from coupon_client import CouponError, _item
from coupon_reminders import CouponReminders, SCOPE
from coupon_use import CouponUseManager
from recharge.cmcc import SHANGHAI
from tests.test_coupon_use import Environment, Storage, LOGIN, ROW


NOW = datetime(2026, 11, 6, 12, tzinfo=SHANGHAI)


class CouponReminderTests(unittest.TestCase):
    def setUp(self):
        self.storage = Storage()
        self.reminders = CouponReminders(self.storage)

    def observe(self, rows, quote=None, owner=LOGIN, state='available'):
        self.reminders.observe(owner, [_item(row) for row in rows], rows,
            state=state, quote=quote or Mock(side_effect=AssertionError('unexpected pricing query')))

    def summary(self, submissions=None):
        return self.reminders.summary([LOGIN], submissions or {}, NOW)

    def test_all_available_fee_exchange_amounts_and_versions_count_but_flow_does_not(self):
        rows = [{**ROW, 'pcardId':str(i), 'pCardNo':str(i), 'cardVersion':version,
                 'couponValue':amount} for i,(version,amount) in enumerate([('1','0.5'),('2','2'),('2','10')])]
        rows.append({**ROW, 'pcardId':'flow', 'pCardNo':'flow', 'busiType':'qy01', 'provPcardType':'ll01'})
        rows.append({**ROW, 'pcardId':'future', 'pCardNo':'future', 'timeFlag':'2'})
        self.observe(rows)
        self.assertEqual(self.summary()['total'], 3)

    def test_strict_discount_boundary_uses_paid_over_credited_amount(self):
        cases = [('boundary','91.00','100.00',False), ('below','90.99','100.00',True),
                 ('bonus','30.00','37.00',True), ('small-bonus','30.00','32.00',False),
                 ('reduction','18.00','20.00',True)]
        for label, payment, credit, expected in cases:
            with self.subTest(label=label):
                row = {**ROW, 'pCardType':'001', 'pcardId':label, 'pCardNo':label}
                self.observe([row], Mock(return_value={'payment':payment,'credit':credit}))
                self.assertEqual(self.summary()['total'], int(expected))

    def test_quote_is_reused_for_duplicate_rows_refresh_and_restart_without_private_coupon_codes(self):
        row = {**ROW, 'pCardType':'001'}
        quote = Mock(return_value={'payment':'18.00','credit':'20.00'})
        self.observe([row, row], quote)
        self.observe([row], quote)
        quote.assert_called_once()
        reloaded = CouponReminders(self.storage)
        self.assertEqual(reloaded.summary([LOGIN], {}, NOW)['total'], 1)
        self.assertNotIn(ROW['pCardNo'], json.dumps(self.storage.activities[SCOPE]))
        self.assertNotIn('msg_id', json.dumps(self.storage.activities[SCOPE]))

    def test_changed_official_rules_require_fresh_quote_and_accounts_stay_separate(self):
        row = {**ROW, 'pCardType':'001'}
        quote = Mock(return_value={'payment':'18.00','credit':'20.00'})
        self.observe([row], quote)
        self.observe([{**row, 'stallInfo':'new threshold'}], quote)
        self.observe([row], quote, owner='other')
        self.assertEqual(quote.call_count, 3)
        self.assertEqual(self.summary()['total'], 1)
        self.assertEqual(self.reminders.summary([LOGIN,'other'], {}, NOW)['total'], 2)

    def test_unavailable_used_expired_and_removed_coupons_are_not_counted(self):
        expired = {**ROW, 'pCardNo':'expired', 'pcardId':'expired', 'expireTime':'20261105120000'}
        self.observe([ROW, expired])
        self.assertEqual(self.summary()['total'], 1)
        self.observe([{**ROW, 'couponStatus':'4'}], state='history')
        self.assertEqual(self.summary()['total'], 0)
        self.observe([ROW])
        self.observe([])
        self.assertEqual(self.summary()['total'], 0)

    def test_submitted_accepted_uncertain_and_unpaid_coupons_are_excluded_without_hiding_other_accounts(self):
        self.observe([ROW])
        coupon_id = _item(ROW)['id']
        for state in ('submitted','unknown','accepted','payment_required'):
            with self.subTest(state=state):
                self.assertEqual(self.summary({'x':{'login_id':LOGIN,'coupon_id':coupon_id,'state':state}})['total'], 0)
        self.assertEqual(self.summary({'x':{'login_id':'other','coupon_id':coupon_id,'state':'accepted'}})['total'], 1)
        self.assertEqual(self.summary({'x':{'login_id':LOGIN,'coupon_id':coupon_id,'state':'closed'}})['total'], 1)
        self.assertEqual(self.reminders.summary([], {}, NOW)['total'], 0)

    def test_unknown_pricing_is_not_a_deal_and_rate_limiting_stops_other_price_reads(self):
        rows = [{**ROW,'pCardType':'001','pcardId':str(i),'pCardNo':str(i)} for i in range(3)]
        quote = Mock(side_effect=CouponError('limited','-800',429))
        self.observe(rows, quote)
        quote.assert_called_once()
        self.assertEqual(self.summary()['accounts'][LOGIN]['unpriced'], 3)
        self.assertEqual(self.summary()['total'], 0)
        self.observe([ROW, *rows], quote)
        self.assertEqual(self.summary()['total'], 1, 'exchange coupons do not need a price request')

    def test_forgetting_an_account_removes_persisted_reminders(self):
        self.observe([ROW])
        self.reminders.forget(LOGIN)
        self.assertNotIn(LOGIN, CouponReminders(self.storage).data)

    def test_wallet_query_uses_official_amount_limits_and_only_reads_new_coupon_rules_once(self):
        env = Environment(self.storage)
        env.recharge()
        env.rule = {'saleType':'10','saleCode':'10~1000:1'}
        env.amount_rule = {'minAmount':'20','maxAmount':'1000'}
        manager = CouponUseManager(env.provider, self.storage)
        with patch('coupon_use.CouponClient', side_effect=env.client):
            manager.query(LOGIN)
            manager.query(LOGIN)
        self.assertEqual(manager.reminders.summary([LOGIN], {}, NOW)['total'], 0, '19/20 is not under 9.1折')
        self.assertEqual(sum('/pay/ruleinfo/' in call[0] for call in env.calls), 1)
        self.assertEqual(sum('/pay/payrule/' in call[0] for call in env.calls), 1)
        self.assertEqual(env.sdk_calls, [])
        self.assertEqual(manager.operations, {}, 'a count never prepares an SMS, order or exchange operation')
        self.assertFalse(any('/sms/' in call[0] or '/order/' in call[0] for call in env.calls))


if __name__ == '__main__':
    unittest.main()
