import unittest
from coupon_use import _choices


class BestCouponAmountTests(unittest.TestCase):
    def test_fixed_bonus_uses_smallest_eligible_tier(self):
        choice, = _choices({'saleType': 7, 'saleCode': '30~1000:7'})
        self.assertEqual((choice['value'], choice['credit']), ('30', '37.00'))

    def test_higher_discount_ratio_can_win_over_smaller_payment(self):
        choice, = _choices({'saleType': 7, 'saleCode': '30~49:3,50~1000:10'})
        self.assertEqual((choice['value'], choice['credit']), ('50', '60.00'))

    def test_equal_ratio_uses_lower_payment(self):
        choice, = _choices({'saleType': 7, 'saleCode': '30~49:3,50~99:5,100~1000:10'})
        self.assertEqual(choice['value'], '30')

    def test_reduction_uses_twenty_yuan_threshold_and_lower_actual_payment(self):
        choice, = _choices({'saleType': 10, 'saleCode': '20~5000:2'}, {'minAmount': 1, 'maxAmount': 5000})
        self.assertEqual((choice['value'], choice['payment'], choice['credit']), ('20', '18.00', '20.00'))

    def test_custom_threshold_respects_official_amount_limits(self):
        choice, = _choices({'saleType': 10, 'saleCode': '25~100:3'}, {'minAmount': 28, 'maxAmount': 80})
        self.assertEqual((choice['value'], choice['payment']), ('28', '25.00'))

    def test_does_not_offer_out_of_range_or_zero_payment(self):
        from coupon_use import CouponUseError
        for rule, limits in [({'saleType': 10, 'saleCode': '20~30:2'}, {'minAmount': 40, 'maxAmount': 50}),
                             ({'saleType': 10, 'saleCode': '20~20:20'}, {})]:
            with self.subTest(rule=rule), self.assertRaises(CouponUseError):
                _choices(rule, limits)
