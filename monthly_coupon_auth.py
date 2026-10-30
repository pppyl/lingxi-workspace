"""Guizhou activity authentication through the shared China Mobile APP login.

The APP's getYDRZToken bridge calls getBigNetToken; the provincial page then
exchanges that short-lived token with /activityWeb/login (type 4).
"""
from contextlib import contextmanager
import json
import re
from urllib.parse import parse_qs, quote, unquote, urlsplit

from http_transport import requests

from app_login import AppLoginError, AppLoginSession
from recharge.cmcc import APP_UA


ORIGIN = 'https://wap.gz.10086.cn'
PLATFORM = ORIGIN + '/activityWeb'
APP_TOKEN_PATH = '/leadeon-abilityopen-biz/BN/obtainToken/getBigNetToken'


class MonthlyCouponAuthError(Exception):
    def __init__(self, message, code='MONTHLY_AUTH_ERROR', status=502):
        super().__init__(message)
        self.code, self.status = code, status


@contextmanager
def guizhou_session(credentials_provider, entry_url, *, session_factory=None):
    """Yield an ephemeral authenticated session; no credentials are persisted.

    Callers remain responsible for checking the shared credential revision
    immediately before any activity mutation after this context is entered.
    """
    if not isinstance(entry_url, str):
        raise MonthlyCouponAuthError('请选择贵州移动官方活动入口', 'INVALID_ENTRY', 400)
    entry = urlsplit(entry_url)
    if (entry.scheme != 'https' or entry.netloc != 'wap.gz.10086.cn'
            or not entry.path.startswith('/activityWeb/')
            or '..' in unquote(entry.path).split('/')):
        raise MonthlyCouponAuthError('请选择贵州移动官方活动入口', 'INVALID_ENTRY', 400)
    snapshot = credentials_provider()
    if (not isinstance(snapshot, dict) or not isinstance(snapshot.get('cookie'), str)
            or not re.search(r'(?i)(?:^|;)\s*JSESSIONID=', snapshot['cookie'])
            or not re.fullmatch(r'1[3-9]\d{9}', str(snapshot.get('phone', '')))):
        raise MonthlyCouponAuthError('共享 APP 登录不可用，请在账号管理重新登录', 'APP_LOGIN_REQUIRED', 401)

    def current():
        latest = credentials_provider()
        if (latest is None or latest.get('cookie') != snapshot['cookie']
                or latest.get('phone') != snapshot['phone']
                or latest.get('credential_revision') != snapshot.get('credential_revision')):
            raise MonthlyCouponAuthError('共享账号凭据已变更，请使用最新登录态重试', 'APP_CREDENTIALS_CHANGED', 409)

    client = session_factory() if session_factory else requests.Session(impersonate='chrome')
    client.headers.update({'User-Agent': APP_UA, 'Origin': ORIGIN, 'Referer': entry_url,
                           'Content-Type': 'application/json;charset=utf-8'})
    try:
        current()
        flow = AppLoginSession.from_credentials(snapshot)
        try:
            _, response, _ = flow.call(APP_TOKEN_PATH, {
                'sourceId': '030011', 'url': entry_url, 'cellNum': snapshot['phone'],
            })
        except AppLoginError as error:
            raise MonthlyCouponAuthError('APP 活动认证暂未完成，请校验并重连后重试', 'APP_TOKEN_FAILED') from error
        if str(response.get('retCode')) != '000000':
            raise MonthlyCouponAuthError('APP 未返回活动认证令牌，请校验并重连后重试', 'APP_TOKEN_FAILED')
        body = response.get('rspBody')
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError as error:
                raise MonthlyCouponAuthError('APP 活动认证响应格式异常', 'APP_TOKEN_FORMAT') from error
        if not isinstance(body, dict) or not isinstance(body.get('token'), str) or not body['token']:
            raise MonthlyCouponAuthError('APP 活动认证缺少令牌', 'APP_TOKEN_FORMAT')
        current()
        query = parse_qs(entry.query)
        ref = {'refCnl': query.get('referChannels', ['1000000002110700006'])[0],
               'fullUrl': quote(entry_url, safe="~()*!.'-")}
        for field, key in [('ChannelPosition', 'cnlPos'), ('actId', 'actId'), ('clickCode', 'clickCode')]:
            if field in query:
                ref[key] = query[field][0]
        try:
            login = client.post(PLATFORM + '/login', json={
                'type': 4, 'key': body['token'], 'refParamRecord': ref,
                'mpToken': '', 'assistantUserInfo': '', 'longitude': '', 'latitude': '',
            }, timeout=(10, 25), allow_redirects=False)
        except requests.RequestsError as error:
            raise MonthlyCouponAuthError('连接贵州活动认证服务失败，请稍后重试', 'UPSTREAM_NETWORK') from error
        if login.status_code != 200:
            raise MonthlyCouponAuthError('贵州活动认证 HTTP ' + str(login.status_code), 'AUTH_HTTP')
        try:
            value = login.json()
        except ValueError as error:
            raise MonthlyCouponAuthError('贵州活动认证响应格式异常', 'AUTH_FORMAT') from error
        if not isinstance(value, dict) or value.get('code') != 200:
            raise MonthlyCouponAuthError('贵州活动认证暂未完成，请校验并重连后重试', 'AUTH_FAILED')
        data = value.get('data')
        if (not isinstance(data, dict) or not isinstance(data.get('token'), str) or not data['token']
                or not isinstance(data.get('serviceNum'), str) or not data['serviceNum']):
            raise MonthlyCouponAuthError('贵州活动认证缺少会话信息', 'AUTH_FORMAT')
        number = data['serviceNum']
        if re.fullmatch(r'1\d{10}', number) and number != snapshot['phone']:
            raise MonthlyCouponAuthError('活动认证返回的账号不一致，本次已停止', 'ACCOUNT_MISMATCH', 409)
        current()
        client.headers['AuthorizationAweb'] = 'Bearer ' + data['token']
        client.cookies.set('ActivityWeb-Token', data['token'], domain='wap.gz.10086.cn', path='/')
        client.cookies.set('serviceNum', number, domain='wap.gz.10086.cn', path='/')
        yield client
    finally:
        client.close()
