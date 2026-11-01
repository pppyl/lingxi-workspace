"""Android foreground-service entry; all requests and task state stay on this phone."""
import os
import json
import secrets
import threading
import hmac

_server = None
_app = None


def start(files_dir):
    global _server, _app
    os.environ['LINGXI_ANDROID'] = '1'
    os.environ['LINGXI_DATA_DIR'] = files_dir + '/workspace'
    from app import app
    from flask import request, jsonify
    from waitress import create_server
    _app = app
    token = secrets.token_urlsafe(32)
    # Guard both the root app and the mounted recharge app on the phone's loopback.
    def require_native_session():
        if not hmac.compare_digest(request.cookies.get('lingxi_mobile', ''), token):
            return jsonify(error='请从本地工具台应用打开页面。'), 403
    app.before_request(require_native_session)
    app.extensions['recharge_app'].before_request(require_native_session)
    _server = create_server(app, host='127.0.0.1', port=0, threads=32, channel_timeout=240)
    threading.Thread(target=_server.run, daemon=True, name='android-http').start()
    for key in ('session_maintenance', 'lingxi_scheduler', 'checkin_scheduler', 'monthly_coupon_scheduler', 'star_day_scheduler', 'platform_manager', 'update_checker'):
        app.extensions[key].start()
    app.extensions['recharge_app'].extensions['scheduler'].start()
    return json.dumps({'url': f'http://127.0.0.1:{_server.effective_port}/', 'token': token})


def stop():
    if _app is not None:
        _app.extensions['session_maintenance'].stop()
        for key in ('lingxi_scheduler', 'checkin_scheduler', 'monthly_coupon_scheduler', 'star_day_scheduler', 'platform_manager', 'update_checker'):
            _app.extensions[key].shutdown()
        _app.extensions['recharge_app'].extensions['scheduler'].stop()
    if _server is not None:
        _server.close()
