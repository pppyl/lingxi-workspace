"""Ordered services and explicitly confirmed ordinary APP unsubscriptions.

Queries remain read-only; cancellation uses separately bound SMS contexts.
See SERVICES_PROTOCOL.md for the supported official paths and scope.
"""
import base64
import binascii
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime, timedelta
from html import unescape
from html.parser import HTMLParser
import json
import logging
import re
import secrets
from threading import RLock

from flask import jsonify, request

from app_login import APP_IT_HOST, AppLoginError, AppLoginSession
from http_transport import requests
from recharge.cmcc import SHANGHAI

SERVICES_PATH = '/biz-orange/BN/queryBuinessService/getNewQueryBusiness'
CANCEL_GATEWAY_HOST = APP_IT_HOST
BINDINGS_PATH = '/biz-orange/BH/unifyQueryService/getUnifyQuery'
CANCEL_SMS_PATH = '/biz-orange/BH/sms/getSmsCode'
CANCEL_PATH = '/biz-orange/BH/unifyCancelService/getUnifyCancel'
AUTH_CODES = {'400007', '400009', '410000', '420000'}
CATEGORIES = {'01': '我的套餐', '02': '增值业务', '03': '基础功能', '04': '活动'}
PLAN_TYPES = {'0': '手机', '1': '固话', '2': '宽带'}


class ServicesError(Exception):
    def __init__(self, message, code='SERVICES_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


class _DescriptionText(HTMLParser):
    """Read description markup as inert text; never load linked resources."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style', 'iframe'):
            self.hidden += 1
        elif tag in ('br', 'p', 'div', 'li', 'tr', 'h1', 'h2', 'h3'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style', 'iframe'):
            self.hidden = max(0, self.hidden-1)
        elif tag in ('p', 'div', 'li', 'tr', 'td'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value, *, description=False):
    if value is None:
        return ''
    text = str(value).strip()
    # Some official provinces encode their rich business explanation as Base64
    # (bundle modules 230/286). Only admit canonical UTF-8 text with actual
    # markup or Chinese prose; ordinary names and price strings stay untouched.
    if description and re.fullmatch(r'[A-Za-z0-9+/]+={0,2}', text) and len(text) % 4 == 0:
        try:
            decoded = base64.b64decode(text, validate=True).decode('utf-8')
        except (binascii.Error, UnicodeDecodeError):
            decoded = ''
        if '<' in decoded or re.search(r'[\u4e00-\u9fff]', decoded):
            text = decoded
    parser = _DescriptionText()
    parser.feed(text)
    text = unescape(''.join(parser.parts))
    text = re.sub(r'(?<!\d)(?:\+?86)?(1[3-9]\d)\d{4}(\d{4})(?!\d)', r'\1****\2', text)
    text = re.sub(r'(?i)\b(cookie|authorization|token|ticket|password|pwd|captcha|smsCode|verifyCode|couponCode|voucherCode)\s*[=:]\s*[^\s&;]+', r'\1=[已隐藏]', text)
    text = re.sub(r'(短信验证码|验证码|兑换码|券码)\s*[:：=]\s*[^\s，,；;]+', r'\1：[已隐藏]', text)
    return re.sub(r'[ \t]+', ' ', re.sub(r'\n\s*\n+', '\n', text)).strip()


def _rows(body, key):
    rows = body.get(key) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ServicesError('官方已订业务明细格式异常，请稍后手动刷新。', 'SERVICES_FORMAT')
    return rows


def _date(value):
    digits = re.sub(r'[-/: T]', '', str(value or ''))
    pattern = {8: '%Y%m%d', 12: '%Y%m%d%H%M', 14: '%Y%m%d%H%M%S'}.get(len(digits))
    if not pattern or not digits.isdigit():
        return None  # Includes official descriptive dates such as 长期有效.
    try:
        return datetime.strptime(digits, pattern).replace(tzinfo=SHANGHAI)
    except ValueError:
        return None


def _product(row, category, now, *, contract=False):
    name = row.get('contractName') if contract else row.get('productName') or row.get('businessName')
    if not name:
        if contract:
            name = row.get('contractId')
        elif str(row.get('codeType')) == '02' and (row.get('sPCode') or row.get('bizCode')):
            name = str(row.get('sPCode') or '')+str(row.get('bizCode') or '')
        else:
            name = row.get('productCode')
    fields = []
    for key, label in (
        ('orderTime', '订购时间'), ('validDate', '生效时间'), ('expireDate', '失效时间'),
        ('unsubscribeTime', '退订时间'), ('actualPayFee', '实际消费'),
        ('busiInfomsg', '业务说明'), ('busiInfoNoCancelReason', '业务办理限制说明'),
        ('productType', '业务分类码'), ('busiState', '官方业务状态码'),
    ):
        if row.get(key) is not None and row[key] != '':
            fields.append({'name': label, 'value': _text(row[key], description=key == 'busiInfomsg')})
    major = {'0': '主套餐', '2': '家庭套餐'}.get(str(row.get('majorType')))
    if major:
        fields.append({'name': '套餐类型', 'value': major})
    if str(row.get('freeType')) == '0':
        fields.append({'name': '费用标记', 'value': '减免'})
    if row.get('busiFee') is not None and row.get('busiFee') != '':
        fields.append({'name': '原价', 'value': _text(row['busiFee'])})
    state, unsubscribe = str(row.get('busiState', '')), str(row.get('unsubscribeFlag', ''))
    start, end = _date(row.get('validDate')), _date(row.get('expireDate'))
    if state == '3':
        status = '已失效'
    elif str(row.get('revokeSign')) in ('0', '2'):
        status = {'0': '已撤销', '2': '撤销中'}[str(row['revokeSign'])]
    elif major and start and start > now and unsubscribe == '2':
        status = '撤销中'
    elif unsubscribe == '0':
        status = '已撤销' if major and start and start > now else '已退订'
    elif start and start > now:
        status = '待生效'
    elif state == '2' and category == '04':
        status = '即将到期'
    elif state == '1' and category != '04':
        status = '即将失效'
    elif end and end < now:
        status = '已失效'
    else:
        status = '已订' if not contract and state in ('', '0') else None
    if str(row.get('revokeSign')) in ('0', '2'):
        fields.append({'name': '撤销状态', 'value': {'0': '已撤销', '2': '撤销中'}[str(row['revokeSign'])]})
    if contract and str(row.get('cancelState')) == '1':
        status = '已退订'
    children = []
    for detail in _rows(row, 'businessdetailsList'):
        entries = []
        if detail.get('titleValue') is not None and detail['titleValue'] != '':
            entries.append({'name': '说明', 'value': _text(detail['titleValue'])})
        for entry in _rows(detail, 'businessdetails'):
            if entry.get('vlues') is not None and entry['vlues'] != '':
                entries.append({'name': _text(entry.get('bubtitle')) or '说明', 'value': _text(entry['vlues'])})
            if entry.get('remark') is not None and entry['remark'] != '':
                entries.append({'name': '备注', 'value': _text(entry['remark'])})
        children.append({'name': _text(detail.get('title')) or '业务详情', 'fee': None, 'status': None, 'fields': entries, 'children': []})
    contracts = row.get('contractList') or {}
    if not isinstance(contracts, dict):
        raise ServicesError('官方业务合约明细格式异常。', 'SERVICES_FORMAT')
    children.extend(_product(child, category, now, contract=True) for child in _rows(contracts, 'contractDetailsList'))
    fee = _text(row.get('busiFee'))
    item = {'name': _text(name) or ('业务合约' if contract else '已订业务'),
            'fee': fee if fee not in ('', '--') else None, 'status': status, 'fields': fields, 'children': children}
    if contract:
        item['cancel'] = {'available': False, 'reason': '此项为业务合约，需在官方 APP 核对合约及违约金后办理。'}
    return item


def normalize_services(body, plan_type, busi_type, now, *, register_cancel=None):
    """Map one officially scoped response, without inventing prices or totals."""
    if 'allBusiQryList' not in body:
        raise ServicesError('官方未返回可识别的已订业务数据。', 'SERVICES_FORMAT')
    groups, mismatched, matched = [], False, False
    scoped = bool(body.get('busiType'))
    for query in _rows(body, 'allBusiQryList'):
        for info in _rows(query, 'allBusiInfoList'):
            entries = _rows(info, 'busiTypeInfoList')
            returned_type = str(info.get('planType', ''))
            if returned_type == plan_type:
                matched = True
            elif returned_type not in PLAN_TYPES:
                mismatched = mismatched or bool(entries)
                continue
            for line_index, line in enumerate(entries, 1):
                prefix = PLAN_TYPES.get(returned_type, '业务类型 '+returned_type)
                if len(entries) > 1:
                    prefix += '线路'+str(line_index)
                for category in _rows(line, 'busiInfoList'):
                    kind = str(category.get('productType', ''))
                    if scoped and kind != busi_type:
                        continue
                    products = _rows(category, 'productInfoList')
                    items = []
                    for row in products:
                        item = _product(row, kind, now)
                        if register_cancel is not None:
                            item['cancel'] = register_cancel(row, kind, returned_type, line.get('busiID'), item['name'])
                        items.append(item)
                    if items:
                        groups.append({'name': prefix+' · '+CATEGORIES.get(kind, '业务分类 '+kind), 'items': items})
    if not matched and (groups or mismatched):
        raise ServicesError('官方返回的业务类型与当前查询不一致，请稍后手动刷新。', 'SERVICES_SCOPE_MISMATCH', 409)
    return groups


class ServiceCancellations:
    """Server-owned product records and account/SMS-bound operation contexts."""
    def __init__(self, credentials, *, recover, accept_credentials, app_factory, now):
        self.credentials, self.recover, self.accept_credentials = credentials, recover, accept_credentials
        self.app_factory, self.now = app_factory, now
        self.lock = RLock()
        self.actions, self.sessions, self.prepared, self.preparing, self.outcomes, self.operation_locks = {}, {}, {}, {}, {}, {}

    @staticmethod
    def _reason(row, category, plan_type, line_phone, phone, now):
        if plan_type != '0' or line_phone and str(line_phone) != phone:
            return '关联固话、宽带或其他线路请在官方 APP 办理退订。'
        if category == '01' and str(row.get('majorType')) in ('0', '2'):
            return '主套餐、家庭套餐的变更或撤销请在官方 APP 办理。'
        if str(row.get('contractFlag')) == '0':
            return '此业务包含合约，需在官方 APP 核对合约及违约金后办理。'
        if str(row.get('unsubscribeFlag')) in ('0', '2'):
            return '此业务已退订或正在退订中。'
        end = _date(row.get('expireDate'))
        if str(row.get('busiState')) == '3' or end and end < now:
            return '此业务已失效。'
        if str(row.get('cancelFlag')) != '0':
            return _text(row.get('busiInfoNoCancelReason')) or '官方未向此业务开放在线退订。'
        code_type = row.get('codeType')
        if code_type not in ('01', '02') or code_type == '01' and not row.get('productCode') or code_type == '02' and not (row.get('sPCode') and row.get('bizCode')):
            return '官方未返回完整的业务退订标识，请在官方 APP 办理。'
        if not row.get('instanceId'):
            return '官方未返回可唯一定位的业务实例，请在官方 APP 办理。'
        guide = row.get('cancelGuideData') or {}
        if guide and guide.get('vUnsubscribeBoot') != '2':
            return _text(guide.get('vSubject5')) or '官方要求通过专属退订流程办理，请在 APP 查看。'
        return ''

    def register_action(self, records, login_id, phone, row, category, plan_type, line_phone, name):
        reason = self._reason(row, category, plan_type, line_phone, phone, self.now())
        fingerprint = tuple(str(row.get(key) or '') for key in ('instanceId', 'codeType', 'productCode', 'sPCode', 'bizCode'))
        outcome_key = (login_id, phone, fingerprint)
        with self.lock:
            outcome = self.outcomes.get(outcome_key)
        if not reason and outcome is not None:
            reason = '此业务退订已提交，正在等待官方结果；请核对 APP 或短信，不会重复提交。'
        if reason:
            return {'available': False, 'reason': reason}
        for action_id, record in records.items():
            if record['fingerprint'] == fingerprint:
                return {'available': True, 'reason': '', 'action_id': action_id}
        action_id = secrets.token_urlsafe(24)
        business_name = row.get('productName') or row.get('businessName') or (str(row.get('sPCode') or '')+str(row.get('bizCode') or '') if row.get('codeType') == '02' else row.get('productCode'))
        records[action_id] = {'login_id': login_id, 'phone': phone, 'business': deepcopy(row), 'business_name': business_name, 'category': category,
                              'plan_type': plan_type, 'line_phone': line_phone, 'name': name,
                              'fingerprint': fingerprint, 'outcome_key': outcome_key, 'query_month': self.now().strftime('%Y-%m')}
        return {'available': True, 'reason': '', 'action_id': action_id}

    def store_actions(self, login_id, records):
        with self.lock:
            self.actions[login_id] = records
            expired = [key for key, value in self.sessions.items() if self.now()-value['created_at'] >= timedelta(minutes=30)]
            for cancel_id in expired:
                session = self.sessions.pop(cancel_id)
                self.prepared.pop((session['login_id'], session['action_id']), None)
                session['flow'].transport.close()

    def busy_login(self, login_id):
        now = self.now()
        with self.lock:
            return any(session['login_id'] == login_id and (session['working'] or
                       session['result'] is None and session['sms_at'] is not None and now-session['sms_at'] < timedelta(minutes=5))
                       for session in self.sessions.values())

    @staticmethod
    def _public(session):
        return {key: deepcopy(session[key]) for key in ('cancel_id', 'name', 'phone_masked', 'effective_options',
                'warnings', 'bound_services', 'related_services', 'description')} | {'sms_required': True}

    def prepare(self, login_id, action_id):
        key = (login_id, action_id)
        snapshot = self.credentials(login_id)
        with self.lock:
            record = deepcopy(self.actions.get(login_id, {}).get(action_id))
            if record is None or record['query_month'] != self.now().strftime('%Y-%m'):
                raise ServicesError('业务信息已更新，请刷新已订业务后重新选择。', 'SERVICES_ACTION_EXPIRED', 409)
            if snapshot is None or snapshot['phone'] != record['phone']:
                raise ServicesError('共享账号已退出或身份已变更，请重新选择账号。', 'SERVICES_LOGIN_REQUIRED', 401)
            if record['outcome_key'] in self.outcomes:
                raise ServicesError('此业务退订已提交，请先核对官方结果，不会重复提交。', 'SERVICES_CANCEL_SUBMITTED', 409)
            existing = self.sessions.get(self.prepared.get(key))
            if existing is not None and not existing.get('invalid') and self.now()-existing['created_at'] < timedelta(minutes=30):
                return self._public(existing)
            future = self.preparing.get(key)
            leader = future is None
            if leader:
                future = self.preparing[key] = Future()
        if not leader:
            return deepcopy(future.result())
        flow = None
        try:
            row = record['business']
            reason = self._reason(row, record['category'], record['plan_type'], record['line_phone'], record['phone'], self.now())
            if reason:
                raise ServicesError(reason, 'SERVICES_CANCEL_UNAVAILABLE', 409)
            for attempt in range(2):
                if not snapshot.get('cookie'):
                    error = ServicesError('请恢复共享账号登录后再办理退订。', 'SERVICES_LOGIN_REQUIRED', 401)
                else:
                    flow = self.app_factory(snapshot)
                    flow.gateway_host, flow.request_version = CANCEL_GATEWAY_HOST, 2
                    flow.transport = requests.Session()
                    try:
                        body = ServicesQueries._call(flow, BINDINGS_PATH, {'cellNum': record['phone'], 'bindedFlag': '2', 'instanceId': row['instanceId']})
                    except ServicesError as call_error:
                        flow.transport.close()
                        error = call_error
                    else:
                        break
                if attempt or error.code != 'SERVICES_LOGIN_REQUIRED' or self.recover is None:
                    raise error
                try:
                    self.recover(snapshot)
                except AppLoginError as recovery_error:
                    raise ServicesError('APP 会话未能恢复，请重新登录后再办理退订。', 'SERVICES_LOGIN_REQUIRED', 401) from recovery_error
                snapshot = self.credentials(login_id)
                if snapshot is None or snapshot['phone'] != record['phone'] or not snapshot.get('cookie'):
                    raise ServicesError('共享账号已退出或身份已变更，请重新选择账号。', 'SERVICES_LOGIN_REQUIRED', 401)
            entries = _rows(body, 'uniQryRecList')
            products = _rows(entries[0], 'prodInfo') if entries else []
            related = [_text(entry.get('bunessName')) for entry in products if any(code in str(entry.get('busiInfomsg') or '') for code in ('1', '2'))]
            bound = [_text(entry.get('bunessName')) for entry in products if any(code in str(entry.get('busiInfomsg') or '') for code in ('0', '2'))]
            if any(not name for name in related+bound):
                raise ServicesError('官方未返回完整的关联业务名称，请在 APP 核对后退订。', 'SERVICES_FORMAT')
            labels = {'0': '下月失效', '1': '立即失效', '3': '次日失效'}
            effective = str(row.get('busiReturnEffTime') or '')
            values = str(row.get('effTimeType') or '').split('|') if effective == '2' else [effective]
            options = [{'value': value, 'label': labels[value]} for value in dict.fromkeys(values) if value in labels]
            if effective == '2' and not options:
                raise ServicesError('官方未提供可选的退订生效方式，请在 APP 办理。', 'SERVICES_FORMAT')
            if not options:
                options = [{'value': 'default', 'label': '失效时间以短信通知为准'}]
            warnings = []
            if str(row.get('offLineFlag')) == '1':
                warnings.append('此业务已下线或停售，退订后无法再次订购。')
            if related:
                warnings.append('退订后将无法继续享受所列关联优惠。')
            if bound:
                warnings.append('所列绑定业务将一同退订。')
            cancel_id = secrets.token_urlsafe(24)
            with self.lock:
                operation_lock = self.operation_locks.setdefault(record['outcome_key'], RLock())
            session = {**record, 'cancel_id': cancel_id, 'action_id': action_id, 'snapshot': deepcopy(snapshot), 'flow': flow,
                       'created_at': self.now(), 'lock': operation_lock, 'working': False, 'sms_at': None, 'last_sms_attempt': None,
                       'sms_effective': None, 'result': None, 'phone_masked': record['phone'][:3]+'****'+record['phone'][-4:],
                       'effective_options': options, 'warnings': warnings, 'bound_services': bound, 'related_services': related,
                       'sms_product_name': ','.join([str(record['business_name'])]+[str(entry['bunessName']) for entry in products if any(code in str(entry.get('busiInfomsg') or '') for code in ('0', '2'))]),
                       'description': _text(row.get('busiInfomsg'), description=True)}
            if self.accept_credentials is not None and flow.cookie != snapshot['cookie']:
                self.accept_credentials(snapshot, flow)
            with self.lock:
                self.sessions[cancel_id] = session
                self.prepared[key] = cancel_id
            result = self._public(session)
            future.set_result(result)
            return deepcopy(result)
        except Exception as error:
            if flow is not None:
                flow.transport.close()
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.preparing.pop(key, None)

    def _session(self, login_id, cancel_id):
        with self.lock:
            session = self.sessions.get(cancel_id)
        if session is None or session['login_id'] != login_id or self.now()-session['created_at'] >= timedelta(minutes=30):
            raise ServicesError('退订确认已过期，请重新选择业务。', 'SERVICES_CANCEL_EXPIRED', 409)
        snapshot = self.credentials(login_id)
        if snapshot is None or snapshot['phone'] != session['phone']:
            raise ServicesError('共享账号已退出或身份已变更，本次退订不会继续。', 'SERVICES_LOGIN_REQUIRED', 401)
        if session.get('invalid'):
            raise ServicesError('退订会话已失效，请重新登录并重新选择业务。', 'SERVICES_LOGIN_REQUIRED', 401)
        return session

    @staticmethod
    def _effective(session, value):
        if value not in {option['value'] for option in session['effective_options']}:
            raise ServicesError('请选择官方提供的退订生效方式。', 'INVALID_REQUEST', 400)
        return {'0': '2', '1': '1', '3': '5', 'default': '3'}[value]

    @staticmethod
    def _rejected(session, value):
        code = str(value.get('retCode', 'unknown'))
        description = _text(value.get('retDesc') or '请稍后在官方 APP 核对。')[:200]
        if code in AUTH_CODES:
            session['invalid'] = True
            session['sms_at'] = None
            return ServicesError('此退订会话需要恢复登录，请重新登录后重新选择业务；没有自动重发。', 'SERVICES_LOGIN_REQUIRED', 401)
        if code in ('-800', '8053') or '频繁' in description:
            return ServicesError('官方提示请求频繁，请稍后再试；尝试更换服务ip再试。', 'RATE_LIMITED', 429)
        code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
        return ServicesError('官方未完成退订操作（'+code+'）：'+description, code, 400 if code in ('312008', '312009') else 502)

    def sms(self, login_id, cancel_id, effective_type):
        session = self._session(login_id, cancel_id)
        with session['lock']:
            effective = self._effective(session, effective_type)
            with self.lock:
                submitted = session['outcome_key'] in self.outcomes
            if session['result'] is not None or submitted:
                raise ServicesError('退订已提交，不再重复发送验证码。', 'SERVICES_CANCEL_SUBMITTED', 409)
            elapsed = (self.now()-session['last_sms_attempt']).total_seconds() if session['last_sms_attempt'] is not None else 60
            if elapsed < 60:
                raise ServicesError('请在 '+str(max(1, int(60-elapsed)))+' 秒后重新获取验证码。', 'SERVICES_SMS_COOLDOWN', 429)
            row = session['business']
            session['last_sms_attempt'], session['sms_at'], session['sms_effective'], session['working'] = self.now(), None, None, True
            try:
                _, value, _ = session['flow'].call(CANCEL_SMS_PATH, {'cellNum': session['phone'], 'busType': '1',
                    'productName': session['sms_product_name'], 'productType': row['codeType'],
                    'effectiveType': effective, 'productId': row.get('productCode') or '', 'spid': row.get('sPCode') or '', 'bizCode': row.get('bizCode') or ''})
                if str(value.get('retCode')) != '000000':
                    raise self._rejected(session, value)
                session['sms_at'], session['sms_effective'] = self.now(), effective_type
                return {'message': '退订验证码已发送，有效期 5 分钟。请核对号码、业务和生效方式后确认。', 'retry_after': 60}
            except AppLoginError as error:
                response = getattr(error, 'response', None)
                if response is not None:
                    logging.getLogger(__name__).warning('退订短信网关失败：HTTP %s, type=%s, server=%s',
                        response.status_code, response.headers.get('content-type', ''), response.headers.get('server', ''))
                raise ServicesError('验证码发送结果未确认：'+_text(str(error))[:160]+' 请检查短信；一分钟后可手动重新获取。', 'SERVICES_SMS_UNCERTAIN') from error
            finally:
                session['working'] = False

    def confirm(self, login_id, cancel_id, effective_type, sms_code, confirmed):
        if confirmed is not True or not isinstance(sms_code, str) or not re.fullmatch(r'\d{6}', sms_code):
            raise ServicesError('请核对退订影响并填写 6 位短信验证码后确认。', 'INVALID_REQUEST', 400)
        session = self._session(login_id, cancel_id)
        with session['lock']:
            self._effective(session, effective_type)
            if session['result'] is not None:
                return deepcopy(session['result'])
            with self.lock:
                previous = self.outcomes.get(session['outcome_key'])
            if previous is not None:
                session['result'] = deepcopy(previous)
                return deepcopy(previous)
            if session['sms_at'] is None or self.now()-session['sms_at'] >= timedelta(minutes=5):
                raise ServicesError('请重新获取本次退订验证码，原验证码已失效。', 'SERVICES_SMS_EXPIRED', 409)
            if session['sms_effective'] != effective_type:
                raise ServicesError('退订生效方式已变化，请重新获取对应验证码。', 'SERVICES_SMS_MISMATCH', 409)
            row = session['business']
            payload = {'cellNum': session['phone'], 'businessName': session['business_name'], 'productType': row['codeType'],
                       'productId': row.get('productCode') or '', 'spid': row.get('sPCode') or '', 'bizCode': row.get('bizCode') or '',
                       'smsCode': sms_code, 'instanceId': row['instanceId']}
            if effective_type != 'default':
                payload['busiReturnEffTime'] = effective_type
            session['working'] = True
            try:
                _, value, _ = session['flow'].call(CANCEL_PATH, payload)
                code = str(value.get('retCode', 'unknown'))
                if code == '000000':
                    result = {'status': 'success', 'message': '退订成功，'+next(option['label'] for option in session['effective_options'] if option['value'] == effective_type)+'；最终生效以官方短信通知为准。'}
                elif code == '900000':
                    result = {'status': 'accepted', 'message': '退订申请已受理，官方正在处理中。请核对 APP 或短信结果，本程序不会重复提交。'}
                elif code in ('999999', 'unknown'):
                    result = {'status': 'unknown', 'message': '本次退订结果尚未确认，请在官方 APP 或短信中核对；本程序不会重复提交。'}
                else:
                    if code == '312009':
                        session['sms_at'], session['sms_effective'] = None, None
                    raise self._rejected(session, value)
            except AppLoginError:
                result = {'status': 'unknown', 'message': '提交后连接中断，退订结果尚未确认。请在官方 APP 或短信中核对；本程序不会重复提交。'}
            finally:
                session['working'] = False
            session['result'] = result
            with self.lock:
                self.outcomes[session['outcome_key']] = deepcopy(result)
            session['flow'].transport.close()
            return deepcopy(result)


class ServicesQueries:
    def __init__(self, credentials, *, recover=None, accept_credentials=None, app_factory=None, now=None):
        self.credentials, self.recover, self.accept_credentials = credentials, recover, accept_credentials
        self.app_factory = app_factory or AppLoginSession.from_credentials
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.pending = RLock(), {}
        self.cancellations = ServiceCancellations(credentials, recover=recover, accept_credentials=accept_credentials,
                                                 app_factory=self.app_factory, now=self.now)

    @staticmethod
    def _call(flow, path, payload):
        try:
            _, value, _ = flow.call(path, payload)
        except AppLoginError as error:
            raise ServicesError('连接官方已订业务服务失败，请稍后手动刷新。', 'SERVICES_NETWORK') from error
        code = str(value.get('retCode', 'unknown'))
        if code != '000000':
            if code in AUTH_CODES:
                raise ServicesError('APP 会话需要恢复，请重新登录对应共享账号。', 'SERVICES_LOGIN_REQUIRED', 401)
            if code in ('-800', '8053') or '频繁' in str(value.get('retDesc', '')):
                raise ServicesError('官方提示请求频繁，请稍后再试；尝试更换服务ip再试。', 'RATE_LIMITED', 429)
            code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            raise ServicesError('官方已订业务查询未完成（'+code+'）：'+_text(value.get('retDesc') or '请稍后再试。')[:200], code)
        body = value.get('rspBody')
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as error:
                raise ServicesError('官方已订业务响应无法解析。', 'SERVICES_FORMAT') from error
        if not isinstance(body, dict):
            raise ServicesError('官方已订业务响应格式异常。', 'SERVICES_FORMAT')
        return body

    def query(self, login_id):
        with self.lock:
            future = self.pending.get(login_id)
            leader = future is None
            if leader:
                future = self.pending[login_id] = Future()
        if not leader:
            return deepcopy(future.result())
        try:
            snapshot = self.credentials(login_id)
            if snapshot is None:
                raise ServicesError('共享账号不存在，请重新选择账号。', 'SERVICES_LOGIN_REQUIRED', 401)
            phone, recovered = snapshot['phone'], False
            now = self.now()
            month = now.strftime('%Y-%m')
            queue = ['01']
            groups, notes, flow, index = [], [], None, 0
            action_records = {}
            def register_cancel(row, category, plan_type, line_phone, name):
                return self.cancellations.register_action(action_records, login_id, phone, row, category, plan_type, line_phone, name)
            with requests.Session() as transport:
                while index < len(queue):
                    busi_type = queue[index]
                    label = '手机 · '+CATEGORIES[busi_type]
                    try:
                        for attempt in range(2):
                            if not snapshot.get('cookie'):
                                error = ServicesError('共享账号尚未登录，请恢复登录后查询。', 'SERVICES_LOGIN_REQUIRED', 401)
                            else:
                                if flow is None:
                                    flow = self.app_factory(snapshot)
                                    flow.transport = transport
                                params = {'cellNum': phone, 'bgnMonth': month, 'endMonth': month,
                                          'planType': '0', 'busiType': busi_type}
                                try:
                                    body = self._call(flow, SERVICES_PATH, params)
                                except ServicesError as request_error:
                                    error = request_error
                                else:
                                    break
                            if error.code != 'SERVICES_LOGIN_REQUIRED' or recovered or attempt or self.recover is None:
                                raise error
                            recovered = True
                            try:
                                self.recover(snapshot)
                            except AppLoginError as recovery_error:
                                raise ServicesError('APP 会话未能恢复，请完成验证或重新登录后查询。', 'SERVICES_LOGIN_REQUIRED', 401) from recovery_error
                            snapshot = self.credentials(login_id)
                            if snapshot is None or snapshot['phone'] != phone or not snapshot.get('cookie'):
                                raise ServicesError('共享账号已退出或身份已变更，请重新选择账号。', 'SERVICES_LOGIN_REQUIRED', 401)
                            flow = None
                        # Reuse any explicit linked-type records already in this
                        # selected account's response; never guess linked types.
                        groups.extend(normalize_services(body, '0', busi_type, now, register_cancel=register_cancel))
                        # An empty response busiType means the first result
                        # already covers the tabs; this is the APP cache rule.
                        if busi_type == '01' and body.get('busiType'):
                            queue.extend(('02', '03', '04'))
                    except ServicesError as error:
                        if index == 0:
                            raise
                        notes.append(label+'未更新：'+str(error))
                        if error.code in ('RATE_LIMITED', 'SERVICES_LOGIN_REQUIRED'):
                            break
                    index += 1
            if self.accept_credentials is not None and flow is not None and flow.cookie != snapshot['cookie']:
                self.accept_credentials(snapshot, flow)
            if not groups:
                notes.append('官方暂未返回已订业务。')
            result = {'groups': groups, 'notes': notes, 'queried_at': now.isoformat()}
            self.cancellations.store_actions(login_id, action_records)
            future.set_result(result)
            return deepcopy(result)
        except Exception as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(login_id, None)


def register_services(app, accounts, credentials, *, recover=None, accept_credentials=None):
    queries = ServicesQueries(credentials, recover=recover, accept_credentials=accept_credentials)
    app.extensions['services_queries'] = queries

    @app.get('/api/services/query')
    def services_query():
        login_id = request.args.get('login_id')
        account = next((row for row in accounts() if row['id'] == login_id), None)
        if account is None:
            return jsonify(error='请选择已有的共享账号。', code='ACCOUNT_NOT_FOUND'), 404
        try:
            result = queries.query(login_id)
        except ServicesError as error:
            account = next((row for row in accounts() if row['id'] == login_id), account)
            return jsonify(error=str(error), code=error.code, account=account), error.status
        result['account'] = next((row for row in accounts() if row['id'] == login_id), account)
        return jsonify(result)

    @app.post('/api/services/cancel/prepare')
    @app.post('/api/services/cancel/sms')
    @app.post('/api/services/cancel/confirm')
    def services_cancel():
        payload = request.get_json(silent=True)
        operation = request.path.rsplit('/', 1)[-1]
        required = {'prepare': ('login_id', 'action_id'), 'sms': ('login_id', 'cancel_id', 'effective_type'),
                    'confirm': ('login_id', 'cancel_id', 'effective_type', 'sms_code')}[operation]
        if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) or not payload[key] for key in required):
            return jsonify(error='退订请求参数不完整，请重新选择业务。', code='INVALID_REQUEST'), 400
        login_id = payload['login_id']
        account = next((row for row in accounts() if row['id'] == login_id), None)
        if account is None:
            return jsonify(error='请选择已有的共享账号。', code='ACCOUNT_NOT_FOUND'), 404
        try:
            if operation == 'prepare':
                result = queries.cancellations.prepare(login_id, payload['action_id'])
            elif operation == 'sms':
                result = queries.cancellations.sms(login_id, payload['cancel_id'], payload['effective_type'])
            else:
                result = queries.cancellations.confirm(login_id, payload['cancel_id'], payload['effective_type'], payload['sms_code'], payload.get('confirmed'))
        except ServicesError as error:
            account = next((row for row in accounts() if row['id'] == login_id), account)
            return jsonify(error=str(error), code=error.code, account=account), error.status
        result['account'] = next((row for row in accounts() if row['id'] == login_id), account)
        return jsonify(result)
