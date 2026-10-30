"""Android equivalent of the desktop browser's official device-identifier step."""
class AndroidActivityBridge:
    def __init__(self, _profile_dir=None):
        self.open = False
        self.user_agent = ''

    def rpc(self, command, **params):
        if command == 'info':
            return {'open': self.open}
        if command == 'app_session':
            self.open = True
            self.user_agent = params['user_agent']
            return {'open': True}
        if command == 'risk':
            from java import jclass
            value = jclass('cn.pppyl.lingxiworkspace.ActivityRiskBridge').getIdentifier(self.user_agent)
            return {'const_id': str(value)}
        if command in ('close', 'logout'):
            self.open = False
            return {'open': False}
        from recharge.cmcc import ActivityError
        raise ActivityError('手机版请使用共享账号的 APP 短信登录。', 'APP_LOGIN_REQUIRED', 409)

    def stop(self):
        self.open = False
