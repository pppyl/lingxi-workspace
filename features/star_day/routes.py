"""HTTP routes for star day."""
from flask import jsonify, render_template, request

from .auth import StarDayAuthError
from .client import StarDayError


def register_star_day_routes(app, star_scheduler):
    @app.get('/star-day')
    def star_day_page():
        return render_template('star_day.html')

    @app.errorhandler(StarDayAuthError)
    @app.errorhandler(StarDayError)
    def star_day_error(error):
        return jsonify(error=str(error), code=error.code), error.status

    @app.get('/api/star-day/status')
    def star_day_status():
        return jsonify(star_scheduler.status())

    @app.post('/api/star-day/open')
    def star_day_open():
        star_scheduler.open_page()
        return jsonify(message='活动凭据检查已安排')

    @app.post('/api/star-day/login/sms')
    def star_day_send_sms():
        return jsonify(star_scheduler.sms.send(star_scheduler.sms_snapshot(request.get_json().get('login_id'))))

    @app.post('/api/star-day/login/submit')
    def star_day_sms_login():
        return jsonify(star_scheduler.sms_login(request.get_json()))

    @app.post('/api/star-day/settings')
    def star_day_settings():
        return jsonify(message=star_scheduler.settings(request.get_json()))

    @app.post('/api/star-day/calibrate')
    def star_day_calibrate():
        return jsonify(star_scheduler.calibrate())

    @app.post('/api/star-day/signin')
    def star_day_signin():
        return jsonify(message=star_scheduler.run(request.get_json().get('login_ids')))

    @app.post('/api/star-day/recheck')
    def star_day_recheck():
        return jsonify(message=star_scheduler.run(request.get_json().get('login_ids'), inspect=True))

    @app.get('/api/star-day/products')
    def star_day_products():
        return jsonify(products=star_scheduler.products(request.args.get('login_id')))

    @app.post('/api/star-day/jobs')
    def star_day_schedule():
        return jsonify(star_scheduler.schedule(request.get_json()))

    @app.post('/api/star-day/claim')
    def star_day_claim():
        return jsonify(star_scheduler.schedule(request.get_json(), immediate=True))

    @app.get('/api/star-day/gift/options')
    def star_day_gift_options():
        return jsonify(star_scheduler.gift_options(request.args.get('login_id')))

    @app.post('/api/star-day/gift')
    def star_day_gift():
        payload = request.get_json()
        if not isinstance(payload.get('recipient'), dict):
            raise StarDayError('请选择赠送接收人。', 'INVALID_RECIPIENT', 400)
        mode = payload.get('mode', 'now')
        if mode not in ('now','daily'):
            raise StarDayError('请选择立即赠送或每天 10:00 抢购赠送。', 'INVALID_MODE', 400)
        return jsonify(star_scheduler.schedule(payload, immediate=mode == 'now'))

    @app.post('/api/star-day/jobs/stop')
    def star_day_stop():
        star_scheduler.stop(request.get_json().get('id'))
        return jsonify(message='任务已停止并移除。')
