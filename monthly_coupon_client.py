"""Protocol of the Guizhou grc page: monthly coupons and four expansion tasks.

Source: /activityWeb/static/activity/grc/js/index.js, observed 2026-10-29.
The official task button saves taskType first, then navigates to its landing
page. There is no elapsed-time completion callback in this shipped page.
"""
from email.utils import parsedate_to_datetime
import html
import math
import re
from urllib.parse import urljoin, urlsplit

from http_transport import requests

from monthly_coupon_auth import guizhou_session, MonthlyCouponAuthError, PLATFORM, DEFAULT_ENTRY_URL, referral_params
from recharge.cmcc import APP_UA, SHANGHAI, parse_server_time


ENTRY = DEFAULT_ENTRY_URL
TASKS = (
    ('bill', '查询个人账单', 'taskOne', '1', 'https://wx.10086.cn/cmcc-service/app-pages/extra/queryPolymerize'),
    ('signin', '浏览签到活动', 'taskTwo', '2', 'https://wx.10086.cn/qwhdhub/qwhdmark/1021122301?channelId=P00000002815'),
    ('age', '浏览网龄专区', 'taskThree', '3', 'https://dev.coc.10086.cn/coc/web6/ageFeedbackV2/?pageId=1879720710683729920&channelId=P00000003727'),
)
PATHS = {'initInfo', 'prizeList', 'receiveCoupon', 'saveExpTask', 'receiveExpCoupon'}
QUALIFICATION_CODES = {911, 912, 913}


class MonthlyCouponError(MonthlyCouponAuthError):
    def __init__(self, message, code='MONTHLY_COUPON_ERROR', status=502, *, uncertain=False,
                 rejected_action=None, rejected_period=None):
        super().__init__(message, code, status)
        self.uncertain = uncertain
        self.rejected_action, self.rejected_period = rejected_action, rejected_period


def _plain(value):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', str(value or '')))).strip()


def _server_time(response, data):
    stamp = response.headers.get('Date')
    if stamp:
        try:
            return parsedate_to_datetime(stamp).astimezone(SHANGHAI)
        except (TypeError, ValueError, OverflowError):
            pass
    now = parse_server_time(data.get('sysTime') or data.get('serverTime'))
    if now is None:
        raise MonthlyCouponError('未取得官方服务器时间，本次未提交', 'SERVER_TIME_MISSING')
    return now


def _stock(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


class MonthlyCouponClient:
    def __init__(self, credentials_provider, *, entry_url=ENTRY, session_provider=guizhou_session, visitor=None, credential_cache=None):
        self.credentials_provider = credentials_provider
        self.entry_url = entry_url
        self.credential_cache = credential_cache
        self.session_provider = session_provider if credential_cache is None else (lambda provider,entry:session_provider(provider,entry,credential_cache=credential_cache))
        self.visitor = visitor or self._visit
        self.observation = None

    def _current(self, snapshot):
        latest = self.credentials_provider()
        if (latest is None or latest.get('phone') != snapshot.get('phone')
                or self.credential_cache is None and latest.get('cookie') != snapshot.get('cookie')):
            raise MonthlyCouponError('共享账号凭据已更新，请使用最新登录态重试', 'APP_CREDENTIALS_CHANGED', 409)

    def _get(self, client, path, params=None, *, action=None, period=None, snapshot=None, before_submit=None):
        if path not in PATHS:
            raise ValueError('Unsupported monthly-coupon endpoint')
        if action:
            self._current(snapshot)
            if before_submit is not None and before_submit(action, period) is False:
                return None, None
        payload = {**referral_params(self.entry_url),
                   'longitude': '', 'latitude': '', **(params or {})}
        try:
            response = client.get(PLATFORM + '/grcNew/' + path, params=payload,
                                  timeout=(10, 25), allow_redirects=False)
        except requests.RequestsError as error:
            raise MonthlyCouponError('请求中断，结果待核实；本次未自动重发' if action else '连接每月话费券服务失败，请稍后重试',
                                     'RESULT_UNCERTAIN' if action else 'UPSTREAM_NETWORK', uncertain=bool(action)) from error
        if response.status_code not in (200,401):
            raise MonthlyCouponError('每月话费券服务 HTTP ' + str(response.status_code), 'UPSTREAM_HTTP', uncertain=bool(action))
        try:
            value = {'code':401} if response.status_code == 401 else response.json()
        except ValueError as error:
            raise MonthlyCouponError('官方响应格式异常，请在 APP 核实', 'UPSTREAM_FORMAT', uncertain=bool(action)) from error
        if not isinstance(value, dict) or isinstance(value.get('code'), bool) or not isinstance(value.get('code'), int):
            raise MonthlyCouponError('官方响应格式异常，请在 APP 核实', 'UPSTREAM_FORMAT', uncertain=bool(action))
        if value['code'] == 401:
            if self.credential_cache is not None and getattr(client,'_credential_binding',None):
                if not action and self.credential_cache.recover(client):
                    return self._get(client,path,params)
                self.credential_cache.invalidate(client)
            raise MonthlyCouponError('贵州活动会话需要重新校验，请在账号管理重连', 'ACTIVITY_LOGIN_REQUIRED', 401,
                                     rejected_action=action, rejected_period=period)
        if value['code'] not in {200, 900, 901, 902, *QUALIFICATION_CODES} and value.get('msg') != '今日已签到':
            raise MonthlyCouponError('官方暂未完成此操作（' + str(value['code']) + '）', 'UPSTREAM_' + str(value['code']), uncertain=bool(action))
        return value, response

    @staticmethod
    def _rejection(value, path, action=None, period=None):
        code = value['code']
        agreement = (path == 'initInfo' and code == 901
                     or path in ('receiveCoupon', 'receiveExpCoupon') and code == 900)
        if agreement:
            raise MonthlyCouponError('活动协议待确认，执行时将自动处理', 'AGREEMENT_REQUIRED', 409,
                                     rejected_action=action, rejected_period=period)
        if code in QUALIFICATION_CODES:
            raise MonthlyCouponError('本活动仅限符合条件的贵州移动实名正常账号', 'NOT_ELIGIBLE', 409,
                                     rejected_action=action, rejected_period=period)
        if path == 'initInfo' and code == 900:
            raise MonthlyCouponError('当前不在每月话费券活动时间内', 'ACTIVITY_CLOSED', 409)

    def _accept_agreement(self, client, response, snapshot, stage, agreements):
        if stage in agreements:
            raise MonthlyCouponError('已尝试确认活动协议，官方仍未通过，本轮已停止', 'AGREEMENT_FAILED', 409)
        agreement = response.get('data')
        if isinstance(agreement, dict) and isinstance(agreement.get('data'), dict):
            agreement = agreement['data']
        if (not isinstance(agreement, dict) or not isinstance(agreement.get('agreementName'), str)
                or not agreement['agreementName'].strip() or not isinstance(agreement.get('agreementContent'), str)
                or not _plain(agreement['agreementContent'])
                or not isinstance(agreement.get('cfmLogId'), (str, int))
                or isinstance(agreement['cfmLogId'], bool) or not str(agreement['cfmLogId']).strip()):
            raise MonthlyCouponError('官方未返回完整活动协议，本轮未提交协议确认', 'AGREEMENT_FORMAT', 409)
        self._current(snapshot)
        # User authorized automatic acceptance of this activity's agreement.
        # Match agrLog in the shipped page and keep its original authenticated session.
        agreements[stage] = agreement['agreementName']
        payload = {'id':'', 'bizName':'首页', 'cfmLogId':agreement['cfmLogId'],
                   'refParamRecord':referral_params(self.entry_url), 'mpToken':'',
                   'assistantUserInfo':'', 'longitude':'', 'latitude':''}
        try:
            result = client.post(PLATFORM+'/grcNew/signAgr', json=payload,
                                 timeout=(10, 25), allow_redirects=False)
        except requests.RequestsError as error:
            raise MonthlyCouponError('协议确认响应中断，结果待核实；本轮未继续领取', 'AGREEMENT_UNCERTAIN', 409) from error
        if result.status_code != 200:
            raise MonthlyCouponError('协议确认 HTTP '+str(result.status_code)+'，本轮未继续领取', 'AGREEMENT_FAILED', 409)
        try:
            value = result.json()
        except ValueError as error:
            raise MonthlyCouponError('协议确认响应格式异常，本轮未继续领取', 'AGREEMENT_UNCERTAIN', 409) from error
        if not isinstance(value, dict) or value.get('code') != 200:
            raise MonthlyCouponError('官方未确认活动协议，本轮未继续领取', 'AGREEMENT_FAILED', 409)
        self._current(snapshot)

    def _inspect(self, client, knowledge=None, *, snapshot=None, agreements=None):
        raw, response = self._get(client, 'initInfo')
        if raw['code'] == 901 and agreements is not None:
            self._accept_agreement(client, raw, snapshot, 'initInfo', agreements)
            raw, response = self._get(client, 'initInfo')
            if raw['code'] == 901:
                raise MonthlyCouponError('协议确认后官方仍未开放活动，本轮已停止', 'AGREEMENT_FAILED', 409)
        self._rejection(raw, 'initInfo')
        if raw['code'] != 200 or not isinstance(raw.get('data'), dict):
            raise MonthlyCouponError('官方未返回可核实的本月任务状态', 'STATUS_UNAVAILABLE', 409)
        data = raw['data']
        now = _server_time(response, data)
        task_info = data.get('grcExpTask') or {}
        if not isinstance(task_info, dict):
            raise MonthlyCouponError('官方任务状态格式异常，本次未提交', 'UPSTREAM_FORMAT')
        sign_num = data.get('signNum')
        if isinstance(sign_num, bool) or not isinstance(sign_num, int) or not 0 <= sign_num <= 3:
            raise MonthlyCouponError('未取得真实打卡次数，本次未提交', 'PUNCH_STATE_MISSING')
        tasks = [{'id': key, 'name': name, 'done': int(task_info.get(field) == '1'), 'total': 1}
                 for key, name, field, _, _ in TASKS]
        punch_done = task_info.get('taskFour') == '1'
        progress = sum(t['done'] * 20 for t in tasks) + (40 if punch_done else 0)
        prize_data, _ = self._get(client, 'prizeList')
        if prize_data['code'] != 200 or not isinstance(prize_data.get('data'), list):
            raise MonthlyCouponError('未能核对官方奖品记录，本次未提交', 'PRIZE_STATE_MISSING')
        result = {'date': now.strftime('%Y%m%d'), 'month': now.strftime('%Y%m'), 'server_time': now.isoformat(),
                  'eligible': True, 'direct_state': 'unknown', 'direct_prize': '',
                  'direct_message': '本月领取结果待核实', 'bonus_state': 'pending' if progress < 100 else 'unknown', 'bonus_prize': '',
                  'bonus_message': '任务完成后可领取' if progress < 100 else '任务已完成，领取结果待核实',
                  'task_progress': progress, 'tasks': tasks, 'punch_days': 3 if punch_done else sign_num,
                  'punched_today': False if sign_num == 0 else None, 'message': '官方任务状态已更新'}
        if knowledge and knowledge['month'] == result['month']:
            for kind in ('direct', 'bonus'):
                if knowledge.get(kind+'_state') in ('claimed', 'participated'):
                    for field in ('state', 'message', 'prize', 'receipt'):
                        key = kind+'_'+field
                        if key in knowledge:
                            result[key] = knowledge[key]
            if knowledge.get('date') == result['date'] and 'punched_today' in knowledge:
                result['punched_today'] = knowledge['punched_today']
        return result, {'direct': _stock(data.get('couponStock')), 'bonus': _stock(data.get('expCouponStock'))}

    def inspect(self):
        snapshot = self.credentials_provider()
        with self.session_provider(self.credentials_provider, self.entry_url) as client:
            result, _ = self._inspect(client)
            self._current(snapshot)
            return result

    @staticmethod
    def _visit(url):
        # A separate visitor prevents the Guizhou AuthorizationAweb header from
        # being forwarded to a different official host on task navigation.
        origins = {'https://wx.10086.cn', 'https://dev.coc.10086.cn', 'https://h.app.coc.10086.cn'}
        with requests.Session(impersonate='chrome') as visitor:
            visitor.headers['User-Agent'] = APP_UA
            for _ in range(8):
                parsed = urlsplit(url)
                if parsed.scheme + '://' + parsed.netloc not in origins or parsed.username or parsed.password:
                    raise MonthlyCouponError('任务落地页出现不支持的跳转，本次已停止', 'TASK_REDIRECT', 409)
                try:
                    response = visitor.get(url, timeout=(10, 25), allow_redirects=False)
                except requests.RequestsError as error:
                    raise MonthlyCouponError('任务已登记，落地页访问中断，请在 APP 核实', 'TASK_VISIT_FAILED') from error
                if response.status_code in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get('Location', ''))
                elif response.status_code == 200:
                    return
                else:
                    raise MonthlyCouponError('任务已登记，落地页访问暂未完成，请在 APP 核实', 'TASK_VISIT_FAILED')
        raise MonthlyCouponError('任务落地页跳转次数过多，请在 APP 核实', 'TASK_REDIRECT', 409)

    def execute(self, *, before_submit, knowledge=None):
        snapshot = self.credentials_provider()
        agreements = {}
        with self.session_provider(self.credentials_provider, self.entry_url) as client:
            result, stocks = self._inspect(client, knowledge, snapshot=snapshot, agreements=agreements)
            self.observation = dict(result)
            knowledge = dict(result)
            messages = []
            steps = [('direct', 'receiveCoupon', {}, None)]
            steps += [(key, 'saveExpTask', {'taskType': task_type}, url) for key, _, _, task_type, url in TASKS]
            steps += [('punch', 'saveExpTask', {'taskType': '4'}, None), ('bonus', 'receiveExpCoupon', {}, None)]
            for action, path, params, url in steps:
                if action == 'direct' and (result['direct_state'] in ('claimed', 'participated') or stocks['direct'] == 0):
                    continue
                if action == 'bonus' and (result['bonus_state'] in ('claimed', 'participated') or result['task_progress'] < 100 or stocks['bonus'] == 0):
                    continue
                if action in ('bill', 'signin', 'age') and (next(t for t in result['tasks'] if t['id'] == action)['done'] or stocks['bonus'] == 0):
                    continue
                if action == 'punch' and (result['punch_days'] >= 3 or result['punched_today'] is True or stocks['bonus'] == 0):
                    continue
                if stocks['direct' if action == 'direct' else 'bonus'] is None:
                    messages.append('未取得官方库存，本次未提交')
                    continue
                period = result['date'] if action == 'punch' else result['month']
                value, _ = self._get(client, path, params, action=action, period=period,
                                     snapshot=snapshot, before_submit=before_submit)
                if value is None:
                    messages.append('本周期已有提交记录，结果待核实，未重复发送')
                    continue
                if path in ('receiveCoupon', 'receiveExpCoupon') and value['code'] == 900:
                    try:
                        self._accept_agreement(client, value, snapshot, path, agreements)
                    except MonthlyCouponError as error:
                        # 900 definitively rejected the coupon claim. Only the
                        # agreement may be uncertain; no coupon was submitted again.
                        error.rejected_action, error.rejected_period = action, period
                        raise
                    # Reuse the durable intent committed before the rejected
                    # claim; retry this step once, without writing a second marker.
                    value, _ = self._get(client, path, params, action=action, period=period,
                                         snapshot=snapshot)
                    if value['code'] == 900:
                        raise MonthlyCouponError('协议确认后官方仍要求确认，本轮已停止', 'AGREEMENT_FAILED', 409,
                                                 rejected_action=action, rejected_period=period)
                self._rejection(value, path, action, period)
                if path in ('receiveCoupon', 'receiveExpCoupon') and value['code'] in (200, 901):
                    prefix = 'direct' if action == 'direct' else 'bonus'
                    knowledge[prefix + '_state'] = 'participated' if value['code'] == 901 else 'claimed'
                    knowledge[prefix + '_message'] = ('官方提示本月参与次数已用完（901），奖品名称与发券结果待核实'
                        if value['code'] == 901 else '领取成功，请在 APP 我的卡券查看')
                    knowledge[prefix + '_receipt'] = {'source': 'claim_response', 'code': value['code'], 'month': result['month']}
                    data = value.get('data') or {}
                    if value['code'] == 200 and isinstance(data, dict):
                        knowledge[prefix + '_prize'] = _plain(data.get('resourceName') or data.get('directionsForUse'))[:160]
                    else:
                        knowledge[prefix + '_prize'] = ''
                    messages.append(knowledge[prefix + '_message'])
                    self.observation = {**result, **knowledge}
                elif action == 'punch' and (value['code'] == 200 or value.get('msg') == '今日已签到'):
                    knowledge['punched_today'] = True
                    self.observation = {**result, **knowledge}
                    messages.append('今日打卡已完成')
                elif path == 'saveExpTask' and value['code'] == 200:
                    self.visitor(url)
                elif value['code'] == 902:
                    messages.append('官方奖品已领完，本次未参与')
                else:
                    raise MonthlyCouponError('官方尚未确认此项任务（' + str(value['code']) + '）', 'ACTION_UNCONFIRMED')
                result, stocks = self._inspect(client, knowledge, snapshot=snapshot, agreements=agreements)
                self.observation = dict(result)
            if agreements:
                messages.insert(0, '活动协议已自动确认')
                result['agreements_confirmed'] = list(agreements.values())
            result['message'] = '；'.join(dict.fromkeys(messages)) or '官方状态已核对，本次没有需要重复提交的任务'
            return result
