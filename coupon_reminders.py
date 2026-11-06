"""Last-confirmed useful fee coupons; sidebar reads never contact the provider."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from threading import RLock

from coupon_client import CouponError, RECHARGE_CARD_TYPES
from recharge.cmcc import SHANGHAI

SCOPE = 'coupon-reminders'
OCCUPIED = {'submitted', 'unknown', 'accepted', 'payment_required'}


def reminder_summary(data, account_ids, submissions, now):
    occupied = {(row.get('login_id'), row.get('coupon_id')) for row in submissions.values()
                if row.get('state') in OCCUPIED}
    accounts = {}
    for owner in account_ids:
        record = data.get(owner)
        if record is None:
            continue
        count, unpriced = 0, 0
        for coupon_id, item in record['items'].items():
            if (owner, coupon_id) in occupied:
                continue
            expiry = item.get('expires_at')
            if expiry:
                end = datetime.fromisoformat(expiry)
                if end.tzinfo is None:
                    end = end.replace(tzinfo=SHANGHAI)
                if end <= now:
                    continue
            count += item['qualifies'] is True
            unpriced += item['qualifies'] is None
        accounts[owner] = {'count': count, 'unpriced': unpriced, 'checked_at': record['checked_at']}
    return {'accounts': accounts, 'total': sum(row['count'] for row in accounts.values())}


class CouponReminders:
    def __init__(self, storage=None):
        self.storage, self.lock = storage, RLock()
        self.data = (storage.load_activity_data(SCOPE) if storage else None) or {}
        self.account_locks = {}

    def observe(self, owner, items, rows, *, state, quote):
        with self.lock:
            account_lock = self.account_locks.setdefault(owner, RLock())
        with account_lock:
            with self.lock:
                previous = deepcopy(self.data.get(owner, {}).get('items', {}))
            observed = {} if state == 'available' else previous
            stopped = False
            for item, raw in zip(items, rows):
                coupon_id = item['id']
                if item['status'] != 'available':
                    observed.pop(coupon_id, None)
                    continue
                if str(raw.get('busiType')) != 'hf01':
                    continue
                card_type = str(raw.get('pCardType'))
                if card_type != '004' and card_type not in RECHARGE_CARD_TYPES:
                    continue
                if state != 'available':
                    continue
                fingerprint = hashlib.sha256(json.dumps([
                    raw.get(key) for key in ('pCardType', 'cardVersion', 'couponValue',
                        'valueFormat', 'stallInfo', 'expireTime', 'useChannel')
                ], ensure_ascii=False).encode()).hexdigest()
                entry = {'expires_at': item['expires_at'], 'fingerprint': fingerprint,
                         'qualifies': True if card_type == '004' else None}
                cached = observed.get(coupon_id) or previous.get(coupon_id)
                if card_type != '004':
                    if cached and cached['fingerprint'] == fingerprint and cached['qualifies'] is not None:
                        entry.update(qualifies=cached['qualifies'], pricing=cached.get('pricing'))
                    elif not stopped:
                        try:
                            best = quote(raw)
                            payment, credit = Decimal(best['payment']), Decimal(best['credit'])
                            entry.update(qualifies=payment < credit*Decimal('.91'),
                                         pricing={'payment': best['payment'], 'credit': best['credit']})
                        except CouponError as error:
                            # A failed price lookup is unknown, not a cheap deal.
                            # Never initialize, send SMS or submit orders here.
                            stopped = error.status == 401 or error.code in ('RATE_LIMITED', '-800', '8053')
                observed[coupon_id] = entry
            record = {'items': observed, 'checked_at': datetime.now(SHANGHAI).isoformat()}
            with self.lock:
                updated = {**self.data, owner: record}
                if self.storage:
                    self.storage.save_activity_data(SCOPE, updated)
                self.data = updated

    def summary(self, account_ids, submissions, now=None):
        with self.lock:
            return reminder_summary(self.data, account_ids, submissions, now or datetime.now(SHANGHAI))

    def forget(self, owner):
        with self.lock:
            updated = {key: value for key, value in self.data.items() if key != owner}
            if self.storage:
                self.storage.save_activity_data(SCOPE, updated)
            self.data = updated
