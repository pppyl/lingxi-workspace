"""HTTP routes for coupons."""
import re

from flask import jsonify, render_template, request

from .client import CouponError, SORTS


def register_coupons_routes(app, unified, coupon_uses, coupon_notices):
    @app.get('/coupons')
    def coupons_page():
        return render_template('coupons.html')

    @app.errorhandler(CouponError)
    def coupon_error(error):
        result = {'error':str(error), 'code':error.code}
        login_id = request.args.get('login_id') or (request.get_json(silent=True) or {}).get('login_id')
        saved = unified.get_id(login_id)
        if saved is not None:
            if error.code == 'APP_SESSION_EXPIRED':
                unified.mark(saved,'login_required',str(error),expected_revision=error.credential_revision)
            result['account'] = saved.public()
        return jsonify(result), error.status

    @app.get('/api/coupons/accounts')
    def coupon_accounts():
        return jsonify(accounts=unified.public(), reminders=coupon_reminder_summary())

    @app.get('/api/coupons/notices')
    def notices_status():
        return jsonify(coupon_notices.get())

    def coupon_reminder_summary():
        with coupon_uses.lock:
            submissions = dict(coupon_uses.data)
        return coupon_uses.reminders.summary([row['id'] for row in unified.public()], submissions)

    @app.post('/api/coupons/use/prepare')
    def coupon_use_prepare():
        payload = request.get_json()
        saved = unified.get_id(payload.get('login_id', ''))
        if saved is None:
            raise CouponError('请选择账号及卡券。', 'INVALID_SELECTION', 400)
        with unified.phone_lock(saved.phone):
            return jsonify(coupon_uses.prepare(saved.id, payload.get('coupon_id', ''), retry=payload.get('retry', False)))

    @app.post('/api/coupons/use/sms')
    def coupon_use_sms():
        return jsonify(coupon_uses.sms(request.get_json().get('operation_id', '')))

    @app.post('/api/coupons/use/confirm')
    def coupon_use_confirm():
        payload = request.get_json()
        return jsonify(coupon_uses.confirm(payload.get('operation_id', ''),
            sms_code=payload.get('sms_code', ''), charge_amount=payload.get('charge_amount', ''),
            captcha_token=payload.get('captcha_token', ''), risk_token=payload.get('risk_token', ''),
            risk_lid=payload.get('risk_lid', ''), prepare_risk=payload.get('prepare_risk', False),
            browser_user_agent=request.headers.get('User-Agent', '')))

    @app.get('/api/coupons')
    def coupon_list():
        login_id = request.args.get('login_id', '')
        state, sort = request.args.get('state', 'available'), request.args.get('sort', 'default')
        page = request.args.get('page', '1')
        if state not in ('available', 'history') or sort not in SORTS or not re.fullmatch(r'[1-9]\d{0,3}', page):
            raise CouponError('卡券筛选或页码无效，请刷新后重试。', 'INVALID_QUERY', 400)
        saved = unified.get_id(login_id)
        if saved is None:
            raise CouponError('请选择共享账号池中的账号。', 'ACCOUNT_NOT_FOUND', 404)
        with unified.phone_lock(saved.phone):
            result = coupon_uses.query(login_id, state=state, page=int(page), sort=sort)
        coupon_uses.bind_pending(login_id, result['items'])
        result['pending_orders'] = coupon_uses.pending(login_id)
        result['reminder'] = coupon_reminder_summary()['accounts'].get(login_id)
        return jsonify(result)

    @app.get('/api/coupons/pending')
    def coupon_pending():
        login_id = request.args.get('login_id', '')
        if unified.get_id(login_id) is None:
            raise CouponError('请选择共享账号池中的账号。', 'ACCOUNT_NOT_FOUND', 404)
        return jsonify(pending_orders=coupon_uses.pending(login_id))

    @app.post('/api/coupons/use/payment')
    def coupon_payment():
        payload = request.get_json()
        login_id = payload.get('login_id', '')
        if unified.get_id(login_id) is None:
            raise CouponError('请选择共享账号池中的账号。', 'ACCOUNT_NOT_FOUND', 404)
        result = coupon_uses.payment(login_id, payload.get('order_id', ''),
                                     refresh=payload.get('refresh', False),
                                     captcha_token=payload.get('captcha_token', ''))
        if result.get('state') in ('closed', 'verification_required'):
            if result.get('state') == 'verification_required':
                app.extensions['coupon_captcha_app_ids'].add(result['challenge']['app_id'])
            return jsonify(result)
        import base64
        import io
        import qrcode
        from qrcode.image.svg import SvgPathImage
        image = qrcode.make(result['payment_url'], image_factory=SvgPathImage, box_size=8, border=3)
        output = io.BytesIO()
        image.save(output)
        result['qr_image'] = 'data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode()
        return jsonify(result)
