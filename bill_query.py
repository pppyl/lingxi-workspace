"""Read-only native APP bills using the existing shared APP credentials."""
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re
from threading import RLock

from flask import jsonify, render_template, request

from app_login import AppLoginError, AppLoginSession
from http_transport import requests
from recharge.cmcc import SHANGHAI

BILLS_PATH = '/biz-orange/BH/newBillsQry/getNewBills'
BALANCE_PATH = '/biz-orange/BN/realFeeQuery/getRealFee'
AUTH_CODES = {'400007', '400009', '410000', '420000'}
ENTRY_NAMES = {'01': '套餐及固定费', '02': '套餐外通话费', '03': '套餐外上网费',
               '04': '套餐外短彩信费', '05': '增值业务费', '06': '代收费', '07': '其他费用'}
PAY_NAMES = {'1': '单位代付', '2': '他人代付', '3': '使用专款', '4': '活动返费', '9': '其他返费'}


class BillError(Exception):
    def __init__(self, message, code='BILLS_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


def _text(value):
    return re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)', r'\1****\2', str(value or ''))


def _money(value, *, deduction=False):
    if value is None or value == '':
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None  # The official service also uses "--" for absent amounts.
    if not amount.is_finite():
        return None
    return format(abs(amount) if deduction else amount, '.2f')


def _bill_items(rows):
    return [{'name': ENTRY_NAMES.get(str(row.get('billEntries')), _text(row.get('billEntriesName')) or '费用项目 '+str(row.get('billEntries', ''))),
             'original': _money(row.get('billEntriesOPValue')),
             'discount': _money(row.get('billEntriesSaveValue')),
             'amount': _money(row.get('billEntriesValue')),
             'children': [{'name': _text(child.get('thirdItemsName')) or '费用明细',
                           'original': _money(child.get('thirdItemsValue')),
                           'discount': _money(child.get('thirdItemsSaveValue')),
                           'amount': _money(child.get('thirdItemsRealValue')), 'children': []}
                          for child in row.get('thirdBillMaterialInfo') or []]}
            for row in rows]


def normalize_bill(body, balance, month, now):
    rows = body.get('historyBillInfo') or []
    if not isinstance(rows, list):
        raise BillError('官方账单响应格式异常，本次查询未完成。', 'BILLS_FORMAT')
    # Never display a different month's row under the month selected by the user.
    row = next((row for row in rows if str(row.get('inBill', '')).replace('-', '') == month), None)
    if row is None and rows:
        raise BillError('官方未返回所选月份的账单，请重新选择月份。', 'BILLS_MONTH_MISMATCH', 409)
    row = row or {}
    payments = row.get('billPayInfoMsgRes') or {}
    savings = row.get('billSaveInfoRes') or {}
    combined = (row.get('billInfoAllRes') or [{}])[0]
    summary = [{'name': name, 'amount': _money(value)} for name, value in (
        ('合账账户消费', combined.get('allRealValue')), ('合账账户应付', combined.get('allRaalPay')),
        ('本月为您节省', savings.get('billSaveSum')), ('总余额', balance.get('curFeeTotal')),
        ('实时费用', balance.get('realFee')), ('欠费', balance.get('oweFee')),
        ('实际余额', balance.get('realBalanceFee')))]
    sections = []
    payment_rows = [{'name': PAY_NAMES.get(str(item.get('billPayname')), '代付／返费类型 '+_text(item.get('billPayname'))),
                     'amount': _money(item.get('billPaytypesum')), 'original': None, 'discount': None,
                     'children': [{'name': _text(child.get('billPayName')) or '返费项目 '+_text(child.get('billPayCode')),
                                   'amount': _money(child.get('billPayInfoSum')), 'original': None,
                                   'discount': None, 'children': []}
                                  for child in item.get('payInfoRes') or []]}
                    for item in payments.get('payInfoListRes') or []]
    for source, item in zip(payments.get('payInfoListRes') or [], payment_rows):
        if str(source.get('billPayname')) not in PAY_NAMES or len(item['children']) != 1:
            continue
        child = item['children'][0]
        name = _text(source['payInfoRes'][0].get('billPayName')).strip()
        if (name in ('', item['name']) and item['amount'] is not None and child['amount'] is not None
                and abs(Decimal(item['amount'])) == abs(Decimal(child['amount']))):
            # Keep original detail data; the UI can omit its duplicate, generated row.
            item['compact_details'] = True
    if payment_rows:
        sections.append({'name': '代付与返费明细', 'items': payment_rows})
    saving_rows = [{'name': _text(item.get('billSaveName')) or '节省项目', 'amount': _money(item.get('billSaveValue')),
                    'original': None, 'discount': None, 'children': []}
                   for item in savings.get('saveInfoMsgRes') or []]
    if saving_rows:
        sections.append({'name': '节省明细', 'items': saving_rows})
    period = ' 至 '.join(str(row[key]) for key in ('billCycleStartDate', 'billCycleEndDate') if row.get(key))
    return {'month': month, 'period': period, 'total': _money(row.get('realConsumeBill')),
            'payable': _money(row.get('realPayBill')), 'original': _money(row.get('totalBill')),
            'discount': _money(row.get('reductionBill'), deduction=True),
            'rebate': _money(payments.get('billPaysum'), deduction=True), 'balance': _money(balance.get('curFee')),
            'items': _bill_items(row.get('billMaterialRes') or []),
            'summary': [item for item in summary if item['amount'] is not None], 'sections': sections,
            'notes': [_text(row['billInfoMsg']).replace('%', '；')] if row.get('billInfoMsg') else [],
            'message': '' if rows else '官方暂未返回该月份账单。', 'queried_at': now.isoformat()}


class BillQueries:
    def __init__(self, credentials, *, recover=None, accept_credentials=None, app_factory=None, now=None):
        self.credentials, self.recover, self.accept_credentials = credentials, recover, accept_credentials
        self.app_factory = app_factory or AppLoginSession.from_credentials
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.pending = RLock(), {}

    def months(self):
        # Same default window as the native monthSelector: current + 12 months.
        current = self.now()
        index = current.year*12+current.month-1
        return [f'{(index-i)//12:04d}{(index-i)%12+1:02d}' for i in range(13)]

    @staticmethod
    def _call(flow, path, payload):
        try:
            _, value, _ = flow.call(path, payload)
        except AppLoginError as error:
            raise BillError('连接官方账单服务失败，请稍后手动刷新。', 'BILLS_NETWORK') from error
        code = str(value.get('retCode', 'unknown'))
        if code != '000000':
            if code in AUTH_CODES:
                raise BillError('APP 会话需要恢复，请重新登录对应共享账号。', 'BILLS_LOGIN_REQUIRED', 401)
            if code in ('-800', '8053') or '频繁' in str(value.get('retDesc', '')):
                raise BillError('官方提示请求频繁，请稍后再试；尝试更换服务ip再试。', 'RATE_LIMITED', 429)
            safe_code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            raise BillError('官方账单查询未完成（'+safe_code+'）：'+_text(value.get('retDesc') or '请稍后再试。')[:200], safe_code)
        body = value.get('rspBody') or {}
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as error:
                raise BillError('官方账单响应无法解析。', 'BILLS_FORMAT') from error
        if not isinstance(body, dict):
            raise BillError('官方账单响应格式异常。', 'BILLS_FORMAT')
        return body

    def query(self, login_id, month):
        if month not in self.months():
            raise BillError('请选择当前月或此前十二个月。', 'INVALID_MONTH', 400)
        key = login_id, month
        with self.lock:
            future = self.pending.get(key)
            leader = future is None
            if leader:
                future = self.pending[key] = Future()
        if not leader:
            return deepcopy(future.result())
        try:
            with requests.Session() as transport:
                for attempt in range(2):
                    snapshot = self.credentials(login_id)
                    if snapshot is None or not snapshot.get('cookie'):
                        raise BillError('共享账号不存在或尚未登录，请在账号管理恢复登录。', 'BILLS_LOGIN_REQUIRED', 401)
                    flow = self.app_factory(snapshot)
                    flow.transport = transport
                    selected_month = month[:4]+'-'+month[4:]
                    try:
                        body = self._call(flow, BILLS_PATH, {'cellNum': snapshot['phone'], 'bgnMonth': selected_month, 'endMonth': selected_month})
                    except BillError as error:
                        if error.code != 'BILLS_LOGIN_REQUIRED' or attempt or self.recover is None:
                            raise
                        try:
                            self.recover(snapshot)
                        except AppLoginError as recovery_error:
                            raise BillError('APP 会话未能恢复，请完成当前验证或在账号管理重新登录后再查询。', 'BILLS_LOGIN_REQUIRED', 401) from recovery_error
                        continue
                    break
                balance, warning = {}, ''
                try:
                    balance = self._call(flow, BALANCE_PATH, {'cellNum': snapshot['phone']})
                except BillError as error:
                    # A secondary balance failure must not discard a valid bill or
                    # trigger another login/whole-bill request.
                    warning = '账单已查询；余额未更新：'+str(error)
            if self.accept_credentials is not None and flow.cookie != snapshot['cookie']:
                self.accept_credentials(snapshot, flow)
            result = normalize_bill(body, balance, month, self.now())
            if warning:
                result['notes'].append(warning)
            future.set_result(result)
            return deepcopy(result)
        except Exception as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(key, None)


def register_bills(app, accounts, credentials, *, recover=None, accept_credentials=None):
    queries = BillQueries(credentials, recover=recover, accept_credentials=accept_credentials)
    app.extensions['bill_queries'] = queries

    @app.get('/information')
    @app.get('/bills')
    def bills_page():
        return render_template('bills.html')

    @app.get('/api/bills/accounts')
    def bills_accounts():
        months = queries.months()
        return jsonify(accounts=accounts(), months=months, default_month=months[0])

    @app.get('/api/bills/query')
    def bill_query():
        login_id = request.args.get('login_id')
        account = next((row for row in accounts() if row['id'] == login_id), None)
        if account is None:
            return jsonify(error='请选择已有的共享账号。', code='ACCOUNT_NOT_FOUND'), 404
        try:
            result = queries.query(login_id, request.args.get('month'))
        except BillError as error:
            account = next((row for row in accounts() if row['id'] == login_id), account)
            return jsonify(error=str(error), code=error.code, account=account), error.status
        result['account'] = next((row for row in accounts() if row['id'] == login_id), account)
        return jsonify(result)
