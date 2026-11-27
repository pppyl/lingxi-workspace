"""Sync an existing dialer role or confirmed real call; never place calls."""
from features.checkin.client import CheckinError
from workspace.accounts.login import AppLoginError, AppLoginSession, REFRESH_PATH

LAST_CALL_PATH = '/biz-orange/DN/call/cson/appinfo/lastCall'
CALL_GRANT_PATH = '/biz-orange/DN/call/cson/appinfo/isCallGrant'


class CallSession(AppLoginSession):
    def envelope(self, body, *, login_component=False):
        value = super().envelope(body, login_component=login_component)
        value.update(tel=self.phone, city=str(self.user_info.get('cityCode') or ''),
                     prov=str(self.user_info.get('provinceCode') or ''))
        return value


def sync_existing_authorization(snapshot, device_context, *, before_submit):
    """Sync a confirmed existing dialer role using this account's own identity."""
    if snapshot.get('primary_status') == 'login_required':
        raise CheckinError('同步通话授权需要有效 APP 登录，请先在账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
    if (not device_context or device_context.get('role_holder') != 'com.greenpoint.android.mc10086.activity'
            or not device_context.get('device_id') or not device_context.get('xk')):
        raise CheckinError('尚未配置已验证的默认电话设备，通话授权同步未接通。', 'CALL_DEVICE_REQUIRED', 409)
    context = {**snapshot.get('app_context', {}),
               **{key: device_context[key] for key in ('device_id', 'xk')}}
    session = CallSession.from_credentials({**snapshot, 'app_context': context})
    try:
        _, result, _ = session.call(REFRESH_PATH, {'cellNum': snapshot['phone']})
    except AppLoginError as error:
        raise CheckinError('授权设备会话核验失败，请稍后主动重试。', 'CALL_DEVICE_SESSION_FAILED', 409) from error
    if str(result.get('retCode')) != '000000' or session.cookie != snapshot['cookie']:
        raise CheckinError('当前账号未通过授权设备会话核验，请恢复登录后重试。', 'CALL_DEVICE_SESSION_FAILED', 409)
    before_submit()
    try:
        _, result, _ = session.call(CALL_GRANT_PATH, {'cellNum': snapshot['phone'], 'isCallGrant': True})
    except AppLoginError as error:
        raise CheckinError('通话授权同步结果待核实，请核对官方进度，不要重复同步。',
                           'RESULT_UNCERTAIN', 409, uncertain=True) from error
    if str(result.get('retCode')) != '000000':
        error = CheckinError(str(result.get('retDesc') or '官方未接受既有通话授权同步。')[:300],
                             str(result.get('retCode') or 'CALL_AUTH_REJECTED'), 409)
        error.definitive_rejection = True
        raise error


def sync_confirmed_call(snapshot, *, before_submit):
    if snapshot.get('primary_status') == 'login_required':
        raise CheckinError('同步通话需要有效 APP 登录，请先在账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
    session = CallSession.from_credentials(snapshot)
    before_submit()
    try:
        _, result, _ = session.call(LAST_CALL_PATH, {'cellNum': snapshot['phone']})
    except AppLoginError as error:
        raise CheckinError('通话同步结果待核实，请刷新官方进度，不要重复同步。',
                           'RESULT_UNCERTAIN', 409, uncertain=True) from error
    if str(result.get('retCode')) != '000000':
        error = CheckinError(str(result.get('retDesc') or '官方未接受通话同步，请先核对登录和通话条件。')[:300],
                             str(result.get('retCode') or 'CALL_SYNC_REJECTED'), 409)
        error.definitive_rejection = True
        raise error
