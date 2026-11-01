"""Isolated WeChat channel; all activity requests run in Python."""
from threading import RLock

from flask import Blueprint, jsonify, render_template, request
from credential_cache import CredentialCache
from activities.checkin_common.client import WebCheckinError
from activities.checkin_common.scheduler import WebCheckinScheduler
from .auth import SCOPE, WechatLoginManager, client_for


class Controller:
    def __init__(self, credentials, accounts, database):
        self.deleted = False
        self.cache = CredentialCache(database)
        self.login = WechatLoginManager(credentials,self.cache)
        self.lock, self.auth_busy = RLock(), set()
        self.scheduler = WebCheckinScheduler(credentials,accounts,storage=database,platform_id='wechat-checkin',
            client_factory=lambda provider:client_for(provider,self.cache))

    def metadata(self):
        return {'active':not self.deleted,'ended':False,'server_time':self.scheduler.now().isoformat()}

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.auth_busy or self.scheduler.busy_login(login_id)

    def forget(self, login_id):
        if self.busy_login(login_id):
            raise WebCheckinError('微信渠道正在处理此账号，请稍候再退出。','ACCOUNT_BUSY',409)
        self.scheduler.forget(login_id)
        self.login.forget(login_id)

    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)
        self.login.close()
        if self.deleted:
            self.cache.forget(scope=SCOPE)


def register(app, credentials, accounts, storage):
    controller = Controller(credentials,accounts,storage)
    blueprint = Blueprint('wechat_checkin',__name__,template_folder='../checkin_common/templates',
        static_folder='../checkin_common/static',static_url_path='/activities/wechat-checkin/static')

    @blueprint.before_request
    def available():
        if controller.deleted:
            raise WebCheckinError('微信签到渠道已永久移除。','PLATFORM_DELETED',410)

    @app.errorhandler(WebCheckinError)
    def error_handler(error):
        value = {'error':str(error),'code':error.code}
        if hasattr(error,'retry_after'):
            value['retry_after'] = error.retry_after
        return jsonify(value),error.status

    @app.context_processor
    def template_flags():
        return {'checkin_wechat_available':not controller.deleted}

    @blueprint.get('/checkin/wechat')
    def page():
        return render_template('web_checkin.html',wechat=True)

    @blueprint.get('/api/checkin-wechat/status')
    def status():
        return jsonify(controller.scheduler.status())

    @blueprint.post('/api/checkin-wechat/settings')
    def settings():
        return jsonify(controller.scheduler.update_settings(request.get_json()))

    @blueprint.post('/api/checkin-wechat/run')
    def run():
        payload = request.get_json()
        if not isinstance(payload,dict):
            raise WebCheckinError('请选择需要处理的账号。','INVALID_OPERATION',400)
        return jsonify(controller.scheduler.run(payload.get('id'),payload.get('kind','refresh')))

    @blueprint.post('/api/checkin-wechat/sms')
    @blueprint.post('/api/checkin-wechat/login')
    def authenticate():
        payload = request.get_json()
        if not isinstance(payload,dict) or not isinstance(payload.get('id'),str):
            raise WebCheckinError('请选择需要授权的共享账号。','INVALID_OPERATION',400)
        login_id = payload['id']
        controller.scheduler._account(login_id)
        with controller.lock:
            if controller.busy_login(login_id):
                raise WebCheckinError('该账号正在处理登录，请稍候。','ACCOUNT_BUSY',409)
            controller.auth_busy.add(login_id)
        try:
            if request.path.endswith('/sms'):
                return jsonify(controller.login.sms(login_id))
            result = controller.login.login(login_id,payload.get('code'))
            controller.scheduler.logged_in(login_id)
            return jsonify(result)
        finally:
            with controller.lock:
                controller.auth_busy.discard(login_id)

    app.register_blueprint(blueprint)
    return controller
