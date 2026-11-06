"""Discoverable, removable daily-gold activity routes."""
from flask import Blueprint, jsonify, render_template, request

from .client import DailyGoldClient, DailyGoldError
from .scheduler import DailyGoldScheduler, SCOPE


class Controller:
    def __init__(self, credentials, accounts, storage, cache):
        self.deleted = False
        self.cache = cache
        self.scheduler = DailyGoldScheduler(credentials, accounts, storage=storage,
            client_factory=lambda provider: DailyGoldClient(provider, credential_cache=self.cache,
                protocol=self.scheduler.data['campaign'].get('protocol')))

    def metadata(self):
        return self.scheduler.metadata()

    def busy_login(self, login_id):
        return self.scheduler.busy_login(login_id)

    def forget(self, login_id):
        self.scheduler.forget(login_id)
        self.cache.forget(login_id, scope=SCOPE)

    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)


def register(app, credentials, accounts, storage):
    controller = Controller(credentials, accounts, storage, app.extensions['credential_cache'])
    app.extensions['daily_gold_login_restored'] = controller.scheduler.login_restored
    blueprint = Blueprint('daily_gold', __name__, template_folder='templates', static_folder='static',
                          static_url_path='/activities/daily-gold/static')

    @blueprint.before_request
    def available():
        if controller.deleted:
            raise DailyGoldError('该活动已永久移除。', 'PLATFORM_DELETED', 410)

    @app.errorhandler(DailyGoldError)
    def error_handler(error):
        return jsonify(error=str(error), code=error.code), error.status

    @blueprint.get('/daily-gold')
    def page():
        return render_template('daily_gold.html')

    @blueprint.get('/api/daily-gold/status')
    def status():
        return jsonify(controller.scheduler.status())

    @blueprint.get('/api/daily-gold/meta')
    def metadata():
        return jsonify(controller.metadata())

    @blueprint.post('/api/daily-gold/settings')
    def settings():
        return jsonify(controller.scheduler.update_settings(request.get_json(silent=True)))

    @blueprint.post('/api/daily-gold/refresh')
    def refresh():
        return jsonify(controller.scheduler.refresh())

    @blueprint.post('/api/daily-gold/run')
    def run():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise DailyGoldError('请提交账号及操作。', 'INVALID_OPERATION', 400)
        return jsonify(controller.scheduler.run(payload.get('id'), payload.get('kind', 'refresh')))

    app.register_blueprint(blueprint)
    return controller
