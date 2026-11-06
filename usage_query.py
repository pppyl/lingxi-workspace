"""Read-only native APP usage, using the selected shared APP identity.

Protocol sources and field/unit evidence: analysis/usage-protocol. No JavaScript
or browser is executed by this module. The 20085, 20066 and 20064 interfaces
use different unit dictionaries; keep those dictionaries separate.
"""
import calendar
from concurrent.futures import Future
from copy import deepcopy
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import json
import re
from threading import RLock

from flask import jsonify, request

from app_login import AppLoginError, AppLoginSession
from http_transport import requests
from recharge.cmcc import SHANGHAI

REMAIN_PATH = '/biz-orange/BH/newPlanRemainQry/getNewPlanRemainQry'
PLAN_PATH = '/biz-orange/BN/userPlanQry/getUserPlanQryNew'
COUNTRY_PATH = '/biz-orange/BH/countryFlowQuery/getFlowInfoQry'
SHARE_PATH = '/biz-orange/BH/userShareInfoQry/getUserShareInfo'
AUTH_CODES = {'400007', '400009', '410000', '420000'}
UNITS = {'01': '分钟', '02': '条', '03': 'MB', '04': 'GB', '05': '次', '06': '天', '1': 'MB'}
FLOW_SCOPES = {'0': '国内通用流量', '1': '定向流量', '2': '国内其他流量', '3': 'WLAN流量'}
VOICE_SCOPES = {'0': '国内通用通话', '1': '国内其他通话', '2': '国际及港澳台通话'}


class UsageError(Exception):
    def __init__(self, message, code='USAGE_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


def _text(value):
    return re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)', r'\1****\2', str(value if value is not None else ''))


def _number(value):
    """Preserve official decimal precision, including an actual numeric zero."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or text == 'N':
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None  # Official responses also contain '--' and unlimited 'N'.
    return text if number.is_finite() else None


def _unlimited(unit, *values):
    # is20085BigData in native module 26081015389186. These are service
    # sentinels, not finite allowances to display or include in arithmetic.
    threshold = {'01': 99999, '02': 9999, '03': 10238976, '1': 10238976, '04': 9999}.get(str(unit))
    return any(value == 'N' or threshold is not None and _number(value) is not None
               and Decimal(str(value)) >= threshold for value in values)


def _rows(body, key):
    rows = body.get(key) or []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UsageError('官方余量明细格式异常，请稍后手动刷新。', 'USAGE_FORMAT')
    return rows


def _description(row):
    parts = [_text(row[key]) for key in ('planSpecialInfo', 'outPlanSpecialInfo', 'busiInfomsg', 'remark') if row.get(key)]
    details = list(_rows(row, 'businessdetailsList'))
    if isinstance(row.get('businessdetailsInfo'), dict):
        details += _rows(row['businessdetailsInfo'], 'businessdetailsList')
    for detail in details:
        values = [_text(detail[key]) for key in ('title', 'titleValue') if detail.get(key)]
        for child in _rows(detail, 'businessdetails'):
            values.append('：'.join(_text(child[key]) for key in ('bubtitle', 'vlues', 'remark') if child.get(key)))
        parts.append('；'.join(filter(None, values)))
    for app in _rows(row, 'appInfoRes'):
        parts.append('：'.join(_text(app[key]) for key in ('appType', 'appTag') if app.get(key)))
    return '；'.join(dict.fromkeys(filter(None, parts)))


def _item(row, *, name, scope='', total='totalRes', used='usedRes', remaining='remainRes', unit=None):
    unit_code = str(row.get('unit', ''))
    unlimited = row.get('unLimitType') == '02' or row.get('plantype') == '03' or _unlimited(unit_code, row.get(total), row.get(remaining))
    labels = [scope] if scope else []
    for key, mapping in (
        ('isMultiTerm', {'0': '可共享', '1': '不可共享'}),
        ('flowContinueFlag', {'0': '上月结转'}), ('gATFlag', {'0': '国内(含港澳台)'}),
        ('expireFlag', {'1': '已失效', '2': '未生效'}),
        ('plantype', {'04': '日租卡', '05': '提速包'}),
    ):
        label = mapping.get(str(row.get(key, '')))
        if label:
            labels.append(label)
    if row.get('continueFlag'):
        labels.append('结转')
    return {'name': _text(name) or '余量项目', 'unit': unit if unit is not None else UNITS.get(unit_code, ''),
            'total': None if unlimited else _number(row.get(total)), 'used': _number(row.get(used)),
            'remaining': None if unlimited else _number(row.get(remaining)),
            'scope': ' / '.join(labels), 'start_at': _text(row.get('validDate')) or None,
            'end_at': _text(row.get('expireDate')) or None, 'description': _description(row),
            'unlimited': unlimited, 'children': []}


def _plans(rows, resource_key, resource_name, scope):
    result = []
    for plan in rows:
        parent = _item(plan, name=plan.get('planName'), scope=scope)
        for resource in _rows(plan, resource_key):
            merged = {**plan, **resource}
            child = _item(merged, name=resource.get(resource_name) or plan.get('planName'), scope=scope)
            if plan.get('plantype') == '06' and resource_key == 'flowInfoRes':
                child['name'] += '（本月）'
            parent['children'].append(child)
        for key, label in (('dayPlanParaInfoRes', '当日用量'), ('planParaInfoRes', '产品累计用量')):
            if plan.get(key):
                if not isinstance(plan[key], dict):
                    raise UsageError('官方余量明细格式异常。', 'USAGE_FORMAT')
                parent['children'].append(_item({**plan, **plan[key]}, name=label, scope=scope))
        # A wrapper has no aggregate amounts; never add potentially overlapping
        # resources together to manufacture a package total.
        result.append(parent)
    return result


def _overage(rows, scope):
    return [_item(row, name=row.get('outPlanTitle') or '套餐外用量', scope=scope,
                  used='usageAmount') for row in rows]


def _present_usage(item):
    """Add readable display units while retaining every official source value."""
    item['display'] = {}
    for field in ('total', 'used', 'remaining'):
        value, unit = item.get(field), item.get('unit', '')
        if value is None:
            continue
        number = Decimal(str(value))
        if unit in ('KB', 'MB', 'GB'):
            megabytes = number * {'KB': Decimal(1)/1024, 'MB': 1, 'GB': 1024}[unit]
            if abs(megabytes) >= 1024:
                number, unit = megabytes/1024, 'GB'
            elif abs(megabytes) >= 1 or not megabytes:
                number, unit = megabytes, 'MB'
            else:
                number, unit = megabytes*1024, 'KB'
            shown = number.quantize(Decimal('.01'), rounding=ROUND_DOWN)
            if number and not shown:
                text = '<0.01' if number > 0 else '>-0.01'
            else:
                text = format(shown, 'f').rstrip('0').rstrip('.')
        else:
            text = format(number, 'f')
            if '.' in text:
                text = text.rstrip('0').rstrip('.')
        item['display'][field] = {'value': text or '0', 'unit': unit}
    total = item.get('total')
    item['remaining_percent'] = None
    item['used_percent'] = None
    if not item.get('unlimited') and total is not None:
        total = Decimal(str(total))
        if total > 0:
            for field in ('remaining', 'used'):
                if item.get(field) is None:
                    continue
                amount = Decimal(str(item[field]))
                if 0 <= amount <= total:
                    item[field+'_percent'] = format((amount/total*100).quantize(Decimal('.01'), rounding=ROUND_DOWN), 'f')
    for child in item.get('children', []):
        _present_usage(child)


def normalize_usage(body, plans, country, now, *, share=None, phone=None):
    root = body.get('newPlanRemainQryRes')
    if not isinstance(root, dict):
        raise UsageError('官方未返回可识别的余量数据，请稍后手动刷新。', 'USAGE_FORMAT')
    overview, groups, notes = [], [], []
    flow = root.get('planRemianFlowListRes') or {}
    voice = root.get('planRemianVoiceListRes') or {}
    messages = root.get('planRemianMSGInfoListRes') or {}
    other = root.get('planRemianOtherListRes') or {}
    if any(not isinstance(section, dict) for section in (flow, voice, messages, other)):
        raise UsageError('官方余量响应格式异常。', 'USAGE_FORMAT')
    for section, array, type_key, scope_map, kind, detail_key, resource_key, resource_name, prefix in (
        (flow, 'planRemianFlowRes', 'flowtype', FLOW_SCOPES, 'flow', 'planRemianFlowInfoRes', 'flowInfoRes', 'flowName', 'flow'),
        (voice, 'planRemianVoiceInfoRes', 'voicetype', VOICE_SCOPES, 'voice', 'planRemianVoiceRes', 'voiceInfoRes', 'voiceName', 'voice'),
    ):
        overage = _rows(section, 'outPlanInfoRes')
        for summary in _rows(section, array):
            type_code = str(summary.get(type_key, ''))
            scope = scope_map.get(type_code, ('流量' if kind == 'flow' else '通话')+'范围 '+type_code)
            item = _item({'unit': '01', **summary} if kind == 'voice' else summary, name=scope, scope=scope,
                         total=prefix+'SumNum', used=prefix+'UsdNum', remaining=prefix+'RemainNum')
            item.pop('children')
            item['key'] = kind
            # Native common-flow/common-voice used amounts include explicit
            # out-of-plan usage. Convert GB to MB only under 20085's dictionary.
            if type_code == '0' and overage:
                amounts = [_number(row.get('usageAmount')) for row in overage]
                if kind == 'flow':
                    can_sum = all(str(row.get('unit')) in ('1', '03', '04') for row in overage)
                    factors = [1024 if str(row.get('unit')) == '04' else 1 for row in overage]
                else:
                    can_sum, factors = True, [1]*len(overage)
                if can_sum and all(amount is not None for amount in amounts):
                    extra = sum((Decimal(amount)*factor for amount, factor in zip(amounts, factors)), Decimal(0))
                    used = Decimal(item['used']) if item['used'] is not None else Decimal(0)
                    if kind == 'flow' and str(summary.get('unit')) == '04':
                        used *= 1024
                    item['used'] = format(used+extra, 'f')
                    if kind == 'flow':
                        # The card contract has one common unit for all values.
                        if str(summary.get('unit')) == '04':
                            for field in ('total', 'remaining'):
                                if item[field] is not None:
                                    item[field] = format(Decimal(item[field])*1024, 'f')
                        item['unit'] = 'MB'
            if kind == 'flow' and type_code == '0' and flow.get('limitType') in ('02', '03', '04', '05'):
                item['remaining'] = None  # Native service exposes usage only for these states.
            overview.append(item)
            items = _plans(_rows(summary, detail_key), resource_key, resource_name, scope)
            if type_code == '0':
                items.extend(_overage(overage, scope+' / 套餐外'))
            if items:
                groups.append({'name': scope, 'items': items})
        if overage and not any(str(row.get(type_key)) == '0' for row in _rows(section, array)):
            groups.append({'name': '套餐外流量' if kind == 'flow' else '套餐外通话', 'items': _overage(overage, '套餐外')})
    for summary in _rows(messages, 'planRemianMSGRes'):
        name = {'0': '短信', '1': '彩信'}.get(str(summary.get('mSGType')), '短彩信类型 '+_text(summary.get('mSGType')))
        item = _item({**summary, 'unit': '02'}, name=name, scope=name, total='mSGSum', used='mSGuserd', remaining='mSGresSum')
        item.pop('children')
        overview.append({**item, 'key': 'sms'})
    msg_items = _plans(_rows(messages, 'planRemianMSGInfoRes'), 'msgInfoRes', 'planRemianMSGName', '短彩信')
    msg_items.extend(_overage(_rows(messages, 'outPlanInfoRes'), '短彩信 / 套餐外'))
    if msg_items:
        groups.append({'name': '短彩信明细', 'items': msg_items})
    other_items = [_item(row, name=' - '.join(_text(row[key]) for key in ('planName', 'otherName') if row.get(key)), scope='其他资源')
                   for row in _rows(other, 'planRemianOtherInfoRes')]
    other_items.extend(_overage(_rows(other, 'outPlanInfoRes'), '其他资源 / 套餐外'))
    if other_items:
        groups.append({'name': '其他资源', 'items': other_items})
    for plan in _rows(plans, 'planQryInfos'):
        items = []
        for resource_type in _rows(plan, 'busiInfoList'):
            kind = str(resource_type.get('type', ''))
            name = {'0': '通话', '1': '流量', '4': '流量', '2': '短信'}.get(kind, '套餐资源 '+kind)
            for resource in _rows(resource_type, 'busiInfo'):
                code = str(resource.get('unit', ''))
                unit = ({'03': 'MB', '05': 'GB'}.get(code, '') if kind in ('1', '4')
                        else '分钟' if kind == '0' else '条' if kind == '2' else '')
                item = _item(resource, name=name, scope='套餐包含（非实时余量）', total='busiNum', unit=unit)
                # 20066 has different unit codes and uses only 'N' for unlimited.
                item['unlimited'] = resource.get('busiNum') == 'N'
                item['total'] = None if item['unlimited'] else _number(resource.get('busiNum'))
                items.append(item)
        family = {'1': '家庭', '0': '个人'}.get(str(plan.get('type')), '')
        parent = _item(plan, name=plan.get('curPlanName') or '当前套餐', scope=family+'套餐说明')
        parent['children'] = items
        reserve = plan.get('reservePlanInfo')
        if isinstance(reserve, dict) and reserve.get('reservePlanName') and reserve.get('reservePlanId') != plan.get('curPlanId'):
            parent['children'].append(_item(reserve, name=reserve['reservePlanName'], scope='预约套餐'))
        groups.append({'name': '套餐信息', 'items': [parent]})
    international = []
    for package in _rows(country, 'flowPacketUsageInfoList'):
        row = {**package, 'validDate': package.get('prodeffTIMSI'), 'expireDate': package.get('prodexpireTIMSI')}
        item = _item(row, name=package.get('productName') or '国际及港澳台资源包', scope='国际及港澳台')
        for name, unit, total, used, remaining in (
            ('流量', 'MB', 'allFlows', 'usedFlows', 'leftFlows'),
            ('通话', '分钟', 'allVoice', 'usedVoice', 'leftVoice'),
            ('天数', '天', 'alldays', 'usedDays', 'leftDays'),
        ):
            if any(package.get(key) is not None for key in (total, used, remaining)):
                item['children'].append(_item(row, name=name, unit=unit, scope='国际及港澳台', total=total, used=used, remaining=remaining))
        item['description'] = '；'.join(filter(None, [item['description']]+[label+'：'+_text(package[key]) for key, label in
            (('prodfirstTIMSI', '激活时间'), ('prodendTime', '使用截止'), ('prodStat', '产品状态')) if package.get(key)]))
        international.append(item)
    for tariff in _rows(country, 'standardTariffUsageInfoList'):
        for visit in _rows(tariff, 'visitInfoList'):
            row = {**visit, 'validDate': tariff.get('visitDate')}
            international.append(_item(row, name=visit.get('standardProductName') or tariff.get('standardProductName') or '国际标准资费用量',
                                       scope='国际及港澳台 / 标准资费', unit='MB', used='visitFlows'))
    if international:
        groups.append({'name': '国际及港澳台用量', 'items': international})
    for shared in _rows(share or {}, 'userShareInfoRes'):
        card = str(shared.get('cardstate'))
        if card not in ('0', '1'):
            continue  # The native APP likewise selects main and sub cards only.
        scope = '共享主卡' if card == '0' else '共享副卡'
        items = []
        for resource in _rows(shared, 'shareDetailInfo'):
            kind = {'04': '共享流量', '01': '共享通话'}.get(str(resource.get('resourcesCode')), '共享资源')
            code = str(resource.get('unit', ''))
            item = _item(resource, name=resource.get('planName') or kind, scope=scope,
                         unit={'01': '分钟', '03': 'KB', '04': 'MB', '05': 'GB'}.get(code, ''),
                         total='shareTotalRes', used='shareUsedRes', remaining='shareRemainRes')
            # 20062's 03/04/05 mean KB/MB/GB, not 20085's MB/GB/times.
            threshold = {'01': 99999, '03': 10382321664, '04': 10238976, '05': 9999}.get(code)
            item['unlimited'] = any(value == 'N' or threshold is not None and _number(value) is not None
                and Decimal(str(value)) >= threshold for value in (resource.get('shareTotalRes'), resource.get('shareRemainRes')))
            item['total'] = None if item['unlimited'] else _number(resource.get('shareTotalRes'))
            item['remaining'] = None if item['unlimited'] else _number(resource.get('shareRemainRes'))
            for member in _rows(resource, 'shareUsedResInfo'):
                if card == '1' and member.get('usedTerm') != phone:
                    continue
                item['children'].append(_item(member, name=_text(member.get('usedTerm')) or '共享成员', scope=scope,
                    unit={'01': '分钟', '03': 'KB', '04': 'MB', '05': 'GB'}.get(str(member.get('unit', '')), ''), used='usedRes'))
            items.append(item)
        for aggregate in _rows(shared, 'allUserShareInfo'):
            kind = {'04': '共享流量总体已用', '01': '共享通话总体已用'}.get(str(aggregate.get('resourcesCodeAll')), '共享资源总体已用')
            item = _item(aggregate, name=kind, scope=scope,
                         unit={'01': '分钟', '03': 'KB', '04': 'MB', '05': 'GB'}.get(str(aggregate.get('unit', '')), ''), used='shareUsedRes')
            for member in _rows(aggregate, 'allShareDetailInfo'):
                if card == '1' and member.get('usedTerms') != phone:
                    continue
                item['children'].append(_item(member, name=_text(member.get('usedTerms')) or '共享成员', scope=scope,
                    unit={'01': '分钟', '03': 'KB', '04': 'MB', '05': 'GB'}.get(str(member.get('unit', '')), ''), used='usedResSum'))
            items.append(item)
        if items:
            groups.append({'name': scope+'用量', 'items': items})
    if flow.get('limitRuleRes'):
        notes.append(_text(flow['limitRuleRes']))
    if flow.get('state') in ('01', '03'):
        notes.append('当前流量已限速'+('：'+_text(flow['limitRate'])+'Mbps' if flow.get('limitRate') else '。'))
    elif flow.get('state') in ('04', '05', '06', '07', '08', '09', '10', '11', '12', '13'):
        notes.append('当前流量已封顶。')
    for key, name, unit in (('flow', '国内通用流量', 'MB'), ('voice', '国内通用通话', '分钟'), ('sms', '短信', '条')):
        if not any(row['key'] == key for row in overview):
            overview.append({'key': key, 'name': name, 'unit': unit, 'total': None, 'used': None, 'remaining': None, 'scope': '', 'unlimited': False})
    if not groups and all(row['used'] is None and row['remaining'] is None for row in overview):
        notes.append('官方暂未返回余量明细。')
    for item in overview:
        _present_usage(item)
    for group in groups:
        for item in group['items']:
            _present_usage(item)
    return {'overview': overview, 'groups': groups, 'notes': notes, 'queried_at': now.isoformat()}


class UsageQueries:
    def __init__(self, credentials, *, recover=None, accept_credentials=None, app_factory=None, now=None):
        self.credentials, self.recover, self.accept_credentials = credentials, recover, accept_credentials
        self.app_factory = app_factory or AppLoginSession.from_credentials
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.lock, self.pending = RLock(), {}

    @staticmethod
    def _call(flow, path, payload):
        try:
            _, value, _ = flow.call(path, payload)
        except AppLoginError as error:
            raise UsageError('连接官方余量服务失败，请稍后手动刷新。', 'USAGE_NETWORK') from error
        code = str(value.get('retCode', 'unknown'))
        if code != '000000':
            if code in AUTH_CODES:
                raise UsageError('APP 会话需要恢复，请重新登录对应共享账号。', 'USAGE_LOGIN_REQUIRED', 401)
            if code in ('-800', '8053') or '频繁' in str(value.get('retDesc', '')):
                raise UsageError('官方提示请求频繁，请稍后再试；尝试更换服务ip再试。', 'RATE_LIMITED', 429)
            code = code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}', code) else 'unknown'
            raise UsageError('官方余量查询未完成（'+code+'）：'+_text(value.get('retDesc') or '请稍后再试。')[:200], code)
        body = value.get('rspBody')
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as error:
                raise UsageError('官方余量响应无法解析。', 'USAGE_FORMAT') from error
        if not isinstance(body, dict):
            raise UsageError('官方余量响应格式异常。', 'USAGE_FORMAT')
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
                raise UsageError('共享账号不存在，请重新选择账号。', 'USAGE_LOGIN_REQUIRED', 401)
            phone, recovered = snapshot['phone'], False
            if not snapshot.get('cookie'):
                if self.recover is None:
                    raise UsageError('共享账号尚未登录，请在账号管理恢复登录。', 'USAGE_LOGIN_REQUIRED', 401)
                try:
                    self.recover(snapshot)
                except AppLoginError as error:
                    raise UsageError('APP 会话未能恢复，请完成验证或重新登录后查询。', 'USAGE_LOGIN_REQUIRED', 401) from error
                snapshot, recovered = self.credentials(login_id), True
                if snapshot is None or snapshot['phone'] != phone or not snapshot.get('cookie'):
                    raise UsageError('共享账号尚未登录，请在账号管理恢复登录。', 'USAGE_LOGIN_REQUIRED', 401)
            flow, now = self.app_factory(snapshot), self.now()
            last_day = calendar.monthrange(now.year, now.month)[1]
            endpoints = [(REMAIN_PATH, {}, '余量'), (PLAN_PATH, {}, '套餐说明'),
                         (COUNTRY_PATH, {'currentPeriodStartTm': now.strftime('%Y%m')+'01000000',
                                         'currentPeriodEndTm': now.strftime('%Y%m')+f'{last_day:02d}235959'}, '国际及港澳台用量'),
                         (SHARE_PATH, {}, '共享用量')]
            bodies, warnings = [{}, {}, {}, {}], []
            with requests.Session() as transport:
                flow.transport = transport
                for index, (path, params, label) in enumerate(endpoints):
                    try:
                        for attempt in range(2):
                            try:
                                bodies[index] = self._call(flow, path, {'cellNum': phone, **params})
                            except UsageError as error:
                                if error.code != 'USAGE_LOGIN_REQUIRED' or recovered or attempt or self.recover is None:
                                    raise
                                recovered = True
                                try:
                                    self.recover(snapshot)
                                except AppLoginError as recovery_error:
                                    raise UsageError('APP 会话未能恢复，请完成验证或重新登录后查询。', 'USAGE_LOGIN_REQUIRED', 401) from recovery_error
                                snapshot = self.credentials(login_id)
                                if snapshot is None or snapshot['phone'] != phone or not snapshot.get('cookie'):
                                    raise UsageError('共享账号已退出或身份已变更，请重新选择账号。', 'USAGE_LOGIN_REQUIRED', 401)
                                flow = self.app_factory(snapshot)
                                flow.transport = transport
                                continue
                            break
                    except UsageError as error:
                        if index == 0:
                            raise
                        warnings.append(label+'未更新：'+str(error))
                        if error.code in ('RATE_LIMITED', 'USAGE_LOGIN_REQUIRED'):
                            break
            if self.accept_credentials is not None and flow.cookie != snapshot['cookie']:
                self.accept_credentials(snapshot, flow)
            result = normalize_usage(bodies[0], bodies[1], bodies[2], now, share=bodies[3], phone=phone)
            result['notes'].extend(warnings)
            future.set_result(result)
            return deepcopy(result)
        except Exception as error:
            future.set_exception(error)
            raise
        finally:
            with self.lock:
                self.pending.pop(login_id, None)


def register_usage(app, accounts, credentials, *, recover=None, accept_credentials=None):
    queries = UsageQueries(credentials, recover=recover, accept_credentials=accept_credentials)
    app.extensions['usage_queries'] = queries

    @app.get('/api/usage/query')
    def usage_query():
        login_id = request.args.get('login_id')
        account = next((row for row in accounts() if row['id'] == login_id), None)
        if account is None:
            return jsonify(error='请选择已有的共享账号。', code='ACCOUNT_NOT_FOUND'), 404
        try:
            result = queries.query(login_id)
        except UsageError as error:
            account = next((row for row in accounts() if row['id'] == login_id), account)
            return jsonify(error=str(error), code=error.code, account=account), error.status
        result['account'] = next((row for row in accounts() if row['id'] == login_id), account)
        return jsonify(result)
