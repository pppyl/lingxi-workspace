"""Optional campaign registration; removing this directory removes the feature."""
from pathlib import Path
import json

from flask import Blueprint, jsonify, render_template, request

from .client import SevenDayError, SevenDayClient
from credential_cache import CredentialCache
from .scheduler import SevenDayScheduler


class Storage:
    def __init__(self, database):
        self.database = database
        # Migrate this activity's initial development table to generic storage.
        with database.lock, database._connect() as connection:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='seven_day_data'").fetchone()
            legacy = connection.execute('SELECT payload FROM seven_day_data WHERE id=1').fetchone() if exists else None
        if legacy and database.load_activity_data('seven-day') is None:
            database.save_activity_data('seven-day', json.loads(database.unprotect(legacy[0]).decode('utf-8')))
        if exists:
            with database.lock, database._connect() as connection, connection:
                connection.execute('DROP TABLE seven_day_data')

    def load_seven_day_data(self):
        return self.database.load_activity_data('seven-day')

    def save_seven_day_data(self, data):
        self.database.save_activity_data('seven-day', data)


class Controller:
    def __init__(self, credentials, accounts, database):
        self.deleted = False
        self.cache = CredentialCache(database)
        self.scheduler = SevenDayScheduler(credentials, accounts, storage=Storage(database) if database else None,
            client_factory=lambda provider:SevenDayClient(provider,credential_cache=self.cache))

    def metadata(self):
        return self.scheduler.metadata()

    def busy_login(self, login_id):
        return self.scheduler.busy_login(login_id)

    def forget(self, login_id):
        self.scheduler.forget(login_id)
        self.cache.forget(login_id,scope='seven-day')

    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)


def register(app, credentials, accounts, storage):
    controller = Controller(credentials, accounts, storage)
    blueprint = Blueprint('seven_day', __name__, template_folder='templates', static_folder='static',
                          static_url_path='/activities/seven-day/static')

    @blueprint.before_request
    def available():
        if controller.deleted:
            raise SevenDayError('该活动已永久移除', 'PLATFORM_DELETED', 410)
        if request.method == 'POST':
            controller.scheduler._available()

    @app.errorhandler(SevenDayError)
    def error_handler(error):
        return jsonify(error=str(error), code=error.code), error.status

    @blueprint.get('/seven-day')
    def page():
        return render_template('seven_day.html')

    @blueprint.get('/api/seven-day/meta')
    def metadata():
        return jsonify(controller.metadata())

    @blueprint.get('/api/seven-day/status')
    def status():
        return jsonify(controller.scheduler.status())

    @blueprint.post('/api/seven-day/settings')
    def settings():
        return jsonify(controller.scheduler.update_settings(request.get_json()))

    @blueprint.post('/api/seven-day/run')
    def run():
        payload = request.get_json()
        return jsonify(controller.scheduler.run(payload.get('id'), payload.get('kind', 'refresh')))

    @blueprint.post('/api/seven-day/sms')
    def sms():
        return jsonify(controller.scheduler.send_sms(request.get_json().get('id')))

    @blueprint.post('/api/seven-day/claim')
    def claim():
        payload = request.get_json()
        return jsonify(controller.scheduler.claim(payload.get('id'), payload.get('code')))

    app.register_blueprint(blueprint)
    return controller
