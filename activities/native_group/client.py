"""Python protocol recovered from act-nativeGroup index.T18cWykC/hooks.BbPJQOeP."""
from contextlib import contextmanager
import html
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from features.checkin.client import CheckinClient, CheckinError, _data
from recharge.cmcc import APP_UA
from workspace.http_transport import requests

ACTIVITY = '1026112045'
ENTRY = 'https://wx.10086.cn/hlwyxhdhub/act-nativeGroup/' + ACTIVITY
BASE = 'https://wx.10086.cn/hlwyxhdhub/api/nativeGroup/'
SCOPE = 'native-group:' + ACTIVITY
RULES = 'https://wx.10086.cn/qwhdhub/activity/prelogin/' + ACTIVITY + '?A_C_CODE=SqzHPGUeub'
READS = {'userInfo', 'activityInfo', 'myPrizes', 'drawTasks', 'shareTextInfo', 'generateInviteToken'}
WRITES = {'createGroup', 'joinGroup', 'quitGroup', 'redeemGroupAward', 'doDraw'}
MESSAGES = {'10002': '官方确认该号码不在本次目标范围内。', '10005': '邀请的队伍不存在。',
            '10006': '该队伍已经满员。', '10007': '该档位奖品库存不足。',
            '10008': '该号码已经在队伍中。', '10019': '该号码已经参加过组队。'}


def award_ready(state):
    campaign = state.get('campaign') or {}
    group = campaign.get('userGroupInfo') or {}
    members = group.get('groupMembers', [])
    task_status = str(group.get('groupTaskStatus'))
    return (bool(campaign.get('showGroup')) and str(campaign.get('groupPrizeBtnStatus')) == '1'
            and group.get('groupSize') in {3, 5, 10} and len(members) == group['groupSize']
            and task_status.isdigit() and int(task_status) > 0
            and (state.get('identity') or {}).get('phone') in {row.get('phone') for row in members}
            and all(str(row.get('authTaskStatus')) == '1' and str(row.get('callTaskStatus')) == '1'
                    for row in members))


def invite_token(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 3000:
        raise CheckinError('请粘贴完整的官方邀请链接或邀请码。', 'INVALID_INVITE', 400)
    value = value.strip()
    if value.startswith(('https://', 'http://')):
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or parsed.hostname not in {'wx.10086.cn', 'h.app.coc.10086.cn'}:
            raise CheckinError('请使用官方活动邀请链接。', 'INVALID_INVITE', 400)
        query = parse_qs(parsed.query)
        if parsed.hostname == 'h.app.coc.10086.cn':
            value = query.get('targetUrl', [''])[0]
            parsed = urlsplit(value)
            query = parse_qs(parsed.query)
        if parsed.scheme != 'https' or parsed.netloc != 'wx.10086.cn' or parsed.path != urlsplit(ENTRY).path:
            raise CheckinError('邀请链接不属于本次 APP 通话组队活动。', 'INVALID_INVITE', 400)
        value = query.get('inviteToken', [''])[0]
    if not value or len(value) > 600 or any(char.isspace() or ord(char) < 32 for char in value):
        raise CheckinError('邀请信息不完整。', 'INVALID_INVITE', 400)
    return value


class NativeGroupClient(CheckinClient):
    def __init__(self, provider, *, device_provider=None, **options):
        super().__init__(provider, **options)
        self.device_provider = device_provider

    def _call(self, client, path, body=None, *, snapshot=None, before_submit=None, recover=True):
        if path not in READS | WRITES:
            raise ValueError('Unsupported native-group endpoint')
        writing = path in WRITES
        if writing:
            self._current(snapshot)
            before_submit()
        try:
            response = client.post(BASE + path, json=body or {}, headers={
                'Content-Type': 'application/json;charset=UTF-8', 'login-check': '1'},
                timeout=(10, 25), allow_redirects=False)
            value = {'code': 'GOTO_LOGIN'} if response.status_code == 401 else response.json()
        except (requests.RequestsError, ValueError) as error:
            raise CheckinError('提交结果待核实，请刷新进度。' if writing else '连接组队活动失败，请稍后刷新。',
                               'RESULT_UNCERTAIN' if writing else 'UPSTREAM_NETWORK', uncertain=writing) from error
        if not isinstance(value, dict) or response.status_code not in {200, 401}:
            raise CheckinError('官方响应格式异常。', 'UPSTREAM_FORMAT', uncertain=writing)
        if value.get('code') == 'GOTO_LOGIN':
            if not writing and recover and self.credential_cache and self.credential_cache.recover(client):
                return self._call(client, path, body, recover=False)
            error = CheckinError('活动登录已失效，请刷新恢复或在账号管理恢复 APP 登录。', 'ACTIVITY_LOGIN_REQUIRED', 401)
            error.definitive_rejection = writing
            raise error
        if str(value.get('code')) != '0':
            code = str(value.get('code') or 'UPSTREAM_REJECTED')
            error = CheckinError(MESSAGES.get(code, str(value.get('msg') or '官方未允许本次操作')[:300]),
                                code, 409, uncertain=path == 'doDraw')
            error.definitive_rejection = writing and path != 'doDraw'
            raise error
        return value.get('data')

    def _authenticate(self, client, snapshot):
        if snapshot.get('primary_status') == 'login_required':
            raise CheckinError('活动会话需要恢复，请先在账号管理恢复 APP 登录。', 'APP_LOGIN_REQUIRED', 401)
        page = self._follow(client, ENTRY, source='APP 通话组队入口')
        match = re.search(r"var\s+loginPath\s*=\s*['\"]([^'\"]+)", page.text)
        if match:
            if not match[1].startswith('/appTokenLogin?sid='):
                raise CheckinError('官方登录入口已变化。', 'APP_SSO_ENTRY', 409)
            self._current(snapshot)
            profile = snapshot['profile']
            response = client.post('https://wx.10086.cn/qwhdsso' + match[1], json={
                'jwtToken': None, 'token': snapshot['cookie'],
                'provinceCode': str(profile.get('provinceCode', '')),
                'cityCode': str(profile.get('cityCode', '')),
                'userCheckId': format(int(snapshot['phone']), 'x'),
                'carrierOperator': str(profile.get('carrierOperator') or '002'),
                'appVersionCode': '12.5.6', 'took': 0}, timeout=(10, 25), allow_redirects=False)
            if response.status_code != 200:
                raise CheckinError('组队活动 APP 认证失败。', 'APP_SSO_HTTP')
            payload = _data(response.json(), stage='组队活动 APP 认证')
            self._follow(client, payload['url'], source='组队活动回调')
        identity = self._call(client, 'userInfo', recover=False)
        masked = snapshot['phone'][:3] + '****' + snapshot['phone'][-4:]
        if (not isinstance(identity, dict) or identity.get('phone') not in {snapshot['phone'], masked}
                or identity.get('channel') != 'app'):
            raise CheckinError('活动返回的账号或登录渠道不一致。', 'ACCOUNT_MISMATCH', 409)
        return identity

    @contextmanager
    def connect(self):
        snapshot = self._snapshot()
        with self.session_factory() as client:
            client.headers.update({'User-Agent': APP_UA, 'Referer': ENTRY, 'Origin': 'https://wx.10086.cn'})
            self.credential_cache.connect(SCOPE, snapshot, client, lambda: self._authenticate(client, snapshot),
                                          expiry_cookies=('HLWHD_SESSION_TOKEN',))
            yield client, snapshot

    def inspect(self, client):
        identity = self._call(client, 'userInfo')
        campaign = self._call(client, 'activityInfo', {'osType': 'android'})
        prizes = self._call(client, 'myPrizes')
        tasks = self._call(client, 'drawTasks', {'osType': 'android'})
        if (not isinstance(identity, dict) or identity.get('phone') != client._credential_data.get('phone')
                or identity.get('channel') != 'app'):
            raise CheckinError('活动身份与当前保存的账号不一致。', 'ACCOUNT_MISMATCH', 409)
        if (not isinstance(campaign, dict) or not campaign.get('startTime') or not campaign.get('endTime')
                or not isinstance(prizes, list) or not isinstance(tasks, list)):
            raise CheckinError('官方活动进度不完整，本次未提交。', 'UPSTREAM_FORMAT')
        return {'identity': identity, 'campaign': campaign, 'prizes': prizes, 'tasks': tasks}

    def rules(self):
        response = requests.get(RULES, headers={'User-Agent': APP_UA}, timeout=(10, 25))
        if response.status_code != 200:
            raise CheckinError('读取官方活动规则失败。', 'UPSTREAM_HTTP')
        value = response.json()
        raw = _data(value)['activityInfo']['rules']
        return re.sub(r'\n\s*\n+', '\n', html.unescape(re.sub(r'<[^>]+>', '\n', raw))).strip()

    def operate(self, client, snapshot, kind, state, *, size=None, token=None, before_submit):
        campaign = state['campaign']
        group = campaign.get('userGroupInfo')
        if kind == 'draw':
            if (not isinstance(campaign.get('drawTimes'), int) or campaign['drawTimes'] <= 0
                    or int(campaign.get('todayAlreadyDrawTimes') or 0) >= 2):
                raise CheckinError('当前没有抽奖机会，或今日已达到 2 次上限。', 'DRAW_UNAVAILABLE', 409)
            risk_token = self.prepare_device(snapshot)
            return self._call(client, 'doDraw', {'riskConstId': risk_token}, snapshot=snapshot,
                              before_submit=before_submit)
        if not campaign.get('showGroup'):
            raise CheckinError('官方当前未开放该账号的通话组队。', 'ACTIVITY_UNAVAILABLE', 409)
        if kind in {'create', 'join'} and group:
            raise CheckinError('该账号已在队伍中，请先查看当前队伍。', 'ALREADY_IN_GROUP', 409)
        if kind not in {'create', 'join'} and not group:
            raise CheckinError('请先创建或加入一个队伍。', 'GROUP_REQUIRED', 409)
        if kind == 'create':
            if type(size) is not int or size not in {3, 5, 10}:
                raise CheckinError('请选择 3、5 或 10 人队。', 'INVALID_SIZE', 400)
            path, body = 'createGroup', {'groupSize': size, 'osType': 'android'}
        elif kind == 'join':
            path, body = 'joinGroup', {'inviteToken': invite_token(token), 'osType': 'android'}
        elif kind == 'quit':
            members = group.get('groupMembers', [])
            if len(members) != 1 or members[0].get('phone') != state['identity']['phone']:
                raise CheckinError('官方仅允许尚未邀请队员的创建人退队。', 'QUIT_UNAVAILABLE', 409)
            path, body = 'quitGroup', {}
        elif kind == 'claim':
            if not award_ready(state):
                raise CheckinError('官方尚未确认全队满员并完成任务。', 'AWARD_NOT_READY', 409)
            path, body = 'redeemGroupAward', {}
        elif kind == 'invite':
            self._current(snapshot)
            config = self._call(client, 'shareTextInfo')
            path, body = 'generateInviteToken', {'riskConstId': self.prepare_device(snapshot)}
        else:
            raise CheckinError('该操作尚未接通。', 'UNSUPPORTED_OPERATION', 400)
        result = self._call(client, path, body, snapshot=snapshot, before_submit=before_submit)
        if kind == 'invite':
            if not isinstance(result, dict) or not result.get('inviteToken'):
                raise CheckinError('官方邀请数据不完整，请刷新后重试。', 'INVITE_RESPONSE_INVALID', 409)
            return {'url': ENTRY + '?' + urlencode({'inviteToken': result['inviteToken']}),
                    'title': config.get('title', ''), 'image': config.get('img', '')}
        return result

    def prepare_device(self, snapshot):
        if self.device_provider is None:
            raise CheckinError('当前客户端尚未接入抽奖认证。', 'DEVICE_UNAVAILABLE', 409)
        return self.device_provider(snapshot)
