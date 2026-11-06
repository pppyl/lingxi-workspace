"""Guizhou daily free wheel: official template/scenes/agreement/draw in Python.

Confirmed template V6WJp8, app.1eb07fc19459fd1c003d.js and /ac/nlInit.
No page engine executes any part of the business protocol.
"""
from copy import deepcopy
from datetime import datetime
import html
import re
from urllib.parse import unquote

from feature_runtime import feature_running
from http_transport import requests
from monthly_coupon_auth import (PLATFORM, MonthlyCouponAuthError,
                                 guizhou_session, referral_params)
from recharge.cmcc import SHANGHAI

ACT_ID = 'V6WJp8'
ENTRY = PLATFORM + '/static/templet/act/index.html?actId=' + ACT_ID
PROTOCOL = {
    'template_id': '2db20bc7-2f68-4fc2-8bd5-22f76d544376',
    'draw_comp': '21208b89-520a-4cb8-8cbb-49feadc4e3d5',
    'agreement_ids': [23212],
    'draw_scenes': [10, 4, 7, 7],
}
DEFAULT_CAMPAIGN = {
    'title': '天天转金喜', 'start_at': '2026-08-01T00:00:00+08:00',
    'end_at': '2027-03-31T23:59:59+08:00',
    'rules_text': '贵州移动实名正常号码每天可免费抽奖一次，当日有效、不累积。奖品以官方结果为准，可能延迟48小时到账；卡券有效期7天。',
    'protocol': PROTOCOL,
}


class DailyGoldError(Exception):
    def __init__(self, message, code='DAILY_GOLD_ERROR', status=502, *, uncertain=False):
        super().__init__(message)
        self.code, self.status, self.uncertain = str(code), status, uncertain


def campaign_config(*, session=None):
    """Public template is shared by all accounts; scheduler persists it daily."""
    client = session or requests.Session(impersonate='chrome')
    try:
        response = client.get(PLATFORM+'/ac/nlInit', params={'actId': ACT_ID}, timeout=(5, 15))
        response.raise_for_status()
        value = response.json()
        if value.get('code') != 200:
            raise DailyGoldError('官方活动配置暂不可用。', 'CONFIG_FAILED')
        import json
        page = json.loads(value['data']['pageDetail'])
        components = [(board['id'], component) for board in page['list']
                      for component in board['boardData']['compList']]
        wheel = next((board, component) for board, component in components
                     if component['cpName'] == 'BigWheelPointNew')
        agreement = next(component for _, component in components if component['cpName'] == 'Agree')
        rules = next(unquote(component['options']['config']['text']) for _, component in components
                     if 'config' in component['options'] and
                     '活动有效期' in unquote(component['options']['config'].get('text', '')))
        rules = html.unescape(re.sub(r'<[^>]+>', '', rules))
        end = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d\d:\d\d:\d\d)', rules)
        if end is None:
            raise ValueError('missing official deadline')
        deadline = datetime(int(end[1]), int(end[2]), int(end[3]),
                            *map(int, end[4].split(':')), tzinfo=SHANGHAI)
        return {**deepcopy(DEFAULT_CAMPAIGN), 'end_at': deadline.isoformat(), 'rules_text': rules,
                'protocol': {'template_id': wheel[0], 'draw_comp': wheel[1]['id'],
                    'agreement_ids': json.loads(agreement['options']['data']['agrId']),
                    'draw_scenes': [scene['condList'][-1]['condType']
                                    for scene in wheel[1]['options']['event']['sceneList']]}}
    except (requests.RequestsError, ValueError, KeyError, TypeError, StopIteration) as error:
        raise DailyGoldError('暂时无法读取官方活动配置，沿用已确认的活动期限。', 'CONFIG_FAILED') from error
    finally:
        if session is None:
            client.close()


class DailyGoldClient:
    def __init__(self, credentials_provider, *, credential_cache=None, session_factory=None,
                 protocol=None, now=None):
        self.provider, self.cache, self.session_factory = credentials_provider, credential_cache, session_factory
        self.protocol = deepcopy(protocol or PROTOCOL)
        self.now = now or (lambda: datetime.now(SHANGHAI))
        self.session, self.context = None, None
        self.confirmed = None

    def connect(self):
        self.context = guizhou_session(self.provider, ENTRY, session_factory=self.session_factory,
                                       credential_cache=self.cache)
        try:
            self.session = self.context.__enter__()
        except MonthlyCouponAuthError as error:
            raise DailyGoldError(str(error), error.code, error.status) from error
        return self

    def close(self):
        if self.session is not None:
            self.context.__exit__(None, None, None)
            self.session = None

    def _call(self, path, payload=None, *, method='get', submission=False, recover=True):
        if not feature_running():
            raise DailyGoldError('功能已停止。', 'STOPPED', 409)
        fields = {'actId': ACT_ID, **(payload or {})}
        if method == 'post':
            fields.update(refParamRecord=referral_params(ENTRY), mpToken='', assistantUserInfo='',
                          longitude='', latitude='')
        else:
            fields.update(referral_params(ENTRY), mpToken='', assistantUserInfo='', longitude='', latitude='')
        try:
            response = self.session.request(method, PLATFORM+path,
                **({'json': fields} if method == 'post' else {'params': fields}), timeout=(5, 20))
            response.raise_for_status()
            value = response.json()
        except (requests.RequestsError, ValueError) as error:
            raise DailyGoldError('官方响应未完整返回，将先核对结果。' if submission else '连接官方活动失败，请稍后重试。',
                                 'RESULT_UNCERTAIN' if submission else 'UPSTREAM_NETWORK',
                                 uncertain=submission) from error
        if not isinstance(value, dict) or not isinstance(value.get('code'), int):
            raise DailyGoldError('官方响应格式异常。', 'UPSTREAM_FORMAT', uncertain=submission)
        code = value['code']
        if code == 200:
            return value.get('data')
        if code == 401:
            if not submission and recover and self.cache is not None:
                try:
                    if self.cache.recover(self.session):
                        return self._call(path, payload, method=method, recover=False)
                except MonthlyCouponAuthError as error:
                    raise DailyGoldError(str(error), error.code, error.status) from error
            raise DailyGoldError('活动登录已失效，请恢复共享账号后再试。', 'APP_LOGIN_REQUIRED', 401)
        message = str(value.get('msg') or '官方暂未允许此操作')[:350]
        if code == 429 or str(code) in ('-800', '8053') or any(text in message for text in ('频繁', '过于频', '限流')):
            raise DailyGoldError(message+'；尝试更换服务ip再试。', 'RATE_LIMITED', 429)
        if code in (505, 508):
            raise DailyGoldError('官方要求安全验证，请在中国移动 APP 完成验证后再试。', 'VERIFICATION_REQUIRED', 409)
        if any(text in message for text in ('不符合', '不满足', '非贵州', '无资格')):
            raise DailyGoldError(message, 'INELIGIBLE', 403)
        raise DailyGoldError(message+'（'+str(code)+'）', str(code), uncertain=submission and code == 506)

    def observe(self):
        # The official page uses these two queries; querying never spins the wheel.
        remaining = self._call('/actHandle/restParticipateNum')
        rows = self._call('/actHandle/initActivities')
        if type(remaining) is not int or remaining < 0 or not isinstance(rows, list):
            raise DailyGoldError('官方进度响应格式异常，未提交抽奖。', 'UPSTREAM_FORMAT')
        prizes = []
        for row in rows:
            resource = row.get('actResource') or {}
            name = resource.get('resourceName') or resource.get('name') or row.get('prizeName')
            if not name:
                continue
            received = row.get('createTime') or row.get('receiveTime') or row.get('effTime') or ''
            status = row.get('statusName') or row.get('receiveStatusName') or '已获得'
            prizes.append({'name': str(name), 'status': str(status), 'received_at': str(received)})
        today = self.now().date().isoformat()
        signed = any(prize['received_at'].startswith(today) for prize in prizes)
        if self.confirmed and self.confirmed['date'] == today:
            signed = True
            if not prizes and self.confirmed['prizes']:
                prizes = deepcopy(self.confirmed['prizes'])
        return {'date': today, 'signed': signed, 'remaining': remaining, 'prizes': prizes}

    def _scene(self):
        value = self._call('/ac/sceneValid', {'tempId': self.protocol['template_id'],
            'compId': self.protocol['draw_comp'], 'cfmLogId': '', 'sid': '', 'sharePhone': '',
            'verifyCode': '', 'verifyCompId': '', 'verifyId': '', 'openToken': '',
            'videoAuthId': '', 'previewId': ''}, method='post')
        if not isinstance(value, dict) or type(value.get('sceneIdx')) is not int:
            raise DailyGoldError('官方参与条件响应格式异常，未提交抽奖。', 'UPSTREAM_FORMAT')
        index = value['sceneIdx']
        if not 0 <= index < len(self.protocol['draw_scenes']):
            raise DailyGoldError('官方未确认可参加此活动。', 'INELIGIBLE', 403)
        return self.protocol['draw_scenes'][index], value

    def signin(self):
        condition, scene = self._scene()
        if condition == 4:
            # First participation follows the free activity agreement shown by
            # the APP. Read the current cfmLogId, never fabricate or reuse it.
            for agreement_id in self.protocol['agreement_ids']:
                agreement = self._call('/actHandle/agreementById/'+str(agreement_id))
                if not isinstance(agreement, dict) or not agreement.get('agrId') or not agreement.get('cfmLogId'):
                    raise DailyGoldError('官方未返回完整活动协议，未提交抽奖。', 'AGREEMENT_FORMAT')
                self._call('/actHandle/signAgree', {'agrId': agreement['agrId'], 'cfmLogId': agreement['cfmLogId']},
                           method='post', submission=True)
            condition, scene = self._scene()
        if condition in (1, 10):
            raise DailyGoldError('官方未确认活动登录，请重新登录共享账号后再试。', 'APP_LOGIN_REQUIRED', 401)
        if condition != 7 or not scene.get('validCode'):
            raise DailyGoldError('官方未确认可参加本次活动，今日自动检查暂停。', 'INELIGIBLE', 403)
        # This GET is a write in the official application. It must never be
        # retried by an auth recovery or transport retry handler.
        data = self._call('/actHandle/draw', {'compId': self.protocol['draw_comp'],
                          'validCode': scene['validCode'], 'cfmLogId': '', 'verifyId': ''}, submission=True)
        if not isinstance(data, dict) or not data.get('prizeName'):
            raise DailyGoldError('已发起抽奖，官方结果待核对，不会重复提交。', 'RESULT_UNCERTAIN', uncertain=True)
        result = {'date': self.now().date().isoformat(), 'signed': True, 'remaining': 0,
                  'prizes': [{'name': str(data['prizeName']), 'status': '官方已确认',
                              'received_at': self.now().isoformat()}]}
        self.confirmed = deepcopy(result)
        # Submission itself is authoritative, so a delayed history read cannot
        # turn a confirmed draw into a failure and cause another submission.
        return result
