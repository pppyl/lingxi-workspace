"""Removable APP calling-team activity with local confirmation and readback."""
from flask import Blueprint, jsonify, render_template, request

from features.checkin.client import CheckinError
from workspace.accounts.credentials import CredentialCache
from .client import NativeGroupClient, SCOPE as AUTH_SCOPE
from .scheduler import GroupScheduler, clean
from .device import GroupDevices


class Controller:
    def __init__(self, credentials, accounts, storage, shared_storage=None):
        self.deleted = False
        self.cache = CredentialCache(storage)
        self.devices = GroupDevices(self.cache, shared_storage)
        def snapshot(key):
            return credentials(key, allow_cached=True)
        def activity_accounts():
            rows = accounts()
            for row in rows:
                value = snapshot(row['id'])
                row['risk_ready'] = bool(value and self.devices.ready(value))
                if row['status'] == 'login_required':
                    if value and self.cache.load(AUTH_SCOPE, value):
                        row.update(status='active', app_login_required=True, activity_session_valid=True)
            return rows
        self.scheduler = GroupScheduler(activity_accounts,
            lambda key: NativeGroupClient(lambda: snapshot(key), credential_cache=self.cache,
                                           device_provider=self.devices.ensure), storage)

    def metadata(self):
        return self.scheduler.metadata()

    def busy_login(self, key):
        return self.scheduler.busy_login(key)

    def forget(self, key):
        self.scheduler.forget(key)
        self.cache.forget(key, scope=AUTH_SCOPE)
        self.devices.forget(key)

    def stop(self, wait=True):
        self.scheduler.shutdown(wait=wait)
        self.devices.close()


def register(app, credentials, accounts, storage):
    host_cache = app.extensions.get('credential_cache')
    controller = Controller(credentials, accounts, storage, host_cache.storage if host_cache else None)
    blueprint = Blueprint('native_group', __name__, template_folder='templates', static_folder='static',
                          static_url_path='/activities/native-group/static')

    @blueprint.before_request
    def available():
        if controller.deleted:
            raise CheckinError('活动已移除。', 'PLATFORM_DELETED', 410)

    @blueprint.errorhandler(CheckinError)
    def error(error):
        return jsonify(error=clean(error), code=error.code, uncertain=error.uncertain,
                       upstream_status=getattr(error, 'upstream_status', None)), error.status

    @blueprint.get('/native-group')
    def page():
        return render_template('native_group.html')

    @blueprint.get('/api/native-group/status')
    def status():
        key = request.args.get('login_id')
        return jsonify(controller.scheduler.run(key) if key else controller.scheduler.status())

    @blueprint.post('/api/native-group/run')
    def run():
        value = request.get_json(silent=True)
        if (not isinstance(value, dict) or not isinstance(value.get('login_id'), str)
                or value.get('kind') not in {'create', 'join', 'quit', 'claim', 'invite', 'draw', 'prepare',
                                              'call_prepare', 'call_confirm', 'call_authorize'}
                or value.get('confirmed') is not True):
            raise CheckinError('请确认当前账号和操作。', 'CONFIRM_REQUIRED', 400)
        return jsonify(controller.scheduler.run(value['login_id'], value['kind'],
            size=value.get('size'), token=value.get('invitation'), destination=value.get('destination'),
            call_id=value.get('call_id'), connected=value.get('connected'),
            existing_role_confirmed=value.get('existing_role_confirmed')))

    @blueprint.post('/api/native-group/plan')
    def plan():
        return jsonify(controller.scheduler.plan())

    @blueprint.post('/api/native-group/assemble')
    def assemble():
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or value.get('confirmed') is not True:
            raise CheckinError('请先确认完整组队方案。', 'CONFIRM_REQUIRED', 400)
        return jsonify(controller.scheduler.assemble(value.get('teams')))

    app.register_blueprint(blueprint)
    return controller
