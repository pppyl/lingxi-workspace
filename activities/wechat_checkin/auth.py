"""Official SMS authentication retaining the WeChat activity SSO callback."""
from activities.checkin_common.client import WebCheckinClient, WebLoginManager

SCOPE = 'wechat-checkin:1021122301'
WECHAT_UA = ('Mozilla/5.0 (Linux; Android 16; 2210132C; wv) AppleWebKit/537.36 '
             '(KHTML, like Gecko) Version/4.0 Chrome/133.0.6943.143 Mobile Safari/537.36 '
             'MicroMessenger/8.0.63 WeChat/arm64 Weixin NetType/WIFI Language/zh_CN')


def client_for(provider, cache):
    return WebCheckinClient(provider, credential_cache=cache, scope=SCOPE,
                           channel='wechat', user_agent=WECHAT_UA)


class WechatLoginManager(WebLoginManager):
    def __init__(self, credentials, cache, **kwargs):
        super().__init__(credentials,cache,scope=SCOPE,channel='wechat',user_agent=WECHAT_UA,**kwargs)
