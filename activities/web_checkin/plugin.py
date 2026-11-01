"""Register the independent web-channel page and Python activity operations."""
from threading import RLock

from flask import Blueprint, jsonify, render_template, request
from credential_cache import CredentialCache

from .client import WebCheckinClient, WebCheckinError, WebLoginManager
from .scheduler import PLATFORM_ID, WebCheckinScheduler

SCOPE = 'web-checkin:1021122301'


class Controller:
    def __init__(self, credentials, accounts, database):
        self.deleted = False
        self.cache = CredentialCache(database)
        self.login = WebLoginManager(credentials, self.cache)
        self.lock, self.auth_busy = RLock(), set()
        self.scheduler = WebCheckinScheduler(credentials, accounts, storage=database,
            client_factory=lambda provider: WebCheckinClient(provider, credential_cache=self.cache))

    def metadata(self):
        return {'active': not self.deleted, 'ended': False, 'server_time': self.scheduler.now().isoformat()}

    def busy_login(self, login_id):
        with self.lock:
            return login_id in self.auth_busy or self.scheduler.busy_login(login_id)

    def forget(self, login_id):
        if self.busy_login(login_id):
            raise WebCheckinError('网页渠道正在处理此账号，请稍候再退出。', 'ACCOUNT_BUSY', 409)
        self.scheduler.forget(login_id)
        self.login.forget(login_id)

    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)
        self.login.close()
        if self.deleted:
            self.cache.forget(scope=SCOPE)


def register(app, credentials, accounts, storage):
    controller = Controller(credentials, accounts, storage)
    blueprint = Blueprint('web_checkin', __name__, template_folder='../checkin_common/templates', static_folder='../checkin_common/static',
                          static_url_path='/activities/web-checkin/static')

    @blueprint.before_request
    def available():
        if controller.deleted:
            raise WebCheckinError('该渠道已永久移除。', 'PLATFORM_DELETED', 410)

    @app.errorhandler(WebCheckinError)
    def error_handler(error):
        value = {'error': str(error), 'code': error.code}
        if hasattr(error, 'retry_after'):
            value['retry_after'] = error.retry_after
        return jsonify(value), error.status

    @app.context_processor
    def template_flags():
        return {'checkin_web_available': not controller.deleted}

    @blueprint.get('/checkin/web')
    def page():
        return render_template('web_checkin.html')

    @blueprint.get('/api/checkin-web/status')
    def status():
        return jsonify(controller.scheduler.status())

    @blueprint.post('/api/checkin-web/settings')
    def settings():
        return jsonify(controller.scheduler.update_settings(request.get_json()))

    @blueprint.post('/api/checkin-web/run')
    def run():
        payload = request.get_json()
        if not isinstance(payload, dict):
            raise WebCheckinError('请选择需要处理的账号。', 'INVALID_OPERATION', 400)
        return jsonify(controller.scheduler.run(payload.get('id'), payload.get('kind', 'refresh')))

    @blueprint.post('/api/checkin-web/sms')
    @blueprint.post('/api/checkin-web/login')
    def authenticate():
        payload = request.get_json()
        if not isinstance(payload, dict) or not isinstance(payload.get('id'), str):
            raise WebCheckinError('请选择需要登录的共享账号。', 'INVALID_OPERATION', 400)
        login_id = payload['id']
        controller.scheduler._account(login_id)
        with controller.lock:
            if login_id in controller.auth_busy:
                raise WebCheckinError('该账号正在处理登录，请稍候。', 'ACCOUNT_BUSY', 409)
            controller.auth_busy.add(login_id)
        try:
            if request.path.endswith('/sms'):
                return jsonify(controller.login.sms(login_id))
            result = controller.login.login(login_id, payload.get('code'))
            controller.scheduler.logged_in(login_id)
            return jsonify(result)
        finally:
            with controller.lock:
                controller.auth_busy.discard(login_id)

    app.register_blueprint(blueprint)
    return controller
