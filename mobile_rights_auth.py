"""APP-to-Guizhou-WAP SSO, shared by rights, New Youth and AI beans."""
from copy import copy
from http.cookiejar import Cookie, CookieJar
import json
import logging
import random
import re
from urllib.parse import quote
from urllib.request import Request as CookieRequest

from app_login import AppLoginError, AppLoginSession
from http_transport import requests, RequestException
from monthly_coupon_auth import APP_TOKEN_PATH
from mobile_rights_gateway import page_signature
from recharge.cmcc import APP_UA

ORIGIN = 'https://wap.gz.10086.cn'
SCOPE = ORIGIN+'/waps'
ENTRY = SCOPE+'/static/activity/apRightPlatform/order_list.html?type=10'


class MobileRightsError(Exception):
    def __init__(self, message, code='MOBILE_RIGHTS_ERROR', status=502, *, authentication=False, retry_after=None):
        super().__init__(message)
        self.code, self.status, self.authentication = code, status, authentication
        self.retry_after=retry_after


def cookie_header(session, nonce, url=SCOPE+'/'):
    """One request's nonce never replaces another operation's CookieJar."""
    jar = CookieJar()
    for item in session.cookies.jar:
        if item.domain.lstrip('.') == 'wap.gz.10086.cn' and (item.name == 'absign' or re.fullmatch(r'0\.\d+', item.name)):
            continue
        jar.set_cookie(copy(item))
    jar.set_cookie(Cookie(0,nonce,'0',None,False,'wap.gz.10086.cn',True,False,'/waps',True,True,None,True,None,None,{},False))
    carrier = CookieRequest(url)
    jar.add_cookie_header(carrier)
    return carrier.get_header('Cookie', '')


def login_required(value):
    # WAP uses both its legacy envelope and {code:'0002',msg:...,data:null}.
    return str(value.get('LoginFlag')) == '1' or any(
        '未登录' in str(value.get(key,'')) for key in ('X_RESULTINFO','msg'))


def response_json(response):
    if response.status_code == 401 or 300 <= response.status_code < 400:
        raise MobileRightsError('移动权益会话未通过认证，请重新连接。', 'WAP_LOGIN_REQUIRED', 401, authentication=True)
    if response.status_code==403:
        raise MobileRightsError('移动权益服务拒绝当前请求（HTTP 403）；尝试更换服务ip再试。','WAP_FORBIDDEN',403)
    if response.status_code==429:
        raise MobileRightsError('官方提示请求频繁；尝试更换服务ip再试。','RATE_LIMITED',429)
    if response.status_code != 200:
        raise MobileRightsError('移动权益服务返回 HTTP '+str(response.status_code)+'，请稍后重试。', 'WAP_HTTP')
    try:
        value = response.json()
    except ValueError as error:
        raise MobileRightsError('移动权益返回内容无法识别，请稍后重新查询。', 'WAP_FORMAT') from error
    if not isinstance(value, dict):
        raise MobileRightsError('移动权益返回内容格式异常。', 'WAP_FORMAT')
    if login_required(value):
        raise MobileRightsError('移动权益会话已失效，请重新连接。', 'WAP_LOGIN_REQUIRED', 401, authentication=True)
    message = str(value.get('X_RESULTINFO') or value.get('msg') or '')
    if str(value.get('X_RESULTCODE',value.get('code'))) in ('-800','8053') or '请求频繁' in message:
        raise MobileRightsError('官方提示请求频繁；尝试更换服务ip再试。', 'RATE_LIMITED', 429)
    return value


def wap_session(snapshot, cache, *, app_factory=AppLoginSession.from_credentials, session_factory=None,
                recover=None, accept_credentials=None):
    """Bootstrap one WAP realm; valid cached WAP sessions ignore APP revisions."""
    client = session_factory() if session_factory else requests.Session(impersonate='chrome')
    client.headers.update({'User-Agent':APP_UA,'Referer':ENTRY,'Origin':ORIGIN,
                          'Accept':'application/json, text/javascript, */*; q=0.01',
                          'X-Requested-With':'XMLHttpRequest'})

    def initialize():
        nonlocal snapshot
        for attempt in range(2):
            if not snapshot.get('cookie'):
                failure = None
            else:
                flow = app_factory(snapshot)
                try:
                    _, result, _ = flow.call(APP_TOKEN_PATH, {'sourceId':'030011','url':ENTRY,'cellNum':snapshot['phone']})
                except AppLoginError as error:
                    raise MobileRightsError('APP 免登凭证暂未获取成功：'+str(error),'APP_TOKEN_FAILED') from error
                if str(result.get('retCode')) == '000000':
                    body = result.get('rspBody')
                    if isinstance(body,str):
                        try: body=json.loads(body)
                        except ValueError as error: raise MobileRightsError('APP 免登返回格式异常。','APP_TOKEN_FORMAT') from error
                    if not isinstance(body,dict) or not isinstance(body.get('token'),str) or not body['token']:
                        raise MobileRightsError('APP 未返回移动权益免登凭证。','APP_TOKEN_FORMAT')
                    if accept_credentials and flow.cookie != snapshot['cookie']:
                        accept_credentials(snapshot,flow)
                    break
                failure = str(result.get('retCode','unknown'))
            if attempt or failure not in (None,'400007','400009','410000','420000') or recover is None:
                if failure in (None,'400007','400009','410000','420000'):
                    raise MobileRightsError('APP 未通过权益免登认证，请在账号管理恢复登录。','APP_LOGIN_REQUIRED',401,authentication=True)
                safe=failure if re.fullmatch(r'[A-Za-z0-9_-]{1,32}',failure) else 'unknown'
                raise MobileRightsError('APP 未允许权益免登（'+safe+'），请稍后重试。','APP_TOKEN_REJECTED')
            try:snapshot = recover(snapshot)
            except AppLoginError as error:
                required=bool(getattr(error,'login_required',False))
                raise MobileRightsError('共享 APP 身份暂未恢复，请完成当前验证或重新登录后重试。',
                    'APP_LOGIN_REQUIRED' if required else 'APP_IDENTITY_BUSY',401 if required else 409,authentication=required) from error
        try:
            value = response_json(client.post(SCOPE+'/login/leadeonLoginByToken', data={'token':body['token']},
                timeout=(10,25),allow_redirects=False))
        except RequestException as error:
            raise MobileRightsError('连接移动权益免登服务失败，请稍后重试。','WAP_NETWORK') from error
        if str(value.get('X_RESULTCODE')) != '0':
            code=str(value.get('X_RESULTCODE','unknown'))
            safe=code if re.fullmatch(r'[A-Za-z0-9_-]{1,32}',code) else 'unknown'
            raise MobileRightsError('APP 凭证未能转换为移动权益会话（'+safe+'），请稍后重新连接。','WAP_SSO_FAILED',401,authentication=True)
        nonce=f'{random.random():.16f}'
        info = response_json(client.get(SCOPE+'/componant/initQueryLoginInfo',params={
            'pageName':'order_list.html','channel':'E004','ORDER_URL':ENTRY.split('?',1)[0],
            'ORDER_URL_PARAM':quote('type=10',safe=''),'ajax_code':page_signature(ENTRY),
            'ajaxSubmitType':'post','ajax_randomcode':nonce},
            headers={'Cookie':cookie_header(client,nonce,SCOPE+'/componant/initQueryLoginInfo')},timeout=(10,25),allow_redirects=False))
        if isinstance(info.get('data'),dict):info=info['data']
        phone = str(info.get('serialNumber') or '')
        if info.get('isValidate') not in ('true',True) or phone != snapshot['phone']:
            diagnostic=re.sub(r'(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)',r'\1****\2',str(info.get('X_RESULTINFO','')))
            logging.getLogger(__name__).warning('移动权益身份字段未匹配：keys=%s, validated=%r, number_length=%s, code=%s, message=%s',sorted(info),info.get('isValidate'),len(phone),info.get('X_RESULTCODE'),diagnostic[:160])
            raise MobileRightsError('移动权益认证返回的号码不一致或未确认登录，已停止查询。','WAP_IDENTITY_MISMATCH',409)
        return {'serviceNum':phone,'method':'app'}

    try:
        cache.connect(SCOPE,snapshot,client,initialize,expiry_cookies=('WAP_SID','jeesite.session.id'))
        return client
    except Exception:
        client.close()
        raise
