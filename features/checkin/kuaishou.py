"""Kuaishou's verified DUCT_CALLBACK protocol, with account-bound imported login.

The task query and undertaking are signed locally. Official mobile task progress
and the bean award remain the completion authority; no finishTask is fabricated.
"""
from contextlib import contextmanager
from urllib.parse import urlencode, urlsplit

from workspace.http_transport import requests
from .client import CheckinError
from .kuaishou_signing import SIGNER

TASK_NAME = '去快手极速版领金币'
TASK = ('6', '1', 'kuaishou')
SCOPE = 'checkin:kuaishou'
BASE = 'https://apissl.gifshow.com'
QUERY = '/rest/n/xinhui/exchange/queryObtainTaskType'
UNDERTAKE = '/rest/n/xinhui/exchange/undertake'
LOGIN_FIELDS = ('uid', 'api_service_token', 'client_salt', 'common', 'feature_bits')
COMMON_NAMES = frozenset({'abi', 'androidApiLevel', 'android_os', 'app', 'apptype', 'appver',
    'boardPlatform', 'browseType', 'c', 'cdid_tag', 'cl', 'client_key', 'countryCode',
    'country_code', 'darkMode', 'ddpi', 'deviceBit', 'deviceName', 'device_abi', 'did',
    'did_gt', 'did_tag', 'egid', 'ftt', 'grant_browse_type', 'icaver', 'is_background',
    'isp', 'kcv', 'keyconfig_state', 'kpf', 'kpn', 'language', 'max_memory', 'mod',
    'nbh', 'net', 'newOc', 'oDid', 'oc', 'rdid', 'sbh', 'sh', 'sid', 'slh', 'socName',
    'sw', 'sys', 'thermal', 'totalMemory', 'ud', 'userId', 'userRecoBit', 'ver'})


def normalize_login(value):
    if (not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key]
            for key in ('uid', 'api_service_token', 'client_salt'))
            or not isinstance(value.get('common'), dict)
            or type(value.get('feature_bits')) is not int or not 0 <= value['feature_bits'] <= 0xffffffff):
        raise CheckinError('快手资料需包含 uid、api_service_token、client_salt、common 和 feature_bits。',
                           'INVALID_IMPORT', 400)
    common = {key: item for key, item in value['common'].items() if key in COMMON_NAMES}
    if (any(not isinstance(item, str) or any(char in item for char in '\r\n\0') for item in common.values())
            or common.get('userId') != value['uid'] or common.get('kpn') != 'NEBULA'
            or common.get('client_key') != '2ac2a76d' or not common.get('did')):
        raise CheckinError('快手本人身份或客户端资料不完整，未替换原登录态。', 'ACCOUNT_MISMATCH', 409)
    # These are the public version and algorithm paired with the installed
    # configuration, not a browser-cookie version or an invented token expiry.
    common.update(appver='13.3.10.9760', ver='13.3')
    return {**{key: value[key] for key in LOGIN_FIELDS if key != 'common'}, 'common': common}


class CheckinKuaishou:
    def __init__(self, credentials, cache, *, signer=SIGNER):
        self.credentials, self.cache, self.signer = credentials, cache, signer

    def _request(self, client, path, values, *, writing=False):
        identity = client._credential_data
        common = {**identity['common'], 'kuaishou.api_st': identity['api_service_token']}
        query = self.signer.sign(path, common if writing else {**common, **values},
            client_salt=identity['client_salt'], feature_bits=identity['feature_bits'],
            form=values if writing else None)
        try:
            response = client.post(BASE + path, params=query, data=values, timeout=(10,25), allow_redirects=False) if writing else (
                client.get(BASE + path, params=query, timeout=(10,25), allow_redirects=False))
        except requests.RequestsError as error:
            raise CheckinError('快手连接中断，请先核对官方任务进度。',
                'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        if response.status_code != 200:
            raise CheckinError(f'快手 HTTP {response.status_code}，未确认任务结果。',
                'RESULT_UNCERTAIN' if writing else 'KUAISHOU_QUERY_REJECTED', uncertain=writing)
        try:
            payload = response.json()
        except ValueError as error:
            raise CheckinError('快手响应格式异常，请核对任务结果。',
                'RESULT_UNCERTAIN' if writing else 'KUAISHOU_QUERY_REJECTED', uncertain=writing) from error
        if not isinstance(payload, dict):
            raise CheckinError('快手响应格式异常。', 'RESULT_UNCERTAIN' if writing else 'KUAISHOU_QUERY_REJECTED', uncertain=writing)
        if payload.get('result') != 1:
            # Do not assume an arbitrary server error proves a submitted
            # callback had no effect. The durable marker must be reconciled.
            raise CheckinError(f'快手接口未通过验证（result={payload.get("result")}），请在合作方登录中检查或更新。',
                'KUAISHOU_QUERY_REJECTED', 409, uncertain=writing)
        return payload

    @contextmanager
    def _session(self):
        snapshot = self.credentials()
        with requests.Session(headers={'User-Agent': 'kwai-android'}) as client:
            def missing():
                raise CheckinError('该账号尚无有效的本人快手登录，请在合作方登录中导入或更新。',
                                   'KUAISHOU_LOGIN_REQUIRED', 401)
            self.cache.connect(SCOPE, snapshot, client, missing, expiry_cookies=())
            yield client

    def complete(self, item, info, *, before_submit):
        back = urlsplit(info['backUrl'])
        if ((str(item['taskCategory']), str(item['taskType']), item['jumpUrl']) != TASK
                or back.netloc != 'wx.10086.cn' or back.path != '/qwhdhub/qwhdmark/1021122301'
                or str(info['scanTime']) != '0' or not info.get('cToken')):
            raise CheckinError('快手任务入口或完成条件已变化，未提交。', 'TASK_CHANGED', 409)
        token = info['cToken']
        deep_link = 'ksnebula://featured?' + urlencode({
            'sceneCode': 'JSB_YYHZ_CHINAMOBILE_TASK',
            'openFrom': 'callback_mixed_bm_ba_yyhzhl_nset_jsb_chinamobile_task',
            'userToken': token,
            'growth_channel_id': 'ug_exchange?' + urlencode({
                'g_sceneCode': 'JSB_YYHZ_CHINAMOBILE_TASK', 'g_userToken': token})})
        with self._session() as client:
            reply = self._request(client, QUERY, {'shareText': '', 'deepLink': deep_link})
            task = reply.get('taskTypeInfo')
            if not isinstance(task, dict) or task.get('taskType') != 'DUCT_CALLBACK':
                raise CheckinError('快手本次任务条件与已接通的回调任务不同，未提交完成记录。',
                                   'KUAISHOU_TASK_CHANGED', 409)
            before_submit()
            self._request(client, UNDERTAKE, {'deepLink': deep_link}, writing=True)
            self.cache.sync(client)
            return {'task_type': 'DUCT_CALLBACK', 'undertake_confirmed': True,
                    'awaiting_mobile_confirmation': True}
