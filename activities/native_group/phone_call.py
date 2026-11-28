"""Sync an existing dialer role or finish the official call task by protocol."""
from features.checkin.client import CheckinError
from workspace.accounts.login import AppLoginError, AppLoginSession, REFRESH_PATH

LAST_CALL_PATH = '/biz-orange/DN/call/cson/appinfo/lastCall'
CALL_GRANT_PATH = '/biz-orange/DN/call/cson/appinfo/isCallGrant'


def automatic_call_context(snapshot):
    """Prepare this account's protocol parameters for unattended task execution."""
    if snapshot.get('primary_status') == 'login_required':
        raise CheckinError('自动配置需要有效 APP 登录，请先在账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
    flow = AppLoginSession.from_credentials(snapshot)
    if not (snapshot.get('app_context') or {}).get('device_id') or not flow.xk or flow.xk == 'null':
        try:
            flow.initialize()
        except AppLoginError as error:
            raise CheckinError('官方设备参数初始化未完成，请稍后主动重试。', 'CALL_DEVICE_INIT_FAILED', 409) from error
    return {'role_holder': 'com.greenpoint.android.mc10086.activity',
            'device_id': flow.device_id, 'xk': flow.xk,
            'source': 'unattended-protocol/automatic-app-device-parameters'}


class CallSession(AppLoginSession):
    def envelope(self, body, *, login_component=False):
        value = super().envelope(body, login_component=login_component)
        value.update(tel=self.phone, city=str(self.user_info.get('cityCode') or ''),
                     prov=str(self.user_info.get('provinceCode') or ''))
        return value


def verified_call_session(snapshot, device_context):
    """Verify this account with the saved activity device parameters."""
    if snapshot.get('primary_status') == 'login_required':
        raise CheckinError('同步通话授权需要有效 APP 登录，请先在账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
    if (not device_context or device_context.get('role_holder') != 'com.greenpoint.android.mc10086.activity'
            or not device_context.get('device_id') or not device_context.get('xk')):
        raise CheckinError('尚未配置通话组队的活动设备参数。', 'CALL_DEVICE_REQUIRED', 409)
    context = {**snapshot.get('app_context', {}),
               **{key: device_context[key] for key in ('device_id', 'xk')}}
    session = CallSession.from_credentials({**snapshot, 'app_context': context})
    try:
        _, result, _ = session.call(REFRESH_PATH, {'cellNum': snapshot['phone']})
    except AppLoginError as error:
        raise CheckinError('授权设备会话核验失败，请稍后主动重试。', 'CALL_DEVICE_SESSION_FAILED', 409) from error
    if str(result.get('retCode')) != '000000' or session.cookie != snapshot['cookie']:
        raise CheckinError('当前账号未通过授权设备会话核验，请恢复登录后重试。', 'CALL_DEVICE_SESSION_FAILED', 409)
    return session


def sync_existing_authorization(snapshot, device_context, *, before_submit):
    """Sync the official call-authorization step with this account's identity."""
    session = verified_call_session(snapshot, device_context)
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


def complete_call_task(snapshot, *, before_submit):
    """Submit the official task event without opening a dialer or placing a call."""
    if snapshot.get('primary_status') == 'login_required':
        raise CheckinError('完成通话任务需要有效 APP 登录，请先在账号管理恢复登录。', 'APP_LOGIN_REQUIRED', 401)
    session = CallSession.from_credentials(snapshot)
    before_submit()
    try:
        _, result, _ = session.call(LAST_CALL_PATH, {'cellNum': snapshot['phone']})
    except AppLoginError as error:
        raise CheckinError('通话任务结果待核实，请刷新官方进度，不要重复提交。',
                           'RESULT_UNCERTAIN', 409, uncertain=True) from error
    if str(result.get('retCode')) != '000000':
        error = CheckinError(str(result.get('retDesc') or '官方未接受通话任务提交，请先核对登录和活动条件。')[:300],
                             str(result.get('retCode') or 'CALL_SYNC_REJECTED'), 409)
        error.definitive_rejection = True
        raise error


# Compatibility for older local callers. New production code uses the name
# that describes the actual protocol behavior and does not require a real call.
sync_confirmed_call = complete_call_task
